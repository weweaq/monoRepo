"""langTrack 位置事实 v1/v2 双读层（Task 5）。

读取类消费者（fact_card / report / persona / label CLI / dashboard 的位置
事实段 / tools 的位置查询）读取 places / stays / trips / anomalies 一律经
本模块，禁止在消费者里直接 JOIN 事实表。（例外：dashboard 日期导航仅取
day 列、geocode / routes 作为生产者直写直读，不经本模块。）

- v1（PRAGMA user_version < 2）：stays ↔ places 按 (device_id, grid_key) 关联；
- v2（user_version >= 2）：按 (device_id, place_id) 关联，stays.grid_key 只是
  stay 自身网格，不再等于 place 代表网格。

统一行结构（兼容契约：只增字段、不删字段、不改旧字段含义）：

- place：含 place_id / visit_count / visit_episodes / point_count / stay_ms。
  v1 无点数/段数区分：place_id=None，point_count 与 visit_episodes 都映射
  visit_count（近似值）；v2 中 visit_count=visit_episodes=落入地点的 stay 段数，
  point_count=成员网格内原始 location 点数。
- stay：内嵌关联 place 的 place_label / place_poi / place_poi_fallback /
  place_address / place_behavior / place_district（JOIN 在本模块一处实现）。
- trip：带 from_place_id / to_place_id（v1 恒为 None）。
- anomaly：带 place_id（v1 恒为 None）。

容错语义（与 fact_card 既有约定一致）：

- 表缺失 → 返回空列表，位置事实缺失不拖垮手机事实；
- 列缺失（最小 schema / 旧测试库）→ SELECT 里补 NULL AS col，行结构不变；
- WHERE 引用了缺失列的查询 → OperationalError 兜底返回空列表。
"""

from __future__ import annotations

import sqlite3

# v1/v2 都存在的 places 公共列（消费者字段并集）。
_PLACE_COLS = (
    "id, device_id, grid_key, lat, lon, label, first_seen, last_seen, "
    "visit_count, is_primary, address, poi, poi_fallback, district, township, "
    "business_area, poi_type, behavior, matched_level, candidate_label, "
    "confidence_home, confidence_work, geocoded_at, poi_l1, poi_l2, poi_l3"
)

# v2 新增计数列（v1 无，由 reader 兼容映射）。
_PLACE_V2_ONLY = ", place_id, point_count, stay_ms"

# v2 新增命名证据列（v1 无：恒 NULL → 归一化默认值；FactCard PlaceRef 用）。
_PLACE_NAME_V2_ONLY = ", name_confidence, name_evidence, parent_poi"

_STAY_COLS = (
    "device_id, start_ts, end_ts, duration_ms, center_lat, center_lon, "
    "min_lat, min_lon, max_lat, max_lon, n_points, radius_m, grid_key, day, "
    "avg_accuracy_m"
)

_TRIP_COLS = (
    "device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "
    "end_lat, end_lon, dist_m, n_points, day"
)

_TRIP_V2_ONLY = ", from_place_id, to_place_id, polyline, route_key, route_mode, route_encoded_at, route_dist_m"

_ANOMALY_COLS = "day, kind, device_id, grid_key, poi, detail, ts"


def schema_version(conn: sqlite3.Connection) -> int:
    """PRAGMA user_version：>=2 表示位置事实 v2 已激活。"""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def is_v2(conn: sqlite3.Connection) -> bool:
    return schema_version(conn) >= 2


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """表存在返回实际列名集合；表缺失返回空集（列级容错，双读层内外共用）。"""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _select_expr(
    alias: str,
    wanted: str,
    actual: set[str],
    force_null: set[str] = frozenset(),
) -> str:
    """SELECT 片段：实际存在的列取真值，缺失列补 NULL AS col（行结构稳定）。

    force_null：该 schema 版本不提供的列（如 v1 的 place_id）恒为 NULL，
    但仍出现在 SELECT 中，保证输出行的键集合与版本无关。
    """
    parts = []
    for c in wanted.split(", "):
        if c in force_null or c not in actual:
            parts.append(f"NULL AS {c}")
        else:
            parts.append(f"{alias}.{c}")
    return ", ".join(parts)


