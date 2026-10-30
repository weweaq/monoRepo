"""V2-Step 6 (part 2): the controlled proactive loop (decide + feedback)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore.companion.outreach import (
    MAX_MESSAGE_CHARS,
    GateInput,
    classify_reply,
    collect_candidates,
    decide,
    evaluate_gate,
    execute_read_only_goal,
    match_reply,
    outreach_multiplier,
    permission_token,
    record_sent,
)
from gacore.companion.store import CompanionStore

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


def _event(
    store: CompanionStore,
    key: str,
    *,
    kind: str = "user.message.received",
    summary: str = "在吗",
    at_ms: int | None = None,
):
    event_id = store.append_event(
        kind=kind,
        actor_id="linwan",
        principal_id="owner",
        source_id=f"src-{key}",
        summary=summary,
        idempotency_key=key,
        occurred_at_ms=_ms(_DAY, 10) if at_ms is None else at_ms,
    ).event_id
    for event in store.events_for_actor("linwan", "owner"):
        if event.event_id == event_id:
            return event
    raise AssertionError("event missing")


def _open_thread(store: CompanionStore, key: str, topic: str = "换工作") -> None:
    source = _event(store, key, summary=f"你觉得我该{topic}吗", at_ms=_ms(_DAY, 9))
    store.add_open_thread(
        actor_id="linwan", principal_id="owner", topic=topic, source_event_id=source.event_id
    )


def _gate(now_ms: int, **overrides) -> GateInput:
    values = {"now_ms": now_ms, "daily_count": 0, "max_daily": 3}
    values.update(overrides)
    return GateInput(**values)


class TestCandidates:
    def test_no_candidates_means_no_model_call(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        called: list[int] = []

        decision = decide(
            store,
            "linwan",
            "owner",
            gate=_gate(_ms(_DAY, 10)),
            runner=lambda _c: called.append(1),
        )

        assert decision.action == "STAY_SILENT"
        assert decision.reason == "NO_CANDIDATE"
        assert decision.llm_calls == 0
        assert called == []

    def test_open_thread_becomes_a_candidate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")

        candidates = collect_candidates(store, "linwan", "owner", now_ms=_ms(_DAY, 10))

        assert len(candidates) == 1
        assert candidates[0].kind == "open_thread"
        assert candidates[0].source_ids


class TestGate:
    @pytest.mark.parametrize(
        ("hour", "overrides", "reason"),
        [
            (2, {}, "QUIET_HOURS"),
            (10, {"last_owner_message_ms": _ms(_DAY, 10)}, "USER_BUSY"),
            (10, {"daily_count": 3}, "POLICY_BLOCKED"),
            (10, {"last_outreach_ms": _ms(_DAY, 10) - 60 * 60 * 1000}, "TOO_REPETITIVE"),
            (10, {"no_response_streak": 3}, "LOW_VALUE"),
        ],
    )
    def test_gate_refusals_carry_reason_codes(self, hour, overrides, reason) -> None:
        result = evaluate_gate(_gate(_ms(_DAY, hour), **overrides))

        assert result.allowed is False
        assert result.reason == reason

    def test_gate_allows_a_free_afternoon(self) -> None:
        assert evaluate_gate(_gate(_ms(_DAY, 15))).allowed is True

    def test_quiet_gate_never_calls_the_model(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")
        called: list[int] = []

        decision = decide(
            store,
            "linwan",
            "owner",
            gate=_gate(_ms(_DAY, 2)),
            runner=lambda _c: called.append(1),
        )

        assert decision.action == "STAY_SILENT"
        assert decision.reason == "QUIET_HOURS"
        assert decision.llm_calls == 0 and called == []


class TestDecision:
    def test_tell_only_carries_a_short_message(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")

        decision = decide(
            store,
            "linwan",
            "owner",
            gate=_gate(_ms(_DAY, 15)),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "上次那个换工作的事，想好了吗"},
        )

        assert decision.action == "TELL_ONLY"
        assert 0 < len(decision.message) <= MAX_MESSAGE_CHARS
        assert decision.llm_calls == 1

    def test_long_message_is_truncated_not_sent_whole(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")

        decision = decide(
            store,
            "linwan",
            "owner",
            gate=_gate(_ms(_DAY, 15)),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "很" * 500},
        )

        assert len(decision.message) == MAX_MESSAGE_CHARS

    def test_stay_silent_has_no_message(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")

        decision = decide(
            store,
            "linwan",
            "owner",
            gate=_gate(_ms(_DAY, 15)),
            runner=lambda _c: {"action": "STAY_SILENT", "reason": "LOW_VALUE"},
        )

        assert decision.action == "STAY_SILENT"
        assert decision.reason == "LOW_VALUE"
        assert decision.message == ""

    def test_system_failure_writes_no_narrative(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")
        before = len(store.events_for_actor("linwan", "owner"))

        def boom(_candidates):
            raise RuntimeError("provider down")

        decision = decide(
            store, "linwan", "owner", gate=_gate(_ms(_DAY, 15)), runner=boom
        )

        assert decision.action == "STAY_SILENT"
        assert decision.reason == "SYSTEM_FAILURE"
        after = len(store.events_for_actor("linwan", "owner"))
        assert after == before, "a system failure must not become a character utterance"

    def test_unknown_action_is_a_system_failure(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")

        decision = decide(
            store,
            "linwan",
            "owner",
            gate=_gate(_ms(_DAY, 15)),
            runner=lambda _c: {"action": "SEND_EMAIL"},
        )

        assert decision.action == "STAY_SILENT" and decision.reason == "SYSTEM_FAILURE"


class TestPermissionAndGoalPolicy:
    def _goal(self, store: CompanionStore, *, status: str):
        _event(store, f"g-{status}", kind="agent.message.sent", summary="明天帮你查")
        return store.add_goal(
            actor_id="linwan",
            principal_id="owner",
            text="查一下 B 站观看记录",
            allowed_tools=["bili_history"],
            deadline_ms=_ms(_DAY, 23),
            status=status,
            commitment_id=None,
        )

    def test_act_read_only_before_approval_becomes_ask_permission(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")
        goal = self._goal(store, status="proposed")

        decision = decide(
            store,
            "linwan",
            "owner",
            gate=_gate(_ms(_DAY, 15)),
            runner=lambda _c: {"action": "ACT_READ_ONLY", "goal_id": goal.goal_id},
        )

        assert decision.action == "ASK_PERMISSION"
        assert decision.token is not None
        assert decision.token["goal_id"] == goal.goal_id
        assert decision.token["approved_by"] is None
        assert decision.token["status"] == "pending"
        # Not executed: the goal is still proposed.
        assert store.get_goal(goal.goal_id).status == "proposed"

    def test_act_read_only_runs_only_with_approval_and_whitelisted_tool(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        _open_thread(store, "u1")
        goal = self._goal(store, status="approved")

        decision = decide(
            store,
            "linwan",
            "owner",
            gate=_gate(_ms(_DAY, 15)),
            runner=lambda _c: {"action": "ACT_READ_ONLY", "goal_id": goal.goal_id},
        )
        assert decision.action == "ACT_READ_ONLY"

        finished = execute_read_only_goal(
            store,
            "linwan",
            "owner",
            goal.goal_id,
            tool="bili_history",
            invoker=lambda tool: f"{tool} ok",
            now_ms=_ms(_DAY, 16),
        )
        assert finished.status == "succeeded"
        assert "read_action.succeeded" in [
            event.kind for event in store.events_for_actor("linwan", "owner")
        ]

    def test_tool_outside_the_goal_is_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        goal = self._goal(store, status="approved")
        events_before = len(store.events_for_actor("linwan", "owner"))

        with pytest.raises(ValueError):
            execute_read_only_goal(
                store,
                "linwan",
                "owner",
                goal.goal_id,
                tool="code_run",
                invoker=lambda tool: "should not run",
            )

        assert len(store.events_for_actor("linwan", "owner")) == events_before
        assert store.get_goal(goal.goal_id).status == "approved"

    def test_permission_token_binds_arguments(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        goal = self._goal(store, status="approved")

        first = permission_token(goal, action="read", arguments={"q": "a"}, now_ms=1)
        second = permission_token(goal, action="read", arguments={"q": "b"}, now_ms=1)

        assert first["exact_arguments_hash"] != second["exact_arguments_hash"]
        assert first["expires_at_ms"] > 1


class TestOutreachFeedback:
    def test_reply_links_to_the_sent_event(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        sent = _event(store, "s1", kind="proactive.message.sent", summary="今天还好吗", at_ms=_ms(_DAY, 9))
        record_sent(store, "linwan", "owner", sent_event_id=sent.event_id)
        reply = _event(store, "u1", summary="还行，你呢", at_ms=_ms(_DAY, 9) + 90_000)

        row = match_reply(store, "linwan", "owner", owner_event=reply)

        assert row is not None
        assert row.sent_event_id == sent.event_id
        assert row.reply_event_id == reply.event_id
        assert row.kind == "positive"
        assert row.response_delay_ms == 90_000

    def test_boundary_reply_is_classified_as_boundary(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        sent = _event(store, "s1", kind="proactive.message.sent", summary="在吗", at_ms=_ms(_DAY, 9))
        record_sent(store, "linwan", "owner", sent_event_id=sent.event_id)
        reply = _event(
            store, "u1", summary="这个话题不想聊这个了", at_ms=_ms(_DAY, 9) + 60_000
        )

        row = match_reply(store, "linwan", "owner", owner_event=reply)

        assert row is not None and row.kind == "boundary"

    def test_reply_outside_the_window_is_not_linked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        sent = _event(store, "s1", kind="proactive.message.sent", summary="在吗", at_ms=_ms(_DAY, 9))
        record_sent(store, "linwan", "owner", sent_event_id=sent.event_id)
        late = _event(store, "u1", summary="刚看到", at_ms=_ms(_DAY, 9) + 20 * _HOUR_MS)

        assert match_reply(store, "linwan", "owner", owner_event=late) is None

    def test_no_response_lowers_the_multiplier_to_a_floor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for index in range(3):
            sent = _event(
                store,
                f"s{index}",
                kind="proactive.message.sent",
                summary="在吗",
                at_ms=_ms(_DAY, 9) + index * _HOUR_MS,
            )
            record_sent(store, "linwan", "owner", sent_event_id=sent.event_id)

        multiplier = outreach_multiplier(
            store, "linwan", "owner", now_ms=_ms(_DAY, 12), window_ms=48 * _HOUR_MS
        )

        assert 0.25 <= multiplier < 1.0
        assert multiplier == pytest.approx(0.25)

    def test_classify_reply_is_deterministic(self) -> None:
        assert classify_reply("好的呀") == "positive"
        assert classify_reply("别问了") == "boundary"
        assert classify_reply("不用了谢谢") == "negative"
