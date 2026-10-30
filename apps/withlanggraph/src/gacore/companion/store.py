"""Companion SQLite store: events, reflections, journals, sources, cursor.

Design: ``docs/companion-self-memory-mvp.md`` §5 and
``docs/companion-self-memory-v2-plan.md`` §4.1. The store is the single fact
source for what the character really received and really said; journals are a
readable projection of those facts, never a second source.

Every connection enables ``foreign_keys``, WAL and a busy timeout. Each schema
step runs in its own ``BEGIN IMMEDIATE`` transaction and stamps
``PRAGMA user_version`` only after every statement in that step succeeded, so a
v1 database upgrades in place without losing events.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Final

from gacore.config import Config
from gacore.jsonl_logger import get_logger

SCHEMA_VERSION: Final = 8

logger = get_logger("companion.store")
DEFAULT_BUSY_TIMEOUT_MS: Final = 5000

# Versioned registry: V1/V2 allow the three message events only. Journal data is a
# projection of those events, so v2 adds no event kind. A future version extends
# this dict instead of rebuilding the event table or adding a CHECK.
_EVENT_KINDS_V1: Final = frozenset(
    {"user.message.received", "agent.message.sent", "proactive.message.sent"}
)
# v6 adds the goal/read-action kinds: a goal's lifecycle must be evidenced by real
# events, never by a mutable status column alone.
_EVENT_KINDS_V6: Final = _EVENT_KINDS_V1 | frozenset(
    {
        "read_action.succeeded",
        "read_action.failed",
        "goal.started",
        "goal.succeeded",
        "goal.failed",
        "goal.cancelled",
        "commitment.created",
        "commitment.succeeded",
        "commitment.failed",
        "commitment.cancelled",
    }
)
# v7 adds the activity lifecycle, so "paused/completed" is evidence, not just a column.
_EVENT_KINDS_V7: Final = _EVENT_KINDS_V6 | frozenset(
    {
        "activity.started",
        "activity.paused",
        "activity.resumed",
        "activity.completed",
        "activity.failed",
        "activity.cancelled",
    }
)
EVENT_KINDS: Final[dict[int, frozenset[str]]] = {
    1: _EVENT_KINDS_V1,
    2: _EVENT_KINDS_V1,
    3: _EVENT_KINDS_V1,
    4: _EVENT_KINDS_V1,
    5: _EVENT_KINDS_V1,
    6: _EVENT_KINDS_V6,
    7: _EVENT_KINDS_V7,
    8: _EVENT_KINDS_V7,
}
SUPPORTED_EVENT_KINDS: Final = EVENT_KINDS[SCHEMA_VERSION]

# Reflection lifecycle (V2-Step 7). A statement below this confidence is never injected.
MIN_INJECT_CONFIDENCE: Final = 0.5
REFLECTION_STATUSES: Final = frozenset({"active", "stale", "contradicted", "retracted"})

# Relationship vocabulary (V2-Step 2). The projector is the only writer, and stage is
# derived by rules — a model may never set it directly.
RELATIONSHIP_STAGES: Final = frozenset({"new", "familiar", "established"})
RELATIONSHIP_STATUSES: Final = frozenset({"stable", "strained", "repairing"})

# Commitment lifecycle (V2-Step 4). A promise only moves forward:
# proposed → active → overdue → failed/succeeded, and anything open may be cancelled.
COMMITMENT_STATUSES: Final = frozenset(
    {"proposed", "active", "overdue", "succeeded", "failed", "cancelled"}
)
COMMITMENT_OPEN_STATUSES: Final = frozenset({"proposed", "active", "overdue"})

# Activity / outreach vocabulary (V2-Step 6).
ACTIVITY_STATUSES: Final = frozenset(
    {"planned", "active", "paused", "completed", "failed", "cancelled"}
)
ACTIVITY_TERMINAL_STATUSES: Final = frozenset({"completed", "failed", "cancelled"})
OUTREACH_KINDS: Final = frozenset(
    {"positive", "negative", "boundary", "topic_shift", "no_response"}
)

# Goal lifecycle (V2-Step 5). One goal at a time may be in_progress (partial unique
# index); execution is restricted to the read-only tool whitelist below.
GOAL_STATUSES: Final = frozenset(
    {"proposed", "approved", "in_progress", "succeeded", "failed", "cancelled"}
)
GOAL_TERMINAL_STATUSES: Final = frozenset({"succeeded", "failed", "cancelled"})

# Mood vocabulary (V2-Step 3). ``neutral`` is the absence of a live mood, so the
# prompt block is simply omitted; the other labels always carry a source event.
MOOD_LABELS: Final = frozenset(
    {"neutral", "curious", "happy", "tired", "concerned", "frustrated"}
)

JOURNAL_VISIBILITIES: Final = frozenset({"to_owner", "private"})
JOURNAL_STATUSES: Final = frozenset({"draft", "final", "stale"})
# UTC+8 is the product's "today" (R6 timestamps use the same offset).
_DAY_TZ: Final = timezone(timedelta(hours=8))


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
    """One self-knowledge statement plus its lifecycle (V2-Step 7)."""

    reflection_id: str
    actor_id: str
    principal_id: str
    statement: str
    confidence: float
    prompt_version: str
    created_at: str
    updated_at: str
    status: str = "active"
    topic_tags: tuple[str, ...] = ()
    valid_from_ms: int | None = None
    valid_to_ms: int | None = None
    expires_at_ms: int | None = None
    supersedes_id: str | None = None
    refutes_id: str | None = None
    projector_version: str | None = None


@dataclass(frozen=True, slots=True)
class ReflectionDraft:
    statement: str
    confidence: float
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JournalRecord:
    journal_id: str
    actor_id: str
    principal_id: str
    day: str
    visibility: str
    body: str
    prompt_version: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JournalDraft:
    """One journal entry to persist; sources must all fall inside the journal day."""

    visibility: str
    body: str
    source_event_ids: tuple[str, ...] = ()
    source_reflection_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JournalSaveResult:
    """Journal ids in draft order plus whether each draft was actually written."""

    ids: tuple[str, ...]
    created: tuple[bool, ...]

    @property
    def wrote_any(self) -> bool:
        """True when at least one draft was inserted or rewrote a stale row."""
        return any(self.created)


@dataclass(frozen=True, slots=True)
class JournalCorrection:
    """One owner correction against a journal, with the events it disputes."""

    correction_id: str
    journal_id: str
    note: str
    source_event_ids: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Cursor:
    actor_id: str
    principal_id: str
    last_reflected_seq: int = 0
    retry_batch_end_seq: int | None = None
    failure_count: int = 0
    last_error: str | None = None
    last_reflected_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Relationship:
    """Derived relationship state: verifiable counters plus a rule-derived stage/status."""

    actor_id: str
    principal_id: str
    stage: str
    status: str
    meaningful_interactions: int
    active_days: int
    positive_feedback: int
    user_boundaries: tuple[dict[str, object], ...]
    actor_boundaries: tuple[str, ...]
    last_negative_event_id: str | None
    last_repair_event_id: str | None
    last_interaction_at_ms: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OpenThread:
    """One unresolved topic the owner raised and the character has not answered yet."""

    open_thread_id: str
    actor_id: str
    principal_id: str
    topic: str
    source_event_id: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Commitment:
    """One promise the character actually made in a confirmed send."""

    commitment_id: str
    actor_id: str
    principal_id: str
    source_event_id: str
    quote: str
    action: str
    due_at_ms: int | None
    success_criteria: str
    status: str
    result_event_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Activity:
    """One bounded, pausable piece of work the character is doing."""

    activity_id: str
    actor_id: str
    principal_id: str
    kind: str
    status: str
    started_at_ms: int | None
    resume_after_ms: int | None
    source_goal_id: str | None
    last_result_event_id: str | None
    budget: dict[str, object]
    spent: dict[str, object]
    reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SelfPreference:
    """The character's own taste for a topic: decays, can invert, never touches persona."""

    actor_id: str
    principal_id: str
    topic: str
    weight: float
    novelty: float
    fatigue: float
    source_event_ids: tuple[str, ...]
    expires_at_ms: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OutreachFeedback:
    """What happened after one proactive/agent outreach — linked to its sent event."""

    outreach_id: str
    actor_id: str
    principal_id: str
    sent_event_id: str
    reply_event_id: str | None
    kind: str
    response_delay_ms: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Goal:
    """One small, read-only, cancellable objective derived from a promise/reflection."""

    goal_id: str
    actor_id: str
    principal_id: str
    commitment_id: str | None
    text: str
    status: str
    allowed_tools: tuple[str, ...]
    deadline_ms: int
    result_event_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Mood:
    """One short-lived emotional state, always traceable to a real event."""

    actor_id: str
    principal_id: str
    label: str
    valence: float
    arousal: float
    source_event_id: str
    expires_at_ms: int
    created_at: str
    updated_at: str


_TIMESTAMP_DEFAULT: Final = "datetime('now','+8 hours')"