def _norm_place(row: sqlite3.Row, *, v2: bool) -> dict:
    visit_count = int(row["visit_count"] or 0)
    out = {
        "id": row["id"],
        "device_id": row["device_id"],
        "grid_key": row["grid_key"],
        "lat": row["lat"],
        "lon": row["lon"],
        "label": row["label"] or "未知",
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "visit_count": visit_count,
        "is_primary": int(row["is_primary"] or 0),
        "address": row["address"],
        "poi": row["poi"],
        "poi_fallback": row["poi_fallback"],
        "district": row["district"],
        "township": row["township"],
        "business_area": row["business_area"],
        "poi_type": row["poi_type"],
        "behavior": row["behavior"],
        "matched_level": row["matched_level"],
        "candidate_label": row["candidate_label"],
        "confidence_home": row["confidence_home"],
        "confidence_work": row["confidence_work"],
        "geocoded_at": row["geocoded_at"],
        "poi_l1": row["poi_l1"] or "",
        "poi_l2": row["poi_l2"] or "",
        "poi_l3": row["poi_l3"] or "",
    }
    if v2:
        out["place_id"] = row["place_id"]
        out["point_count"] = int(row["point_count"] or 0)
        out["stay_ms"] = int(row["stay_ms"] or 0)
        out["visit_episodes"] = visit_count  # v2: visit_count 即 stay 段数
    else:
        out["place_id"] = None
        out["point_count"] = visit_count  # v1 近似：visits 兼容映射 point_count
        out["stay_ms"] = 0
        out["visit_episodes"] = visit_count
    # 命名证据（v2 列；v1/缺列恒 NULL → 默认值，PlaceRef 经 resolve_place_name 使用）
    out["name_confidence"] = float(row["name_confidence"] or 0.0)
    out["name_evidence"] = row["name_evidence"] or ""
    out["parent_poi"] = row["parent_poi"] or ""
    return out


def read_places(
    conn: sqlite3.Connection,
    *,
    device_id: str | None = None,
    label_in: tuple[str, ...] | list[str] | None = None,
    candidate_only: bool = False,
    order_by_visit: bool = True,
    limit: int | None = None,
) -> list[dict]:
    """读取 places 统一行。label_in 过滤人工标签；candidate_only 只取待确认候选。"""
    actual = table_columns(conn, "places")
    if not actual:
        return []
    v2 = is_v2(conn)
    v2_cols = {"place_id", "point_count", "stay_ms"}
    wanted = _PLACE_COLS + _PLACE_V2_ONLY + _PLACE_NAME_V2_ONLY
    force_null = set() if v2 else v2_cols | set(_PLACE_NAME_V2_ONLY.split(", "))
    sql = f"SELECT {_select_expr('p', wanted, actual, force_null)} FROM places p WHERE 1=1"
    params: list = []
    if device_id is not None:
        sql += " AND p.device_id=?"
        params.append(device_id)
    if label_in:
        sql += f" AND p.label IN ({','.join('?' for _ in label_in)})"
        params.extend(label_in)
    if candidate_only:
        sql += " AND p.label='未知' AND p.candidate_label IS NOT NULL"
    if order_by_visit:
        sql += " ORDER BY visit_count DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_norm_place(r, v2=v2) for r in rows]


_PLACE_JOIN_COLS = (
    "label AS place_label, poi AS place_poi, poi_fallback AS place_poi_fallback, "
    "address AS place_address, behavior AS place_behavior, district AS place_district"
)


def _place_join_cols(place_cols: set[str]) -> str:
    parts = []
    for c in _PLACE_JOIN_COLS.split(", "):
        src, alias = c.split(" AS ")
        parts.append(f"p.{c}" if src in place_cols else f"NULL AS {alias}")
    return ", " + ", ".join(parts)


def _stay_place_join(v2: bool, stay_cols: set[str], place_cols: set[str]) -> str:
    """stay ↔ place JOIN 片段；关联键缺失或 places 表缺失时返回 ""（无 JOIN）。"""
    if not place_cols:
        return ""
    if v2 and "place_id" in stay_cols and "place_id" in place_cols:
        on = "s.place_id=p.place_id"
    elif "grid_key" in stay_cols and "grid_key" in place_cols:
        on = "s.grid_key=p.grid_key"
    else:
        return ""
    return _place_join_cols(place_cols) + f" FROM stays s LEFT JOIN places p ON {on} AND s.device_id=p.device_id"


