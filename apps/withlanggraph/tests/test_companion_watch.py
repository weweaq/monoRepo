"""V3: watches — "owner finishes at six, ask at six" (kind='watch' intents)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore.companion.follow_up import (
    WatchOutcome,
    looks_like_watch,
    parse_clock_ms,
    record_watch,
)
from gacore.companion.loop import run_life_tick
from gacore.companion.outreach import collect_candidates
from gacore.companion.store import CompanionStore

_TZ = timezone(timedelta(hours=8))
_DAY = "2026-10-31"
_HOUR_MS = 60 * 60 * 1000


def _ms(day: str, hour: int = 10, minute: int = 0) -> int:
    return int(
        datetime.strptime(day, "%Y-%m-%d")
        .replace(hour=hour, minute=minute, tzinfo=_TZ)
        .timestamp()
        * 1000
    )


def _store(tmp_path: Path) -> CompanionStore:
    return CompanionStore(tmp_path / "companion.db")


def _event(store: CompanionStore, summary: str = "我六点下班", at_ms: int | None = None) -> str:
    return store.append_event(
        kind="user.message.received",
        actor_id="linwan",
        principal_id="owner",
        source_id="u1",
        summary=summary,
        idempotency_key="u1",
        occurred_at_ms=_ms(_DAY, 10) if at_ms is None else at_ms,
    ).event_id


def _runner(**overrides):
    payload = {
        "ok": True,
        "topic": "下班",
        "time_text": "六点",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return lambda _text: WatchOutcome(**payload)


class TestParseClock:
    @pytest.mark.parametrize(
        ("text", "hour", "minute"),
        [
            ("六点下班", 18, 0),
            ("6点下班", 18, 0),
            ("18:00 下班", 18, 0),
            ("六点半下班", 18, 30),
            ("下午三点开会", 15, 0),
            ("晚上八点吃饭", 20, 0),
        ],
    )
    def test_evening_clock_forms(self, text: str, hour: int, minute: int) -> None:
        due = parse_clock_ms(
            text, _ms(_DAY, 10), evening_hint=any(w in text for w in ("下班", "晚上", "吃饭"))
        )

        assert due is not None
        local = datetime.fromtimestamp(due / 1000, tz=_TZ)
        assert (local.hour, local.minute) == (hour, minute)

    def test_morning_clock_is_not_shifted(self) -> None:
        due = parse_clock_ms("上午九点上班", _ms(_DAY, 8))

        local = datetime.fromtimestamp(due / 1000, tz=_TZ)
        assert (local.hour, local.minute) == (9, 0)

    def test_passed_time_rolls_to_tomorrow(self) -> None:
        due = parse_clock_ms("六点下班", _ms(_DAY, 20), evening_hint=True)

        local = datetime.fromtimestamp(due / 1000, tz=_TZ)
        assert local.day == 1  # 2026-11-01
        assert local.hour == 18

    def test_no_clock_returns_none(self) -> None:
        assert parse_clock_ms("我最近很忙", _ms(_DAY, 10)) is None


class TestPrefilter:
    def test_life_words_with_clock_pass(self) -> None:
        assert looks_like_watch("我六点下班") is True

    def test_clock_without_life_event_is_ignored(self) -> None:
        assert looks_like_watch("现在六点整") is False

    def test_life_event_without_clock_is_ignored(self) -> None:
        assert looks_like_watch("今天要加班") is False


class TestRecordWatch:
    def test_creates_a_watch_card(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _event(store)

        card = record_watch(
            store,
            "linwan",
            "owner",
            event_id=event,
            summary="我六点下班",
            occurred_at_ms=_ms(_DAY, 10),
            runner=_runner(),
        )

        assert card is not None
        assert card.kind == "watch"
        assert card.status == "armed"
        assert card.trigger_at_ms == _ms(_DAY, 18)
        assert card.expires_at_ms == _ms(_DAY, 18) + 3 * _HOUR_MS
        assert card.source_event_id == event

    def test_low_confidence_is_dropped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _event(store)

        assert (
            record_watch(
                store,
                "linwan",
                "owner",
                event_id=event,
                summary="我六点下班",
                occurred_at_ms=_ms(_DAY, 10),
                runner=_runner(confidence=0.3),
            )
            is None
        )

    def test_too_far_ahead_is_dropped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _event(store, at_ms=_ms(_DAY, 1))

        assert (
            record_watch(
                store,
                "linwan",
                "owner",
                event_id=event,
                summary="我六点下班",
                occurred_at_ms=_ms(_DAY, 1),
                runner=_runner(),
            )
            is None
        )  # 17h ahead > 12h cap


class TestWatchBecomesAQuestion:
    def test_due_watch_is_the_top_candidate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _event(store)
        record_watch(
            store,
            "linwan",
            "owner",
            event_id=event,
            summary="我六点下班",
            occurred_at_ms=_ms(_DAY, 10),
            runner=_runner(),
        )

        candidates = collect_candidates(store, "linwan", "owner", now_ms=_ms(_DAY, 18, 5))

        assert candidates[0].kind == "watch_due"
        assert candidates[0].ref_id is not None

    def test_before_the_trigger_there_is_no_candidate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _event(store)
        record_watch(
            store,
            "linwan",
            "owner",
            event_id=event,
            summary="我六点下班",
            occurred_at_ms=_ms(_DAY, 10),
            runner=_runner(),
        )

        assert collect_candidates(store, "linwan", "owner", now_ms=_ms(_DAY, 17)) == []

    def test_asking_closes_the_watch(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _event(store)
        card = record_watch(
            store,
            "linwan",
            "owner",
            event_id=event,
            summary="我六点下班",
            occurred_at_ms=_ms(_DAY, 10),
            runner=_runner(),
        )
        sent: list[str] = []

        result = run_life_tick(
            store,
            "linwan",
            "owner",
            now_ms=_ms(_DAY, 18, 5),
            runner=lambda _c: {"action": "TELL_ONLY", "message": "还没下班吗？"},
            sender=sent.append,
        )

        assert result.action == "TELL_ONLY"
        assert sent == ["还没下班吗？"]
        assert store.get_intent(card.intent_id).status == "done"

    def test_expired_watch_never_asks(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        event = _event(store)
        record_watch(
            store,
            "linwan",
            "owner",
            event_id=event,
            summary="我六点下班",
            occurred_at_ms=_ms(_DAY, 10),
            runner=_runner(),
        )

        candidates = collect_candidates(store, "linwan", "owner", now_ms=_ms(_DAY, 22))

        assert candidates == []
