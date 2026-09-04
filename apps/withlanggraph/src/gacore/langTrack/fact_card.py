"""langTrack 事实卡片：纯读函数，双出口（system prompt compact / 工具完整 FactCard）。

外挂式设计（高内聚/低耦合）：

- 只读已有事实表（daily_stats / stays / trips / anomalies / places / etl_state），
  不加表、不改 ETL、不在注入路径触发 ETL（禁止 subprocess / etl.run）。
- ``build()`` 返回完整 ``FactCard``，并通过可注册的 ``_SECTION_BUILDERS`` 生成一次
  半结构化 compact；每个 section 只把已有事实格式化成一行，统一预算器按优先级装入
  600 字，不做行为推断或自然语言总结。
- ``render_compact(card)`` 只读返回已存文本，禁止 dashboard/context 再拼一份。
- 失败降级不挡对话：无库 / 缺表 / 异常 → ``available=False``、``has_facts=False``、
  ``persona={}``、``compact=""``。
- 禁止 import ``gacore.tools.langTrack_tools``（避免 tools → fact_card → tools 环）。

时间口径：东八区（UTC+8）显式时区，不依赖服务器本地时区。
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Final, Literal, TypedDict

from gacore.jsonl_logger import get_logger
from gacore.langTrack.location_facts import (
    PlaceRef,
    format_place,
    resolve_place_name,
    user_tag_of,
)
from gacore.langTrack.persona import build as build_persona

_TZ = datetime.timezone(datetime.timedelta(hours=8))
_DAY_MS: Final = 86_400_000
_MAX_COMPACT_CHARS: Final = 600
_MAX_TIMELINE_CHARS: Final = 260
_CARD_PREFIX: Final = "=== 生活事实（"

logger = get_logger("langTrack.fact_card")

# ---------------------------------------------------------------------------
# TypedDict 契约（§2.1）——不要临场改名
# ---------------------------------------------------------------------------


class CurrentKnown(TypedDict):
    # PlaceRef 地名载荷（§2.6）：与 StayBrief 保持一致，以 place_name 为显示名
    label: str
    place_id: str | None
    place_name: str
    user_tag: str
    name_source: str
    poi: str
    poi_fallback: str
    behavior: str
    district: str
    since_hhmm: str
    observed_until_hhmm: str


class StayBrief(TypedDict):
    # §2.6 兼容契约：新增 PlaceRef 字段，保留 label/poi 旧字段（label=format_place 结果）
    place_id: str | None
    label: str
    place_name: str
    user_tag: str
    name_source: str
    poi: str
    poi_fallback: str
    district: str
    behavior: str
    start_hhmm: str
    end_hhmm: str
    mins: int
    point_count: int
    avg_accuracy_m: float | None


class TripBrief(TypedDict):
    # §2.7 兼容契约：from_place/to_place 为 PlaceRef；保留 from_label/to_label 旧字段
    start_hhmm: str
    end_hhmm: str
    dist_m: int
    from_place: PlaceRef | None
    to_place: PlaceRef | None
    from_label: str
    to_label: str
    route_dist_m: int | None  # 高德实测距离；本轮未落实际距离 → None


class AnomalyBrief(TypedDict):
    kind: str
    poi: str
    detail: str


class PlaceBrief(TypedDict):
    # §2.6 兼容契约：label=format_place 结果；旧字段 visits/poi/behavior/address 已废弃待移除
    label: str
    visits: int  # deprecated：旧消费者兼容，映射 point_count
    poi: str  # deprecated：保留
    behavior: str  # deprecated：保留
    address: str  # deprecated：保留
    place_id: str | None
    place_name: str
    user_tag: str
    name_source: str
    poi_fallback: str
    district: str
    point_count: int
    visit_episodes: int
    stay_ms: int


class CompactSection(TypedDict):
    id: str
    text: str
    priority: int


class FactCard(TypedDict, total=False):
    device_id: str
    day: str
    available: bool
    has_facts: bool
    ambiguous_device: bool
    candidate_device_ids: list[str]
    generated_at: str
    etl_watermark: str
    etl_watermark_ms: int | None
    data_as_of: str
    data_as_of_ms: int | None
    data_as_of_source: str
    location_as_of: str
    location_as_of_ms: int | None
    data_age_min: int | None
    day_window_closed: bool
    current_known: CurrentKnown | None
    screen_ms: int
    screen_hours: float
    top_apps: list[dict]
    notification_count: int
    notification_clicked: int
    top_notification_apps: list[dict]
    screen_on_count: int
    screen_off_count: int
    unlock_count: int
    switch_count: int
    location_count: int
    audio_clip_count: int
    places: list[PlaceBrief]
    stays: list[StayBrief]
    trips: list[TripBrief]
    stay_minutes: dict[str, int]
    anomalies: list[AnomalyBrief]
    daily_location_quality: dict | None
    tag_conflict_count: int
    # Task 8 §2.9：长期空间画像（7/30/90 客观聚合，full 卡专属；compact 不携带）
    spatial_profile: dict | None
    midnight_audio_n: int | None
    sleep_signal: str
    sleep_start_hhmm: str | None
    sleep_end_hhmm: str | None
    sleep_duration_min: int | None
    time_app: list
    coverage: list[dict]
    persona: dict
    compact_sections: list[CompactSection]
    compact: str
    compact_chars: int
    compact_lines: list[str]
    compact_omitted: dict[str, str]
    card_fp: str


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(datetime.datetime.now(tz=_TZ).timestamp() * 1000)


def _today_str() -> str:
    return datetime.datetime.now(tz=_TZ).strftime("%Y-%m-%d")


def _hhmm(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=_TZ).strftime("%H:%M")


def _fmt_full(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _day_bounds_ms(day: str) -> tuple[int, int]:
    """东八区日界（毫秒）：[start, end)。午夜必须显式 UTC+8。"""
    start = datetime.datetime.fromisoformat(f"{day} 00:00:00").replace(tzinfo=_TZ)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _looks_like_coord(s: str) -> bool:
    """是否为网格坐标串（如 '31.97,118.76'）；grid 地点不进 compact 标记。"""
    if not s:
        return False
    return bool(re.fullmatch(r"[\d.\-]+\s*[,，]\s*[\d.\-]+", s.strip()))


def _row_val(row, col: str, default=None):
    """安全读取 sqlite3.Row 列；旧库缺列时返回 default，不抛异常。"""
    if row is None:
        return default
    try:
        v = row[col]
        return default if v is None else v
    except (KeyError, IndexError):
        return default


def _parse_json_list(raw) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


# ---------------------------------------------------------------------------
# 设备锁定
# ---------------------------------------------------------------------------

_DEVICE_SOURCE_TABLES: Final = ("daily_stats", "stays", "etl_state")


def _resolve_device(conn: sqlite3.Connection, device_id: str | None):
    """传参优先；未传时从事实表并集取 distinct，恰好一个才自动选。

    返回 (device_id | None, candidates, ambiguous)。多设备未指定 → 歧义降级。
    """
    if device_id:
        return device_id, [device_id], False
    ids: set[str] = set()
    legacy_daily_stats = False
    for table in _DEVICE_SOURCE_TABLES:
        try:
            rows = conn.execute(f"SELECT DISTINCT device_id FROM {table}").fetchall()
            for r in rows:
                v = r[0]
                if v is not None and v != "":
                    ids.add(str(v))
        except sqlite3.OperationalError:
            # 旧库 daily_stats 无 device_id 列：整表视为单设备（兼容）
            if table == "daily_stats":
                legacy_daily_stats = True
            continue
    ids = sorted(ids)
    if len(ids) == 1:
        return ids[0], ids, False
    if len(ids) > 1:
        return None, ids, True
    if legacy_daily_stats:
        return None, [], False  # 旧库单设备：device_id=None，走 legacy 读取
    return None, [], False


# ---------------------------------------------------------------------------
# 查询（位置事实经 location_reader 双读层，v1: grid_key 关联 / v2: place_id 关联）
# PlaceRef 地名解析只发生在本模块（§2.6 resolver 唯一入口），服务方不得再造第二份。
# ---------------------------------------------------------------------------


def _load_places(conn: sqlite3.Connection, device_id: str | None) -> tuple[dict, dict]:
    """读取该设备全部 canonical places（含命名证据列），构建两个索引。

    返回 (by_id, by_grid)：v2 优先按 place_id 寻址；v1/缺表按 grid_key 回落。
    读取失败降级为空索引不抛——stay 内嵌字段仍可用，展示降级为「未知地点」。
    """
    by_id: dict[str, dict] = {}
    by_grid: dict[str, dict] = {}
    if device_id is None:
        return by_id, by_grid
    try:
        from gacore.langTrack import location_reader as lr

        for p in lr.read_places(conn, device_id=device_id):
            if p.get("place_id"):
                by_id[str(p["place_id"])] = p
            if p.get("grid_key"):
                by_grid.setdefault(str(p["grid_key"]), p)
        # v2：place_cells 成员网格展开（同网格多 place 取 visit_count 高者，已按访问降序）
        if by_id:
            for c in lr.read_place_cells(conn, device_id=device_id):
                p = by_id.get(str(c.get("place_id")))
                if p is not None and c.get("grid_key"):
                    by_grid.setdefault(str(c["grid_key"]), p)
    except Exception:  # noqa: BLE001 - 缺表/坏库降级
        return {}, {}
    return by_id, by_grid


def _place_of_stay(
    stay: dict,
    by_id: dict[str, dict] | None = None,
    by_grid: dict[str, dict] | None = None,
) -> dict | None:
    """stay 关联的 canonical place（含命名证据列）。

    解析顺序：v2 按 stay.place_id 取 → 按 stay.grid_key 查 → 回退为 location_reader
    内嵌 place 字段（只剩基本语义、命名证据归零，展示降级）。全无地点信息返回 None。
    """
    by_id = by_id or {}
    by_grid = by_grid or {}
    top = stay.get("place_id")
    if top:
        p = by_id.get(str(top))
        if p is not None:
            return p
    gk = stay.get("grid_key")
    if gk:
        p = by_grid.get(str(gk))
        if p is not None:
            return p
    if stay.get("place_label") is None and stay.get("place_poi") is None:
        return None
    return {
        "place_id": stay.get("place_id"),
        "grid_key": stay.get("grid_key"),
        "label": stay.get("place_label") or "",
        "poi": stay.get("place_poi") or "",
        "poi_fallback": stay.get("place_poi_fallback") or "",
        "address": stay.get("place_address") or "",
        "behavior": stay.get("place_behavior") or "",
        "district": stay.get("place_district") or "",
        "township": "",
        "business_area": "",
        "parent_poi": "",
        "name_confidence": 0.0,
        "name_evidence": "",
        "point_count": int(stay.get("n_points") or 0),
        "visit_episodes": 0,
        "stay_ms": 0,
    }


def _place_ref(place: dict | None) -> PlaceRef | None:
    """唯一 PlaceRef 派生（§2.6）：只调 resolve_place_name + user_tag_of。"""
    if not place:
        return None
    name, source, gran = resolve_place_name(
        poi=place.get("poi") or "",
        poi_fallback=place.get("poi_fallback") or "",
        address=place.get("address") or "",
        district=place.get("district") or "",
        township=place.get("township") or "",
        business_area=place.get("business_area") or "",
        parent_poi=place.get("parent_poi") or "",
        name_confidence=float(place.get("name_confidence") or 0.0),
        label=place.get("label") or "",
    )
    return PlaceRef(
        place_id=str(place.get("place_id") or "") or "",
        grid_key=str(place.get("grid_key") or "") or "",
        place_name=name,
        name_source=source,
        user_tag=user_tag_of(place.get("label") or ""),
        poi=place.get("poi") or "",
        poi_fallback=place.get("poi_fallback") or "",
        address=place.get("address") or "",
        district=place.get("district") or "",
        township=place.get("township") or "",
        business_area=place.get("business_area") or "",
        parent_poi=place.get("parent_poi") or "",
        behavior=place.get("behavior") or "",
        display_granularity=gran,
        name_confidence=float(place.get("name_confidence") or 0.0),
        name_evidence=place.get("name_evidence") or "",
    )


def _friendly_label(place: dict | None) -> str:
    """展示名：format_place(place_name, user_tag)（§2.6），compact 与旧 label 字段共用。"""
    ref = _place_ref(place)
    if ref is None:
        return "未知地点"
    return format_place(ref["place_name"], ref["user_tag"])


def _load_stays(conn: sqlite3.Connection, device_id: str | None, day_start_ms: int, day_end_ms: int) -> list[dict]:
    """与日窗相交的 stay（时间窗相交，不 WHERE day=?）。

    可选事实表缺失（stays 表不存在）时返回空列表，不整卡降级——
    有 daily_stats 仍应能生成手机事实。
    """
    if device_id is None:
        return []
    from gacore.langTrack import location_reader as lr

    return lr.read_stays(conn, device_id=device_id, overlap=(day_start_ms, day_end_ms))


def _load_trips(conn: sqlite3.Connection, device_id: str | None, day_start_ms: int, day_end_ms: int) -> list[dict]:
    """同 _load_stays：trips 表缺失时返回空列表，不清空手机事实。"""
    if device_id is None:
        return []
    from gacore.langTrack import location_reader as lr

    return lr.read_trips(conn, device_id=device_id, overlap=(day_start_ms, day_end_ms))


def _load_anomalies(conn: sqlite3.Connection, device_id: str | None, day: str) -> list[dict]:
    """同 _load_stays：anomalies 表缺失时返回空列表，不清空手机事实。"""
    if device_id is None:
        return []
    from gacore.langTrack import location_reader as lr

    rows = lr.read_anomalies(conn, day=day, device_id=device_id)
    return [
        {"kind": r["kind"] or "", "poi": r["poi"] or "", "detail": r["detail"] or ""}
        for r in rows
    ]


def _read_daily_stats(conn: sqlite3.Connection, device_id: str | None, day: str):
    """读取当日 daily_stats 行；legacy（无 device 列）回退 WHERE day=?。"""
    if device_id is not None:
        try:
            return conn.execute(
                "SELECT * FROM daily_stats WHERE device_id=? AND day=?", (device_id, day)
            ).fetchone()
        except sqlite3.OperationalError:
            pass  # 旧库无 device_id 列 → 单设备读取
    try:
        return conn.execute("SELECT * FROM daily_stats WHERE day=?", (day,)).fetchone()
    except sqlite3.OperationalError:
        return None


# ---------------------------------------------------------------------------
# build 主入口
# ---------------------------------------------------------------------------


def _new_card(day: str, now_ms: int) -> FactCard:
    return FactCard(
        device_id="",
        day=day,
        available=False,
        has_facts=False,
        ambiguous_device=False,
        candidate_device_ids=[],
        generated_at=_fmt_full(now_ms),
        etl_watermark="",
        etl_watermark_ms=None,
        data_as_of="",
        data_as_of_ms=None,
        data_as_of_source="unknown",
        location_as_of="",
        location_as_of_ms=None,
        data_age_min=None,
        day_window_closed=False,
        current_known=None,
        screen_ms=0,
        screen_hours=0.0,
        top_apps=[],
        notification_count=0,
        notification_clicked=0,
        top_notification_apps=[],
        screen_on_count=0,
        screen_off_count=0,
        unlock_count=0,
        switch_count=0,
        location_count=0,
        audio_clip_count=0,
        places=[],
        stays=[],
        trips=[],
        stay_minutes={},
        anomalies=[],
        daily_location_quality=None,
        tag_conflict_count=0,
        spatial_profile=None,
        midnight_audio_n=None,
        sleep_signal="",
        sleep_start_hhmm=None,
        sleep_end_hhmm=None,
        sleep_duration_min=None,
        time_app=[],
        coverage=[],
        persona={},
        compact_sections=[],
        compact="",
        compact_chars=0,
        compact_lines=[],
        compact_omitted={},
        card_fp="",
    )


def _degrade(card: FactCard, error_type: str, error: str, t0: float, outlet: str) -> FactCard:
    """失败降级：置空并打一条 warning 日志；日志失败不影响主路径。"""
    card["available"] = False
    card["has_facts"] = False
    card["persona"] = {}
    card["compact"] = ""
    card["compact_chars"] = 0
    card["compact_lines"] = []
    card["compact_omitted"] = {}
    card["compact_sections"] = []
    card["card_fp"] = ""
    with contextlib.suppress(Exception):
        logger.warning(
            "fact card degraded",
            outlet=outlet,
            day=card["day"],
            device_id=card["device_id"],
            error_type=error_type,
            error=error[:200],
            elapsed_ms=round((time.time() - t0) * 1000, 1),
            available=False,
        )
    return card


def build(
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    day: str | None = None,
    device_id: str | None = None,
    now_ms: int | None = None,
    detail: Literal["compact", "full"] = "full",
    outlet: str = "unknown",
) -> FactCard:
    """构建事实卡片（纯读）。返回完整 ``FactCard``；失败降级为空卡不抛。"""
    t0 = time.time()
    day = day or _today_str()
    if now_ms is None:
        now_ms = _now_ms()
    card = _new_card(day, now_ms)

    own = False
    try:
        if conn is None:
            if db_path is None:
                db_path = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"
            path = Path(db_path)
            if not path.exists():
                return _degrade(card, "db_missing", "langTrack 数据库不存在", t0, outlet)
            conn = sqlite3.connect(str(path))
            own = True
        conn.row_factory = sqlite3.Row
        _fill_card(conn, card, day, device_id, now_ms, detail)
    except Exception as e:  # noqa: BLE001 - 缺表/锁库等一律降级
        _degrade(card, type(e).__name__, str(e), t0, outlet)
    finally:
        if own and conn is not None:
            conn.close()

    if card["compact"] or card["compact_sections"]:
        _log_built(card, t0, outlet)
    return card


def _fill_card(
    conn: sqlite3.Connection,
    card: FactCard,
    day: str,
    device_id: str | None,
    now_ms: int,
    detail: Literal["compact", "full"],
) -> None:
    dev, candidates, ambiguous = _resolve_device(conn, device_id)
    card["device_id"] = dev or ""
    card["candidate_device_ids"] = candidates
    card["ambiguous_device"] = ambiguous
    if ambiguous:
        # 多设备未指定 → 降级空卡，保留候选供 dashboard 显示歧义
        return
    today = _today_str()
    if day > today:
        return  # 未来日期无当日事实
    is_today = day == today
    day_start_ms, day_end_ms = _day_bounds_ms(day)

    # ETL 全局水位：只读该设备 etl_state.last_event_ts；缺表/空值保持 None
    etl_wm = None
    if dev is not None:
        try:
            row = conn.execute(
                "SELECT last_event_ts FROM etl_state WHERE device_id=?", (dev,)
            ).fetchone()
            if row is not None and row["last_event_ts"]:
                etl_wm = int(row["last_event_ts"])
        except sqlite3.OperationalError:
            etl_wm = None
    card["etl_watermark_ms"] = etl_wm
    card["etl_watermark"] = _fmt_full(etl_wm) if etl_wm else ""

    # 当日 daily_stats（legacy 兼容单设备）
    stat = _read_daily_stats(conn, dev, day)
    card["available"] = stat is not None
    if stat is not None:
        _map_daily_stats(card, stat)
        if detail == "compact":
            card["sleep_signal"] = "未计"
    else:
        card["sleep_signal"] = "当日无 daily_stats"

    # 当日 stays / trips（时间窗相交；place 字段由 location_reader 内嵌）
    stays_raw = _load_stays(conn, dev, day_start_ms, day_end_ms)
    trips_raw = _load_trips(conn, dev, day_start_ms, day_end_ms)
    # 地点索引：全卡 stay/trip/place 的 PlaceRef 解析唯一数据源（§2.6）
    place_by_id, place_by_grid = _load_places(conn, dev)

    # 事实水位：ETL 优先，其次当日 stays/trips 最大 end_ts
    fallback = None
    for s in stays_raw:
        fallback = max(fallback or 0, s["end_ts"])
    for t in trips_raw:
        fallback = max(fallback or 0, t["end_ts"])

    if etl_wm is not None:
        source = "etl_state"
        source_ms = etl_wm
    elif fallback is not None:
        source = "stay_trip_fallback"
        source_ms = fallback
    else:
        source = "unknown"
        source_ms = None

    cutoff = None
    if source_ms is not None and source_ms >= day_start_ms:
        # 有有效水位：cutoff = min(日末, now, 水位)
        cutoff = min(day_end_ms, now_ms, source_ms)
    card["data_as_of_source"] = source
    card["data_as_of_ms"] = cutoff
    card["data_as_of"] = _fmt_full(cutoff) if cutoff else ""
    if is_today and source_ms is not None and source_ms <= now_ms and cutoff is not None:
        card["data_age_min"] = max(0, (now_ms - cutoff) // 60000)
    elif is_today and source_ms is not None and source_ms > now_ms:
        card["data_age_min"] = None  # 未来异常水位 → 无负年龄
    card["day_window_closed"] = (not is_today) and etl_wm is not None and etl_wm >= day_end_ms

    # 裁剪 stays/trips 到 [日界, cutoff]，不延伸到 now 之后
    clipped_stays: list[tuple[dict, int, int]] = []
    for s in stays_raw:
        cs, ce = s["start_ts"], s["end_ts"]
        if cutoff is not None:
            ce = min(ce, cutoff)
        cs = max(cs, day_start_ms)
        ce = min(ce, day_end_ms)
        if cs < ce:
            clipped_stays.append((s, cs, ce))
    clipped_trips: list[tuple[dict, int, int]] = []
    for t in trips_raw:
        cs, ce = t["start_ts"], t["end_ts"]
        if cutoff is not None:
            ce = min(ce, cutoff)
        cs = max(cs, day_start_ms)
        ce = min(ce, day_end_ms)
        if cs < ce:
            clipped_trips.append((t, cs, ce))
    clipped_stays.sort(key=lambda x: x[1])
    clipped_trips.sort(key=lambda x: x[1])

    # stay_minutes：按人工 tag 聚合（家/公司/其他/未知）；StayBrief 带全量 PlaceRef 载荷
    stay_minutes: dict[str, int] = {}
    stay_briefs: list[StayBrief] = []
    for s, cs, ce in clipped_stays:
        place = _place_of_stay(s, place_by_id, place_by_grid)
        ref = _place_ref(place)
        label = _friendly_label(place)
        poi = (place.get("poi") or "") if place else ""
        mins = (ce - cs) // 60000
        tag = (ref or {}).get("user_tag", "")
        if tag == "家":
            bucket = "家"
        elif tag == "公司":
            bucket = "公司"
        elif label == "未知地点":
            bucket = "未知"
        else:
            bucket = "其他"
        stay_minutes[bucket] = stay_minutes.get(bucket, 0) + mins
        stay_briefs.append(StayBrief(
            place_id=(str((place or {}).get("place_id") or "") if place else None),
            label=label,
            place_name=(ref or {}).get("place_name", ""),
            user_tag=tag,
            name_source=(ref or {}).get("name_source", ""),
            poi=poi,
            poi_fallback=(place.get("poi_fallback") or "") if place else "",
            district=(place.get("district") or "") if place else "",
            behavior=(place.get("behavior") or "") if place else "",
            start_hhmm=_hhmm(cs),
            end_hhmm=_hhmm(ce),
            mins=mins,
            point_count=int((place or {}).get("point_count") or 0),
            avg_accuracy_m=s.get("avg_accuracy_m"),
        ))

    # location_as_of：裁剪后 stays 最大 end_ts
    loc_as_of = max((ce for _, _, ce in clipped_stays), default=None)
    card["location_as_of_ms"] = loc_as_of
    card["location_as_of"] = _fmt_full(loc_as_of) if loc_as_of else ""

    # current_known：覆盖 cutoff 的最后一段 stay（闭区间）
    current_known: CurrentKnown | None = None
    if cutoff is not None:
        covering = [(s, cs, ce) for s, cs, ce in clipped_stays if s["start_ts"] <= cutoff <= ce]
        if covering:
            s, cs, ce = covering[-1]
            place = _place_of_stay(s, place_by_id, place_by_grid)
            ref = _place_ref(place)
            current_known = CurrentKnown(
                label=_friendly_label(place),
                place_id=(str((place or {}).get("place_id") or "") if place else None),
                place_name=(ref or {}).get("place_name", ""),
                user_tag=(ref or {}).get("user_tag", ""),
                name_source=(ref or {}).get("name_source", ""),
                poi=(place.get("poi") or "") if place else "",
                poi_fallback=(place.get("poi_fallback") or "") if place else "",
                behavior=(place.get("behavior") or "") if place else "",
                district=(place.get("district") or "") if place else "",
                since_hhmm=_hhmm(cs),
                observed_until_hhmm=_hhmm(ce),
            )
    card["current_known"] = current_known
    card["stays"] = stay_briefs

    # trips：逐条匹配前后最近 stay（禁全日首尾复用）；from/to PlaceRef 直取（§2.7）
    trip_briefs: list[TripBrief] = []
    for t, cs, ce in clipped_trips:
        prev_label, next_label = "", ""
        prev_candidates = [(s[2], s) for s in clipped_stays if s[2] <= t["start_ts"]]
        next_candidates = [(s[1], s) for s in clipped_stays if s[1] >= t["end_ts"]]
        if prev_candidates:
            prev_candidates.sort(key=lambda x: x[0], reverse=True)
            prev_label = _stay_label(prev_candidates[0][1], place_by_id, place_by_grid)
        if next_candidates:
            next_candidates.sort(key=lambda x: x[0])
            next_label = _stay_label(next_candidates[0][1], place_by_id, place_by_grid)
        from_place = None
        if t.get("from_place_id"):
            fp = place_by_id.get(str(t["from_place_id"]))
            from_place = _place_ref(fp) if fp else None
        to_place = None
        if t.get("to_place_id"):
            tp = place_by_id.get(str(t["to_place_id"]))
            to_place = _place_ref(tp) if tp else None
        trip_briefs.append(TripBrief(
            start_hhmm=_hhmm(cs), end_hhmm=_hhmm(ce),
            dist_m=int(t["dist_m"] or 0),
            from_place=from_place,
            to_place=to_place,
            from_label=prev_label, to_label=next_label,
            route_dist_m=t.get("route_dist_m"),
        ))
    card["trips"] = trip_briefs
    card["stay_minutes"] = stay_minutes
    card["anomalies"] = [AnomalyBrief(**a) for a in _load_anomalies(conn, dev, day)]

    card["has_facts"] = bool(card["available"]) or bool(stay_briefs) or bool(trip_briefs)

    # 凌晨音频：仅 full 模式扫 events（注入路径不扫）；
    # 无 daily_stats 时保持"当日无 daily_stats"，不因 0 样本误报"未见熬夜信号"
    if detail == "full" and card.get("available"):
        if dev is not None:
            try:
                n = conn.execute(
                    "SELECT COUNT(*) c FROM events WHERE type='audio_env' AND device_id=? "
                    "AND date(ts/1000,'unixepoch','+8 hours')=? "
                    "AND strftime('%H', ts/1000,'unixepoch','+8 hours') BETWEEN '00' AND '05'",
                    (dev, day),
                ).fetchone()["c"]
                card["midnight_audio_n"] = int(n)
                card["sleep_signal"] = "凌晨 00-05 点仍有环境音频样本，疑似熬夜" if n > 5 else "未见熬夜信号"
            except sqlite3.OperationalError:
                card["midnight_audio_n"] = None
        else:
            card["midnight_audio_n"] = None

    if detail == "full":
        _fill_full_extras(conn, card, dev, day)

    # 拼 compact（section builders + 预算器）
    _pack_compact(card)


def _stay_label(stay_tuple, by_id: dict[str, dict], by_grid: dict[str, dict]) -> str:
    s, _, _ = stay_tuple
    return _friendly_label(_place_of_stay(s, by_id, by_grid))


def _map_daily_stats(card: FactCard, stat) -> None:
    card["screen_ms"] = int(_row_val(stat, "total_screen_ms", 0) or 0)
    card["screen_hours"] = round(card["screen_ms"] / 3600000, 2)
    card["top_apps"] = _parse_json_list(_row_val(stat, "app_ranking_json"))[:8]
    card["notification_count"] = int(_row_val(stat, "notification_count", 0) or 0)
    card["notification_clicked"] = int(_row_val(stat, "notification_clicked", 0) or 0)
    card["top_notification_apps"] = _parse_json_list(_row_val(stat, "top_notification_apps_json"))
    card["screen_on_count"] = int(_row_val(stat, "screen_on_count", 0) or 0)
    card["screen_off_count"] = int(_row_val(stat, "screen_off_count", 0) or 0)
    card["unlock_count"] = int(_row_val(stat, "unlock_count", 0) or 0)
    card["switch_count"] = int(_row_val(stat, "switch_count", 0) or 0)
    card["location_count"] = int(_row_val(stat, "location_count", 0) or 0)
    card["audio_clip_count"] = int(_row_val(stat, "audio_clip_count", 0) or 0)
    card["sleep_start_hhmm"] = _row_val(stat, "sleep_start_hhmm")
    card["sleep_end_hhmm"] = _row_val(stat, "sleep_end_hhmm")
    card["sleep_duration_min"] = _row_val(stat, "sleep_duration_min")
    card["time_app"] = _parse_json_list(_row_val(stat, "time_app_json"))


def _fill_full_extras(conn: sqlite3.Connection, card: FactCard, dev: str | None, day: str) -> None:
    """full 模式补齐：全历史 places / coverage / persona。不查 events 之外的非必要数据。"""
    if dev is not None:
        from gacore.langTrack import location_reader as lr

        try:
            rows = lr.read_places(conn, device_id=dev, limit=4)
            card["places"] = []
            for r in rows:
                ref = _place_ref(r)
                card["places"].append(PlaceBrief(
                    label=_friendly_label(r),
                    visits=int(r.get("point_count") or 0),
                    poi=r.get("poi") or "",
                    behavior=r.get("behavior") or "",
                    address=r.get("address") or "",
                    place_id=str(r.get("place_id") or "") or None,
                    place_name=(ref or {}).get("place_name", ""),
                    user_tag=(ref or {}).get("user_tag", ""),
                    name_source=(ref or {}).get("name_source", ""),
                    poi_fallback=r.get("poi_fallback") or "",
                    district=r.get("district") or "",
                    point_count=int(r.get("point_count") or 0),
                    visit_episodes=int(r.get("visit_episodes") or 0),
                    stay_ms=int(r.get("stay_ms") or 0),
                ))
        except Exception:  # noqa: BLE001 - 缺表降级
            card["places"] = []
        # full 卡透传（Task 6 §3.2 / 人工 tag 冲突；v1/缺表 → None/0）
        try:
            card["daily_location_quality"] = lr.read_daily_quality(
                conn, device_id=dev, day=day
            )
        except Exception:  # noqa: BLE001
            card["daily_location_quality"] = None
        try:
            card["tag_conflict_count"] = lr.read_tag_conflict_count(conn, device_id=dev)
        except Exception:  # noqa: BLE001
            card["tag_conflict_count"] = 0
        # Task 8 §2.9：长期空间画像（7/30/90 聚合，纯读；缺表/无数据 → 空骨架 None）
        try:
            from gacore.langTrack.spatial_profile import build_spatial_profile

            card["spatial_profile"] = build_spatial_profile(
                conn, device_id=dev, as_of_day=day
            )
        except Exception:  # noqa: BLE001 - 缺表/空库降级
            card["spatial_profile"] = None
    # A① 契约覆盖：非 ok 类型
    coverage: list[dict] = []
    try:
        cov_rows = conn.execute(
            "SELECT type, desc, status, last_seen_ts, consumed FROM contract_coverage "
            "WHERE status != 'ok' ORDER BY status, type"
        ).fetchall()
        coverage = [
            {"type": r["type"], "desc": r["desc"], "status": r["status"],
             "last_seen": r["last_seen_ts"], "consumed": r["consumed"]}
            for r in cov_rows
        ]
    except sqlite3.OperationalError:
        coverage = []
    card["coverage"] = coverage
    # persona：完整卡/dashboard 对照用（compact 注入路径不调用）
    try:
        card["persona"] = build_persona(conn=conn, device_id=dev, days=7)
    except Exception:  # noqa: BLE001 - 缺表降级
        card["persona"] = {}


# ---------------------------------------------------------------------------
# compact：section builders + 预算器（§2.2）
# ---------------------------------------------------------------------------


def _build_waterline_section(card: FactCard) -> CompactSection | None:
    """标题 + 数据水位（priority 0，强制）。"""
    is_today = card["day"] == _today_str()
    if is_today:
        if card["data_age_min"] is not None and card["data_as_of_ms"]:
            status = f"今日未完；数据至 {_hhmm(card['data_as_of_ms'])}，距现在 {card['data_age_min']} 分"
        elif card["data_as_of_ms"]:
            status = f"今日未完；数据至 {_hhmm(card['data_as_of_ms'])}"
        else:
            status = "今日未完；数据水位未知"
    else:
        status = "历史日"
    return CompactSection(id="waterline", text=f"{_CARD_PREFIX}{status}）===", priority=0)


def _build_timeline_section(card: FactCard) -> CompactSection | None:
    """今日轨迹：按时序排列裁剪后 stays；trips 只报移动段数。"""
    stays = card.get("stays") or []
    trips = card.get("trips") or []
    if not stays:
        if trips:
            return CompactSection(
                id="timeline", text=f"今日移动：{len(trips)} 段（地点端点未知）", priority=10,
            )
        return None
    parts = [f"{s['label']} {s['start_hhmm']}-{s['end_hhmm']}" for s in stays]
    line = "今日轨迹：" + " → ".join(parts)
    if trips:
        line += f"；移动 {len(trips)} 段"
    # 超长：保留最早 2 段 + 最近 3 段，中间折叠，禁止字符串硬切
    if len(line) > _MAX_TIMELINE_CHARS and len(parts) > 5:
        hidden = len(parts) - 5
        line = (
            "今日轨迹：" + " → ".join(parts[:2]) +
            f" → …另 {hidden} 段… → " + " → ".join(parts[-3:])
        )
        if trips:
            line += f"；移动 {len(trips)} 段"
    return CompactSection(id="timeline", text=line, priority=10)


def _build_current_section(card: FactCard) -> CompactSection | None:
    """当前已知：覆盖 cutoff 的最后一段可证明位置（明写区间终点，不称「此刻」）。"""
    ck = card.get("current_known")
    if not ck:
        return None
    line = f"当前已知：{ck['label']} {ck['since_hhmm']}-{ck['observed_until_hhmm']}"
    if ck.get("district"):
        line += f" · {ck['district']}"
    return CompactSection(id="current", text=line, priority=20)


def _build_stay_section(card: FactCard) -> CompactSection | None:
    """停留累计：裁剪后 stays 按 label 纯聚合（时长求和，不推断上班/居家）。"""
    sm = card.get("stay_minutes") or {}
    items = []
    for key in ("家", "公司", "其他", "未知"):
        mins = sm.get(key, 0)
        if mins > 0:
            items.append(f"{key} {mins / 60:.1f}h")
    if not items:
        return None
    return CompactSection(id="stay", text="停留累计：" + " · ".join(items), priority=30)


def _build_phone_section(card: FactCard) -> CompactSection | None:
    """手机累计：屏幕/解锁/切换/App 前二及时长（当日 stats）。"""
    if not card.get("available"):
        return None
    parts = [f"屏幕 {card.get('screen_hours', 0.0):.1f}h"]
    if card.get("unlock_count") is not None and card["unlock_count"] > 0:
        parts.append(f"解锁 {card['unlock_count']}")
    if card.get("switch_count") is not None and card["switch_count"] > 0:
        parts.append(f"切换 {card['switch_count']}")
    apps = card.get("top_apps") or []
    if apps:
        app_parts = [f"{a.get('app','')} {a.get('ms',0)/3600000:.1f}h" for a in apps[:2]]
        parts.append(" / ".join(app_parts))
    return CompactSection(id="phone", text="手机累计：" + " · ".join(parts), priority=40)


def _build_notification_section(card: FactCard) -> CompactSection | None:
    """通知累计：总数/点击数/Top 来源；缺字段局部省略。

    门禁：必须有当日 daily_stats（available）才报通知数——无 stats 时
    notification_count 为 0，不得输出「通知累计：0 条」冒充事实。
    """
    if not card.get("available"):
        return None
    n = card.get("notification_count")
    if n is None:
        return None
    parts = [f"{n} 条"]
    if card.get("notification_clicked"):
        parts.append(f"点击 {card['notification_clicked']} 条")
    srcs = card.get("top_notification_apps") or []
    if srcs:
        parts.append("来源 " + "/".join(str(s.get("app", "")) for s in srcs[:3] if s.get("app")))
    return CompactSection(id="notifications", text="通知累计：" + " · ".join(parts), priority=50)


def _build_tag_section(card: FactCard) -> CompactSection | None:
    """系统标记：anomalies.kind + 非网格 poi，最多 2 条；不贴 detail。"""
    anoms = [a for a in (card.get("anomalies") or []) if not _looks_like_coord(a.get("poi") or "")]
    if not anoms:
        return None
    tags = []
    for a in anoms[:2]:
        tag = f"#{a['kind']}"
        if a.get("poi"):
            tag += f" {a['poi']}"
        tags.append(tag)
    return CompactSection(id="tags", text="系统标记：" + " ".join(tags), priority=60)


_SECTION_BUILDERS = (
    _build_waterline_section,
    _build_timeline_section,
    _build_current_section,
    _build_stay_section,
    _build_phone_section,
    _build_notification_section,
    _build_tag_section,
)


def _pack_compact(card: FactCard) -> None:
    """运行全部 section builders 并做 600 字预算；记录 included / omitted。

    稳定性：按 priority 排序；预算器只整段纳入/整段省略，禁止截半文本。
    门禁：has_facts=False 时不运行 builders（水位/tag 不能单独成卡）。
    """
    if not card.get("has_facts"):
        card["compact_sections"] = []
        card["compact"] = ""
        card["compact_chars"] = 0
        card["compact_lines"] = []
        card["compact_omitted"] = {}
        card["card_fp"] = ""
        return
    sections: list[CompactSection] = []
    for builder in _SECTION_BUILDERS:
        sec = builder(card)
        if sec is not None:
            sections.append(sec)
    sections.sort(key=lambda s: s["priority"])

    included: list[str] = []
    omitted: dict[str, str] = {}
    total = 0
    lines: list[str] = []
    for sec in sections:
        if total + len(sec["text"]) + 1 <= _MAX_COMPACT_CHARS:
            lines.append(sec["text"])
            included.append(sec["id"])
            total += len(sec["text"]) + 1
        else:
            omitted[sec["id"]] = "budget"

    compact = "\n".join(lines)
    card["compact_sections"] = sections
    card["compact"] = compact
    card["compact_chars"] = len(compact)
    card["compact_lines"] = included
    card["compact_omitted"] = omitted
    card["card_fp"] = hashlib.sha256(compact.encode("utf-8")).hexdigest()[:12] if compact else ""


def render_compact(card: FactCard) -> str:
    """只读返回已存 compact 文本；不二次重组。"""
    return card.get("compact", "")


# ---------------------------------------------------------------------------
# 维测日志（§2.6 A）
# ---------------------------------------------------------------------------


def _log_built(card: FactCard, t0: float, outlet: str) -> None:
    """build 完成（含 compact 为空但有 sections）打一条 info；日志失败不影响主路径。"""
    with contextlib.suppress(Exception):
        ck = card.get("current_known")
        anoms = card.get("anomalies") or []
        logger.info(
            "fact card built",
            outlet=outlet,
            day=card["day"],
            device_id=card["device_id"],
            available=card["available"],
            has_facts=card["has_facts"],
            elapsed_ms=round((time.time() - t0) * 1000, 1),
            generated_at=card["generated_at"],
            etl_watermark=card["etl_watermark"],
            data_as_of=card["data_as_of"],
            data_as_of_source=card["data_as_of_source"],
            location_as_of=card["location_as_of"],
            data_age_min=card["data_age_min"],
            current_label=(ck or {}).get("label", ""),
            current_since=(ck or {}).get("since_hhmm", ""),
            current_until=(ck or {}).get("observed_until_hhmm", ""),
            district=(ck or {}).get("district", ""),
            timeline_n=len(card.get("stays") or []),
            timeline_compact=(card.get("compact") or "").splitlines()[1] if len((card.get("compact") or "").splitlines()) > 1 else "",
            screen_hours=card.get("screen_hours", 0.0),
            unlock_count=card.get("unlock_count", 0),
            top_apps=[a.get("app") for a in (card.get("top_apps") or [])[:2]],
            midnight_audio_n=card.get("midnight_audio_n"),
            anomaly_kind=anoms[0].get("kind", "") if anoms else "",
            anomaly_poi=anoms[0].get("poi", "") if anoms else "",
            daily_quality_present=int(card.get("daily_location_quality") is not None),
            tag_conflict_count=card.get("tag_conflict_count", 0),
            lines=card.get("compact_lines", []),
            compact=card.get("compact", ""),
            compact_chars=card.get("compact_chars", 0),
            card_fp=card.get("card_fp", ""),
            omitted=card.get("compact_omitted", {}),
        )
