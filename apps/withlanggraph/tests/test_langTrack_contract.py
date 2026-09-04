"""A① 契约覆盖校验单测：构造假 events，断言 build_contract_coverage 的 status 判定。

测试用 sqlite3 内存库，通过 executescript(etl._SCHEMA) 建立 contract_coverage 表，
不依赖真实 data/langTrack.db。运行需 PYTHONPATH=src。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.langTrack import etl  # noqa: E402
from gacore.langTrack.contract import EXPECTED_EVENT_TYPES, STALE_DAYS


def _seed_events(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: [(type, ts, payload), ...]"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events "
        "(id INTEGER PRIMARY KEY, type TEXT, ts INTEGER, payload TEXT)"
    )
    conn.executemany("INSERT INTO events(type, ts, payload) VALUES (?,?,?)", rows)


def _build(conn: sqlite3.Connection) -> dict[str, str]:
    """跑一次 build_contract_coverage，返回 type -> status。"""
    conn.executescript(etl._SCHEMA)
    conn.row_factory = sqlite3.Row
    etl.build_contract_coverage(conn)
    return {
        r["type"]: r["status"]
        for r in conn.execute("SELECT type, status FROM contract_coverage")
    }


def test_contract_coverage_status():
    now_ms = int(time.time() * 1000)
    old_ms = now_ms - (STALE_DAYS + 1) * 86400000  # 超过 STALE_DAYS 天 → stale
    recent_ms = now_ms - 3600 * 1000

    rows = []
    # 部分契约类型近期到达（ok）
    for t in ("usage", "session", "notification", "location"):
        rows.append((t, recent_ms, "{}"))
    # 契约类型到达但陈旧（stale）
    rows.append(("clipboard", old_ms, "{}"))
    # 实际到达但不在契约（unexpected）
    rows.append(("mystery_type", recent_ms, "{}"))
    # screen_content / media / bt_device / call 故意缺失（missing）

    conn = sqlite3.connect(":memory:")
    _seed_events(conn, rows)

    cov = _build(conn)

    # ok
    assert cov["usage"] == "ok"
    assert cov["session"] == "ok"
    assert cov["notification"] == "ok"
    assert cov["location"] == "ok"
    # stale
    assert cov["clipboard"] == "stale"
    # missing（契约存在但未到达）
    assert cov["screen_content"] == "missing"
    assert cov["media"] == "missing"
    assert cov["bt_device"] == "missing"
    assert cov["call"] == "missing"
    # unexpected（到达但不在契约），且 expected=0
    assert cov["mystery_type"] == "unexpected"
    exp = conn.execute(
        "SELECT expected FROM contract_coverage WHERE type='mystery_type'"
    ).fetchone()[0]
    assert exp == 0


def test_contract_coverage_expected_present():
    """所有契约类型都到达且近期 → 全部 ok，无 unexpected。"""
    now_ms = int(time.time() * 1000)
    recent_ms = now_ms - 3600 * 1000
    rows = [(t, recent_ms, "{}") for t in EXPECTED_EVENT_TYPES]
    conn = sqlite3.connect(":memory:")
    _seed_events(conn, rows)
    cov = _build(conn)
    assert all(s == "ok" for s in cov.values())
    assert "unexpected" not in cov


def test_contract_coverage_rowcount():
    """重建行数 = 契约类型数（含 missing）+ unexpected 数。"""
    now_ms = int(time.time() * 1000)
    recent_ms = now_ms - 3600 * 1000
    rows = [("usage", recent_ms, "{}"), ("ghost_type", recent_ms, "{}")]
    conn = sqlite3.connect(":memory:")
    _seed_events(conn, rows)
    conn.executescript(etl._SCHEMA)
    n = etl.build_contract_coverage(conn)
    assert n == len(EXPECTED_EVENT_TYPES) + 1  # 17 契约 + 1 unexpected
