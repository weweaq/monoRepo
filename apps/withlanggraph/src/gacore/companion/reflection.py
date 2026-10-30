"""Evidence-bound reflection: 0-2 first-person takeaways from a fixed event batch.

Design: ``docs/companion-self-memory-mvp.md`` §7. The reflector is deliberately
strict — every takeaway must cite 2-5 events from the same batch and the same
actor, and a batch never crosses a failure: the first failure retries the exact
same events, the second failure skips them and records why.

The model is an injection seam (``model=`` or ``runner=``) so tests never touch a
real provider; ``get_llm([], bind_tools=False)`` is resolved lazily only when a
real run happens.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from langchain_core.messages import HumanMessage, SystemMessage

from gacore.companion.store import (
    CompanionEvent,
    CompanionStore,
    CompanionStoreError,
    ReflectionDraft,
)
from gacore.jsonl_logger import get_logger
from gacore.redact import redact_text

logger = get_logger("companion.reflection")

REFLECTION_PROMPT_VERSION: Final = "companion-reflection@v1"
BATCH_SIZE: Final = 10
MIN_BATCH_EVENTS: Final = 3
MAX_REFLECTIONS: Final = 2
MIN_SOURCES: Final = 2
MAX_SOURCES: Final = 5
MAX_STATEMENT_CHARS: Final = 200

# UTC+8 is the product's "today" (R6 timestamps use the same offset).
_DAY_TZ: Final = timezone(timedelta(hours=8))

_JSON_OBJECT: Final = re.compile(r"\{.*\}", re.S)
# Statements must stay free of local paths and credential-looking strings.
_SENSITIVE: Final = re.compile(
    r"(?:[A-Za-z]:\\|\\\\[A-Za-z]|/home/|/Users/|/etc/|~/|\.env\b"
    r"|\b(?:sk|pk|ak|rk|ghp|gho|ghs|github_pat)[-_][A-Za-z0-9_\-]{8,}\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/=\-]{8,})",
)
# A statement is injected as background later (Step 8), so command-like text that
# tries to switch roles, invoke tools or rewrite the system prompt is rejected at
# write time instead of being sanitised downstream.
_COMMAND_LIKE: Final = re.compile(
    r"(?:忽略|无视|跳过|绕过|覆盖)(?:上述|以上|之前|所有|全部)?(?:的)?(?:规则|指令|限制|设定)"
    r"|(?:切换|更换|扮演|变成|成为)(?:成|为)?(?:其他|别的|另一个)?(?:角色|人格|助手)"
    r"|(?:调用|执行)(?:工具|函数|qq_push)"
    r"|(?:系统指令|系统提示|提示词|system\s*prompt)",
    re.IGNORECASE,
)

_SYSTEM_PROMPT: Final = """你是角色自我反思提取器，不是聊天角色。

任务：
仅根据输入事件，提取 0-2 条对角色自身行为或互动方式有复用价值的认识。

硬规则：
1. 每条反思必须引用 2-5 个 source_event_ids。
2. 不得声称事件中没有发生的行动。
3. user.message.received 是用户说过的话，不是角色自己的经历。
4. agent.message.sent / proactive.message.sent 只表示确认发送过相应内容。
5. 不修改角色人格，不推断关系阶段，不创建承诺或目标。
6. 不输出用户隐私、凭据、完整路径或完整聊天。
7. 证据不足时返回空数组。
8. 只输出符合下面 schema 的 JSON。

