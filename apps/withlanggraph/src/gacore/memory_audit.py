"""Observability for memory-maintenance (维测层): every trigger & write is auditable.

Two artifacts, both under ``logs/<YYYY-MM-DD>/``:

- ``memory_maintenance.jsonl`` — one append-only line per judged trigger (SKIP also
  counts as a probe, tagged ``skipped``). Kept separate from ``app.jsonl`` so the
  maintenance trail can be inspected without wading through process logs.
- ``memory_maintenance_state.json`` — a day-level rollup (probes, triggers, per-verdict
  counts, first/last write) rebuilt from the jsonl. Handy for "今天旁路触发了多少次、
  改动了什么" at a glance.

Both are written by the maintenance pipeline (see ``memory_maintenance.record_audit``).
Pure functions here for testability: no module-level state, cfg resolved per call so
tests stay in tmp.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gacore.config import Config

_AUDIT_LOG: Final = "memory_maintenance.jsonl"
_AUDIT_STATE: Final = "memory_maintenance_state.json"
_SKIPPED: Final = "SKIPPED"

# Verdict labels (duplicated from memory_maintenance to avoid an import cycle; they are
# stable string constants shared across the two modules).
VERDICT_MERGE: Final = "MERGE"
VERDICT_NEW: Final = "NEW"
VERDICT_NOOP: Final = "NOOP"
ERROR_LBL: Final = "ERROR"


def _day_str() -> str:
    """East-8 calendar day string, e.g. 2026-09-09."""
    return datetime.now(UTC).astimezone().date().isoformat()


def _audit_dir(cfg: Config, day: str) -> Path:
    return cfg.logs_dir / day


def _state_path(cfg: Config, day: str) -> Path:
    return _audit_dir(cfg, day) / _AUDIT_STATE


def _append_line(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _bump_counter(state: dict, key: str, by: int = 1) -> None:
    state[key] = state.get(key, 0) + by


def _action_key(res: dict) -> str:
    """Map a maintenance result to a stable audit bucket: MERGE/NEW/NOOP/ERROR/NOOP(skip)."""
    action = res.get("action")
    if res.get("updated") is True:
        return str(action)
    if res.get("triggered") is False or action == _SKIPPED:
        return _SKIPPED
    # judged-but-unchanged: NOOP and ERROR both surface here; distinguish via 'action'
    return str(action or "UNKNOWN")


def record_audit(
    cfg: Config,
    res: dict,
    *,
    trigger: object | None = None,
    ms: int = 0,
) -> dict:
    """Persist one maintenance result to the day's audit jsonl and roll up the state.

    ``res`` is whatever ``memory_maintenance.maintain_once`` returned. Writes are
    best-effort: a failing audit must never break the turn (wrap failures, drop the
    entry rather than raise).
    """
    day = _day_str()
    entry = {
        "ts": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "day": day,
        "action": _action_key(res),
        "triggered": bool(res.get("triggered")),
        "matched": res.get("matched") or [],
        "reason": res.get("reason") or "",
        "ms": ms,
        "updated": bool(res.get("updated")),
        "fact": _truncate(res.get("fact") or res.get("reason") or ""),
        "paths": res.get("paths") or [],
        "error": res.get("error") or "",
    }
    # attach trigger kind when an object with .kind was passed
    kind = getattr(trigger, "kind", "")
    if kind:
        entry["trigger_kind"] = kind

    log_path = _audit_dir(cfg, day) / _AUDIT_LOG
    state_path = _state_path(cfg, day)
    try:
        _append_line(log_path, entry)
        state = _load_state(state_path)
        _bump_counter(state, "probes")
        if res.get("triggered"):
            _bump_counter(state, "triggers")
        _bump_counter(state, "by_action", _action_key(res))
        state["last_ts"] = entry["ts"]
        state["last_action"] = entry["action"]
        state["touched_files"] = sorted(set(state.get("touched_files", []) + list(res.get("paths") or [])))
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        # audit must stay invisible to the happy path — swallow anything here.
        pass
    return entry


def _truncate(s: str, limit: int = 160) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _load_state(path: Path) -> dict:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {"by_action": {}}


def query_day(cfg: Config, day: str | None = None) -> dict:
    """Return the rollup for a day (default today) from the state file.

    Empty state (no audits yet, or a corrupt file) yields a zeroed rollup so callers can
    render consistently.
    """
    day = day or _day_str()
    state = _load_state(_state_path(cfg, day))
    counters = ("probes", "triggers", VERDICT_MERGE, VERDICT_NEW, VERDICT_NOOP, ERROR_LBL, _SKIPPED)
    for key in counters:
        state.setdefault(key, 0)
    by_action = state.setdefault("by_action", {})
    for key in (VERDICT_MERGE, VERDICT_NEW, VERDICT_NOOP, ERROR_LBL, _SKIPPED):
        by_action.setdefault(key, 0)
    return {"day": day, **state}


def read_entries(cfg: Config, day: str | None = None) -> list[dict]:
    """Return the raw audit lines for a day (empty list when none)."""
    day = day or _day_str()
    path = _audit_dir(cfg, day) / _AUDIT_LOG
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except ValueError:
            continue
    return out


__all__ = (
    "query_day",
    "read_entries",
    "record_audit",
)