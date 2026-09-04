"""langTrack 位置事实 v2：schema 冻结与事务化迁移骨架（§2.2 / §2.4）。

本模块只负责“位置事实 v2 表结构”与“数据库层面的事务切换”，不负责：
- 坐标解析 / canonical 聚类 / 新旧匹配（见 location_facts.py，纯算法层）；
- 标签文件两阶段替换（label_places.py，Task 4 落地）；
- geocode 外呼 / 路线编码（只能在 status=complete 之后运行）。

对外四个事务函数：
- :func:`create_location_v2_tables`：幂等创建全部 v2 事实表 + 迁移审计表（正式名 *v2）。
- :func:`validate_location_v2`：唯一键 / 索引 / 东八区时间列 / 孤儿 stays.place_id 校验。
- :func:`activate_location_v2`：BEGIN IMMEDIATE 事务内把 v1 表备份、shadow/v2 表转正、
  写 PRAGMA user_version=2 与 pending_label_swap 状态；任一步失败整段 ROLLBACK。
- :func:`rollback_location_v2`：BEGIN IMMEDIATE 事务内把 v2 正式表改名为
  *_failed_v2_<run_id>、恢复 *_v1_backup、写 user_version=1 与 rolled_back。

设计约束（高内聚低耦合）：
- 所有 DDL/DML 只出现在本模块；etl.py 仅通过 CLI 参数调用 build_location_shadow。
- activate/rollback 内部不允许出现文件写入或网络外呼（geocode/route 一律在外）。
- shadow 表（shadow_*_v2）由 build_location_shadow 生成，只读对比，不覆盖正式表。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from pathlib import Path

# 迁移编排默认标签文件路径（与 label_places.CONFIG_PATH 同源）。
DEFAULT_LABELS_PATH = Path(__file__).resolve().parents[3] / "data" / "place_labels.json"

# ---------------------------------------------------------------------------
# 表清单
# ---------------------------------------------------------------------------

# v1 业务表：activate 时备份为 *_v1_backup，rollback 时恢复。
V1_FACT_TABLES: tuple[str, ...] = (
    "places",
    "stays",
    "trips",
    "anomalies",
    "route_grids",
    "grid_pois",
)

# v2 事实表（§2.2）：activate 转正后的正式表名。
V2_FACT_TABLES: tuple[str, ...] = (
    "places",
    "place_cells",
    "stays",
    "trips",
    "place_tag_conflicts",
    "anomalies",
    "route_grids",
    "grid_pois",
)

# shadow 数据源表（Task 3 由 build_location_shadow 生成）。
SHADOW_SOURCE_TABLES: dict[str, str] = {
    "places": "shadow_places_v2",
    "place_cells": "shadow_place_cells_v2",
    "stays": "shadow_stays_v2",
    "trips": "shadow_trips_v2",
}

# 有旧表、activate 时直接由 v2 表转正的（无 shadow 数据源）。
V2_DIRECT_TABLES: tuple[str, ...] = (
    "place_tag_conflicts",
    "anomalies",
    "route_grids",
    "grid_pois",
)

# 迁移审计表（§2.2）。
AUDIT_TABLES: tuple[str, ...] = (
    "location_migration_state",
    "location_place_mapping",
    "location_migration_issues",
    "location_migration_metrics",
)

MIGRATION_STATE_STATUSES: tuple[str, ...] = (
    "prepared",        # shadow 构建完成、未切换
    "pending_label_swap",  # DB 已提交、标签文件待原子替换
    "complete",        # 标签文件已替换、ETL 可运行
    "rolled_back",     # 已回滚到 v1
)


class LocationMigrationError(RuntimeError):
    """位置迁移流程错误（校验失败 / 事务异常），不会静默吞掉。"""


def new_run_id() -> str:
    """东八区时间戳 run_id（审计行与 failed 表快照名共用）。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return now.strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# schema v2（§2.2 原文，含东八区 created_at/updated_at 默认值）
# ---------------------------------------------------------------------------

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS places_v2 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  place_id TEXT NOT NULL,
  grid_key TEXT NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  label TEXT NOT NULL DEFAULT '未知',
  first_seen INTEGER,
  last_seen INTEGER,
  point_count INTEGER NOT NULL DEFAULT 0,
  visit_count INTEGER NOT NULL DEFAULT 0,
  stay_ms INTEGER NOT NULL DEFAULT 0,
  is_primary INTEGER NOT NULL DEFAULT 0,
  source_coord_system TEXT NOT NULL DEFAULT 'unknown',
  center_method TEXT NOT NULL DEFAULT 'stay_median',
  address TEXT, poi TEXT, district TEXT, township TEXT,
  business_area TEXT, poi_type TEXT,
  poi_l1 TEXT, poi_l2 TEXT, poi_l3 TEXT,
  poi_signal TEXT, poi_fallback TEXT,
  matched_level TEXT, behavior TEXT, geocoded_at INTEGER,
  candidate_label TEXT,
  confidence_home REAL NOT NULL DEFAULT 0,
  confidence_work REAL NOT NULL DEFAULT 0,
  aoi TEXT, parent_poi TEXT, poi_distance_m REAL,
  display_granularity TEXT NOT NULL DEFAULT 'neighborhood',
  name_confidence REAL NOT NULL DEFAULT 0,
  name_evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  UNIQUE(device_id, place_id),
  UNIQUE(device_id, grid_key)
);
CREATE TABLE IF NOT EXISTS place_cells_v2 (
  device_id TEXT NOT NULL,
  place_id TEXT NOT NULL,
  grid_key TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  PRIMARY KEY(device_id, grid_key),
  UNIQUE(device_id, place_id, grid_key)
);
CREATE TABLE IF NOT EXISTS stays_v2 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL,
  center_lat REAL NOT NULL,
  center_lon REAL NOT NULL,
  min_lat REAL NOT NULL,
  min_lon REAL NOT NULL,
  max_lat REAL NOT NULL,
  max_lon REAL NOT NULL,
  n_points INTEGER NOT NULL DEFAULT 0,
  accuracy_known_points INTEGER NOT NULL DEFAULT 0,
  avg_accuracy_m REAL,
  radius_m REAL NOT NULL DEFAULT 0,
  grid_key TEXT,
  place_id TEXT,
  source_coord_system TEXT NOT NULL DEFAULT 'unknown',
  day TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_stays_v2_device_time
ON stays_v2(device_id, start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_stays_v2_device_place
ON stays_v2(device_id, place_id);
CREATE TABLE IF NOT EXISTS trips_v2 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL,
  start_lat REAL NOT NULL,
  start_lon REAL NOT NULL,
  end_lat REAL NOT NULL,
  end_lon REAL NOT NULL,
  from_place_id TEXT,
  to_place_id TEXT,
  endpoint_coord_system TEXT NOT NULL DEFAULT 'unknown',
  dist_m REAL NOT NULL,
  n_points INTEGER NOT NULL DEFAULT 0,
  day TEXT,
  polyline TEXT,
  polyline_coord_system TEXT,
  route_key TEXT,
  route_mode TEXT,
  route_encoded_at INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  UNIQUE(device_id, start_ts, end_ts)
);
CREATE INDEX IF NOT EXISTS idx_trips_v2_device_time
ON trips_v2(device_id, start_ts, end_ts);
CREATE TABLE IF NOT EXISTS place_tag_conflicts_v2 (
  device_id TEXT NOT NULL,
  new_place_id TEXT NOT NULL,
  old_place_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  reason TEXT NOT NULL,
  resolved_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  PRIMARY KEY(device_id, new_place_id, old_place_id, tag)
);
CREATE TABLE IF NOT EXISTS anomalies_v2 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  device_id TEXT NOT NULL,
  place_id TEXT,
  grid_key TEXT,
  poi TEXT,
  detail TEXT,
  ts INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_anomalies_v2_unique
