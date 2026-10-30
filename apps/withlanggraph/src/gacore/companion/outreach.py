"""Controlled proactive loop (V2-Step 6, migration v7).

Design: ``docs/companion-self-memory-v2-plan.md`` V2-Step 6.

    V1 events / reflections
    → relationship / mood / commitment / open threads   (deterministic candidates)
    → gate (quiet hours, busy, quota, gap, no-response decay)
    → Decision LLM (only when candidates survive the gate)
    → policy tightening (code has the final veto)
    → STAY_SILENT / TELL_ONLY / ASK_PERMISSION / ACT_READ_ONLY

Two invariants the tests pin down: no candidates means **zero** model calls, and a system
failure is reported as ``SYSTEM_FAILURE`` — it never becomes a character utterance or a
story about why the character stayed quiet.
"""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Final

from langchain_core.messages import HumanMessage, SystemMessage

from gacore.companion.activity import derive_availability, derive_day_phase
from gacore.companion.goal import ALLOWED_TOOLS, finish_goal
from gacore.companion.mood import active_mood
from gacore.companion.store import (
    CompanionEvent,
    CompanionStore,
    Goal,
    OutreachFeedback,
)
from gacore.jsonl_logger import get_logger
from gacore.redact import redact_text

logger = get_logger("companion.outreach")

OUTREACH_PROMPT_VERSION: Final = "companion-outreach@v1"
MAX_MESSAGE_CHARS: Final = 200
MAX_CANDIDATES: Final = 3
REPLY_WINDOW_MS: Final = 15 * 60 * 60 * 1000
MIN_OUTREACH_GAP_MS: Final = 3 * 60 * 60 * 1000
NO_RESPONSE_DECAY: Final = 0.5
MIN_MULTIPLIER: Final = 0.25
PERMISSION_TTL_MS: Final = 30 * 60 * 1000

REASON_CODES: Final = frozenset(
    {
        "NO_CANDIDATE",
        "POLICY_BLOCKED",
        "TOO_REPETITIVE",
        "USER_BUSY",
        "QUIET_HOURS",
        "LOW_VALUE",
        "SYSTEM_FAILURE",
    }
)
ACTIONS: Final = frozenset(
    {
        "STAY_SILENT",
        "TELL_ONLY",
        "ASK_PERMISSION",
        "ACT_READ_ONLY",
        "ACT_SILENTLY",
        "ACT_AND_TELL",
    }
)
# Design §16.3: the life loop may do a small read-only thing silently, or do it and then
# tell the owner. Both are bounded by the goal whitelist and the activity budget.
ACTING_ACTIONS: Final = frozenset(
    {"ACT_READ_ONLY", "ACT_SILENTLY", "ACT_AND_TELL"}
)

_TZ: Final = timezone(timedelta(hours=8))
_JSON_OBJECT = None  # set below; kept out of the import block for readability
import re  # noqa: E402

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)

_SYSTEM_PROMPT: Final = """你是角色主动联系决策器。给定候选与当前状态，决定这一轮做什么。

只能从这些动作里选一个：
- STAY_SILENT：不打扰（价值低/重复/时机不对）。
- TELL_ONLY：以角色口吻发一条短消息（≤200 字）。
- ASK_PERMISSION：为一个具体只读目标征得主人同意（必须给 goal_id）。
- ACT_READ_ONLY：执行已批准的只读目标（必须给 goal_id）。
- ACT_SILENTLY：自己做一件只读小事，不打扰主人（可选 goal_id）。
- ACT_AND_TELL：自己做一件只读小事，做完再告诉主人（可选 goal_id）。

硬规则：
1. 优先沉默；没有值得说的就不要发。
2. 不说"我是机器人/定时任务/系统提示"。
3. 不重复最近已说过的内容。
4. 只输出 JSON，键名一字不差：
{"action": "STAY_SILENT", "reason": "LOW_VALUE", "message": "", "goal_id": null}
reason 只能取：NO_CANDIDATE / POLICY_BLOCKED / TOO_REPETITIVE / USER_BUSY /
QUIET_HOURS / LOW_VALUE / SYSTEM_FAILURE。"""


@dataclass(frozen=True, slots=True)
class OutreachCandidate:
    """One reason the character might speak, derived from stored state."""

    kind: str
    text: str
    priority: int
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GateInput:
    """Everything the gate needs; gathering it is the caller's job (no hidden reads)."""

    now_ms: int
    daily_count: int = 0
    max_daily: int = 3
    last_outreach_ms: int | None = None
    min_gap_ms: int = MIN_OUTREACH_GAP_MS
    no_response_streak: int = 0
    last_owner_message_ms: int | None = None
    has_running_activity: bool = False


