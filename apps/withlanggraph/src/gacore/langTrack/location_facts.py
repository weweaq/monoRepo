"""location_facts.py —— 位置智能 canonical 纯算法层（零 DB / 文件 / 网络访问）。

本模块只负责“给定规范化坐标点 / 停留段，算出客观事实”，不触碰任何外部 IO：

- :class:`LocationPoint`：规范化坐标点（内存对象，不落 raw 表）。
- :func:`parse_location_point`：从事件 payload 解析坐标点（白名单键 + 严格数值校验）。
- :func:`quality_stats`：按设备聚合坐标质量统计（provider / accuracy 桶 / 30 分钟格 / 采样间隔）。
- :func:`cluster_stays`：canonical place 确定性几何聚类（§2.3 规则 1-6）。
- :func:`canonical_places`：cluster + 稳定 place_id 生成（§2.3 规则 9/12）。
- :func:`match_old_new`：新旧 place 全局一对一 matching（§2.3 规则 7-11，旧 ID 认领）。
- :func:`resolve_place_name` / :func:`format_place`：地点显示契约（§2.6）。
- :func:`to_amap_coord`：WGS84 → GCJ02 近似转换（未知坐标系原样放行）。
- :func:`location_points` / :func:`accuracy_filter` / :func:`daily_quality_rows`：
  Task 6 共用解析 / §3.1 精度过滤 / §3.2 日质量行（几何构建与质量统计共用输入）。

imports 全部位于文件顶部；本模块被 etl / migration / fact_card 等消费方共用，
禁止在本模块内出现 sqlite3 / open / requests 等副作用调用。
"""

from __future__ import annotations

import datetime
import hashlib
import itertools
import json
import math
import statistics
from collections.abc import Iterable
from typing import NamedTuple, TypedDict

from gacore.langTrack.etl_config import resolve_coord_system

_TZ_CST = datetime.timezone(datetime.timedelta(hours=8))

# ---------------------------------------------------------------------------
# 常量（与 etl_config 语义对齐；纯算法默认值，配置层可在调用时覆盖）
# ---------------------------------------------------------------------------

# canonical place 聚类（§2.3）
CLUSTER_CENTER_MAX_M = 120.0     # 规则 3：seed 中心到 cluster 中心 ≤120m
CLUSTER_SPREAD_MAX_M = 150.0     # 规则 3：加入后所有成员到新中心最大距离 ≤150m
GRID_STEP = 0.001                # 0.001° (~110m) 快速分桶粒度

# 新旧匹配（§2.3 规则 7）
MATCH_JACCARD_MIN = 0.5
MATCH_CENTER_DIST_MAX_M = 80.0

# 质量桶（§3.2）
ACCURACY_BUCKETS: tuple = ("lt10", "10_50", "50_100", "gt100", "unknown")

# 显示契约分值（§2.6，仅用于选粒度，不表示到访置信度）
NAME_EVIDENCE_SCORE = {
    "poi_near": 0.90,
    "poi_mid": 0.75,
    "aoi_parent": 0.80,
    "around_fallback": 0.55,
    "address": 0.60,
    "district": 0.40,
    "unknown": 0.0,
}


class LocationPoint(NamedTuple):
    """规范化坐标点（§2.1）。acc 缺失/非法记 None；provider 统一小写；coord_system 来自配置。"""

    device_id: str
    ts: int
    lat: float
    lon: float
    accuracy_m: float | None
    provider: str
    coord_system: str


# ---------------------------------------------------------------------------
# 坐标解析（§2.1）
# ---------------------------------------------------------------------------

