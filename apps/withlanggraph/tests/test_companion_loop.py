"""V2 收尾: the Companion life tick (design §16 / §17A.8).

Acceptance: an empty tick costs 0 LLM calls and writes no events; a silent decision sends
nothing; ACT_SILENTLY does a read-only thing without talking; ACT_AND_TELL talks only after
the action really succeeded; a failed delivery is recorded as SYSTEM_FAILURE, never as the
character "not wanting to talk".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


from gacore.companion.loop import run_life_tick
from gacore.companion.store import CompanionStore

_TZ = timezone(timedelta(hours=8))
_DAY = "2026-10-30"
_HOUR_MS = 60 * 60 * 1000


def _ms(day: str, hour: int = 15, minute: int = 0) -> int:
    return int(
        datetime.strptime(day, "%Y-%m-%d")
        .replace(hour=hour, minute=minute, tzinfo=_TZ)
        .timestamp()
        * 1000
    )


def _store(tmp_path: Path) -> CompanionStore:
    return CompanionStore(tmp_path / "companion.db")


def _event(store: CompanionStore, key: str, *, kind: str = "user.message.received", at_ms: int | None = None):
    return store.append_event(
        kind=kind,
        actor_id="linwan",
        principal_id="owner",
        source_id=f"src-{key}",
        summary=f"消息 {key}",
        idempotency_key=key,
        occurred_at_ms=_ms(_DAY, 10) if at_ms is None else at_ms,
    ).event_id


def _open_thread(store: CompanionStore, key: str = "t1", topic: str = "换工作") -> None:
    source = _event(store, key)
    store.add_open_thread(
        actor_id="linwan", principal_id="owner", topic=topic, source_event_id=source
    )


def _kinds(store: CompanionStore) -> list[str]:
    return [event.kind for event in store.events_for_actor("linwan", "owner")]


class TestEmptyTick:
    def test_no_events_means_no_tick(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        called: list[int] = []

        result = run_life_tick(
            store, "linwan", "owner", now_ms=_ms(_DAY), runner=lambda _c: called.append(1)
        )

        assert result.action == "STAY_SILENT"
        assert result.reason == "NO_CANDIDATE"
        assert result.llm_calls == 0 and called == []
        assert _kinds(store) == []

    def test_no_candidate_sends_nothing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _event(store, "u1")
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "hi"},
            sender=sent.append,
        )

        assert result.action == "STAY_SILENT" and result.reason == "NO_CANDIDATE"
        assert sent == []

    def test_daily_cap_blocks_before_the_model(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        for index in range(3):
            _event(
                store,
                f"s{index}",
                kind="proactive.message.sent",
                at_ms=_ms(_DAY, 9) + index * _HOUR_MS,
            )
        called: list[int] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: called.append(1),
            sender=lambda _t: None,
        )

        assert result.action == "STAY_SILENT"
        assert result.reason == "POLICY_BLOCKED"
        assert called == []


class TestTelling:
    def test_tell_only_sends_once_and_records_the_event(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "上次换工作的事想好了吗"},
            sender=sent.append,
        )

        assert result.action == "TELL_ONLY"
        assert sent == ["上次换工作的事想好了吗"]
        assert "proactive.message.sent" in _kinds(store)
        assert store.outreach_rows("linwan", "owner"), "send must be attributable"

    def test_failed_delivery_is_a_system_failure(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)

        def boom(_text: str) -> None:
            raise RuntimeError("qq down")

        before = len(store.events_for_actor("linwan", "owner"))
        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "在吗"},
            sender=boom,
        )

        assert result.action == "STAY_SILENT" and result.reason == "SYSTEM_FAILURE"
        assert len(store.events_for_actor("linwan", "owner")) == before

    def test_empty_message_is_not_sent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "   "},
            sender=sent.append,
        )

        assert result.action == "STAY_SILENT" and result.reason == "LOW_VALUE"
        assert sent == []


class TestActing:
    def test_act_silently_does_work_without_talking(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []
        calls: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "ACT_SILENTLY"},
            sender=sent.append,
            invoker=lambda tool, args: calls.append(tool) or f"{tool} ok",
        )

        assert result.action == "ACT_SILENTLY"
        assert calls and calls[0] in {"read_daily", "search_daily", "bili_history", "get_time"}
        assert sent == [], "ACT_SILENTLY must not talk"
        kinds = _kinds(store)
        assert "read_action.succeeded" in kinds
        assert "proactive.message.sent" not in kinds
        assert store.get_goal(result.goal_id).status == "succeeded"

    def test_act_and_tell_talks_after_success(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "ACT_AND_TELL", "message": "我回看了一下，上次说的是换工作"},
            sender=sent.append,
            invoker=lambda tool, args: "ok",
        )

        assert result.action == "ACT_AND_TELL"
        assert sent == ["我回看了一下，上次说的是换工作"]
        assert "read_action.succeeded" in _kinds(store)
        assert "proactive.message.sent" in _kinds(store)

    def test_action_failure_is_recorded_not_narrated(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []

        def boom(_tool: str, _args: dict) -> str:
            raise RuntimeError("tool down")

        run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "ACT_AND_TELL", "message": "我查了一下"},
            sender=sent.append,
            invoker=boom,
        )

        assert sent == [], "a failed action must not be told as if it happened"
        assert "read_action.failed" in _kinds(store)

    def test_missing_invoker_stays_silent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "ACT_SILENTLY"},
            sender=sent.append,
        )

        assert result.action == "STAY_SILENT" and result.reason == "POLICY_BLOCKED"
        assert sent == []


class TestGateAndDryRun:
    def test_quiet_hours_never_reach_the_model(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        called: list[int] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 2),
            runner=lambda _c: called.append(1),
            sender=lambda _t: None,
        )

        assert result.action == "STAY_SILENT" and result.reason == "QUIET_HOURS"
        assert called == []

    def test_dry_run_reports_without_sending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []
        before = len(store.events_for_actor("linwan", "owner"))

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "在吗"},
            sender=sent.append,
            dry_run=True,
        )

        assert result.action == "TELL_ONLY" and result.sent_text == "在吗"
        assert sent == []
        assert len(store.events_for_actor("linwan", "owner")) == before

class TestNeverAtNight:
    """半夜绝不发消息：gate 与 delivery 两道闸都要成立。"""

    def test_night_tick_never_sends(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 2),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "睡了吗"},
            sender=sent.append,
        )

        assert result.reason == "QUIET_HOURS"
        assert sent == []

    def test_before_eight_is_still_quiet(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _open_thread(store)
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 7, 30),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "早"},
            sender=sent.append,
        )

        assert result.reason == "QUIET_HOURS" and sent == []

    def test_delivery_layer_refuses_at_night(self, tmp_path: Path) -> None:
        """Even a pre-computed decision cannot be delivered inside quiet hours."""
        from gacore.companion.loop import _tell
        from gacore.companion.outreach import Decision

        store = _store(tmp_path)
        sent: list[str] = []
        decision = Decision(action="TELL_ONLY", message="睡了吗", llm_calls=1)

        result = _tell(
            store,
            "linwan",
            "owner",
            decision,
            sender=sent.append,
            now=_ms(_DAY, 2),
            dry_run=False,
        )

        assert result.action == "STAY_SILENT" and result.reason == "QUIET_HOURS"
        assert sent == []

class TestOwnLifeMaterial:
    """P2: 自己的只读活动既是经历，也是"有事找你"的由头。"""

    def _observation(self, store: CompanionStore, at_ms: int, summary: str = "B站：看了《凡人修仙传》第 120 集"):
        return store.append_event(
            kind="read_action.succeeded",
            actor_id="linwan",
            principal_id="owner",
            source_id=f"obs-{at_ms}",
            summary=summary,
            idempotency_key=f"obs-{at_ms}",
            occurred_at_ms=at_ms,
        ).event_id

    def test_fresh_observation_becomes_the_top_candidate(self, tmp_path: Path) -> None:
        from gacore.companion.outreach import collect_candidates

        store = _store(tmp_path)
        _open_thread(store)
        self._observation(store, _ms(_DAY, 14))

        candidates = collect_candidates(store, "linwan", "owner", now_ms=_ms(_DAY, 15))

        assert candidates[0].kind == "recent_activity"
        assert "凡人修仙传" in candidates[0].text

    def test_told_observation_stops_being_a_candidate(self, tmp_path: Path) -> None:
        from gacore.companion.outreach import collect_candidates

        store = _store(tmp_path)
        observation = self._observation(store, _ms(_DAY, 14))
        store.append_event(
            kind="proactive.message.sent",
            actor_id="linwan",
            principal_id="owner",
            source_id="told",
            summary="我看了凡人修仙传",
            idempotency_key="told",
            occurred_at_ms=_ms(_DAY, 14, 30),
        )

        candidates = collect_candidates(store, "linwan", "owner", now_ms=_ms(_DAY, 15))

        assert observation not in [sid for c in candidates for sid in c.source_ids]

    def test_activity_uses_a_rotating_readonly_tool_with_args(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._observation(store, _ms(_DAY, 14))
        observed: list[tuple[str, dict]] = []

        def invoker(tool: str, args: dict) -> str:
            observed.append((tool, args))
            return "ok"

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "ACT_SILENTLY"},
            invoker=invoker,
        )

        assert result.action == "ACT_SILENTLY"
        tool, args = observed[0]
        assert tool in {"bili_history", "read_daily", "browser_history", "get_time"}
        if tool == "bili_history":
            assert args == {"limit": 5}
        elif tool == "read_daily":
            assert args == {"date": "yesterday"} or args == {"date": "today"}

    def test_act_and_tell_falls_back_to_the_observed_snippet(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._observation(store, _ms(_DAY, 14))
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 15),
            runner=lambda _c: {"action": "ACT_AND_TELL", "message": ""},
            sender=sent.append,
            invoker=lambda tool, args: "看到你在追凡人修仙传",
        )

        assert result.action == "ACT_AND_TELL"
        assert sent and "凡人修仙传" in sent[0]