@dataclass(frozen=True, slots=True)
class GateResult:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """The loop's answer for this tick; ``llm_calls`` makes the cost auditable."""

    action: str
    reason: str | None = None
    message: str = ""
    goal_id: str | None = None
    token: dict[str, object] | None = None
    candidates: tuple[OutreachCandidate, ...] = field(default_factory=tuple)
    llm_calls: int = 0


def collect_candidates(
    store: CompanionStore, actor_id: str, principal_id: str, *, now_ms: int
) -> list[OutreachCandidate]:
    """Deterministic candidate list: own fresh activity, threads, promises, concern, goal."""
    candidates: list[OutreachCandidate] = []
    events = store.events_for_actor(actor_id, principal_id)

    # What the character itself just looked at (ACT_SILENTLY). A result newer than the last
    # outbound message is a genuine "I did something, want to tell you" hook; once told it
    # stops being a candidate, so the same finding is never repeated.
    last_sent = max(
        (event.occurred_at_ms for event in events if event.kind == "proactive.message.sent"),
        default=0,
    )
    fresh = [
        event
        for event in events
        if event.kind == "read_action.succeeded"
        and event.occurred_at_ms >= last_sent
        and now_ms - event.occurred_at_ms <= 12 * 60 * 60 * 1000
    ]
    if fresh:
        latest = fresh[-1]
        candidates.append(
            OutreachCandidate(
                kind="recent_activity",
                text=f"我刚看到：{latest.summary}",
                priority=4,
                source_ids=(latest.event_id,),
            )
        )

    for thread in store.open_threads(actor_id, principal_id):
        candidates.append(
            OutreachCandidate(
                kind="open_thread",
                text=f"上次没说完的话题：{thread.topic}",
                priority=3,
                source_ids=(thread.source_event_id,),
            )
        )

    for commitment in store.commitments(actor_id, principal_id, status="active"):
        if commitment.due_at_ms is None or commitment.due_at_ms <= now_ms + 24 * 60 * 60 * 1000:
            candidates.append(
                OutreachCandidate(
                    kind="commitment_due",
                    text=f"答应过的事：{commitment.action}",
                    priority=2,
                    source_ids=(commitment.source_event_id,),
                )
            )

    mood = active_mood(store, actor_id, principal_id, now_ms=now_ms)
    if mood is not None and mood.label == "concerned":
        candidates.append(
            OutreachCandidate(
                kind="owner_concern",
                text="主人最近状态不太好，可以问候一句",
                priority=1,
                source_ids=(mood.source_event_id,),
            )
        )

    for goal in store.goals(actor_id, principal_id, status="proposed"):
        candidates.append(
            OutreachCandidate(
                kind="goal_permission",
                text=f"想做的事：{goal.text}",
                priority=0,
                source_ids=(goal.goal_id,),
            )
        )

    candidates.sort(key=lambda item: item.priority, reverse=True)
    return candidates[:MAX_CANDIDATES]


def evaluate_gate(gate: GateInput) -> GateResult:
    """Pure gate: every refusal carries one of the plan's reason codes."""
    if derive_day_phase(gate.now_ms) == "quiet":
        return GateResult(False, "QUIET_HOURS")
    availability = derive_availability(
        now_ms=gate.now_ms,
        last_owner_message_ms=gate.last_owner_message_ms,
        last_outreach_ms=gate.last_outreach_ms,
        cooldown_ms=gate.min_gap_ms,
        running_activity=_RunningActivity(gate.has_running_activity),
    )
    if availability in {"chatting", "working"}:
        return GateResult(False, "USER_BUSY")
    if gate.daily_count >= gate.max_daily:
        return GateResult(False, "POLICY_BLOCKED")
    if gate.last_outreach_ms is not None and gate.now_ms - gate.last_outreach_ms < gate.min_gap_ms:
        return GateResult(False, "TOO_REPETITIVE")
    if gate.no_response_streak >= 3:
        return GateResult(False, "LOW_VALUE")
    return GateResult(True, None)


