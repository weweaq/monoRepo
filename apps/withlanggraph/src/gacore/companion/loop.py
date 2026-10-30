"""The Companion life tick (V2 收尾, design §16 / §17A.8).

One tick is one chance for the character to (a) stay quiet, (b) do a small read-only thing
on its own, or (c) say something. It is deliberately cheap:

    SENSE      candidates come from stored state only (0 LLM when there is nothing)
    APPRAISE   the same deterministic gate as outreach
    DECIDE     one Decision LLM call, only when a candidate survives the gate
    ACT        STAY_SILENT / TELL_ONLY / ASK_PERMISSION / ACT_SILENTLY / ACT_AND_TELL
    RECORD     every real effect writes an event (send → proactive.message.sent,
               action → goal.started / read_action.* / goal.succeeded)
    EXPRESS    telling is a separate, capped step — a tick is allowed to stay silent

Design references: §16.3 decision table, §15.3 ACT_SILENTLY whitelist, §16.4 budgets
(one LLM per tick, zero for an empty tick), D-10 (a tick never guarantees a message).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Final

from gacore.companion.activity import (
    ActivityBudget,
    complete_activity,
    derive_day_phase,
    spend,
    start_activity,
)
from gacore.companion.goal import (
    ALLOWED_TOOLS,
    MAX_DEADLINE_DAYS,
    GoalProposal,
    create_goal,
    finish_goal,
)
from gacore.companion.outreach import (
    Decision,
    OutreachCandidate,
    build_gate_input,
    decide,
)
from gacore.companion.store import CompanionStore, Goal
from gacore.jsonl_logger import get_logger
from gacore.redact import redact_text

logger = get_logger("companion.loop")

LIFE_TICK_VERSION: Final = "companion-life@v1"
MAX_SENDS_PER_DAY: Final = 3
MAX_TICK_LLM_CALLS: Final = 1
ACTIVITY_DEADLINE_MINUTES: Final = 30
ACTIVITY_MAX_TOOL_CALLS: Final = 1
MAX_EXPRESSION_CHARS: Final = 200
_TZ: Final = timezone(timedelta(hours=8))
_DAY_MS: Final = 24 * 60 * 60 * 1000

# Deterministic tool choice per candidate kind: the character may only touch read-only data.
_CANDIDATE_ACTIVITY: Final = {
    "recent_activity": ("get_time", {}),
    "open_thread": ("search_daily", {"query": "还没聊完"}),
    "commitment_due": ("read_daily", {"date": "today"}),
    "owner_concern": ("read_daily", {"date": "today"}),
    "goal_permission": ("get_time", {}),
}

# When no candidate points at a specific tool, the character rotates through cheap read-only
# views of its own life so consecutive ticks do not all look at the same thing.
_ROTATION: Final = (
    ("bili_history", {"limit": 5}),
    ("read_daily", {"date": "yesterday"}),
    ("browser_history", {"limit": 20, "days": 1}),
)


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one tick actually did; the scheduler logs this verbatim."""

    action: str
    reason: str | None = None
    sent_text: str | None = None
    goal_id: str | None = None
    llm_calls: int = 0
    events: tuple[str, ...] = ()
    error: str | None = None


