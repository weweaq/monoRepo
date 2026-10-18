"""区间闭合测试（封闭：裸 sqlite + tmp 库，不触网、不触真实 langTrack.db）。

覆盖 §5 S2 的 interval closure（修正设计债①）：按 valid_from 全量重排后重算 valid_to，
接受乱序、不拒绝迟到数据；只改派生列 valid_to；invalidated 边不参与闭合。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_langTrack_interval_closure.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.langTrack.extract.intervals import reclose_day, reclose_intervals  # noqa: E402
from gacore.langTrack.extract.schema import migrate_extract_tables  # noqa: E402

DAY = "2026-10-16"
RUN = "run1"


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "kg.db")
    migrate_extract_tables(conn)
    return conn


def _add_edge(
    conn: sqlite3.Connection,
    edge_id: str,
    valid_from: str | None,
    *,
    frm: str = "person_a",
    to: str = "theme_x",
    relation: str = "prefers",
    invalidated: int = 0,
    assertion_text: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO kg_edges (edge_id, from_node, to_node, relation, edge_level, "
        "assertion_text, valid_from, invalidated, day, src, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (edge_id, frm, to, relation, "L1", assertion_text, valid_from, invalidated, DAY, "test", RUN),
    )


def _valid_to(conn: sqlite3.Connection, edge_id: str) -> str | None:
    return conn.execute(
        "SELECT valid_to FROM kg_edges WHERE edge_id = ?", (edge_id,)
    ).fetchone()[0]


def test_closes_in_valid_from_order(tmp_path):
    conn = _db(tmp_path)
    _add_edge(conn, "e1", "2026-01-01")
    _add_edge(conn, "e2", "2026-01-05")
    _add_edge(conn, "e3", "2026-01-10")
    conn.commit()

    n = reclose_intervals(conn, "person_a", "theme_x", "prefers")

    assert n == 3
    assert _valid_to(conn, "e1") == "2026-01-05"
    assert _valid_to(conn, "e2") == "2026-01-10"
    assert _valid_to(conn, "e3") is None  # 末条保持开区间


def test_accepts_out_of_order_arrival(tmp_path):
    conn = _db(tmp_path)
    # 迟到数据先落（乱序写入）——不拒绝，重排后闭合
    _add_edge(conn, "e1", "2026-01-10")
    _add_edge(conn, "e2", "2026-01-01")
    _add_edge(conn, "e3", "2026-01-05")
    conn.commit()

    reclose_intervals(conn, "person_a", "theme_x", "prefers")

    assert _valid_to(conn, "e2") == "2026-01-05"
    assert _valid_to(conn, "e3") == "2026-01-10"
    assert _valid_to(conn, "e1") is None


def test_invalidated_edges_are_excluded(tmp_path):
    conn = _db(tmp_path)
    _add_edge(conn, "e1", "2026-01-01")
    _add_edge(conn, "e2", "2026-01-05", invalidated=1)
    _add_edge(conn, "e3", "2026-01-10")
    conn.commit()

    n = reclose_intervals(conn, "person_a", "theme_x", "prefers")

    assert n == 2
    # e2 被撤销，不参与闭合：e1 直接接到 e3
    assert _valid_to(conn, "e1") == "2026-01-10"
    assert _valid_to(conn, "e2") is None  # 未处理，保持原值
    assert _valid_to(conn, "e3") is None


def test_null_valid_from_sorts_last(tmp_path):
    conn = _db(tmp_path)
    _add_edge(conn, "e1", "2026-01-01")
    _add_edge(conn, "e2", None)
    conn.commit()

    reclose_intervals(conn, "person_a", "theme_x", "prefers")

    # NULL 排在末位，故 e1 的下一条 valid_from 为 NULL → e1 保持开区间
    assert _valid_to(conn, "e1") is None
    assert _valid_to(conn, "e2") is None


def test_only_valid_to_column_is_touched(tmp_path):
    conn = _db(tmp_path)
    _add_edge(conn, "e1", "2026-01-01", assertion_text="喜欢复古机身")
    _add_edge(conn, "e2", "2026-01-05", assertion_text="换了新款")
    conn.commit()

    reclose_intervals(conn, "person_a", "theme_x", "prefers")

    row = conn.execute(
        "SELECT assertion_text, event_time, invalidated, updated_at FROM kg_edges WHERE edge_id='e1'"
    ).fetchone()
    assert row[0] == "喜欢复古机身"  # 非派生列不动
    assert row[1] is None
    assert row[2] == 0
    assert row[3] is not None  # updated_at 刷新


def test_reclose_day_covers_all_triples(tmp_path):
    conn = _db(tmp_path)
    _add_edge(conn, "a1", "2026-01-01", frm="person_a", to="theme_x", relation="prefers")
    _add_edge(conn, "a2", "2026-01-05", frm="person_a", to="theme_x", relation="prefers")
    _add_edge(conn, "b1", "2026-02-01", frm="person_a", to="person_b", relation="knows")
    _add_edge(conn, "b2", "2026-02-03", frm="person_a", to="person_b", relation="knows")
    conn.commit()

    n = reclose_day(conn, DAY)

    assert n == 4
    assert _valid_to(conn, "a1") == "2026-01-05"
    assert _valid_to(conn, "a2") is None
    assert _valid_to(conn, "b1") == "2026-02-03"
    assert _valid_to(conn, "b2") is None


def test_reclose_missing_group_returns_zero(tmp_path):
    conn = _db(tmp_path)
    assert reclose_intervals(conn, "person_x", "theme_y", "none") == 0