def day_key(ms: int) -> str:
    """Product-local (UTC+8) ``YYYY-MM-DD`` for one epoch-millisecond timestamp."""
    return datetime.fromtimestamp(ms / 1000, tz=_DAY_TZ).date().isoformat()


def day_bounds_ms(day: str) -> tuple[int, int]:
    """Half-open ``[start, end)`` epoch-millisecond bounds of one product-local day.

    Only a bare ``YYYY-MM-DD`` is accepted. ``datetime.fromisoformat`` would also
    take ``2026-10-29T08:00:00+00:00`` and silently reinterpret that instant as a
    UTC+8 wall clock, so the date is parsed strictly instead.
    """
    try:
        start_date = date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError(f"day must be YYYY-MM-DD, got {day!r}") from exc
    start = datetime.combine(start_date, dt_time.min, tzinfo=_DAY_TZ)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


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

MIGRATION_V2: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS companion_journal (
        journal_id       TEXT PRIMARY KEY,
        actor_id         TEXT NOT NULL,
        principal_id     TEXT NOT NULL,
        day              TEXT NOT NULL,
        visibility       TEXT NOT NULL CHECK (visibility IN ('to_owner', 'private')),
        body             TEXT NOT NULL,
        prompt_version   TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('draft', 'final', 'stale')),
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        UNIQUE (actor_id, principal_id, day, visibility)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_companion_journal_actor_day
    ON companion_journal(actor_id, principal_id, day)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_journal_event_source (
        journal_id       TEXT NOT NULL REFERENCES companion_journal(journal_id),
        event_id         TEXT NOT NULL REFERENCES companion_event(event_id),
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        PRIMARY KEY (journal_id, event_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_journal_reflection_source (
        journal_id       TEXT NOT NULL REFERENCES companion_journal(journal_id),
        reflection_id    TEXT NOT NULL REFERENCES companion_reflection(reflection_id),
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        PRIMARY KEY (journal_id, reflection_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_companion_event_actor_occurred
    ON companion_event(actor_id, principal_id, occurred_at_ms)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_journal_correction (
        correction_id    TEXT PRIMARY KEY,
        journal_id       TEXT NOT NULL REFERENCES companion_journal(journal_id),
        note             TEXT NOT NULL,
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_companion_journal_correction_journal
    ON companion_journal_correction(journal_id)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_journal_correction_source (
        correction_id    TEXT NOT NULL REFERENCES companion_journal_correction(correction_id),
        event_id         TEXT NOT NULL REFERENCES companion_event(event_id),
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        PRIMARY KEY (correction_id, event_id)
    )
    """,
)

MIGRATION_V3: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS companion_relationship (
        actor_id                TEXT NOT NULL,
        principal_id            TEXT NOT NULL,
        stage                   TEXT NOT NULL CHECK (stage IN (
                                  'new', 'familiar', 'established'
                                )),
        status                  TEXT NOT NULL DEFAULT 'stable' CHECK (status IN (
                                  'stable', 'strained', 'repairing'
                                )),
        meaningful_interactions INTEGER NOT NULL DEFAULT 0,
        active_days             INTEGER NOT NULL DEFAULT 0,
        positive_feedback       INTEGER NOT NULL DEFAULT 0,
        user_boundaries_json    TEXT NOT NULL DEFAULT '[]'
                                  CHECK (json_valid(user_boundaries_json)),
        actor_boundaries_json   TEXT NOT NULL DEFAULT '[]'
                                  CHECK (json_valid(actor_boundaries_json)),
        last_negative_event_id  TEXT REFERENCES companion_event(event_id),
        last_repair_event_id    TEXT REFERENCES companion_event(event_id),
        last_interaction_at_ms  INTEGER,
        created_at              TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at              TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        PRIMARY KEY (actor_id, principal_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_open_thread (
        open_thread_id   TEXT PRIMARY KEY,
        actor_id         TEXT NOT NULL,
        principal_id     TEXT NOT NULL,
        topic            TEXT NOT NULL,
        source_event_id  TEXT NOT NULL REFERENCES companion_event(event_id),
        status           TEXT NOT NULL CHECK (status IN (
                           'open', 'resolved', 'cancelled'
                         )),
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        FOREIGN KEY (actor_id, principal_id)
            REFERENCES companion_relationship(actor_id, principal_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_companion_open_thread_actor
    ON companion_open_thread(actor_id, principal_id, status)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_companion_open_thread_topic
    ON companion_open_thread(actor_id, principal_id, topic)
    WHERE status = 'open'
    """,
)

MIGRATION_V4: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS companion_mood (
        actor_id         TEXT NOT NULL,
        principal_id     TEXT NOT NULL,
        label            TEXT NOT NULL CHECK (label IN (
                           'neutral', 'curious', 'happy',
                           'tired', 'concerned', 'frustrated'
                         )),
        valence          REAL NOT NULL CHECK (valence >= -1 AND valence <= 1),
        arousal          REAL NOT NULL CHECK (arousal >= 0 AND arousal <= 1),
        source_event_id  TEXT NOT NULL REFERENCES companion_event(event_id),
        expires_at_ms    INTEGER NOT NULL,
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        PRIMARY KEY (actor_id, principal_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_companion_mood_expiry
    ON companion_mood(expires_at_ms)
    """,
)

MIGRATION_V5: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS companion_commitment (
        commitment_id    TEXT PRIMARY KEY,
        actor_id         TEXT NOT NULL,
        principal_id     TEXT NOT NULL,
        source_event_id  TEXT NOT NULL REFERENCES companion_event(event_id),
        quote            TEXT NOT NULL,
        action           TEXT NOT NULL,
        due_at_ms        INTEGER,
        success_criteria TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN (
                           'proposed', 'active', 'overdue',
                           'succeeded', 'failed', 'cancelled'
                         )),
        result_event_id  TEXT REFERENCES companion_event(event_id),
        created_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at       TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_companion_commitment_key
    ON companion_commitment(actor_id, principal_id, source_event_id, quote)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_companion_commitment_status
    ON companion_commitment(actor_id, principal_id, status)
    """,
)

MIGRATION_V6: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS companion_goal (
        goal_id            TEXT PRIMARY KEY,
        actor_id           TEXT NOT NULL,
        principal_id       TEXT NOT NULL,
        commitment_id      TEXT REFERENCES companion_commitment(commitment_id),
        text               TEXT NOT NULL,
        status             TEXT NOT NULL CHECK (status IN (
                             'proposed', 'approved', 'in_progress',
                             'succeeded', 'failed', 'cancelled'
                           )),
        allowed_tools_json TEXT NOT NULL DEFAULT '[]'
                             CHECK (json_valid(allowed_tools_json)),
        deadline_ms        INTEGER NOT NULL,
        result_event_id    TEXT REFERENCES companion_event(event_id),
        created_at         TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at         TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_goal
    ON companion_goal(actor_id, principal_id)
    WHERE status = 'in_progress'
    """,
)

MIGRATION_V7: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS companion_activity (
        activity_id          TEXT PRIMARY KEY,
        actor_id             TEXT NOT NULL,
        principal_id         TEXT NOT NULL,
        kind                 TEXT NOT NULL,
        status               TEXT NOT NULL CHECK (status IN (
                               'planned', 'active', 'paused',
                               'completed', 'failed', 'cancelled'
                             )),
        started_at_ms        INTEGER,
        resume_after_ms      INTEGER,
        source_goal_id       TEXT REFERENCES companion_goal(goal_id),
        last_result_event_id TEXT REFERENCES companion_event(event_id),
        budget_json          TEXT NOT NULL DEFAULT '{{}}'
                               CHECK (json_valid(budget_json)),
        spent_json           TEXT NOT NULL DEFAULT '{{}}'
                               CHECK (json_valid(spent_json)),
        reason               TEXT,
        created_at           TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at           TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_one_running_activity
    ON companion_activity(actor_id, principal_id)
    WHERE status IN ('active', 'paused')
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_self_preference (
        actor_id              TEXT NOT NULL,
        principal_id          TEXT NOT NULL,
        topic                 TEXT NOT NULL,
        weight                REAL NOT NULL CHECK (weight >= -1 AND weight <= 1),
        novelty               REAL NOT NULL DEFAULT 0.5
                                CHECK (novelty >= 0 AND novelty <= 1),
        fatigue               REAL NOT NULL DEFAULT 0
                                CHECK (fatigue >= 0 AND fatigue <= 1),
        source_event_ids_json TEXT NOT NULL DEFAULT '[]'
                                CHECK (json_valid(source_event_ids_json)),
        expires_at_ms         INTEGER,
        created_at            TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at            TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        PRIMARY KEY (actor_id, principal_id, topic)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS companion_outreach_feedback (
        outreach_id       TEXT PRIMARY KEY,
        actor_id          TEXT NOT NULL,
        principal_id      TEXT NOT NULL,
        sent_event_id     TEXT NOT NULL REFERENCES companion_event(event_id),
        reply_event_id    TEXT REFERENCES companion_event(event_id),
        kind              TEXT NOT NULL CHECK (kind IN (
                            'positive', 'negative', 'boundary',
                            'topic_shift', 'no_response'
                          )),
        response_delay_ms INTEGER,
        created_at        TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT}),
        updated_at        TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_companion_outreach_sent
    ON companion_outreach_feedback(actor_id, principal_id, sent_event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_companion_outreach_kind
    ON companion_outreach_feedback(actor_id, principal_id, kind)
    """,
)

MIGRATION_V8: Final[tuple[str, ...]] = (
    # Reflection lifecycle: a statement can go stale, be contradicted or retracted, and
    # every projection records which projector version produced it.
    "ALTER TABLE companion_reflection ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE companion_reflection ADD COLUMN topic_tags_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE companion_reflection ADD COLUMN valid_from_ms INTEGER",
    "ALTER TABLE companion_reflection ADD COLUMN valid_to_ms INTEGER",
    "ALTER TABLE companion_reflection ADD COLUMN expires_at_ms INTEGER",
    "ALTER TABLE companion_reflection ADD COLUMN supersedes_id TEXT",
    "ALTER TABLE companion_reflection ADD COLUMN refutes_id TEXT",
    "ALTER TABLE companion_reflection ADD COLUMN projector_version TEXT",
    f"""
    CREATE TABLE IF NOT EXISTS companion_projection_state (
        projection        TEXT PRIMARY KEY,
        projector_version TEXT NOT NULL,
        cursor_seq        INTEGER NOT NULL DEFAULT 0,
        updated_at        TEXT NOT NULL DEFAULT ({_TIMESTAMP_DEFAULT})
    )
    """,
)

MIGRATIONS: Final[dict[int, tuple[str, ...]]] = {
    1: MIGRATION_V1,
    2: MIGRATION_V2,
    3: MIGRATION_V3,
    4: MIGRATION_V4,
    5: MIGRATION_V5,
    6: MIGRATION_V6,
    7: MIGRATION_V7,
    8: MIGRATION_V8,
}


def companion_db_path(cfg: Config) -> Path:
    """Resolve the Companion DB path, defaulting to ``<root>/data/companion.db``."""
    return cfg.companion_db_path or (cfg.root / "data" / "companion.db")


def apply_migration(
    conn: sqlite3.Connection,
    statements: Sequence[str] | None = None,
    *,
    target_version: int | None = None,
) -> None:
    """Bring the schema up to ``target_version`` (default: current), one atomic step per version.

    ``statements`` overrides the statements of the target step; tests use it to
    prove a broken step rolls back and leaves ``user_version`` untouched. It
    requires an explicit ``target_version``: otherwise the broken step would be
    stamped with the current version and the next real migration would be skipped.
    """
    if statements is not None and target_version is None:
        raise ValueError("statements= requires an explicit target_version")
    target = SCHEMA_VERSION if target_version is None else int(target_version)
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current >= target:
        return
    if statements is not None:
        steps = [(target, tuple(statements))]
    else:
        steps = [(version, MIGRATIONS[version]) for version in range(current + 1, target + 1)]
    for version, step in steps:
        _apply_migration_step(conn, step, version)


def _apply_migration_step(conn: sqlite3.Connection, statements: Sequence[str], version: int) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in statements:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {int(version)}")
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
        self,
        actor_id: str,
        principal_id: str,
        *,
        limit: int = 3,
        now_ms: int | None = None,
        min_confidence: float = MIN_INJECT_CONFIDENCE,
        status: str = "active",
    ) -> list[ReflectionRecord]:
        """Inject-eligible reflections, ranked by confidence x recency.

        Lifecycle filtering happens here so no caller can accidentally inject a stale,
        retracted or expired statement: only ``active`` rows with enough confidence and a
        live ``expires_at_ms`` are returned, newest-first as a tie-break.
        """
        if limit <= 0:
            return []
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_reflection
                WHERE actor_id = ? AND principal_id = ?
                  AND status = ?
                  AND confidence >= ?
                  AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                ORDER BY confidence DESC, created_at DESC, rowid DESC
                LIMIT ?
                """,
                (actor_id, principal_id, status, float(min_confidence), now, int(limit)),
            ).fetchall()
        return [_row_to_reflection(row) for row in rows]

    def events_for_day(
        self, actor_id: str, principal_id: str, day: str
    ) -> list[CompanionEvent]:
        """Events of one actor that happened inside one product-local day, oldest first."""
        start_ms, end_ms = day_bounds_ms(day)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_event
                WHERE actor_id = ? AND principal_id = ?
                  AND occurred_at_ms >= ? AND occurred_at_ms < ?
                ORDER BY seq ASC
                """,
                (actor_id, principal_id, start_ms, end_ms),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def reflections_for_day(
        self, actor_id: str, principal_id: str, day: str
    ) -> list[ReflectionRecord]:
        """Reflections of one actor written inside one product-local day, oldest first."""
        next_day = day_key(day_bounds_ms(day)[1])
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_reflection
                WHERE actor_id = ? AND principal_id = ?
                  AND created_at >= ? AND created_at < ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (actor_id, principal_id, f"{day} 00:00:00", f"{next_day} 00:00:00"),
            ).fetchall()
        return [_row_to_reflection(row) for row in rows]

    def journals_for_day(
        self, actor_id: str, principal_id: str, day: str
    ) -> list[JournalRecord]:
        """Both journals of one actor for one day, ordered ``private`` then ``to_owner``."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_journal
                WHERE actor_id = ? AND principal_id = ? AND day = ?
                ORDER BY visibility ASC
                """,
                (actor_id, principal_id, day),
            ).fetchall()
        return [_row_to_journal(row) for row in rows]

    def journal_sources(
        self, journal_id: str
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(event_ids, reflection_ids)`` cited by one journal, both ordered."""
        with self._connect() as conn:
            event_ids = tuple(
                row["event_id"]
                for row in conn.execute(
                    """
                    SELECT source.event_id
                    FROM companion_journal_event_source AS source
                    JOIN companion_event AS event ON event.event_id = source.event_id
                    WHERE source.journal_id = ?
                    ORDER BY event.seq
                    """,
                    (journal_id,),
                )
            )
            reflection_ids = tuple(
                row["reflection_id"]
                for row in conn.execute(
                    """
                    SELECT source.reflection_id
                    FROM companion_journal_reflection_source AS source
                    JOIN companion_reflection AS reflection
                      ON reflection.reflection_id = source.reflection_id
                    WHERE source.journal_id = ?
                    ORDER BY reflection.created_at, reflection.rowid
                    """,
                    (journal_id,),
                )
            )
        return event_ids, reflection_ids

    def save_journals(
        self,
        *,
        actor_id: str,
        principal_id: str,
        day: str,
        drafts: Sequence[JournalDraft],
        prompt_version: str,
        replace_stale: bool = False,
    ) -> JournalSaveResult:
        """Insert (or rewrite stale) journals and their sources in one transaction.

        Every cited event/reflection must belong to the same ``(actor_id,
        principal_id)`` and fall inside ``day``; a violation rejects the whole
        batch before anything is written. An existing non-stale row is left
        untouched, which makes a repeated same-day run idempotent — callers must
        check ``created`` before claiming a write.
        """
        if not drafts:
            raise ValueError("at least one journal draft is required")
        _require_non_empty("day", day)
        _require_non_empty("prompt_version", prompt_version)
        day_bounds_ms(day)  # fail fast on a malformed day
        visibilities = [draft.visibility for draft in drafts]
        if len(set(visibilities)) != len(visibilities):
            raise ValueError(f"duplicate journal visibility in one batch: {visibilities}")
        for draft in drafts:
            if draft.visibility not in JOURNAL_VISIBILITIES:
                raise ValueError(f"unknown journal visibility {draft.visibility!r}")
            _require_non_empty("body", draft.body)
            if not draft.source_event_ids and not draft.source_reflection_ids:
                raise ValueError(f"journal {draft.visibility!r} must cite at least one source")

        with self._transaction() as conn:
            _assert_journal_sources_in_day(
                conn,
                actor_id=actor_id,
                principal_id=principal_id,
                day=day,
                event_ids={e for draft in drafts for e in draft.source_event_ids},
                reflection_ids={r for draft in drafts for r in draft.source_reflection_ids},
            )
            saved: list[str] = []
            created: list[bool] = []
            for draft in drafts:
                existing = conn.execute(
                    """
                    SELECT journal_id, status FROM companion_journal
                    WHERE actor_id = ? AND principal_id = ? AND day = ? AND visibility = ?
                    """,
                    (actor_id, principal_id, day, draft.visibility),
                ).fetchone()
                if existing is None:
                    journal_id = f"jrn_{uuid.uuid4().hex}"
                    conn.execute(
                        """
                        INSERT INTO companion_journal (
                            journal_id, actor_id, principal_id, day,
                            visibility, body, prompt_version, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'final')
                        """,
                        (journal_id, actor_id, principal_id, day, draft.visibility, draft.body, prompt_version),
                    )
                    _insert_journal_sources(conn, journal_id, draft)
                    created.append(True)
                elif replace_stale and existing["status"] == "stale":
                    journal_id = str(existing["journal_id"])
                    conn.execute(
                        f"""
                        UPDATE companion_journal
                        SET body = ?, prompt_version = ?, status = 'final',
                            updated_at = {_TIMESTAMP_DEFAULT}
                        WHERE journal_id = ?
                        """,
                        (draft.body, prompt_version, journal_id),
                    )
                    conn.execute(
                        "DELETE FROM companion_journal_event_source WHERE journal_id = ?",
                        (journal_id,),
                    )
                    conn.execute(
                        "DELETE FROM companion_journal_reflection_source WHERE journal_id = ?",
                        (journal_id,),
                    )
                    _insert_journal_sources(conn, journal_id, draft)
                    created.append(True)
                else:
                    journal_id = str(existing["journal_id"])
                    created.append(False)
                saved.append(journal_id)
        return JournalSaveResult(ids=tuple(saved), created=tuple(created))

    def mark_journal_stale(
        self, actor_id: str, principal_id: str, day: str, visibility: str
    ) -> bool:
        """Flag one journal as stale so the next run may rewrite it; False if it did not exist."""
        if visibility not in JOURNAL_VISIBILITIES:
            raise ValueError(f"unknown journal visibility {visibility!r}")
        day_bounds_ms(day)  # fail fast on a malformed day
        with self._transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE companion_journal
                SET status = 'stale', updated_at = {_TIMESTAMP_DEFAULT}
                WHERE actor_id = ? AND principal_id = ? AND day = ?
                  AND visibility = ? AND status <> 'stale'
                """,
                (actor_id, principal_id, day, visibility),
            )
        return cur.rowcount > 0

    def record_journal_correction(
        self,
        *,
        journal_id: str,
        note: str,
        source_event_ids: Sequence[str] = (),
    ) -> str:
        """Record an owner correction against one journal and mark that journal stale.

        ``source_event_ids`` must be a subset of the events the journal itself
        cites, so a correction can never be re-attributed to evidence the journal
        never used. The correction, its sources and the stale flag land in one
        transaction: a rejected correction changes nothing.
        """
        _require_non_empty("journal_id", journal_id)
        _require_non_empty("note", note)
        cited = tuple(dict.fromkeys(source_event_ids))
        correction_id = f"corr_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT journal_id FROM companion_journal WHERE journal_id = ?",
                (journal_id,),
            ).fetchone()
            if row is None:
                raise CompanionStoreError(f"journal {journal_id!r} does not exist")
            if cited:
                placeholders = ",".join("?" for _ in cited)
                known = {
                    source["event_id"]
                    for source in conn.execute(
                        f"""
                        SELECT event_id FROM companion_journal_event_source
                        WHERE journal_id = ? AND event_id IN ({placeholders})
                        """,
                        (journal_id, *cited),
                    )
                }
                unknown = [event_id for event_id in cited if event_id not in known]
                if unknown:
                    raise SourceOwnershipError(
                        f"correction cites events the journal never used: {unknown}"
                    )
            conn.execute(
                """
                INSERT INTO companion_journal_correction (correction_id, journal_id, note)
                VALUES (?, ?, ?)
                """,
                (correction_id, journal_id, note),
            )
            for event_id in cited:
                conn.execute(
                    """
                    INSERT INTO companion_journal_correction_source (correction_id, event_id)
                    VALUES (?, ?)
                    """,
                    (correction_id, event_id),
                )
            conn.execute(
                f"""
                UPDATE companion_journal
                SET status = 'stale', updated_at = {_TIMESTAMP_DEFAULT}
                WHERE journal_id = ?
                """,
                (journal_id,),
            )
        return correction_id

    def journal_corrections(self, journal_id: str) -> list[JournalCorrection]:
        """Corrections recorded against one journal, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_journal_correction
                WHERE journal_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (journal_id,),
            ).fetchall()
            corrections = []
            for row in rows:
                sources = tuple(
                    source["event_id"]
                    for source in conn.execute(
                        """
                        SELECT source.event_id
                        FROM companion_journal_correction_source AS source
                        JOIN companion_event AS event ON event.event_id = source.event_id
                        WHERE source.correction_id = ?
                        ORDER BY event.seq
                        """,
                        (row["correction_id"],),
                    )
                )
                corrections.append(
                    JournalCorrection(
                        correction_id=row["correction_id"],
                        journal_id=row["journal_id"],
                        note=row["note"],
                        source_event_ids=sources,
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
        return corrections


    def append_reflection(
        self,
        *,
        actor_id: str,
        principal_id: str,
        statement: str,
        confidence: float,
        source_event_ids: Sequence[str],
        prompt_version: str,
    ) -> str:
        """Insert one reflection + its sources without moving the V1 reflection cursor.

        Used by the journal projector: the journal is not part of the reflection
        batch, so it must not advance ``last_reflected_seq``.
        """
        draft = ReflectionDraft(
            statement=statement,
            confidence=float(confidence),
            source_event_ids=tuple(dict.fromkeys(source_event_ids)),
        )
        reflection_id = f"refl_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            _assert_sources_owned(conn, actor_id, principal_id, [draft])
            conn.execute(
                """
                INSERT INTO companion_reflection (
                    reflection_id, actor_id, principal_id, statement, confidence,
                    prompt_version, projector_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reflection_id,
                    actor_id,
                    principal_id,
                    draft.statement,
                    draft.confidence,
                    prompt_version,
                    prompt_version,
                ),
            )
            for event_id in draft.source_event_ids:
                conn.execute(
                    "INSERT INTO companion_reflection_source (reflection_id, event_id) VALUES (?, ?)",
                    (reflection_id, event_id),
                )
        return reflection_id

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

    def begin_reflection_batch(
        self, actor_id: str, principal_id: str, batch_end_seq: int
    ) -> None:
        """Pin the oldest unreflected batch so a failed run retries exactly this range."""
        with self._transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO companion_cursor (
                    actor_id, principal_id, retry_batch_end_seq, failure_count, last_error
                ) VALUES (?, ?, ?, 0, NULL)
                ON CONFLICT(actor_id, principal_id) DO UPDATE SET
                    retry_batch_end_seq = excluded.retry_batch_end_seq,
                    failure_count = 0,
                    last_error = NULL,
                    updated_at = {_TIMESTAMP_DEFAULT}
                """,
                (actor_id, principal_id, int(batch_end_seq)),
            )

    def record_reflection_failure(
        self,
        *,
        actor_id: str,
        principal_id: str,
        batch_end_seq: int,
        failure_count: int,
        last_error: str,
        advance: bool,
    ) -> None:
        """Persist a failed reflection run.

        ``advance=False`` keeps the pinned batch for a same-batch retry;
        ``advance=True`` skips it (``last_reflected_seq`` moves past the batch).
        """
        with self._transaction() as conn:
            if advance:
                conn.execute(
                    f"""
                    INSERT INTO companion_cursor (
                        actor_id, principal_id, last_reflected_seq,
                        retry_batch_end_seq, failure_count, last_error
                    ) VALUES (?, ?, ?, NULL, 0, ?)
                    ON CONFLICT(actor_id, principal_id) DO UPDATE SET
                        last_reflected_seq = excluded.last_reflected_seq,
                        retry_batch_end_seq = NULL,
                        failure_count = 0,
                        last_error = excluded.last_error,
                        updated_at = {_TIMESTAMP_DEFAULT}
                    """,
                    (actor_id, principal_id, int(batch_end_seq), last_error),
                )
            else:
                conn.execute(
                    f"""
                    INSERT INTO companion_cursor (
                        actor_id, principal_id, retry_batch_end_seq, failure_count, last_error
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(actor_id, principal_id) DO UPDATE SET
                        retry_batch_end_seq = excluded.retry_batch_end_seq,
                        failure_count = excluded.failure_count,
                        last_error = excluded.last_error,
                        updated_at = {_TIMESTAMP_DEFAULT}
                    """,
                    (actor_id, principal_id, int(batch_end_seq), int(failure_count), last_error),
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
                        statement, confidence, prompt_version, projector_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reflection_id,
                        actor_id,
                        principal_id,
                        draft.statement,
                        draft.confidence,
                        prompt_version,
                        prompt_version,
                    ),
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


    # ---------------------------------------------------- reflection lifecycle

    def set_reflection_status(
        self,
        reflection_id: str,
        *,
        status: str,
        reason: str | None = None,
        supersedes_id: str | None = None,
        refutes_id: str | None = None,
    ) -> None:
        """Move one reflection through its lifecycle (never silently deleted)."""
        if status not in REFLECTION_STATUSES:
            raise ValueError(f"unknown reflection status: {status!r}")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT reflection_id FROM companion_reflection WHERE reflection_id = ?",
                (reflection_id,),
            ).fetchone()
            if row is None:
                raise CompanionStoreError(f"unknown reflection: {reflection_id!r}")
            conn.execute(
                f"""
                UPDATE companion_reflection
                SET status = ?, supersedes_id = COALESCE(?, supersedes_id),
                    refutes_id = COALESCE(?, refutes_id),
                    updated_at = {_TIMESTAMP_DEFAULT}
                WHERE reflection_id = ?
                """,
                (status, supersedes_id, refutes_id, reflection_id),
            )
        logger.info(
            "companion reflection status changed",
            reflection_id=reflection_id,
            status=status,
            reason=reason,
        )

    def reflection_sources(self, reflection_id: str) -> tuple[str, ...]:
        """Event ids backing one reflection (the audit trail for corrections)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source.event_id
                FROM companion_reflection_source AS source
                JOIN companion_event AS event ON event.event_id = source.event_id
                WHERE source.reflection_id = ?
                ORDER BY event.seq ASC
                """,
                (reflection_id,),
            ).fetchall()
        return tuple(row["event_id"] for row in rows)

    def reflections_for_events(self, event_ids: Sequence[str]) -> list[ReflectionRecord]:
        """Reflections that cite any of the given events (correction propagation)."""
        ids = [event_id for event_id in dict.fromkeys(event_ids) if event_id]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT reflection.*
                FROM companion_reflection AS reflection
                JOIN companion_reflection_source AS source
                  ON source.reflection_id = reflection.reflection_id
                WHERE source.event_id IN ({placeholders})
                ORDER BY reflection.created_at DESC, reflection.rowid DESC
                """,
                tuple(ids),
            ).fetchall()
        return [_row_to_reflection(row) for row in rows]

    # ------------------------------------------------------- projection state

    def projection_state(self, projection: str) -> dict[str, object] | None:
        """Recorded projector version + cursor for one derived projection."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM companion_projection_state WHERE projection = ?",
                (projection,),
            ).fetchone()
        if row is None:
            return None
        return {
            "projection": row["projection"],
            "projector_version": row["projector_version"],
            "cursor_seq": int(row["cursor_seq"]),
            "updated_at": row["updated_at"],
        }

    def record_projection(
        self, projection: str, *, projector_version: str, cursor_seq: int = 0
    ) -> None:
        """Stamp which projector version produced one derived table (rebuild auditing)."""
        _require_non_empty("projection", projection)
        _require_non_empty("projector_version", projector_version)
        with self._transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO companion_projection_state (
                    projection, projector_version, cursor_seq
                ) VALUES (?, ?, ?)
                ON CONFLICT(projection) DO UPDATE SET
                    projector_version = excluded.projector_version,
                    cursor_seq = excluded.cursor_seq,
                    updated_at = {_TIMESTAMP_DEFAULT}
                """,
                (projection, projector_version, int(cursor_seq)),
            )

    # ---------------------------------------------------------------- relationship

    def relationship(self, actor_id: str, principal_id: str) -> Relationship | None:
        """Current relationship row for one actor, or ``None`` before the first projection."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM companion_relationship WHERE actor_id = ? AND principal_id = ?",
                (actor_id, principal_id),
            ).fetchone()
        return _row_to_relationship(row) if row is not None else None

    def events_for_actor(
        self, actor_id: str, principal_id: str, *, limit: int = 500
    ) -> list[CompanionEvent]:
        """All events of one actor, oldest first (the projection's input)."""
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_event
                WHERE actor_id = ? AND principal_id = ?
                ORDER BY seq ASC LIMIT ?
                """,
                (actor_id, principal_id, int(limit)),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def save_relationship(
        self,
        *,
        actor_id: str,
        principal_id: str,
        stage: str,
        status: str,
        meaningful_interactions: int,
        active_days: int,
        positive_feedback: int,
        user_boundaries: Sequence[dict[str, object]],
        actor_boundaries: Sequence[str],
        last_negative_event_id: str | None = None,
        last_repair_event_id: str | None = None,
        last_interaction_at_ms: int | None = None,
    ) -> Relationship:
        """Insert or replace the derived relationship row (one writer: the projector)."""
        _require_non_empty("actor_id", actor_id)
        _require_non_empty("principal_id", principal_id)
        if stage not in RELATIONSHIP_STAGES:
            raise ValueError(f"unknown relationship stage: {stage!r}")
        if status not in RELATIONSHIP_STATUSES:
            raise ValueError(f"unknown relationship status: {status!r}")
        payload_user = json.dumps(list(user_boundaries), ensure_ascii=False)
        payload_actor = json.dumps(list(actor_boundaries), ensure_ascii=False)
        with self._transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO companion_relationship (
                    actor_id, principal_id, stage, status, meaningful_interactions,
                    active_days, positive_feedback, user_boundaries_json,
                    actor_boundaries_json, last_negative_event_id,
                    last_repair_event_id, last_interaction_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_id, principal_id) DO UPDATE SET
                    stage = excluded.stage,
                    status = excluded.status,
                    meaningful_interactions = excluded.meaningful_interactions,
                    active_days = excluded.active_days,
                    positive_feedback = excluded.positive_feedback,
                    user_boundaries_json = excluded.user_boundaries_json,
                    actor_boundaries_json = excluded.actor_boundaries_json,
                    last_negative_event_id = excluded.last_negative_event_id,
                    last_repair_event_id = excluded.last_repair_event_id,
                    last_interaction_at_ms = excluded.last_interaction_at_ms,
                    updated_at = {_TIMESTAMP_DEFAULT}
                """,
                (
                    actor_id,
                    principal_id,
                    stage,
                    status,
                    int(meaningful_interactions),
                    int(active_days),
                    int(positive_feedback),
                    payload_user,
                    payload_actor,
                    last_negative_event_id,
                    last_repair_event_id,
                    last_interaction_at_ms,
                ),
            )
            row = conn.execute(
                "SELECT * FROM companion_relationship WHERE actor_id = ? AND principal_id = ?",
                (actor_id, principal_id),
            ).fetchone()
        assert row is not None  # pragma: no cover - the upsert above always leaves a row
        return _row_to_relationship(row)

    def open_threads(
        self, actor_id: str, principal_id: str, *, status: str | None = "open"
    ) -> list[OpenThread]:
        """Threads of one actor; ``status=None`` returns every state, oldest first."""
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT * FROM companion_open_thread
                    WHERE actor_id = ? AND principal_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (actor_id, principal_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM companion_open_thread
                    WHERE actor_id = ? AND principal_id = ? AND status = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (actor_id, principal_id, status),
                ).fetchall()
        return [_row_to_open_thread(row) for row in rows]

    def add_open_thread(
        self, *, actor_id: str, principal_id: str, topic: str, source_event_id: str
    ) -> str:
        """Record one open thread; a repeated same-topic open thread returns the existing id.

        The thread's ``source_event_id`` must be a real event of the same actor (the FK to
        ``companion_event`` plus an ownership check), and a relationship row must exist
        first because ``companion_open_thread`` references it.
        """
        _require_non_empty("topic", topic)
        _require_non_empty("source_event_id", source_event_id)
        with self._transaction() as conn:
            owned = conn.execute(
                """
                SELECT 1 FROM companion_event
                WHERE event_id = ? AND actor_id = ? AND principal_id = ?
                """,
                (source_event_id, actor_id, principal_id),
            ).fetchone()
            if owned is None:
                raise SourceOwnershipError(
                    f"open thread source {source_event_id!r} is not an event of "
                    f"({actor_id!r}, {principal_id!r})"
                )
            conn.execute(
                """
                INSERT INTO companion_relationship (actor_id, principal_id, stage)
                VALUES (?, ?, 'new')
                ON CONFLICT(actor_id, principal_id) DO NOTHING
                """,
                (actor_id, principal_id),
            )
            conn.execute(
                """
                INSERT INTO companion_open_thread (
                    open_thread_id, actor_id, principal_id, topic, source_event_id, status
                ) VALUES (?, ?, ?, ?, ?, 'open')
                ON CONFLICT DO NOTHING
                """,
                (f"thr_{uuid.uuid4().hex}", actor_id, principal_id, topic, source_event_id),
            )
            row = conn.execute(
                """
                SELECT open_thread_id FROM companion_open_thread
                WHERE actor_id = ? AND principal_id = ? AND topic = ? AND status = 'open'
                """,
                (actor_id, principal_id, topic),
            ).fetchone()
        if row is None:  # pragma: no cover - the insert above always leaves a row
            raise CompanionStoreError(f"open thread {topic!r} vanished after insert")
        return str(row["open_thread_id"])

    def set_open_thread_status(self, open_thread_id: str, *, status: str) -> None:
        """Move one thread to ``resolved`` or ``cancelled`` (never back to ``open``)."""
        if status not in {"resolved", "cancelled"}:
            raise ValueError(f"threads can only be resolved or cancelled, got {status!r}")
        with self._transaction() as conn:
            conn.execute(
                f"""
                UPDATE companion_open_thread
                SET status = ?, updated_at = {_TIMESTAMP_DEFAULT}
                WHERE open_thread_id = ?
                """,
                (status, open_thread_id),
            )


    # --------------------------------------------------------------------- mood

    def mood(self, actor_id: str, principal_id: str) -> Mood | None:
        """The stored mood row, expired or not; callers decide whether it is still live."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM companion_mood WHERE actor_id = ? AND principal_id = ?",
                (actor_id, principal_id),
            ).fetchone()
        return _row_to_mood(row) if row is not None else None

    def save_mood(
        self,
        *,
        actor_id: str,
        principal_id: str,
        label: str,
        valence: float,
        arousal: float,
        source_event_id: str,
        expires_at_ms: int,
    ) -> Mood:
        """Insert or replace one actor's mood (the projector is the only writer)."""
        _require_non_empty("actor_id", actor_id)
        _require_non_empty("principal_id", principal_id)
        _require_non_empty("source_event_id", source_event_id)
        if label not in MOOD_LABELS:
            raise ValueError(f"unknown mood label: {label!r}")
        with self._transaction() as conn:
            owned = conn.execute(
                """
                SELECT 1 FROM companion_event
                WHERE event_id = ? AND actor_id = ? AND principal_id = ?
                """,
                (source_event_id, actor_id, principal_id),
            ).fetchone()
            if owned is None:
                raise SourceOwnershipError(
                    f"mood source {source_event_id!r} is not an event of "
                    f"({actor_id!r}, {principal_id!r})"
                )
            conn.execute(
                f"""
                INSERT INTO companion_mood (
                    actor_id, principal_id, label, valence, arousal,
                    source_event_id, expires_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_id, principal_id) DO UPDATE SET
                    label = excluded.label,
                    valence = excluded.valence,
                    arousal = excluded.arousal,
                    source_event_id = excluded.source_event_id,
                    expires_at_ms = excluded.expires_at_ms,
                    updated_at = {_TIMESTAMP_DEFAULT}
                """,
                (
                    actor_id,
                    principal_id,
                    label,
                    float(valence),
                    float(arousal),
                    source_event_id,
                    int(expires_at_ms),
                ),
            )
            row = conn.execute(
                "SELECT * FROM companion_mood WHERE actor_id = ? AND principal_id = ?",
                (actor_id, principal_id),
            ).fetchone()
        assert row is not None  # pragma: no cover - the upsert above always leaves a row
        return _row_to_mood(row)

    def clear_mood(self, actor_id: str, principal_id: str) -> None:
        """Drop the row entirely (used when a mood expires and nothing replaces it)."""
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM companion_mood WHERE actor_id = ? AND principal_id = ?",
                (actor_id, principal_id),
            )

    # ---------------------------------------------------------------- commitment

    def commitments(
        self,
        actor_id: str,
        principal_id: str,
        *,
        status: str | None = None,
        open_only: bool = False,
    ) -> list[Commitment]:
        """Commitments of one actor, oldest first; filter by status or "still open"."""
        clauses = ["actor_id = ?", "principal_id = ?"]
        params: list[object] = [actor_id, principal_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        elif open_only:
            clauses.append(
                "status IN (%s)" % ",".join("?" for _ in COMMITMENT_OPEN_STATUSES)
            )
            params.extend(sorted(COMMITMENT_OPEN_STATUSES))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM companion_commitment WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at ASC, rowid ASC",
                tuple(params),
            ).fetchall()
        return [_row_to_commitment(row) for row in rows]

    def add_commitment(
        self,
        *,
        actor_id: str,
        principal_id: str,
        source_event_id: str,
        quote: str,
        action: str,
        due_at_ms: int | None,
        success_criteria: str,
        status: str,
    ) -> tuple[Commitment, bool]:
        """Insert one commitment; the (source, quote) pair is the deterministic key.

        Returns ``(row, created)``. A repeated extraction of the same send therefore
        cannot duplicate the promise — it returns the row that is already there.
        """
        for field, value in (
            ("actor_id", actor_id),
            ("principal_id", principal_id),
            ("source_event_id", source_event_id),
            ("quote", quote),
            ("action", action),
            ("success_criteria", success_criteria),
        ):
            _require_non_empty(field, value)
        if status not in COMMITMENT_STATUSES:
            raise ValueError(f"unknown commitment status: {status!r}")
        with self._transaction() as conn:
            owned = conn.execute(
                """
                SELECT 1 FROM companion_event
                WHERE event_id = ? AND actor_id = ? AND principal_id = ?
                """,
                (source_event_id, actor_id, principal_id),
            ).fetchone()
            if owned is None:
                raise SourceOwnershipError(
                    f"commitment source {source_event_id!r} is not an event of "
                    f"({actor_id!r}, {principal_id!r})"
                )
            cur = conn.execute(
                """
                INSERT INTO companion_commitment (
                    commitment_id, actor_id, principal_id, source_event_id,
                    quote, action, due_at_ms, success_criteria, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    f"cmt_{uuid.uuid4().hex}",
                    actor_id,
                    principal_id,
                    source_event_id,
                    quote,
                    action,
                    None if due_at_ms is None else int(due_at_ms),
                    success_criteria,
                    status,
                ),
            )
            created = cur.rowcount == 1
            row = conn.execute(
                """
                SELECT * FROM companion_commitment
                WHERE actor_id = ? AND principal_id = ?
                  AND source_event_id = ? AND quote = ?
                """,
                (actor_id, principal_id, source_event_id, quote),
            ).fetchone()
        if row is None:  # pragma: no cover - the insert above always leaves a row
            raise CompanionStoreError("commitment vanished after insert")
        return _row_to_commitment(row), created

    def set_commitment_status(
        self,
        commitment_id: str,
        *,
        status: str,
        result_event_id: str | None = None,
    ) -> Commitment:
        """Move one commitment forward; terminal states are final."""
        if status not in COMMITMENT_STATUSES:
            raise ValueError(f"unknown commitment status: {status!r}")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM companion_commitment WHERE commitment_id = ?",
                (commitment_id,),
            ).fetchone()
            if row is None:
                raise CompanionStoreError(f"unknown commitment: {commitment_id!r}")
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                raise CompanionStoreError(
                    f"commitment {commitment_id!r} is already {row['status']!r}"
                )
            conn.execute(
                f"""
                UPDATE companion_commitment
                SET status = ?, result_event_id = COALESCE(?, result_event_id),
                    updated_at = {_TIMESTAMP_DEFAULT}
                WHERE commitment_id = ?
                """,
                (status, result_event_id, commitment_id),
            )
            updated = conn.execute(
                "SELECT * FROM companion_commitment WHERE commitment_id = ?",
                (commitment_id,),
            ).fetchone()
        assert updated is not None  # pragma: no cover
        return _row_to_commitment(updated)

    def commitments_for_sweep(self, *, now_ms: int) -> list[Commitment]:
        """Open commitments whose due time already passed (sweeper input).

        The grace window is applied by the sweeper, not here: an overdue promise stays
        visible as ``overdue`` for a while before it is declared failed.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_commitment
                WHERE status IN ('active', 'overdue')
                  AND due_at_ms IS NOT NULL
                  AND due_at_ms <= ?
                ORDER BY due_at_ms ASC
                """,
                (int(now_ms),),
            ).fetchall()
        return [_row_to_commitment(row) for row in rows]

    # --------------------------------------------------------------------- goal

    def goals(
        self, actor_id: str, principal_id: str, *, status: str | None = None
    ) -> list[Goal]:
        """Goals of one actor, oldest first; ``status=None`` returns every state."""
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT * FROM companion_goal
                    WHERE actor_id = ? AND principal_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (actor_id, principal_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM companion_goal
                    WHERE actor_id = ? AND principal_id = ? AND status = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (actor_id, principal_id, status),
                ).fetchall()
        return [_row_to_goal(row) for row in rows]

    def get_goal(self, goal_id: str) -> Goal | None:
        """Load one goal by id (``None`` when it does not exist)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM companion_goal WHERE goal_id = ?", (goal_id,)
            ).fetchone()
        return _row_to_goal(row) if row is not None else None

    def active_goal(self, actor_id: str, principal_id: str) -> Goal | None:
        """The single ``in_progress`` goal (guaranteed by the partial unique index)."""
        goals = self.goals(actor_id, principal_id, status="in_progress")
        return goals[0] if goals else None

    def add_goal(
        self,
        *,
        actor_id: str,
        principal_id: str,
        text: str,
        allowed_tools: Sequence[str],
        deadline_ms: int,
        status: str = "proposed",
        commitment_id: str | None = None,
    ) -> Goal:
        """Insert one goal; a second ``in_progress`` goal is rejected by the index."""
        _require_non_empty("actor_id", actor_id)
        _require_non_empty("principal_id", principal_id)
        _require_non_empty("text", text)
        if status not in GOAL_STATUSES:
            raise ValueError(f"unknown goal status: {status!r}")
        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO companion_goal (
                        goal_id, actor_id, principal_id, commitment_id, text,
                        status, allowed_tools_json, deadline_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"goal_{uuid.uuid4().hex}",
                        actor_id,
                        principal_id,
                        commitment_id,
                        text,
                        status,
                        json.dumps(list(allowed_tools), ensure_ascii=False),
                        int(deadline_ms),
                    ),
                )
            except sqlite3.IntegrityError as exc:  # ux_one_active_goal
                raise CompanionStoreError(
                    "another goal is already in progress for this actor"
                ) from exc
            row = conn.execute(
                """
                SELECT * FROM companion_goal
                WHERE actor_id = ? AND principal_id = ?
                ORDER BY rowid DESC LIMIT 1
                """,
                (actor_id, principal_id),
            ).fetchone()
        assert row is not None  # pragma: no cover
        return _row_to_goal(row)

    def set_goal_status(
        self,
        goal_id: str,
        *,
        status: str,
        result_event_id: str | None = None,
    ) -> Goal:
        """Move one goal forward; terminal states are final."""
        if status not in GOAL_STATUSES:
            raise ValueError(f"unknown goal status: {status!r}")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM companion_goal WHERE goal_id = ?", (goal_id,)
            ).fetchone()
            if row is None:
                raise CompanionStoreError(f"unknown goal: {goal_id!r}")
            if row["status"] in GOAL_TERMINAL_STATUSES:
                raise CompanionStoreError(
                    f"goal {goal_id!r} is already {row['status']!r}"
                )
            try:
                conn.execute(
                    f"""
                    UPDATE companion_goal
                    SET status = ?, result_event_id = COALESCE(?, result_event_id),
                        updated_at = {_TIMESTAMP_DEFAULT}
                    WHERE goal_id = ?
                    """,
                    (status, result_event_id, goal_id),
                )
            except sqlite3.IntegrityError as exc:  # second in_progress
                raise CompanionStoreError(
                    "another goal is already in progress for this actor"
                ) from exc
            updated = conn.execute(
                "SELECT * FROM companion_goal WHERE goal_id = ?", (goal_id,)
            ).fetchone()
        assert updated is not None  # pragma: no cover
        return _row_to_goal(updated)

    # ----------------------------------------------------------------- activity

    def activities(
        self, actor_id: str, principal_id: str, *, status: str | None = None
    ) -> list[Activity]:
        """Activities of one actor, oldest first; ``status=None`` returns every state."""
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT * FROM companion_activity
                    WHERE actor_id = ? AND principal_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (actor_id, principal_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM companion_activity
                    WHERE actor_id = ? AND principal_id = ? AND status = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (actor_id, principal_id, status),
                ).fetchall()
        return [_row_to_activity(row) for row in rows]

    def get_activity(self, activity_id: str) -> Activity | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM companion_activity WHERE activity_id = ?", (activity_id,)
            ).fetchone()
        return _row_to_activity(row) if row is not None else None

    def running_activity(self, actor_id: str, principal_id: str) -> Activity | None:
        """The single active/paused activity (partial unique index guarantees one)."""
        for status in ("active", "paused"):
            rows = self.activities(actor_id, principal_id, status=status)
            if rows:
                return rows[0]
        return None

    def add_activity(
        self,
        *,
        actor_id: str,
        principal_id: str,
        kind: str,
        budget: dict[str, object],
        source_goal_id: str | None = None,
        started_at_ms: int | None = None,
    ) -> Activity:
        """Start one activity; a second running activity is refused by the index."""
        _require_non_empty("actor_id", actor_id)
        _require_non_empty("principal_id", principal_id)
        _require_non_empty("kind", kind)
        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO companion_activity (
                        activity_id, actor_id, principal_id, kind, status,
                        started_at_ms, source_goal_id, budget_json, spent_json
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, '{{}}')
                    """.replace("'{{}}'", "'{}'"),
                    (
                        f"act_{uuid.uuid4().hex}",
                        actor_id,
                        principal_id,
                        kind,
                        started_at_ms,
                        source_goal_id,
                        json.dumps(budget, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CompanionStoreError(
                    "another activity is already running for this actor"
                ) from exc
            row = conn.execute(
                """
                SELECT * FROM companion_activity
                WHERE actor_id = ? AND principal_id = ?
                ORDER BY rowid DESC LIMIT 1
                """,
                (actor_id, principal_id),
            ).fetchone()
        assert row is not None  # pragma: no cover
        return _row_to_activity(row)

    def set_activity_status(
        self,
        activity_id: str,
        *,
        status: str,
        reason: str | None = None,
        resume_after_ms: int | None = None,
        last_result_event_id: str | None = None,
    ) -> Activity:
        """Move one activity forward; terminal states are final."""
        if status not in ACTIVITY_STATUSES:
            raise ValueError(f"unknown activity status: {status!r}")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM companion_activity WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            if row is None:
                raise CompanionStoreError(f"unknown activity: {activity_id!r}")
            if row["status"] in ACTIVITY_TERMINAL_STATUSES:
                raise CompanionStoreError(
                    f"activity {activity_id!r} is already {row['status']!r}"
                )
            try:
                conn.execute(
                    f"""
                    UPDATE companion_activity
                    SET status = ?, reason = COALESCE(?, reason),
                        resume_after_ms = COALESCE(?, resume_after_ms),
                        last_result_event_id = COALESCE(?, last_result_event_id),
                        updated_at = {_TIMESTAMP_DEFAULT}
                    WHERE activity_id = ?
                    """,
                    (status, reason, resume_after_ms, last_result_event_id, activity_id),
                )
            except sqlite3.IntegrityError as exc:
                raise CompanionStoreError(
                    "another activity is already running for this actor"
                ) from exc
            updated = conn.execute(
                "SELECT * FROM companion_activity WHERE activity_id = ?", (activity_id,)
            ).fetchone()
        assert updated is not None  # pragma: no cover
        return _row_to_activity(updated)

    def update_activity_spend(
        self, activity_id: str, spent: dict[str, object]
    ) -> Activity:
        """Persist the running spend counters (called after every bounded step)."""
        with self._transaction() as conn:
            conn.execute(
                f"""
                UPDATE companion_activity
                SET spent_json = ?, updated_at = {_TIMESTAMP_DEFAULT}
                WHERE activity_id = ?
                """,
                (json.dumps(spent, ensure_ascii=False), activity_id),
            )
            row = conn.execute(
                "SELECT * FROM companion_activity WHERE activity_id = ?", (activity_id,)
            ).fetchone()
        if row is None:
            raise CompanionStoreError(f"unknown activity: {activity_id!r}")
        return _row_to_activity(row)

    # ------------------------------------------------------------- preferences

    def preferences(
        self, actor_id: str, principal_id: str, *, now_ms: int | None = None
    ) -> list[SelfPreference]:
        """The character's own topic taste, newest first; expired rows are excluded."""
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM companion_self_preference
                WHERE actor_id = ? AND principal_id = ?
                  AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                ORDER BY updated_at DESC, rowid DESC
                """,
                (actor_id, principal_id, now),
            ).fetchall()
        return [_row_to_preference(row) for row in rows]

    def save_preference(
        self,
        *,
        actor_id: str,
        principal_id: str,
        topic: str,
        weight: float,
        novelty: float,
        fatigue: float,
        source_event_ids: Sequence[str],
        expires_at_ms: int | None = None,
    ) -> SelfPreference:
        """Upsert one topic preference (weight -1..1, novelty/fatigue 0..1)."""
        _require_non_empty("topic", topic)
        for name, value, low, high in (
            ("weight", weight, -1.0, 1.0),
            ("novelty", novelty, 0.0, 1.0),
            ("fatigue", fatigue, 0.0, 1.0),
        ):
            if not low <= float(value) <= high:
                raise ValueError(f"{name} out of range: {value}")
        with self._transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO companion_self_preference (
                    actor_id, principal_id, topic, weight, novelty, fatigue,
                    source_event_ids_json, expires_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_id, principal_id, topic) DO UPDATE SET
                    weight = excluded.weight,
                    novelty = excluded.novelty,
                    fatigue = excluded.fatigue,
                    source_event_ids_json = excluded.source_event_ids_json,
                    expires_at_ms = excluded.expires_at_ms,
                    updated_at = {_TIMESTAMP_DEFAULT}
                """,
                (
                    actor_id,
                    principal_id,
                    topic,
                    float(weight),
                    float(novelty),
                    float(fatigue),
                    json.dumps(list(source_event_ids), ensure_ascii=False),
                    expires_at_ms,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM companion_self_preference
                WHERE actor_id = ? AND principal_id = ? AND topic = ?
                """,
                (actor_id, principal_id, topic),
            ).fetchone()
        assert row is not None  # pragma: no cover
        return _row_to_preference(row)

    # ---------------------------------------------------------------- outreach

    def record_outreach(
        self, *, actor_id: str, principal_id: str, sent_event_id: str
    ) -> OutreachFeedback:
        """Create the unanswered outreach row for one confirmed send."""
        _require_non_empty("sent_event_id", sent_event_id)
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO companion_outreach_feedback (
                    outreach_id, actor_id, principal_id, sent_event_id, kind
                ) VALUES (?, ?, ?, ?, 'no_response')
                ON CONFLICT(actor_id, principal_id, sent_event_id) DO NOTHING
                """,
                (f"out_{uuid.uuid4().hex}", actor_id, principal_id, sent_event_id),
            )
            row = conn.execute(
                """
                SELECT * FROM companion_outreach_feedback
                WHERE actor_id = ? AND principal_id = ? AND sent_event_id = ?
                """,
                (actor_id, principal_id, sent_event_id),
            ).fetchone()
        assert row is not None  # pragma: no cover
        return _row_to_outreach(row)

    def outreach_rows(
        self, actor_id: str, principal_id: str, *, kind: str | None = None
    ) -> list[OutreachFeedback]:
        """Outreach feedback rows, oldest first, optionally filtered by kind."""
        with self._connect() as conn:
            if kind is None:
                rows = conn.execute(
                    """
                    SELECT * FROM companion_outreach_feedback
                    WHERE actor_id = ? AND principal_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (actor_id, principal_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM companion_outreach_feedback
                    WHERE actor_id = ? AND principal_id = ? AND kind = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (actor_id, principal_id, kind),
                ).fetchall()
        return [_row_to_outreach(row) for row in rows]

    def set_outreach_reply(
        self,
        outreach_id: str,
        *,
        kind: str,
        reply_event_id: str | None,
        response_delay_ms: int | None,
    ) -> OutreachFeedback:
        """Attach the owner's reply (or ``no_response``) to one outreach row."""
        if kind not in OUTREACH_KINDS:
            raise ValueError(f"unknown outreach kind: {kind!r}")
        with self._transaction() as conn:
            conn.execute(
                f"""
                UPDATE companion_outreach_feedback
                SET kind = ?, reply_event_id = ?, response_delay_ms = ?,
                    updated_at = {_TIMESTAMP_DEFAULT}
                WHERE outreach_id = ?
                """,
                (kind, reply_event_id, response_delay_ms, outreach_id),
            )
            row = conn.execute(
                "SELECT * FROM companion_outreach_feedback WHERE outreach_id = ?",
                (outreach_id,),
            ).fetchone()
        if row is None:
            raise CompanionStoreError(f"unknown outreach: {outreach_id!r}")
        return _row_to_outreach(row)

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
    keys = set(row.keys())

    def _opt(name: str) -> object | None:
        return row[name] if name in keys else None

    try:
        tags = tuple(json.loads(_opt("topic_tags_json") or "[]"))
    except (TypeError, ValueError):
        tags = ()
    return ReflectionRecord(
        reflection_id=row["reflection_id"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        statement=row["statement"],
        confidence=float(row["confidence"]),
        prompt_version=row["prompt_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=str(_opt("status") or "active"),
        topic_tags=tags,
        valid_from_ms=_maybe_int(_opt("valid_from_ms")),
        valid_to_ms=_maybe_int(_opt("valid_to_ms")),
        expires_at_ms=_maybe_int(_opt("expires_at_ms")),
        supersedes_id=_opt("supersedes_id"),
        refutes_id=_opt("refutes_id"),
        projector_version=_opt("projector_version"),
    )


def _maybe_int(value: object | None) -> int | None:
    return None if value is None else int(value)


def _assert_journal_sources_in_day(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    principal_id: str,
    day: str,
    event_ids: set[str],
    reflection_ids: set[str],
) -> None:
    """Reject any cited event/reflection that is missing, foreign, or outside ``day``."""
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        rows = conn.execute(
            f"""
            SELECT event_id, actor_id, principal_id, occurred_at_ms
            FROM companion_event WHERE event_id IN ({placeholders})
            """,
            tuple(event_ids),
        ).fetchall()
        owned = {
            row["event_id"]
            for row in rows
            if row["actor_id"] == actor_id
            and row["principal_id"] == principal_id
            and day_key(int(row["occurred_at_ms"])) == day
        }
        missing = event_ids - owned
        if missing:
            raise SourceOwnershipError(
                f"journal event sources not owned by ({actor_id!r}, {principal_id!r})"
                f" on {day}: {sorted(missing)}"
            )
    if reflection_ids:
        placeholders = ",".join("?" for _ in reflection_ids)
        rows = conn.execute(
            f"""
            SELECT reflection_id, actor_id, principal_id, created_at
            FROM companion_reflection WHERE reflection_id IN ({placeholders})
            """,
            tuple(reflection_ids),
        ).fetchall()
        owned = {
            row["reflection_id"]
            for row in rows
            if row["actor_id"] == actor_id
            and row["principal_id"] == principal_id
            and str(row["created_at"])[:10] == day
        }
        missing = reflection_ids - owned
        if missing:
            raise SourceOwnershipError(
                f"journal reflection sources not owned by ({actor_id!r}, {principal_id!r})"
                f" on {day}: {sorted(missing)}"
            )


def _insert_journal_sources(conn: sqlite3.Connection, journal_id: str, draft: JournalDraft) -> None:
    for event_id in dict.fromkeys(draft.source_event_ids):
        conn.execute(
            "INSERT INTO companion_journal_event_source (journal_id, event_id) VALUES (?, ?)",
            (journal_id, event_id),
        )
    for reflection_id in dict.fromkeys(draft.source_reflection_ids):
        conn.execute(
            "INSERT INTO companion_journal_reflection_source (journal_id, reflection_id) VALUES (?, ?)",
            (journal_id, reflection_id),
        )


def _row_to_journal(row: sqlite3.Row) -> JournalRecord:
    return JournalRecord(
        journal_id=row["journal_id"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        day=row["day"],
        visibility=row["visibility"],
        body=row["body"],
        prompt_version=row["prompt_version"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

def _row_to_relationship(row: sqlite3.Row) -> Relationship:
    try:
        user_boundaries = tuple(json.loads(row["user_boundaries_json"] or "[]"))
    except ValueError:  # pragma: no cover - the column is CHECK(json_valid)
        user_boundaries = ()
    try:
        actor_boundaries = tuple(json.loads(row["actor_boundaries_json"] or "[]"))
    except ValueError:  # pragma: no cover
        actor_boundaries = ()
    return Relationship(
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        stage=row["stage"],
        status=row["status"],
        meaningful_interactions=int(row["meaningful_interactions"]),
        active_days=int(row["active_days"]),
        positive_feedback=int(row["positive_feedback"]),
        user_boundaries=user_boundaries,
        actor_boundaries=actor_boundaries,
        last_negative_event_id=row["last_negative_event_id"],
        last_repair_event_id=row["last_repair_event_id"],
        last_interaction_at_ms=row["last_interaction_at_ms"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_open_thread(row: sqlite3.Row) -> OpenThread:
    return OpenThread(
        open_thread_id=row["open_thread_id"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        topic=row["topic"],
        source_event_id=row["source_event_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_mood(row: sqlite3.Row) -> Mood:
    return Mood(
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        label=row["label"],
        valence=float(row["valence"]),
        arousal=float(row["arousal"]),
        source_event_id=row["source_event_id"],
        expires_at_ms=int(row["expires_at_ms"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_commitment(row: sqlite3.Row) -> Commitment:
    return Commitment(
        commitment_id=row["commitment_id"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        source_event_id=row["source_event_id"],
        quote=row["quote"],
        action=row["action"],
        due_at_ms=None if row["due_at_ms"] is None else int(row["due_at_ms"]),
        success_criteria=row["success_criteria"],
        status=row["status"],
        result_event_id=row["result_event_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_goal(row: sqlite3.Row) -> Goal:
    try:
        tools = tuple(json.loads(row["allowed_tools_json"] or "[]"))
    except ValueError:  # pragma: no cover - CHECK(json_valid)
        tools = ()
    return Goal(
        goal_id=row["goal_id"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        commitment_id=row["commitment_id"],
        text=row["text"],
        status=row["status"],
        allowed_tools=tools,
        deadline_ms=int(row["deadline_ms"]),
        result_event_id=row["result_event_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_activity(row: sqlite3.Row) -> Activity:
    try:
        budget = json.loads(row["budget_json"] or "{}")
    except ValueError:  # pragma: no cover
        budget = {}
    try:
        spent = json.loads(row["spent_json"] or "{}")
    except ValueError:  # pragma: no cover
        spent = {}
    return Activity(
        activity_id=row["activity_id"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        kind=row["kind"],
        status=row["status"],
        started_at_ms=None if row["started_at_ms"] is None else int(row["started_at_ms"]),
        resume_after_ms=(
            None if row["resume_after_ms"] is None else int(row["resume_after_ms"])
        ),
        source_goal_id=row["source_goal_id"],
        last_result_event_id=row["last_result_event_id"],
        budget=budget,
        spent=spent,
        reason=row["reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_preference(row: sqlite3.Row) -> SelfPreference:
    try:
        sources = tuple(json.loads(row["source_event_ids_json"] or "[]"))
    except ValueError:  # pragma: no cover
        sources = ()
    return SelfPreference(
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        topic=row["topic"],
        weight=float(row["weight"]),
        novelty=float(row["novelty"]),
        fatigue=float(row["fatigue"]),
        source_event_ids=sources,
        expires_at_ms=None if row["expires_at_ms"] is None else int(row["expires_at_ms"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_outreach(row: sqlite3.Row) -> OutreachFeedback:
    return OutreachFeedback(
        outreach_id=row["outreach_id"],
        actor_id=row["actor_id"],
        principal_id=row["principal_id"],
        sent_event_id=row["sent_event_id"],
        reply_event_id=row["reply_event_id"],
        kind=row["kind"],
        response_delay_ms=(
            None if row["response_delay_ms"] is None else int(row["response_delay_ms"])
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