输出格式（键名一字不差，不要自创字段；不要输出 schema 以外的键）：
{"reflections": [{"statement": "一句 1-200 字的第一人称认识", "source_event_ids": ["evt_xxx", "evt_yyy"], "confidence": 0.0}]}
- 没有可复用认识时输出 {"reflections": []}
- confidence 是 0 到 1 的小数；source_event_ids 必须取自输入的 event_id。
"""

Runner = Callable[[Sequence[CompanionEvent]], "ReflectionOutcome"]


@dataclass(frozen=True, slots=True)
class ReflectionOutcome:
    """Validated reflection result; ``ok=False`` carries a human-readable reason."""

    ok: bool
    drafts: tuple[ReflectionDraft, ...] = ()
    error: str | None = None


def run_reflection(
    events: Sequence[CompanionEvent], *, model: object | None = None
) -> ReflectionOutcome:
    """Ask the reflector for 0-2 takeaways and validate them against ``events``."""
    if not events:
        return ReflectionOutcome(ok=False, error="no events to reflect on")
    try:
        resolved = model if model is not None else _default_model()
        payload = json.dumps(
            {
                "events": [
                    {
                        "event_id": event.event_id,
                        "kind": event.kind,
                        "summary": event.summary,
                        "occurred_at_ms": event.occurred_at_ms,
                    }
                    for event in events
                ]
            },
            ensure_ascii=False,
        )
        response = resolved.invoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=payload)]
        )
        raw = getattr(response, "content", response)
        text = raw if isinstance(raw, str) else str(raw)
    except Exception as exc:  # noqa: BLE001 — reflection failure is a retry signal, never a crash
        logger.warning(
            "companion reflection model call failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return ReflectionOutcome(ok=False, error=f"model call failed: {type(exc).__name__}")

    match = _JSON_OBJECT.search(text or "")
    if not match:
        return ReflectionOutcome(ok=False, error="response is not JSON")
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return ReflectionOutcome(ok=False, error="response JSON is malformed")
    drafts, error = validate_reflections(data, {event.event_id for event in events})
    if drafts is None:
        return ReflectionOutcome(ok=False, error=error)
    return ReflectionOutcome(ok=True, drafts=drafts)


def validate_reflections(
    data: object, allowed_event_ids: set[str]
) -> tuple[tuple[ReflectionDraft, ...] | None, str | None]:
    """Validate the reflector payload; ``(None, reason)`` means the batch is invalid."""
    if not isinstance(data, dict):
        return None, "payload is not an object"
    raw_reflections = data.get("reflections")
    if not isinstance(raw_reflections, list):
        return None, "reflections is not a list"
    if len(raw_reflections) > MAX_REFLECTIONS:
        return None, f"too many reflections ({len(raw_reflections)} > {MAX_REFLECTIONS})"
    drafts: list[ReflectionDraft] = []
    for raw in raw_reflections:
        if not isinstance(raw, dict):
            return None, "reflection is not an object"
        statement = raw.get("statement")
        if not isinstance(statement, str):
            return None, "statement is not a string"
        statement = statement.strip()
        if not 1 <= len(statement) <= MAX_STATEMENT_CHARS:
            return None, "statement length out of range"
        if _contains_sensitive(statement):
            return None, "statement contains a secret, token or path"
        if _COMMAND_LIKE.search(statement):
            return None, "statement contains an instruction or role switch"
        confidence = raw.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None, "confidence is not a number"
        if not 0 <= float(confidence) <= 1:
            return None, "confidence out of range"
        sources = raw.get("source_event_ids")
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            return None, "source_event_ids is not a string list"
        unique = list(dict.fromkeys(sources))
        if not MIN_SOURCES <= len(unique) <= MAX_SOURCES:
            return None, f"source count out of range ({len(unique)})"
        unknown = [s for s in unique if s not in allowed_event_ids]
        if unknown:
            return None, f"sources outside the batch: {unknown[:3]}"
        drafts.append(
            ReflectionDraft(
                statement=statement,
                confidence=float(confidence),
                source_event_ids=tuple(unique),
            )
        )
    return tuple(drafts), None


def reflect_due(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    runner: Runner | None = None,
    now_ms: int | None = None,
) -> str:
    """Run one reflection check; returns a short outcome code for logs and tests.

    Codes: ``too_few``, ``not_due_today``, ``reflected``, ``retry_scheduled``,
    ``batch_skipped``. Never raises for model/store contract failures — those are
    recorded on the cursor instead so the next event retries the same batch.
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cursor = store.get_cursor(actor_id, principal_id)

    if cursor.retry_batch_end_seq is not None:
        batch = store.events_between(
            actor_id, principal_id, cursor.last_reflected_seq + 1, cursor.retry_batch_end_seq
        )
        batch_end_seq = int(cursor.retry_batch_end_seq)
        if not batch:
            store.record_reflection_failure(
                actor_id=actor_id,
                principal_id=principal_id,
                batch_end_seq=batch_end_seq,
                failure_count=0,
                last_error="pinned retry batch is empty",
                advance=True,
            )
            logger.warning(
                "companion reflection retry batch vanished; cursor advanced",
                actor_id=actor_id,
                batch_end_seq=batch_end_seq,
            )
            return "batch_skipped"
    else:
        batch = store.events_after(actor_id, principal_id, cursor.last_reflected_seq, limit=BATCH_SIZE)
        if len(batch) < MIN_BATCH_EVENTS:
            return "too_few"
        batch_end_seq = batch[-1].seq
        store.begin_reflection_batch(actor_id, principal_id, batch_end_seq)

    if _reflected_today(cursor.last_reflected_at_ms, now):
        return "not_due_today"

    outcome = runner(batch) if runner is not None else run_reflection(batch)
    drafts, error = (
        validate_reflections(
            {
                "reflections": [
                    {
                        "statement": draft.statement,
                        "confidence": draft.confidence,
                        "source_event_ids": list(draft.source_event_ids),
                    }
                    for draft in outcome.drafts
                ]
            },
            {event.event_id for event in batch},
        )
        if outcome.ok
        else (None, outcome.error)
    )

    if drafts is not None:
        try:
            store.commit_reflection_batch(
                actor_id=actor_id,
                principal_id=principal_id,
                reflections=drafts,
                prompt_version=REFLECTION_PROMPT_VERSION,
                last_reflected_seq=batch_end_seq,
                retry_batch_end_seq=None,
                failure_count=0,
                last_error=None,
                last_reflected_at_ms=now,
            )
        except CompanionStoreError as exc:
            error = f"commit rejected: {exc}"
        else:
            logger.info(
                "companion reflection committed",
                actor_id=actor_id,
                batch_events=len(batch),
                reflections=len(drafts),
            )
            return "reflected"

    reason = error or "reflection failed"
    if cursor.failure_count == 0:
        store.record_reflection_failure(
            actor_id=actor_id,
            principal_id=principal_id,
            batch_end_seq=batch_end_seq,
            failure_count=1,
            last_error=reason,
            advance=False,
        )
        logger.warning(
            "companion reflection failed; the same batch will be retried",
            actor_id=actor_id,
            batch_end_seq=batch_end_seq,
            error=reason,
        )
        return "retry_scheduled"

    store.record_reflection_failure(
        actor_id=actor_id,
        principal_id=principal_id,
        batch_end_seq=batch_end_seq,
        failure_count=0,
        last_error=reason,
        advance=True,
    )
    logger.warning(
        "companion reflection failed twice; skipping the batch",
        actor_id=actor_id,
        batch_end_seq=batch_end_seq,
        error=reason,
    )
    return "batch_skipped"


