"""家/公司标签确认工具：把常驻点坐标映射持久化，ETL 重跑后标签不丢。

用法（首次确认）：
    python -m gacore.langTrack.label_places
    # 输出 top 主点 + 高德地址，按提示输入每个点的标签（家/公司/未知）

配置文件：data/place_labels.json（gitignore 之外的用户数据）
ETL 每次重跑后读取该配置恢复标签。

标签文件格式（location v2 迁移后为 v3，本模块同时兼容读取 v1）：
- v1：{"<grid_key>": "<tag>"} 平铺字典（无设备维度，仅单设备语义）；
- v3：{"version": 3, "labels": [{"device_id", "place_id", "anchor_grid_key",
  "tag", "updated_at"}]}，主键 (device_id, place_id)；anchor_grid_key 仅作
  追溯，不参与任何匹配或更新。

两阶段切换（§2.4，location_migration.prepare_location_migration 编排）：
- prepare：正式文件备份为 *.v2_backup，迁移结果 fsync 写 *.v3.pending；
- activate COMMIT 后：finalize_label_swap / recover_pending_swap 用 pending
  原子替换正式文件，再以短事务写 status=complete。
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import tempfile
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"
CONFIG_PATH = Path(__file__).resolve().parents[3] / "data" / "place_labels.json"

LABEL_VERSION = 3
PENDING_SUFFIX = ".v3.pending"
BACKUP_SUFFIX = ".v2_backup"


class LabelFileError(RuntimeError):
    """标签文件格式/状态错误（迁移期坏文件必须显式失败，不静默吞掉）。"""


def now_cst() -> str:
    """东八区 ISO 时间戳（v3 label 的 updated_at）。"""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()


def parse_label_doc(raw: object) -> tuple[int, list[dict]]:
    """解析标签文件内容为 (version, rows)；rows 统一为 dict 列表。

    - v1 平铺 {grid_key: tag} → rows=[{grid_key, tag}]（无 device_id/place_id）；
    - v2 {"version":2,"labels":[{device_id,grid_key,tag}]}；
    - v3 {"version":3,"labels":[{device_id,place_id,anchor_grid_key,tag,updated_at}]}。
    非法结构 / 缺关键字段抛 LabelFileError。
    """
    if not isinstance(raw, dict):
        raise LabelFileError("label doc must be a JSON object")
    version = raw.get("version", 1)
    if version == 1:
        rows: list[dict] = []
        for gk, tag in raw.items():
            if gk == "version":
                continue
            if not isinstance(gk, str) or not isinstance(tag, str) or not gk:
                raise LabelFileError(f"invalid v1 label entry: {gk!r}")
            rows.append({"grid_key": gk, "tag": tag})
        return 1, rows
    if version not in (2, 3):
        raise LabelFileError(f"unsupported label version: {version!r}")
    labels = raw.get("labels")
    if not isinstance(labels, list):
        raise LabelFileError(f"v{version} doc must contain a labels list")
    out: list[dict] = []
    for i, r in enumerate(labels):
        if not isinstance(r, dict):
            raise LabelFileError(f"labels[{i}] must be an object")
        tag = r.get("tag")
        if not isinstance(tag, str) or not tag:
            raise LabelFileError(f"labels[{i}].tag must be a non-empty string")
        if version == 2:
            dev, gk = r.get("device_id"), r.get("grid_key")
            if not isinstance(dev, str) or not dev or not isinstance(gk, str) or not gk:
                raise LabelFileError(f"labels[{i}] requires device_id and grid_key")
            out.append({"device_id": dev, "grid_key": gk, "tag": tag})
        else:
            dev, pid = r.get("device_id"), r.get("place_id")
            if not isinstance(dev, str) or not dev or not isinstance(pid, str) or not pid:
                raise LabelFileError(f"labels[{i}] requires device_id and place_id")
            anchor = r.get("anchor_grid_key")
            out.append(
                {
                    "device_id": dev,
                    "place_id": pid,
                    "anchor_grid_key": anchor if isinstance(anchor, str) else None,
                    "tag": tag,
                    "updated_at": r.get("updated_at"),
                }
            )
    return version, out


def load_label_doc(path: Path) -> tuple[int, list[dict]]:
    """读取并解析标签文件（严格模式：坏 JSON / 坏结构抛 LabelFileError）。

    文件不存在返回 (0, [])。迁移编排用本函数；旧 v1 消费者继续用容错的
    load_labels()。
    """
    p = Path(path)
    if not p.exists():
        return 0, []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        raise LabelFileError(f"label file unreadable: {p}: {e}") from e
    return parse_label_doc(raw)


def _atomic_write(path: Path, text: str) -> None:
    """tmp 文件 + fsync + os.replace 原子写。"""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def labels_v3_doc(labels: list[dict]) -> dict:
    """构造 v3 标签文档（labels 为 parse_label_doc 的 v3 rows）。"""
    return {"version": LABEL_VERSION, "labels": labels}


def write_labels_v3_atomic(path: Path, labels: list[dict]) -> None:
    """原子写 v3 标签文件（已确认标签的最终落盘）。"""
    _atomic_write(Path(path), json.dumps(labels_v3_doc(labels), ensure_ascii=False, indent=2))


def write_labels_v3_pending(labels_path: Path, labels: list[dict]) -> Path:
    """两阶段切换第一步：备份正式文件并 fsync 写 pending。

    - 正式文件存在 → 拷贝为 <labels_path>.v2_backup（回滚恢复用）；
    - 迁移结果写 <labels_path>.v3.pending；正式文件保持不动。
    返回 pending 路径。
    """
    labels_path = Path(labels_path)
    if labels_path.exists():
        backup = labels_path.with_name(labels_path.name + BACKUP_SUFFIX)
        backup.write_bytes(labels_path.read_bytes())
    pending = labels_path.with_name(labels_path.name + PENDING_SUFFIX)
    _atomic_write(pending, json.dumps(labels_v3_doc(labels), ensure_ascii=False, indent=2))
    return pending


def swap_pending_labels(pending_path: Path, labels_path: Path) -> None:
    """两阶段切换第二步：pending 原子替换正式标签文件。"""
    pending_path, labels_path = Path(pending_path), Path(labels_path)
    if not pending_path.exists():
        raise LabelFileError(f"pending label file missing: {pending_path}")
    os.replace(pending_path, labels_path)


def restore_labels_backup(backup_path: Path, labels_path: Path) -> Path:
    """rollback 后恢复标签文件：v2_backup 原子替换正式文件（§2.4 rollback 步骤 5）。

    backup 不存在时抛 LabelFileError（回滚不允许静默丢备份）。
    """
    backup_path, labels_path = Path(backup_path), Path(labels_path)
    if not backup_path.exists():
        raise LabelFileError(f"label backup missing: {backup_path}")
    # 先 fsync 拷贝为临时文件再原子替换，保证正式文件要么旧 v1/v2 内容要么不变
    fd, tmp = tempfile.mkstemp(dir=str(labels_path.parent), prefix=labels_path.name, suffix=".restore.tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(backup_path.read_bytes())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, labels_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return labels_path


def project_labels_v3_from_db(conn: sqlite3.Connection) -> list[dict]:
    """从正式 places 表（v2 结构）的 label 列投影重建 v3 标签行。

    pending 文件丢失时的兜底恢复路径（§2.4 步骤 13）；v1 表无 place_id
    列时抛 LabelFileError（投影只在 v2 激活后有效）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(places)")}
    if "place_id" not in cols:
        raise LabelFileError("places table has no place_id column; v2 not activated?")
    rows = conn.execute(
        "SELECT device_id, place_id, grid_key, label FROM places "
        "WHERE label IS NOT NULL AND label != '未知'"
    ).fetchall()
    return [
        {
            "device_id": r[0],
            "place_id": r[1],
            "anchor_grid_key": r[2],
            "tag": r[3],
            "updated_at": now_cst(),
        }
        for r in rows
    ]


