"""实体消解测试（封闭：裸 sqlite + tmp 库，不触网、不触真实 langTrack.db）。

覆盖 §5 S2 三级消解与追加式可撤销（修正设计债②）：
exact 合并 / fuzzy 生效（≥0.85）/ fuzzy 候选不生效（0.6~0.85）/ 低于阈值不落行 /
撤销为追加式反向指针（旧行永不改）/ 幂等重跑 / 非 Person 不参与。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_langTrack_resolve.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.langTrack.extract.resolve import (  # noqa: E402
    alias_key,
    jaro_winkler,
    resolve_alias,
    resolve_entities,
    revoke_alias,
)
from gacore.langTrack.extract.schema import migrate_extract_tables  # noqa: E402

DAY = "2026-10-16"
RUN = "run1"


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "kg.db")
    migrate_extract_tables(conn)
    return conn


def _add_node(conn: sqlite3.Connection, node_id: str, label: str, node_type: str = "Person") -> None:
    conn.execute(
        "INSERT INTO kg_nodes (node_id, node_type, label, first_day, last_day, src, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (node_id, node_type, label, DAY, DAY, "test", RUN),
    )


def _add_edge(conn: sqlite3.Connection, edge_id: str, frm: str, to: str) -> None:
    conn.execute(
        "INSERT INTO kg_edges (edge_id, from_node, to_node, relation, edge_level, day, src, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (edge_id, frm, to, "co_occurs", "L1", DAY, "test", RUN),
    )


def _alias_rows(conn: sqlite3.Connection, alias: str) -> list[tuple]:
    return conn.execute(
        "SELECT canonical, method, score FROM kg_aliases WHERE alias = ? ORDER BY method",
        (alias,),
    ).fetchall()


def test_exact_merge_collapses_same_label(tmp_path):
    conn = _db(tmp_path)
    _add_node(conn, "person_a", "张三")
    _add_node(conn, "person_b", "张三")
    conn.commit()

    resolve_entities(conn, DAY, RUN)

    # 字典序最小的 node_id 作 canonical
    assert _alias_rows(conn, "person_b") == [("person_a", "exact", 1.0)]
    assert resolve_alias(conn, "person_b") == "person_a"


def test_fuzzy_auto_merge_when_score_high(tmp_path):
    conn = _db(tmp_path)
    _add_node(conn, "person_a", "张伟")
    _add_node(conn, "person_b", "张玮")
    # 共享同一邻居 → context=1.0、temporal=1 → score=0.4*0.7+0.3+0.2+0.1=0.88 ≥ 0.85
    _add_edge(conn, "e1", "person_a", "node_x")
    _add_edge(conn, "e2", "person_b", "node_x")
    conn.commit()

    resolve_entities(conn, DAY, RUN)

    rows = _alias_rows(conn, "person_b")
    assert len(rows) == 1
    canonical, method, score = rows[0]
    assert method == "fuzzy"
    assert canonical == "person_a"
    assert score >= 0.85
    assert resolve_alias(conn, "person_b") == "person_a"


def test_fuzzy_candidate_below_auto_merge_is_not_effective(tmp_path):
    conn = _db(tmp_path)
    _add_node(conn, "person_a", "张伟")
    _add_node(conn, "person_b", "张玮")
    # 部分共享邻居 → context=1/3、temporal=1 → score=0.28+0.1+0.2+0.1=0.68（候选带）
    _add_edge(conn, "e1", "person_a", "甲")
    _add_edge(conn, "e2", "person_a", "乙")
    _add_edge(conn, "e3", "person_b", "甲")
    _add_edge(conn, "e4", "person_b", "丙")
    conn.commit()

    resolve_entities(conn, DAY, RUN)

    rows = _alias_rows(conn, "person_b")
    assert len(rows) == 1
    _, method, score = rows[0]
    assert method == "fuzzy"
    assert 0.6 <= score < 0.85
    # 候选不生效
    assert resolve_alias(conn, "person_b") is None


def test_below_candidate_min_writes_nothing(tmp_path):
    conn = _db(tmp_path)
    _add_node(conn, "person_a", "张三")
    _add_node(conn, "person_b", "李四")
    conn.commit()

    resolve_entities(conn, DAY, RUN)

    assert conn.execute("SELECT count(*) FROM kg_aliases").fetchone()[0] == 0
    assert resolve_alias(conn, "person_b") is None


def test_revoke_is_append_only_with_reverse_pointer(tmp_path):
    conn = _db(tmp_path)
    _add_node(conn, "person_a", "张三")
    _add_node(conn, "person_b", "张三")
    conn.commit()
    resolve_entities(conn, DAY, RUN)
    assert resolve_alias(conn, "person_b") == "person_a"

    assert revoke_alias(conn, "person_b", DAY, "run2") is True

    # 旧行原封不动（追加式铁律）
    old = conn.execute(
        "SELECT canonical, method, superseded_by FROM kg_aliases "
        "WHERE alias='person_b' AND run_id=?",
        (RUN,),
    ).fetchone()
    assert old == ("person_a", "exact", None)
    # 新行以反向指针指向旧行 key，撤销后 alias 生效映射指向自身
    new = conn.execute(
        "SELECT canonical, method, superseded_by FROM kg_aliases "
        "WHERE alias='person_b' AND run_id='run2'",
    ).fetchone()
    assert new == ("person_b", "manual", alias_key("person_b", DAY, RUN))
    assert resolve_alias(conn, "person_b") == "person_b"


def test_revoke_unknown_alias_returns_false(tmp_path):
    conn = _db(tmp_path)
    assert revoke_alias(conn, "person_ghost", DAY, RUN) is False


def test_resolve_entities_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    _add_node(conn, "person_a", "张三")
    _add_node(conn, "person_b", "张三")
    _add_node(conn, "person_c", "张伟")
    _add_node(conn, "person_d", "张玮")
    _add_edge(conn, "e1", "person_c", "node_x")
    _add_edge(conn, "e2", "person_d", "node_x")
    conn.commit()

    resolve_entities(conn, DAY, RUN)
    first = conn.execute("SELECT count(*) FROM kg_aliases").fetchone()[0]

    resolve_entities(conn, DAY, RUN)

    assert conn.execute("SELECT count(*) FROM kg_aliases").fetchone()[0] == first


def test_non_person_nodes_are_not_resolved(tmp_path):
    conn = _db(tmp_path)
    _add_node(conn, "event_1", "相机选型讨论", node_type="Event")
    _add_node(conn, "event_2", "相机选型讨论", node_type="Event")
    _add_node(conn, "person_a", "张三")
    conn.commit()

    resolve_entities(conn, DAY, RUN)

    assert conn.execute("SELECT count(*) FROM kg_aliases").fetchone()[0] == 0


def test_jaro_winkler_is_local_and_bounded():
    assert jaro_winkler("张三", "张三") == 1.0
    assert jaro_winkler("", "张三") == 0.0
    assert 0.0 < jaro_winkler("张伟", "张玮") < 1.0
