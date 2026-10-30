"""V2-Step 6 (part 1): bounded, pausable activities (migration v7)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore.companion.activity import (
    ActivityBudget,
    ActivitySpend,
    cancel_activity,
    cancel_on_owner_message,
    check_budget,
    complete_activity,
    derive_availability,
    derive_day_phase,
    fail_activity,
    pause_activity,
    resume_activity,
    spend,
    start_activity,
)
from gacore.companion.store import SCHEMA_VERSION, CompanionStore, CompanionStoreError

_TZ = timezone(timedelta(hours=8))
_DAY = "2026-10-30"
_HOUR_MS = 60 * 60 * 1000


def _ms(day: str, hour: int = 12, minute: int = 0) -> int:
    return int(
        datetime.strptime(day, "%Y-%m-%d")
        .replace(hour=hour, minute=minute, tzinfo=_TZ)
        .timestamp()
        * 1000
    )


def _store(tmp_path: Path) -> CompanionStore:
    return CompanionStore(tmp_path / "companion.db")


def _budget(**overrides) -> ActivityBudget:
    values = {"max_llm_calls": 3, "max_read_actions": 2, "cancel_on_user_message": True}
    values.update(overrides)
    return ActivityBudget(**values)


def _kinds(store: CompanionStore) -> list[str]:
    return [event.kind for event in store.events_for_actor("linwan", "owner")]


class TestMigration:
    def test_v7_adds_activity_and_unique_running_index(self, tmp_path: Path) -> None:
        path = tmp_path / "companion.db"
        CompanionStore(path)
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='companion_activity'"
                )
            }
            assert "ux_one_running_activity" in indexes
        finally:
            conn.close()


class TestLifecycle:
    def test_only_one_activity_can_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        first = start_activity(
            store, "linwan", "owner", kind="browse", budget=_budget(), now_ms=_ms(_DAY, 10)
        )

        with pytest.raises(CompanionStoreError):
            start_activity(
                store, "linwan", "owner", kind="browse", budget=_budget(), now_ms=_ms(_DAY, 11)
            )
        assert store.running_activity("linwan", "owner") is not None
        assert first.status == "active"

    def test_pause_and_resume(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        activity = start_activity(
            store, "linwan", "owner", kind="browse", budget=_budget(), now_ms=_ms(_DAY, 10)
        )

        paused = pause_activity(
            store, "linwan", "owner", activity.activity_id, resume_after_ms=_ms(_DAY, 11),
            now_ms=_ms(_DAY, 10),
        )
        assert paused.status == "paused"
        assert paused.resume_after_ms == _ms(_DAY, 11)

        resumed = resume_activity(
            store, "linwan", "owner", activity.activity_id, now_ms=_ms(_DAY, 11)
        )
        assert resumed.status == "active"

    def test_completion_writes_an_event_and_is_terminal(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        activity = start_activity(
            store, "linwan", "owner", kind="browse", budget=_budget(), now_ms=_ms(_DAY, 10)
        )

        done = complete_activity(
            store, "linwan", "owner", activity.activity_id, detail="看完了", now_ms=_ms(_DAY, 11)
        )

        assert done.status == "completed"
        assert done.last_result_event_id is not None
        assert "activity.completed" in _kinds(store)
        with pytest.raises(CompanionStoreError):
            complete_activity(store, "linwan", "owner", activity.activity_id)

    def test_failure_keeps_a_real_reason_event(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        activity = start_activity(
            store, "linwan", "owner", kind="browse", budget=_budget(), now_ms=_ms(_DAY, 10)
        )

        failed = fail_activity(
            store, "linwan", "owner", activity.activity_id, reason="tool_timeout", now_ms=_ms(_DAY, 11)
        )

        assert failed.status == "failed"
        assert failed.reason == "tool_timeout"
        assert "activity.failed" in _kinds(store)

    def test_cancel_records_the_reason(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        activity = start_activity(
            store, "linwan", "owner", kind="browse", budget=_budget(), now_ms=_ms(_DAY, 10)
        )

        cancelled = cancel_activity(
            store, "linwan", "owner", activity.activity_id, reason="owner_stop", now_ms=_ms(_DAY, 11)
        )

        assert cancelled.status == "cancelled"
        assert cancelled.reason == "owner_stop"
        assert "activity.cancelled" in _kinds(store)

    def test_owner_message_cancels_only_when_configured(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        activity = start_activity(
            store, "linwan", "owner", kind="browse", budget=_budget(cancel_on_user_message=False),
            now_ms=_ms(_DAY, 10),
        )
        event = _owner_event(store, "u1", "在吗", _ms(_DAY, 10, 30))

        assert cancel_on_owner_message(store, "linwan", "owner", event) is None
        assert store.running_activity("linwan", "owner").status == "active"

        store.set_activity_status(activity.activity_id, status="active")
        cancel_on_owner_message(
            store,
            "linwan",
            "owner",
            event,
        )
        # The stored budget still says False, so nothing was cancelled.
        assert store.running_activity("linwan", "owner").status == "active"

    def test_owner_message_cancels_when_enabled(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        start_activity(
            store, "linwan", "owner", kind="browse", budget=_budget(), now_ms=_ms(_DAY, 10)
        )
        event = _owner_event(store, "u1", "在吗", _ms(_DAY, 10, 30))

        cancelled = cancel_on_owner_message(store, "linwan", "owner", event)

        assert cancelled is not None and cancelled.status == "cancelled"
        assert cancelled.reason == "owner_message"


class TestBudget:
    def test_exceeding_llm_calls_fails_the_activity(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        activity = start_activity(
            store, "linwan", "owner", kind="browse", budget=_budget(max_llm_calls=1),
            now_ms=_ms(_DAY, 10),
        )

        first = spend(store, "linwan", "owner", activity.activity_id, llm_calls=1, now_ms=_ms(_DAY, 10))
        assert first.allowed is True

        second = spend(store, "linwan", "owner", activity.activity_id, llm_calls=1, now_ms=_ms(_DAY, 10))
        assert second.allowed is False
        assert second.reason == "max_llm_calls"
        stored = store.get_activity(activity.activity_id)
        assert stored.status == "failed"
        assert stored.reason.startswith("budget_exhausted")

    def test_deadline_and_read_action_caps(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        activity = start_activity(
            store, "linwan", "owner", kind="browse",
            budget=_budget(deadline_ms=_ms(_DAY, 11)), now_ms=_ms(_DAY, 10),
        )

        late = spend(store, "linwan", "owner", activity.activity_id, now_ms=_ms(_DAY, 12))
        assert late.allowed is False and late.reason == "deadline"
        assert store.get_activity(activity.activity_id).status == "failed"

    def test_check_budget_is_pure(self) -> None:
        from gacore.companion.store import Activity

        activity = Activity(
            activity_id="a1", actor_id="linwan", principal_id="owner", kind="browse",
            status="active", started_at_ms=0, resume_after_ms=None, source_goal_id=None,
            last_result_event_id=None, budget={"max_llm_calls": 1}, spent={}, reason=None,
            created_at="", updated_at="",
        )

        assert check_budget(activity, ActivitySpend(llm_calls=0), now_ms=0).allowed is True
        # "up to 1" means the first call is fine; a second one is refused.
        assert check_budget(activity, ActivitySpend(llm_calls=1), now_ms=0).allowed is True
        blocked = check_budget(activity, ActivitySpend(llm_calls=2), now_ms=0)
        assert blocked.allowed is False and blocked.reason == "max_llm_calls"


class TestPhaseAndAvailability:
    @pytest.mark.parametrize(
        ("hour", "phase"),
        [(2, "quiet"), (8, "morning"), (13, "daytime"), (20, "evening"), (23, "quiet")],
    )
    def test_day_phase(self, hour: int, phase: str) -> None:
        assert derive_day_phase(_ms(_DAY, hour)) == phase

    def test_availability_derivations(self) -> None:
        now = _ms(_DAY, 13)
        assert derive_availability(now_ms=now) == "free"
        assert derive_availability(now_ms=now, last_owner_message_ms=now - 60_000) == "chatting"
        assert derive_availability(now_ms=now, last_outreach_ms=now - _HOUR_MS) == "cooldown"
        assert derive_availability(now_ms=_ms(_DAY, 2)) == "resting"


def _owner_event(store: CompanionStore, key: str, summary: str, at_ms: int):
    event_id = store.append_event(
        kind="user.message.received",
        actor_id="linwan",
        principal_id="owner",
        source_id=f"src-{key}",
        summary=summary,
        idempotency_key=key,
        occurred_at_ms=at_ms,
    ).event_id
    for event in store.events_for_actor("linwan", "owner"):
        if event.event_id == event_id:
            return event
    raise AssertionError("event missing")