def run_life_tick(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now_ms: int | None = None,
    model: object | None = None,
    runner: object | None = None,
    sender: object | None = None,
    invoker: object | None = None,
    max_sends_per_day: int = MAX_SENDS_PER_DAY,
    dry_run: bool = False,
) -> TickResult:
    """One life tick: sense → gate → decide → act → record. Never raises at the caller."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        return _tick(
            store,
            actor_id,
            principal_id,
            now=now,
            model=model,
            runner=runner,
            sender=sender,
            invoker=invoker,
            max_sends_per_day=max_sends_per_day,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — a tick failure must never break the scheduler
        logger.warning(
            "companion life tick crashed", error_type=type(exc).__name__, error_message=str(exc)
        )
        return TickResult(action="STAY_SILENT", reason="SYSTEM_FAILURE", error=type(exc).__name__)


def _tick(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now: int,
    model: object | None,
    runner: object | None,
    sender: object | None,
    invoker: object | None,
    max_sends_per_day: int,
    dry_run: bool,
) -> TickResult:
    # SENSE + cheap refusals (0 LLM).
    sent_today = _sent_within(store, actor_id, principal_id, now, window_ms=_DAY_MS)
    if sent_today >= max_sends_per_day:
        return TickResult(action="STAY_SILENT", reason="POLICY_BLOCKED")
    if not store.events_for_actor(actor_id, principal_id):
        return TickResult(action="STAY_SILENT", reason="NO_CANDIDATE")

    gate = build_gate_input(store, actor_id, principal_id, now_ms=now)
    decision = decide(
        store,
        actor_id,
        principal_id,
        gate=gate,
        model=model,
        runner=runner,
        now_ms=now,
    )
    if decision.action == "STAY_SILENT":
        return TickResult(
            action="STAY_SILENT",
            reason=decision.reason,
            llm_calls=decision.llm_calls,
        )

    # ACT.
    if decision.action in {"TELL_ONLY", "ASK_PERMISSION"}:
        return _tell(
            store,
            actor_id,
            principal_id,
            decision,
            sender=sender,
            now=now,
            dry_run=dry_run,
        )
    if decision.action in {"ACT_SILENTLY", "ACT_AND_TELL", "ACT_READ_ONLY"}:
        return _act(
            store,
            actor_id,
            principal_id,
            decision,
            sender=sender,
            invoker=invoker,
            now=now,
            dry_run=dry_run,
        )
    return TickResult(
        action="STAY_SILENT", reason="POLICY_BLOCKED", llm_calls=decision.llm_calls
    )


def _tell(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    decision: Decision,
    *,
    sender: object | None,
    now: int,
    dry_run: bool,
) -> TickResult:
    if _quiet_now(now):
        return TickResult(
            action="STAY_SILENT", reason="QUIET_HOURS", llm_calls=decision.llm_calls
        )
    text = _expression(decision)
    if not text:
        return TickResult(
            action="STAY_SILENT", reason="LOW_VALUE", llm_calls=decision.llm_calls
        )
    if sender is None:
        return TickResult(
            action="STAY_SILENT",
            reason="POLICY_BLOCKED",
            llm_calls=decision.llm_calls,
            error="no_sender",
        )
    if dry_run:
        return TickResult(
            action=decision.action,
            reason=decision.reason,
            sent_text=text,
            llm_calls=decision.llm_calls,
        )
    try:
        sender(text)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 — failed delivery is recorded, never narrated
        logger.warning(
            "companion life tick delivery failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return TickResult(
            action="STAY_SILENT",
            reason="SYSTEM_FAILURE",
            llm_calls=decision.llm_calls,
            error="delivery_failed",
        )
    event_id = _record_sent(store, actor_id, principal_id, text, now)
    return TickResult(
        action=decision.action,
        reason=decision.reason,
        sent_text=text,
        goal_id=decision.goal_id,
        llm_calls=decision.llm_calls,
        events=("proactive.message.sent",) if event_id else (),
    )


def _act(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    decision: Decision,
    *,
    sender: object | None,
    invoker: object | None,
    now: int,
    dry_run: bool,
) -> TickResult:
    goal = store.get_goal(decision.goal_id) if decision.goal_id else None
    if goal is None:
        goal = _propose_small_goal(store, actor_id, principal_id, decision, now)
        if goal is None:
            return TickResult(
                action="STAY_SILENT", reason="POLICY_BLOCKED", llm_calls=decision.llm_calls
            )
    tool, args = _pick_tool(goal, decision, now)
    if tool is None or invoker is None:
        return TickResult(
            action="STAY_SILENT",
            reason="POLICY_BLOCKED",
            llm_calls=decision.llm_calls,
            goal_id=goal.goal_id,
        )
    if dry_run:
        return TickResult(
            action=decision.action,
            goal_id=goal.goal_id,
            llm_calls=decision.llm_calls,
        )

    budget = ActivityBudget(
        max_llm_calls=MAX_TICK_LLM_CALLS,
        max_read_actions=ACTIVITY_MAX_TOOL_CALLS,
        deadline_ms=now + ACTIVITY_DEADLINE_MINUTES * 60 * 1000,
        cancel_on_user_message=True,
    )
    activity = start_activity(
        store,
        actor_id,
        principal_id,
        kind=f"life:{tool}",
        budget=budget,
        source_goal_id=goal.goal_id,
        now_ms=now,
    )
    try:
        finished, detail = _execute(
            store, actor_id, principal_id, goal, tool, args, invoker, now
        )
    finally:
        if store.get_activity(activity.activity_id).status in {"active", "paused"}:
            complete_activity(
                store, actor_id, principal_id, activity.activity_id, now_ms=now
            )
        spend(store, actor_id, principal_id, activity.activity_id, read_actions=1, now_ms=now)

    if finished.status != "succeeded":
        # The read failed: record it, tell nobody. A failure is not the character's mood.
        return TickResult(
            action="STAY_SILENT",
            reason="SYSTEM_FAILURE",
            goal_id=finished.goal_id,
            llm_calls=decision.llm_calls,
            error="action_failed",
        )

    if decision.action == "ACT_SILENTLY":
        return TickResult(
            action=decision.action,
            goal_id=finished.goal_id,
            llm_calls=decision.llm_calls,
            events=("goal.started", "read_action.succeeded", "activity.completed"),
        )

    text = _expression(decision) or _fallback_expression(decision, detail)
    if sender is None:
        return TickResult(
            action=decision.action,
            goal_id=finished.goal_id,
            llm_calls=decision.llm_calls,
            error="no_sender",
        )
    try:
        sender(text)  # type: ignore[operator]
    except Exception:  # noqa: BLE001 — the action already succeeded; telling failed quietly
        return TickResult(
            action="STAY_SILENT",
            reason="SYSTEM_FAILURE",
            goal_id=finished.goal_id,
            llm_calls=decision.llm_calls,
            error="delivery_failed",
        )
    event_id = _record_sent(store, actor_id, principal_id, text, now)
    return TickResult(
        action=decision.action,
        sent_text=text,
        goal_id=finished.goal_id,
        llm_calls=decision.llm_calls,
        events=("goal.started", "read_action.succeeded", "proactive.message.sent")
        if event_id
        else ("goal.started", "read_action.succeeded"),
    )


def _execute(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    goal: Goal,
    tool: str,
    args: dict[str, object],
    invoker: object,
    now: int,
) -> tuple[Goal, str]:
    """Run the goal's single read-only tool and close it with real events."""
    """Run the goal's single read-only tool and close it with real events."""
    if goal.status == "proposed":
        store.set_goal_status(goal.goal_id, status="approved")
    if goal.status not in {"approved", "in_progress"}:
        store.set_goal_status(goal.goal_id, status="approved")
    if store.get_goal(goal.goal_id).status == "approved":
        store.set_goal_status(goal.goal_id, status="in_progress")
    try:
        detail = invoker(tool, args)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 — a failed read is a real, recorded failure
        failed = finish_goal(
            store,
            actor_id,
            principal_id,
            goal.goal_id,
            succeeded=False,
            detail=f"只读执行失败：{type(exc).__name__}",
            occurred_at_ms=now,
        )
        return failed, ""
    snippet = redact_text(str(detail) or "ok")[:200]
    succeeded = finish_goal(
        store,
        actor_id,
        principal_id,
        goal.goal_id,
        succeeded=True,
        detail=snippet,
        occurred_at_ms=now,
    )
    return succeeded, snippet