def _to_finite_float(value) -> float | None:
    """白名单数值解析：接受有限数字及可被 float() 解析的纯数字字符串。

    明确拒绝：bool、空串、非数值字符串、NaN/Inf。其余返回 None（视为缺省）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(f):
        return None
    return f


def parse_location_point(
    device_id: str,
    ts: int,
    payload: dict,
    coord_system: str = "unknown",
) -> LocationPoint | None:
    """从事件 payload 解析规范化坐标点。

    支持当前键 lat/lon/acc/provider；不猜测 latitude/lng/accuracy 别名。
    纬度不在 [-90,90]、经度不在 [-180,180]、(0,0) 一律拒绝。
    acc 缺失保留 unknown(None)，负数或非有限值按 unknown；provider 小写，缺失写 unknown。
    """
    if not isinstance(payload, dict):
        return None
    lat = _to_finite_float(payload.get("lat"))
    lon = _to_finite_float(payload.get("lon"))
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0):
        return None
    if not (-180.0 <= lon <= 180.0):
        return None
    if lat == 0.0 and lon == 0.0:
        return None

    acc = _to_finite_float(payload.get("acc"))
    if acc is not None and acc < 0:
        acc = None

    provider = payload.get("provider")
    if provider is None:
        provider = "unknown"
    else:
        provider = str(provider).strip().lower() or "unknown"

    return LocationPoint(device_id, ts, lat, lon, acc, provider, coord_system)


# ---------------------------------------------------------------------------
# 基础几何工具
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine 球面距离（米）。"""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def grid_key_of(lat: float, lon: float) -> str:
    """0.001° 网格键（与 etl.build_places/build_stays 同一语义：round 最近值，%.3f 格式）。

    全链路（v1 places、v2 shadow、point_count 分桶）必须共用本函数，
    保证新旧网格词汇一致——Task 4 新旧 place Jaccard matching 依赖同一分桶。
    """
    glat = round(lat * 1000) / 1000
    glon = round(lon * 1000) / 1000
    return f"{glat:.3f},{glon:.3f}"


# ---------------------------------------------------------------------------
# 质量统计（§3.2 只读聚合，纯函数）
# ---------------------------------------------------------------------------

