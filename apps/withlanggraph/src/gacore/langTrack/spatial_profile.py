"""长期空间画像 SpatialProfile 客观聚合（§2.9 / Task 8）。

本模块负责把 stays / places / daily_location_quality 等**已落库事实**按 7/30/90
天窗口聚合为 6 大客观指标：frequent_places / spatial_extent / commute_profile /
home_work_rhythm / scene_exposure / place_change，并为每个指标挂载可复算的
Evidence（覆盖率 / 样本量 / 解析质量 / accuracy 已知率，取最小值作为置信分）。

契约要点（与实现计划 §2.9 对齐）：

- 只读已落库事实，**不新建快照表**；所有 SQL 带 device_id，走
  ``(device_id,start_ts,end_ts)`` / ``(device_id,place_id)`` 索引。
- 跨午夜 stay 一律按「窗口 ∩ 自然日」半开区间裁剪（CST，东八区）。
- observed bin = 当日至少一个 parse 合法 LocationPoint 的 30 分钟格，
  直接取自 daily_location_quality.observed_half_hour_bins，**不受 accuracy 过滤影响**。
- expected bins 从 ``max(window_start, device.first_seen)`` 算至
  ``min(window_end, data_as_of)``，当前日截断到 data_as_of 所在半小时格；
  同时返回 requested / available window days，防新设备被误读为完整窗口。
- 家 / 公司仅认人工 label（家 / 公司）；未确认时 home distance、commute、
  home_work_rhythm 一律 None，**不用 is_primary 冒充人工事实**。
- scene exposure 用当前 places.poi_l1 回算历史 stay，输出
  ``classification_basis="current_place_semantics"``，不伪装为历史 POI 快照。
- commute 仅统计「同一自然日家 stay 后直接相邻公司 stay、gap ∈ [5min,4h]」；
  rhythm 有效样本日要求当日 coverage_ratio≥0.5 且存在对应 anchor stay，
  缺测日从 median/IQR 排除并单独计 missing_days。
- radius_of_gyration / weighted center 使用 stay_ms 加权球面质心。
- 所有分位数使用等权（每段 stay=1 个样本）鲁棒估算：median=P50、IQR=P75-P25。
- confidence 是规则型数据质量分，必须同时展示 components，由本模块计算，
  不由 LLM 填写。

本模块不做任何 LLM 调用、不输出"动机/意图"语义；只输出客观聚合与质量分数。
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from datetime import date, datetime, time, timedelta, timezone
from typing import TypedDict

from gacore.langTrack import location_reader as lr
from gacore.langTrack.location_facts import (
    haversine_m,
    resolve_place_name,
    user_tag_of,
)

_TZ_CST = timezone(timedelta(hours=8))
_HALF_HOUR_MS = 30 * 60 * 1000
_DAY_MS = 24 * 60 * 60 * 1000

# 指标要求的样本门槛（§2.9）
REQUIRED_SAMPLES_PLACE = 5
REQUIRED_SAMPLES_BEHAVIOR = 10

# 家 → 公司「直接相邻」gap 合法区间（含端点），单位毫秒
COMMUTE_GAP_MIN_MS = 5 * 60 * 1000
COMMUTE_GAP_MAX_MS = 4 * 60 * 60 * 1000

# 四段时段划分（按 stay 开始时刻的 CST 小时，§2.9 时段分布）
SEGMENTS = (
    ("凌晨", 0, 6),
    ("上午", 6, 12),
    ("下午", 12, 18),
    ("晚上", 18, 24),
)


# ---------------------------------------------------------------------------
# TypedDict 契约
# ---------------------------------------------------------------------------

class Evidence(TypedDict):
    """指标级数据质量证据；components 全部可复算，conf 由本模块规则计算。"""

    window_days: int  # 指标窗口（§2.9：frequent_places 7/30/90，其余 30）
    requested_window_days: int
    available_window_days: int
    observed_bins: int
    expected_bins: int
    coverage_ratio: float  # 0..1（分母 0 → 0.0）
    sample_count: int
    required_samples: int
    sample_score: float  # min(1, sample/required)
    parse_validity_score: float  # valid/total
    accuracy_known_score: float  # acc_known/valid
    quality_score: float  # min(parse_validity, accuracy_known)
    confidence_score: float  # min(coverage, sample, quality)
    confidence_level: str  # low / medium / high


class SpatialProfile(TypedDict):
    """长期空间画像入口（读单一出口；FactCard full / tool / dashboard 共用）。"""

    as_of_day: str
    data_as_of: str | None
    data_as_of_ms: int | None
    frequent_places: list[dict]
    spatial_extent: dict | None
    commute_profile: dict | None
    home_work_rhythm: dict | None
    scene_exposure: list[dict]
    place_change: dict | None
    time_space: dict | None  # 小时 × 地点类分布（Task 12d：生活轨迹时段分布）
    per_window: dict  # 7/30/90 聚合证据汇总（可选，测试与 dashboard 用）


# ---------------------------------------------------------------------------
# 纯计算辅助（不碰 DB）
# ---------------------------------------------------------------------------

def _ts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=_TZ_CST).timestamp() * 1000)


def _day_bounds(day_str: str) -> tuple[int, int]:
    """自然日 [day 00:00 CST, day+1 00:00 CST) 半开区间。"""
    y, mo, d = (int(x) for x in day_str.split("-"))
    return _ts(y, mo, d), _ts(y, mo, d) + _DAY_MS


def _win_bounds(as_of_day: str, days: int) -> tuple[int, int]:
    """窗口 [as_of_day-days+1 00:00, as_of_day+1 00:00)（半开）。"""
    y, mo, d = (int(x) for x in as_of_day.split("-"))
    start = _ts(y, mo, d) - (days - 1) * _DAY_MS
    return start, start + days * _DAY_MS


def _add_days(day_str: str, n: int) -> str:
    y, mo, d = (int(x) for x in day_str.split("-"))
    dt = date(y, mo, d) + timedelta(days=n)
    return dt.strftime("%Y-%m-%d")


def _clip(stay: dict, a: int, b: int) -> tuple[int, int] | None:
    """与 [a,b) 相交的裁剪段 (cs, ce)；无交集返回 None。"""
    cs = max(stay["start_ts"], a)
    ce = min(stay["end_ts"], b)
    return (cs, ce) if cs < ce else None


def _clip_to_window_and_days(stays, win_start: int, win_end: int, day_strs) -> list[dict]:
    """把一批 stay 按「窗口 ∩ 自然日」裁剪展开。

    返回 list[dict]，每项：(day, start_ts, end_ts, stay 原字段)——
    跨午夜 stay 会按自然日切开成多条，保证与自然日时间线对齐。
    """
    out = []
    for day_str in day_strs:
        d0, d1 = _day_bounds(day_str)
        a, b = max(win_start, d0), min(win_end, d1)
        for st in stays:
            clipped = _clip(st, a, b)
            if clipped is None:
                continue
            item = dict(st)
            item["day"] = day_str
            item["start_ts"], item["end_ts"] = clipped
            out.append(item)
    return out


def _quantile_robust(values: list[float] | list[int], q: float) -> float | None:
    """鲁棒分位数（type=2，R6 兼容）；空序列返回 None。"""
    if not values:
        return None
    s = sorted(float(v) for v in values)
    if q <= 0.0:
        return s[0]
    if q >= 1.0:
        return s[-1]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _median(values) -> float | None:
    return _quantile_robust(list(values), 0.5)


def _iqr(values) -> float | None:
    if not values:
        return None
    return _quantile_robust(values, 0.75) - _quantile_robust(values, 0.25)


def _weighted_centroid(points_ms: list[tuple[float, float, int]]) -> tuple[float, float]:
    """stay_ms 加权球面质心（先转单位向量：lat/lon→地心直角，加权平均，再反算）。"""
    tx = ty = tz = 0.0
    total = 0
    for lat, lon, ms in points_ms:
        if ms <= 0:
            continue
        lat_r, lon_r = math.radians(lat), math.radians(lon)
        w = float(ms)
        tx += w * math.cos(lat_r) * math.cos(lon_r)
        ty += w * math.cos(lat_r) * math.sin(lon_r)
        tz += w * math.sin(lat_r)
        total += w
    if total <= 0:
        return 0.0, 0.0
    x, y, z = tx / total, ty / total, tz / total
    hyp = math.sqrt(x * x + y * y)
    lat = math.degrees(math.atan2(z, hyp))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon


def _confidence_level(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# 数据读层（只读，全部带 device_id；缺表容错）
# ---------------------------------------------------------------------------

def _read_devices_first_seen(conn, device_id: str) -> int | None:
    """读取设备最早出现时间；devices 表缺失或查不到 → None。"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)").fetchall()}
    except sqlite3.OperationalError:
        return None
    if "first_seen" not in cols or "device_id" not in cols:
        return None
    try:
        row = conn.execute(
            "SELECT first_seen FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        v = int(row[0])
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _read_etl_watermark(conn, device_id: str) -> int | None:
    try:
        row = conn.execute(
            "SELECT last_event_ts FROM etl_state WHERE device_id=?", (device_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        v = int(row[0])
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _fallback_as_of(conn, device_id: str) -> int | None:
    """无 etl_state 时回退到该设备 stays 最大 end_ts（与 fact_card 一致）。"""
    try:
        row = conn.execute(
            "SELECT MAX(end_ts) AS m FROM stays WHERE device_id=?", (device_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row["m"] is None:
        return None
    return int(row["m"])


def _read_daily_quality_agg(
    conn, device_id: str, day_from: str, day_to: str
) -> dict:
    """窗口内日质量表聚合（缺表/无行 → 全 0）。day 用字符串比较（YYYY-MM-DD）。"""
    agg = {
        "points_total": 0,
        "points_valid": 0,
        "accuracy_known": 0,
        "observed_bins": 0,
    }
    try:
        row = conn.execute(
            "SELECT "
            "COALESCE(SUM(points_total),0) AS total, "
            "COALESCE(SUM(points_valid),0) AS valid, "
            "COALESCE(SUM(accuracy_known),0) AS known, "
            "COALESCE(SUM(observed_half_hour_bins),0) AS bins "
            "FROM daily_location_quality WHERE device_id=? AND day>=? AND day<=?",
            (device_id, day_from, day_to),
        ).fetchone()
    except sqlite3.OperationalError:
        return agg
    if row is None:
        return agg
    return {
        "points_total": int(row["total"] or 0),
        "points_valid": int(row["valid"] or 0),
        "accuracy_known": int(row["known"] or 0),
        "observed_bins": int(row["bins"] or 0),
    }


def _quality_by_day(conn, device_id: str, day_strs) -> dict[str, int]:
    """按日 observed bins（rhythm 的 coverage 用；缺行按 0）。"""
    if not day_strs:
        return {}
    try:
        rows = conn.execute(
            "SELECT day, COALESCE(observed_half_hour_bins,0) AS bins "
            "FROM daily_location_quality "
            "WHERE device_id=? AND day>=? AND day<=?",
            (device_id, day_strs[0], day_strs[-1]),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["day"]: int(r["bins"] or 0) for r in rows}


def _expected_bins(
    win_start: int, win_end: int, first_seen: int | None, data_as_of: int | None
) -> tuple[int, int]:
    """计算 expected bins 与 available window days。

    end = min(win_end, data_as_of)，当前日截断到 data_as_of 所在半小时格起点；
    start = max(win_start, first_seen)。返回 (expected_bins, available_days)。
    """
    start = max(win_start, first_seen) if first_seen else win_start
    end = min(win_end, data_as_of) if data_as_of else win_end
    if end <= start:
        return 0, 0
    # 当前日截断到半小时格起点
    end_aligned = end - (end % _HALF_HOUR_MS)
    expected = max(0, (end_aligned - start) // _HALF_HOUR_MS)
    available_days = max(0, (end // _DAY_MS - start // _DAY_MS))
    return expected, available_days


def _coverage_by_recorded_days(
    daylist: list, quality_by_day: dict[str, int]
) -> float:
    """按「有记录日」的 48-bin 覆盖率取中位（缺失日不稀释采样密度）。

    与纯 ``sum(observed)/sum(expected)`` 相比，可避免短窗口因个别缺测日
    占比更高而被过度惩罚，进而保证多窗口（7/30/90）覆盖率单调不升。
    """
    covs = [
        min(1.0, (quality_by_day.get(d, 0) or 0) / 48)
        for d in daylist
        if (quality_by_day.get(d, 0) or 0) > 0
    ]
    return _median(covs) if covs else 0.0


def build_evidence(
    *,
    requested_window_days: int,
    win_start: int,
    win_end: int,
    first_seen: int | None,
    data_as_of: int | None,
    daily_agg: dict,
    sample_count: int,
    required_samples: int,
    daylist: list | None = None,
    quality_by_day: dict | None = None,
) -> Evidence:
    """构造指标 Evidence（§2.9 规则；分母为 0 记 0，conf 取三者最小）。

    coverage：当提供 ``daylist+quality_by_day`` 时按有记录日 48-bin 覆盖率
    中位数（缺测日不稀释）；否则回退为 ``sum(observed)/sum(expected)``。
    """
    expected_bins, available_days = _expected_bins(
        win_start, win_end, first_seen, data_as_of
    )
    if daylist is not None and quality_by_day is not None:
        coverage = _coverage_by_recorded_days(daylist, quality_by_day)
    else:
        coverage = expected_bins and daily_agg["observed_bins"] / expected_bins
    if coverage is None:
        coverage = 0.0
    coverage = max(0.0, min(1.0, coverage))
    sample_score = min(1.0, sample_count / required_samples) if required_samples else 0.0
    total = daily_agg["points_total"]
    valid = daily_agg["points_valid"]
    known = daily_agg["accuracy_known"]
    parse_score = (valid / total) if total else 0.0
    acc_score = (known / valid) if valid else 0.0
    quality_score = min(parse_score, acc_score)
    conf = min(coverage, sample_score, quality_score)
    return {
        "window_days": requested_window_days,
        "requested_window_days": requested_window_days,
        "available_window_days": available_days,
        "observed_bins": daily_agg["observed_bins"],
        "expected_bins": expected_bins,
        "coverage_ratio": round(coverage, 4),
        "sample_count": sample_count,
        "required_samples": required_samples,
        "sample_score": round(sample_score, 4),
        "parse_validity_score": round(parse_score, 4),
        "accuracy_known_score": round(acc_score, 4),
        "quality_score": round(quality_score, 4),
        "confidence_score": round(conf, 4),
        "confidence_level": _confidence_level(conf),
    }


# ---------------------------------------------------------------------------
# 6 大聚合
# ---------------------------------------------------------------------------

def _day_of_week_cst(ts_ms: int) -> int:
    """CST 自然日星期（0=周一 … 6=周日）。"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=_TZ_CST).weekday()


def _segment_of(ts_ms: int) -> str:
    h = datetime.fromtimestamp(ts_ms / 1000, tz=_TZ_CST).hour
    for name, lo, hi in SEGMENTS:
        if lo <= h < hi:
            return name
    return "晚上"


def _frequent_places(
    conn, device_id, places_by_id, stays_clipped, win_str, win_start, win_end,
    first_seen, data_as_of, daily_agg, daylist, quality_by_day,
) -> tuple[list[dict], int]:
    """Top 常去地点聚合（每窗口一份）。

    返回 (places_list, place_sample_n)；每段 stay=1 样本（用于 Evidence.sample_count）。
    """
    by_place: dict[str, dict] = {}
    sample_n = 0
    for it in stays_clipped:
        pid = it.get("place_id")
        if not pid:
            continue
        sample_n += 1
        g = by_place.setdefault(pid, {
            "day_set": set(),
            "n": 0,
            "stay_ms": 0,
            "median_ms": [],
            "last_seen_ms": 0,
            "weekday_n": 0,
            "weekend_n": 0,
            "seg": {name: 0 for name, _, _ in SEGMENTS},
        })
        g["day_set"].add(it["day"])
        g["n"] += 1
        g["stay_ms"] += it["end_ts"] - it["start_ts"]
        g["median_ms"].append(it["end_ts"] - it["start_ts"])
        g["last_seen_ms"] = max(g["last_seen_ms"], it["end_ts"])
        if _day_of_week_cst(it["start_ts"]) < 5:
            g["weekday_n"] += 1
        else:
            g["weekend_n"] += 1
        g["seg"][_segment_of(it["start_ts"])] += 1

    out = []
    for pid, g in by_place.items():
        pl = places_by_id.get(pid) or {}
        # §2.6 契约：地名只来自 poi/poi_fallback/address 等地理字段（resolve_place_name），
        # label 仅作 user_tag；无地理证据时 place_name 置空（显示层只出 tag，不冒充"未知"）
        pn, _src, _gran = resolve_place_name(
            poi=pl.get("poi") or "",
            poi_fallback=pl.get("poi_fallback") or "",
            address=pl.get("address") or "",
            district=pl.get("district") or "",
            township=pl.get("township") or "",
            business_area=pl.get("business_area") or "",
            parent_poi=pl.get("parent_poi") or "",
            name_confidence=float(pl.get("name_confidence") or 0.0),
            label=pl.get("label") or "",
        )
        name = "" if _src == "unknown" else pn
        tag = user_tag_of(pl.get("label") or "")
        seg = dict(g["seg"])
        for key in list(seg):
            if seg[key] == 0:
                del seg[key]
        out.append({
            "window_days": int(win_str),
            "place_id": pid,
            "place_name": name,
            "user_tag": tag or None,
            "poi": pl.get("poi") or "",
            "poi_l1": pl.get("poi_l1") or "",
            "visit_days": len(g["day_set"]),
            "visit_episodes": g["n"],
            "stay_ms": g["stay_ms"],
            "median_stay_ms": int(_median(g["median_ms"]) or 0),
            "last_seen_ms": g["last_seen_ms"],
            "weekday_visits": g["weekday_n"],
            "weekend_visits": g["weekend_n"],
            "period_dist": seg,
            "qualified": len(g["day_set"]) >= 3 or g["stay_ms"] >= 6 * 60 * 60 * 1000,
            "evidence": None,  # 下方统一填充
        })
    out.sort(key=lambda x: (-x["visit_days"], -x["stay_ms"], x["place_id"]))
    # 每窗口聚合一个 Evidence（样本量=窗口内 stay 段数）
    _ev = build_evidence(
        requested_window_days=int(win_str), win_start=win_start, win_end=win_end,
        first_seen=first_seen, data_as_of=data_as_of, daily_agg=daily_agg,
        sample_count=sample_n, required_samples=REQUIRED_SAMPLES_PLACE,
        daylist=daylist, quality_by_day=quality_by_day,
    )
    for item in out:
        item["evidence"] = _ev
    return out, sample_n


def _spatial_extent(
    conn, device_id, places_by_id, home_place, stays_clipped, win_start, win_end,
    first_seen, data_as_of, daily_agg,
) -> dict | None:
    """生活半径（相对已确认家）metrics、radius_of_gyration、地点访问熵。

    无障碍/被用户暂停使用。无家标签时 home_distance=None（其余指标照常）。
    """
    if not stays_clipped:
        return None
    # 每段 stay 一个等权样本：家距离分位数（坐标经 place 索引反查）
    def _coord_of(it):
        pl = places_by_id.get(it.get("place_id")) or {}
        lat, lon = pl.get("lat"), pl.get("lon")
        return (lat, lon) if lat is not None and lon is not None else None

    home_dists = []
    if home_place and home_place.get("lat") is not None and home_place.get("lon") is not None:
        for it in stays_clipped:
            c = _coord_of(it)
            if c is None:
                continue
            d = haversine_m(c[0], c[1], home_place["lat"], home_place["lon"])
            home_dists.append(d)
    # radius_of_gyration（stay_ms 加权）
    cx_samples = []
    for it in stays_clipped:
        c = _coord_of(it)
        if c is None:
            continue
        cx_samples.append((c[0], c[1], it["end_ts"] - it["start_ts"]))
    if cx_samples:
        clat, clon = _weighted_centroid(
            [(a, b, ms) for a, b, ms in cx_samples if ms > 0]
        )
        rg_ms = 0.0
        total_ms = 0.0
        for a, b, ms in cx_samples:
            rg_ms += ms * haversine_m(a, b, clat, clon) ** 2
            total_ms += ms
        rg = math.sqrt(rg_ms / total_ms) if total_ms else 0.0
    else:
        rg = None

    # 地点访问熵（按 stay_ms 占比）
    stay_ms_by_place: dict[str, int] = {}
    for it in stays_clipped:
        pid = it.get("place_id") or ""
        stay_ms_by_place[pid] = stay_ms_by_place.get(pid, 0) + (it["end_ts"] - it["start_ts"])
    total_ms = sum(stay_ms_by_place.values()) or 1
    place_entropy = 0.0
    distinct_places = 0
    for ms in stay_ms_by_place.values():
        if ms <= 0:
            continue
        distinct_places += 1
        p = ms / total_ms
        place_entropy -= p * math.log(p)

    n_dists = len(home_dists)
    _ev = build_evidence(
        requested_window_days=30, win_start=win_start, win_end=win_end,
        first_seen=first_seen, data_as_of=data_as_of, daily_agg=daily_agg,
        sample_count=n_dists, required_samples=REQUIRED_SAMPLES_PLACE,
    )
    out: dict = {
        "home_distance": None,
        "radius_of_gyration_m": round(rg, 1) if rg is not None else None,
        "place_count": distinct_places,
        "place_entropy": round(place_entropy, 3),
        "weighted_center": [round(clat, 6), round(clon, 6)] if cx_samples else None,
        "evidence": _ev,
    }
    if home_place and n_dists:
        dists = sorted(home_dists)
        out["home_distance"] = {
            "p50_m": round(_quantile_robust(dists, 0.5), 1),
            "p90_m": round(_quantile_robust(dists, 0.9), 1),
            "max_m": round(_quantile_robust(dists, 1.0), 1),
            "home_place_id": home_place.get("place_id"),
        }
    return out


def _is_weekday(ts_ms: int) -> bool:
    return _day_of_week_cst(ts_ms) < 5


def _tmin(ts_ms: int) -> int:
    """当日 0 点起的分钟数（0..1439），用于跨日序列的时刻中位数/分位数。

    对跨多日的 epoch 序列直接做线性插值中数会得到『两天之间』的无意义时刻
    （漂移 ±12h），必须先归一化到当日分钟。
    """
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=_TZ_CST)
    return dt.hour * 60 + dt.minute


def _fmt_min(m) -> str | None:
    """把分钟数（可含小数）格式化为 HH:MM。"""
    if m is None:
        return None
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


def _commute_profile(
    conn, device_id, home_place, work_place, stays_clipped,
    day_strs, win_start, win_end, first_seen, data_as_of, daily_agg,
) -> dict | None:
    """家→公司通勤概览（仅同一自然日直接相邻、gap∈[5min,4h]）。"""
    if not home_place or not work_place:
        return None
    # 按自然日分组家/公司 stay 时间线
    by_day: dict[str, dict] = {}
    for it in stays_clipped:
        pid = it.get("place_id")
        if pid == home_place.get("place_id"):
            by_day.setdefault(it["day"], {"home": []}).setdefault("home", []).append(it)
        elif pid == work_place.get("place_id"):
            by_day.setdefault(it["day"], {"work": []}).setdefault("work", []).append(it)
    valid_commutes = []  # (depart_ms, arrive_ms, duration_ms)
    weekday_days = 0
    weekend_days = 0
    for day_str, buckets in by_day.items():
        homes = sorted(buckets.get("home", []), key=lambda x: x["start_ts"])
        works = sorted(buckets.get("work", []), key=lambda x: x["start_ts"])
        # 首个家 stay 结束 → 其后第一个公司 stay 开始
        h_end = min((h["end_ts"] for h in homes), default=None)
        if h_end is None:
            continue
        next_w = next(
            (w for w in works if w["start_ts"] >= h_end), None
        )
        if next_w is None:
            continue
        gap = next_w["start_ts"] - h_end
        if not (COMMUTE_GAP_MIN_MS <= gap <= COMMUTE_GAP_MAX_MS):
            continue
        valid_commutes.append((h_end, next_w["start_ts"], gap))
        if _is_weekday(h_end):
            weekday_days += 1
        else:
            weekend_days += 1
    if not valid_commutes:
        return None
    departs = [c[0] for c in valid_commutes]
    arrives = [c[1] for c in valid_commutes]
    durations = [c[2] for c in valid_commutes]
    # 时刻类指标必须先归一化到「当日分钟」再取中位/IQR，否则跨 20 个自然日
    # 的绝对 epoch 序列线性插值会把启示时刻漂移 ±12h（如 08:00 → 20:00）。
    departs_min = [_tmin(c[0]) for c in valid_commutes]
    arrives_min = [_tmin(c[1]) for c in valid_commutes]

    def _hu(ms) -> str:
        mins = int(ms / 60000)
        return f"{mins // 60}小时{mins % 60}分"

    _ev = build_evidence(
        requested_window_days=30, win_start=win_start, win_end=win_end,
        first_seen=first_seen, data_as_of=data_as_of, daily_agg=daily_agg,
        sample_count=len(valid_commutes), required_samples=REQUIRED_SAMPLES_BEHAVIOR,
    )
    return {
        "valid_days": len(valid_commutes),
        "weekday_valid_days": weekday_days,
        "weekend_valid_days": weekend_days,
        "depart_hhmm_median": _fmt_min(_median(departs_min)),
        "depart_hhmm_iqr": _fmt_min(_iqr(departs_min)),
        "arrive_hhmm_median": _fmt_min(_median(arrives_min)),
        "arrive_hhmm_iqr": _fmt_min(_iqr(arrives_min)),
        "duration_ms_median": _median(durations),
        "duration_ms_iqr": _iqr(durations),
        "duration_human": _hu(_median(durations)) if _median(durations) else "",
        "endpoint_dist_m": round(
            haversine_m(home_place["lat"], home_place["lon"], work_place["lat"], work_place["lon"]), 1
        ) if home_place.get("lat") is not None and work_place.get("lat") is not None else None,
        "gap_windows": [COMMUTE_GAP_MIN_MS // 60000, COMMUTE_GAP_MAX_MS // 60000],
        "computed_from": "home_stay_work_stay_direct_adjacent",
        "evidence": _ev,
    }


def _home_work_rhythm(
    conn, device_id, home_place, work_place, day_strs,
    win_start, win_end, first_seen, data_as_of, daily_agg, quality_by_day,
    stays_clipped,
) -> dict | None:
    """工作日/周末：在家时长、公司时长、首次离家/最后回家/到公司/离公司 median/IQR。

    有效样本日：当日 coverage_ratio>=0.5 且有对应 anchor stay；缺测日从 median/IQR
    排除并单独计 missing_days（不能按 0 小时计入）。周一至周五标 calendar_basis=weekday。
    """
    if not home_place or not work_place:
        return None
    home_pid = home_place.get("place_id")
    work_pid = work_place.get("place_id") if work_place else None

    # 按日聚合（使用已裁剪展开的 stay）
    day_meta: dict[str, dict] = {}
    for it in stays_clipped:
        pid = it.get("place_id")
        dm = day_meta.setdefault(it["day"], {
            "home_ms": 0, "work_ms": 0,
            "home_starts": [], "home_ends": [],
            "work_starts": [], "work_ends": [],
        })
        dur = it["end_ts"] - it["start_ts"]
        if pid == home_pid:
            dm["home_ms"] += dur
            dm["home_starts"].append(it["start_ts"])
            dm["home_ends"].append(it["end_ts"])
        elif work_pid and pid == work_pid:
            dm["work_ms"] += dur
            dm["work_starts"].append(it["start_ts"])
            dm["work_ends"].append(it["end_ts"])

    if not day_meta:
        return None

    weekday_metrics = {"home_ms": [], "work_ms": [],
                       "first_leave": [], "last_back": [],
                       "arrive_work": [], "leave_work": []}
    weekend_metrics = {k: [] for k in weekday_metrics}
    missing_days = 0
    anchor_days = 0

    for day_str, dm in day_meta.items():
        has_home = dm["home_ms"] > 0
        has_work = work_pid is not None and dm["work_ms"] > 0
        if not (has_home or has_work):
            continue
        anchor_days += 1
        # coverage_ratio>=0.5 才计为有效样本日（expected bins 按自然日）
        wstart, wend = _day_bounds(day_str)
        a, b = max(win_start, wstart), min(win_end, wend)
        expected, _ = _expected_bins(a, b, first_seen, data_as_of)
        observed = quality_by_day.get(day_str, 0)
        cov = (observed / expected) if expected else 0.0
        valid_day = cov >= 0.5
        bucket = weekday_metrics if _day_of_week_cst(dm["home_starts"][0] if dm["home_starts"] else b - 1) < 5 else weekend_metrics
        if valid_day:
            bucket["home_ms"].append(dm["home_ms"])
            # work_ms 只统计「当天确实去过公司」的样本；整天在家（含周末留守）
            # 的 work_ms=0 不进桶，避免把空 0 当有效公司时长或拖低中位。
            if has_work:
                bucket["work_ms"].append(dm["work_ms"])
            if dm["home_ends"]:
                bucket["first_leave"].append(min(dm["home_ends"]))
                bucket["last_back"].append(max(dm["home_starts"]))
            if dm["work_starts"]:
                bucket["arrive_work"].append(min(dm["work_starts"]))
                bucket["leave_work"].append(max(dm["work_ends"]))
        else:
            missing_days += 1

    # 缺测口径补充：窗口自然日内、设备首次可见之后、完全无 quality 记录（定位
    # 缺失）且当日也无家/公司锚点的自然日，同样计入 missing_days——此类日既不
    # 进 day_meta、也不会被上面的 cov<0.5 分支捕获，漏计会导致 missing_days
    # 低估（P3-2）。有 quality 但当日确凿未去家/公司的日不算缺测（真实行为）。
    _span = set(day_strs)
    if first_seen:
        _first_day = datetime.fromtimestamp(
            first_seen / 1000, tz=_TZ_CST
        ).strftime("%Y-%m-%d")
        _span = {d for d in _span if d >= _first_day}
    _no_anchor = _span - set(day_meta.keys())
    missing_days += len(_no_anchor - set(quality_by_day))

    def _summ(bucket):
        out = {}
        for key, vals in bucket.items():
            if key == "home_ms" or key == "work_ms":
                out[key] = {
                    "median_ms": _median(vals),
                    "iqr_ms": _iqr(vals),
                }
            else:
                # 时刻指标跨多日，先归一化到「当日分钟」再取中位/IQR（避免 ±12h 漂移）
                mins = [_tmin(v) for v in vals]
                out[key] = {
                    "median_hhmm": _fmt_min(_median(mins)),
                    "iqr_hhmm": _fmt_min(_iqr(mins)),
                }
        return out

    _ev = build_evidence(
        requested_window_days=30, win_start=win_start, win_end=win_end,
        first_seen=first_seen, data_as_of=data_as_of, daily_agg=daily_agg,
        sample_count=anchor_days, required_samples=REQUIRED_SAMPLES_BEHAVIOR,
    )
    return {
        "weekday": _summ(weekday_metrics),
        "weekend": _summ(weekend_metrics),
        "calendar_basis": "weekday（周一至周五，不冒充法定工作日）",
        "anchor_days": anchor_days,
        "missing_days": missing_days,
        "missing_ratio": round(min(1.0, missing_days / anchor_days), 4) if anchor_days else None,
        "coverage_required": 0.5,
        "computed_from": "daily_cst_stays",
        "evidence": _ev,
    }

def _scene_exposure(
    conn, device_id, places_by_id, cur_stays, prev_stays,
    win_str, win_start, win_end, prev_start, prev_end,
    first_seen, data_as_of, daily_agg,
) -> tuple[list[dict], int]:
    """场景暴露（当前窗口 vs 前窗口）：当前 places.poi_l1 回算历史 stay。"""
    def _agg(stays):
        agg = {}
        for it in stays:
            pid = it.get("place_id")
            pl = places_by_id.get(pid)
            l1 = (pl or {}).get("poi_l1") or "unknown"
            g = agg.setdefault(l1, {"days": set(), "n": 0, "ms": 0, "places": set()})
            g["days"].add(it["day"])
            g["n"] += 1
            g["ms"] += it["end_ts"] - it["start_ts"]
            if pid:
                g["places"].add(pid)
        return agg

    cur = _agg(cur_stays)
    prev = _agg(prev_stays)
    all_keys = sorted(set(cur) | set(prev))
    out = []
    for l1 in all_keys:
        c, p = cur.get(l1), prev.get(l1)
        cn, pn = c["n"] if c else 0, p["n"] if p else 0
        c_ms = c["ms"] if c else 0
        p_ms = p["ms"] if p else 0
        c_days = len(c["days"]) if c else 0
        p_days = len(p["days"]) if p else 0
        c_places = len(c["places"]) if c else 0
        p_places = len(p["places"]) if p else 0
        change_pct = (
            ((c_ms - p_ms) / p_ms * 100) if p_ms > 0 else None
        )
        out.append({
            "poi_l1": l1,
            "classification_basis": "current_place_semantics",
            "cur_visit_days": c_days,
            "cur_episodes": cn,
            "cur_stay_ms": c_ms,
            "prev_visit_days": p_days,
            "prev_episodes": pn,
            "prev_stay_ms": p_ms,
            "cur_place_count": c_places,
            "prev_place_count": p_places,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "abs_change_ms": c_ms - p_ms,
        })
    out.sort(key=lambda x: (-x["cur_visit_days"], -x["cur_stay_ms"], x["poi_l1"]))
    _ev = build_evidence(
        requested_window_days=int(win_str), win_start=win_start, win_end=win_end,
        first_seen=first_seen, data_as_of=data_as_of, daily_agg=daily_agg,
        sample_count=len(cur_stays) + len(prev_stays), required_samples=REQUIRED_SAMPLES_PLACE,
    )
    for item in out:
        item["evidence"] = _ev
    return out, len(cur_stays)


def _place_change(
    conn, device_id, places_by_id, cur_stays, prev_stays,
    prev_start, prev_end, win_str, win_start, win_end,
    first_seen, data_as_of, daily_agg, all_places,
) -> dict | None:
    """地点变化：新 canonical 地点数、重复到访率、集合 Jaccard。

    previously seen = place.first_seen < 当前窗口起点（窗口开始前已出现）。
    """
    prev_place_ids = {s.get("place_id") for s in prev_stays if s.get("place_id")}
    cur_place_ids = {s.get("place_id") for s in cur_stays if s.get("place_id")}
    if not cur_place_ids:
        return None
    # 新地点：first_seen 落在当前窗口（用 places 表，不只以 visit 判断）
    new_places = 0
    new_ids = []
    for pid in cur_place_ids:
        pl = places_by_id.get(pid)
        fs = pl.get("first_seen") if pl else None
        if fs is not None and win_start <= fs < win_end:
            new_places += 1
            new_ids.append(pid)
        elif pid not in prev_place_ids:
            # 无 first_seen 时，用「上一窗口未出现」兜底判定为新
            new_places += 1
            new_ids.append(pid)
    # 重复到访率：当前窗口 visit 中恰在窗口开始前已存在的 place 占比
    visits_total = len(cur_stays)
    repeat_visits = sum(
        1 for s in cur_stays
        if s.get("place_id") and s["place_id"] in prev_place_ids
    )
    repeat_ratio = repeat_visits / visits_total if visits_total else 0.0
    # Jaccard
    union = cur_place_ids | prev_place_ids
    jaccard = len(cur_place_ids & prev_place_ids) / len(union) if union else None

    _ev = build_evidence(
        requested_window_days=int(win_str), win_start=win_start, win_end=win_end,
        first_seen=first_seen, data_as_of=data_as_of, daily_agg=daily_agg,
        sample_count=len(cur_stays), required_samples=REQUIRED_SAMPLES_BEHAVIOR,
    )
    return {
        "window_days": int(win_str),
        "prev_window_days": (win_start - prev_start) // _DAY_MS,
        "new_place_count": new_places,
        "new_place_ids": new_ids,
        "repeat_visit_ratio": round(repeat_ratio, 4),
        "cur_places": len(cur_place_ids),
        "prev_places": len(prev_place_ids),
        "place_set_jaccard": round(jaccard, 4) if jaccard is not None else None,
        "evidence": _ev,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _time_space_matrix(
    conn, device_id, places_by_id, clipped30, day30s,
    win_start: int, win_end: int, first_seen: int | None,
    data_as_of: int | None, daily_agg: dict,
) -> dict:
    """小时 × 地点类分布（Task 12d：生活轨迹时段分布）。

    口径：
    - 地点类按 places.label 映射：家 / 公司，其余（未知、无 tag）归"其他"；
    - stay 已按「窗口 ∩ 自然日」裁剪展开，逐小时桶按交集分钟数累计；
    - 单小时同一类别 ≥15 分钟才计入该类"天数"；不足 15 分钟计"无数据"；
      同一小时可同时计入两类（如跨地点转移的过渡小时），如实呈现；
    - evidence 复用 build_evidence：样本 = 有定位数据的天数，需求 10 天（rhythm 档）。
    """
    hour_ms = 3600000
    presence_min = 15 * 60 * 1000

    day_hour: dict[tuple[str, int], dict[str, float]] = {}
    for it in clipped30:
        pl = places_by_id.get(it.get("place_id")) or {}
        label = pl.get("label") or ""
        cls = label if label in ("家", "公司") else "其他"
        cur = it["start_ts"]
        while cur < it["end_ts"]:
            dt = datetime.fromtimestamp(cur / 1000, tz=_TZ_CST)
            hh = dt.hour
            next_h = int(dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000) + hour_ms
            seg = min(it["end_ts"], next_h) - cur
            g = day_hour.setdefault((it["day"], hh), {})
            g[cls] = g.get(cls, 0) + seg
            cur += seg

    hours: list[dict] = []
    days_with_data = 0
    for h in range(24):
        counts = {"hour": h, "home": 0, "work": 0, "other": 0, "no_data": 0}
        for day in day30s:
            g = day_hour.get((day, h)) or {}
            tot = sum(g.values())
            if tot < presence_min:
                counts["no_data"] += 1
            else:
                days_with_data += 1
            if g.get("家", 0) >= presence_min:
                counts["home"] += 1
            if g.get("公司", 0) >= presence_min:
                counts["work"] += 1
            if g.get("其他", 0) >= presence_min:
                counts["other"] += 1
        hours.append(counts)

    evidence = build_evidence(
        requested_window_days=30,
        win_start=win_start,
        win_end=win_end,
        first_seen=first_seen,
        data_as_of=data_as_of,
        daily_agg=daily_agg,
        sample_count=days_with_data,
        required_samples=10,
    )
    return {
        "window_days": 30,
        "days": len(day30s),
        "hours": hours,
        "evidence": evidence,
    }


def build_spatial_profile(
    conn: sqlite3.Connection,
    device_id: str,
    as_of_day: str,
) -> SpatialProfile:
    """构建长期空间画像（Task 8 唯一读出口）。

    缺表 / 无数据时返回空骨架（各聚合为 None / []），不抛异常（调用方已包 try）。
    """
    empty: SpatialProfile = {
        "as_of_day": as_of_day,
        "data_as_of": None,
        "data_as_of_ms": None,
        "frequent_places": [],
        "spatial_extent": None,
        "commute_profile": None,
        "home_work_rhythm": None,
        "scene_exposure": [],
        "place_change": None,
        "time_space": None,
        "per_window": {},
    }

    # -- 数据水位与窗口 -------------------------------
    watermark = _read_etl_watermark(conn, device_id)
    as_of_ms = watermark if watermark else _fallback_as_of(conn, device_id)
    first_seen = _read_devices_first_seen(conn, device_id)
    if as_of_ms is None:
        # 完全无水位 → 无法裁剪 expected bins；仍可尝试读窗口内数据（覆盖率为 0）
        as_of_ms = _ts(*(int(x) for x in as_of_day.split("-"))) + _DAY_MS - 1
    data_as_of_iso = datetime.fromtimestamp(as_of_ms / 1000, tz=_TZ_CST).strftime("%Y-%m-%d %H:%M")

    # 30 天主窗口；前 30 天（对比窗）
    w30_s, w30_e = _win_bounds(as_of_day, 30)
    prev_s, prev_e = w30_s - 30 * _DAY_MS, w30_s

    day30s = [_add_days(as_of_day, i - 29) for i in range(30)]
    day_prev = [_add_days(as_of_day, i - 59) for i in range(30)]

    # -- places 索引 / 家公司 ------------------------------------
    all_places = lr.read_places(conn, device_id=device_id)
    places_by_id = {p["place_id"]: p for p in all_places if p.get("place_id")}
    home_place = next((p for p in all_places if p.get("label") == "家"), None)
    work_place = next((p for p in all_places if p.get("label") == "公司"), None)

    # -- stays 读取（30 天窗 + 前 30 天窗） -----------------------
    stays30 = lr.read_stays(
        conn, device_id=device_id, overlap=(w30_s, w30_e), with_place=True
    )
    stays_prev = lr.read_stays(
        conn, device_id=device_id, overlap=(prev_s, prev_e), with_place=True
    )

    # 跨午夜按「窗口∩自然日」裁剪展开
    clipped30 = _clip_to_window_and_days(stays30, w30_s, w30_e, day30s)
    clipped_prev = _clip_to_window_and_days(stays_prev, prev_s, prev_e, day_prev)

    # 质量聚合（30 天）
    day_from30 = day30s[0]
    day_to30 = day30s[-1]
    daily_agg = _read_daily_quality_agg(
        conn, device_id, day_from30, day_to30
    )
    quality_by_day = _quality_by_day(conn, device_id, day30s)

    # -- ① frequent_places（7/30/90） -----------------------------
    freq_result = []
    for days in (7, 30, 90):
        if days == 30:
            ws, we = w30_s, w30_e
            daylist = day30s
            stays = stays30
            q_by_day = quality_by_day
        else:
            ws, we = _win_bounds(as_of_day, days)
            daylist = [_add_days(as_of_day, i - (days - 1)) for i in range(days)]
            stays = lr.read_stays(
                conn, device_id=device_id, overlap=(ws, we), with_place=True
            )
            q_by_day = _quality_by_day(conn, device_id, daylist)
        clipped = _clip_to_window_and_days(stays, ws, we, daylist)
        dagg = _read_daily_quality_agg(conn, device_id, daylist[0], daylist[-1])
        lst, _ = _frequent_places(
            conn, device_id, places_by_id, clipped, days, ws, we,
            first_seen, as_of_ms, dagg, daylist, q_by_day,
        )
        # 以 30 天为准去重（7/90 仅补充窗口级证据，不重复塞入同一 place 三份）
        # 文档要求 frequent_places 为 list，保留全部窗口，但项目按 window 区分
        freq_result.append({"window_days": days, "places": lst})
    # 展平：仅保留每窗口 top，字段带 window_days 标识
    for witem in freq_result:
        for p in witem["places"]:
            p["window"] = witem["window_days"]

    # -- ② spatial_extent（30 天） --------------------------------
    spatial_extent = _spatial_extent(
        conn, device_id, places_by_id, home_place, clipped30,
        w30_s, w30_e, first_seen, as_of_ms, daily_agg,
    )

    # -- ③ commute_profile（30 天） ------------------------------
    commute_profile = _commute_profile(
        conn, device_id, home_place, work_place, clipped30,
        day30s, w30_s, w30_e, first_seen, as_of_ms, daily_agg,
    )

    # -- ④ home_work_rhythm（30 天） ----------------------------
    home_work_rhythm = _home_work_rhythm(
        conn, device_id, home_place, work_place, day30s,
        w30_s, w30_e, first_seen, as_of_ms, daily_agg, quality_by_day,
        clipped30,
    )

    # -- ⑤ scene_exposure（30 vs 前 30） -------------------------
    scene_exposure, _ = _scene_exposure(
        conn, device_id, places_by_id, clipped30, clipped_prev,
        30, w30_s, w30_e, prev_s, prev_e, first_seen, as_of_ms, daily_agg,
    )

    # -- ⑥ place_change（30 vs 前 30） --------------------------
    place_change = _place_change(
        conn, device_id, places_by_id, clipped30, clipped_prev,
        prev_s, prev_e, 30, w30_s, w30_e, first_seen, as_of_ms, daily_agg, all_places,
    )

    # -- ⑦ time_space（小时 × 地点类分布，30 天；Task 12d） ------
    time_space = _time_space_matrix(
        conn, device_id, places_by_id, clipped30, day30s,
        w30_s, w30_e, first_seen, as_of_ms, daily_agg,
    )

    empty.update({
        "data_as_of": data_as_of_iso,
        "data_as_of_ms": as_of_ms,
        "frequent_places": _dedupe_frequent_places([p for w in freq_result for p in w["places"]]),
        "spatial_extent": spatial_extent,
        "commute_profile": commute_profile,
        "home_work_rhythm": home_work_rhythm,
        "scene_exposure": scene_exposure,
        "place_change": place_change,
        "time_space": time_space,
        "per_window": {
            "30": {"observed_bins": daily_agg["observed_bins"],
                   "expected_bins": _expected_bins(
                       max(w30_s, first_seen or w30_s),
                       min(w30_e, as_of_ms),
                       first_seen, as_of_ms)[0],
                   "quality": daily_agg,
                   "stays": len(stays30)},
            "7": {}, "90": {},
        },
    })
    return empty


def _dedupe_frequent_places(items):
    """同一 place 以 30 天窗口为准合并：7/90 仅补充窗口标签，不重复塞入三份。

    优先保留 30 天窗口条目，其余窗口并入其 per_window_days 标记。
    """
    by_pid = {}
    for it in items:
        pid = it["place_id"]
        cur = by_pid.get(pid)
        if cur is None or (it["window_days"] == 30 and cur["window_days"] != 30):
            # 以 30 天条目为准覆盖时，继承此前窗口（7/90）已积累的 windows 标记
            old_windows = cur.get("windows") if cur else None
            by_pid[pid] = dict(it)
            by_pid[pid]["windows"] = list(old_windows or [])
        by_pid[pid].setdefault("windows", []).append(it["window_days"])
    return [v for k, v in by_pid.items()]
