"""V2-Step 2: relationship stages/status and open threads projected from V1 events.

Every acceptance bullet of ``docs/companion-self-memory-v2-plan.md`` Step 2 has one test
here: single interaction cannot jump stage, actor isolation, threads carry a source event,
an explicit "不想聊这个" becomes a boundary that a later send can violate, silence never
strains, strained freezes the stage, and repair is only claimed after acknowledgement
plus a follow-up.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore.companion.relationship import (
    DEFAULT_ACTOR_BOUNDARIES,
    POSITIVE_WINDOW_MS,
    project,
    refresh_relationship,
)
from gacore.companion.store import (
    SCHEMA_VERSION,
    MIGRATION_V1,
    MIGRATION_V2,
    CompanionStore,
    SourceOwnershipError,
)

_TZ = timezone(timedelta(hours=8))
_DAY = "2026-10-30"


def _ms(day: str, hour: int = 12, minute: int = 0) -> int:
    base = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, tzinfo=_TZ
    )
    return int(base.timestamp() * 1000)


def _store(tmp_path: Path) -> CompanionStore:
    return CompanionStore(tmp_path / "companion.db")


def _add(
    store: CompanionStore,
    key: str,
    *,
    kind: str = "user.message.received",
    summary: str,
    at_ms: int,
    actor: str = "linwan",
) -> str:
    return store.append_event(
        kind=kind,
        actor_id=actor,
        principal_id="owner",
        source_id=f"src-{key}",
        summary=summary,
        idempotency_key=key,
        occurred_at_ms=at_ms,
    ).event_id


def _sent(store: CompanionStore, key: str, summary: str, at_ms: int, actor: str = "linwan") -> str:
    return _add(
        store, key, kind="agent.message.sent", summary=summary, at_ms=at_ms, actor=actor
    )


def _user(store: CompanionStore, key: str, summary: str, at_ms: int, actor: str = "linwan") -> str:
    return _add(store, key, kind="user.message.received", summary=summary, at_ms=at_ms, actor=actor)


class TestMigration:
    def test_v2_database_upgrades_to_v3_and_keeps_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "companion.db"
        conn = sqlite3.connect(str(path))
        try:
            for step in (MIGRATION_V1, MIGRATION_V2):
                for statement in step:
                    conn.execute(statement)
            conn.execute("PRAGMA user_version = 2")
            conn.execute(
                """
                INSERT INTO companion_event (
                    event_id, idempotency_key, actor_id, principal_id,
                    kind, summary, source_id, occurred_at_ms
                ) VALUES ('evt_old', 'old', 'linwan', 'owner',
                          'user.message.received', 'kept', 'src', 1)
                """
            )
            conn.commit()
        finally:
            conn.close()

        store = CompanionStore(path)
        assert [event.event_id for event in store.events_for_actor("linwan", "owner")] == ["evt_old"]
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"companion_relationship", "companion_open_thread"} <= tables
        finally:
            conn.close()


class TestStage:
    def test_single_interaction_cannot_jump_stage(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "你好", _ms(_DAY, 9))
        _sent(store, "s1", "在。", _ms(_DAY, 9, 1))

        saved = refresh_relationship(store, "linwan", "owner")

        assert saved.stage == "new"
        assert saved.status == "stable"

    def test_counters_promote_to_familiar_only_after_both_thresholds(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # 5 meaningful interactions (3 owner turns + 2 sends) across 2 days.
        _user(store, "u1", "早啊", _ms("2026-10-29", 9))
        _user(store, "u2", "在忙吗", _ms("2026-10-29", 10))
        _user(store, "u3", "晚安", _ms(_DAY, 9))
        _sent(store, "s1", "早。", _ms("2026-10-29", 9, 1))
        _sent(store, "s2", "在。", _ms(_DAY, 9, 1))

        saved = refresh_relationship(store, "linwan", "owner")

        assert (saved.meaningful_interactions, saved.active_days) == (5, 2)
        assert saved.stage == "familiar"

    def test_established_needs_a_reliability_signal(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for index in range(15):
            day = f"2026-10-{index + 1:02d}"
            _user(store, f"u{index}", f"今天怎么样啦 {index}", _ms(day, 9))
            if index < 10:
                _sent(store, f"s{index}", f"还行 {index}", _ms(day, 9, 5))

        without = refresh_relationship(store, "linwan", "owner")
        assert (without.meaningful_interactions, without.active_days) == (25, 15)
        assert without.positive_feedback == 0
        assert without.stage == "familiar"  # 20/14 reached, reliability missing

        # Owner replies to a proactive outreach: the Step-4 stand-in for "a commitment kept".
        _add(
            store,
            "p1",
            kind="proactive.message.sent",
            summary="今天还顺利吗",
            at_ms=_ms(_DAY, 20),
        )
        _user(store, "u-answer", "还行，你呢", _ms(_DAY, 20, 3))

        with_reliability = refresh_relationship(store, "linwan", "owner")
        assert with_reliability.positive_feedback >= 1
        assert with_reliability.stage == "established"

    def test_actors_are_isolated(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for index in range(3):
            _user(store, f"a{index}", f"linwan 消息 {index}", _ms(_DAY, 9 + index), actor="linwan")
            _user(store, f"b{index}", f"hanli 消息 {index}", _ms(_DAY, 9 + index), actor="hanli")

        first = refresh_relationship(store, "linwan", "owner")
        second = refresh_relationship(store, "hanli", "owner")

        assert first.meaningful_interactions == second.meaningful_interactions == 3
        assert first.actor_id == "linwan" and second.actor_id == "hanli"
        assert len(store.events_for_actor("linwan", "owner")) == 3


class TestBoundaries:
    def test_explicit_refusal_becomes_a_boundary_with_source(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        source = _user(store, "u1", "加班这个话题不想聊这个了", _ms(_DAY, 9))

        saved = refresh_relationship(store, "linwan", "owner")

        assert len(saved.user_boundaries) == 1
        boundary = saved.user_boundaries[0]
        assert boundary["source_event_id"] == source
        assert boundary["topic"] == "加班"
        assert saved.status == "stable"  # nothing violated it yet

    def test_ordinary_negation_is_not_a_boundary(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "今天不想吃饭", _ms(_DAY, 9))

        saved = refresh_relationship(store, "linwan", "owner")

        assert saved.user_boundaries == ()

    def test_revisiting_the_topic_strains_the_relationship(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "加班这个话题不想聊这个了", _ms(_DAY, 9))
        violation = _sent(store, "s1", "那说说加班的事吧", _ms(_DAY, 10))

        saved = refresh_relationship(store, "linwan", "owner")

        assert saved.status == "strained"
        assert saved.last_negative_event_id == violation

    def test_silence_never_strains(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "在吗", _ms(_DAY, 9))
        _user(store, "u2", "算了", _ms(_DAY, 15))  # no character reply at all

        saved = refresh_relationship(store, "linwan", "owner")

        assert saved.status == "stable"
        assert saved.last_negative_event_id is None

    def test_repair_requires_acknowledgement_and_follow_up(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "加班这个话题不想聊这个了", _ms(_DAY, 9))
        _sent(store, "s1", "那说说加班的事吧", _ms(_DAY, 10))

        assert refresh_relationship(store, "linwan", "owner").status == "strained"

        _user(store, "u2", "没事，我们聊点别的", _ms(_DAY, 11))
        repairing = refresh_relationship(store, "linwan", "owner")
        assert repairing.status == "repairing"

        _user(store, "u3", "你今天看的剧挺有意思", _ms(_DAY, 12))
        repaired = refresh_relationship(store, "linwan", "owner")
        assert repaired.status == "stable"
        assert repaired.last_repair_event_id is not None

    def test_actor_boundaries_come_from_policy_not_the_model(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "你是谁", _ms(_DAY, 9))

        saved = refresh_relationship(store, "linwan", "owner")

        assert saved.actor_boundaries == DEFAULT_ACTOR_BOUNDARIES
        assert "不冒充别人，也不自称是人类" in saved.actor_boundaries

    def test_strained_freezes_the_stage(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # Earn "familiar" first: 5 meaningful interactions across 2 days.
        for index in range(2):
            _user(store, f"u{index}", f"早啊 {index}", _ms("2026-10-29", 9 + index))
        _user(store, "u2", "在忙吗", _ms(_DAY, 8))
        _sent(store, "s1", "早。", _ms("2026-10-29", 9, 5))
        _sent(store, "s2", "在。", _ms(_DAY, 8, 5))
        assert refresh_relationship(store, "linwan", "owner").stage == "familiar"

        # Boundary stated, then violated; counters meanwhile reach the established bar.
        _user(store, "u-boundary", "加班这个话题不想聊这个了", _ms(_DAY, 9))
        _sent(store, "s-violation", "还是聊聊加班吧", _ms(_DAY, 9, 5))
        for index in range(20):
            day = f"2026-10-{index + 1:02d}"
            _user(store, f"f{index}", f"今天怎么样 {index}", _ms(day, 15))

        frozen = refresh_relationship(store, "linwan", "owner")

        assert frozen.status == "strained"
        assert frozen.meaningful_interactions >= 20
        assert frozen.active_days >= 14
        assert frozen.stage == "familiar", "a strained relationship must not upgrade"


class TestOpenThreads:
    def test_unanswered_question_becomes_a_thread_with_source(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        source = _user(store, "u1", "你觉得我应该换工作吗", _ms(_DAY, 9))

        refresh_relationship(store, "linwan", "owner")

        threads = store.open_threads("linwan", "owner")
        assert len(threads) == 1
        assert threads[0].source_event_id == source
        assert "换工作" in threads[0].topic
        assert threads[0].status == "open"

    def test_question_answered_in_time_opens_no_thread(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "你觉得我应该换工作吗", _ms(_DAY, 9))
        _sent(store, "s1", "先别急，说说现在的情况。", _ms(_DAY, 9, 5))

        refresh_relationship(store, "linwan", "owner")

        assert store.open_threads("linwan", "owner") == []

    def test_thread_resolves_once_the_character_answers_later(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "你觉得我应该换工作吗", _ms(_DAY, 9))
        refresh_relationship(store, "linwan", "owner")
        assert len(store.open_threads("linwan", "owner")) == 1

        _sent(store, "s1", "昨天那个问题，我想了想。", _ms(_DAY, 20))
        refresh_relationship(store, "linwan", "owner")

        assert store.open_threads("linwan", "owner") == []
        resolved = store.open_threads("linwan", "owner", status="resolved")
        assert len(resolved) == 1

    def test_same_topic_is_not_duplicated(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _user(store, "u1", "你觉得我应该换工作吗", _ms(_DAY, 9))
        _user(store, "u2", "你觉得我应该换工作吗", _ms(_DAY, 10))

        refresh_relationship(store, "linwan", "owner")
        refresh_relationship(store, "linwan", "owner")

        assert len(store.open_threads("linwan", "owner")) == 1

    def test_thread_source_must_be_an_event_of_the_same_actor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        foreign = _user(store, "u-foreign", "别人问的问题吗", _ms(_DAY, 9), actor="other")

        with pytest.raises(SourceOwnershipError):
            store.add_open_thread(
                actor_id="linwan",
                principal_id="owner",
                topic="借用别人的事件",
                source_event_id=foreign,
            )


class TestProjectionIsPure:
    def test_project_never_touches_the_store_and_counts_the_proxy_positive_window(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        _sent(store, "s1", "早安", _ms(_DAY, 9))
        _user(store, "u1", "早呀", _ms(_DAY, 9) + POSITIVE_WINDOW_MS // 2)
        _user(store, "u2", "晚上聊", _ms(_DAY, 21))  # outside the window

        events = store.events_for_actor("linwan", "owner")
        projection = project(events)

        assert projection.positive_feedback == 1
        assert projection.meaningful_interactions == 3
        assert store.relationship("linwan", "owner") is None  # pure function, no writes
        assert isinstance(_ms(_DAY), int)
        assert datetime.now(UTC)  # keep the import meaningful for future windows