def apply_labels_v3(db_path: Path | str, labels: list[dict]) -> int:
    """ETL 后调用：按 v3 标签以 (device_id, place_id) 恢复人工 tag（v2 正式表）。

    anchor_grid_key 仅追溯，不参与更新。返回更新的行数。
    """
    conn = sqlite3.connect(db_path)
    n = 0
    try:
        for row in labels:
            cur = conn.execute(
                "UPDATE places SET label=?, updated_at=datetime('now','+8 hours') "
                "WHERE device_id=? AND place_id=?",
                (row["tag"], row["device_id"], row["place_id"]),
            )
            n += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def load_labels() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
    return {}


def save_labels(labels: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_labels(db_path: Path = DB_PATH) -> int:
    """ETL 后调用：按配置文件恢复标签。返回更新的点数。

    按"标签行形态"而非 DB 版本分支：
    - v1/v2 行（grid_key 定位）：UPDATE ... WHERE grid_key=?（v1/v2 places 均有该列）；
    - v3 行（device_id+place_id 定位）：UPDATE ... WHERE device_id=? AND place_id=?。
    """
    try:
        _, rows = load_label_doc(CONFIG_PATH)
    except LabelFileError as e:
        print(f"[label] 标签文件不可用，跳过恢复: {e}")
        return 0
    if not rows:
        return 0
    conn = sqlite3.connect(db_path)
    n = 0
    try:
        for row in rows:
            if "place_id" in row and "device_id" in row:
                cur = conn.execute(
                    "UPDATE places SET label=?, updated_at=datetime('now','+8 hours') "
                    "WHERE device_id=? AND place_id=?",
                    (row["tag"], row["device_id"], row["place_id"]),
                )
            else:
                gk = row.get("grid_key")
                if not gk:
                    continue
                if row.get("device_id"):
                    # v2 标签行（device_id + grid_key）：设备隔离，不波及他设备同网格地点
                    cur = conn.execute(
                        "UPDATE places SET label=? WHERE device_id=? AND grid_key=?",
                        (row["tag"], row["device_id"], gk),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE places SET label=? WHERE grid_key=?", (row["tag"], gk)
                    )
            n += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def confirm() -> None:
    """交互式确认：打印主点地址 + 家/公司置信度候选，让用户输入标签。

    候选制：ETL 推断的 candidate_label 仅作建议展示，用户确认前 label 保持中性
    （未知/常驻点），确认后写入 place_labels.json 持久化。

    P1-1 确认闭环：优先展示【待确认候选点】（label=未知 且 candidate_label 非空），
    用户确认后正式定名；随后展示已确认点供复核修改。

    双格式：v1 库写平铺 {grid_key: tag}；v2 库（user_version>=2）写 v3 doc
    （(device_id, place_id) 主键）。位置事实读取经 location_reader 双读层。
    """
    from gacore.langTrack import location_reader as lr
    from gacore.langTrack.etl_config import load_coord_systems, resolve_coord_system
    from gacore.langTrack.geocode import _amap_key, reverse_geocode

    key = _amap_key()
    coord_cfg = load_coord_systems()  # 坐标制配置错误时拒绝进入交互确认
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    v2 = lr.is_v2(conn)
    # 待确认候选点优先（P1-1 画像确认入口），再列已确认点供复核
    rows = lr.read_places(conn, candidate_only=True, limit=6)
    confirmed_rows = lr.read_places(conn, label_in=("家", "公司"), limit=4)
    conn.close()

    # 当前标签：v1 → {grid_key: tag}；v3 → {(device_id, place_id): row}
    try:
        _, label_rows = load_label_doc(CONFIG_PATH)
    except LabelFileError as e:
        print(f"[label] 标签文件不可读: {e}")
        return
    v3_rows = [r for r in label_rows if "place_id" in r and "device_id" in r]
    grid_labels = {r["grid_key"]: r["tag"] for r in label_rows if r.get("grid_key")}
    v3_map = {(r["device_id"], r["place_id"]): r for r in v3_rows}

    def _cur_tag(p: dict) -> str:
        if v2 and p.get("place_id"):
            row = v3_map.get((p["device_id"], p["place_id"]))
            return row["tag"] if row else "未知"
        return grid_labels.get(p["grid_key"], "未知")

    def _upsert(p: dict, tag: str) -> None:
        if v2 and p.get("place_id"):
            v3_map[(p["device_id"], p["place_id"])] = {
                "device_id": p["device_id"],
                "place_id": p["place_id"],
                "anchor_grid_key": p["grid_key"],
                "tag": tag,
                "updated_at": now_cst(),
            }
        else:
            grid_labels[p["grid_key"]] = tag

    print("=== 常驻点标签确认 ===")
    print("输入标签: 家 / 公司 / 未知（直接回车=保持不变）")
    print("候选为 ETL 置信度推断，确认后正式写入画像\n")

    if not rows and not confirmed_rows:
        print("（无常驻点可确认）")
        return

    # 多设备库候选按 (device_id, place_id) 独立展示，输出加设备段避免混淆
    devices = {p["device_id"] for p in rows} | {p["device_id"] for p in confirmed_rows}
    show_dev = len(devices) > 1

    def _dev_part(p: dict) -> str:
        return f" ·设备{p['device_id']}" if show_dev else ""

    if rows:
        print("-- 待确认候选（ETL 推断，请确认是否定名） --")
        for r in rows:
            # §3.3：label reverse 按点位 (device_id, first_seen) 解析坐标制后转换
            cs = resolve_coord_system(r["device_id"], r["first_seen"] or 0, coord_cfg)
            info = reverse_geocode(r["lat"], r["lon"], key, coord_system=cs)
            addr = info.get("formatted", "") if info else (r["address"] or "")
            poi = info.get("poi", "") if info else (r["poi"] or "")
            cur = _cur_tag(r)
            cand = r["candidate_label"] or "-"
            conf = f"(家 {r['confidence_home']:.2f} / 公司 {r['confidence_work']:.2f})"
            print(f"  ({r['lat']:.4f},{r['lon']:.4f}){_dev_part(r)} 访问{r['visit_count']}次 候选:{cand} {conf}")
            print(f"    {poi} | {addr}")
            answer = input(f"    标签 [{cur}] (家/公司/未知/回车): ").strip()
            if answer in ("家", "公司", "未知"):
                _upsert(r, answer)

    if confirmed_rows:
        print("\n-- 已确认点（可复核修改） --")
        for r in confirmed_rows:
            poi = r["poi"] or ""
            cur = _cur_tag(r)
            conf = f"(家 {r['confidence_home']:.2f} / 公司 {r['confidence_work']:.2f})"
            print(f"  [{r['label']}] ({r['lat']:.4f},{r['lon']:.4f}){_dev_part(r)} 访问{r['visit_count']}次 {conf} {poi}")
            answer = input(f"    标签 [{cur}] (家/公司/未知/回车): ").strip()
            if answer in ("家", "公司", "未知"):
                _upsert(r, answer)

    if v2:
        write_labels_v3_atomic(CONFIG_PATH, list(v3_map.values()))
        n = apply_labels_v3(DB_PATH, list(v3_map.values()))
    else:
        save_labels(grid_labels)
        n = apply_labels()
    print(f"\n已保存 {len(v3_map) if v2 else len(grid_labels)} 个标签, 更新 {n} 个常驻点")


def main() -> None:
    confirm()


if __name__ == "__main__":
    main()
