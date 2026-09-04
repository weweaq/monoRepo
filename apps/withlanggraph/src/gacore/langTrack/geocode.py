"""高德逆地理编码（L2 语义化）：把 places 表未编码常驻点 → 语义字段全量落库。

改造要点（v0.2 P0）：
- regeo(extensions=all)：一次拿到 行政区/门牌号/POI 列表/AOI/道路/商圈，全部落库
- 批量编码：regeo 支持一次传 20 点（| 分隔）；单请求响应过大/失败时自动降级 10/5/1 点重试
- 增量缓存：只对 geocoded_at IS NULL 的常驻点编码，ETL 重跑零新增调用
- 先 regeo 命中、再 around 兜底：regeo 无 POI 时才调周边搜索补一次
- 行为语义：POI type 六位码前两位 → 行为标签（behavior）

用法：
    python -m gacore.langTrack.geocode              # 增量编码（只编码未编码点）
    python -m gacore.langTrack.geocode --all        # 强制全部重编码
    python -m gacore.langTrack.geocode --label 家   # 给已编码结果统一标"家"（强制标注）

依赖环境变量 AMAP_KEY（.env 中配置，高德 Web 服务 Key）。
版权合规：结果展示需标注"高德地图"。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from gacore.langTrack.location_facts import to_amap_coord, warn_unknown_coord_once

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"
REVERSED_GEO_URL = "https://restapi.amap.com/v3/geocode/regeo"
AROUND_URL = "https://restapi.amap.com/v3/place/around"

# 批量档位：默认 20 点/次（高德上限），响应过大/失败逐级降级
BATCH_SIZES = (20, 10, 5, 1)

# POI type 中文大类（高德 regeo 返回 type 形如"餐饮服务;中餐厅;中餐馆"，
# 第一段即大类中文名，非六位数字码）→ 行为语义（画像叙事用）
BEHAVIOR_MAP = {
    "汽车服务": "汽车服务",
    "汽车销售": "汽车销售",
    "汽车维修": "汽车维修",
    "摩托车服务": "摩托车服务",
    "餐饮服务": "用餐",
    "购物服务": "购物",
    "生活服务": "办事/日常",
    "体育休闲服务": "健身/娱乐",
    "医疗保健服务": "就医",
    "住宿服务": "住宿/过夜",
    "风景名胜": "游玩",
    "商务住宅": "住宅/楼宇",
    "政府机构及社会团体": "办事",
    "科教文化服务": "学习/文化活动",
    "交通设施服务": "出行中转",
    "金融保险服务": "银行/金融",
    "公司企业": "办公",
    "道路附属设施": "道路设施",
    "地名地址信息": "地名地址",
    "公共设施": "公共设施",
    "事件活动": "事件活动",
    "室内设施": "室内设施",
}


# POI 名称品牌/类型硬信号（P1-2：name 命中即强化行为语义，弥补 type 大类映射的盲区）
SIGNAL_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("医院", "就医"),
    ("诊所", "就医"),
    ("学校", "学习/文化活动"),
    ("大学", "学习/文化活动"),
    ("学院", "学习/文化活动"),
    ("中学", "学习/文化活动"),
    ("小学", "学习/文化活动"),
    ("图书馆", "学习/文化活动"),
    ("政府", "办事"),
    ("政务", "办事"),
    ("派出所", "办事"),
    ("税务局", "办事"),
    ("地铁", "出行中转"),
    ("公交", "出行中转"),
    ("车站", "出行中转"),
    ("银行", "银行/金融"),
    ("商场", "购物"),
    ("购物中心", "购物"),
    ("超市", "购物"),
    ("影院", "娱乐"),
    ("电影院", "娱乐"),
    ("KTV", "娱乐"),
    ("公园", "游玩"),
    ("景区", "游玩"),
    ("酒店", "住宿/过夜"),
    ("健身", "健身/娱乐"),
    ("体育馆", "健身/娱乐"),
    ("体育场", "健身/娱乐"),
)


def _amap_key() -> str:
    key = os.environ.get("AMAP_KEY", "")
    if not key:
        # 尝试从 .env 读取：直接按字节找 AMAP_KEY=（ASCII 前缀不受文件编码影响）
        env_path = Path(__file__).resolve().parents[3] / ".env"
        if env_path.exists():
            raw = env_path.read_bytes()
            marker = b"AMAP_KEY="
            idx = raw.find(marker)
            if idx >= 0:
                rest = raw[idx + len(marker):]
                end = rest.find(b"\n")
                if end >= 0:
                    rest = rest[:end]
                key = rest.decode("utf-8", errors="ignore").strip().strip('"').strip("'")
    if not key:
        raise SystemExit("[geocode] 未配置 AMAP_KEY（.env 中设置，或环境变量）")
    return key


def _to_amap(lat: float, lon: float, coord_system: str) -> tuple[float, float]:
    """入口统一转换 + unknown 警告（§3.3：所有高德入口收到的坐标先经 to_amap_coord）。"""
    if (coord_system or "unknown").strip().lower() == "unknown":
        warn_unknown_coord_once("geocode")
    return to_amap_coord(lat, lon, coord_system)


def behavior_of(poi_type: str, poi_name: str = "") -> str:
    """POI type（中文大类）→ 行为语义。type 形如 '交通设施服务;地铁站;地铁站'，取第一段。

    P1-2 增强：POI 名称硬信号（品牌/类型关键词）优先于 type 大类映射——
    如名称含"医院/学校/地铁"等时直接采用对应行为，弥补大类映射盲区。
    """
    if poi_name:
        for _kw, beh in SIGNAL_KEYWORDS:
            if _kw in poi_name:
                return beh
    if not poi_type:
        return "未知"
    head = poi_type.split(";")[0].strip()
    return BEHAVIOR_MAP.get(head, "未知")


def _parse_regeocode(rc: dict) -> dict:
    """解析单个点的 regeocode（兼容批量 regeocodes[i].regeocode 与单点 regeocode 两种结构）。"""
    inner = rc.get("regeocode") or rc
    formatted = inner.get("formatted_address", "")
    address = inner.get("addressComponent", {})
    pois = inner.get("pois") or []
    aois = inner.get("aois") or []
    business = inner.get("businessAreas") or []
    roads = inner.get("roads") or []
    poi = pois[0] if pois else {}
    aoi = aois[0] if aois else {}
    poi_type = poi.get("type", "") or ""
    # 语义匹配粒度：POI > AOI > 道路 > 行政区
    if poi:
        matched_level = "POI"
    elif aoi:
        matched_level = "AOI"
    elif roads:
        matched_level = "道路"
    else:
        matched_level = "行政区"
    # P1-2 POI 三级语义：高德 type 为"大类;中类;细类"（如"餐饮服务;中餐厅;中餐馆"）
    type_levels = [t.strip() for t in poi_type.split(";") if t.strip()] if poi_type else []
    poi_l1 = type_levels[0] if len(type_levels) > 0 else ""
    poi_l2 = type_levels[1] if len(type_levels) > 1 else ""
    poi_l3 = type_levels[2] if len(type_levels) > 2 else ""
    # P1-2 品牌/名称硬信号：POI 名称命中关键词 → 强化行为语义
    poi_signal = ""
    name = poi.get("name", "") or ""
    for kw, _beh in SIGNAL_KEYWORDS:
        if kw in name:
            poi_signal = kw
            break
    district = address.get("district", "")
    township = address.get("township", "")
    # P1-2 无 POI 兜底：AOI / 道路 / 行政区拼接成人话描述
    if poi:
        fallback_desc = f"{name}（{district}{township}）" if district or township else name
    else:
        parts: list[str] = []
        if aois:
            parts.append(aois[0].get("name", ""))
        if roads:
            parts.append(roads[0].get("name", ""))
        if district or township:
            parts.append(f"{district}{township}")
        fallback_desc = "；".join(p for p in parts if p) or formatted
    return {
        "formatted": formatted,
        "poi": name,
        "poi_type": poi_type,
        "poi_l1": poi_l1,
        "poi_l2": poi_l2,
        "poi_l3": poi_l3,
        "poi_signal": poi_signal,
        "poi_fallback": fallback_desc,
        "province": address.get("province", ""),
        "city": address.get("city", "") or address.get("province", ""),
        "district": district,
        "township": township,
        "business_area": (business[0].get("name", "") if business else ""),
        "road": (roads[0].get("name", "") if roads else ""),
        "aoi": aoi.get("name", ""),
        "matched_level": matched_level,
    }


# 高德 regeo 的"多点批量"在多数 Key（含当前 Key）下实测不生效：
# 无论 location 传几个点（base/all），返回都是单点 regeocode 结构（只覆盖首点）。
# 因此采用"探测式批量"：首次探测发现不支持 → 永久降级逐点单请求，并缓存结论避免反复探测。
_SUPPORT_BATCH: bool | None = None


def _probe_batch_support(key: str) -> bool:
    """探测当前 Key 是否真正支持多点批量 regeo。结果模块级缓存，只探测一次。"""
    global _SUPPORT_BATCH
    if _SUPPORT_BATCH is not None:
        return _SUPPORT_BATCH
    pts = [(31.99, 118.78), (31.99 + 0.001, 118.78)]
    # 探测点为硬编码测试坐标，固定按 gcj02 原样放行（identity），避免误触发
    # unknown 坐标制警告——业务数据是否声明坐标系与探测无关
    out, failed = _regeo_request(pts, key, coord_system="gcj02")
    _SUPPORT_BATCH = (not failed)
    if not _SUPPORT_BATCH:
        print("[geocode] 当前 Key 不支持多点批量 regeo（返回单点结构），已降级为逐点编码")
    return _SUPPORT_BATCH


def _regeo_one(lat: float, lon: float, key: str, coord_system: str = "unknown") -> dict | None:
    """单点 regeo(extensions=all)，返回完整语义 dict；失败返回 None。

    坐标按 coord_system 经 to_amap_coord 转换后请求（§3.3）。
    """
    lat, lon = _to_amap(lat, lon, coord_system)
    params = urllib.parse.urlencode({
        "location": f"{lon},{lat}",
        "key": key,
        "extensions": "all",
        "radius": "500",
    })
    try:
        with urllib.request.urlopen(f"{REVERSED_GEO_URL}?{params}", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[geocode] 单点请求失败 ({lat},{lon}): {e}")
        return None
    if data.get("status") != "1":
        return None
    info = _parse_regeocode(data.get("regeocode") or {})
    return info if info.get("formatted") else None


def _regeo_request(
    points: list[tuple[float, float]], key: str, coord_system: str = "unknown"
) -> tuple[dict, list[int]]:
    """请求一组点（批量 regeo）。返回 ({idx: info}, 失败idx列表)。

    组内各点按 coord_system 统一转换（§3.3；调用方保证同组同源坐标系）。
    """
    if not points:
        return {}, []
    pts = [_to_amap(lat, lon, coord_system) for lat, lon in points]
    locs = "|".join(f"{lon},{lat}" for lat, lon in pts)
    params = urllib.parse.urlencode({
        "location": locs,
        "key": key,
        "extensions": "all",
        "radius": "500",
    })
    try:
        with urllib.request.urlopen(f"{REVERSED_GEO_URL}?{params}", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[geocode] 批量请求失败({len(points)}点): {e}")
        return {}, list(range(len(points)))
    if data.get("status") != "1":
        return {}, list(range(len(points)))
    regeocodes = data.get("regeocodes")
    if not isinstance(regeocodes, list):
        # 不支持批量：返回的是单点 regeocode（只覆盖首点），不可信 → 全部视为失败
        return {}, list(range(len(points)))
    out: dict[int, dict] = {}
    failed: list[int] = []
    for i, rc in enumerate(regeocodes):
        if not rc:
            failed.append(i)
            continue
        info = _parse_regeocode(rc)
        if info.get("formatted"):
            out[i] = info
        else:
            failed.append(i)
    # 响应数量不足（被截断/超限）也视为失败
    for i in range(len(points)):
        if i not in out:
            failed.append(i)
    return out, failed


def batch_reverse_geocode(
    points: list[tuple[float, float]], key: str, coord_system: str = "unknown"
) -> tuple[dict, list[int]]:
    """高德 regeo 编码入口：优先多点批量（20 点/次，失败按 10/5/1 降级）；
    若 Key 不支持批量（实测返回单点结构），自动降级为逐点单请求。

    本批点须同源坐标系（coord_system 逐组传递；混合来源由调用方分组，
    见 incremental_encode）。返回 ({idx: info}, 最终失败 idx 列表)。
    不抛异常，失败点留待下次增量重试。
    """
    results: dict[int, dict] = {}
    pending: list[tuple[int, tuple[float, float]]] = list(enumerate(points))
    if not pending:
        return results, []
    support_batch = _probe_batch_support(key)
    if not support_batch:
        # 逐点单请求：每次 1 点，可靠性最高；成本受增量缓存控制（每天仅新增点）
        failed: list[int] = []
        for original_idx, (lat, lon) in pending:
            info = _regeo_one(lat, lon, key, coord_system)
            if info:
                results[original_idx] = info
            else:
                failed.append(original_idx)
        return results, failed
    for batch_size in BATCH_SIZES:
        if not pending:
            break
        # 按 batch_size 分组
        groups = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
        still_failed: list[tuple[int, tuple[float, float]]] = []
        for group in groups:
            idxs = [p[0] for p in group]
            pts = [p[1] for p in group]
            out, failed_local = _regeo_request(pts, key, coord_system)
            for j, original_idx in enumerate(idxs):
                if j in out:
                    results[original_idx] = out[j]
                else:
                    still_failed.append((original_idx, pts[j]))
        pending = still_failed
    failed = [p[0] for p in pending]
    return results, failed


def reverse_geocode(lat: float, lon: float, key: str, coord_system: str = "unknown") -> dict | None:
    """单点 regeo(extensions=all)，返回完整语义 dict（供 label_places 交互确认展示）。

    坐标按 coord_system 经 to_amap_coord 转换后请求（§3.3）。
    """
    out, failed = batch_reverse_geocode([(lat, lon)], key, coord_system)
    if failed:
        return None
    return out.get(0)


def around_search(
    lat: float, lon: float, key: str, radius: int = 500, coord_system: str = "unknown"
) -> dict | None:
    """周边搜索兜底：regeo 无 POI 时按坐标检索最近 POI 补行为语义。

    坐标按 coord_system 经 to_amap_coord 转换后请求（§3.3）。
    extensions=all 的 POI 才带商圈字段 business_area（下划线）；base 仅 businessArea（驼峰）且常为空。
    """
    lat, lon = _to_amap(lat, lon, coord_system)
    params = urllib.parse.urlencode({
        "location": f"{lon},{lat}",
        "key": key,
        "radius": str(radius),
        "extensions": "all",
    })
    try:
        with urllib.request.urlopen(f"{AROUND_URL}?{params}", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[geocode] 周边搜索失败 ({lat},{lon}): {e}")
        return None
    if data.get("status") != "1":
        return None
    pois = data.get("pois") or []
    if not pois:
        return None
    p = pois[0]
    ba = next((_p.get("business_area") or _p.get("businessArea") or "" for _p in pois), "")
    return {"poi": p.get("name", ""), "poi_type": p.get("type", ""), "business_area": ba}


def infer_label(info: dict | None) -> str:
    """根据逆编码结果推断常驻点语义（保留兼容；家/公司改由置信度候选系统决定）。"""
    if not info:
        return "未知"
    text = " ".join([info.get("poi", ""), info.get("formatted", "")])
    home_kw = ["小区", "家园", "公寓", "新村", "住宅", "栋"]
    work_kw = ["科技园", "大厦", "中心", "产业园", "写字楼", "广场", "软件园", "金融城"]
    if any(k in text for k in work_kw):
        return "公司"
    if any(k in text for k in home_kw):
        return "家"
    return "未知"


def incremental_encode(db_path: Path = DB_PATH, force_all: bool = False) -> int:
    """增量编码：只对 geocoded_at IS NULL 的常驻点调用高德 regeo（force_all 则全部重编）。

    语义字段全量落库；regeo 无 POI 时 around 兜底补一次；成功才标 geocoded_at，
    失败点不标记、下次 ETL 重跑自动重试。ETL 重跑时无新增点 → 零 API 调用。
    每点坐标系按 (device_id, first_seen) 从配置解析，按坐标制分组批量编码
    （§3.3：混合来源不共用一个转换参数）。返回成功编码数。
    """
    from gacore.langTrack.etl_config import load_coord_systems, resolve_coord_system

    key = _amap_key()
    coord_cfg = load_coord_systems()  # 配置错误（重叠 period 等）直接拒绝本次编码
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if force_all:
        rows = conn.execute(
            "SELECT id, device_id, first_seen, lat, lon, grid_key FROM places"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, device_id, first_seen, lat, lon, grid_key FROM places "
            "WHERE geocoded_at IS NULL"
        ).fetchall()
    if not rows:
        print("[geocode] 无新增待编码常驻点（增量缓存命中，零调用）")
        conn.close()
        return 0
    print(f"[geocode] 待编码常驻点: {len(rows)} 个")

    row_cs = [
        resolve_coord_system(r["device_id"], r["first_seen"] or 0, coord_cfg) for r in rows
    ]
    groups: dict[str, list[int]] = {}
    for i, cs in enumerate(row_cs):
        groups.setdefault(cs, []).append(i)
    results: dict[int, dict] = {}
    failed: list[int] = []
    for cs, idxs in groups.items():
        pts = [(rows[i]["lat"], rows[i]["lon"]) for i in idxs]
        out, failed_local = batch_reverse_geocode(pts, key, cs)
        for j, original_idx in enumerate(idxs):
            if j in out:
                results[original_idx] = out[j]
        failed.extend(idxs[j] for j in failed_local)

    now = int(time.time() * 1000)
    n = 0
    for i, r in enumerate(rows):
        if i not in results:
            continue
        info = results[i]
        # around 兜底：regeo 无 POI 时补一次周边搜索
        if not info.get("poi"):
            around = around_search(r["lat"], r["lon"], key, coord_system=row_cs[i])
            if around:
                info.update(around)
                # 周边搜索返回 base 结构，补充三级/信号/兜底字段
                tl = [t.strip() for t in (info.get("poi_type") or "").split(";") if t.strip()]
                info.setdefault("poi_l1", tl[0] if len(tl) > 0 else "")
                info.setdefault("poi_l2", tl[1] if len(tl) > 1 else "")
                info.setdefault("poi_l3", tl[2] if len(tl) > 2 else "")
                sig = ""
                for _kw, _b in SIGNAL_KEYWORDS:
                    if _kw in (info.get("poi") or ""):
                        sig = _kw
                        break
                info.setdefault("poi_signal", sig)
                info.setdefault("poi_fallback", info.get("poi") or "")
        behavior = behavior_of(info.get("poi_type", ""), info.get("poi", ""))
        conn.execute(
            "UPDATE places SET address=?, poi=?, district=?, township=?, business_area=?, "
            "poi_type=?, matched_level=?, behavior=?, poi_l1=?, poi_l2=?, poi_l3=?, "
            "poi_signal=?, poi_fallback=?, geocoded_at=? WHERE id=?",
            (
                info.get("formatted", ""), info.get("poi", ""), info.get("district", ""),
                info.get("township", ""), info.get("business_area", ""),
                info.get("poi_type", ""), info.get("matched_level", ""),
                behavior, info.get("poi_l1", ""), info.get("poi_l2", ""),
                info.get("poi_l3", ""), info.get("poi_signal", ""),
                info.get("poi_fallback", ""), now, r["id"],
            ),
        )
        n += 1
    conn.commit()
    conn.close()
    if failed:
        print(f"[geocode] 失败 {len(failed)} 点（未标 geocoded_at，下次重试）")
    print(f"[geocode] 完成，成功编码 {n} 个常驻点")
    return n


def refresh_behavior(db_path: Path = DB_PATH) -> int:
    """刷新已编码点的 behavior（不调 API）：基于现有 poi_type 中文大类 + 名称硬信号重算。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, poi_type, poi FROM places WHERE poi IS NOT NULL AND poi != ''"
    ).fetchall()
    n = 0
    for pid, pt, pn in rows:
        b = behavior_of(pt, pn)
        conn.execute("UPDATE places SET behavior=? WHERE id=?", (b, pid))
        n += 1
    conn.commit()
    conn.close()
    return n


