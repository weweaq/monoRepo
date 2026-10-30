"""Commitment extraction from confirmed character sends (V2-Step 4, migration v5).

Design: ``docs/companion-self-memory-v2-plan.md`` §4.4 / §5.2 / V2-Step 4.

The character may only be credited with a promise it really made, so extraction runs on
``agent.message.sent`` / ``proactive.message.sent`` events and the returned ``quote`` must
be an exact substring of that send. Deadlines are parsed by code from the quote — the
model never supplies a date. Confidence decides the initial state:

    >= 0.85  → active
    0.60–0.85 → proposed
    < 0.60   → not stored

A cheap deterministic pre-filter keeps the model out of ordinary replies: only sends that
contain a future-action marker are even considered.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from langchain_core.messages import HumanMessage, SystemMessage

from gacore.companion.store import Commitment, CompanionStore, CompanionStoreError
from gacore.jsonl_logger import get_logger
from gacore.redact import redact_text

logger = get_logger("companion.commitment")

COMMITMENT_PROMPT_VERSION: Final = "companion-commitment@v1"
AUTO_ACTIVE_CONFIDENCE: Final = 0.85
MIN_CONFIDENCE: Final = 0.60
MAX_ACTION_CHARS: Final = 200
MAX_CRITERIA_CHARS: Final = 200
OVERDUE_GRACE_MS: Final = 24 * 60 * 60 * 1000
_SENT_KINDS: Final = frozenset({"agent.message.sent", "proactive.message.sent"})
_TZ: Final = timezone(timedelta(hours=8))
_HOUR_MS: Final = 60 * 60 * 1000
_DAY_MS: Final = 24 * _HOUR_MS

# Cheap gate: without a future-action marker the send is not even sent to the extractor.
PROMISE_MARKERS: Final = (
    "明天",
    "后天",
    "今晚",
    "周末",
    "下周",
    "一会儿",
    "稍后",
    "等会",
    "马上",
    "回头",
    "改天",
    "过两天",
)

_JSON_OBJECT: Final = re.compile(r"\{.*\}", re.S)
_DATE: Final = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DAYS_LATER: Final = re.compile(r"([一二三四五六七八九十\d]+)\s*天后")
_CN_NUMBERS: Final = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_SYSTEM_PROMPT: Final = """判断角色是否在已确认发送文本中明确答应未来完成一个可验证动作。

不算承诺：
- 客套话、愿望、建议；
- “我看看”“以后再说”；
- 用户要求但角色没有明确接受；
- 已经完成的事情。

只输出 JSON，键名一字不差：
{"is_commitment": true, "quote": "输入文本的精确子串", "action": "要做的动作",
 "due_text": "原文里的时间词，如 明天", "success_criteria": "怎样算完成", "confidence": 0.0}

