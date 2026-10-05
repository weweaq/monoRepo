"""区县背景配置 + 语义地点编辑器数据层（v3.5 重构）。

v3.4 曾把地点 tag/note 放本配置文件（显示层叠加）；v3.5 按用户拍板改为
**数据库即真源**：自定义标签与地点 note 由编辑器直写 places.label /
places.note 列（迁移见 location_reader.ensure_note_column），本文件不再承担
地点级语义，只保留**区县背景 note**（按区县共享的低频长文）。

配置格式（data/place_semantics.json，gitignore 内用户数据）：
    {"districts": [{"district": "当涂县", "note": "（手写人文/背景注记）"}]}

读取容错：文件缺失/坏 JSON/坏结构 → 空配置（显示层无叠加，不抛异常挡卡）；
旧版含 places/roles 键的文档兼容读取（忽略未知键）。mtime 缓存改完即生效。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[3] / "data" / "place_semantics.json"
DB_PATH = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"

_EMPTY: dict = {"districts": []}
_cache_path: Path | None = None
_cache_mtime: float | None = None
_cache_doc: dict = _EMPTY


def normalize_doc(raw: object) -> dict:
    """规范化配置文档：只认 districts（旧版 places/roles 键忽略），非法条目跳过。"""
    if not isinstance(raw, dict):
        return _EMPTY
    districts: list[dict] = []
    for r in raw.get("districts") or []:
        if not isinstance(r, dict):
            continue
        name = r.get("district")
        if not isinstance(name, str) or not name:
            continue
        districts.append({
            "district": name,
            "note": r.get("note") if isinstance(r.get("note"), str) else "",
        })
    return {"districts": districts}


def _load_doc(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _EMPTY
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return _EMPTY
    return normalize_doc(raw)


def load(path: Path | str | None = None) -> dict:
    """加载配置（mtime 缓存）；path 可注入（测试隔离）。"""
    global _cache_path, _cache_mtime, _cache_doc
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return _EMPTY
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _EMPTY
    if p == _cache_path and mtime == _cache_mtime:
        return _cache_doc
    doc = _load_doc(p)
    _cache_path, _cache_mtime, _cache_doc = p, mtime, doc
    return doc


def save_doc(doc: dict, path: Path | str | None = None) -> None:
    """规范化后原子写配置文档（编辑器保存入口），并清 mtime 缓存。"""
    p = Path(path) if path else CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = normalize_doc(doc)
    text = json.dumps(clean, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    global _cache_path, _cache_mtime, _cache_doc
    _cache_path, _cache_mtime, _cache_doc = None, None, _EMPTY


def district_note(district: str, cfg: dict | None = None) -> str:
    """区县背景注记，无则空串。"""
    doc = cfg if cfg is not None else load()
    d = (district or "").strip()
    if not d:
        return ""
    for r in doc["districts"]:
        if r["district"] == d:
            return r["note"]
    return ""


def save_district_notes(pairs: list[dict], path: Path | str | None = None) -> int:
    """编辑器保存区县背景：整表替换（页面发全量）。返回条数。"""
    doc = {"districts": list(pairs or [])}
    save_doc(doc, path)
    return len(doc["districts"])


# ---------------------------------------------------------------------------
# 编辑器数据层（/places 页）：places 全量 + 近 14 天出现线索 + 区县聚合
# ---------------------------------------------------------------------------

_RECENT_DAYS = 14


def editor_data(db_path: Path | str = DB_PATH, config_path: Path | str | None = None) -> dict:
    """收集 /places 页所需数据（纯读；db 缺失/缺表降级空态不抛）。

    返回 {places, districts, home_district, recent_days, db_note}。
    places 按访问次数降序；每点带 last_day（最近停留日）与
    recent（近 14 天 [{day, n}]，供"出现在哪几天"线索）。
    """
    import sqlite3

    from gacore.langTrack.location_reader import ensure_note_column, is_v2, table_columns

    db_path = Path(db_path)
    out: dict = {
        "places": [], "districts": [], "home_district": "",
        "recent_days": [], "db_note": "",
    }
    if not db_path.exists():
        out["db_note"] = "data/langTrack.db 不存在（无位置数据可编辑）"
        out["districts"] = load(config_path)["districts"]
        return out
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            ensure_note_column(conn)
        except sqlite3.OperationalError:
            pass  # 只读连接无法迁移：note 列缺失时该字段为空，页面仍可用
        cols = table_columns(conn, "places")
        if not cols:
            out["db_note"] = "places 表缺失（库尚未跑过 ETL）"
            out["districts"] = load(config_path)["districts"]
            return out
        sel_pid = "place_id" if "place_id" in cols else "NULL AS place_id"
        sel_note = "note" if "note" in cols else "NULL AS note"
        sel_poi = "poi" if "poi" in cols else "'' AS poi"
        sel_addr = "address" if "address" in cols else "'' AS address"
        sel_dist = "district" if "district" in cols else "'' AS district"
        sel_town = "township" if "township" in cols else "'' AS township"
        rows = conn.execute(
            f"SELECT {sel_pid}, device_id, grid_key, lat, lon, label, "
            f"first_seen, last_seen, visit_count, {sel_poi}, {sel_addr}, "
            f"{sel_dist}, {sel_town}, {sel_note} "
            "FROM places ORDER BY visit_count DESC"
        ).fetchall()
        v2 = is_v2(conn)
        places = []
        for r in rows:
            places.append({
                "place_id": r["place_id"] or "",
                "device_id": r["device_id"] or "",
                "grid_key": r["grid_key"] or "",
                "lat": round(float(r["lat"]), 6),
                "lon": round(float(r["lon"]), 6),
                "label": r["label"] or "未知",
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "vc": int(r["visit_count"] or 0),
                "poi": r["poi"] or "",
                "address": r["address"] or "",
                "district": r["district"] or "",
                "township": r["township"] or "",
                "note": r["note"] or "",
                "v2": v2,
            })
        for p in places:
            if (p["label"]) == "家" and p["district"]:
                out["home_district"] = p["district"]
                break
        # 近 14 天出现线索：stay 段数按 (place_id 优先, grid_key 兜底) 归点
        import datetime as _dt

        tz = _dt.timezone(_dt.timedelta(hours=8))
        today = _dt.datetime.now(tz).date()
        days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(_RECENT_DAYS - 1, -1, -1)]
        out["recent_days"] = days
        try:
            stay_cols = {c[1] for c in conn.execute("PRAGMA table_info(stays)")}
            if v2 and "place_id" in stay_cols:
                key_expr = "place_id"
            else:
                key_expr = "grid_key"
            srows = conn.execute(
                f"SELECT {key_expr} AS k, day, COUNT(*) AS n FROM stays "
                "WHERE day IS NOT NULL AND day != '' GROUP BY k, day"
            ).fetchall()
            by_place: dict[str, dict[str, int]] = {}
            for r in srows:
                by_place.setdefault(r["k"] or "", {})[r["day"]] = int(r["n"])
            for p in places:
                k = p["place_id"] if (v2 and p["place_id"]) else p["grid_key"]
                dmap = by_place.get(k, {})
                p["last_day"] = max(dmap, default="")
                p["recent"] = [
                    {"day": d, "n": dmap.get(d, 0)} for d in days if dmap.get(d)
                ]
        except sqlite3.OperationalError:
            for p in places:
                p["last_day"], p["recent"] = "", []
        out["places"] = places
        dcount: dict[str, int] = {}
        for p in places:
            if p["district"]:
                dcount[p["district"]] = dcount.get(p["district"], 0) + 1
        doc = load(config_path)
        dnote = {r["district"]: r["note"] for r in doc["districts"]}
        out["districts"] = [
            {"district": d, "n": n, "note": dnote.get(d, "")}
            for d, n in sorted(dcount.items(), key=lambda kv: -kv[1])
        ]
    finally:
        conn.close()
    return out


def save_place_labels(db_path: Path | str, items: list[dict]) -> int:
    """编辑器保存：直写 places.label / places.note（用户自定义标签即真源）。

    匹配 place_id 优先、poi 兜底；label ≤24 字、note ≤500 字（超长截断）。
    计算逻辑按精确值"家"/"公司"识别，其余标签纯显示与聚合。返回更新行数。
    """
    import sqlite3

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        from gacore.langTrack.location_reader import ensure_note_column

        ensure_note_column(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(places)")}
        if not cols:
            raise ValueError("places 表不存在（先跑 ETL）")
        n = 0
        for it in items:
            label = str(it.get("label") or "")[:24].strip()
            note = str(it.get("note") or "")[:500].strip()
            pid = str(it.get("place_id") or "")
            poi = str(it.get("poi") or "")
            if not label and not note:
                continue
            # SET 按实际列拼（updated_at 非所有库都有；R6 时间戳列存在则必刷）
            sets = ["label=?", "note=?"]
            args: list = [label or "未知", note]
            if "updated_at" in cols:
                sets.append("updated_at=datetime('now','+8 hours')")
            if pid and "place_id" in cols:
                cur = conn.execute(
                    f"UPDATE places SET {', '.join(sets)} WHERE place_id=?",
                    [*args, pid],
                )
            elif poi and "poi" in cols:
                cur = conn.execute(
                    f"UPDATE places SET {', '.join(sets)} WHERE poi=?",
                    [*args, poi],
                )
            else:
                continue
            n += cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()
