"""emotions 通道：把当日素材抽成"情绪信号"（谁表现出什么情绪）。

素材路由（§3.2）：_CHAT / _PHONE_NOTIF / _MEDIA——情绪信号来自表达与消费内容。

落库不变量：
- subject 必填（缺失即丢弃并计数），并按需补占位节点，避免悬空引用；
- emotion 标签必填；valence/arousal/intensity 各自钳制到值域；confidence 钳制 0..1；
- emotion_id 内容派生 → 重跑幂等。

情绪是日粒度事实，不参与 interval closure。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from gacore.langTrack.extract.channels.common import (
    clamp_range,
    clamp_score,
    collect_nodes,
    derived_id,
    ensure_node,
    insert_nodes,
    optional_text,
    text,
)
from gacore.langTrack.extract.types import ChannelSpec, ExtractContext

KEY: Final = "emotions"
PROMPT: Final = "emotions@v1.md"
SOURCES: Final[tuple[str, ...]] = ("_CHAT", "_PHONE_NOTIF", "_MEDIA")

_INSERT: Final = """
INSERT OR IGNORE INTO kg_emotions (
  emotion_id, subject, emotion, valence, arousal, intensity, trigger_text,
  evidence, confidence, day, src, run_id
) VALUES (
  :emotion_id, :subject, :emotion, :valence, :arousal, :intensity, :trigger_text,
  :evidence, :confidence, :day, :src, :run_id
)
"""


def _rows(
    payload: dict[str, Any], ctx: ExtractContext, nodes: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    stats = {"dropped_malformed": 0}
    seen: set[str] = set()
    for raw in payload.get("emotions") or []:
        if not isinstance(raw, dict):
            stats["dropped_malformed"] += 1
            continue
        subject = text(raw.get("subject")).strip()
        emotion = text(raw.get("emotion")).strip()
        if not (subject and emotion):
            stats["dropped_malformed"] += 1
            continue
        evidence = optional_text(raw.get("evidence"))
        eid = derived_id(ctx.day, subject, emotion, evidence or "")
        if eid in seen:
            continue
        seen.add(eid)
        ensure_node(nodes, subject, ctx, KEY)
        rows.append({
            "emotion_id": eid,
            "subject": subject,
            "emotion": emotion,
            "valence": clamp_range(raw.get("valence"), -1.0, 1.0),
            "arousal": clamp_score(raw.get("arousal")),
            "intensity": clamp_score(raw.get("intensity")),
            "trigger_text": optional_text(raw.get("trigger_text")),
            "evidence": evidence,
            "confidence": clamp_score(raw.get("confidence")),
            "day": ctx.day,
            "src": KEY,
            "run_id": ctx.run_id,
        })
    return rows, stats


def handle(conn: sqlite3.Connection, ctx: ExtractContext, payload: Any) -> tuple[int, dict[str, Any]]:
    """单事务落库 emotions 通道产物；返回 (新写入行数=节点+情绪, 统计)。"""
    if not isinstance(payload, dict):
        raise ValueError("emotions payload must be a JSON object")
    nodes = collect_nodes(payload, ctx, KEY)
    rows, stats = _rows(payload, ctx, nodes)
    written = 0
    with conn:
        written += insert_nodes(conn, nodes)
        for row in rows:
            written += conn.execute(_INSERT, row).rowcount
    return written, {"nodes": len(nodes), "emotions": len(rows), **stats}


SPEC: Final = ChannelSpec(key=KEY, prompt=PROMPT, sources=SOURCES, handle=handle)
