"""Typed configuration for gacore: project paths and tunables resolved from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import dotenv

_DEFAULT_MAX_TURNS: Final = 40
_MIN_MAX_TURNS: Final = 1
# config.py lives at <root>/src/gacore/config.py, so parents[2] is the project root (parent of src/).
_PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    """Raised when configuration cannot be built from the given environment."""


def _resolve_dir(root: Path, env: Mapping[str, str], env_name: str, default_rel: Path) -> Path:
    """Resolve a directory: absolute override kept, relative override rooted at root, else root/default_rel."""
    raw = env.get(env_name)
    if raw is None:
        return root / default_rel
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else root / candidate


def _parse_max_turns(raw: str) -> int:
    """Parse DEFAULT_MAX_TURNS, rejecting non-integers and values below one."""
    try:
        max_turns = int(raw)
    except ValueError as e:
        raise ConfigError(f"DEFAULT_MAX_TURNS must be an integer, got {raw!r}") from e
    if max_turns < _MIN_MAX_TURNS:
        raise ConfigError(f"DEFAULT_MAX_TURNS must be at least {_MIN_MAX_TURNS}, got {max_turns}")
    return max_turns


def _parse_bool(raw: str | None, default: bool) -> bool:
    """Parse a boolean env value; unknown/missing values fall back to the given default."""
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_positive_int(raw: str | None, default: int) -> int:
    """Parse a positive-int env value; invalid/missing values fall back to the given default."""
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class RolloverConfig:
    """Cross-day memory rollover policy for the QQ frontend.

    Controls the "fresh thread every day + inject yesterday's memory pack into the
    first message of a new day" mechanism:
      - enabled: master switch. Off disables both rollover and pack consumption.
      - inject_long_term_full: True injects the full global_mem_insight.txt into the
        system prompt on the first turn after rollover; False injects a compact
        summary derived from it.
      - keep_old_thread: True keeps the previous day's SQLite checkpoint (searchable
        history); False would delete it (not yet wired to deletion, reserved flag).
      - recent_days: how many recent daily notes get summarized into the onboard
        pack exported by scheduler.py after the daily-report job.
    """

    enabled: bool = True
    inject_long_term_full: bool = False
    keep_old_thread: bool = True
    recent_days: int = 3


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration shared by every gacore module.

    Directories resolve against the project root (the parent of src/) unless overridden by the
    GACORE_* environment variables. Frozen by construction; every field is read-only.
    """

    root: Path
    asset_dir: Path
    memory_dir: Path
    logs_dir: Path
    temp_dir: Path
    max_turns: int = _DEFAULT_MAX_TURNS
    rollover: RolloverConfig = RolloverConfig()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build a Config from an environment mapping; os.environ when env is None."""
        source = os.environ if env is None else env
        return cls(
            root=_PROJECT_ROOT,
            asset_dir=_resolve_dir(_PROJECT_ROOT, source, "GACORE_ASSET_DIR", Path("config") / "assets"),
            memory_dir=_resolve_dir(_PROJECT_ROOT, source, "GACORE_MEMORY_DIR", Path("memory")),
            logs_dir=_resolve_dir(_PROJECT_ROOT, source, "GACORE_LOGS_DIR", Path("logs")),
            temp_dir=_resolve_dir(_PROJECT_ROOT, source, "GACORE_TEMP_DIR", Path("temp")),
            max_turns=_parse_max_turns(source.get("DEFAULT_MAX_TURNS", str(_DEFAULT_MAX_TURNS))),
            rollover=RolloverConfig(
                enabled=_parse_bool(source.get("GACORE_ROLLOVER_ENABLED"), True),
                inject_long_term_full=_parse_bool(source.get("GACORE_ROLLOVER_INJECT_LONG_TERM_FULL"), False),
                keep_old_thread=_parse_bool(source.get("GACORE_ROLLOVER_KEEP_OLD_THREAD"), True),
                recent_days=_parse_positive_int(source.get("GACORE_ROLLOVER_RECENT_DAYS"), 3),
            ),
        )

    @classmethod
    def default(cls) -> Config:
        """Build the Config for this process from os.environ."""
        return cls.from_env(os.environ)

    @classmethod
    def for_tests(cls, tmp_path: Path) -> Config:
        """Build a Config rooted at a pytest tmp_path so tests never touch real project dirs."""
        return cls(
            root=tmp_path,
            asset_dir=tmp_path / "config" / "assets",
            memory_dir=tmp_path / "memory",
            logs_dir=tmp_path / "logs",
            temp_dir=tmp_path / "temp",
            rollover=RolloverConfig(),
        )


def load_dotenv() -> None:
    """Load the project root .env into os.environ when present; otherwise a no-op."""
    dotenv.load_dotenv(_PROJECT_ROOT / ".env")