def read_stays(
    conn: sqlite3.Connection,
    *,
    device_id: str | None = None,
    overlap: tuple[int, int] | None = None,
    day_from: str | None = None,
    day: str | None = None,
    with_place: bool = True,
) -> list[dict]:
    """读取 stays；with_place 时内嵌关联 place 字段（v1: grid_key JOIN；v2: place_id JOIN）。

    overlap=(start_ms, end_ms) 取时间窗相交的段（与 fact_card 语义一致：
    start_ts < win_end AND end_ts > win_start）。
    """
    stay_cols = table_columns(conn, "stays")
    if not stay_cols:
        return []
    v2 = is_v2(conn)
    place_cols = table_columns(conn, "places") if with_place else set()
    join_sql = _stay_place_join(v2, stay_cols, place_cols) if with_place else ""
    select = _select_expr("s", _STAY_COLS, stay_cols)
    if v2 and "place_id" in stay_cols:
        select += ", s.place_id"
    else:
        select += ", NULL AS place_id"
    select += join_sql if join_sql else " FROM stays s"
    sql = f"SELECT {select} WHERE 1=1"
    params: list = []
    if device_id is not None:
        sql += " AND s.device_id=?"
        params.append(device_id)
    if overlap is not None:
        sql += " AND s.start_ts < ? AND s.end_ts > ?"
        params.extend((overlap[1], overlap[0]))
    if day is not None:
        sql += " AND s.day=?"
        params.append(day)
    if day_from is not None:
        sql += " AND s.day >= ?"
        params.append(day_from)
    sql += " ORDER BY start_ts"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        row = {
            "device_id": r["device_id"],
            "start_ts": int(r["start_ts"] or 0),
            "end_ts": int(r["end_ts"] or 0),
            "duration_ms": int(r["duration_ms"] or 0),
            "center_lat": r["center_lat"],
            "center_lon": r["center_lon"],
            "min_lat": r["min_lat"],
            "min_lon": r["min_lon"],
            "max_lat": r["max_lat"],
            "max_lon": r["max_lon"],
            "n_points": int(r["n_points"] or 0),
            "radius_m": r["radius_m"],
            "grid_key": r["grid_key"],
            "day": r["day"],
            "place_id": r["place_id"],
            # stay 中心质量（stays_v2.avg_accuracy_m；v1 恒 None）
            "avg_accuracy_m": r["avg_accuracy_m"],
        }
        if join_sql:
            row["place_label"] = r["place_label"]
            row["place_poi"] = r["place_poi"]
            row["place_poi_fallback"] = r["place_poi_fallback"]
            row["place_address"] = r["place_address"]
            row["place_behavior"] = r["place_behavior"]
            row["place_district"] = r["place_district"]
        else:
            row["place_label"] = None
            row["place_poi"] = None
            row["place_poi_fallback"] = None
            row["place_address"] = None
            row["place_behavior"] = None
            row["place_district"] = None
        out.append(row)
    return out


def read_trips(
    conn: sqlite3.Connection,
    *,
    device_id: str | None = None,
    overlap: tuple[int, int] | None = None,
    day_from: str | None = None,
    day: str | None = None,
) -> list[dict]:
    """读取 trips 统一行；from/to_place_id 在 v1 恒为 None。"""
    actual = table_columns(conn, "trips")
    if not actual:
        return []
    v2 = is_v2(conn)
    v2_cols = {"from_place_id", "to_place_id", "polyline", "route_key", "route_mode", "route_encoded_at", "route_dist_m"}
    wanted = _TRIP_COLS + _TRIP_V2_ONLY
    force_null = set() if v2 else v2_cols
    sql = f"SELECT {_select_expr('t', wanted, actual, force_null)} FROM trips t WHERE 1=1"
    params: list = []
    if device_id is not None:
        sql += " AND t.device_id=?"
        params.append(device_id)
    if overlap is not None:
        sql += " AND t.start_ts < ? AND t.end_ts > ?"
        params.extend((overlap[1], overlap[0]))
    if day is not None:
        sql += " AND t.day=?"
        params.append(day)
    if day_from is not None:
        sql += " AND t.day >= ?"
        params.append(day_from)
    sql += " ORDER BY start_ts"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "device_id": r["device_id"],
            "start_ts": int(r["start_ts"] or 0),
            "end_ts": int(r["end_ts"] or 0),
            "duration_ms": int(r["duration_ms"] or 0),
            "start_lat": r["start_lat"],
            "start_lon": r["start_lon"],
            "end_lat": r["end_lat"],
            "end_lon": r["end_lon"],
            "dist_m": int(r["dist_m"] or 0),
            "n_points": int(r["n_points"] or 0),
            "day": r["day"],
            "from_place_id": r["from_place_id"],
            "to_place_id": r["to_place_id"],
            "polyline": r["polyline"],
            "route_key": r["route_key"],
            "route_mode": r["route_mode"],
            "route_encoded_at": r["route_encoded_at"],
            "route_dist_m": r["route_dist_m"],
        }
        for r in rows
    ]


