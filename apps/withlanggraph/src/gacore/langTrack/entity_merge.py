"""KG 实体合并服务（kg-entity-registry-design §5.1）：人工 keep/drop 决策 + 读时解析入口。

职责边界（设计 §9.5）：本模块只写 ``kg_aliases``（append-only 身份账本），带 reviewer/reason
审计字段；**绝不 UPDATE/DELETE raw 的 kg_nodes / kg_edges / kg_relations**——折叠由
``extract.schema.ensure_entity_views`` 的 resolved 视图在读时完成。

交互参考 ``place_semantics`` 的 merge_preview/merge_places：纯读预览 → 校验 → 单事务写。
错误策略：类型不一致 / 环 / 节点不存在 / 同一节点抛 ``MergeConflict``；重复操作返回
``changed=False`` 不报错。日志只写 node_id / canonical / 计数，不写真实姓名（设计 §4.4-11）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from gacore.langTrack.extract.resolve import alias_key
from gacore.langTrack.extract.schema import migrate_extract_tables

_TZ: Final = timezone(timedelta(hours=8))
_METHOD_MANUAL: Final = "manual"
_METHOD_REJECTED: Final = "rejected"
_CANDIDATE_METHODS: Final = ("exact", "fuzzy")
_ACTIVE_METHODS_SQL: Final = "'manual', 'self', 'linker'"
_SAMPLE_EDGES: Final = 3

try:  # 日志 best-effort：失败不影响合并主路径
    from gacore.jsonl_logger import get_logger

    _logger = get_logger("entity_merge")
except Exception:  # noqa: BLE001
    _logger = None  # type: ignore[assignment]


def _log(event: str, **fields: Any) -> None:
    if _logger is not None:
        _logger.info(event, **fields)


class MergeConflict(Exception):
    """非法合并：类型不一致 / alias 环 / canonical 不存在 / 同一节点（设计 §4.5）。"""


def _now_day() -> str:
    return datetime.now(_TZ).date().isoformat()


_manual_seq: Final = __import__("itertools").count()


def _manual_run_id() -> str:
    """manual 决策 run_id：秒级时间戳 + 进程内序号（同秒多次决策也唯一，避免 PK 冲突）。"""
    return f"manual-{datetime.now(_TZ).strftime('%Y%m%d%H%M%S')}{next(_manual_seq):04d}"


def resolve_node(conn: sqlite3.Connection, node_id: str) -> str | None:
    """node 的 terminal canonical；不在图上返回 None（正常 sync 后不应发生）。"""
    row = conn.execute(
        "SELECT entity_id FROM kg_entity_map WHERE node_id = ?", (node_id,)
    ).fetchone()
    return str(row[0]) if row else None


def _terminal(conn: sqlite3.Connection, node_id: str) -> str:
    return resolve_node(conn, node_id) or node_id


def _node_type(conn: sqlite3.Connection, node_id: str) -> str | None:
    row = conn.execute(
        "SELECT node_type FROM kg_nodes WHERE node_id = ?", (node_id,)
    ).fetchone()
    return str(row[0]) if row else None


def _pair_active(conn: sqlite3.Connection, a: str, b: str) -> bool:
    """pair 任一方向已有生效决策（manual/self/linker）→ 不再作为候选。"""
    row = conn.execute(
        "SELECT 1 FROM kg_alias_active WHERE (alias = ? AND canonical = ?) "
        "OR (alias = ? AND canonical = ?) LIMIT 1",
        (a, b, b, a),
    ).fetchone()
    return row is not None


def _pair_rejected(conn: sqlite3.Connection, a: str, b: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM kg_aliases r WHERE r.method = 'rejected' "
        "AND ((r.alias = ? AND r.canonical = ?) OR (r.alias = ? AND r.canonical = ?)) "
        "AND NOT EXISTS (SELECT 1 FROM kg_aliases s "
        "  WHERE s.superseded_by = r.alias || '|' || r.day || '|' || r.run_id) LIMIT 1",
        (a, b, b, a),
    ).fetchone()
    return row is not None


def list_candidates(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """待人工确认的合并候选：exact/fuzzy 行 + 同 label 组 + 共享显式 alias（设计 §5.1）。

    排除：已 active（任一方向生效决策）或已 rejected 的 pair。只产候选，不改 resolved 图。
    """
    out: list[dict[str, Any]] = []

    def _add(keep: str, drop: str, node_type: str, label: str, score: float | None,
             source: str) -> None:
        if keep == drop or _pair_active(conn, keep, drop) or _pair_rejected(conn, keep, drop):
            return
        out.append(
            {
                "keep_id": keep,
                "drop_id": drop,
                "node_type": node_type,
                "label": label,
                "score": score,
                "source": source,
            }
        )

    # ① 账本里的 exact/fuzzy 行（未被撤销）：alias=drop、canonical=keep 就按账本方向
    rows = conn.execute(
        "SELECT a.alias, a.canonical, a.method, a.score, n.node_type, n.label "
        "FROM kg_aliases a JOIN kg_nodes n ON n.node_id = a.alias "
        "WHERE a.method IN ('exact','fuzzy') "
        "AND NOT EXISTS (SELECT 1 FROM kg_aliases s "
        "  WHERE s.superseded_by = a.alias || '|' || a.day || '|' || a.run_id) "
        "ORDER BY a.score DESC"
    ).fetchall()
    for alias, canonical, method, score, node_type, label in rows:
        _add(str(canonical), str(alias), str(node_type), str(label),
             float(score) if score is not None else None, str(method))

    # ② 同 label 同类型的多节点（同名不同人只能候选，绝不自动合并）
    nodes: list[tuple[str, str, str, list[str]]] = []
    for node_id, node_type, label, aliases in conn.execute(
        "SELECT node_id, node_type, label, aliases FROM kg_nodes"
    ).fetchall():
        try:
            alias_list = [str(x) for x in json.loads(aliases or "[]")]
        except (ValueError, TypeError):
            alias_list = []
        nodes.append((str(node_id), str(node_type), str(label), alias_list))
    by_label: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
    for node_id, node_type, label, alias_list in nodes:
        if label:
            by_label.setdefault((node_type, label), []).append((node_id, alias_list))
    for (node_type, label), members in by_label.items():
        ids = sorted(n for n, _ in members)
        for drop in ids[1:]:
            _add(ids[0], drop, node_type, label, 1.0, "same_label")

    # ③ 共享显式 alias：不同节点声明了同一别名（妈妈/母亲 这类写法收敛的证据）
    by_alias: dict[str, list[tuple[str, str, str]]] = {}
    for node_id, node_type, label, alias_list in nodes:
        for a in alias_list:
            by_alias.setdefault(a, []).append((node_id, node_type, label))
    for alias_text, members in by_alias.items():
        ids = sorted({n for n, _, _ in members})
        if len(ids) < 2:
            continue
        node_type, label = members[0][1], members[0][2]
        for drop in ids[1:]:
            _add(ids[0], drop, node_type, label, 0.9, "shared_alias")

    out.sort(key=lambda c: -(c["score"] if c["score"] is not None else 0.0))
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for c in out:
        sig = (c["keep_id"], c["drop_id"])
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(c)
        if len(deduped) >= limit:
            break
    return deduped


def _validate_pair(conn: sqlite3.Connection, keep_id: str, drop_id: str) -> tuple[str, str]:
    """校验 + terminal 解析；返回 (keep_terminal, drop_terminal)。非法抛 MergeConflict。"""
    if keep_id == drop_id:
        raise MergeConflict("same_node")
    keep_type = _node_type(conn, keep_id)
    drop_type = _node_type(conn, drop_id)
    if keep_type is None or drop_type is None:
        raise MergeConflict("node_not_found")
    if keep_type != drop_type:
        raise MergeConflict("type_mismatch")
    kt, dt = _terminal(conn, keep_id), _terminal(conn, drop_id)
    # 环判定：keep 的 terminal 就是 drop 本身 → 写 drop→keep 会闭合成环。
    # （drop 解析到 keep 属"已合并"，由调用方的 kt==dt 幂等分支处理，不算环。）
    if kt == drop_id:
        raise MergeConflict("cycle")
    return kt, dt


def _impact(conn: sqlite3.Connection, keep_terminal: str, drop_terminal: str) -> dict[str, Any]:
    """合并影响统计（纯读）：折叠节点/边/关系数、新增自环、实体数前后、天数跨度、样例 edge。"""
    drop_members = [
        str(r[0])
        for r in conn.execute(
            "SELECT node_id FROM kg_entity_map WHERE entity_id = ?", (drop_terminal,)
        ).fetchall()
    ]
    keep_members = [
        str(r[0])
        for r in conn.execute(
            "SELECT node_id FROM kg_entity_map WHERE entity_id = ?", (keep_terminal,)
        ).fetchall()
    ]
    placeholders = ",".join("?" for _ in drop_members) or "''"
    edge_rows = conn.execute(
        f"SELECT edge_id, from_node, to_node, day FROM kg_edges "
        f"WHERE from_node IN ({placeholders}) OR to_node IN ({placeholders}) "
        f"OR attributor IN ({placeholders})",
        drop_members + drop_members + drop_members,
    ).fetchall()
    relation_rows = conn.execute(
        f"SELECT relation_id FROM kg_relations "
        f"WHERE from_node IN ({placeholders}) OR to_node IN ({placeholders}) "
        f"OR attributor IN ({placeholders})",
        drop_members + drop_members + drop_members,
    ).fetchall()
    keep_set, drop_set = set(keep_members), set(drop_members)
    self_loops = sum(
        1
        for _eid, f, t, _d in edge_rows
        if (f in keep_set and t in drop_set) or (f in drop_set and t in keep_set)
    )
    days = [str(d) for _eid, _f, _t, d in edge_rows if d]
    entities_before = conn.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM kg_entity_map"
    ).fetchone()[0]
    same_entity = keep_terminal == drop_terminal
    return {
        "nodes": len(drop_members),
        "edges": len(edge_rows),
        "relations": len(relation_rows),
        "self_loops": self_loops,
        "days_span": [min(days), max(days)] if days else None,
        "entities_before": int(entities_before),
        "entities_after": int(entities_before) - (0 if same_entity else 1),
        "sample_edge_ids": [str(r[0]) for r in edge_rows[:_SAMPLE_EDGES]],
    }


def preview_merge(conn: sqlite3.Connection, keep_id: str, drop_id: str) -> dict[str, Any]:
    """纯读预览：校验 + 影响统计。校验失败返回 ``{ok: False, error: ...}``，不抛。"""
    try:
        kt, dt = _validate_pair(conn, keep_id, drop_id)
    except MergeConflict as exc:
        return {"ok": False, "error": str(exc), "keep_id": keep_id, "drop_id": drop_id}
    moved = _impact(conn, kt, dt)
    return {
        "ok": True,
        "keep_id": keep_id,
        "drop_id": drop_id,
        "keep_terminal": kt,
        "drop_terminal": dt,
        "already_merged": kt == dt,
        "moved": moved,
    }


def merge_nodes(
    conn: sqlite3.Connection, keep_id: str, drop_id: str, reviewer: str, reason: str
) -> dict[str, Any]:
    """人工合并：单事务写一条 manual 行（alias=drop 的 terminal → canonical=keep 的 terminal）。

    raw 不动；幂等：两者已是同一实体时 ``changed=False``。非法 pair 抛 ``MergeConflict``，事务不写。
    """
    if not reviewer or not reason:
        raise MergeConflict("reviewer_and_reason_required")
    kt, dt = _validate_pair(conn, keep_id, drop_id)
    if kt == dt:
        return {"ok": True, "changed": False, "keep_id": keep_id, "drop_id": drop_id}
    moved = _impact(conn, kt, dt)
    run_id = _manual_run_id()
    with conn:
        conn.execute(
            "INSERT INTO kg_aliases "
            "(alias, canonical, method, score, day, src, run_id, reviewer, reason) "
            "VALUES (?, ?, ?, 1.0, ?, 'entity_merge', ?, ?, ?)",
            (dt, kt, _METHOD_MANUAL, _now_day(), run_id, reviewer, reason),
        )
    _log(
        "merge_nodes",
        node_id=dt,
        canonical=kt,
        moved_nodes=moved["nodes"],
        moved_edges=moved["edges"],
    )
    return {
        "ok": True,
        "changed": True,
        "keep_id": keep_id,
        "drop_id": drop_id,
        "run_id": run_id,
        "moved": moved,
    }


def reject_merge(
    conn: sqlite3.Connection, keep_id: str, drop_id: str, reviewer: str, reason: str
) -> dict[str, Any]:
    """人工拒绝候选：写 rejected 行抑制重复提案；不改 resolved 图。幂等。"""
    if not reviewer or not reason:
        raise MergeConflict("reviewer_and_reason_required")
    if _node_type(conn, keep_id) is None or _node_type(conn, drop_id) is None:
        raise MergeConflict("node_not_found")
    if _pair_rejected(conn, keep_id, drop_id):
        return {"ok": True, "changed": False, "keep_id": keep_id, "drop_id": drop_id}
    score_row = conn.execute(
        "SELECT score FROM kg_aliases WHERE (alias = ? AND canonical = ?) "
        "OR (alias = ? AND canonical = ?) ORDER BY score DESC LIMIT 1",
        (drop_id, keep_id, keep_id, drop_id),
    ).fetchone()
    with conn:
        conn.execute(
            "INSERT INTO kg_aliases "
            "(alias, canonical, method, score, day, src, run_id, reviewer, reason) "
            "VALUES (?, ?, ?, ?, ?, 'entity_merge', ?, ?, ?)",
            (
                drop_id,
                keep_id,
                _METHOD_REJECTED,
                float(score_row[0]) if score_row and score_row[0] is not None else None,
                _now_day(),
                _manual_run_id(),
                reviewer,
                reason,
            ),
        )
    _log("reject_merge", node_id=drop_id, canonical=keep_id)
    return {"ok": True, "changed": True, "keep_id": keep_id, "drop_id": drop_id}


def revoke_merge(
    conn: sqlite3.Connection, drop_id: str, reviewer: str, reason: str
) -> dict[str, Any]:
    """撤销 drop 当前生效的 manual 合并：追加自指行 + superseded_by 反向指针（旧行不改）。

    链式恢复：A→B→C 撤销 B 的合并后，A 解析回 B。无生效 manual 行时 ``changed=False``。
    """
    if not reviewer or not reason:
        raise MergeConflict("reviewer_and_reason_required")
    row = conn.execute(
        "SELECT day, run_id, canonical FROM kg_aliases a "
        "WHERE a.alias = ? AND a.method = 'manual' AND a.alias <> a.canonical "
        "AND NOT EXISTS (SELECT 1 FROM kg_aliases b "
        "  WHERE b.superseded_by = a.alias || '|' || a.day || '|' || a.run_id) "
        "ORDER BY a.day DESC, a.run_id DESC LIMIT 1",
        (drop_id,),
    ).fetchone()
    if not row:
        return {"ok": True, "changed": False, "drop_id": drop_id}
    old_day, old_run_id, old_canonical = str(row[0]), str(row[1]), str(row[2])
    with conn:
        conn.execute(
            "INSERT INTO kg_aliases "
            "(alias, canonical, method, score, day, src, superseded_by, run_id, reviewer, reason) "
            "VALUES (?, ?, ?, 1.0, ?, 'entity_merge', ?, ?, ?, ?)",
            (
                drop_id,
                drop_id,
                _METHOD_MANUAL,
                _now_day(),
                alias_key(drop_id, old_day, old_run_id),
                _manual_run_id(),
                reviewer,
                reason,
            ),
        )
    _log("revoke_merge", node_id=drop_id, canonical=old_canonical)
    return {"ok": True, "changed": True, "drop_id": drop_id, "restored_from": old_canonical}


def _open_db(db_path: str | None) -> sqlite3.Connection:
    from gacore.config import Config

    path = db_path or str(Config.default().root / "data" / "langTrack.db")
    conn = sqlite3.connect(path)
    migrate_extract_tables(conn)  # 幂等：补 reviewer/reason 列 + 视图
    return conn


def main(argv: list[str] | None = None) -> int:
    """CLI（设计 §5.3）：--list / --preview / --merge / --reject / --revoke。"""
    import argparse

    parser = argparse.ArgumentParser(description="KG entity merge service")
    parser.add_argument("--db", default=None, help="sqlite path (default: data/langTrack.db)")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list merge candidates")
    g.add_argument("--preview", nargs=2, metavar=("KEEP", "DROP"))
    g.add_argument("--merge", nargs=2, metavar=("KEEP", "DROP"))
    g.add_argument("--reject", nargs=2, metavar=("KEEP", "DROP"))
    g.add_argument("--revoke", metavar="DROP")
    parser.add_argument("--reviewer", default="user")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    conn = _open_db(args.db)
    try:
        if args.list:
            print(json.dumps(list_candidates(conn), ensure_ascii=False, indent=2))
            return 0
        if args.preview:
            print(json.dumps(preview_merge(conn, args.preview[0], args.preview[1]),
                             ensure_ascii=False, indent=2))
            return 0
        if not args.reason:
            print("--reason is required for write operations", file=sys.stderr)
            return 2
        if args.merge:
            result = merge_nodes(conn, args.merge[0], args.merge[1], args.reviewer, args.reason)
        elif args.reject:
            result = reject_merge(conn, args.reject[0], args.reject[1], args.reviewer, args.reason)
        else:
            result = revoke_merge(conn, args.revoke, args.reviewer, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except MergeConflict as exc:
        print(f"MergeConflict: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
