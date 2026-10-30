"""Tests for the minimal Companion V1 SQLite store.

Covers schema migration (idempotent, atomic failure), per-connection PRAGMAs,
event idempotency and actor isolation, reflection/source/cursor atomicity, FK
enforcement, and independent concurrent writers.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gacore.companion.store import (
    SCHEMA_VERSION,
    MIGRATION_V1,
    AppendResult,
    CompanionStore,
    Cursor,
    ReflectionDraft,
    SourceOwnershipError,
    UnknownEventKind,
    apply_migration,
    companion_db_path,
)
from gacore.config import Config


def _store(tmp_path: Path) -> CompanionStore:
    return CompanionStore(tmp_path / "companion.db")


def _append(
    store: CompanionStore,
    key: str,
    *,
    actor: str = "linwan",
    principal: str = "owner",
    kind: str = "user.message.received",
    summary: str = "hello",
) -> AppendResult:
    return store.append_event(
        kind=kind,
        actor_id=actor,
        principal_id=principal,
        source_id=f"src-{key}",
        summary=summary,
        idempotency_key=key,
    )


def _seed(store: CompanionStore, count: int, *, actor: str = "linwan", principal: str = "owner") -> list[str]:
    return [
        _append(store, f"seed-{actor}-{principal}-{i}", actor=actor, principal=principal).event_id
        for i in range(count)
    ]


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


class TestSchema:
    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "companion.db"
        CompanionStore(path)
        CompanionStore(path)  # second init must be a no-op

        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()
        assert {
            "companion_event",
            "companion_reflection",
            "companion_reflection_source",
            "companion_cursor",
        } <= _table_names(path)

    def test_migration_failure_rolls_back_and_leaves_version_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.db"
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                apply_migration(
                    conn,
                    statements=(MIGRATION_V1[0], "CREATE TABLE broken ("),
                    target_version=1,
                )
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            conn.close()
        assert "companion_event" not in tables

    def test_overriding_statements_without_a_target_version_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.db"
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            with pytest.raises(ValueError):
                apply_migration(conn, statements=(MIGRATION_V1[0],))
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        finally:
            conn.close()

    def test_every_connection_sets_pragmas(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for _ in range(2):  # a fresh connection must re-apply them, not inherit migration's
            with store._connect() as conn:
                assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
                assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


class TestEvents:
    def test_idempotency_key_deduplicates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        first = _append(store, "qq-user:1")
        second = _append(store, "qq-user:1")

        assert first.inserted is True
        assert second.inserted is False
        assert (first.event_id, first.seq) == (second.event_id, second.seq)
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM companion_event").fetchone()[0] == 1

    def test_unknown_event_kind_is_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(UnknownEventKind):
            _append(store, "k", kind="tool.result")

    def test_events_after_and_between_are_actor_scoped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a1 = _append(store, "a1", actor="a")
        _append(store, "b1", actor="b")
        a2 = _append(store, "a2", actor="a")

        after = store.events_after("a", "owner", a1.seq)
        assert [e.event_id for e in after] == [a2.event_id]
        between = store.events_between("a", "owner", a1.seq, a2.seq)
        assert [e.event_id for e in between] == [a1.event_id, a2.event_id]
        assert store.events_after("a", "owner", a2.seq, limit=10) == []

    def test_events_after_respects_limit_and_returns_oldest_first(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        ids = _seed(store, 4)
        rows = store.events_after("linwan", "owner", 0, limit=3)

        assert [r.event_id for r in rows] == ids[:3]
        assert [r.seq for r in rows] == sorted(r.seq for r in rows)


class TestReflections:
    def test_commit_persists_reflection_sources_and_cursor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        events = _seed(store, 3)
        draft = ReflectionDraft(
            statement="技术分享更容易得到回应",
            confidence=0.78,
            source_event_ids=(events[0], events[1]),
        )

        (reflection_id,) = store.commit_reflection_batch(
            actor_id="linwan",
            principal_id="owner",
            reflections=[draft],
            prompt_version="v1",
            last_reflected_seq=3,
            last_reflected_at_ms=123,
        )

        latest = store.latest_reflections("linwan", "owner")
        assert [r.reflection_id for r in latest] == [reflection_id]
        assert latest[0].statement == draft.statement
        assert latest[0].confidence == pytest.approx(0.78)
        cursor = store.get_cursor("linwan", "owner")
        assert cursor.last_reflected_seq == 3
        assert cursor.retry_batch_end_seq is None
        assert cursor.failure_count == 0
        assert cursor.last_reflected_at_ms == 123
        with store._connect() as conn:
            sources = conn.execute(
                "SELECT event_id FROM companion_reflection_source WHERE reflection_id = ? ORDER BY event_id",
                (reflection_id,),
            ).fetchall()
        assert {row["event_id"] for row in sources} == {events[0], events[1]}

    def test_commit_rejects_sources_of_another_actor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _append(store, "mine", actor="a")
        foreign = _append(store, "theirs", actor="b").event_id

        with pytest.raises(SourceOwnershipError):
            store.commit_reflection_batch(
                actor_id="a",
                principal_id="owner",
                reflections=[ReflectionDraft("bad", 0.5, (foreign,))],
                prompt_version="v1",
                last_reflected_seq=1,
            )
        assert store.latest_reflections("a", "owner") == []
        assert store.get_cursor("a", "owner") == Cursor(actor_id="a", principal_id="owner")

    def test_missing_source_is_rejected_before_writes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _append(store, "real", actor="a")

        with pytest.raises(SourceOwnershipError):
            store.commit_reflection_batch(
                actor_id="a",
                principal_id="owner",
                reflections=[ReflectionDraft("bad", 0.5, ("evt_missing",))],
                prompt_version="v1",
                last_reflected_seq=1,
            )
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM companion_reflection").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM companion_reflection_source").fetchone()[0] == 0

    def test_reflection_check_violation_rolls_back_everything(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        events = _seed(store, 2)
        good = ReflectionDraft("ok", 0.5, (events[0], events[1]))
        bad = ReflectionDraft("bad", 1.5, (events[0],))  # violates the confidence CHECK

        with pytest.raises(sqlite3.IntegrityError):
            store.commit_reflection_batch(
                actor_id="linwan",
                principal_id="owner",
                reflections=[good, bad],
                prompt_version="v1",
                last_reflected_seq=2,
                last_reflected_at_ms=1,
            )

        assert store.latest_reflections("linwan", "owner") == []
        assert store.get_cursor("linwan", "owner") == Cursor(actor_id="linwan", principal_id="owner")
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM companion_reflection").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM companion_reflection_source").fetchone()[0] == 0

    def test_reflection_source_fk_is_enforced(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        events = _seed(store, 2)
        (reflection_id,) = store.commit_reflection_batch(
            actor_id="linwan",
            principal_id="owner",
            reflections=[ReflectionDraft("ok", 0.5, (events[0], events[1]))],
            prompt_version="v1",
            last_reflected_seq=2,
        )
        with pytest.raises(sqlite3.IntegrityError):
            with store._transaction() as conn:
                conn.execute(
                    "INSERT INTO companion_reflection_source (reflection_id, event_id) VALUES (?, ?)",
                    (reflection_id, "evt_missing"),
                )

    def test_reflections_are_actor_scoped_and_newest_first(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a_events = _seed(store, 4, actor="a")
        b_events = _seed(store, 1, actor="b")

        store.commit_reflection_batch(
            actor_id="a",
            principal_id="owner",
            reflections=[ReflectionDraft(f"a-{i}", 0.5, (a_events[0], a_events[1])) for i in range(4)],
            prompt_version="v1",
            last_reflected_seq=4,
        )
        store.commit_reflection_batch(
            actor_id="b",
            principal_id="owner",
            reflections=[ReflectionDraft("b-0", 0.5, (b_events[0],))],
            prompt_version="v1",
            last_reflected_seq=1,
        )

        latest = store.latest_reflections("a", "owner", limit=3)
        assert [r.statement for r in latest] == ["a-3", "a-2", "a-1"]
        assert [r.statement for r in store.latest_reflections("b", "owner")] == ["b-0"]


class TestConcurrency:
    def test_independent_writers_do_not_lose_rows(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        per_thread = 25
        threads = 4

        def writer(index: int) -> int:
            for i in range(per_thread):
                _append(store, f"t{index}-{i}", actor="linwan")
            return per_thread

        with ThreadPoolExecutor(max_workers=threads) as pool:
            assert sum(pool.map(writer, range(threads))) == threads * per_thread

        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM companion_event").fetchone()[0] == threads * per_thread
            seqs = [row[0] for row in conn.execute("SELECT seq FROM companion_event")]
        assert len(set(seqs)) == threads * per_thread


class TestConfigPaths:
    def test_for_tests_points_at_tmp_data_dir(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        assert companion_db_path(cfg) == tmp_path / "data" / "companion.db"

    def test_absolute_env_override_wins(self, tmp_path: Path) -> None:
        target = tmp_path / "custom" / "companion.db"
        cfg = Config.from_env({"COMPANION_DB_PATH": str(target)})
        assert cfg.companion_db_path == target

    def test_relative_env_override_is_rooted_at_project(self) -> None:
        cfg = Config.from_env({"COMPANION_DB_PATH": "custom/companion.db"})
        assert cfg.companion_db_path == cfg.root / "custom" / "companion.db"

    def test_blank_env_falls_back_to_default(self) -> None:
        # .env.example ships ``COMPANION_DB_PATH=``; load_dotenv exports it as "".
        cfg = Config.from_env({"COMPANION_DB_PATH": ""})
        assert companion_db_path(cfg) == cfg.root / "data" / "companion.db"

    def test_from_config_creates_parent_dir(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        store = CompanionStore.from_config(cfg)

        assert store.db_path == tmp_path / "data" / "companion.db"
        assert store.db_path.parent.is_dir()
