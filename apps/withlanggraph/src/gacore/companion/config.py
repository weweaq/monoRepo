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
_MAX_SENDS_ENV: Final = "COMPANION_MAX_SENDS_PER_DAY"
_MIN_GAP_ENV: Final = "COMPANION_MIN_GAP_MINUTES"
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
MAX_SENDS_DEFAULT: Final = 30
MIN_GAP_MINUTES_DEFAULT: Final = 30


@dataclass(frozen=True, slots=True)
class CompanionConfig:
    """Resolved Companion switches; callers should gate on ``active``, not ``enabled``."""

    enabled: bool = False
    owner_openid: str | None = None
    # Proactive pacing knobs: how many messages a day, and the minimum gap between two.
    max_sends_per_day: int = MAX_SENDS_DEFAULT
    min_gap_minutes: int = MIN_GAP_MINUTES_DEFAULT

    @property
    def active(self) -> bool:
        """Companion is usable only when the switch is on AND an owner is configured."""
        return self.enabled and bool(self.owner_openid)


def load_companion_config(env: Mapping[str, str] | None = None) -> CompanionConfig:
    """Read the Companion switches from the environment (``os.environ`` by default)."""
    source = os.environ if env is None else env
    enabled = (source.get(_ENABLED_ENV) or "").strip().lower() in _TRUTHY
    owner = (source.get(_OWNER_ENV) or "").strip() or None
    return CompanionConfig(
        enabled=enabled,
        owner_openid=owner,
        max_sends_per_day=_int_env(
            source, _MAX_SENDS_ENV, MAX_SENDS_DEFAULT, low=1, high=200
        ),
        min_gap_minutes=_int_env(
            source, _MIN_GAP_ENV, MIN_GAP_MINUTES_DEFAULT, low=1, high=24 * 60
        ),
    )


def _int_env(
    source: Mapping[str, str], name: str, default: int, *, low: int, high: int
) -> int:
    """Read a bounded integer switch; bad values fall back to the default, never crash."""
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(low, min(high, value))
