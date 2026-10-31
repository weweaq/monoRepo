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
    fail_activity,
    spend,
    start_activity,
)
from gacore.companion.goal import (
    ALLOWED_TOOLS,
    MAX_DEADLINE_DAYS,
    GoalProposal,
    approve_goal,
    create_goal,
    finish_goal,
    start_goal,
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
MAX_SENDS_PER_DAY: Final = 30  # fallback only
MAX_TICK_LLM_CALLS: Final = 1
# Per-tick hard caps: one model call, one read-only action, token ceilings and a deadline.
# Exceeding any of them fails the tick's activity and stops further work (design §16.4).
MAX_TICK_INPUT_TOKENS: Final = 8000
MAX_TICK_OUTPUT_TOKENS: Final = 1000
TICK_DEADLINE_MINUTES: Final = 5
MAX_LIFE_LLM_PER_DAY: Final = 8
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
    # Ordered by signal value: what the owner is actually watching/browsing beats a
    # daily-note replay (which is mostly ops lines and rarely worth telling anyone).
    ("bili_history", {"limit": 5}),
    ("browser_history", {"limit": 20, "days": 1}),
    ("read_daily", {"date": "yesterday"}),
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
    max_sends_per_day: int | None = None,
    min_gap_minutes: int | None = None,
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
            min_gap_minutes=min_gap_minutes,
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
    max_sends_per_day: int | None,
    min_gap_minutes: int | None,
    dry_run: bool,
) -> TickResult:
    return _run_tick(
        store,
        actor_id,
        principal_id,
        now=now,
        model=model,
        runner=runner,
        sender=sender,
        invoker=invoker,
        max_sends_per_day=max_sends_per_day,
        min_gap_minutes=min_gap_minutes,
        dry_run=dry_run,
    )


def _run_tick(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    now: int,
    model: object | None,
    runner: object | None,
    sender: object | None,
    invoker: object | None,
    max_sends_per_day: int | None,
    min_gap_minutes: int | None,
    dry_run: bool,
) -> TickResult:
    # SENSE + cheap refusals (0 LLM). Pacing knobs come from CompanionConfig unless the
    # caller overrides them explicitly (tests do).
    from gacore.companion.config import load_companion_config

    companion = load_companion_config()
    cap = companion.max_sends_per_day if max_sends_per_day is None else max_sends_per_day
    gap_ms = (
        companion.min_gap_minutes if min_gap_minutes is None else min_gap_minutes
    ) * 60 * 1000
    sent_today = _sent_within(store, actor_id, principal_id, now, window_ms=_DAY_MS)
    if sent_today >= cap:
        return TickResult(action="STAY_SILENT", reason="POLICY_BLOCKED")
    if not store.events_for_actor(actor_id, principal_id):
        return TickResult(action="STAY_SILENT", reason="NO_CANDIDATE")
    if _llm_calls_today(store, actor_id, principal_id, now) >= MAX_LIFE_LLM_PER_DAY:
        return TickResult(action="STAY_SILENT", reason="POLICY_BLOCKED", error="llm_budget")

    budget = _TickBudget(store, actor_id, principal_id, now)
    try:
        return _step(
            store,
            actor_id,
            principal_id,
            gate=build_gate_input(
                store, actor_id, principal_id, now_ms=now, min_gap_ms=gap_ms
            ),
            min_gap_ms=gap_ms,
            budget=budget,
            now=now,
            model=model,
            runner=runner,
            sender=sender,
            invoker=invoker,
            dry_run=dry_run,
        )
    finally:
        budget.close(now)


