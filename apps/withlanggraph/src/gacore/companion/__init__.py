"""Companion self-memory: the character remembers what it really said and did.

V1 modules land per ``docs/companion-self-memory-v1-plan.md``; V2 additions follow
``docs/companion-self-memory-v2-plan.md``:

- ``config`` — master switch + the single owner openid (Step 2)
- ``identity`` — the shared per-user active-card repository and owner gate (Step 1/2)
- ``store`` — the SQLite fact source for events / reflections / journals / cursor (Step 3, V2-Step 1)
- ``summary`` — redacted, bounded event summaries (Step 4)
- ``reflection`` — evidence-bound 0-2 takeaways from a fixed batch (Step 7)
- ``coordinator`` — single-flight background reflection checks (Step 7)
- ``context`` — the bounded Self memory block injected into owner C2C prompts (Step 8)
- ``reply_batch`` — 1-3 semantic segments for owner C2C chat replies (Step 9)
- ``journal`` — the two daily journals projected from real events (V2-Step 1)
"""

from __future__ import annotations

from gacore.companion.config import CompanionConfig, load_companion_config
from gacore.companion.context import (
    MAX_SELF_MEMORY_CHARS,
    MAX_SELF_MEMORY_REFLECTIONS,
    SELF_MEMORY_BLOCK_VERSION,
    build_self_memory_block,
)
from gacore.companion.coordinator import (
    ReflectionCoordinator,
    schedule_reflection_check,
    shutdown_reflection_coordinator,
)
from gacore.companion.identity import (
    DEFAULT_ACTOR_ID,
    ActiveCardCorruptError,
    ActiveCardError,
    ActiveCardRepository,
    active_cards_path,
    actor_id_for,
    default_active_cards_path,
    is_owner,
    owner_active_card,
)
from gacore.companion.journal import (
    JOURNAL_PROMPT_VERSION,
    PRIVATE_REFLECTION_PROMPT_VERSION,
    JournalOutcome,
    JournalPayload,
    export_day_journals,
    generate_day_journals,
    generate_due_journals,
    run_journal,
    validate_journal_payload,
)
from gacore.companion.reflection import (
    REFLECTION_PROMPT_VERSION,
    ReflectionOutcome,
    reflect_due,
    run_reflection,
    validate_reflections,
)
from gacore.companion.mood import (
    MAX_MOOD_LINE_CHARS,
    MOOD_BLOCK_VERSION,
    MoodSignal,
    active_mood,
    build_mood_line,
    detect_signal,
    refresh_mood,
)
from gacore.companion.relationship import (
    DEFAULT_ACTOR_BOUNDARIES,
    RELATIONSHIP_PROJECTION_VERSION,
    BoundaryHit,
    RelationshipProjection,
    project,
    refresh_relationship,
)
from gacore.companion.reply_batch import (
    MAX_REPLY_SEGMENTS,
    REPLY_BATCH_VERSION,
    ReplyBatch,
    plan_final_reply,
)
from gacore.companion.summary import minimize_user_message

__all__ = [
    "ActiveCardCorruptError",
    "ActiveCardError",
    "ActiveCardRepository",
    "CompanionConfig",
    "DEFAULT_ACTOR_BOUNDARIES",
    "DEFAULT_ACTOR_ID",
    "RELATIONSHIP_PROJECTION_VERSION",
    "BoundaryHit",
    "RelationshipProjection",
    "JOURNAL_PROMPT_VERSION",
    "JournalOutcome",
    "JournalPayload",
    "MAX_MOOD_LINE_CHARS",
    "MAX_REPLY_SEGMENTS",
    "MOOD_BLOCK_VERSION",
    "MoodSignal",
    "MAX_SELF_MEMORY_CHARS",
    "MAX_SELF_MEMORY_REFLECTIONS",
    "PRIVATE_REFLECTION_PROMPT_VERSION",
    "REFLECTION_PROMPT_VERSION",
    "REPLY_BATCH_VERSION",
    "ReflectionCoordinator",
    "ReflectionOutcome",
    "ReplyBatch",
    "SELF_MEMORY_BLOCK_VERSION",
    "active_cards_path",
    "active_mood",
    "build_mood_line",
    "detect_signal",
    "actor_id_for",
    "build_self_memory_block",
    "default_active_cards_path",
    "export_day_journals",
    "generate_day_journals",
    "generate_due_journals",
    "is_owner",
    "load_companion_config",
    "minimize_user_message",
    "owner_active_card",
    "plan_final_reply",
    "project",
    "refresh_mood",
    "refresh_relationship",
    "reflect_due",
    "run_journal",
    "run_reflection",
    "schedule_reflection_check",
    "shutdown_reflection_coordinator",
    "validate_journal_payload",
    "validate_reflections",
]
