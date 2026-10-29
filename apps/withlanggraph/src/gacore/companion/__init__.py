"""Companion self-memory (V1): the character remembers what it really said and did.

Modules land step by step per ``docs/companion-self-memory-v1-plan.md``:

- ``identity`` — the shared per-user active-card repository (Step 1)
"""

from __future__ import annotations

from gacore.companion.identity import (
    ActiveCardCorruptError,
    ActiveCardError,
    ActiveCardRepository,
    default_active_cards_path,
)

__all__ = [
    "ActiveCardCorruptError",
    "ActiveCardError",
    "ActiveCardRepository",
    "default_active_cards_path",
]