规则：
- quote 必须是输入文本里原样出现的片段，不得改写；
- due_text 只摘原文里的时间词，不要自己算日期；
- confidence 是你对"这确实是一个承诺"的把握，0 到 1。"""

Runner = Callable[[str], "CommitmentOutcome"]


@dataclass(frozen=True, slots=True)
class CommitmentOutcome:
    """Extraction result; ``ok=False`` carries a short reason instead of a promise."""

    ok: bool
    quote: str = ""
    action: str = ""
    success_criteria: str = ""
    confidence: float = 0.0
    error: str | None = None


def looks_like_promise(text: str) -> bool:
    """Deterministic pre-filter: does this send even mention a future action?"""
    return any(marker in (text or "") for marker in PROMISE_MARKERS)


def extract_commitment(
    text: str,
    *,
    model: object | None = None,
    runner: Runner | None = None,
) -> CommitmentOutcome:
    """Ask the extractor whether ``text`` makes a promise (no date parsing here)."""
    if not looks_like_promise(text):
        return CommitmentOutcome(ok=False, error="no promise marker")
    if runner is not None:
        return runner(text)
    try:
        resolved = model if model is not None else _default_model()
        response = resolved.invoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=text)]
        )
        raw = getattr(response, "content", response)
        payload = raw if isinstance(raw, str) else str(raw)
    except Exception as exc:  # noqa: BLE001 — extraction failure is never a crash
        logger.warning(
            "companion commitment model call failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return CommitmentOutcome(ok=False, error=f"model call failed: {type(exc).__name__}")

    match = _JSON_OBJECT.search(payload or "")
    if not match:
        return CommitmentOutcome(ok=False, error="response is not JSON")
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return CommitmentOutcome(ok=False, error="response JSON is malformed")
    return _validate(data, text)


def record_commitment(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    event_id: str,
    kind: str,
    summary: str,
    occurred_at_ms: int,
    model: object | None = None,
    runner: Runner | None = None,
    now_ms: int | None = None,
) -> Commitment | None:
    """Extract and persist one commitment from a confirmed send; never raises to chat.

    Returns the stored commitment (``None`` when the send made no promise or the
    extraction failed). A repeated call for the same send is a no-op thanks to the
    deterministic ``(source_event_id, quote)`` key.
    """
    if kind not in _SENT_KINDS:
        return None
    if not looks_like_promise(summary):
        return None
    try:
        outcome = extract_commitment(summary, model=model, runner=runner)
    except Exception:  # noqa: BLE001 — a promise scan must never break the caller
        logger.warning("companion commitment extraction crashed", stack_trace=traceback.format_exc())
        return None
    if not outcome.ok:
        if outcome.error not in (None, "no promise marker", "not a commitment"):
            logger.info("companion commitment skipped", reason=outcome.error)
        return None
    if outcome.quote not in summary:
        logger.warning("companion commitment quote not in send", quote=outcome.quote[:40])
        return None
    if outcome.confidence < MIN_CONFIDENCE:
        return None
    status = "active" if outcome.confidence >= AUTO_ACTIVE_CONFIDENCE else "proposed"
    due_at_ms = parse_due_ms(outcome.quote, occurred_at_ms)
    try:
        commitment, created = store.add_commitment(
            actor_id=actor_id,
            principal_id=principal_id,
            source_event_id=event_id,
            quote=outcome.quote,
            action=outcome.action,
            due_at_ms=due_at_ms,
            success_criteria=outcome.success_criteria,
            status=status,
        )
    except (CompanionStoreError, ValueError) as exc:
        logger.warning("companion commitment rejected", error_type=type(exc).__name__)
        return None
    if created:
        logger.info(
            "companion commitment recorded",
            actor_id=actor_id,
            status=commitment.status,
            due_at_ms=commitment.due_at_ms,
        )
    return commitment


def parse_due_ms(quote: str, now_ms: int) -> int | None:
    """Parse a deadline out of the promise text; ``None`` when the promise is open-ended.

    Only shallow, deterministic forms are understood. Vague phrases that do promise
    action but no checkable time ("回头", "改天", "以后") stay deadline-less instead of
    inventing one.
    """
    text = quote or ""
    match = _DATE.search(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            due = datetime(year, month, day, 23, 59, tzinfo=_TZ)
        except ValueError:
            return None
        return int(due.timestamp() * 1000)

    match = _DAYS_LATER.search(text)
    if match:
        days = _number(match.group(1))
        if days is None:
            return None
        return _end_of_day(now_ms + days * _DAY_MS)

    if "今晚" in text or "今天晚上" in text:
        return _end_of_day(now_ms)
    if "后天" in text:
        return _end_of_day(now_ms + 2 * _DAY_MS)
    if "明天" in text:
        return _end_of_day(now_ms + _DAY_MS)
    if "今天" in text:
        return _end_of_day(now_ms)
    if "周末" in text:
        return _next_weekday(now_ms, 6)
    if "下周" in text:
        return _next_weekday(now_ms, 0)
    if any(word in text for word in ("一会儿", "稍后", "等会", "马上")):
        return now_ms + 2 * _HOUR_MS
    return None


def sweep_overdue(
    store: CompanionStore, *, now_ms: int | None = None, grace_ms: int = OVERDUE_GRACE_MS
) -> dict[str, int]:
    """Move active→overdue and long-overdue→failed; returns the counts per transition."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    counts = {"overdue": 0, "failed": 0}
    for commitment in store.commitments_for_sweep(now_ms=now):
        if commitment.status == "active":
            store.set_commitment_status(commitment.commitment_id, status="overdue")
            counts["overdue"] += 1
        elif commitment.status == "overdue" and commitment.due_at_ms is not None:
            if commitment.due_at_ms + grace_ms <= now:
                store.set_commitment_status(commitment.commitment_id, status="failed")
                counts["failed"] += 1
    if counts["overdue"] or counts["failed"]:
        logger.info("companion commitments swept", **counts)
    return counts


