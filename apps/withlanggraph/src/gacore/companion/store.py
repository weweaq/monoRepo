"""Minimal Companion V1 SQLite store: events, reflections, sources, cursor.

Design: ``docs/companion-self-memory-mvp.md`` §5. The store is the single fact
source for what the character really received and really said. V1 deliberately
has no outbox, no episode projection, and no cross-actor visibility.

Every connection enables ``foreign_keys``, WAL and a busy timeout. Schema
creation runs in one ``BEGIN IMMEDIATE`` transaction and stamps
``PRAGMA user_version = 1`` only after every statement succeeded.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gacore.config import Config

SCHEMA_VERSION: Final = 1
DEFAULT_BUSY_TIMEOUT_MS: Final = 5000

# Versioned registry: V1 allows the three message events only. A future version
# extends this dict instead of rebuilding the event table or adding a CHECK.
EVENT_KINDS: Final[dict[int, frozenset[str]]] = {
    1: frozenset({"user.message.received", "agent.message.sent", "proactive.message.sent"}),
}
SUPPORTED_EVENT_KINDS: Final = EVENT_KINDS[SCHEMA_VERSION]


class CompanionStoreError(Exception):
    """Base class for store contract violations."""


class UnknownEventKind(CompanionStoreError):
    """Raised when an event kind is not in the current version's registry."""


class SourceOwnershipError(CompanionStoreError):
    """Raised when a reflection cites an event that does not belong to its actor."""


@dataclass(frozen=True, slots=True)
class AppendResult:
    inserted: bool
    event_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class CompanionEvent:
    seq: int
    event_id: str
    idempotency_key: str
    actor_id: str
    principal_id: str
    kind: str
    summary: str
    source_id: str
    occurred_at_ms: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ReflectionRecord:
    reflection_id: str
    actor_id: str
    principal_id: str
    statement: str
    confidence: float
    prompt_version: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ReflectionDraft:
    statement: str
    confidence: float
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Cursor:
    actor_id: str
    principal_id: str
    last_reflected_seq: int = 0
    retry_batch_end_seq: int | None = None
    failure_count: int = 0
    last_error: str | None = None
    last_reflected_at_ms: int | None = None


_TIMESTAMP_DEFAULT: Final = "datetime('now','+8 hours')"