def _reflected_today(last_reflected_at_ms: int | None, now_ms: int) -> bool:
    if last_reflected_at_ms is None:
        return False
    return _day_key(int(last_reflected_at_ms)) == _day_key(now_ms)


def _day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=_DAY_TZ).date().isoformat()


def _contains_sensitive(statement: str) -> bool:
    if redact_text(statement) != statement:
        return True
    return bool(_SENSITIVE.search(statement))


def is_unsafe_statement(text: str) -> bool:
    """True when ``text`` carries a credential/path or tries to act like an instruction.

    Shared by the reflection and journal validators: both write text that is later
    injected as background, so the check happens once at write time.
    """
    return _contains_sensitive(text) or bool(_COMMAND_LIKE.search(text))


def _default_model() -> object:
    from gacore.llm import get_llm

    return get_llm([], bind_tools=False)

# ------------------------------------------------------------------ lifecycle


def mark_stale(store: CompanionStore, reflection_id: str, *, reason: str) -> None:
    """A statement that no longer holds (its source changed) stops being injected."""
    store.set_reflection_status(reflection_id, status="stale", reason=reason)


def retract(store: CompanionStore, reflection_id: str, *, reason: str = "retracted") -> None:
    """The owner explicitly asked to forget this statement."""
    store.set_reflection_status(reflection_id, status="retracted", reason=reason)


def refute(
    store: CompanionStore, reflection_id: str, *, refuting_event_ids: Sequence[str], reason: str
) -> None:
    """A newer piece of evidence contradicts the statement: keep it, mark it contradicted."""
    if not refuting_event_ids:
        raise ValueError("refuting a reflection requires at least one event")
    store.set_reflection_status(
        reflection_id, status="contradicted", reason=reason, refutes_id=refuting_event_ids[0]
    )


def supersede(
    store: CompanionStore, old_reflection_id: str, new_reflection_id: str
) -> None:
    """A newer statement replaces an older one; the old row is kept as ``stale``."""
    store.set_reflection_status(
        old_reflection_id,
        status="stale",
        reason=f"superseded_by:{new_reflection_id}",
        supersedes_id=new_reflection_id,
    )


def affected_reflections(
    store: CompanionStore, event_ids: Sequence[str]
) -> list[str]:
    """Reflections that cite any of these events — the blast radius of a correction."""
    return [row.reflection_id for row in store.reflections_for_events(event_ids)]
