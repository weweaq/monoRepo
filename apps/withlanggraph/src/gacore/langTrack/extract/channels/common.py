"""候选类通道（emotions / patterns / worldview）的共享落库原语。

三通道形状一致：``payload = {"nodes": [...], "<records>": [...]}``，每条记录都带
``subject``（引用某个 node_id）与 ``confidence``。故把"文本/数值清洗、节点补占位与插入、
内容派生主键"抽到这里，避免三份复制。

events / relations / todos 早于本模块，各自保留原实现——本模块只服务 S3 新增三通道，
不改动存量通道（零回归）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Final

from gacore.langTrack.extract.types import ExtractContext, infer_node_type

INSERT_NODE: Final = """
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


def text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def clamp_score(value: Any, *, ceiling: float | None = None) -> float | None:
    """数值钳制到 0..1；非数值返回 None；给了 ``ceiling`` 再取上限（L3 0.6 / 候选 0.7 等）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = max(0.0, min(1.0, float(value)))
    return min(score, ceiling) if ceiling is not None else score


def clamp_range(value: Any, low: float, high: float) -> float | None:
    """数值钳制到 [low, high]（如 valence -1..1）；非数值返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(low, min(high, float(value)))


def derived_id(*parts: str) -> str:
    """内容派生的确定性主键：同素材重跑产出同一 id，撞 PK 即静默忽略。"""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def placeholder_node(node_id: str, ctx: ExtractContext, src: str) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": infer_node_type(node_id),
        "label": node_id,
        "canonical": None,
        "aliases": "[]",
        "confidence": None,
        "day": ctx.day,
        "src": src,
        "run_id": ctx.run_id,
    }


def collect_nodes(payload: dict[str, Any], ctx: ExtractContext, src: str) -> dict[str, dict[str, Any]]:
    """收 payload.nodes；node_type/label 缺失时按 node_id 前缀与 node_id 回落。"""
    nodes: dict[str, dict[str, Any]] = {}
    for raw in payload.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        node_id = text(raw.get("node_id")).strip()
        if not node_id:
            continue
        aliases = raw.get("aliases")
        nodes[node_id] = {
            "node_id": node_id,
            "node_type": text(raw.get("node_type")).strip() or infer_node_type(node_id),
            "label": text(raw.get("label")).strip() or node_id,
            "canonical": optional_text(raw.get("canonical")),
            "aliases": json.dumps(
                [a for a in aliases if isinstance(a, str)] if isinstance(aliases, list) else [],
                ensure_ascii=False,
            ),
            "confidence": clamp_score(raw.get("confidence")),
            "day": ctx.day,
            "src": src,
            "run_id": ctx.run_id,
        }
    return nodes


def ensure_node(nodes: dict[str, dict[str, Any]], node_id: str, ctx: ExtractContext, src: str) -> None:
    """保证 subject/引用节点存在（缺失则补占位），避免悬空引用。"""
    nodes.setdefault(node_id, placeholder_node(node_id, ctx, src))


def insert_nodes(conn: sqlite3.Connection, nodes: dict[str, dict[str, Any]]) -> int:
    """插入节点并顺带扩展 first_day/last_day；须在调用方事务内执行。返回新插入行数。"""
    written = 0
    for node in nodes.values():
        written += conn.execute(INSERT_NODE, node).rowcount
        conn.execute(_WIDEN_NODE, {"node_id": node["node_id"], "day": node["day"]})
        conn.execute(_EXTEND_NODE, {"node_id": node["node_id"], "day": node["day"]})
    return written
