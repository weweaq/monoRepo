"""V3: the unified intent card (migration v9).

One card covers promise / action / watch. These tests pin the migration copy, the
lifecycle, the "one running action" rule and the due/expire queries.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore.companion import intent as intent_api
from gacore.companion.store import (
    SCHEMA_VERSION,
    CompanionStore,
    CompanionStoreError,
    apply_migration,
)

_TZ = timezone(timedelta(hours=8))
_DAY = "2026-10-31"
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


def _event(store: CompanionStore, key: str = "u1") -> str:
    return store.append_event(
        kind="user.message.received",
        actor_id="linwan",
        principal_id="owner",
        source_id=key,
        summary="我六点下班",
        idempotency_key=key,
        occurred_at_ms=_ms(_DAY, 10),
    ).event_id


class TestMigration:
    def test_v9_creates_the_intent_table(self, tmp_path: Path) -> None:
        path = tmp_path / "companion.db"
        CompanionStore(path)
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "companion_intent" in tables
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='companion_intent'"
                )
            }
            assert {"ux_intent_source", "ux_one_running_act"} <= indexes
        finally:
            conn.close()

    def test_v9_copies_existing_goals(self, tmp_path: Path) -> None:
        path = tmp_path / "companion.db"
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            apply_migration(conn, target_version=8)
            conn.execute(
                """
                INSERT INTO companion_goal (
                    goal_id, actor_id, principal_id, text, status,
                    allowed_tools_json, deadline_ms
                ) VALUES ('goal_old', 'linwan', 'owner', '查 B 站', 'approved',
                          '["bili_history"]', ?)
                """,
                (_ms(_DAY, 23),),
            )
            conn.commit()
            apply_migration(conn, target_version=9)
            row = conn.execute(
                "SELECT kind, title, status, authority_json FROM companion_intent WHERE intent_id = ?",
                ("gint_goal_old",),
            ).fetchone()
            assert row is not None
            assert row[0] == "act"
            assert row[1] == "查 B 站"
            assert row[2] == "armed"
            assert "bili_history" in row[3]
        finally:
            conn.close()


class TestIntentLifecycle:
    def test_act_card_goes_planned_armed_running_done(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        card = intent_api.create_act(
            store, "linwan", "owner", title="查 B 站", tools=["bili_history"]
        )
        assert card.status == "planned"

        intent_api.arm(store, card.intent_id)
        intent_api.start(store, card.intent_id)
        done = intent_api.finish(store, card.intent_id, result_event_id=_event(store, "r1"))

        assert done.status == "done"
        assert done.result_event_id is not None
        assert done.authority["tools"] == ["bili_history"]

    def test_watch_card_carries_a_trigger(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        card = intent_api.create_watch(
            store,
            "linwan",
            "owner",
            title="问下班没",
            trigger_at_ms=_ms(_DAY, 18),
            expires_at_ms=_ms(_DAY, 21),
            source_event_id=_event(store),
        )

        assert card.kind == "watch"
        assert card.status == "armed"
        assert card.trigger_at_ms == _ms(_DAY, 18)
        assert card.authority["may_speak"] is True

    def test_terminal_card_cannot_move(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        card = intent_api.create_act(store, "linwan", "owner", title="x", tools=["get_time"])
        intent_api.cancel(store, card.intent_id)

        with pytest.raises(CompanionStoreError):
            intent_api.finish(store, card.intent_id)

    def test_only_one_action_may_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        first = intent_api.create_act(store, "linwan", "owner", title="a", tools=["get_time"])
        second = intent_api.create_act(store, "linwan", "owner", title="b", tools=["get_time"])
        intent_api.start(store, first.intent_id)

        with pytest.raises(CompanionStoreError):
            intent_api.start(store, second.intent_id)

    def test_same_source_and_title_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        source = _event(store)
        first = intent_api.create_watch(
            store, "linwan", "owner", title="问下班没", trigger_at_ms=_ms(_DAY, 18),
            source_event_id=source,
        )
        second = intent_api.create_watch(
            store, "linwan", "owner", title="问下班没", trigger_at_ms=_ms(_DAY, 19),
            source_event_id=source,
        )

        assert first.intent_id == second.intent_id
        assert len(store.intents("linwan", "owner", kind="watch")) == 1


class TestDueAndExpire:
    def test_future_card_is_not_due(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        intent_api.create_watch(
            store, "linwan", "owner", title="问下班没", trigger_at_ms=_ms(_DAY, 18)
        )

        assert intent_api.due(store, "linwan", "owner", now_ms=_ms(_DAY, 17)) == []

    def test_due_card_appears_after_trigger(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        card = intent_api.create_watch(
            store, "linwan", "owner", title="问下班没", trigger_at_ms=_ms(_DAY, 18)
        )

        due = intent_api.due(store, "linwan", "owner", now_ms=_ms(_DAY, 18, 5))

        assert [item.intent_id for item in due] == [card.intent_id]

    def test_overdue_card_expires_and_leaves_the_due_list(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        card = intent_api.create_watch(
            store,
            "linwan",
            "owner",
            title="问下班没",
            trigger_at_ms=_ms(_DAY, 18),
            expires_at_ms=_ms(_DAY, 20),
        )

        due = intent_api.due(store, "linwan", "owner", now_ms=_ms(_DAY, 21))

        assert due == []
        assert store.get_intent(card.intent_id).status == "expired"

    def test_extend_moves_the_trigger(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        card = intent_api.create_watch(
            store, "linwan", "owner", title="问下班没", trigger_at_ms=_ms(_DAY, 18)
        )

        moved = intent_api.extend(store, card.intent_id, trigger_at_ms=_ms(_DAY, 21))

        assert moved.trigger_at_ms == _ms(_DAY, 21)
        assert moved.intent_id == card.intent_id
        assert moved.status == "armed"
        assert len(store.intents("linwan", "owner", kind="watch")) == 1
