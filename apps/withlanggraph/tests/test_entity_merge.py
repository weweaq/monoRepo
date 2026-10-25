"""KG 实体合并 P1 的封闭测试（设计 §10：T1/T2/T3/T5/T6/T7/T8/T9/T10）。

全部用临时 SQLite + 合成人名，不触真实 langTrack.db、不调 LLM、不触网（T16）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from gacore.langTrack.entity_merge import (
    MergeConflict,
    list_candidates,
    merge_nodes,
    preview_merge,
    reject_merge,
    resolve_node,
    revoke_merge,
)
from gacore.langTrack.extract.schema import migrate_extract_tables


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    migrate_extract_tables(c)
    yield c
    c.close()


def _seed_node(conn, node_id: str, label: str, node_type: str = "Person",
               aliases: list[str] | None = None, day: str = "2026-10-01") -> None:
    conn.execute(
        "INSERT INTO kg_nodes (node_id, node_type, label, aliases, first_day, last_day, src, run_id)"
        " VALUES (?, ?, ?, ?, ?, ?, 'test', 'run-1')",
        (node_id, node_type, label, json.dumps(aliases or []), day, day),
    )
    conn.commit()


def _seed_edge(conn, edge_id: str, from_node: str, to_node: str, day: str,
               assertion: str = "evidence") -> None:
    conn.execute(
        "INSERT INTO kg_edges (edge_id, from_node, to_node, relation, edge_level,"
        " assertion_text, day, src, run_id)"
        " VALUES (?, ?, ?, 'mentions', 'L1', ?, ?, 'test', 'run-1')",
        (edge_id, from_node, to_node, assertion, day),
    )
    conn.commit()


def _raw_counts(conn) -> tuple[int, int]:
    nodes = conn.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
    return int(nodes), int(edges)


# T1/T2：迁移幂等，raw 行数不变；reviewer/reason 列存在且重复迁移不报错
def test_t1_t2_migration_idempotent_raw_unchanged(conn):
    _seed_node(conn, "person_user", "用户（本人）")
    _seed_node(conn, "person_a", "张三")
    _seed_edge(conn, "edge-1", "person_user", "person_a", "2026-10-01")
    before = _raw_counts(conn)

    migrate_extract_tables(conn)  # 再跑一次（幂等）

    assert _raw_counts(conn) == before
    cols = {row[1] for row in conn.execute("PRAGMA table_info(kg_aliases)")}
    assert {"reviewer", "reason"} <= cols
    views = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'kg_%'"
        )
    }
    assert {
        "kg_alias_active",
        "kg_entity_map",
        "kg_resolved_nodes",
        "kg_resolved_edges",
        "kg_resolved_edges_agg",
        "kg_resolved_relations",
    } <= views


# T3：kg_entity_map 覆盖全部 kg_nodes 且无重复
def test_t3_entity_map_covers_all_nodes(conn):
    for i in range(5):
        _seed_node(conn, f"person_{i}", f"人物{i}")
    rows = conn.execute("SELECT node_id, entity_id FROM kg_entity_map").fetchall()
    assert len(rows) == 5
    assert {r[0] for r in rows} == {f"person_{i}" for i in range(5)}
    assert all(r[0] == r[1] for r in rows)  # 无 alias 时映射到自身


# T5：manual merge 折叠 resolved、raw 不变；审计字段落盘
def test_t5_manual_merge_folds_and_keeps_raw(conn):
    _seed_node(conn, "person_user", "用户（本人）")
    _seed_node(conn, "person_wo", "我")
    _seed_edge(conn, "edge-1", "person_wo", "person_user", "2026-10-01")
    before = _raw_counts(conn)

    result = merge_nodes(conn, "person_user", "person_wo", "reviewer-x", "dup self")

    assert result["ok"] and result["changed"]
    assert result["moved"]["nodes"] == 1 and result["moved"]["edges"] == 1
    assert _raw_counts(conn) == before  # raw 永不改写
    assert resolve_node(conn, "person_wo") == "person_user"
    row = conn.execute(
        "SELECT reviewer, reason, method, src FROM kg_aliases WHERE alias='person_wo'"
    ).fetchone()
    assert row == ("reviewer-x", "dup self", "manual", "entity_merge")
    resolved = conn.execute(
        "SELECT member_count FROM kg_resolved_nodes WHERE entity_id='person_user'"
    ).fetchone()
    assert resolved[0] == 2
    # 幂等：同一 pair 再合并 → changed=False
    again = merge_nodes(conn, "person_user", "person_wo", "reviewer-x", "retry")
    assert again["changed"] is False


# T6：链式 A→B→C 解析到 C 的 canonical；撤销后链式恢复
def test_t6_chain_merge_and_revoke_restores(conn):
    for nid in ("person_a", "person_b", "person_c"):
        _seed_node(conn, nid, nid)

    merge_nodes(conn, "person_b", "person_c", "r", "chain1")  # C→B
    merge_nodes(conn, "person_a", "person_b", "r", "chain2")  # B→A

    assert resolve_node(conn, "person_c") == "person_a"
    assert resolve_node(conn, "person_b") == "person_a"

    revoked = revoke_merge(conn, "person_b", "r", "undo chain2")
    assert revoked["changed"] and revoked["restored_from"] == "person_a"

    assert resolve_node(conn, "person_c") == "person_b"  # 链式恢复：C 回到 B
    assert resolve_node(conn, "person_b") == "person_b"
    assert resolve_node(conn, "person_a") == "person_a"


# T7：reject 后 pair 不再出现在候选里，resolved 不变
def test_t7_reject_suppresses_candidate(conn):
    _seed_node(conn, "person_a", "张三")
    _seed_node(conn, "person_b", "张三")  # 同名不同人 → same_label 候选
    pair = next(c for c in list_candidates(conn) if c["source"] == "same_label")

    result = reject_merge(conn, pair["keep_id"], pair["drop_id"], "r", "not same person")
    assert result["changed"]

    assert all(
        {c["keep_id"], c["drop_id"]} != {"person_a", "person_b"}
        for c in list_candidates(conn)
    )
    assert resolve_node(conn, "person_a") == "person_a"  # resolved 不变
    assert reject_merge(conn, pair["keep_id"], pair["drop_id"], "r", "again")["changed"] is False


# T8：同点 / 类型不一致 / 节点不存在 → MergeConflict，事务不写入；直接写入的环不炸视图
def test_t8_merge_conflicts(conn):
    _seed_node(conn, "person_a", "张三")
    _seed_node(conn, "project_x", "项目X", node_type="Project")

    with pytest.raises(MergeConflict):
        merge_nodes(conn, "person_a", "person_a", "r", "same")
    with pytest.raises(MergeConflict):
        merge_nodes(conn, "person_a", "project_x", "r", "type mismatch")
    with pytest.raises(MergeConflict):
        merge_nodes(conn, "person_a", "person_ghost", "r", "missing")
    with pytest.raises(MergeConflict):
        merge_nodes(conn, "person_a", "person_a2", "r", "missing keep")  # keep 不存在

    assert conn.execute("SELECT COUNT(*) FROM kg_aliases").fetchone()[0] == 0
    assert preview_merge(conn, "person_a", "project_x")["ok"] is False

    # 直接写入的 alias 环（绕过服务）：视图深度上限兜底，查询不挂不死
    conn.executemany(
        "INSERT INTO kg_aliases (alias, canonical, method, day, run_id) VALUES (?, ?, 'manual', '2026-10-01', 'raw')",
        [("person_x", "person_y"), ("person_y", "person_x")],
    )
    conn.commit()
    conn.execute("SELECT COUNT(*) FROM kg_entity_map").fetchone()  # 能查完即通过


# T9：合并后自环边标记且默认不进聚合视图
def test_t9_self_loop_filtered_in_agg(conn):
    _seed_node(conn, "person_a", "张三")
    _seed_node(conn, "person_b", "李四")
    _seed_edge(conn, "edge-1", "person_b", "person_a", "2026-10-01")

    merge_nodes(conn, "person_a", "person_b", "r", "same")

    loop = conn.execute(
        "SELECT is_self_loop FROM kg_resolved_edges WHERE edge_id='edge-1'"
    ).fetchone()
    assert loop[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM kg_resolved_edges_agg").fetchone()[0] == 0


# T10：聚合代表文本取最新一条（day DESC），不是 MAX(text) 字典序
def test_t10_agg_latest_text_not_max(conn):
    _seed_node(conn, "person_a", "张三")
    _seed_node(conn, "person_b", "李四")
    _seed_edge(conn, "edge-1", "person_a", "person_b", "2026-10-01", assertion="晚归")
    _seed_edge(conn, "edge-2", "person_a", "person_b", "2026-10-03", assertion="早班")

    row = conn.execute(
        "SELECT latest_assertion_text, first_day, last_day, support_count "
        "FROM kg_resolved_edges_agg WHERE from_entity='person_a' AND to_entity='person_b'"
    ).fetchone()

    assert row[0] == "早班"  # 最新日 10-03 的文本；MAX 字典序会给 "晚归"
    assert (row[1], row[2], row[3]) == ("2026-10-01", "2026-10-03", 2)


# 候选三源：exact/fuzzy 行 + 同 label + 共享 alias；已 active 的 pair 不再列
def test_list_candidates_sources_and_exclusions(conn):
    _seed_node(conn, "person_mom", "妈妈", aliases=["母亲"])
    _seed_node(conn, "person_mother", "母亲", aliases=["母亲"])  # 两节点都声明 alias「母亲」→ 共享 alias 候选
    conn.execute(
        "INSERT INTO kg_aliases (alias, canonical, method, score, day, run_id)"
        " VALUES ('person_mother', 'person_mom', 'fuzzy', 0.71, '2026-10-01', 'run-1')"
    )
    conn.commit()

    cands = list_candidates(conn)
    assert any(c["source"] == "shared_alias" and {c["keep_id"], c["drop_id"]} == {"person_mom", "person_mother"} for c in cands)

    # 该 pair 人工合并生效后，不再作为候选出现
    merge_nodes(conn, "person_mom", "person_mother", "r", "same person")
    assert all(
        {c["keep_id"], c["drop_id"]} != {"person_mom", "person_mother"}
        for c in list_candidates(conn)
    )


# CLI：--list 出 JSON；写操作缺 --reason 退出码 2
def test_cli_list_and_reason_required(tmp_path: Path):
    from gacore.langTrack.entity_merge import main

    db = tmp_path / "kg.db"
    c = sqlite3.connect(db)
    migrate_extract_tables(c)
    _seed_node(c, "person_user", "用户（本人）")
    _seed_node(c, "person_wo", "我")
    c.close()

    assert main(["--db", str(db), "--list"]) == 0
    assert main(["--db", str(db), "--merge", "person_user", "person_wo"]) == 2

    assert main(
        ["--db", str(db), "--merge", "person_user", "person_wo", "--reason", "dup"]
    ) == 0
    c = sqlite3.connect(db)
    assert c.execute(
        "SELECT canonical FROM kg_alias_active WHERE alias='person_wo'"
    ).fetchone()[0] == "person_user"
    c.close()