def enrich_business_area(db_path: Path = DB_PATH) -> int:
    """P2-1 商圈补充：对已编码、business_area 为空且非住宅/楼宇/办公的活动类常驻点，
    用 around 周边搜索补 business_area（不覆盖已有值）。低频增量，仅首次需配额。

    每点坐标系按 (device_id, first_seen) 从配置解析（§3.3）。
    """
    from gacore.langTrack.etl_config import load_coord_systems, resolve_coord_system

    key = _amap_key()
    coord_cfg = load_coord_systems()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, device_id, first_seen, lat, lon, poi, behavior FROM places "
        "WHERE geocoded_at IS NOT NULL AND (business_area IS NULL OR business_area='') "
        "AND behavior IS NOT NULL AND behavior != '' "
        "AND behavior NOT IN ('住宅/楼宇', '办公')"
    ).fetchall()
    if not rows:
        print("[geocode] 无待补商圈的消费/活动点")
        conn.close()
        return 0
    print(f"[geocode] 待补商圈点: {len(rows)} 个")
    n = 0
    for r in rows:
        ba = ""
        cs = resolve_coord_system(r["device_id"], r["first_seen"] or 0, coord_cfg)
        around = around_search(r["lat"], r["lon"], key, radius=800, coord_system=cs)
        if around and around.get("business_area"):
            ba = around["business_area"]
        conn.execute("UPDATE places SET business_area=? WHERE id=?", (ba or None, r["id"]))
        if ba:
            n += 1
    conn.commit()
    conn.close()
    print(f"[geocode] 商圈补充完成: {n} 个点获得商圈名")
    return n