class _TickBudget:
    """Lazy per-tick budget: the activity row exists only if the tick really does work.

    A pure "nothing to say, nothing to do" tick therefore writes no activity events at all
    (design: empty ticks must not pollute the event log), while any model call or read
    action is charged against hard caps and fails the activity when exceeded.
    """

    def __init__(
        self, store: CompanionStore, actor_id: str, principal_id: str, now: int
    ) -> None:
        self.store = store
        self.actor_id = actor_id
        self.principal_id = principal_id
        self.now = now
        self.activity: object | None = None

    def _ensure(self) -> object:
        if self.activity is None:
            self.activity = start_activity(
                self.store,
                self.actor_id,
                self.principal_id,
                kind="life:tick",
                budget=ActivityBudget(
                    max_llm_calls=MAX_TICK_LLM_CALLS,
                    max_input_tokens=MAX_TICK_INPUT_TOKENS,
                    max_output_tokens=MAX_TICK_OUTPUT_TOKENS,
                    max_read_actions=ACTIVITY_MAX_TOOL_CALLS,
                    deadline_ms=self.now + TICK_DEADLINE_MINUTES * 60 * 1000,
                    cancel_on_user_message=True,
                ),
                now_ms=self.now,
            )
        return self.activity

    def charge(self, decision: Decision) -> bool:
        """Bill one decision; False means the tick must stop (budget exhausted)."""
        if not decision.llm_calls and not decision.input_tokens and not decision.output_tokens:
            return True
        activity = self._ensure()
        check = spend(
            self.store,
            self.actor_id,
            self.principal_id,
            activity.activity_id,  # type: ignore[attr-defined]
            llm_calls=decision.llm_calls,
            input_tokens=decision.input_tokens,
            output_tokens=decision.output_tokens,
            now_ms=self.now,
        )
        return check.allowed

    def spend_read(self) -> bool:
        """Bill one read-only action (creating the activity if this is the first work)."""
        self._ensure()
        check = spend(
            self.store,
            self.actor_id,
            self.principal_id,
            self.activity.activity_id,  # type: ignore[attr-defined]
            read_actions=1,
            now_ms=self.now,
        )
        return check.allowed

    def fail(self, reason: str) -> None:
        if self.activity is None:
            return
        current = self.store.get_activity(self.activity.activity_id)  # type: ignore[attr-defined]
        if current is not None and current.status in {"active", "paused"}:
            fail_activity(
                self.store,
                self.actor_id,
                self.principal_id,
                self.activity.activity_id,  # type: ignore[attr-defined]
                reason=reason,
                now_ms=self.now,
            )

    def close(self, now: int) -> None:
        if self.activity is None:
            return
        current = self.store.get_activity(self.activity.activity_id)  # type: ignore[attr-defined]
        if current is not None and current.status in {"active", "paused"}:
            complete_activity(
                self.store,
                self.actor_id,
                self.principal_id,
                self.activity.activity_id,  # type: ignore[attr-defined]
                now_ms=now,
            )


def _step(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    gate: object,
    min_gap_ms: int,
    budget: _TickBudget,
    now: int,
    model: object | None,
    runner: object | None,
    sender: object | None,
    invoker: object | None,
    dry_run: bool,
) -> TickResult:
    """One decision + action inside the tick's lazy budget."""
    decision = decide(
        store,
        actor_id,
        principal_id,
        gate=gate,  # type: ignore[arg-type]
        model=model,
        runner=runner,
        now_ms=now,
    )
    if not budget.charge(decision):
        return TickResult(
            action="STAY_SILENT",
            reason="POLICY_BLOCKED",
            llm_calls=decision.llm_calls,
            error="budget_exhausted",
        )

    if decision.action == "STAY_SILENT":
        maintenance = _maintenance_activity(
            store,
            actor_id,
            principal_id,
            gate=gate,
            now=now,
            invoker=invoker,
            dry_run=dry_run,
            budget=budget,
        )
        if maintenance is None:
            return TickResult(
                action="STAY_SILENT", reason=decision.reason, llm_calls=decision.llm_calls
            )
        if decision.llm_calls == 0 and decision.reason == "NO_CANDIDATE":
            decision = decide(
                store,
                actor_id,
                principal_id,
                gate=build_gate_input(
                    store, actor_id, principal_id, now_ms=now, min_gap_ms=min_gap_ms
                ),
                model=model,
                runner=runner,
                now_ms=now,
            )
            if not budget.charge(decision):
                return maintenance
            if decision.action == "STAY_SILENT":
                return maintenance
        else:
            return maintenance

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
            budget=budget,
        )
    return TickResult(
        action="STAY_SILENT", reason="POLICY_BLOCKED", llm_calls=decision.llm_calls
    )


def _llm_calls_today(
    store: CompanionStore, actor_id: str, principal_id: str, now: int
) -> int:
    total = 0
    for activity in store.activities(actor_id, principal_id):
        if activity.kind != "life:tick":
            continue
        started = activity.started_at_ms or 0
        if now - started > _DAY_MS:
            continue
        total += int(activity.spent.get("llm_calls", 0) or 0)
    return total


# --------------------------------------------------------------- own life (P2)

MAX_SILENT_ACTIVITIES_PER_DAY: Final = 3
MIN_ACTIVITY_GAP_MS: Final = 3 * 60 * 60 * 1000
OWNER_IDLE_BEFORE_ACT_MS: Final = 10 * 60 * 1000


