"""V2-Step 4: promises extracted from confirmed sends (migration v5).

Acceptance from ``docs/companion-self-memory-v2-plan.md`` Step 4: only confirmed sends are
analysed, the quote must be an exact substring, the same send never yields two promises,
and every promise can end as succeeded/failed/cancelled.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore.companion.commitment import (
    COMMITMENT_PROMPT_VERSION,
    CommitmentOutcome,
    cancel_commitment,
    commitment_view,
    complete_commitment,
    extract_commitment,
    looks_like_promise,
    parse_due_ms,
    record_commitment,
    sweep_overdue,
)
from gacore.companion.store import (
    SCHEMA_VERSION,
    CompanionStore,
    CompanionStoreError,
    SourceOwnershipError,
)

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


def _send(store: CompanionStore, key: str, summary: str, at_ms: int) -> str:
    return store.append_event(
        kind="agent.message.sent",
        actor_id="linwan",
        principal_id="owner",
        source_id=f"src-{key}",
        summary=summary,
        idempotency_key=key,
        occurred_at_ms=at_ms,
    ).event_id


def _runner(**overrides):
    payload = {
        "ok": True,
        "quote": "明天帮你看看",
        "action": "查看资料",
        "success_criteria": "给出结论",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return lambda _text: CommitmentOutcome(**payload)


class TestMigration:
    def test_v5_adds_commitments_with_a_unique_key(self, tmp_path: Path) -> None:
        path = tmp_path / "companion.db"
        CompanionStore(path)
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='companion_commitment'"
                )
            }
            assert "ux_companion_commitment_key" in indexes
        finally:
            conn.close()


class TestPrefilter:
    def test_ordinary_reply_never_reaches_the_model(self) -> None:
        calls: list[str] = []

        def runner(text: str) -> CommitmentOutcome:
            calls.append(text)
            return CommitmentOutcome(ok=True, quote=text, action="x", success_criteria="y")

        outcome = extract_commitment("在的，你说的事我记着", runner=runner)

        assert outcome.ok is False
        assert calls == []
        assert looks_like_promise("明天帮你看看") is True
        assert looks_like_promise("好呀") is False


class TestExtraction:
    def test_promise_from_a_confirmed_send_is_stored_active(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        summary = "行，明天帮你看看那份资料"
        event = _send(store, "s1", summary, _ms(_DAY, 9))

        commitment = record_commitment(
            store,
            "linwan",
            "owner",
            event_id=event,
            kind="agent.message.sent",
            summary=summary,
            occurred_at_ms=_ms(_DAY, 9),
            runner=_runner(quote="明天帮你看看那份资料"),
        )

        assert commitment is not None
        assert commitment.status == "active"
        assert commitment.source_event_id == event
        assert commitment.due_at_ms == _ms("2026-10-31", 23, 59)

    def test_quote_must_be_an_exact_substring(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        summary = "明天帮你看看"
        event = _send(store, "s1", summary, _ms(_DAY, 9))

        commitment = record_commitment(
            store,
            "linwan",
            "owner",
            event_id=event,
            kind="agent.message.sent",
            summary=summary,
            occurred_at_ms=_ms(_DAY, 9),
            runner=_runner(quote="明天一定帮你看看那份资料"),  # not in the send
        )

        assert commitment is None
        assert store.commitments("linwan", "owner") == []

    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [(0.59, None), (0.6, "proposed"), (0.84, "proposed"), (0.85, "active")],
    )
    def test_confidence_decides_the_initial_state(
        self, tmp_path: Path, confidence: float, expected: str | None
    ) -> None:
        store = _store(tmp_path)
        summary = "明天帮你看看"
        event = _send(store, f"s-{confidence}", summary, _ms(_DAY, 9))

        commitment = record_commitment(
            store,
            "linwan",
            "owner",
            event_id=event,
            kind="agent.message.sent",
            summary=summary,
            occurred_at_ms=_ms(_DAY, 9),
            runner=_runner(confidence=confidence),
        )

        assert (commitment.status if commitment else None) == expected

    def test_owner_messages_are_never_analysed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = store.append_event(
            kind="user.message.received",
            actor_id="linwan",
            principal_id="owner",
            source_id="src-u",
            summary="明天帮你看看",
            idempotency_key="u1",
            occurred_at_ms=_ms(_DAY, 9),
        ).event_id

        commitment = record_commitment(
            store,
            "linwan",
            "owner",
            event_id=event,
            kind="user.message.received",
            summary="明天帮你看看",
            occurred_at_ms=_ms(_DAY, 9),
            runner=_runner(),
        )

        assert commitment is None

    def test_same_send_is_not_duplicated(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        summary = "明天帮你看看"
        event = _send(store, "s1", summary, _ms(_DAY, 9))
        kwargs = dict(
            event_id=event,
            kind="agent.message.sent",
            summary=summary,
            occurred_at_ms=_ms(_DAY, 9),
            runner=_runner(),
        )

        first = record_commitment(store, "linwan", "owner", **kwargs)
        second = record_commitment(store, "linwan", "owner", **kwargs)

        assert first is not None and second is not None
        assert first.commitment_id == second.commitment_id
        assert len(store.commitments("linwan", "owner")) == 1

    def test_source_must_belong_to_the_same_actor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        foreign = store.append_event(
            kind="agent.message.sent",
            actor_id="other",
            principal_id="owner",
            source_id="src-x",
            summary="明天帮你看看",
            idempotency_key="x1",
            occurred_at_ms=_ms(_DAY, 9),
        ).event_id

        with pytest.raises(SourceOwnershipError):
            store.add_commitment(
                actor_id="linwan",
                principal_id="owner",
                source_event_id=foreign,
                quote="明天帮你看看",
                action="看看",
                due_at_ms=None,
                success_criteria="给出结论",
                status="active",
            )


class TestDueParsing:
    @pytest.mark.parametrize(
        ("quote", "expected_day", "expected_hour"),
        [
            ("明天给你", "2026-10-31", 23),
            ("后天给你", "2026-11-01", 23),
            ("今晚给你", "2026-10-30", 23),
            ("三天后给你", "2026-11-02", 23),
            ("2026-11-05 给你", "2026-11-05", 23),
        ],
    )
    def test_relative_words_become_deadlines(
        self, quote: str, expected_day: str, expected_hour: int
    ) -> None:
        due = parse_due_ms(quote, _ms(_DAY, 9))

        assert due is not None
        local = datetime.fromtimestamp(due / 1000, tz=_TZ)
        assert local.strftime("%Y-%m-%d") == expected_day
        assert local.hour == expected_hour

    @pytest.mark.parametrize("quote", ["回头给你看看", "改天聊", "以后再说"])
    def test_vague_words_stay_deadline_less(self, quote: str) -> None:
        assert parse_due_ms(quote, _ms(_DAY, 9)) is None

    def test_soon_words_get_a_two_hour_window(self) -> None:
        due = parse_due_ms("稍后给你发", _ms(_DAY, 9))

        assert due == _ms(_DAY, 9) + 2 * _HOUR_MS


class TestLifecycle:
    def test_sweep_moves_active_to_overdue_then_failed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _send(store, "s1", "明天帮你看看", _ms(_DAY, 9))
        commitment, _ = store.add_commitment(
            actor_id="linwan",
            principal_id="owner",
            source_event_id=event,
            quote="明天帮你看看",
            action="看看",
            due_at_ms=_ms(_DAY, 10),
            success_criteria="给出结论",
            status="active",
        )

        first = sweep_overdue(store, now_ms=_ms(_DAY, 12))
        assert first == {"overdue": 1, "failed": 0}
        assert store.commitments("linwan", "owner")[0].status == "overdue"

        second = sweep_overdue(store, now_ms=_ms(_DAY, 12) + 25 * _HOUR_MS)
        assert second == {"overdue": 0, "failed": 1}
        assert store.commitments("linwan", "owner")[0].status == "failed"

    def test_promise_can_succeed_be_cancelled_or_fail(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _send(store, "s1", "明天帮你看看", _ms(_DAY, 9))
        made, _ = store.add_commitment(
            actor_id="linwan",
            principal_id="owner",
            source_event_id=event,
            quote="明天帮你看看",
            action="看看",
            due_at_ms=None,
            success_criteria="给出结论",
            status="active",
        )

        done = complete_commitment(store, made.commitment_id, succeeded=True)
        assert done.status == "succeeded"
        with pytest.raises(CompanionStoreError):
            cancel_commitment(store, made.commitment_id)  # terminal stays terminal

        other, _ = store.add_commitment(
            actor_id="linwan",
            principal_id="owner",
            source_event_id=event,
            quote="明晚再聊",
            action="聊聊",
            due_at_ms=None,
            success_criteria="聊完",
            status="proposed",
        )
        assert cancel_commitment(store, other.commitment_id).status == "cancelled"

    def test_view_lists_open_promises_only_when_asked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _send(store, "s1", "明天帮你看看", _ms(_DAY, 9))
        store.add_commitment(
            actor_id="linwan",
            principal_id="owner",
            source_event_id=event,
            quote="明天帮你看看",
            action="看看",
            due_at_ms=None,
            success_criteria="给出结论",
            status="active",
        )

        assert len(commitment_view(store, "linwan", "owner")) == 1
        assert commitment_view(store, "linwan", "owner", open_only=True)[0]["status"] == "active"


class TestPrompt:
    def test_prompt_declares_the_exact_output_schema(self) -> None:
        import gacore.companion.commitment as module

        prompt = module._SYSTEM_PROMPT
        for key in ('"is_commitment"', '"quote"', '"action"', '"due_text"',
                    '"success_criteria"', '"confidence"'):
            assert key in prompt, f"prompt must declare {key}"
        assert COMMITMENT_PROMPT_VERSION == "companion-commitment@v1"