def complete_commitment(
    store: CompanionStore,
    commitment_id: str,
    *,
    succeeded: bool,
    result_event_id: str | None = None,
) -> Commitment:
    """Close a promise as succeeded/failed (requires the evidence event in Step 5)."""
    return store.set_commitment_status(
        commitment_id,
        status="succeeded" if succeeded else "failed",
        result_event_id=result_event_id,
    )


def cancel_commitment(store: CompanionStore, commitment_id: str) -> Commitment:
    """Human entry point: cancel a promise that will not be kept."""
    return store.set_commitment_status(commitment_id, status="cancelled")


def commitment_view(
    store: CompanionStore, actor_id: str, principal_id: str, *, open_only: bool = False
) -> list[dict[str, object]]:
    """Human-readable view of one actor's promises (debug/export entry point)."""
    return [
        {
            "commitment_id": item.commitment_id,
            "status": item.status,
            "due_at_ms": item.due_at_ms,
            "quote": item.quote,
            "action": item.action,
            "success_criteria": item.success_criteria,
            "source_event_id": item.source_event_id,
        }
        for item in store.commitments(actor_id, principal_id, open_only=open_only)
    ]


# ---------------------------------------------------------------- background scan


_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="companion-commitment")
        return _EXECUTOR


def schedule_commitment_extraction(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    event_id: str,
    kind: str,
    summary: str,
    occurred_at_ms: int,
) -> None:
    """Best-effort background extraction; the reply path never waits for the model."""
    if kind not in _SENT_KINDS or not looks_like_promise(summary):
        return
    try:
        _executor().submit(
            _extract_in_background,
            store,
            actor_id,
            principal_id,
            event_id,
            kind,
            summary,
            occurred_at_ms,
        )
    except Exception:  # noqa: BLE001 — scheduling must never break a send
        logger.warning(
            "companion commitment scheduling failed", stack_trace=traceback.format_exc()
        )


def _extract_in_background(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    event_id: str,
    kind: str,
    summary: str,
    occurred_at_ms: int,
) -> None:
    try:
        record_commitment(
            store,
            actor_id,
            principal_id,
            event_id=event_id,
            kind=kind,
            summary=summary,
            occurred_at_ms=occurred_at_ms,
        )
    except Exception:  # noqa: BLE001 — worker must not leak into the pool
        logger.warning(
            "companion commitment background scan failed",
            stack_trace=traceback.format_exc(),
        )


# ------------------------------------------------------------------------- helpers


def _validate(data: object, text: str) -> CommitmentOutcome:
    if not isinstance(data, dict):
        return CommitmentOutcome(ok=False, error="payload is not an object")
    if data.get("is_commitment") is not True:
        return CommitmentOutcome(ok=False, error="not a commitment")
    quote = data.get("quote")
    action = data.get("action")
    criteria = data.get("success_criteria")
    confidence = data.get("confidence")
    for name, value in (("quote", quote), ("action", action), ("success_criteria", criteria)):
        if not isinstance(value, str) or not value.strip():
            return CommitmentOutcome(ok=False, error=f"{name} missing")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return CommitmentOutcome(ok=False, error="confidence is not a number")
    if not 0 <= float(confidence) <= 1:
        return CommitmentOutcome(ok=False, error="confidence out of range")
    quote_text = redact_text(quote.strip())
    if quote_text not in text:
        return CommitmentOutcome(ok=False, error="quote is not an exact substring")
    return CommitmentOutcome(
        ok=True,
        quote=quote_text,
        action=redact_text(action.strip())[:MAX_ACTION_CHARS],
        success_criteria=redact_text(criteria.strip())[:MAX_CRITERIA_CHARS],
        confidence=float(confidence),
    )


def _number(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    value = 0
    for char in raw:
        digit = _CN_NUMBERS.get(char)
        if digit is None:
            return None
        value = value * 10 + digit if value else digit
    return value or None


def _end_of_day(ms: int) -> int:
    day = datetime.fromtimestamp(ms / 1000, tz=_TZ)
    return int(day.replace(hour=23, minute=59, second=0, microsecond=0).timestamp() * 1000)


def _next_weekday(now_ms: int, weekday: int) -> int:
    """End of the next occurrence of ``weekday`` (Mon=0 … Sun=6)."""
    current = datetime.fromtimestamp(now_ms / 1000, tz=_TZ)
    delta = (weekday - current.weekday()) % 7 or 7
    return _end_of_day(now_ms + delta * _DAY_MS)


def _default_model() -> object:
    from gacore.llm import get_llm

    return get_llm([], bind_tools=False)