def decide(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    gate: GateInput,
    model: object | None = None,
    runner: object | None = None,
    now_ms: int | None = None,
) -> Decision:
    """One tick of the loop. Never sends anything; the caller owns delivery.

    The clock comes from the caller: an explicit ``now_ms`` wins, otherwise the gate's own
    ``now_ms`` is authoritative (so a tick can be replayed deterministically in tests).
    """
    now = _now(now_ms) if now_ms is not None else int(gate.now_ms)
    candidates = collect_candidates(store, actor_id, principal_id, now_ms=now)
    if not candidates:
        return Decision(action="STAY_SILENT", reason="NO_CANDIDATE")

    gate_result = evaluate_gate(replace(gate, now_ms=now))
    if not gate_result.allowed:
        return Decision(
            action="STAY_SILENT",
            reason=gate_result.reason or "POLICY_BLOCKED",
            candidates=tuple(candidates),
        )

    try:
        if runner is not None:
            payload = runner(candidates)  # type: ignore[operator]
        else:
            payload = _ask_model(
                model, candidates, store.relationship(actor_id, principal_id), now
            )
        decision = _validate_decision(payload, candidates)
    except Exception as exc:  # noqa: BLE001 — a decision failure is silent, never narrated
        logger.warning(
            "companion outreach decision failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return Decision(
            action="STAY_SILENT",
            reason="SYSTEM_FAILURE",
            candidates=tuple(candidates),
            llm_calls=1,
        )

    if decision.action in {"TELL_ONLY", "ASK_PERMISSION"} | ACTING_ACTIONS:
        tightened = _tighten(store, actor_id, principal_id, decision, now)
        if tightened is not None:
            return tightened
    return decision


def record_sent(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    sent_event_id: str,
) -> OutreachFeedback:
    """Register one outreach so a later owner reply can be linked back to it."""
    return store.record_outreach(
        actor_id=actor_id, principal_id=principal_id, sent_event_id=sent_event_id
    )


def match_reply(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    owner_event: CompanionEvent,
    window_ms: int = REPLY_WINDOW_MS,
) -> OutreachFeedback | None:
    """Attach one owner event to the newest unanswered outreach inside the window."""
    rows = [row for row in store.outreach_rows(actor_id, principal_id, kind="no_response")]
    if not rows:
        return None
    latest = rows[-1]
    sent = _event_time(store, actor_id, principal_id, latest.sent_event_id)
    if sent is None or not 0 < owner_event.occurred_at_ms - sent <= window_ms:
        return None
    return store.set_outreach_reply(
        latest.outreach_id,
        kind=classify_reply(owner_event.summary),
        reply_event_id=owner_event.event_id,
        response_delay_ms=owner_event.occurred_at_ms - sent,
    )


def classify_reply(text: str) -> str:
    """Deterministic reply classification (no model, no sentiment guessing)."""
    from gacore.companion.relationship import _boundary_phrase  # shared vocabulary

    if _boundary_phrase(text or "") is not None:
        return "boundary"
    if any(word in (text or "") for word in ("烦", "别", "再说", "不用", "算了")):
        return "negative"
    return "positive"


def expire_unanswered(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now_ms: int,
    window_ms: int = REPLY_WINDOW_MS,
) -> int:
    """Keep ``no_response`` for outreach whose reply window closed (evidence, not a mood)."""
    expired = 0
    for row in store.outreach_rows(actor_id, principal_id, kind="no_response"):
        sent = _event_time(store, actor_id, principal_id, row.sent_event_id)
        if sent is None:
            continue
        if now_ms - sent >= window_ms and row.reply_event_id is None:
            expired += 1  # already stored as no_response; counting keeps the decay auditable
    return expired


def outreach_multiplier(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now_ms: int,
    window_ms: int = REPLY_WINDOW_MS,
) -> float:
    """Lower the next outreach probability after unanswered ones; never below the floor.

    This only touches the *character's* eagerness — the owner is never recorded as
    negative or unhappy just because they did not reply.
    """
    streak = 0
    for row in reversed(store.outreach_rows(actor_id, principal_id)):
        sent = _event_time(store, actor_id, principal_id, row.sent_event_id)
        if sent is None or now_ms - sent > window_ms:
            break
        if row.kind != "no_response":
            break
        streak += 1
    return max(MIN_MULTIPLIER, NO_RESPONSE_DECAY**min(streak, 4))


def permission_token(
    goal: Goal,
    *,
    action: str,
    arguments: dict[str, object],
    now_ms: int,
    ttl_ms: int = PERMISSION_TTL_MS,
) -> dict[str, object]:
    """Bind a permission request to one exact action, so approval cannot be replayed."""
    digest = hashlib.sha256(
        json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "goal_id": goal.goal_id,
        "action": action,
        "exact_arguments_hash": digest,
        "expires_at_ms": now_ms + ttl_ms,
        "approved_by": None,
        "status": "pending",
    }


