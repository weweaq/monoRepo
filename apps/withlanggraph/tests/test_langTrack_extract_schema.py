"""KG 抽取表结构测试（封闭）：幂等建表 + R6 时间字段 + 索引齐备。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_langTrack_extract_schema.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.langTrack.extract.schema import migrate_extract_tables  # noqa: E402

_S1_TABLES = ("kg_runs", "kg_nodes", "kg_edges")


def _connect(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "kg.db")


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
    ).fetchall()


def test_migrate_creates_s1_tables(tmp_path):
    conn = _connect(tmp_path)
    try:
        migrate_extract_tables(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(_S1_TABLES) <= names
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    """连跑两次，sqlite_master 完全一致（只增不改的幂等建表）。"""
    conn = _connect(tmp_path)
    try:
        migrate_extract_tables(conn)
        first = _schema_snapshot(conn)
        migrate_extract_tables(conn)
        assert _schema_snapshot(conn) == first
    finally:
        conn.close()


def test_every_table_has_r6_timestamp_columns(tmp_path):
    """R6：每表必有 created_at / updated_at，且默认东八区时间。"""
    conn = _connect(tmp_path)
    try:
        migrate_extract_tables(conn)
        for table in _S1_TABLES:
            cols = {r[1]: r[4] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert "created_at" in cols and "updated_at" in cols, table
            for col in ("created_at", "updated_at"):
                assert "+8 hours" in (cols[col] or ""), f"{table}.{col} 默认值应为东八区"
    finally:
        conn.close()


def test_runs_unique_index_and_edge_lookup_indexes(tmp_path):
    conn = _connect(tmp_path)
    try:
        migrate_extract_tables(conn)
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "ux_kg_runs_day_hash_ver" in indexes
        assert {"idx_kg_edges_from", "idx_kg_edges_to", "idx_kg_edges_day"} <= indexes
        assert {"idx_kg_nodes_type", "idx_kg_nodes_label"} <= indexes
    finally:
        conn.close()


def test_runs_day_hash_ver_is_unique(tmp_path):
    """幂等锚：同日 + 同素材 + 同 prompt 版本只能有一条运行记录。"""
    conn = _connect(tmp_path)
    try:
        migrate_extract_tables(conn)
        sql = (
            "INSERT INTO kg_runs (run_id, day, raw_hash, prompt_ver, status, started_at) "
            "VALUES (?, '2026-10-16', 'h1', 'kg@v1', 'ok', '2026-10-16 23:30:00')"
        )
        with conn:
            conn.execute(sql, ("r1",))
        try:
            with conn:
                conn.execute(sql, ("r2",))
        except sqlite3.IntegrityError:
            return
        raise AssertionError("ux_kg_runs_day_hash_ver 未拦住重复 (day, raw_hash, prompt_ver)")
    finally:
        conn.close()