MIGRATION_V1: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS companion_event (
        seq              INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id         TEXT NOT NULL UNIQUE,
        idempotency_key  TEXT NOT NULL UNIQUE,
        actor_id         TEXT NOT NULL,
        principal_id     TEXT NOT NULL,
        kind             TEXT NOT NULL,
        summary          TEXT NOT NULL,
        source_id        TEXT NOT NULL,
        occurred_at_ms   INTEGER NOT NULL,
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_companion_event_actor_seq
    ON companion_event(actor_id, principal_id, seq)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_reflection (
        reflection_id    TEXT PRIMARY KEY,
        actor_id         TEXT NOT NULL,
        principal_id     TEXT NOT NULL,
        statement        TEXT NOT NULL,
        confidence       REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
        prompt_version   TEXT NOT NULL,
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_reflection_source (
        reflection_id    TEXT NOT NULL REFERENCES companion_reflection(reflection_id),
        event_id         TEXT NOT NULL REFERENCES companion_event(event_id),
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        PRIMARY KEY (reflection_id, event_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_cursor (
        actor_id             TEXT NOT NULL,
        principal_id         TEXT NOT NULL,
        last_reflected_seq   INTEGER NOT NULL DEFAULT 0,
        retry_batch_end_seq  INTEGER,
        failure_count        INTEGER NOT NULL DEFAULT 0,
        last_error           TEXT,
        last_reflected_at_ms INTEGER,
        created_at           TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at           TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        PRIMARY KEY (actor_id, principal_id)
    )
    """,
)


def companion_db_path(cfg: Config) -> Path:
    """Resolve the Companion DB path, defaulting to ``<root>/data/companion.db``."""
    return cfg.companion_db_path or (cfg.root / "data" / "companion.db")


def apply_migration(
    conn: sqlite3.Connection, statements: Sequence[str] = MIGRATION_V1
) -> None:
    """Create schema v1 atomically; a failure rolls back and leaves user_version untouched."""
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= SCHEMA_VERSION:
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in statements:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def _require_kind(kind: str) -> None:
    if kind not in SUPPORTED_EVENT_KINDS:
        raise UnknownEventKind(
            f"event kind {kind!r} is not in v{SCHEMA_VERSION} registry {sorted(SUPPORTED_EVENT_KINDS)}"
        )


def _require_non_empty(field: str, value: str) -> None:
    if not value:
        raise ValueError(f"{field} must not be empty")


class CompanionStore:
    """SQLite-backed Companion fact source.

    A store instance is cheap to share across threads: every call opens its own
    connection, so concurrent writers never share a cursor.
    """

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_migration(conn)

    @classmethod
    def from_config(cls, cfg: Config, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> CompanionStore:
        """Build a store from a Config, creating ``<root>/data`` when needed."""
        return cls(companion_db_path(cfg), busy_timeout_ms=busy_timeout_ms)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run one explicit ``BEGIN IMMEDIATE`` transaction; roll back on any failure."""
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

    def append_event(
        self,
        *,
        kind: str,
        actor_id: str,
        principal_id: str,
        source_id: str,
        summary: str,
        idempotency_key: str,
        event_id: str | None = None,
        occurred_at_ms: int | None = None,
    ) -> AppendResult:
        """Append one event; a repeated ``idempotency_key`` returns the existing row."""
        _require_kind(kind)
        for field, value in (
            ("actor_id", actor_id),
            ("principal_id", principal_id),
            ("source_id", source_id),
            ("idempotency_key", idempotency_key),
        ):
            _require_non_empty(field, value)
        resolved_id = event_id or f"evt_{uuid.uuid4().hex}"
        occurred = int(time.time() * 1000) if occurred_at_ms is None else int(occurred_at_ms)
        with self._transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO companion_event (
                    event_id, idempotency_key, actor_id, principal_id,
                    kind, summary, source_id, occurred_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (resolved_id, idempotency_key, actor_id, principal_id, kind, summary, source_id, occurred),
            )
            inserted = cur.rowcount == 1
            row = conn.execute(
                "SELECT seq, event_id FROM companion_event WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:  # pragma: no cover - the insert above always leaves a row
            raise CompanionStoreError(f"event {idempotency_key!r} vanished after append")
        return AppendResult(inserted=inserted, event_id=row["event_id"], seq=int(row["seq"]))

    def events_after(
        self, actor_id: str, principal_id: str, seq: int, *, limit: int = 10
    ) -> list[CompanionEvent]:
        """Events of one actor with ``seq > seq``, oldest first."""
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_event
                WHERE actor_id = ? AND principal_id = ? AND seq > ?
                ORDER BY seq ASC LIMIT ?
                """,
                (actor_id, principal_id, int(seq), int(limit)),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def events_between(
        self, actor_id: str, principal_id: str, start_seq: int, end_seq: int
    ) -> list[CompanionEvent]:
        """Events of one actor in ``[start_seq, end_seq]``, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_event
                WHERE actor_id = ? AND principal_id = ? AND seq BETWEEN ? AND ?
                ORDER BY seq ASC
                """,
                (actor_id, principal_id, int(start_seq), int(end_seq)),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def latest_reflections(
        self, actor_id: str, principal_id: str, *, limit: int = 3
    ) -> list[ReflectionRecord]:
        """Most recent reflections of one actor, newest first."""
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_reflection
                WHERE actor_id = ? AND principal_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (actor_id, principal_id, int(limit)),
            ).fetchall()
        return [_row_to_reflection(row) for row in rows]

    def get_cursor(self, actor_id: str, principal_id: str) -> Cursor:
        """Current cursor for one actor; a missing row is an all-zero cursor."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM companion_cursor WHERE actor_id = ? AND principal_id = ?",
                (actor_id, principal_id),
            ).fetchone()
        if row is None:
            return Cursor(actor_id=actor_id, principal_id=principal_id)
        return Cursor(
            actor_id=row["actor_id"],
            principal_id=row["principal_id"],
            last_reflected_seq=int(row["last_reflected_seq"]),
            retry_batch_end_seq=row["retry_batch_end_seq"],
            failure_count=int(row["failure_count"]),
            last_error=row["last_error"],
            last_reflected_at_ms=row["last_reflected_at_ms"],
        )

    def commit_reflection_batch(
        self,
        *,
        actor_id: str,
        principal_id: str,
        reflections: Sequence[ReflectionDraft],
        prompt_version: str,
        last_reflected_seq: int,
        retry_batch_end_seq: int | None = None,
        failure_count: int = 0,
        last_error: str | None = None,
        last_reflected_at_ms: int | None = None,
    ) -> tuple[str, ...]:
        """Insert reflections + sources and update the cursor in one transaction.

        Every cited event must exist for the same ``(actor_id, principal_id)``;
        otherwise the whole batch is rejected before anything is written.
        """
        with self._transaction() as conn:
            _assert_sources_owned(conn, actor_id, principal_id, reflections)
            reflection_ids: list[str] = []
            for draft in reflections:
                reflection_id = f"refl_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO companion_reflection (
                        reflection_id, actor_id, principal_id,
                        statement, confidence, prompt_version
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (reflection_id, actor_id, principal_id, draft.statement, draft.confidence, prompt_version),
                )
                for event_id in dict.fromkeys(draft.source_event_ids):
                    conn.execute(
                        """
                        INSERT INTO companion_reflection_source (reflection_id, event_id)
                        VALUES (?, ?)
                        """,
                        (reflection_id, event_id),
                    )
                reflection_ids.append(reflection_id)
            conn.execute(
                f"""
                INSERT INTO companion_cursor (
                    actor_id, principal_id, last_reflected_seq, retry_batch_end_seq,
                    failure_count, last_error, last_reflected_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_id, principal_id) DO UPDATE SET
                    last_reflected_seq = excluded.last_reflected_seq,
                    retry_batch_end_seq = excluded.retry_batch_end_seq,
                    failure_count = excluded.failure_count,
                    last_error = excluded.last_error,
                    last_reflected_at_ms = excluded.last_reflected_at_ms,
                    updated_at = {_TIMESTAMP_DEFAULT}
                """,
                (
                    actor_id,
                    principal_id,
                    int(last_reflected_seq),
                    retry_batch_end_seq,
                    int(failure_count),
                    last_error,
                    last_reflected_at_ms,
                ),
            )
        return tuple(reflection_ids)


def _assert_sources_owned(
    conn: sqlite3.Connection,
    actor_id: str,
    principal_id: str,
    reflections: Sequence[ReflectionDraft],
) -> None:
    event_ids = {event_id for draft in reflections for event_id in draft.source_event_ids}
    if not event_ids:
        return
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(
        f"SELECT event_id, actor_id, principal_id FROM companion_event WHERE event_id IN ({placeholders})",
        tuple(event_ids),
    ).fetchall()
    owned = {
        row["event_id"]
        for row in rows
        if row["actor_id"] == actor_id and row["principal_id"] == principal_id
    }
    missing = event_ids - owned
    if missing:
        raise SourceOwnershipError(
            f"reflection sources not owned by ({actor_id!r}, {principal_id!r}): {sorted(missing)}"
        )


def _row_to_event(row: sqlite3.Row) -> CompanionEvent:
    return CompanionEvent(
        seq=int(row["seq"]),
        event_id=row["event_id"],
        idempotency_key=row["idempotency_key"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        kind=row["kind"],
        summary=row["summary"],
        source_id=row["source_id"],
        occurred_at_ms=int(row["occurred_at_ms"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_reflection(row: sqlite3.Row) -> ReflectionRecord:
    return ReflectionRecord(
        reflection_id=row["reflection_id"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        statement=row["statement"],
        confidence=float(row["confidence"]),
        prompt_version=row["prompt_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