def execute_read_only_goal(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    goal_id: str,
    *,
    tool: str,
    invoker: object,
    now_ms: int | None = None,
) -> Goal:
    """Execute an approved goal with one whitelisted tool, then close it with real events."""
    goal = store.get_goal(goal_id)
    if goal is None:
        raise ValueError(f"unknown goal: {goal_id!r}")
    if goal.status not in {"approved", "in_progress"}:
        raise ValueError(f"goal {goal_id!r} is {goal.status!r}, not approved")
    if tool not in ALLOWED_TOOLS or tool not in goal.allowed_tools:
        raise ValueError(f"tool {tool!r} is not allowed for this goal")
    if goal.status == "approved":
        store.set_goal_status(goal_id, status="in_progress")
    try:
        detail = invoker(tool)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 — a failed read is a real, recorded failure
        return finish_goal(
            store,
            actor_id,
            principal_id,
            goal_id,
            succeeded=False,
            detail=f"只读执行失败：{type(exc).__name__}",
            occurred_at_ms=now_ms,
        )
    return finish_goal(
        store,
        actor_id,
        principal_id,
        goal_id,
        succeeded=True,
        detail=redact_text(str(detail) or "ok"),
        occurred_at_ms=now_ms,
    )


# ------------------------------------------------------------------------ internals


class _RunningActivity:
    """Tiny adapter so the pure availability helper can see "working"."""

    def __init__(self, running: bool) -> None:
        self.status = "active" if running else "idle"


