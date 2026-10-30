"""V2-Step 5: one small read-only goal per actor (migration v6).

Acceptance from ``docs/companion-self-memory-v2-plan.md`` Step 5: concurrent creation can
never produce two active goals, non-whitelisted tools are refused, a restart never re-runs
a finished goal, and a failure always carries a real result event.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore.companion.commitment import parse_due_ms
from gacore.companion.goal import (
    ALLOWED_TOOLS,
    MAX_DEADLINE_DAYS,
    approve_goal,
    cancel_goal,
    create_goal,
    finish_goal,
    goal_view,
    propose_goal,
    start_goal,
    validate_proposal,
)
from gacore.companion.store import (
    SCHEMA_VERSION,
    CompanionStore,
    CompanionStoreError,
)

_TZ = timezone(timedelta(hours=8))
_DAY = "2026-10-30"
_DAY_MS = 24 * 60 * 60 * 1000


def _ms(day: str, hour: int = 12, minute: int = 0) -> int:
    return int(
        datetime.strptime(day, "%Y-%m-%d")
        .replace(hour=hour, minute=minute, tzinfo=_TZ)
        .timestamp()
        * 1000
    )


def _store(tmp_path: Path) -> CompanionStore:
    return CompanionStore(tmp_path / "companion.db")


def _proposal(**overrides):
    payload = {
        "text": "查一下最近的 B 站观看记录",
        "allowed_tools": ["bili_history"],
        "deadline_text": "明天",
        "success_criteria": "给出三条记录",
    }
    payload.update(overrides)
    return payload


def _valid(now_ms: int, **overrides):
    proposal = validate_proposal({"goal": _proposal(**overrides)}, now_ms=now_ms)
    assert proposal is not None
    return proposal


class TestMigration:
    def test_v6_adds_goals_and_the_single_active_index(self, tmp_path: Path) -> None:
        path = tmp_path / "companion.db"
        CompanionStore(path)
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='companion_goal'"
                )
            }
            assert "ux_one_active_goal" in indexes
        finally:
            conn.close()

    def test_goal_lifecycle_event_kinds_are_registered(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for kind in (
            "goal.started",
            "read_action.succeeded",
            "read_action.failed",
            "goal.succeeded",
            "goal.failed",
            "goal.cancelled",
        ):
            store.append_event(
                kind=kind,
                actor_id="linwan",
                principal_id="owner",
                source_id=f"src-{kind}",
                summary="x",
                idempotency_key=f"key-{kind}",
            )


class TestValidation:
    def test_null_goal_is_none(self) -> None:
        assert validate_proposal({"goal": None}, now_ms=_ms(_DAY)) is None

    def test_non_whitelisted_tool_is_refused(self) -> None:
        assert (
            validate_proposal(
                {"goal": _proposal(allowed_tools=["code_run"])}, now_ms=_ms(_DAY)
            )
            is None
        )
        assert "code_run" not in ALLOWED_TOOLS
        assert "file_write" not in ALLOWED_TOOLS
        assert "send_email" not in ALLOWED_TOOLS
        assert "qq_push" not in ALLOWED_TOOLS

    @pytest.mark.parametrize(
        "overrides",
        [
            {"text": "长" * 200},
            {"allowed_tools": []},
            {"allowed_tools": ["bili_history", "browser_history", "get_time", "web_scan", "last_seen"]},
            {"deadline_text": "回头"},
            {"deadline_text": "30天后"},
            {"success_criteria": "  "},
        ],
    )
    def test_invalid_proposals_are_refused(self, overrides) -> None:
        assert validate_proposal({"goal": _proposal(**overrides)}, now_ms=_ms(_DAY)) is None

    def test_deadline_is_parsed_by_code(self) -> None:
        proposal = _valid(_ms(_DAY))

        assert proposal.deadline_ms == parse_due_ms("明天", _ms(_DAY))
        assert proposal.deadline_ms <= _ms(_DAY) + MAX_DEADLINE_DAYS * _DAY_MS

    def test_propose_goal_uses_the_runner_and_returns_none_for_null(self) -> None:
        proposal = propose_goal(
            "明天帮你看看", runner=lambda _text: {"goal": _proposal()}, now_ms=_ms(_DAY)
        )
        assert proposal is not None and proposal.allowed_tools == ("bili_history",)

        assert (
            propose_goal("随便聊聊", runner=lambda _text: {"goal": None}, now_ms=_ms(_DAY))
            is None
        )


class TestLifecycle:
    def test_approve_start_finish_writes_events(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        proposal = _valid(_ms(_DAY))
        goal = create_goal(store, "linwan", "owner", proposal)
        assert goal.status == "proposed"

        approve_goal(store, goal.goal_id)
        running = start_goal(store, "linwan", "owner", goal.goal_id, occurred_at_ms=_ms(_DAY, 10))
        assert running.status == "in_progress"
        assert store.active_goal("linwan", "owner") is not None

        done = finish_goal(
            store,
            "linwan",
            "owner",
            goal.goal_id,
            succeeded=True,
            detail="查到三条 B 站记录",
            occurred_at_ms=_ms(_DAY, 11),
        )

        assert done.status == "succeeded"
        assert done.result_event_id is not None
        assert store.active_goal("linwan", "owner") is None
        kinds = [event.kind for event in store.events_for_actor("linwan", "owner")]
        assert "goal.started" in kinds
        assert "read_action.succeeded" in kinds
        assert "goal.succeeded" in kinds

    def test_failure_also_carries_a_real_result_event(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        goal = create_goal(store, "linwan", "owner", _valid(_ms(_DAY)))
        approve_goal(store, goal.goal_id)
        start_goal(store, "linwan", "owner", goal.goal_id, occurred_at_ms=_ms(_DAY, 10))

        failed = finish_goal(
            store,
            "linwan",
            "owner",
            goal.goal_id,
            succeeded=False,
            detail="接口超时",
            occurred_at_ms=_ms(_DAY, 11),
        )

        assert failed.status == "failed"
        assert failed.result_event_id is not None
        kinds = [event.kind for event in store.events_for_actor("linwan", "owner")]
        assert "read_action.failed" in kinds and "goal.failed" in kinds

    def test_second_active_goal_is_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        first = create_goal(store, "linwan", "owner", _valid(_ms(_DAY)))
        second = create_goal(
            store,
            "linwan",
            "owner",
            _valid(_ms(_DAY), text="看看今天的天气"),
        )
        approve_goal(store, first.goal_id)
        approve_goal(store, second.goal_id)
        start_goal(store, "linwan", "owner", first.goal_id, occurred_at_ms=_ms(_DAY, 10))

        with pytest.raises(CompanionStoreError):
            start_goal(store, "linwan", "owner", second.goal_id, occurred_at_ms=_ms(_DAY, 10))

    def test_finished_goal_is_never_restarted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        goal = create_goal(store, "linwan", "owner", _valid(_ms(_DAY)))
        approve_goal(store, goal.goal_id)
        start_goal(store, "linwan", "owner", goal.goal_id, occurred_at_ms=_ms(_DAY, 10))
        finish_goal(
            store,
            "linwan",
            "owner",
            goal.goal_id,
            succeeded=True,
            detail="done",
            occurred_at_ms=_ms(_DAY, 11),
        )

        with pytest.raises(CompanionStoreError):
            start_goal(store, "linwan", "owner", goal.goal_id, occurred_at_ms=_ms(_DAY, 12))
        assert store.active_goal("linwan", "owner") is None

    def test_finish_requires_in_progress(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        goal = create_goal(store, "linwan", "owner", _valid(_ms(_DAY)))
        approve_goal(store, goal.goal_id)

        with pytest.raises(CompanionStoreError):
            finish_goal(
                store, "linwan", "owner", goal.goal_id, succeeded=True, detail="过早收尾"
            )

    def test_cancel_records_an_event_and_is_terminal(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        goal = create_goal(store, "linwan", "owner", _valid(_ms(_DAY)))

        cancelled = cancel_goal(
            store, "linwan", "owner", goal.goal_id, reason="主人改主意", occurred_at_ms=_ms(_DAY, 10)
        )

        assert cancelled.status == "cancelled"
        assert "goal.cancelled" in [
            event.kind for event in store.events_for_actor("linwan", "owner")
        ]
        with pytest.raises(CompanionStoreError):
            cancel_goal(store, "linwan", "owner", goal.goal_id)

    def test_view_lists_goals(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        create_goal(store, "linwan", "owner", _valid(_ms(_DAY)))

        view = goal_view(store, "linwan", "owner")

        assert len(view) == 1
        assert view[0]["status"] == "proposed"
        assert view[0]["allowed_tools"] == ["bili_history"]


class TestPrompt:
    def test_prompt_declares_tools_and_schema(self) -> None:
        import gacore.companion.goal as module

        prompt = module._SYSTEM_PROMPT
        for tool in sorted(ALLOWED_TOOLS):
            assert tool in prompt, f"prompt must mention the allowed tool {tool}"
        for key in ('"goal"', '"text"', '"allowed_tools"', '"deadline_text"', '"success_criteria"'):
            assert key in prompt, f"prompt must declare {key}"