def run(db_path: Path = DB_PATH, label: str | None = None, force_all: bool = False) -> None:
    key = _amap_key()  # 提前校验 key，避免走到一半才发现缺配置
    n = incremental_encode(db_path, force_all=force_all)
    if label:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "UPDATE places SET label=? WHERE geocoded_at IS NOT NULL", (label,)
        )
        conn.commit()
        conn.close()
        print(f"[geocode] 强制标注 [{label}]: {cur.rowcount} 个已编码常驻点")


def main() -> None:
    parser = argparse.ArgumentParser(description="高德逆地理编码（L2 语义化增量编码）")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--all", action="store_true", help="强制全部重编码（忽略增量缓存）")
    parser.add_argument("--label", type=str, default=None,
                        help="给已编码常驻点强制统一标标签（如 家/公司），不传则不覆盖 label")
    parser.add_argument("--rebehavior", action="store_true",
                        help="仅刷新已编码点的 behavior（不调 API），基于 poi_type 中文大类重算")
    parser.add_argument("--enrich-business", action="store_true",
                        help="P2-1：对消费/活动类常驻点用周边搜索补商圈(business_area)")
    args = parser.parse_args()
    if args.rebehavior:
        n = refresh_behavior(args.db)
        print(f"[geocode] 刷新 behavior 完成: {n} 条")
        return
    if args.enrich_business:
        n = enrich_business_area(args.db)
        print(f"[geocode] 商圈补充完成: {n} 条")
        return
    run(args.db, args.label, args.all)


if __name__ == "__main__":
    main()
