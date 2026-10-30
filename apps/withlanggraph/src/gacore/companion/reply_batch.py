"""Companion reply batching: split one final answer into 1-3 chat-sized messages.

Only owner C2C companion turns are planned. Every other turn (operator, group chat,
non-owner, command, trivial reply, tool trace) stays atomic. Splitting is deliberately
conservative: blank-line paragraphs suggested by the model are the primary boundary,
sentence boundaries are a guarded fallback, and anything that looks technical stays in
one message.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final, Literal

REPLY_BATCH_VERSION: Final = "companion-reply-batch@v1"
MAX_REPLY_SEGMENTS: Final = 3
MAX_SEGMENT_CHARS: Final = 200
MAX_CHAT_CHARS: Final = 400

ReplyMode = Literal["companion", "atomic"]

# Structures that must never be cut for "human rhythm": fenced code, tables, stack
# traces, paths/commands, report headings, bullet lists, and 【方案N】 proposals.
_FENCED_CODE: Final = re.compile(r"```")
_TABLE_ROW: Final = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_STACK_TRACE: Final = re.compile(
    r"Traceback \(most recent call last\)|^\s*File \".+\", line \d+", re.MULTILINE
)
_PATH: Final = re.compile(
    r"(?:[A-Za-z]:\\|(?:^|\s)(?:~|\.{1,2})?/(?:[\w.-]+/)+|(?:^|\s)/(?:usr|etc|var|home|opt|tmp)/)",
    re.MULTILINE,
)
_COMMAND_LINE: Final = re.compile(r"^\s*(?:\$|PS>|>)\s+\S", re.MULTILINE)
_LIST_LINE: Final = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+\S", re.MULTILINE)
_HEADING: Final = re.compile(r"^\s*#{1,6}\s+\S", re.MULTILINE)
_PROPOSAL: Final = re.compile(r"【方案[0-9一二三四五六七八九十]+】")

_SENTENCE: Final = re.compile(r"[^。！？!?]+[。！？!?]+|[^。！？!?]+$")
_TERMINALS: Final = "。！？!?"


@dataclass(frozen=True)
class ReplyBatch:
    """One logical reply split into the segments that will actually be sent."""

    logical_turn_id: str
    segments: tuple[str, ...]


def plan_final_reply(text: str, mode: ReplyMode | str = "companion") -> ReplyBatch:
    """Plan the chat segments for one final answer.

    ``mode="companion"`` may split into 1-3 messages; any other mode is atomic. An
    empty answer yields no segments, so the caller sends nothing.
    """
    cleaned = (text or "").strip()
    turn_id = uuid.uuid4().hex
    if not cleaned:
        return ReplyBatch(logical_turn_id=turn_id, segments=())
    if mode != "companion" or _looks_technical(cleaned) or len(cleaned) > MAX_CHAT_CHARS:
        return ReplyBatch(logical_turn_id=turn_id, segments=(cleaned,))
    paragraphs = _paragraphs(cleaned)
    if len(paragraphs) > 1:
        if (
            len(paragraphs) <= MAX_REPLY_SEGMENTS
            and all(len(p) <= MAX_SEGMENT_CHARS for p in paragraphs)
        ):
            return ReplyBatch(logical_turn_id=turn_id, segments=tuple(paragraphs))
        # Too many blocks or one oversized block: do not force a split.
        return ReplyBatch(logical_turn_id=turn_id, segments=(cleaned,))
    sentences = _sentences(cleaned)
    if (
        2 <= len(sentences) <= MAX_REPLY_SEGMENTS
        and all(s[-1] in _TERMINALS for s in sentences)
        and all(4 <= len(s) <= MAX_SEGMENT_CHARS for s in sentences)
        and sum(len(s) for s in sentences) <= MAX_CHAT_CHARS
    ):
        return ReplyBatch(logical_turn_id=turn_id, segments=tuple(sentences))
    return ReplyBatch(logical_turn_id=turn_id, segments=(cleaned,))


def _looks_technical(text: str) -> bool:
    if (
        _FENCED_CODE.search(text)
        or _TABLE_ROW.search(text)
        or _STACK_TRACE.search(text)
        or _PATH.search(text)
        or _COMMAND_LINE.search(text)
        or _HEADING.search(text)
        or _PROPOSAL.search(text)
    ):
        return True
    return len(_LIST_LINE.findall(text)) >= 3


def _paragraphs(text: str) -> list[str]:
    blocks = (block.strip() for block in re.split(r"\n\s*\n", text))
    return [block for block in blocks if block]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.findall(text) if part.strip()]