def read_anomalies(
    conn: sqlite3.Connection,
    *,
    day: str | None = None,
    device_id: str | None = None,
) -> list[dict]:
    """读取 anomalies 统一行；place_id 在 v1 恒为 None。"""
    actual = table_columns(conn, "anomalies")
    if not actual:
        return []
    v2 = is_v2(conn)
    wanted = _ANOMALY_COLS + ", place_id"
    force_null = set() if v2 else {"place_id"}
    sql = f"SELECT {_select_expr('a', wanted, actual, force_null)} FROM anomalies a WHERE 1=1"
    params: list = []
    if day is not None:
        sql += " AND a.day=?"
        params.append(day)
    if device_id is not None:
        sql += " AND a.device_id=?"
        params.append(device_id)
    sql += " ORDER BY ts"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "day": r["day"],
            "kind": r["kind"],
            "device_id": r["device_id"],
            "grid_key": r["grid_key"],
            "poi": r["poi"],
            "detail": r["detail"],
            "ts": r["ts"],
            "place_id": r["place_id"],
        }
        for r in rows
    ]


def read_place_cells(
    conn: sqlite3.Connection,
    *,
    device_id: str | None = None,
    place_id: str | None = None,
) -> list[dict]:
    """读取 place_cells 成员网格（v2 独有；v1 / 表缺失返回空列表）。"""
    if not is_v2(conn):
        return []
    actual = table_columns(conn, "place_cells")
    if not actual:
        return []
    sql = "SELECT device_id, place_id, grid_key FROM place_cells c WHERE 1=1"
    params: list = []
    if device_id is not None:
        sql += " AND c.device_id=?"
        params.append(device_id)
    if place_id is not None:
        sql += " AND c.place_id=?"
        params.append(place_id)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"device_id": r["device_id"], "place_id": r["place_id"], "grid_key": r["grid_key"]}
        for r in rows
    ]


def place_grid_map(
    conn: sqlite3.Connection,
    *,
    device_id: str | None = None,
    label_in: tuple[str, ...] | list[str] | None = None,
) -> dict[str, dict]:
    """grid_key → place 行映射。

    v1：places 自身网格；v2：place_cells 成员网格全部展开（含代表网格）。
    供"按原始网格找地点"的消费者（report._outings 等）在两个版本下
    得到一致的查找语义；同网格多 place 时取 visit_count 最高者。
    """
    places = read_places(conn, device_id=device_id, label_in=label_in, order_by_visit=True)
    if not places:
        return {}
    # place_id=sha1(device_id|grids)[:16] 内嵌设备，单 place_id 作键不会跨设备串扰；
    # 若未来改变生成规则须改回 (device_id, place_id) 复合键
    by_id = {p["place_id"]: p for p in places if p["place_id"]}
    grid_map: dict[str, dict] = {}
    for p in places:
        if p["grid_key"]:
            # 列表已按 visit_count 降序，先占位者胜
            grid_map.setdefault(str(p["grid_key"]), p)
    if by_id:
        for c in read_place_cells(conn, device_id=device_id):
            p = by_id.get(c["place_id"])
            if p is not None and c["grid_key"]:
                grid_map.setdefault(str(c["grid_key"]), p)
    return grid_map


def read_daily_quality(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    day: str,
) -> dict | None:
    """读取 (day, device_id) 的坐标质量日行（Task 6 §3.2，FactCard full 透传）。

    表缺失（v1 旧库 / 未跑 Task 6 ETL）或无行返回 None；
    created_at/updated_at 审计列不透传（保证快照确定性）。
    """
    if not table_columns(conn, "daily_location_quality"):
        return None
    try:
        row = conn.execute(
            "SELECT * FROM daily_location_quality WHERE day=? AND device_id=?",
            (day, device_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    out = dict(row)
    out.pop("created_at", None)
    out.pop("updated_at", None)
    return out


def read_tag_conflict_count(conn: sqlite3.Connection, *, device_id: str) -> int:
    """读取设备的人工 tag 冲突数（v2 place_tag_conflicts；v1/缺表恒 0）。"""
    if not table_columns(conn, "place_tag_conflicts"):
        return 0
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM place_tag_conflicts WHERE device_id=?", (device_id,)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    return int(n or 0)
