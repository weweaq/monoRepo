"""events 通道：把当日素材抽成"事件 + 参与实体"的节点与边。

素材路由（§3.2）：_CHAT/_MEDIA/_BILI/_PHONE_NOTIF/_PHONE_PLACE/_EDGE/_FILES/_GIT/_LONG_TERM
——事件散布在所有素材里，故取全部源。

落库不变量（§9）：
- 无悬空边：边的 from/to 若不在 payload.nodes 里，按 node_id 前缀补占位节点；
- L2 边必须有 attributor，缺失即丢弃并计数（宁可少一条，不可留一条无归属的"观点"）；
- L3 边 confidence 上限 0.6，超出即钳制（LLM 越权把推断说得很确定时收敛回推断档）。
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

KEY: Final = "events"
PROMPT: Final = "events@v1.md"
SOURCES: Final[tuple[str, ...]] = (
    "_CHAT",
    "_MEDIA",
    "_BILI",
    "_PHONE_NOTIF",
    "_PHONE_PLACE",
    "_EDGE",
    "_FILES",
    "_GIT",
    "_LONG_TERM",
)

_EDGE_LEVELS: Final = ("L1", "L2", "L3")
_L3_MAX_CONFIDENCE: Final = 0.6

_INSERT_NODE: Final = """
INSERT OR IGNORE INTO kg_nodes (
  node_id, node_type, label, canonical, aliases, first_day, last_day, src, confidence, run_id
) VALUES (
  :node_id, :node_type, :label, :canonical, :aliases, :day, :day, :src, :confidence, :run_id
)
"""

# 补跑历史日会带来比现有 first_day 更早、比 last_day 更晚的记录，两个方向都要收敛。
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

_INSERT_EDGE: Final = """
INSERT OR IGNORE INTO kg_edges (
  edge_id, from_node, to_node, relation, edge_level, attributor, assertion_text,
  event_time, ingest_time, valid_from, valid_to, confidence, day, src, run_id
) VALUES (
  :edge_id, :from_node, :to_node, :relation, :edge_level, :attributor, :assertion_text,
  :event_time, datetime('now', '+8 hours'), :valid_from, NULL, :confidence, :day, :src, :run_id
)
"""


def edge_id(day: str, from_node: str, to_node: str, relation: str, edge_level: str, assertion: str) -> str:
    """内容派生的确定性主键：同素材重跑产出同一 edge_id，撞 PK 即静默忽略。"""
    raw = f"{day}|{from_node}|{to_node}|{relation}|{edge_level}|{assertion}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _confidence(value: Any, *, ceiling: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = max(0.0, min(1.0, float(value)))
    return min(score, ceiling) if ceiling is not None else score


def _collect_nodes(payload: dict[str, Any], ctx: ExtractContext) -> dict[str, dict[str, Any]]:
    """抽出 payload.nodes 里合法的节点（node_id 非空即收）。"""
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
            "confidence": _confidence(raw.get("confidence")),
            "day": ctx.day,
            "src": KEY,
            "run_id": ctx.run_id,
        }
    return nodes


def _edge_rows(
    payload: dict[str, Any], ctx: ExtractContext, nodes: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """把 payload.edges 规范成可落库的行；返回 (行, 计数统计)。"""
    rows: list[dict[str, Any]] = []
    stats = {"dropped_l2_no_attributor": 0, "clamped_l3": 0, "dropped_malformed": 0}
    seen: set[str] = set()
    for raw in payload.get("edges") or []:
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
        ceiling = _L3_MAX_CONFIDENCE if level == "L3" else None
        raw_conf = raw.get("confidence")
        confidence = _confidence(raw_conf, ceiling=ceiling)
        if (
            ceiling is not None
            and isinstance(raw_conf, (int, float))
            and not isinstance(raw_conf, bool)
            and float(raw_conf) > ceiling
        ):
            stats["clamped_l3"] += 1
        assertion = _text(raw.get("assertion_text")).strip()
        eid = edge_id(ctx.day, from_node, to_node, relation, level, assertion)
        if eid in seen:
            continue
        seen.add(eid)
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
            "edge_id": eid,
            "from_node": from_node,
            "to_node": to_node,
            "relation": relation,
            "edge_level": level,
            "attributor": attributor,
            "assertion_text": assertion or None,
            "event_time": _optional_text(raw.get("event_time")),
            "valid_from": _optional_text(raw.get("valid_from")) or _optional_text(raw.get("event_time")),
            "confidence": confidence,
            "day": ctx.day,
            "src": KEY,
            "run_id": ctx.run_id,
        })
    return rows, stats


def handle(conn: sqlite3.Connection, ctx: ExtractContext, payload: Any) -> tuple[int, dict[str, Any]]:
    """单事务落库 events 通道产物；返回 (新写入行数=节点+边, 统计)。"""
    if not isinstance(payload, dict):
        raise ValueError("events payload must be a JSON object")
    nodes = _collect_nodes(payload, ctx)
    rows, stats = _edge_rows(payload, ctx, nodes)
    written = 0
    with conn:
        for node in nodes.values():
            written += conn.execute(_INSERT_NODE, node).rowcount
            conn.execute(_WIDEN_NODE, {"node_id": node["node_id"], "day": ctx.day})
            conn.execute(_EXTEND_NODE, {"node_id": node["node_id"], "day": ctx.day})
        for row in rows:
            written += conn.execute(_INSERT_EDGE, row).rowcount
    return written, {"nodes": len(nodes), "edges": len(rows), **stats}


SPEC: Final = ChannelSpec(key=KEY, prompt=PROMPT, sources=SOURCES, handle=handle)