def _ask_model(
    model: object | None,
    candidates: list[OutreachCandidate],
    relationship: object | None,
    now_ms: int,
) -> object:
    if model is None:
        from gacore.llm import get_llm

        model = get_llm([], bind_tools=False)
    payload = {
        "now_ms": now_ms,
        "candidates": [
            {"kind": item.kind, "text": item.text, "priority": item.priority}
            for item in candidates
        ],
        "relationship": {
            "stage": getattr(relationship, "stage", None),
            "status": getattr(relationship, "status", None),
        },
    }
    response = model.invoke(  # type: ignore[attr-defined]
        [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )
    raw = getattr(response, "content", response)
    text = raw if isinstance(raw, str) else str(raw)
    match = _JSON_OBJECT.search(text or "")
    if not match:
        raise ValueError("decision is not JSON")
    return json.loads(match.group(0))


def _validate_decision(
    payload: object, candidates: list[OutreachCandidate]
) -> Decision:
    if not isinstance(payload, dict):
        raise ValueError("decision payload is not an object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    reason = payload.get("reason")
    if reason is not None and reason not in REASON_CODES:
        reason = "POLICY_BLOCKED"
    message = redact_text(str(payload.get("message") or "").strip())[:MAX_MESSAGE_CHARS]
    goal_id = payload.get("goal_id") or None
    if goal_id is not None and not isinstance(goal_id, str):
        raise ValueError("goal_id must be a string or null")
    return Decision(
        action=action,
        reason=reason,
        message=message,
        goal_id=goal_id,
        candidates=tuple(candidates),
        llm_calls=1,
    )


def _tighten(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    decision: Decision,
    now_ms: int,
) -> Decision | None:
    """Code has the final veto: an unsafe or unsupported action degrades, never executes."""
    if decision.action == "TELL_ONLY":
        if not decision.message:
            return Decision(
                action="STAY_SILENT",
                reason="LOW_VALUE",
                candidates=decision.candidates,
                llm_calls=decision.llm_calls,
            )
        return None

    if decision.action in {"ACT_SILENTLY", "ACT_AND_TELL"} and not decision.goal_id:
        # The loop proposes a small read-only goal itself and validates it against the
        # whitelist before executing; nothing here can widen that permission.
        return None

    goal = store.get_goal(decision.goal_id or "")
    if goal is None:
        return Decision(
            action="STAY_SILENT",
            reason="POLICY_BLOCKED",
            candidates=decision.candidates,
            llm_calls=decision.llm_calls,
        )
    if goal.status in {"succeeded", "failed", "cancelled"}:
        return Decision(
            action="STAY_SILENT",
            reason="POLICY_BLOCKED",
            candidates=decision.candidates,
            llm_calls=decision.llm_calls,
        )
    if any(tool not in ALLOWED_TOOLS for tool in goal.allowed_tools):
        return Decision(
            action="STAY_SILENT",
            reason="POLICY_BLOCKED",
            candidates=decision.candidates,
            llm_calls=decision.llm_calls,
        )

    if decision.action == "ACT_READ_ONLY":
        if goal.status not in {"approved", "in_progress"}:
            # Not approved yet: ask instead of acting (ASK_PERMISSION never pre-executes).
            return Decision(
                action="ASK_PERMISSION",
                reason=decision.reason,
                message=decision.message or f"想帮你做一件事：{goal.text}",
                goal_id=goal.goal_id,
                token=permission_token(goal, action=goal.text, arguments={}, now_ms=now_ms),
                candidates=decision.candidates,
                llm_calls=decision.llm_calls,
            )
        return None

    if decision.action == "ASK_PERMISSION":
        return Decision(
            action=decision.action,
            reason=decision.reason,
            message=decision.message or f"想帮你做一件事：{goal.text}",
            goal_id=goal.goal_id,
            token=permission_token(goal, action=goal.text, arguments={}, now_ms=now_ms),
            candidates=decision.candidates,
            llm_calls=decision.llm_calls,
        )
    return None


def _event_time(
    store: CompanionStore, actor_id: str, principal_id: str, event_id: str
) -> int | None:
    for event in store.events_for_actor(actor_id, principal_id):
        if event.event_id == event_id:
            return event.occurred_at_ms
    return None


def _now(now_ms: int | None) -> int:
    if now_ms is not None:
        return int(now_ms)
    return int(datetime.now(_TZ).timestamp() * 1000)

# ---------------------------------------------------------------- shadow mode


_OWNER_KINDS: Final = frozenset({"user.message.received"})
_OUTBOUND_KINDS: Final = frozenset(
    {"proactive.message.sent", "agent.message.sent"}
)


def build_gate_input(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now_ms: int,
    max_daily: int = 3,
    min_gap_ms: int = MIN_OUTREACH_GAP_MS,
    window_ms: int = REPLY_WINDOW_MS,
) -> GateInput:
    """Derive the gate inputs from stored state (no model, no side effects)."""
    events = store.events_for_actor(actor_id, principal_id)
    last_owner = next(
        (event.occurred_at_ms for event in reversed(events) if event.kind in _OWNER_KINDS),
        None,
    )
    outbound = [event for event in events if event.kind in _OUTBOUND_KINDS]
    last_outreach = outbound[-1].occurred_at_ms if outbound else None
    day_start = now_ms - 24 * 60 * 60 * 1000
    daily_count = sum(1 for event in outbound if event.occurred_at_ms >= day_start)
    streak = 0
    for row in reversed(store.outreach_rows(actor_id, principal_id)):
        sent = next(
            (event.occurred_at_ms for event in events if event.event_id == row.sent_event_id),
            None,
        )
        if sent is None or now_ms - sent > window_ms:
            break
        if row.kind != "no_response":
            break
        streak += 1
    return GateInput(
        now_ms=now_ms,
        daily_count=daily_count,
        max_daily=max_daily,
        last_outreach_ms=last_outreach,
        min_gap_ms=min_gap_ms,
        no_response_streak=streak,
        last_owner_message_ms=last_owner,
        has_running_activity=store.running_activity(actor_id, principal_id) is not None,
    )


def shadow_decision(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now_ms: int | None = None,
    model: object | None = None,
    runner: object | None = None,
) -> Decision:
    """Run one full tick of the loop against live state, for logging only.

    Nothing is sent, nothing is written: ``decide()`` is pure with respect to storage, so
    shadow mode is a read-only rehearsal of the new decision layer.
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    gate = build_gate_input(store, actor_id, principal_id, now_ms=now)
    return decide(
        store,
        actor_id,
        principal_id,
        gate=gate,
        model=model,
        runner=runner,
        now_ms=now,
    )


def log_shadow_decision(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    job_name: str,
    now_ms: int | None = None,
    model: object | None = None,
    runner: object | None = None,
) -> Decision | None:
    """Shadow-mode entry used by the scheduler: log the decision, never raise, never send."""
    try:
        decision = shadow_decision(
            store,
            actor_id,
            principal_id,
            now_ms=now_ms,
            model=model,
            runner=runner,
        )
    except Exception:  # noqa: BLE001 — shadow mode must never disturb the real job
        logger.warning(
            "outreach shadow decision failed", job=job_name, stack_trace=traceback.format_exc()
        )
        return None
    logger.info(
        "outreach shadow decision",
        job=job_name,
        action=decision.action,
        reason=decision.reason,
        candidates=[candidate.kind for candidate in decision.candidates],
        message_chars=len(decision.message),
        goal_id=decision.goal_id,
        llm_calls=decision.llm_calls,
    )
    return decision
