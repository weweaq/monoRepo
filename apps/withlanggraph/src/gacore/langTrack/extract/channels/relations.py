"""relations 通道：把当日素材抽成"实体间稳定关系"（人际/协作/长期关联）。

素材路由（§3.2）：_CHAT / _PHONE_NOTIF / _LONG_TERM——关系证据主要来自对话与人际上下文。

与 events 通道的区别：events 记"当天发生了什么"（事件边，落 ``kg_edges``，参与 interval closure）；
relations 记"谁与谁是什么关系"（稳定关联，落 ``kg_relations``，不带 valid_to 闭合语义）。

落库不变量（§9，与 events 同档）：
- 无悬空边：关系的 from/to 若不在 payload.nodes 里，按 node_id 前缀补占位节点；
- L2 关系必须有 attributor，缺失即丢弃并计数；
- L3 关系 strength 上限 0.6，超出即钳制。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Final

from gacore.langTrack.extract.types import (
    ChannelSpec,
    ExtractContext,
    infer_node_type,
)

KEY: Final = "relations"
PROMPT: Final = "relations@v1.md"
SOURCES: Final[tuple[str, ...]] = ("_CHAT", "_PHONE_NOTIF", "_LONG_TERM")

_EDGE_LEVELS: Final = ("L1", "L2", "L3")
_L3_MAX_STRENGTH: Final = 0.6

_INSERT_NODE: Final = """
INSERT OR IGNORE INTO kg_nodes (
  node_id, node_type, label, canonical, aliases, first_day, last_day, src, confidence, run_id
) VALUES (
  :node_id, :node_type, :label, :canonical, :aliases, :day, :day, :src, :confidence, :run_id
)
"""

_WIDEN_NODE: Final = """
UPDATE kg_nodes
SET first_day = :day, updated_at = datetime('now', '+8 hours')
WHERE node_id = :node_id AND first_day > :day
"""

_EXTEND_NODE: Final = """
UPDATE kg_nodes
SET last_day = :day, updated_at = datetime('now', '+8 hours')
WHERE node_id = :node_id AND last_day < :day
"""

_INSERT_RELATION: Final = """
INSERT OR IGNORE INTO kg_relations (
  relation_id, from_node, to_node, relation, edge_level, attributor, assertion_text,
  evidence, strength, valid_from, valid_to, day, src, run_id
) VALUES (
  :relation_id, :from_node, :to_node, :relation, :edge_level, :attributor, :assertion_text,
  :evidence, :strength, :valid_from, NULL, :day, :src, :run_id
)
"""


def relation_id(day: str, from_node: str, to_node: str, relation: str) -> str:
    """内容派生的确定性主键：同素材重跑产出同一 relation_id，撞 PK 即静默忽略。"""
    raw = f"{day}|{from_node}|{to_node}|{relation}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _strength(value: Any, *, ceiling: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = max(0.0, min(1.0, float(value)))
    return min(score, ceiling) if ceiling is not None else score


def _collect_nodes(payload: dict[str, Any], ctx: ExtractContext) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for raw in payload.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        node_id = _text(raw.get("node_id")).strip()
        if not node_id:
            continue
        aliases = raw.get("aliases")
        nodes[node_id] = {
            "node_id": node_id,
            "node_type": _text(raw.get("node_type")).strip() or infer_node_type(node_id),
            "label": _text(raw.get("label")).strip() or node_id,
            "canonical": _optional_text(raw.get("canonical")),
            "aliases": json.dumps(
                [a for a in aliases if isinstance(a, str)] if isinstance(aliases, list) else [],
                ensure_ascii=False,
            ),
            "confidence": _strength(raw.get("confidence")),
            "day": ctx.day,
            "src": KEY,
            "run_id": ctx.run_id,
        }
    return nodes


def _relation_rows(
    payload: dict[str, Any], ctx: ExtractContext, nodes: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    stats = {"dropped_l2_no_attributor": 0, "clamped_l3": 0, "dropped_malformed": 0}
    seen: set[str] = set()
    for raw in payload.get("relations") or []:
        if not isinstance(raw, dict):
            stats["dropped_malformed"] += 1
            continue
        from_node = _text(raw.get("from")).strip()
        to_node = _text(raw.get("to")).strip()
        relation = _text(raw.get("relation")).strip()
        level = _text(raw.get("edge_level")).strip().upper()
        if not (from_node and to_node and relation and level in _EDGE_LEVELS):
            stats["dropped_malformed"] += 1
            continue
        attributor = _optional_text(raw.get("attributor"))
        if level == "L2" and not attributor:
            stats["dropped_l2_no_attributor"] += 1
            continue
        ceiling = _L3_MAX_STRENGTH if level == "L3" else None
        raw_strength = raw.get("strength")
        strength = _strength(raw_strength, ceiling=ceiling)
        if (
            ceiling is not None
            and isinstance(raw_strength, (int, float))
            and not isinstance(raw_strength, bool)
            and float(raw_strength) > ceiling
        ):
            stats["clamped_l3"] += 1
        rid = relation_id(ctx.day, from_node, to_node, relation)
        if rid in seen:
            continue
        seen.add(rid)
        for endpoint in (from_node, to_node):
            nodes.setdefault(
                endpoint,
                {
                    "node_id": endpoint,
                    "node_type": infer_node_type(endpoint),
                    "label": endpoint,
                    "canonical": None,
                    "aliases": "[]",
                    "confidence": None,
                    "day": ctx.day,
                    "src": KEY,
                    "run_id": ctx.run_id,
                },
            )
        rows.append({
            "relation_id": rid,
            "from_node": from_node,
            "to_node": to_node,
            "relation": relation,
            "edge_level": level,
            "attributor": attributor,
            "assertion_text": _optional_text(raw.get("assertion_text")),
            "evidence": _optional_text(raw.get("evidence")),
            "strength": strength,
            "valid_from": _optional_text(raw.get("valid_from")),
            "day": ctx.day,
            "src": KEY,
            "run_id": ctx.run_id,
        })
    return rows, stats


def handle(conn: sqlite3.Connection, ctx: ExtractContext, payload: Any) -> tuple[int, dict[str, Any]]:
    """单事务落库 relations 通道产物；返回 (新写入行数=节点+关系, 统计)。"""
    if not isinstance(payload, dict):
        raise ValueError("relations payload must be a JSON object")
    nodes = _collect_nodes(payload, ctx)
    rows, stats = _relation_rows(payload, ctx, nodes)
    written = 0
    with conn:
        for node in nodes.values():
            written += conn.execute(_INSERT_NODE, node).rowcount
            conn.execute(_WIDEN_NODE, {"node_id": node["node_id"], "day": ctx.day})
            conn.execute(_EXTEND_NODE, {"node_id": node["node_id"], "day": ctx.day})
        for row in rows:
            written += conn.execute(_INSERT_RELATION, row).rowcount
    return written, {"nodes": len(nodes), "relations": len(rows), **stats}


SPEC: Final = ChannelSpec(key=KEY, prompt=PROMPT, sources=SOURCES, handle=handle)
