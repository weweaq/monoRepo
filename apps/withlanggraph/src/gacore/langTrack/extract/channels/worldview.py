"""worldview 通道：把当日素材抽成"世界观/信念候选"。

素材路由（§3.2）：_LONG_TERM / _CHAT——世界观察来自长期画像与对话。

落库不变量（§5 S3 要点）：
- **只产候选**：confidence 上限 0.6，超出即钳制并计数；``status`` 恒为 ``candidate``，
  不宣布规律（候选→规律升格机制属后续独立阶段，见 §12）；
- subject 必填（缺失丢弃），按需补占位节点；statement 陈述必填；
- view_id 内容派生 → 重跑幂等。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from gacore.langTrack.extract.channels.common import (
    clamp_score,
    collect_nodes,
    derived_id,
    ensure_node,
    insert_nodes,
    optional_text,
    text,
)
from gacore.langTrack.extract.types import ChannelSpec, ExtractContext

KEY: Final = "worldview"
PROMPT: Final = "worldview@v1.md"
SOURCES: Final[tuple[str, ...]] = ("_LONG_TERM", "_CHAT")

_CONFIDENCE_CEILING: Final = 0.6

_INSERT: Final = """
INSERT OR IGNORE INTO kg_worldview (
  view_id, subject, statement, domain, evidence, confidence, status, day, src, run_id
) VALUES (
  :view_id, :subject, :statement, :domain, :evidence, :confidence, :status, :day, :src, :run_id
)
"""


def _rows(
    payload: dict[str, Any], ctx: ExtractContext, nodes: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    stats = {"dropped_malformed": 0, "clamped_confidence": 0}
    seen: set[str] = set()
    for raw in payload.get("worldview") or []:
        if not isinstance(raw, dict):
            stats["dropped_malformed"] += 1
            continue
        subject = text(raw.get("subject")).strip()
        statement = text(raw.get("statement")).strip()
        if not (subject and statement):
            stats["dropped_malformed"] += 1
            continue
        vid = derived_id(ctx.day, subject, statement)
        if vid in seen:
            continue
        seen.add(vid)
        raw_conf = raw.get("confidence")
        confidence = clamp_score(raw_conf, ceiling=_CONFIDENCE_CEILING)
        if (
            isinstance(raw_conf, (int, float))
            and not isinstance(raw_conf, bool)
            and float(raw_conf) > _CONFIDENCE_CEILING
        ):
            stats["clamped_confidence"] += 1
        ensure_node(nodes, subject, ctx, KEY)
        rows.append({
            "view_id": vid,
            "subject": subject,
            "statement": statement,
            "domain": optional_text(raw.get("domain")),
            "evidence": optional_text(raw.get("evidence")),
            "confidence": confidence if confidence is not None else 0.0,
            "status": "candidate",
            "day": ctx.day,
            "src": KEY,
            "run_id": ctx.run_id,
        })
    return rows, stats


def handle(conn: sqlite3.Connection, ctx: ExtractContext, payload: Any) -> tuple[int, dict[str, Any]]:
    """单事务落库 worldview 通道产物；返回 (新写入行数=节点+候选, 统计)。"""
    if not isinstance(payload, dict):
        raise ValueError("worldview payload must be a JSON object")
    nodes = collect_nodes(payload, ctx, KEY)
    rows, stats = _rows(payload, ctx, nodes)
    written = 0
    with conn:
        written += insert_nodes(conn, nodes)
        for row in rows:
            written += conn.execute(_INSERT, row).rowcount
    return written, {"nodes": len(nodes), "worldview": len(rows), **stats}


SPEC: Final = ChannelSpec(key=KEY, prompt=PROMPT, sources=SOURCES, handle=handle)
