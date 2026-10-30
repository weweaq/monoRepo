"""V2-Step 3: a short-lived mood derived from real owner events (migration v4).

Each acceptance bullet of ``docs/companion-self-memory-v2-plan.md`` Step 3 has a test
here: mood always carries a source, an expired mood is not injected, silence never turns
the mood negative, and mood never touches persona/relationship.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore.companion.mood import (
    MAX_MOOD_LINE_CHARS,
    active_mood,
    build_mood_line,
    detect_signal,
    refresh_mood,
)
from gacore.companion.relationship import refresh_relationship
from gacore.companion.store import (
    SCHEMA_VERSION,
    CompanionStore,
    SourceOwnershipError,
)
from gacore.config import Config
from gacore.context import build_system_prompt
from gacore.state import new_state

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


def _add(
    store: CompanionStore,
    key: str,
    summary: str,
    at_ms: int,
    *,
    kind: str = "user.message.received",
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


class TestMigration:
    def test_v3_database_upgrades_to_v4(self, tmp_path: Path) -> None:
        path = tmp_path / "companion.db"
        store = CompanionStore(path)  # migrates to the current version
        assert store.mood("linwan", "owner") is None
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "companion_mood" in tables
        finally:
            conn.close()


class TestDeterministicMapping:
    def test_thanks_turns_happy_with_a_six_hour_ttl(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        source = _add(store, "u1", "谢谢你啊，今天帮了大忙", _ms(_DAY, 9))
        now = _ms(_DAY, 10)

        mood = refresh_mood(store, "linwan", "owner", now_ms=now)

        assert mood is not None
        assert (mood.label, mood.valence, mood.arousal) == ("happy", 0.6, 0.45)
        assert mood.source_event_id == source
        assert mood.expires_at_ms == _ms(_DAY, 9) + 6 * _HOUR_MS

    def test_question_turns_curious(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "这个方案你怎么看？", _ms(_DAY, 9))

        mood = refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 9, 30))

        assert mood is not None and mood.label == "curious"

    def test_venting_turns_concerned(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "今天加班到现在，好累", _ms(_DAY, 21))

        mood = refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 22))

        assert mood is not None and mood.label == "concerned"
        assert mood.valence < 0

    def test_hostility_turns_frustrated_and_outranks_concerned(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # Contains both a vent word (烦) and a hostile phrase: frustration wins the tie.
        _add(store, "u1", "你烦不烦，闭嘴", _ms(_DAY, 9))

        mood = refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 9, 10))

        assert mood is not None and mood.label == "frustrated"

    def test_late_night_message_turns_tired(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "还没睡着", _ms(_DAY, 1, 30))

        mood = refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 2))

        assert mood is not None and mood.label == "tired"

    def test_newest_signal_wins(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "谢谢你", _ms(_DAY, 8))
        _add(store, "u2", "今天好累", _ms(_DAY, 9))

        mood = refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 9, 30))

        assert mood is not None and mood.label == "concerned"

    def test_character_sends_never_set_the_mood(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "s1", "辛苦了，早点睡", _ms(_DAY, 9), kind="agent.message.sent")

        assert refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 10)) is None


class TestExpiry:
    def test_expired_state_is_cleared_and_not_injected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "谢谢你", _ms(_DAY, 9))
        assert refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 10)) is not None

        # Six hours later the happy state has expired: refresh clears the row.
        later = _ms(_DAY, 9) + 7 * _HOUR_MS
        assert refresh_mood(store, "linwan", "owner", now_ms=later) is None
        assert store.mood("linwan", "owner") is None
        assert active_mood(store, "linwan", "owner", now_ms=later) is None
        assert build_mood_line(None) == ""

    def test_stored_but_expired_row_is_not_active(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "谢谢你", _ms(_DAY, 9))
        refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 10))

        stale = _ms(_DAY, 9) + 7 * _HOUR_MS
        assert store.mood("linwan", "owner") is not None  # row still there
        assert active_mood(store, "linwan", "owner", now_ms=stale) is None

    def test_detect_signal_skips_expired_events(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "谢谢你", _ms(_DAY, 9))

        assert detect_signal(store.events_for_actor("linwan", "owner"), now_ms=_ms(_DAY, 20)) is None


class TestIsolation:
    def test_silence_never_produces_a_negative_mood(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "在吗", _ms(_DAY, 9))

        # Two hours of no answer: "在吗" is a question, so the mood is curious, never sad.
        mood = refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 11))

        assert mood is not None and mood.label == "curious"
        assert mood.valence >= 0

    def test_mood_never_touches_relationship_state(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "今天加班好累", _ms(_DAY, 9))
        before = refresh_relationship(store, "linwan", "owner")
        refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 10))
        after = refresh_relationship(store, "linwan", "owner")

        assert (before.stage, before.status) == (after.stage, after.status)
        assert before.meaningful_interactions == after.meaningful_interactions

    def test_mood_source_must_belong_to_the_same_actor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        foreign = _add(store, "u-foreign", "谢谢你", _ms(_DAY, 9), actor="other")

        with pytest.raises(SourceOwnershipError):
            store.save_mood(
                actor_id="linwan",
                principal_id="owner",
                label="happy",
                valence=0.5,
                arousal=0.4,
                source_event_id=foreign,
                expires_at_ms=_ms(_DAY, 12),
            )

    def test_unknown_label_is_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        source = _add(store, "u1", "谢谢你", _ms(_DAY, 9))

        with pytest.raises(ValueError):
            store.save_mood(
                actor_id="linwan",
                principal_id="owner",
                label="ecstatic",
                valence=0.5,
                arousal=0.4,
                source_event_id=source,
                expires_at_ms=_ms(_DAY, 12),
            )


class TestPromptLine:
    def test_line_is_one_bounded_sentence(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _add(store, "u1", "今天加班好累", _ms(_DAY, 9))
        mood = refresh_mood(store, "linwan", "owner", now_ms=_ms(_DAY, 10))

        line = build_mood_line(mood)

        assert line
        assert len(line) <= MAX_MOOD_LINE_CHARS
        assert "\n" not in line
        assert "担心" in line

    def test_prompt_carries_a_live_mood_and_drops_an_expired_one(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        store = CompanionStore.from_config(cfg)
        now = int(time.time() * 1000)
        _add(store, "u1", "谢谢你", now - 60_000)
        refresh_mood(store, "linwan", "owner", now_ms=now)

        state = new_state(
            "在干嘛",
            cfg,
            companion_context={"actor_id": "linwan", "principal_id": "owner"},
        )
        prompt = build_system_prompt(state, cfg)
        assert "此刻心情" in prompt

        # Force expiry in the past and rebuild: the line must disappear.
        _add(store, "u1b", "谢谢你", now - 10 * _HOUR_MS)
        refresh_mood(store, "linwan", "owner", now_ms=now - 9 * _HOUR_MS)
        store.save_mood(
            actor_id="linwan",
            principal_id="owner",
            label="happy",
            valence=0.6,
            arousal=0.4,
            source_event_id=store.events_for_actor("linwan", "owner")[-1].event_id,
            expires_at_ms=now - 1,
        )
        assert "此刻心情" not in build_system_prompt(state, cfg)
