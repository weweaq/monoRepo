"""Companion V1 switches: one master enable plus the single owner's QQ openid.

Companion self-memory is deliberately owner-only (design §4.1). Both knobs are
read from the environment on every call so a missing owner leaves the feature
off — the code never guesses who the owner is.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

_ENABLED_ENV: Final = "COMPANION_MVP_ENABLED"
_OWNER_ENV: Final = "COMPANION_OWNER_QQ_OPENID"
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class CompanionConfig:
    """Resolved Companion switches; callers should gate on ``active``, not ``enabled``."""

    enabled: bool = False
    owner_openid: str | None = None

    @property
    def active(self) -> bool:
        """Companion is usable only when the switch is on AND an owner is configured."""
        return self.enabled and bool(self.owner_openid)


def load_companion_config(env: Mapping[str, str] | None = None) -> CompanionConfig:
    """Read the Companion switches from the environment (``os.environ`` by default)."""
    source = os.environ if env is None else env
    enabled = (source.get(_ENABLED_ENV) or "").strip().lower() in _TRUTHY
    owner = (source.get(_OWNER_ENV) or "").strip() or None
    return CompanionConfig(enabled=enabled, owner_openid=owner)