def quality_stats(points: Iterable[LocationPoint]) -> dict:
    """按设备聚合坐标质量统计。

    输出结构：
    {
      <device_id>: {
        "points": int,
        "providers": {<provider>: int},
        "accuracy": {"lt10": int, "10_50": int, "50_100": int, "gt100": int, "unknown": int},
        "slots_30m": int,          # 覆盖的 30 分钟格数（稀疏度）
        "avg_sample_gap_s": float, # 平均采样间隔（秒），<2 点记 0
        "max_sample_gap_s": float,
      }
    }
    """
    by_device: dict[str, list[LocationPoint]] = {}
    for p in points:
        by_device.setdefault(p.device_id, []).append(p)

    out: dict = {}
    for device_id, plist in by_device.items():
        plist.sort(key=lambda p: p.ts)
        providers: dict[str, int] = {}
        acc_buckets = {b: 0 for b in ACCURACY_BUCKETS}
        slots: set[int] = set()
        for p in plist:
            providers[p.provider] = providers.get(p.provider, 0) + 1
            if p.accuracy_m is None:
                acc_buckets["unknown"] += 1
            elif p.accuracy_m < 10:
                acc_buckets["lt10"] += 1
            elif p.accuracy_m <= 50:
                acc_buckets["10_50"] += 1
            elif p.accuracy_m <= 100:
                acc_buckets["50_100"] += 1
            else:
                acc_buckets["gt100"] += 1
            slots.add(p.ts // (30 * 60 * 1000))

        gaps = [b.ts - a.ts for a, b in itertools.pairwise(plist)]
        avg_gap = (sum(gaps) / len(gaps) / 1000.0) if gaps else 0.0
        max_gap = (max(gaps) / 1000.0) if gaps else 0.0
        out[device_id] = {
            "points": len(plist),
            "providers": providers,
            "accuracy": acc_buckets,
            "slots_30m": len(slots),
            "avg_sample_gap_s": round(avg_gap, 1),
            "max_sample_gap_s": round(max_gap, 1),
        }
    return out


def location_points(events, coord_cfg: dict) -> list[LocationPoint]:
    """events → LocationPoint 共用解析入口（§2.1，Task 6 起全链路唯一解析点）。

    build_stays / build_places / build_trips / 质量统计共用本函数，保证：
    - 坐标白名单校验单点维护（非法坐标 / (0,0) / 越界统一拒绝）；
    - coord_system 由 (device_id, ts) + coord_cfg 解析，不从数值自动猜；
    - 输出按 (device_id, ts) 排序。

    events: [(device_id, ts, type, payload)]；仅解析 location 事件，解析失败的丢弃。
    """
    points: list[LocationPoint] = []
    for device_id, ts, type_, payload in events:
        if type_ != "location":
            continue
        cs = resolve_coord_system(device_id, ts, coord_cfg)
        pt = parse_location_point(device_id, ts, payload, coord_system=cs)
        if pt is not None:
            points.append(pt)
    points.sort(key=lambda p: (p.device_id, p.ts))
    return points


def accuracy_filter(points: list[LocationPoint], cfg: dict | None = None) -> list[LocationPoint]:
    """§3.1 坐标精度过滤（作用于几何构建输入；质量统计本身只观测不过滤）。

    cfg 为 etl_config 的 location 节点：
    - apply_accuracy_filter=False（默认）：只观测，原样返回；
    - True：仅剔除 accuracy 已知且 > max_accuracy_m 的点；accuracy 缺失按
      accept_missing_accuracy 决定去留（室内 network 点常缺 accuracy，默认保留）。
    """
    c = cfg or {}
    if not bool(c.get("apply_accuracy_filter", False)):
        return points
    max_acc = float(c.get("max_accuracy_m", 150.0))
    keep_missing = bool(c.get("accept_missing_accuracy", True))
    out: list[LocationPoint] = []
    for p in points:
        if p.accuracy_m is None:
            if keep_missing:
                out.append(p)
            continue
        if p.accuracy_m <= max_acc:
            out.append(p)
    return out


def daily_quality_rows(events, coord_cfg: dict) -> list[tuple]:
    """按 (day, device_id) 聚合坐标质量，直接产出 daily_location_quality 行（§3.2）。

    返回 [(day, device_id, points_total, points_valid, accuracy_known,
          accuracy_le_50, accuracy_51_150, accuracy_gt_150,
          observed_half_hour_bins, median_interval_sec, providers_json)]：
    - points_total：该日该设备全部 location 事件数（含解析失败）；
    - points_valid：解析成功（LocationPoint）的点数；accuracy 桶基于 valid 点；
    - 采样间隔在 (device, day) 桶内独立排序计算，不跨设备不跨日混合；<2 点记 None；
    - day 为东八区自然日，与 etl.day_of 同一语义。
    """
    by_key: dict[tuple[str, str], dict] = {}
    for device_id, ts, type_, payload in events:
        if type_ != "location":
            continue
        day = datetime.datetime.fromtimestamp(ts / 1000, tz=_TZ_CST).strftime("%Y-%m-%d")
        cell = by_key.setdefault(
            (day, device_id),
            {
                "total": 0, "valid": [], "providers": {},
                "le50": 0, "q51_150": 0, "gt150": 0, "known": 0, "slots": set(),
            },
        )
        cell["total"] += 1
        pt = parse_location_point(
            device_id, ts, payload,
            coord_system=resolve_coord_system(device_id, ts, coord_cfg),
        )
        if pt is None:
            continue
        cell["valid"].append(pt)
        cell["providers"][pt.provider] = cell["providers"].get(pt.provider, 0) + 1
        acc = pt.accuracy_m
        if acc is None:
            pass
        else:
            cell["known"] += 1
            if acc <= 50:
                cell["le50"] += 1
            elif acc <= 150:
                cell["q51_150"] += 1
            else:
                cell["gt150"] += 1
        cell["slots"].add(ts // (30 * 60 * 1000))

    rows: list[tuple] = []
    for (day, device_id), c in sorted(by_key.items()):
        pts = sorted(c["valid"], key=lambda p: p.ts)
        if len(pts) >= 2:
            gaps = [b.ts - a.ts for a, b in itertools.pairwise(pts)]
            median_gap = round(statistics.median(gaps) / 1000.0, 1)
        else:
            median_gap = None
        rows.append(
            (
                day, device_id, c["total"], len(pts),
                c["known"], c["le50"], c["q51_150"], c["gt150"],
                len(c["slots"]), median_gap,
                json.dumps(c["providers"], ensure_ascii=False, sort_keys=True),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# 停留段输入结构（canonical 聚类消费的最小字段集）
# ---------------------------------------------------------------------------

class StayInput(NamedTuple):
    """canonical 聚类所需的最小停留段字段（来自 shadow_stays_v2 / stays）。"""

    device_id: str
    start_ts: int
    end_ts: int
    duration_ms: int
    center_lat: float
    center_lon: float
    grid_key: str


def _weighted_center(stays: list[StayInput]) -> tuple[float, float]:
    """duration 加权球面质心（近似：短距内按平面加权平均，误差可忽略）。"""
    total = sum(s.duration_ms for s in stays) or 1.0
    lat = sum(s.center_lat * s.duration_ms for s in stays) / total
    lon = sum(s.center_lon * s.duration_ms for s in stays) / total
    return round(lat, 6), round(lon, 6)


class PlaceCluster(NamedTuple):
    """一个 canonical place cluster（§2.3 规则 6 产物）。"""

    device_id: str
    grid_key: str                       # 代表网格 = sorted(member_grid_keys)[0]
    member_grid_keys: tuple[str, ...]
    member_stays: tuple[StayInput, ...]
    center_lat: float
    center_lon: float
    radius_m: float


def cluster_stays(stays: Iterable[StayInput]) -> list[PlaceCluster]:
    """canonical place 确定性几何聚类（§2.3 规则 1-6）。

    1. 先按 (device_id, grid_key) 聚合为原子 seed，seed 不可拆分；
    2. seed 中心为内部 stays 的 duration 加权质心；
    3. 按 seed.grid_key 排序，将 seed 分配给最近 cluster：中心距离 ≤120m 且
       加入后所有成员到新中心最大距离 ≤150m；没有满足时新建；同距按代表 grid_key 排序；
    5. 代表 grid_key = sorted(member_grid_keys)[0]（seed 不可拆分 → 同设备唯一）；
    6. cluster 中心 = 全部 stays 的 duration 加权质心；半径 = 所有成员到中心的最大距离。

    浮点距离统一四舍五入到 0.01m 比较（规则 13）。输入顺序不影响结果。
    """
    # 1) 原子 seed 聚合
    seed_map: dict[tuple[str, str], list[StayInput]] = {}
    for s in stays:
        seed_map.setdefault((s.device_id, s.grid_key), []).append(s)

    seeds: list[dict] = []
    for (device_id, gk), members in sorted(seed_map.items()):
        clat, clon = _weighted_center(members)
        seeds.append(
            {
                "device_id": device_id,
                "grid_key": gk,
                "stays": members,
                "lat": clat,
                "lon": clon,
            }
        )

    # 3) 按 seed.grid_key 排序逐个分配
    clusters: list[dict] = []
    for seed in sorted(seeds, key=lambda x: x["grid_key"]):
        best_idx = None
        best_dist = None
        for i, cl in enumerate(clusters):
            if cl["device_id"] != seed["device_id"]:
                continue
            d_center = haversine_m(cl["lat"], cl["lon"], seed["lat"], seed["lon"])
            if d_center > CLUSTER_CENTER_MAX_M:
                continue
            # 试加入后 spread 校验
            merged_stays = list(cl["stays"]) + seed["stays"]
            mlat, mlon = _weighted_center(merged_stays)
            spread = max(
                haversine_m(mlat, mlon, st.center_lat, st.center_lon)
                for st in merged_stays
            )
            if spread > CLUSTER_SPREAD_MAX_M:
                continue
            d_round = round(d_center, 2)
            if best_dist is None or d_round < best_dist or (
                d_round == best_dist and seed["grid_key"] < clusters[best_idx]["grid_key"]
            ):
                best_idx = i
                best_dist = d_round
        if best_idx is None:
            cl = {
                "device_id": seed["device_id"],
                "stays": seed["stays"],
                "lat": seed["lat"],
                "lon": seed["lon"],
                "member_keys": {seed["grid_key"]},
            }
            clusters.append(cl)
        else:
            cl = clusters[best_idx]
            cl["stays"] = list(cl["stays"]) + seed["stays"]
            cl["lat"], cl["lon"] = _weighted_center(cl["stays"])
            cl["member_keys"].add(seed["grid_key"])

    out: list[PlaceCluster] = []
    for cl in clusters:
        member_keys = tuple(sorted(cl["member_keys"]))
        clat, clon = cl["lat"], cl["lon"]
        radius = round(
            max(haversine_m(clat, clon, st.center_lat, st.center_lon) for st in cl["stays"]),
            1,
        )
        out.append(
            PlaceCluster(
                device_id=cl["device_id"],
                grid_key=member_keys[0],
                member_grid_keys=member_keys,
                member_stays=tuple(sorted(cl["stays"], key=lambda s: (s.start_ts, s.end_ts))),
                center_lat=clat,
                center_lon=clon,
                radius_m=radius,
            )
        )
    out.sort(key=lambda c: (c.device_id, c.grid_key))
    return out


def _cluster_key(device_id: str, member_grid_keys: tuple[str, ...]) -> str:
    """§2.3 规则 9：new_cluster_key = sha1(device_id+"|"+join(sorted(member_grid_keys)))[:16]。"""
    joined = "|".join(member_grid_keys)
    return hashlib.sha1(f"{device_id}|{joined}".encode()).hexdigest()[:16]


def canonical_places(stays: Iterable[StayInput]) -> list[dict]:
    """cluster + 稳定 place_id 生成（§2.3 规则 1-6/9/12）。

    输出 place 草稿（未做新旧匹配时的身份）：
    {device_id, place_id, grid_key, lat, lon, member_grid_keys,
     point_count, visit_count, stay_ms, first_seen, last_seen}
    visit_count = 关联 stay 段数；point_count = 成员网格内原始点数（调用方回填）。
    """
    clusters = cluster_stays(stays)
    out: list[dict] = []
    for c in clusters:
        first_seen = min(s.start_ts for s in c.member_stays)
        last_seen = max(s.end_ts for s in c.member_stays)
        stay_ms = sum(s.duration_ms for s in c.member_stays)
        out.append(
            {
                "device_id": c.device_id,
                "place_id": _cluster_key(c.device_id, c.member_grid_keys),
                "grid_key": c.grid_key,
                "lat": c.center_lat,
                "lon": c.center_lon,
                "member_grid_keys": list(c.member_grid_keys),
                "point_count": 0,  # 调用方按成员网格回填
                "visit_count": len(c.member_stays),
                "stay_ms": stay_ms,
                "first_seen": first_seen,
                "last_seen": last_seen,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 新旧 place 全局一对一 matching（§2.3 规则 7-11）
# ---------------------------------------------------------------------------

class OldPlace(NamedTuple):
    """旧 places 行（matching 输入最小字段集）。"""

    device_id: str
    place_id: str
    grid_key: str
    lat: float
    lon: float
    label: str
    poi: str | None
    address: str | None
    matched_level: str | None
    grid_keys: tuple[str, ...] = ()  # 旧 place 的成员网格（无则 [grid_key]）
    # 附加 geocode 派生列（缓存迁移载荷；matching 逻辑不读）。NamedTuple 只读共享默认值，
    # 消费方 _decide_labels_and_cache 仅整体透传，不做就地修改。
    geocode: dict = {}  # noqa: RUF012


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_old_new(
    old_places: Iterable[OldPlace],
    new_places: Iterable[dict],
) -> tuple[dict[str, str], list[dict]]:
    """新旧 place 全局一对一 matching（§2.3 规则 7-11）。

    返回 (old_to_new: {old_place_id: new_place_id}, conflicts: [dict])。

    - 候选边：成员网格 Jaccard ≥0.5 或 中心距离 ≤80m（规则 7）；
    - 全局排序：Jaccard DESC、distance ASC、old_place_id、new_cluster_key（规则 8）；
    - 旧 place_id 和新 cluster 均只能认领一次（规则 8）；多对多取排序最佳边；
    - 一旧拆二：旧 ID 只给排序最佳 child，其他 child 用 new_cluster_key（规则 10）；
    - 两旧并一：排序最佳旧 ID 成为 survivor，其他旧 ID 进入 conflicts（规则 11）；
    - 无旧匹配：place_id = new_cluster_key（规则 12）。
    """
    old_list = list(old_places)
    new_list = list(new_places)

    new_by_key: dict[tuple[str, str], dict] = {}
    for n in new_list:
        new_by_key[(n["device_id"], n["place_id"])] = n

    # 构建候选边
    edges: list[dict] = []
    for o in old_list:
        o_keys = set(o.grid_keys) if o.grid_keys else {o.grid_key}
        for n in new_list:
            if n["device_id"] != o.device_id:
                continue
            n_keys = set(n.get("member_grid_keys", [n["grid_key"]]))
            jac = jaccard(o_keys, n_keys)
            dist = haversine_m(o.lat, o.lon, n["lat"], n["lon"])
            if jac >= MATCH_JACCARD_MIN or dist <= MATCH_CENTER_DIST_MAX_M:
                edges.append(
                    {
                        "old": o.place_id,
                        "new": n["place_id"],
                        "jaccard": round(jac, 4),
                        "dist_m": round(dist, 2),
                    }
                )

    # 规则 8：全局排序后按 old 逐一认领其最佳 new。
    # 一个 new 可被多个 old 命中（两旧并一）：首个认领者为 survivor，
    # 后续命中同一 new 的旧 place 同样映射过去，但写 merge_survivor_tag conflict。
    edges.sort(
        key=lambda e: (
            -e["jaccard"],
            e["dist_m"],
            e["old"],
            e["new"],
        )
    )

    old_to_new: dict[str, str] = {}
    survivor_by_new: dict[str, str] = {}  # new -> survivor old
    conflicts: list[dict] = []

    for e in edges:
        if e["old"] in old_to_new:
            continue  # 每个 old 只能认领一次（规则 8）
        old_to_new[e["old"]] = e["new"]
        if e["new"] not in survivor_by_new:
            survivor_by_new[e["new"]] = e["old"]

    # 规则 11：两旧并一 → 非 survivor 的旧 tag 写 conflict
    for o in old_list:
        new_id = old_to_new.get(o.place_id)
        if new_id is None:
            continue
        survivor = survivor_by_new.get(new_id)
        if o.label and o.label != "未知" and survivor != o.place_id:
            conflicts.append(
                {
                    "device_id": o.device_id,
                    "old_place_id": o.place_id,
                    "new_place_id": new_id,
                    "tag": o.label,
                    "reason": "merge_survivor_tag",
                }
            )
    return old_to_new, conflicts


def resolve_place_ids(
    old_places: Iterable[OldPlace],
    new_places: Iterable[dict],
) -> tuple[list[dict], dict[str, str], list[dict]]:
    """canonical_places + match_old_new 的组合入口。

    输出 (places, old_to_new, conflicts)，places 的 place_id 已按规则 9/10/12 落定。
    """
    old_list = list(old_places)
    # new_places 由调用方通过 canonical_places(stays) 生成后传入，此处只做身份落定。
    old_to_new, conflicts = match_old_new(old_list, new_places)
    finalized: list[dict] = []
    for n in new_places:
        matched_old = next(
            (o for o in old_list if old_to_new.get(o.place_id) == n["place_id"]),
            None,
        )
        finalized.append(
            {
                **n,
                "place_id": matched_old.place_id if matched_old else n["place_id"],
            }
        )
    return finalized, old_to_new, conflicts


# ---------------------------------------------------------------------------
# 显示契约（§2.6）
# ---------------------------------------------------------------------------

class PlaceRef(TypedDict):
    place_id: str
    grid_key: str
    place_name: str
    name_source: str          # poi | poi_fallback | address | district | unknown
    user_tag: str             # 家 | 公司 | ""
    poi: str
    poi_fallback: str
    address: str
    district: str
    township: str
    business_area: str
    parent_poi: str
    behavior: str
    display_granularity: str
    name_confidence: float
    name_evidence: str


def _pick_name(
    poi: str,
    poi_fallback: str,
    address: str,
    district: str,
    township: str,
    business_area: str,
    parent_poi: str,
    name_confidence: float,
) -> tuple[str, str, str]:
    """按 name_confidence 决定粒度（§2.6）：

    高置信 venue → 具体 POI；商场/园区内不确定 → parent_poi / business_area；
    住宅附近不稳定 → address 中小区或 township；仍不确定 → district；完全无语义 → 未知地点。
    返回 (place_name, name_source, display_granularity)。
    """
    # 优先级与分值映射（分值仅用于选粒度，不表示到访置信度）；
    # 阶梯顺序对齐 §2.6：address(0.60) > township/district(0.40)，township 先于 district
    if name_confidence >= 0.75 and poi:
        return poi, "poi", "venue"
    if parent_poi and name_confidence >= 0.60:
        return parent_poi, "poi", "area"
    if business_area and name_confidence >= 0.55:
        return business_area, "poi_fallback", "area"
    if address:
        return address, "address", "address"
    if township:
        return township, "district", "neighborhood"
    if district:
        return district, "district", "district"
    return "未知地点", "unknown", "unknown"


def resolve_place_name(
    *,
    poi: str = "",
    poi_fallback: str = "",
    address: str = "",
    district: str = "",
    township: str = "",
    business_area: str = "",
    parent_poi: str = "",
    name_confidence: float = 0.0,
    label: str = "",
) -> tuple[str, str, str]:
    """统一地名派生纯函数（§2.6）：所有出口只能调用本函数，禁止各自写 fallback 链。

    返回 (place_name, name_source, display_granularity)。
    label 只作为 user_tag 展示用，不参与地名选择。
    """
    return _pick_name(
        poi or "",
        poi_fallback or "",
        address or "",
        district or "",
        township or "",
        business_area or "",
        parent_poi or "",
        float(name_confidence or 0.0),
    )


def user_tag_of(label: str) -> str:
    """user_tag = NULLIF(label,'未知')。"""
    return label if label and label != "未知" else ""


def format_place(place_name: str, user_tag: str) -> str:
    """compact 显示（§2.6）：真名+tag / 仅真名 / 仅 tag / 全空 → 未知地点。"""
    name = (place_name or "").strip()
    tag = (user_tag or "").strip()
    if name and tag:
        return f"{name}〔{tag}〕"
    if name:
        return name
    if tag:
        return tag
    return "未知地点"


# ---------------------------------------------------------------------------
# 高德坐标边界（§3.3）
# ---------------------------------------------------------------------------

# WGS84 → GCJ02 近似基准（中国境内常用常量，Gauss-Krüger 投影近似）
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lat: float, lon: float) -> bool:
    """中国境外近似 guard：境外 GCJ02 与 WGS84 基本一致，原样返回。"""
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y
        + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (
        300.0 + x + 2.0 * y + 0.1 * x * x
        + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 → GCJ02（火星坐标）近似转换。境外原样返回。"""
    if _out_of_china(lat, lon):
        return lat, lon
    d_lat = _transform_lat(lon - 105.0, lat - 35.0)
    d_lon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    d_lon = (d_lon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return round(lat + d_lat, 6), round(lon + d_lon, 6)


def to_amap_coord(lat: float, lon: float, coord_system: str) -> tuple[float, float]:
    """按源坐标系转换到高德 GCJ02 坐标系（§3.3）。

    - unknown / gcj02：原样返回（设备已经上报火星坐标或未知，不猜）；
    - wgs84：已知基准，中国境内做 GCJ02 近似转换，境外 guard 原样。
    """
    system = (coord_system or "unknown").strip().lower()
    if system == "wgs84":
        return wgs84_to_gcj02(lat, lon)
    return lat, lon


_unknown_coord_warned_scopes: set[str] = set()


def warn_unknown_coord_once(scope: str) -> None:
    """§3.3 source=unknown 警告：按原样调用高德（不猜坐标系），每个 scope 只提示一次。

    单点实现供 geocode/routes 共用（测试可用 monkeypatch 重置
    _unknown_coord_warned_scopes 为空集）。
    """
    if scope not in _unknown_coord_warned_scopes:
        _unknown_coord_warned_scopes.add(scope)
        print(f"[{scope}] 坐标制为 unknown：坐标原样调用高德（如有偏移请在 "
              "data/location_coord_systems.json 声明设备坐标系）")
