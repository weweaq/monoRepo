"""Companion self-memory (V1): the character remembers what it really said and did.

Modules land step by step per ``docs/companion-self-memory-v1-plan.md``:

- ``config`` — master switch + the single owner openid (Step 2)
- ``identity`` — the shared per-user active-card repository and owner gate (Step 1/2)
- ``store`` — the SQLite fact source for events / reflections / cursor (Step 3)
- ``summary`` — redacted, bounded event summaries (Step 4)
- ``reflection`` — evidence-bound 0-2 takeaways from a fixed batch (Step 7)
- ``coordinator`` — single-flight background reflection checks (Step 7)
"""

from __future__ import annotations

from gacore.companion.config import CompanionConfig, load_companion_config
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
from gacore.companion.reflection import (
    ReflectionOutcome,
    reflect_due,
    run_reflection,
    validate_reflections,
)
from gacore.companion.summary import minimize_user_message

__all__ = [
    "ActiveCardCorruptError",
    "ActiveCardError",
    "ActiveCardRepository",
    "CompanionConfig",
    "DEFAULT_ACTOR_ID",
    "ReflectionCoordinator",
    "ReflectionOutcome",
    "active_cards_path",
    "actor_id_for",
    "default_active_cards_path",
    "is_owner",
    "load_companion_config",
    "minimize_user_message",
    "owner_active_card",
    "reflect_due",
    "run_reflection",
    "schedule_reflection_check",
    "shutdown_reflection_coordinator",
    "validate_reflections",
]