ON anomalies_v2(
  day, kind, device_id,
  COALESCE(place_id,''),
  COALESCE(grid_key,'')
);
CREATE TABLE IF NOT EXISTS route_grids_v2 (
  device_id TEXT NOT NULL,
  day TEXT NOT NULL,
  grid_lat REAL NOT NULL,
  grid_lon REAL NOT NULL,
  n_pass INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  PRIMARY KEY(device_id, day, grid_lat, grid_lon)
);
CREATE TABLE IF NOT EXISTS grid_pois_v2 (
  grid_lat REAL NOT NULL,
  grid_lon REAL NOT NULL,
  name TEXT,
  type TEXT,
  distance TEXT,
  queried_at INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  PRIMARY KEY(grid_lat, grid_lon)
);
CREATE TABLE IF NOT EXISTS location_migration_state (
  id INTEGER PRIMARY KEY CHECK(id=1),
  run_id TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  status TEXT NOT NULL,
  pending_labels_path TEXT,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours'))
);
CREATE TABLE IF NOT EXISTS location_place_mapping (
  run_id TEXT NOT NULL,
  old_device_id TEXT NOT NULL,
  old_grid_key TEXT NOT NULL,
  old_place_id TEXT NOT NULL,
  new_place_id TEXT,
  match_reason TEXT NOT NULL,
  jaccard REAL,
  distance_m REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  PRIMARY KEY(run_id, old_device_id, old_place_id)
);
CREATE TABLE IF NOT EXISTS location_migration_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  source_payload TEXT NOT NULL,
  device_id TEXT,
  grid_key TEXT,
  tag TEXT,
  resolution_status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours'))
);
CREATE TABLE IF NOT EXISTS location_migration_metrics (
  run_id TEXT NOT NULL,
  metric TEXT NOT NULL,
  value INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  PRIMARY KEY(run_id, metric)
);
"""

# 每张 v2 表期望的唯一约束 / 主键（用于 validate 校验，§2.2 逐表核对）。
EXPECTED_UNIQUES: dict[str, tuple[str, ...]] = {
    "places_v2": ("device_id, place_id", "device_id, grid_key"),
    "place_cells_v2": ("device_id, grid_key", "device_id, place_id, grid_key"),
    "stays_v2": (),
    "trips_v2": ("device_id, start_ts, end_ts",),
    "place_tag_conflicts_v2": ("device_id, new_place_id, old_place_id, tag",),
    "anomalies_v2": (),
    "route_grids_v2": ("device_id, day, grid_lat, grid_lon",),
    "grid_pois_v2": ("grid_lat, grid_lon",),
}

# 每张 v2 表期望的显式索引（validate 校验存在性）。
EXPECTED_INDEXES: dict[str, tuple[str, ...]] = {
    "stays_v2": ("idx_stays_v2_device_time", "idx_stays_v2_device_place"),
    "trips_v2": ("idx_trips_v2_device_time",),
    "anomalies_v2": ("idx_anomalies_v2_unique",),
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return (row[0] if row else "") or ""


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    # PRAGMA 不接受参数占位符；table 来自固定常量集（模块内生成），无注入面。
    return {r[1] for r in conn.execute(f"PRAGMA index_list('{table}')")}


# ---------------------------------------------------------------------------
# 创建 / 校验
# ---------------------------------------------------------------------------

def create_location_v2_tables(conn: sqlite3.Connection) -> None:
    """幂等创建全部 v2 事实表 + 迁移审计表（§2.2）。不触碰任何 v1 表。"""
    conn.executescript(SCHEMA_V2)


def validate_location_v2(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    """校验 v2 层是否满足切换条件（§2.4 步骤 9）。

    返回 (ok, errors)：
    - 全部 v2 事实表 + 审计表存在；
    - 每张 v2 表含东八区 created_at / updated_at 默认值；
    - 唯一约束 / 显式索引与 §2.2 一致；
    - 孤儿校验：stays_v2.place_id 非空且必须存在于 places_v2.place_id，
      否则（有数据时）阻止切换。
    """
    errors: list[str] = []
    names = _table_names(conn)

    all_v2 = [f"{t}_v2" for t in V2_FACT_TABLES] + list(AUDIT_TABLES)
    for t in all_v2:
        if t not in names:
            errors.append(f"missing table: {t}")

    # 时间列 + 唯一约束 + 索引（仅对已存在的表做细化校验，避免噪音堆叠）
    for t in all_v2:
        if t not in names:
            continue
        sql = _table_sql(conn, t)
        if "created_at" not in sql or "updated_at" not in sql:
            errors.append(f"{t}: missing created_at/updated_at")
        elif "+8 hours" not in sql:
            errors.append(f"{t}: created_at/updated_at must default to CST (+8 hours)")
        expected = EXPECTED_UNIQUES.get(t)
        if expected:
            for uni in expected:
                if uni not in sql:
                    errors.append(f"{t}: missing unique/pk ({uni})")

    for t, idxs in EXPECTED_INDEXES.items():
        if t not in names:
            continue
        actual = _index_names(conn, t)
        for idx in idxs:
            if idx not in actual:
                errors.append(f"{t}: missing index {idx}")

    # 孤儿 stays.place_id 校验：有数据时不允许悬空引用（阻止切换）。
    if "stays_v2" in names and "places_v2" in names:
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM stays_v2 s WHERE s.place_id IS NOT NULL "
                "AND s.place_id NOT IN (SELECT place_id FROM places_v2)"
            ).fetchone()
            if row and row[0] > 0:
                errors.append(f"orphan stays_v2.place_id: {row[0]} rows reference missing places_v2")
        except sqlite3.OperationalError as e:
            errors.append(f"stays_v2 orphan check failed: {e}")

    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# 迁移状态辅助（location_migration_state 单行）
# ---------------------------------------------------------------------------

def _write_state(
    conn: sqlite3.Connection,
    run_id: str,
    schema_version: int,
    status: str,
    pending_labels_path: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO location_migration_state(id, run_id, schema_version, status, "
        "pending_labels_path, error, updated_at) "
        "VALUES (1,?,?,?,?,?,datetime('now','+8 hours')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "run_id=excluded.run_id, schema_version=excluded.schema_version, "
        "status=excluded.status, pending_labels_path=excluded.pending_labels_path, "
        "error=excluded.error, updated_at=datetime('now','+8 hours')",
        (run_id, schema_version, status, pending_labels_path, error),
    )


def read_migration_state(conn: sqlite3.Connection) -> dict | None:
    """读取迁移状态；不存在返回 None。"""
    row = conn.execute(
        "SELECT run_id, schema_version, status, pending_labels_path, error "
        "FROM location_migration_state WHERE id=1"
    ).fetchone()
    if not row:
        return None
    return {
        "run_id": row[0],
        "schema_version": row[1],
        "status": row[2],
        "pending_labels_path": row[3],
        "error": row[4],
    }


def _rename_table(conn: sqlite3.Connection, old: str, new: str) -> None:
    # 标识符统一加双引号（run_id 可能含连字符，如 *-failed_v2_<run_id>）。
    conn.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')


def _missing_tables(conn: sqlite3.Connection, tables: list[str]) -> list[str]:
    """返回列表中实际不存在的表名（缺失集），供调用方报错。"""
    names = _table_names(conn)
    return [t for t in tables if t not in names]


# ---------------------------------------------------------------------------
# 切换（activate）与回滚（rollback）—— 必须在单事务内，禁止 IO 外呼
# ---------------------------------------------------------------------------

def activate_location_v2(
    conn: sqlite3.Connection,
    run_id: str,
    pending_labels_path: str | None = None,
) -> None:
    """执行位置事实 v2 切换（§2.4 步骤 7-12 的 DB 部分）。

    前置：validate_location_v2 必须通过；shadow/v2 表数据已就绪。
    流程（单个 BEGIN IMMEDIATE 事务，任一步失败整段 ROLLBACK）：
      1. 校验未通过 → 抛 LocationMigrationError，不开启事务；
      2. 旧表 places/stays/trips/anomalies/route_grids/grid_pois → *_v1_backup；
      3. shadow 数据源表（shadow_*_v2）→ 正式表名；无 shadow 时用 v2 表转正；
      4. place_tag_conflicts/anomalies/route_grids/grid_pois 的 v2 表 → 正式表名；
      5. 写 PRAGMA user_version=2 与 status=pending_label_swap + pending 路径；
      6. COMMIT。
    本函数内不进行任何文件写入或网络外呼（geocode/route 一律在事务外）。
    """
    ok, errors = validate_location_v2(conn)
    if not ok:
        raise LocationMigrationError("validate_location_v2 failed: " + "; ".join(errors))

    names = _table_names(conn)
    missing = _missing_tables(conn, [f"{t}_v2" for t in V2_FACT_TABLES] + list(AUDIT_TABLES))
    if missing:
        raise LocationMigrationError("missing v2 tables: " + ", ".join(missing))

    # Task 4：未解决的阻断性迁移 issue（如 v1 标签多设备歧义）禁止切换
    if "location_migration_issues" in names:
        placeholders = ",".join("?" for _ in BLOCKING_ISSUE_KINDS)
        n_blocking = conn.execute(
            f"SELECT COUNT(*) FROM location_migration_issues "
            f"WHERE kind IN ({placeholders}) AND resolution_status='open'",
            BLOCKING_ISSUE_KINDS,
        ).fetchone()[0]
        if n_blocking:
            raise LocationMigrationError(
                f"blocking label migration issues: {n_blocking} open "
                f"({','.join(BLOCKING_ISSUE_KINDS)}) — resolve before activate"
            )

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1) 备份旧表
        for t in V1_FACT_TABLES:
            if t not in names:
                raise LocationMigrationError(f"v1 table not found: {t}")
            _rename_table(conn, t, f"{t}_v1_backup")

        # 2) shadow 数据源转正（优先），否则用 v2 表转正
        for formal in ("places", "place_cells", "stays", "trips"):
            shadow = SHADOW_SOURCE_TABLES[formal]
            source = shadow if shadow in names else f"{formal}_v2"
            if source not in _table_names(conn):
                raise LocationMigrationError(f"source table not found: {source}")
            _rename_table(conn, source, formal)

        # 3) 无 shadow 的 v2 表直接转正
        for formal in V2_DIRECT_TABLES:
            _rename_table(conn, f"{formal}_v2", formal)

        # 4) 写版本与状态
        conn.execute("PRAGMA user_version = 2")
        _write_state(conn, run_id, 2, "pending_label_swap", pending_labels_path)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def rollback_location_v2(conn: sqlite3.Connection, run_id: str) -> None:
    """回滚到 v1（§2.4 rollback 清单）。

    单个 BEGIN IMMEDIATE 事务：
      1. 校验所有 *_v1_backup 存在，否则抛错（禁止半回滚）；
      2. 当前 v2 正式表 → *_failed_v2_<run_id>（六张业务表）；
      3. *_v1_backup → 正式表名；
      4. 写 PRAGMA user_version=1 与 status=rolled_back；COMMIT。
    恢复的表保留全部索引与 polyline/route_key 缓存（rename 不丢数据）。
    """
    names = _table_names(conn)
    missing = [f"{t}_v1_backup" for t in V1_FACT_TABLES if f"{t}_v1_backup" not in names]
    if missing:
        raise LocationMigrationError("missing v1 backups: " + ", ".join(missing))

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 当前正式表改名 failed 快照（仅当确实处于 v2 状态）
        for t in V1_FACT_TABLES:
            if t in _table_names(conn):
                _rename_table(conn, t, f"{t}_failed_v2_{run_id}")
        # 恢复 v1
        for t in V1_FACT_TABLES:
            _rename_table(conn, f"{t}_v1_backup", t)

        conn.execute("PRAGMA user_version = 1")
        _write_state(conn, run_id, 1, "rolled_back", error=None)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# shadow 构建（Task 3：全量位置事实 v2 重建，只写 shadow_* 表）
# ---------------------------------------------------------------------------

# 从 SCHEMA_V2 派生四张 shadow 表 DDL（表名/索引名前缀 shadow_），
# 避免 schema 双份维护漂移；只挑 places/stays/trips/place_cells 相关语句。
def _shadow_ddl() -> str:
    wanted = ("place_cells_v2", "places_v2", "stays_v2", "trips_v2")
    stmts = [s.strip() for s in SCHEMA_V2.split(";") if s.strip()]
    picked = [s for s in stmts if any(w in s for w in wanted)]
    for old, new in (
        ("place_cells_v2", "shadow_place_cells_v2"),
        ("places_v2", "shadow_places_v2"),
        ("stays_v2", "shadow_stays_v2"),
        ("trips_v2", "shadow_trips_v2"),
    ):
        picked = [s.replace(old, new) for s in picked]
    return ";\n".join(picked) + ";"


def _ensure_shadow_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_shadow_ddl())


def _accuracy_stats(
    points: list, start_ts: int, end_ts: int
) -> tuple[int, float | None]:
    """窗口 [start_ts, end_ts] 内点的 accuracy 统计（points 已按 ts 排序）。

    返回 (accuracy_known_points, avg_accuracy_m)；无已知精度时 avg 为 None。
    """
    import bisect

    ts_list = [p.ts for p in points]
    lo = bisect.bisect_left(ts_list, start_ts)
    hi = bisect.bisect_right(ts_list, end_ts)
    known = [p.accuracy_m for p in points[lo:hi] if p.accuracy_m is not None]
    if not known:
        return 0, None
    return len(known), round(sum(known) / len(known), 1)


def _top2_primary(places: list[dict]) -> set[str]:
    """每设备按 stay_ms 取 top2 作为 is_primary 候选（稳定地点的初步候选，非人工事实）。"""
    by_device: dict[str, list[dict]] = {}
    for p in places:
        by_device.setdefault(p["device_id"], []).append(p)
    primary: set[str] = set()
    for device_id, plist in by_device.items():
        plist.sort(key=lambda x: (-x["stay_ms"], x["place_id"]))
        for p in plist[:2]:
            primary.add((device_id, p["place_id"]))
    return primary


def build_location_shadow(
    db_path: Path | str,
    *,
    incremental: bool = False,
    coord_config: dict | None = None,
) -> int:
    """构建只读对比表 shadow_places_v2/shadow_place_cells_v2/shadow_stays_v2/shadow_trips_v2。

    全量从 events 重建（首版 location v2 只允许全量；incremental 参数仅用于记录
    “location v2 full rebuild”），不修改任何正式表与标签文件：

    1. events → etl.build_stays（v1 停驻检测算法单一来源，stays 参数接 etl_config）；
    2. stays → location_facts.canonical_places（确定性聚类 + 稳定 place_id）；
    3. place_cells 成员网格 + point_count（成员网格内原始 location 点数）+
       visit_count（stay 段数）+ stay_ms（stay 总时长），禁止累加旧 visit_count；
    4. stays.place_id 回填所属 canonical place；trips 重建并写 from/to_place_id
       与 endpoint_coord_system；旧 trips 的 polyline/route_key 按
       (device_id,start_ts,end_ts) 精确匹配迁移，无匹配不迁移；
    5. 连续运行两次内容一致（幂等，DELETE + INSERT 重建）。

    返回 shadow_stays_v2 行数。
    """
    from collections import Counter

    from gacore.langTrack import etl, routes
    from gacore.langTrack import location_facts as lf
    from gacore.langTrack.etl_config import (
        CoordSystemConfigError,
        load_coord_systems,
        load_etl_config,
        resolve_coord_system,
    )

    db_path = Path(db_path)
    conn = sqlite3.connect(db_path)
    try:
        if incremental:
            print("[etl] location v2 full rebuild (incremental ignored)")

        _ensure_shadow_tables(conn)
        create_location_v2_tables(conn)

        events = etl.load_events(conn)
        cfg = load_etl_config()
        stays_cfg = cfg.get("stays", {})
        trips_cfg = cfg.get("trips", {})
        if coord_config is None:
            try:
                coord_config = load_coord_systems()
            except CoordSystemConfigError as e:
                raise LocationMigrationError(f"coord system config error: {e}") from e

        # ---- 1) 规范化坐标点（Task 6 共用解析：location_points 单一入口）----
        all_points = lf.location_points(events, coord_config)
        points_by_device: dict[str, list] = {}
        for pt in all_points:
            points_by_device.setdefault(pt.device_id, []).append(pt)

        # §3.1 精度过滤只作用于几何构建输入（与 v1 _build_location_v1 的
        # geom_points 同口径）；point_count/stay accuracy 统计保持原始点
        # （§2.3：point_count 为成员网格内原始 location 点数）
        geom_points = lf.accuracy_filter(all_points, cfg.get("location", {}))

        # ---- 2) 停驻段（v1 算法单一来源；跨午夜 stay 不按 day 截断）----
        stays = etl.build_stays(
            geom_points,
            large_radius_m=float(stays_cfg.get("large_radius_m", 120.0)),
            small_radius_m=float(stays_cfg.get("small_radius_m", 60.0)),
            min_stay_ms=int(stays_cfg.get("min_duration_ms", 600000)),
            merge_gap_ms=int(stays_cfg.get("merge_gap_ms", 300000)),
            merge_radius_m=float(stays_cfg.get("merge_radius_m", 150.0)),
            max_jump_m=float(stays_cfg.get("max_jump_m", 500.0)),
            max_speed_mps=float(stays_cfg.get("max_speed_mps", 40.0)),
        )

        # ---- 2.5) 坐标质量日表（§3.2 只读聚合；v1/v2 共表，全量重建。
        #      不触碰 places/stays/trips 等位置事实表，与 shadow 只读承诺不冲突）----
        etl._write_daily_quality(
            conn, lf.daily_quality_rows(events, coord_config), False, set()
        )

        # ---- 3) canonical places（stay 聚类；网格键统一 lf.grid_key_of 词汇）----
        stay_inputs = [
            lf.StayInput(
                device_id=s[0], start_ts=s[1], end_ts=s[2], duration_ms=s[3],
                center_lat=s[4], center_lon=s[5], grid_key=lf.grid_key_of(s[4], s[5]),
            )
            for s in stays
        ]
        drafts = lf.canonical_places(stay_inputs)

        # seed (device, grid) → place_id；point_count 按成员网格分桶统计
        place_by_seed: dict[tuple[str, str], str] = {}
        for d in drafts:
            for gk in d["member_grid_keys"]:
                place_by_seed[(d["device_id"], gk)] = d["place_id"]
        point_count_by_seed: Counter = Counter()
        for device_id, plist in points_by_device.items():
            for p in plist:
                point_count_by_seed[(device_id, lf.grid_key_of(p.lat, p.lon))] += 1

        primary_keys = _top2_primary(drafts)

        place_rows = []
        cell_rows = []
        for d in drafts:
            device_id = d["device_id"]
            point_count = sum(
                point_count_by_seed.get((device_id, gk), 0) for gk in d["member_grid_keys"]
            )
            place_rows.append(
                (
                    device_id, d["place_id"], d["grid_key"], d["lat"], d["lon"],
                    "未知", d["first_seen"], d["last_seen"],
                    point_count, d["visit_count"], d["stay_ms"],
                    1 if (device_id, d["place_id"]) in primary_keys else 0,
                    resolve_coord_system(device_id, d["first_seen"], coord_config),
                    "stay_duration_weighted",
                )
            )
            for gk in d["member_grid_keys"]:
                cell_rows.append((device_id, d["place_id"], gk))

        # ---- 4) shadow stays（place_id 回填 + accuracy 统计）----
        stay_rows = []
        for s in stays:
            device_id = s[0]
            pts = points_by_device.get(device_id, [])
            acc_known, avg_acc = _accuracy_stats(pts, s[1], s[2])
            stay_rows.append(
                (
                    device_id, s[1], s[2], s[3], s[4], s[5], s[6], s[7], s[8], s[9],
                    s[10], acc_known, avg_acc, s[11],
                    lf.grid_key_of(s[4], s[5]),
                    place_by_seed.get((device_id, lf.grid_key_of(s[4], s[5]))),
                    resolve_coord_system(device_id, s[1], coord_config),
                    s[13],
                )
            )

        # ---- 5) trips（相邻 stay 间隙；from/to place + 坐标制 + 旧缓存迁移）----
        trips = routes.build_trips(
            geom_points, stays,
            min_duration_ms=int(trips_cfg.get("min_duration_ms", 60000)),
            min_dist_m=float(trips_cfg.get("min_dist_m", 300.0)),
            max_infer_gap_ms=int(trips_cfg.get("max_infer_gap_ms", 7200000)),
        )

        # 旧 trips 缓存（polyline 为高德 GCJ02）
        old_route: dict[tuple, tuple] = {}
        if "trips" in _table_names(conn):
            for r in conn.execute(
                "SELECT device_id, start_ts, end_ts, polyline, route_key, route_mode, "
                "route_encoded_at FROM trips"
            ):
                old_route[(r[0], r[1], r[2])] = (r[3], r[4], r[5], r[6])

        # from/to place：trip 起点前最近 stay、终点后最近 stay（build_trips 由相邻对生成，边界可复现）
        stays_by_dev: dict[str, list] = {}
        for s in stays:
            stays_by_dev.setdefault(s[0], []).append(s)
        for lst in stays_by_dev.values():
            lst.sort(key=lambda x: x[1])

        def _place_of(stay) -> str | None:
            return place_by_seed.get((stay[0], lf.grid_key_of(stay[4], stay[5])))

        trip_rows = []
        for t in trips:
            device_id, start_ts, end_ts = t[0], t[1], t[2]
            lst = stays_by_dev.get(device_id, [])
            from_place = to_place = None
            for stay in lst:
                if stay[2] <= start_ts:
                    from_place = _place_of(stay)
                if stay[1] >= end_ts and to_place is None:
                    to_place = _place_of(stay)
            poly, rk, mode, enc = old_route.get((device_id, start_ts, end_ts), (None, None, None, None))
            trip_rows.append(
                (
                    device_id, start_ts, end_ts, t[3], t[4], t[5], t[6], t[7],
                    from_place, to_place,
                    resolve_coord_system(device_id, start_ts, coord_config),
                    t[8], t[9], t[10],
                    poly, "gcj02" if poly else None, rk, mode, enc,
                )
            )

        # ---- 6) 幂等落表（DELETE + INSERT，两次运行内容一致）----
        conn.execute("DELETE FROM shadow_places_v2")
        conn.execute("DELETE FROM shadow_place_cells_v2")
        conn.execute("DELETE FROM shadow_stays_v2")
        conn.execute("DELETE FROM shadow_trips_v2")
        # 重置 AUTOINCREMENT 序列，保证连 id 在内两次运行逐行一致
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone():
            conn.execute("DELETE FROM sqlite_sequence WHERE name LIKE 'shadow_%'")
        conn.executemany(
            "INSERT INTO shadow_places_v2(device_id, place_id, grid_key, lat, lon, label, "
            "first_seen, last_seen, point_count, visit_count, stay_ms, is_primary, "
            "source_coord_system, center_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            place_rows,
        )
        conn.executemany(
            "INSERT INTO shadow_place_cells_v2(device_id, place_id, grid_key) VALUES (?,?,?)",
            cell_rows,
        )
        conn.executemany(
            "INSERT INTO shadow_stays_v2(device_id, start_ts, end_ts, duration_ms, center_lat, "
            "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, accuracy_known_points, "
            "avg_accuracy_m, radius_m, grid_key, place_id, source_coord_system, day) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            stay_rows,
        )
        conn.executemany(
            "INSERT INTO shadow_trips_v2(device_id, start_ts, end_ts, duration_ms, start_lat, "
            "start_lon, end_lat, end_lon, from_place_id, to_place_id, endpoint_coord_system, "
            "dist_m, n_points, day, polyline, polyline_coord_system, route_key, route_mode, "
            "route_encoded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            trip_rows,
        )

        # 运行血缘（etl_runs 存在时记录 location_v2_full；mode 即"首版只允许全量"的声明）
        names = _table_names(conn)
        if "etl_runs" in names:
            conn.execute(
                "INSERT INTO etl_runs(version, mode, status, started_at, finished_at, rows_stays) "
                "VALUES (?, 'location_v2_full', 'done', datetime('now','+8 hours'), "
                "datetime('now','+8 hours'), ?)",
                ("2.0.0-shadow", len(stay_rows)),
            )
        conn.commit()
        print(
            f"[etl] location shadow: places={len(place_rows)} cells={len(cell_rows)} "
            f"stays={len(stay_rows)} trips={len(trip_rows)}"
        )
        return len(stay_rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task 5b：激活后全量重建（正式 v2 表）—— etl.run v2 分支的唯一位置事实入口
# ---------------------------------------------------------------------------

# geocode 缓存派生列（§2.5；rebuild 失效与 Task 4 迁移决策共用）。
GEOCODE_FIELDS: tuple[str, ...] = (
    "address", "poi", "district", "township", "business_area", "poi_type",
    "poi_l1", "poi_l2", "poi_l3", "poi_signal", "poi_fallback",
    "matched_level", "behavior", "geocoded_at",
)

# rebuild UPSERT 的 places 统计列（label/geocode 缓存列不在此列，保留正式表现值）。
REBUILD_PLACE_STATS: tuple[str, ...] = (
    "grid_key", "lat", "lon", "first_seen", "last_seen",
    "point_count", "visit_count", "stay_ms", "is_primary",
    "source_coord_system", "center_method",
)


def _load_formal_places(conn: sqlite3.Connection) -> list:
    """v2 正式 places + place_cells → OldPlace 列表（rebuild 的 place_id 沿用基准）。

    成员网格取 place_cells（无 cell 时退化为代表网格，与 match_old_new 兜底一致）。
    """
    from gacore.langTrack.location_facts import OldPlace

    members: dict[tuple[str, str], list[str]] = {}
    for r in conn.execute("SELECT device_id, place_id, grid_key FROM place_cells"):
        members.setdefault((r[0], r[1]), []).append(r[2])

    cols = ("device_id", "place_id", "grid_key", "lat", "lon", "label", *GEOCODE_FIELDS)
    out = []
    for r in conn.execute(f"SELECT {','.join(cols)} FROM places"):
        row = dict(zip(cols, r))
        out.append(
            OldPlace(
                device_id=row["device_id"],
                place_id=row["place_id"],
                grid_key=row["grid_key"],
                lat=row["lat"],
                lon=row["lon"],
                label=row["label"] or "未知",
                poi=row["poi"],
                address=row["address"],
                matched_level=row["matched_level"],
                grid_keys=tuple(
                    members.get((row["device_id"], row["place_id"]), [row["grid_key"]])
                ),
                geocode={k: row[k] for k in GEOCODE_FIELDS},
            )
        )
    return out


def rebuild_location_v2(
    db_path: Path | str,
    *,
    incremental: bool = False,
    coord_config: dict | None = None,
    regeo_shift_m: float = 50.0,
) -> int:
    """v2 激活后的位置事实全量重建（正式表，§2.4 ETL v2 分支）。

    复用 :func:`build_location_shadow` 作为唯一计算来源（先全量重建 shadow_* 表，
    路线缓存已在该步从正式 trips 读回），再单事务合并进正式 v2 表：

    - places：按 (device_id, place_id) UPSERT 统计列；label / geocode 缓存列保留
      正式表现值；中心偏移 > regeo_shift_m 的保留地点清空 geocode 缓存（§2.5 失效
      待重编）；shadow 中不存在的地点删除（无 stay 支持 → 事实消失）；
    - place_cells / stays / trips：DELETE + INSERT 全量重建（重置自增序列保证
      幂等逐行一致）；trips 的 polyline/route_key 缓存经 shadow 带回，不烧配额。

    幂等：相同 events 连续跑两次，四张表内容（含 id，除 created_at/updated_at
    审计列）完全一致。返回写入正式 stays 的行数。
    """
    from gacore.langTrack.location_facts import haversine_m, resolve_place_ids

    db_path = Path(db_path)
    pre = sqlite3.connect(db_path)
    try:
        if pre.execute("PRAGMA user_version").fetchone()[0] < 2:
            raise LocationMigrationError(
                "rebuild_location_v2 requires user_version >= 2 (activate first)"
            )
    finally:
        pre.close()

    build_location_shadow(db_path, incremental=incremental, coord_config=coord_config)

    conn = sqlite3.connect(db_path)
    try:
        names = _table_names(conn)
        needed = ("places", "place_cells", "stays", "trips",
                  "shadow_places_v2", "shadow_place_cells_v2",
                  "shadow_stays_v2", "shadow_trips_v2")
        missing = [t for t in needed if t not in names]
        if missing:
            raise LocationMigrationError("missing tables: " + ", ".join(missing))

        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            # a0) place_id 落定：正式表现有 ID 沿用（resolve 规则 9/10/12，与
            # Task 4 迁移同源——shadow 的 cluster key 会破坏稳定 ID 语义）；
            # shadow 四表引用同步改写；merge 冲突 tag 写 place_tag_conflicts
            # （非 survivor 行随后删除，tag 不静默丢）
            olds = _load_formal_places(conn)
            drafts = _load_shadow_drafts(conn)
            finalized, _, conflicts = resolve_place_ids(olds, drafts)
            rename: dict[str, str] = {}
            final_by_cluster: dict[str, dict] = {}
            for d, f in zip(drafts, finalized):
                final_by_cluster[d["place_id"]] = f
                if d["place_id"] != f["place_id"]:
                    rename[d["place_id"]] = f["place_id"]
            _rewrite_shadow_place_ids(conn, rename, final_by_cluster)
            if conflicts:
                conn.executemany(
                    "INSERT OR REPLACE INTO place_tag_conflicts_v2(device_id, "
                    "new_place_id, old_place_id, tag, reason) VALUES (?,?,?,?,?)",
                    [
                        (c["device_id"], rename.get(c["new_place_id"], c["new_place_id"]),
                         c["old_place_id"], c["tag"], c["reason"])
                        for c in conflicts
                    ],
                )

            # a0.5) geocode 失效判定：shadow（已 rewrite，final ID）vs 正式表
            # 旧中心——必须在 UPSERT 改写 lat/lon 前快照
            old_center: dict[tuple[str, str], tuple[float, float]] = {
                (r[0], r[1]): (r[2], r[3])
                for r in conn.execute("SELECT device_id, place_id, lat, lon FROM places")
            }
            shadow_centers = [
                (r["device_id"], r["place_id"], r["lat"], r["lon"])
                for r in conn.execute(
                    "SELECT device_id, place_id, lat, lon FROM shadow_places_v2"
                )
            ]
            invalidate = [
                (d, p) for d, p, lat, lon in shadow_centers
                if (d, p) in old_center
                and haversine_m(old_center[(d, p)][0], old_center[(d, p)][1], lat, lon)
                > regeo_shift_m
            ]

            # a) places UPSERT：统计列以 shadow 为准；label/geocode 保留正式表现值
            cols = ", ".join(REBUILD_PLACE_STATS)
            sets = ", ".join(f"{c}=excluded.{c}" for c in REBUILD_PLACE_STATS)
            conn.execute(
                f"INSERT INTO places(device_id, place_id, {cols}) "
                f"SELECT device_id, place_id, {cols} FROM shadow_places_v2 WHERE 1 "
                f"ON CONFLICT(device_id, place_id) DO UPDATE SET {sets}, "
                "updated_at=datetime('now','+8 hours')"
            )

            # b) geocode 缓存失效（§2.5）：中心偏移超阈值 → 清空待重编
            if invalidate:
                nulls = ", ".join(f"{c}=NULL" for c in GEOCODE_FIELDS)
                conn.executemany(
                    f"UPDATE places SET {nulls}, name_evidence='' "
                    "WHERE device_id=? AND place_id=?",
                    invalidate,
                )

            # c) shadow 中不存在的地点删除（事实消失；人工 tag 由标签文件兜底）
            conn.execute(
                "DELETE FROM places WHERE (device_id, place_id) NOT IN "
                "(SELECT device_id, place_id FROM shadow_places_v2)"
            )

            # d) place_cells / stays / trips 全量重建（幂等）。AUTOINCREMENT 表 DELETE
            # 不重置 sqlite_sequence，必须在 INSERT 前重置，否则本轮行沿用旧序列
            # 高位 id（第一次 5..8、第二次 1..4 → 两次内容不一致）
            has_seq = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone() is not None
            conn.execute("DELETE FROM place_cells")
            conn.execute(
                "INSERT INTO place_cells(device_id, place_id, grid_key) "
                "SELECT device_id, place_id, grid_key FROM shadow_place_cells_v2"
            )
            conn.execute("DELETE FROM stays")
            if has_seq:
                conn.execute("DELETE FROM sqlite_sequence WHERE name='stays'")
            stay_cols = ("device_id, start_ts, end_ts, duration_ms, center_lat, "
                         "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, "
                         "accuracy_known_points, avg_accuracy_m, radius_m, grid_key, "
                         "place_id, source_coord_system, day")
            conn.execute(
                f"INSERT INTO stays({stay_cols}) SELECT {stay_cols} FROM shadow_stays_v2"
            )
            conn.execute("DELETE FROM trips")
            if has_seq:
                conn.execute("DELETE FROM sqlite_sequence WHERE name='trips'")
            trip_cols = ("device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "
                         "end_lat, end_lon, from_place_id, to_place_id, "
                         "endpoint_coord_system, dist_m, n_points, day, polyline, "
                         "polyline_coord_system, route_key, route_mode, route_encoded_at")
            conn.execute(
                f"INSERT INTO trips({trip_cols}) SELECT {trip_cols} FROM shadow_trips_v2"
            )

            n_stays = conn.execute("SELECT COUNT(*) FROM stays").fetchone()[0]
            if "etl_runs" in _table_names(conn):
                conn.execute(
                    "INSERT INTO etl_runs(version, mode, status, started_at, finished_at, "
                    "rows_stays) VALUES (?, 'location_v2_rebuild', 'done', "
                    "datetime('now','+8 hours'), datetime('now','+8 hours'), ?)",
                    ("2.0.0", n_stays),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        print(
            f"[etl] location v2 rebuild: stays={n_stays} "
            f"geocode_invalidated={len(invalidate)}"
        )
        return n_stays
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task 4：迁移稳定 ID、人工 tag 与 geocode 缓存（§2.4 步骤 3-6 / §2.5）
# ---------------------------------------------------------------------------

# 阻断 activate 的 issue 种类（resolution_status=open 时）。
BLOCKING_ISSUE_KINDS: tuple[str, ...] = ("multi_device_ambiguity",)


def _legacy_place_id(device_id: str, grid_key: str) -> str:
    """§2.4 步骤 3：旧 (device_id,grid_key) 的 legacy place ID。"""
    return hashlib.sha1(f"{device_id}|legacy|{grid_key}".encode()).hexdigest()[:16]


def _load_old_places(conn: sqlite3.Connection) -> list:
    """v1 places 表 → OldPlace 列表（含 geocode 字段，供 matching 与缓存迁移）。"""
    from gacore.langTrack.location_facts import OldPlace

    cols = ",".join(("device_id", "grid_key", "lat", "lon", "label", *GEOCODE_FIELDS))
    out = []
    for r in conn.execute(f"SELECT {cols} FROM places"):
        row = dict(zip(("device_id", "grid_key", "lat", "lon", "label", *GEOCODE_FIELDS), r))
        out.append(
            OldPlace(
                device_id=row["device_id"],
                place_id=_legacy_place_id(row["device_id"], row["grid_key"]),
                grid_key=row["grid_key"],
                lat=row["lat"],
                lon=row["lon"],
                label=row["label"] or "未知",
                poi=row["poi"],
                address=row["address"],
                matched_level=row["matched_level"],
                grid_keys=(row["grid_key"],),
                geocode={k: row[k] for k in GEOCODE_FIELDS},
            )
        )
    return out


def _load_shadow_drafts(conn: sqlite3.Connection) -> list[dict]:
    """shadow_places_v2 + shadow_place_cells_v2 → matching 输入 drafts。"""
    drafts = []
    for r in conn.execute(
        "SELECT device_id, place_id, grid_key, lat, lon FROM shadow_places_v2"
    ):
        members = tuple(
            gk
            for (gk,) in conn.execute(
                "SELECT grid_key FROM shadow_place_cells_v2 WHERE device_id=? AND place_id=?",
                (r[0], r[1]),
            )
        )
        drafts.append(
            {
                "device_id": r[0],
                "place_id": r[1],
                "grid_key": r[2],
                "lat": r[3],
                "lon": r[4],
                "member_grid_keys": members,
            }
        )
    return drafts


def _rewrite_shadow_place_ids(
    conn: sqlite3.Connection,
    rename: dict[str, str],
    final_by_cluster: dict[str, dict],
) -> None:
    """shadow 四表 place_id 落定改写（cluster key → final ID；prepare/rebuild 共用）。"""
    for old_cluster, final_id in sorted(rename.items()):
        device_id = final_by_cluster[old_cluster]["device_id"]
        conn.execute(
            "UPDATE shadow_place_cells_v2 SET place_id=? WHERE device_id=? AND place_id=?",
            (final_id, device_id, old_cluster),
        )
        conn.execute(
            "UPDATE shadow_places_v2 SET place_id=? WHERE device_id=? AND place_id=?",
            (final_id, device_id, old_cluster),
        )
        conn.execute(
            "UPDATE shadow_stays_v2 SET place_id=? WHERE device_id=? AND place_id=?",
            (final_id, device_id, old_cluster),
        )
        for col in ("from_place_id", "to_place_id"):
            conn.execute(
                f"UPDATE shadow_trips_v2 SET {col}=? WHERE device_id=? AND {col}=?",
                (final_id, device_id, old_cluster),
            )


def _decide_labels_and_cache(
    olds: list,
    drafts: list[dict],
    finalized: list[dict],
    old_to_new: dict[str, str],
    regeo_shift_m: float,
) -> dict:
    """纯决策（无 DB/IO）：DB 人工 tag 归属 + geocode 缓存迁移/失效。

    返回 {rename, label_updates, tag_conflicts, geocode_updates,
    geocode_reused, geocode_invalidated}。
    """
    from gacore.langTrack.location_facts import haversine_m

    final_by_cluster = {d["place_id"]: f for d, f in zip(drafts, finalized)}
    olds_by_cluster: dict[str, list] = {}
    for o in olds:
        ck = old_to_new.get(o.place_id)
        if ck is not None:
            olds_by_cluster.setdefault(ck, []).append(o)

    rename: dict[str, str] = {}
    for d, f in zip(drafts, finalized):
        if d["place_id"] != f["place_id"]:
            rename[d["place_id"]] = f["place_id"]

    label_updates: list[tuple[str, str, str]] = []  # (device_id, final_id, label)
    tag_conflicts: list[tuple] = []  # (device_id, new_place_id, old_place_id, tag, reason)
    geocode_updates: list[tuple[str, str, dict]] = []  # (device_id, final_id, fields)
    geocode_reused = geocode_invalidated = 0

    for ck, draft in final_by_cluster.items():
        final_id = rename.get(ck, ck)
        device_id = draft["device_id"]
        members = olds_by_cluster.get(ck, [])

        # 人工 tag：唯一 tag 直接归属；多个不同 tag → conflict + 未知（禁止静默择一）
        tags = sorted({o.label for o in members if o.label and o.label != "未知"})
        if len(tags) <= 1:
            label_updates.append((device_id, final_id, tags[0] if tags else "未知"))
        else:
            label_updates.append((device_id, final_id, "未知"))
            for o in members:
                if o.label and o.label != "未知":
                    tag_conflicts.append(
                        (device_id, final_id, o.place_id, o.label, "merge_conflicting_tags")
                    )

        if not members:
            continue
        # geocode 缓存（§2.5）：中心偏移>阈值 → 失效；单 old 需 poi/address 非空；
        # merge 需全部偏移达标且非空 poi/address/matched_level 完全一致
        dists = [haversine_m(o.lat, o.lon, draft["lat"], draft["lon"]) for o in members]
        if any(d > regeo_shift_m for d in dists):
            geocode_invalidated += 1
            continue
        if len(members) == 1:
            o = members[0]
            if o.poi or o.address:
                geocode_updates.append((device_id, final_id, dict(o.geocode)))
                geocode_reused += 1
            else:
                geocode_invalidated += 1
        else:
            sigs = {(o.poi or "", o.address or "", o.matched_level or "") for o in members}
            if len(sigs) == 1 and (members[0].poi or members[0].address):
                geocode_updates.append((device_id, final_id, dict(members[0].geocode)))
                geocode_reused += 1
            else:
                geocode_invalidated += 1

    return {
        "rename": rename,
        "label_updates": label_updates,
        "tag_conflicts": tag_conflicts,
        "geocode_updates": geocode_updates,
        "geocode_reused": geocode_reused,
        "geocode_invalidated": geocode_invalidated,
        "final_by_cluster": final_by_cluster,
    }


def _map_label_file(
    version: int,
    rows: list[dict],
    cells_final: dict[tuple[str, str], str],
    shadow_final: set[tuple[str, str]],
    now_iso: str,
) -> tuple[list[dict], list[tuple]]:
    """纯映射：标签文件行 → (v3 labels, issues)。

    - v1 平铺：仅单设备 shadow 可自动迁移；多设备全部进 multi_device_ambiguity；
    - v2 (device_id,grid_key)：经 place_cells 映射 place_id，无匹配进 issue；
    - v3：place_id 仍存在于 shadow 才保留，否则进 issue。
    issues 元素：(kind, source_payload_json, device_id, grid_key, tag)。
    """
    v3_labels: list[dict] = []
    issues: list[tuple] = []

    def _issue(kind: str, payload: dict, device_id=None, grid_key=None, tag=None):
        issues.append(
            (kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), device_id, grid_key, tag)
        )

    if version == 0:
        return v3_labels, issues

    if version == 1:
        devices = {d for d, _ in cells_final}
        if len(devices) > 1:
            for row in rows:
                _issue(
                    "multi_device_ambiguity",
                    {"grid_key": row["grid_key"], "tag": row["tag"]},
                    grid_key=row["grid_key"],
                    tag=row["tag"],
                )
            return v3_labels, issues
        dev = next(iter(devices)) if devices else None
        for row in rows:
            pid = cells_final.get((dev, row["grid_key"])) if dev else None
            if pid is None:
                _issue(
                    "unmapped_tag",
                    {"grid_key": row["grid_key"], "tag": row["tag"]},
                    grid_key=row["grid_key"],
                    tag=row["tag"],
                )
                continue
            v3_labels.append(
                {
                    "device_id": dev,
                    "place_id": pid,
                    "anchor_grid_key": row["grid_key"],
                    "tag": row["tag"],
                    "updated_at": now_iso,
                }
            )
        return v3_labels, issues

    for row in rows:
        if version == 2:
            pid = cells_final.get((row["device_id"], row["grid_key"]))
            if pid is None:
                _issue(
                    "unmapped_tag",
                    {**row},
                    device_id=row["device_id"],
                    grid_key=row["grid_key"],
                    tag=row["tag"],
                )
                continue
            v3_labels.append(
                {
                    "device_id": row["device_id"],
                    "place_id": pid,
                    "anchor_grid_key": row["grid_key"],
                    "tag": row["tag"],
                    "updated_at": now_iso,
                }
            )
        else:  # v3
            if (row["device_id"], row["place_id"]) not in shadow_final:
                _issue(
                    "unmapped_tag",
                    {**row},
                    device_id=row["device_id"],
                    grid_key=row.get("anchor_grid_key"),
                    tag=row["tag"],
                )
                continue
            v3_labels.append(row)

    return v3_labels, issues


def prepare_location_migration(
    db_path: Path | str,
    *,
    labels_path: Path | str,
    run_id: str,
    regeo_shift_m: float = 50.0,
) -> dict:
    """Task 4：迁移稳定 ID、人工 tag 与 geocode 缓存（§2.4 步骤 3-6）。

    前置：build_location_shadow 已运行（shadow_* 表就绪且非空）。
    单个 BEGIN IMMEDIATE 事务内完成 DB 部分，事务外 fsync 写标签 pending：

    1. 旧 places → legacy OldPlace（ID=sha1(device|legacy|grid)[:16]）；
    2. resolve_place_ids 落定 shadow place 最终 place_id（规则 9/10/12），并同步改写
       shadow_places/place_cells/stays/trips 全部引用；
    3. DB 人工 tag：split 随最佳 child；merge 多个不同 tag 写 place_tag_conflicts_v2，
       label 置未知（禁止静默择一）；
    4. geocode 缓存（§2.5）：偏移≤regeo_shift_m 且证据满足才迁移（name_evidence=
       legacy_cache），其余失效待重编；
    5. 标签文件 → v3：v1 平铺仅单设备自动迁移（多设备歧义写 issues 并阻断 activate）；
       v2 (device,grid) 经 place_cells 映射；无匹配写 issues（原始 payload 保留）；
    6. 写 location_place_mapping / location_migration_issues / location_migration_metrics，
       state=prepared；
    7. 事务外写 <labels>.v3.pending（fsync）+ <labels>.v2_backup 备份，正式文件不动。

    返回报告 dict（mapping/tag/conflict/geocode 计数与 pending 路径）。
    """
    from gacore.langTrack import label_places
    from gacore.langTrack.location_facts import (
        haversine_m,
        jaccard,
        resolve_place_ids,
    )

    db_path = Path(db_path)
    labels_path = Path(labels_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        names = _table_names(conn)
        for t in ("places", "shadow_places_v2", "shadow_place_cells_v2"):
            if t not in names:
                raise LocationMigrationError(
                    f"missing table: {t} (run build_location_shadow first)"
                )
        create_location_v2_tables(conn)  # 审计表 / conflicts 表幂等创建

        olds = _load_old_places(conn)
        drafts = _load_shadow_drafts(conn)
        finalized, old_to_new, _ = resolve_place_ids(olds, drafts)
        decision = _decide_labels_and_cache(olds, drafts, finalized, old_to_new, regeo_shift_m)
        rename = decision["rename"]
        final_by_cluster = decision["final_by_cluster"]

        # mapping 指标（jaccard / distance 重算，供 dashboard 迁移审查）
        mapping_rows = []
        for o in olds:
            ck = old_to_new.get(o.place_id)
            if ck is None:
                mapping_rows.append(
                    (run_id, o.device_id, o.grid_key, o.place_id, None, "unmatched", None, None)
                )
                continue
            draft = final_by_cluster[ck]
            final_id = rename.get(ck, ck)
            jac = jaccard({o.grid_key}, set(draft["member_grid_keys"]))
            dist = haversine_m(o.lat, o.lon, draft["lat"], draft["lon"])
            mapping_rows.append(
                (
                    run_id, o.device_id, o.grid_key, o.place_id, final_id,
                    "matched", round(jac, 4), round(dist, 2),
                )
            )

        # 标签文件映射（纯计算，cells_final 事务内改写后读取）
        now_iso = label_places.now_cst()
        version, rows = label_places.load_label_doc(labels_path)

        conn.execute("BEGIN IMMEDIATE")
        try:
            # a) place_id 落定改写（places/cells/stays/trips 引用一致）
            _rewrite_shadow_place_ids(conn, rename, final_by_cluster)

            # b) DB 人工 tag + merge conflicts
            for device_id, final_id, label in decision["label_updates"]:
                conn.execute(
                    "UPDATE shadow_places_v2 SET label=? WHERE device_id=? AND place_id=?",
                    (label, device_id, final_id),
                )
            conn.executemany(
                "INSERT OR REPLACE INTO place_tag_conflicts_v2(device_id, new_place_id, "
                "old_place_id, tag, reason) VALUES (?,?,?,?,?)",
                decision["tag_conflicts"],
            )

            # c) geocode 缓存迁移
            for device_id, final_id, fields in decision["geocode_updates"]:
                sets = ", ".join(f"{k}=?" for k in GEOCODE_FIELDS)
                conn.execute(
                    f"UPDATE shadow_places_v2 SET {sets}, name_evidence='legacy_cache' "
                    "WHERE device_id=? AND place_id=?",
                    (*fields.values(), device_id, final_id),
                )

            # d) mapping / issues / metrics / state
            conn.executemany(
                "INSERT OR REPLACE INTO location_place_mapping(run_id, old_device_id, "
                "old_grid_key, old_place_id, new_place_id, match_reason, jaccard, distance_m) "
                "VALUES (?,?,?,?,?,?,?,?)",
                mapping_rows,
            )

            cells_final = {
                (r[0], r[1]): r[2]
                for r in conn.execute(
                    "SELECT device_id, grid_key, place_id FROM shadow_place_cells_v2"
                )
            }
            shadow_final = {
                (r[0], r[1])
                for r in conn.execute("SELECT device_id, place_id FROM shadow_places_v2")
            }
            v3_labels, label_issues = _map_label_file(
                version, rows, cells_final, shadow_final, now_iso
            )
            conn.executemany(
                "INSERT INTO location_migration_issues(run_id, kind, source_payload, "
                "device_id, grid_key, tag) VALUES (?,?,?,?,?,?)",
                [(run_id, *issue) for issue in label_issues],
            )

            metrics = {
                "old_places_total": len(olds),
                "old_places_matched": len(old_to_new),
                "place_id_renamed": len(rename),
                "shadow_places_total": len(drafts),
                "label_file_version": version,
                "tag_total": len(rows),
                "tag_migrated": len(v3_labels),
                "tag_issues": len(label_issues),
                "tag_conflicts": len(decision["tag_conflicts"]),
                "geocode_reused": decision["geocode_reused"],
                "geocode_invalidated": decision["geocode_invalidated"],
                "blocked": 1 if any(i[0] in BLOCKING_ISSUE_KINDS for i in label_issues) else 0,
            }
            conn.executemany(
                "INSERT OR REPLACE INTO location_migration_metrics(run_id, metric, value) "
                "VALUES (?,?,?)",
                [(run_id, k, v) for k, v in metrics.items()],
            )
            _write_state(conn, run_id, 1, "prepared")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # e) 事务外两阶段文件：pending + backup（正式文件不动）
        pending = label_places.write_labels_v3_pending(labels_path, v3_labels)

        report = {
            "run_id": run_id,
            **metrics,
            "pending_labels_path": str(pending),
        }
        print(
            f"[migration] prepare: old={len(olds)} matched={len(old_to_new)} "
            f"renamed={len(rename)} tag={len(rows)}→{len(v3_labels)} "
            f"issues={len(label_issues)} conflicts={len(decision['tag_conflicts'])} "
            f"geocode reused/invalidated={decision['geocode_reused']}/{decision['geocode_invalidated']}"
        )
        return report
    finally:
        conn.close()


def _pending_label_path(labels_path: Path, state_pending: str | None) -> Path:
    return Path(state_pending) if state_pending else labels_path.with_name(
        labels_path.name + ".v3.pending"
    )


def _finish_state_complete(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        _write_state(conn, run_id, 2, "complete")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def finalize_label_swap(db_path: Path | str, *, labels_path: Path | str | None = None) -> Path:
    """两阶段切换第二步：DB 已 COMMIT 后原子替换标签文件并写 status=complete。

    仅接受 status=pending_label_swap；pending 文件缺失抛错（用
    recover_pending_swap 走 DB 投影兜底）。返回正式标签文件路径。
    """
    from gacore.langTrack import label_places

    db_path = Path(db_path)
    target = Path(labels_path) if labels_path else label_places.CONFIG_PATH
    conn = sqlite3.connect(db_path)
    try:
        state = read_migration_state(conn)
        if not state or state["status"] != "pending_label_swap":
            current = state["status"] if state else None
            raise LocationMigrationError(f"no pending label swap (status={current!r})")
        pending = _pending_label_path(target, state["pending_labels_path"])
        label_places.swap_pending_labels(pending, target)
        _finish_state_complete(conn, state["run_id"])
        return target
    finally:
        conn.close()


def recover_pending_swap(db_path: Path | str, *, labels_path: Path | str | None = None) -> str:
    """服务启动恢复：发现 pending_label_swap 时完成标签文件切换（§2.4 步骤 13）。

    - pending 文件存在 → 原子替换正式文件（"swapped"）；
    - pending 丢失 → 从正式 places（v2）label 列投影重建标签文件（"projected"）；
    - 无 pending 状态 → no-op（"none"）。
    完成后均以短事务写 status=complete。
    """
    from gacore.langTrack import label_places

    db_path = Path(db_path)
    target = Path(labels_path) if labels_path else label_places.CONFIG_PATH
    conn = sqlite3.connect(db_path)
    try:
        state = read_migration_state(conn)
        if not state or state["status"] != "pending_label_swap":
            return "none"
        pending = _pending_label_path(target, state["pending_labels_path"])
        if pending.exists():
            label_places.swap_pending_labels(pending, target)
            action = "swapped"
        else:
            rows = label_places.project_labels_v3_from_db(conn)
            label_places.write_labels_v3_atomic(target, rows)
            action = "projected"
        _finish_state_complete(conn, state["run_id"])
        print(f"[migration] label swap recovered: {action}")
        return action
    finally:
        conn.close()