def _pick_tool(
    goal: Goal, decision: Decision, now: int
) -> tuple[str | None, dict[str, object]]:
    """Pick one read-only tool: the goal's own tool if set, else rotate through the day.

    A goal with no pinned tool is how the character "lives": each tick looks at a different
    part of its own data (B站 / 日记 / 浏览历史) so the material is not always the same.
    """
    for tool in goal.allowed_tools:
        if tool in ALLOWED_TOOLS:
            return tool, _args_for(tool, decision, now)
    rotation = _ROTATION[int(now // (6 * 60 * 60 * 1000)) % len(_ROTATION)]
    return rotation


def _args_for(tool: str, decision: Decision, now: int) -> dict[str, object]:
    candidate = decision.candidates[0] if decision.candidates else None
    kind = candidate.kind if candidate else ""
    mapped = _CANDIDATE_ACTIVITY.get(kind)
    if mapped is not None and mapped[0] == tool:
        return dict(mapped[1])
    if tool == "read_daily":
        return {"date": "today"}
    if tool == "bili_history":
        return {"limit": 5}
    if tool == "browser_history":
        return {"limit": 20, "days": 1}
    if tool == "search_daily":
        return {"query": (candidate.text if candidate else "最近")[:40]}
    return {}


def _fallback_expression(decision: Decision, detail: str) -> str:
    candidate = decision.candidates[0] if decision.candidates else None
    if candidate is not None and candidate.kind == "recent_activity":
        return redact_text(candidate.text)[:MAX_EXPRESSION_CHARS]
    snippet = redact_text(detail or "").strip()
    if not snippet:
        return ""
    return f"我刚看了一眼：{snippet[:80]}"


def _propose_small_goal(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    decision: Decision,
    now: int,
) -> Goal | None:
    """Turn a candidate into one tiny, whitelisted read-only goal (no model call)."""
    candidate = decision.candidates[0] if decision.candidates else None
    kind = candidate.kind if candidate else ""
    # Specific candidates pin a tool; generic ones leave it open so the rotation decides.
    mapped = _CANDIDATE_ACTIVITY.get(kind)
    tools: tuple[str, ...] = ()
    if mapped is not None and kind not in {"recent_activity", "goal_permission"}:
        if mapped[0] not in ALLOWED_TOOLS:
            return None
        tools = (mapped[0],)
    text = _goal_text(candidate)
    proposal = GoalProposal(
        text=text[:120],
        allowed_tools=tools,
        deadline_ms=min(
            now + ACTIVITY_DEADLINE_MINUTES * 60 * 1000,
            now + MAX_DEADLINE_DAYS * _DAY_MS,
        ),
        success_criteria="取到一条只读结果",
    )
    goal = create_goal(store, actor_id, principal_id, proposal)
    store.set_goal_status(goal.goal_id, status="approved")
    return store.get_goal(goal.goal_id)


def _goal_text(candidate: OutreachCandidate | None) -> str:
    if candidate is None:
        return "看一眼现在几点"
    if candidate.kind == "open_thread":
        return f"回看一下关于{candidate.text}的记录"
    if candidate.kind == "commitment_due":
        return "回看答应过的事的记录"
    if candidate.kind == "owner_concern":
        return "回看对方最近的状态记录"
    return "回看最近的记录"


def _quiet_now(now_ms: int) -> bool:
    """Second gate: delivery is refused inside quiet hours no matter who decided to talk."""
    return derive_day_phase(now_ms) == "quiet"


def _expression(decision: Decision) -> str:
    return redact_text((decision.message or "").strip())[:MAX_EXPRESSION_CHARS]


def _record_sent(
    store: CompanionStore, actor_id: str, principal_id: str, text: str, now: int
) -> str | None:
    """Persist the send as a real event + outreach row so it can be attributed later."""
    from gacore.companion.outreach import record_sent

    result = store.append_event(
        kind="proactive.message.sent",
        actor_id=actor_id,
        principal_id=principal_id,
        source_id=f"life:{now}",
        summary=redact_text(text),
        idempotency_key=f"life-sent:{actor_id}:{now}",
        occurred_at_ms=now,
    )
    if result.inserted:
        record_sent(
            store, actor_id, principal_id, sent_event_id=result.event_id
        )
        return result.event_id
    return None


def _sent_within(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    now: int,
    *,
    window_ms: int,
) -> int:
    return sum(
        1
        for event in store.events_for_actor(actor_id, principal_id)
        if event.kind == "proactive.message.sent" and event.occurred_at_ms >= now - window_ms
    )