def _maintenance_activity(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    gate: object,
    now: int,
    invoker: object | None,
    dry_run: bool,
    budget: _TickBudget,
) -> TickResult | None:
    """Do one small read-only thing when there is nothing to talk about.

    This is the "living" half of the loop: it still respects quiet hours, the owner being
    busy, a 3h minimum gap and 3 activities/day, and it never calls the model or speaks.
    """
    if invoker is None:
        return None
    if derive_day_phase(now) == "quiet":
        return None
    last_owner = getattr(gate, "last_owner_message_ms", None)
    if last_owner is not None and now - int(last_owner) < OWNER_IDLE_BEFORE_ACT_MS:
        return None

    history = [
        event
        for event in store.events_for_actor(actor_id, principal_id)
        if event.kind == "read_action.succeeded"
    ]
    if len([e for e in history if e.occurred_at_ms >= now - 24 * 60 * 60 * 1000]) >= (
        MAX_SILENT_ACTIVITIES_PER_DAY
    ):
        return None
    if history and now - history[-1].occurred_at_ms < MIN_ACTIVITY_GAP_MS:
        return None

    # 3h rotation: three views of its own life, one per maintenance slot, so consecutive
    # activities never look at the same source twice in a row.
    tool, args = _ROTATION[int(now // MIN_ACTIVITY_GAP_MS) % len(_ROTATION)]
    goal = _propose_small_goal(
        store, actor_id, principal_id, Decision(action="ACT_SILENTLY"), now
    )
    if goal is None:
        return None
    if dry_run:
        return TickResult(action="ACT_SILENTLY", reason="MAINTENANCE", goal_id=goal.goal_id)
    if not budget.spend_read():
        return None
    finished, detail = _execute(store, actor_id, principal_id, goal, tool, args, invoker, now)
    if finished.status != "succeeded":
        budget.fail("read_action_failed")
        return None
    logger.info(
        "companion silent activity",
        goal_id=finished.goal_id,
        tool=tool,
        chars=len(detail),
    )
    return TickResult(
        action="ACT_SILENTLY",
        reason="MAINTENANCE",
        goal_id=finished.goal_id,
        events=("read_action.succeeded",),
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
    _close_watch(store, decision, event_id)
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
    budget: _TickBudget,
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

    if not budget.spend_read():
        return TickResult(
            action="STAY_SILENT",
            reason="POLICY_BLOCKED",
            llm_calls=decision.llm_calls,
            error="budget_exhausted",
        )
    finished, detail = _execute(
        store, actor_id, principal_id, goal, tool, args, invoker, now
    )
    if finished.status != "succeeded":
        budget.fail("read_action_failed")

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
    """Run the goal's single read-only tool through the standard goal lifecycle.

    ``approve_goal``/``start_goal`` emit ``goal.started``, so every execution is visible
    in the event log exactly like a goal created by any other path.
    """
    if goal.status == "proposed":
        approve_goal(store, goal.goal_id)
    if store.get_goal(goal.goal_id).status == "approved":
        start_goal(store, actor_id, principal_id, goal.goal_id, occurred_at_ms=now)
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
        _settle_commitment(
            store, goal, succeeded=False, result_event_id=failed.result_event_id
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
    _settle_commitment(store, goal, succeeded=True, result_event_id=succeeded.result_event_id)
    return succeeded, snippet


def _settle_commitment(
    store: CompanionStore,
    goal: Goal,
    *,
    succeeded: bool,
    result_event_id: str | None,
) -> None:
    """A goal born from a promise settles that promise with the real result event.

    Only open promises are settled; an already cancelled/fulfilled one is left alone.
    """
    if not goal.commitment_id:
        return
    try:
        from gacore.companion.commitment import complete_commitment

        complete_commitment(
            store,
            goal.commitment_id,
            succeeded=succeeded,
            result_event_id=result_event_id,
        )
        logger.info(
            "companion commitment settled",
            commitment_id=goal.commitment_id,
            goal_id=goal.goal_id,
            succeeded=succeeded,
        )
    except Exception as exc:  # noqa: BLE001 — settlement must never break the tick
        logger.warning(
            "companion commitment settlement skipped",
            commitment_id=goal.commitment_id,
            error_type=type(exc).__name__,
        )


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
    rotation = _ROTATION[int(now // MIN_ACTIVITY_GAP_MS) % len(_ROTATION)]
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
    commitment_id = (
        candidate.ref_id if candidate is not None and candidate.kind == "commitment_due" else None
    )
    goal = create_goal(
        store, actor_id, principal_id, proposal, commitment_id=commitment_id
    )
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


def _close_watch(store: CompanionStore, decision: Decision, event_id: str | None) -> None:
    """A watch that we actually asked about is done, not pending forever."""
    if not decision.candidates:
        return
    candidate = decision.candidates[0]
    if candidate.kind != "watch_due" or not candidate.ref_id:
        return
    try:
        from gacore.companion import intent as intent_api

        intent_api.finish(store, candidate.ref_id, result_event_id=event_id)
    except Exception as exc:  # noqa: BLE001 — closing the card must not break the send
        logger.warning(
            "companion watch close skipped",
            intent_id=candidate.ref_id,
            error_type=type(exc).__name__,
        )


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
