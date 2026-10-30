"""Bounded, pausable activities (V2-Step 6, migration v7).

Design: ``docs/companion-self-memory-v2-plan.md`` V2-Step 6. An activity is the only
place where the character spends resources over time, so it always carries a budget:

    max_llm_calls / max_input_tokens / max_output_tokens / max_read_actions
    deadline_ms / cancel_on_user_message / daily_cost_limit

Exceeding any budget stops the activity as ``failed`` (or ``cancelled`` for a user-driven
stop) and writes a real event with the reason — never a silent retry with a bigger budget.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final

from gacore.companion.store import (
    Activity,
    CompanionStore,
    CompanionStoreError,
    CompanionEvent,
)
from gacore.jsonl_logger import get_logger
from gacore.redact import redact_text

logger = get_logger("companion.activity")

_TZ: Final = timezone(timedelta(hours=8))
_HOUR_MS: Final = 60 * 60 * 1000
_DAY_MS: Final = 24 * _HOUR_MS

DAY_PHASES: Final = frozenset({"quiet", "morning", "daytime", "evening"})
AVAILABILITY: Final = frozenset({"free", "chatting", "working", "cooldown", "resting"})

QUIET_START_HOUR: Final = 23
QUIET_END_HOUR: Final = 7
CHATTING_WINDOW_MS: Final = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class ActivityBudget:
    """Hard caps for one activity; a missing cap means "not bounded by this one"."""

    max_llm_calls: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_read_actions: int = 0
    deadline_ms: int = 0
    cancel_on_user_message: bool = True
    daily_cost_limit: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ActivitySpend:
    """Mutable counters mirrored into ``companion_activity.spent_json``."""

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    read_actions: int = 0
    daily_cost: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    """Outcome of one bounded step: whether the activity may continue."""

    allowed: bool
    reason: str | None = None
    exhausted: tuple[str, ...] = field(default_factory=tuple)


def derive_day_phase(now_ms: int) -> str:
    """Coarse product-local day phase (quiet includes the small hours)."""
    hour = datetime.fromtimestamp(now_ms / 1000, tz=_TZ).hour
    if hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR:
        return "quiet"
    if hour < 11:
        return "morning"
    if hour < 18:
        return "daytime"
    return "evening"


def derive_availability(
    *,
    now_ms: int,
    last_owner_message_ms: int | None = None,
    last_outreach_ms: int | None = None,
    cooldown_ms: int = 3 * _HOUR_MS,
    running_activity: Activity | None = None,
) -> str:
    """Deterministic availability for gating; never guesses, only derives from timestamps."""
    if running_activity is not None and running_activity.status == "active":
        return "working"
    if last_owner_message_ms is not None and now_ms - last_owner_message_ms <= CHATTING_WINDOW_MS:
        return "chatting"
    if last_outreach_ms is not None and now_ms - last_outreach_ms < cooldown_ms:
        return "cooldown"
    if derive_day_phase(now_ms) == "quiet":
        return "resting"
    return "free"


def start_activity(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    kind: str,
    budget: ActivityBudget,
    source_goal_id: str | None = None,
    now_ms: int | None = None,
) -> Activity:
    """Start the single running activity (a second one is refused by the store)."""
    now = _now(now_ms)
    activity = store.add_activity(
        actor_id=actor_id,
        principal_id=principal_id,
        kind=kind,
        budget=budget.to_dict(),
        source_goal_id=source_goal_id,
        started_at_ms=now,
    )
    _emit(store, "activity.started", actor_id, principal_id, activity, reason=f"kind={kind}", now_ms=now)
    logger.info("companion activity started", actor_id=actor_id, kind=kind)
    return activity


def pause_activity(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    activity_id: str,
    *,
    resume_after_ms: int | None = None,
    now_ms: int | None = None,
) -> Activity:
    """Pause a running activity; ``resume_after_ms`` is an advisory, not a timer."""
    activity = _require(store, activity_id)
    if activity.status != "active":
        raise CompanionStoreError(f"activity {activity_id!r} is {activity.status!r}, not active")
    paused = store.set_activity_status(
        activity_id, status="paused", resume_after_ms=resume_after_ms
    )
    _emit(store, "activity.paused", actor_id, principal_id, paused, reason="paused", now_ms=now_ms)
    return paused


def resume_activity(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    activity_id: str,
    *,
    now_ms: int | None = None,
) -> Activity:
    """Resume a paused activity (the unique index still allows only one runner)."""
    activity = _require(store, activity_id)
    if activity.status != "paused":
        raise CompanionStoreError(f"activity {activity_id!r} is {activity.status!r}, not paused")
    resumed = store.set_activity_status(activity_id, status="active")
    _emit(store, "activity.resumed", actor_id, principal_id, resumed, reason="resumed", now_ms=now_ms)
    return resumed


def complete_activity(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    activity_id: str,
    *,
    detail: str = "completed",
    now_ms: int | None = None,
) -> Activity:
    """Finish successfully; a terminal activity is never re-run after a restart."""
    activity = _require(store, activity_id)
    if activity.status not in {"active", "paused"}:
        raise CompanionStoreError(f"activity {activity_id!r} is {activity.status!r}")
    event_id = _emit_event(
        store, "activity.completed", actor_id, principal_id, activity, detail, now_ms
    )
    return store.set_activity_status(
        activity_id, status="completed", last_result_event_id=event_id
    )


def fail_activity(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    activity_id: str,
    *,
    reason: str,
    now_ms: int | None = None,
) -> Activity:
    """Finish as failed with a real reason event (never hidden behind "character quiet")."""
    event_id = _emit_event(
        store, "activity.failed", actor_id, principal_id, _require(store, activity_id), reason, now_ms
    )
    return store.set_activity_status(
        activity_id, status="failed", reason=reason, last_result_event_id=event_id
    )


def cancel_activity(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    activity_id: str,
    *,
    reason: str = "cancelled",
    now_ms: int | None = None,
) -> Activity:
    """Cancel a planned/running activity (user stop or budget refusal)."""
    event_id = _emit_event(
        store, "activity.cancelled", actor_id, principal_id, _require(store, activity_id), reason, now_ms
    )
    return store.set_activity_status(
        activity_id, status="cancelled", reason=reason, last_result_event_id=event_id
    )


def cancel_on_owner_message(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    owner_event: CompanionEvent,
) -> Activity | None:
    """A new owner message stops a cancel-on-message activity (the owner takes priority)."""
    activity = store.running_activity(actor_id, principal_id)
    if activity is None:
        return None
    if not bool(activity.budget.get("cancel_on_user_message", False)):
        return None
    return cancel_activity(
        store,
        actor_id,
        principal_id,
        activity.activity_id,
        reason="owner_message",
        now_ms=owner_event.occurred_at_ms,
    )


def check_budget(
    activity: Activity,
    spend: ActivitySpend,
    *,
    now_ms: int,
) -> BudgetCheck:
    """Read-only budget check (the caller decides whether to persist a stop)."""
    budget = activity.budget
    exhausted: list[str] = []
    if budget.get("deadline_ms") and now_ms >= int(budget["deadline_ms"]):
        exhausted.append("deadline")
    # "Up to N" semantics: spending the Nth unit is fine, the next one is refused.
    for field_name in ("max_llm_calls", "max_input_tokens", "max_output_tokens", "max_read_actions"):
        cap = int(budget.get(field_name) or 0)
        if cap and int(getattr(spend, _spend_field(field_name))) > cap:
            exhausted.append(field_name)
    daily = int(budget.get("daily_cost_limit") or 0)
    if daily and spend.daily_cost > daily:
        exhausted.append("daily_cost_limit")
    if exhausted:
        return BudgetCheck(allowed=False, reason=exhausted[0], exhausted=tuple(exhausted))
    return BudgetCheck(allowed=True)


def spend(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    activity_id: str,
    *,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    read_actions: int = 0,
    daily_cost: int = 0,
    now_ms: int | None = None,
) -> BudgetCheck:
    """Record one bounded step; exceeding a cap fails the activity immediately."""
    now = _now(now_ms)
    activity = _require(store, activity_id)
    current = ActivitySpend(
        llm_calls=int(activity.spent.get("llm_calls", 0)),
        input_tokens=int(activity.spent.get("input_tokens", 0)),
        output_tokens=int(activity.spent.get("output_tokens", 0)),
        read_actions=int(activity.spent.get("read_actions", 0)),
        daily_cost=int(activity.spent.get("daily_cost", 0)),
    )
    current.llm_calls += llm_calls
    current.input_tokens += input_tokens
    current.output_tokens += output_tokens
    current.read_actions += read_actions
    current.daily_cost += daily_cost
    updated = store.update_activity_spend(activity_id, current.to_dict())

    check = check_budget(updated, current, now_ms=now)
    if not check.allowed:
        fail_activity(
            store,
            actor_id,
            principal_id,
            activity_id,
            reason=f"budget_exhausted:{check.reason}",
            now_ms=now,
        )
        logger.warning(
            "companion activity stopped: budget exhausted",
            activity_id=activity_id,
            budget=check.exhausted,
        )
    return check


# ------------------------------------------------------------------------- helpers


def _spend_field(budget_field: str) -> str:
    return budget_field.removeprefix("max_")


def _require(store: CompanionStore, activity_id: str) -> Activity:
    activity = store.get_activity(activity_id)
    if activity is None:
        raise CompanionStoreError(f"unknown activity: {activity_id!r}")
    return activity


def _emit(
    store: CompanionStore,
    kind: str,
    actor_id: str,
    principal_id: str,
    activity: Activity,
    *,
    reason: str,
    now_ms: int | None,
) -> str:
    return _emit_event(store, kind, actor_id, principal_id, activity, reason, now_ms)


def _emit_event(
    store: CompanionStore,
    kind: str,
    actor_id: str,
    principal_id: str,
    activity: Activity,
    reason: str,
    now_ms: int | None,
) -> str:
    result = store.append_event(
        kind=kind,
        actor_id=actor_id,
        principal_id=principal_id,
        source_id=f"{kind}:{activity.activity_id}",
        summary=redact_text(f"{activity.kind} {reason}"[:200]),
        idempotency_key=f"{kind}:{activity.activity_id}",
        occurred_at_ms=_now(now_ms),
    )
    return result.event_id


def _now(now_ms: int | None) -> int:
    if now_ms is not None:
        return int(now_ms)
    return int(datetime.now(_TZ).timestamp() * 1000)
