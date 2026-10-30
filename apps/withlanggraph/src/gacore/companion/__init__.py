"""Companion self-memory (V1): the character remembers what it really said and did.

Modules land step by step per ``docs/companion-self-memory-v1-plan.md``:

- ``config`` — master switch + the single owner openid (Step 2)
- ``identity`` — the shared per-user active-card repository and owner gate (Step 1/2)
"""

from __future__ import annotations

from gacore.companion.config import CompanionConfig, load_companion_config
from gacore.companion.identity import (
    ActiveCardCorruptError,
    ActiveCardError,
    ActiveCardRepository,
    active_cards_path,
    default_active_cards_path,
    is_owner,
    owner_active_card,
)

__all__ = [
    "ActiveCardCorruptError",
    "ActiveCardError",
    "ActiveCardRepository",
    "CompanionConfig",
    "active_cards_path",
    "default_active_cards_path",
    "is_owner",
    "load_companion_config",
    "owner_active_card",
]
