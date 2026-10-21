"""Component heartbeat files for the P0 observability loop.

One JSON file per component under ``data/run/`` (override: ``GACORE_RUN_DIR``);
see ``docs/observability-design.md`` §5 for the file contract and state machine.

Deliberately loop-driven: ``Heartbeat`` never starts a thread of its own, so a
stuck component stops beating and ages out (stale) instead of appearing alive.
Every write is atomic (temp file + ``os.replace``) and write failures are
swallowed with a throttled WARNING — heartbeat trouble must never break business.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, final

from gacore.jsonl_logger import get_logger

logger = get_logger("heartbeat")

DEFAULT_INTERVAL_SECONDS: Final = 15
RUN_DIR_ENV: Final = "GACORE_RUN_DIR"
SCHEMA_VERSION: Final = 1
_WARN_THROTTLE_SECONDS: Final = 60
_UTC_PLUS_8: Final = timezone(timedelta(hours=8))


def run_dir(root: Path) -> Path:
    """Return the heartbeat directory: GACORE_RUN_DIR override, else <root>/data/run.

    A relative override resolves against ``root`` (the project root for production
    callers, a tmp dir for tests).
    """
    override = os.environ.get(RUN_DIR_ENV)
    if override:
        path = Path(override)
        if not path.is_absolute():
            path = Path(root) / path
        return path
    return Path(root) / "data" / "run"


@final
class Heartbeat:
    """Writes one component's heartbeat file; never raises into business code.

    Callers own the cadence: call ``beat()`` from the component's own loop or from
    a task bound to the same event loop. State-only ``beat()`` calls keep the last
    state, so a periodic beat cannot flatten ``reconnecting`` into ``running``.
    """

    def __init__(
        self,
        component: str,
        *,
        root: Path,
        interval_sec: int = DEFAULT_INTERVAL_SECONDS,
        stale_after_sec: int | None = None,
    ) -> None:
        self._component = component
        self._root = Path(root)
        self._path = self._root / f"{component}.json"
        self._interval_sec = interval_sec
        self._default_stale_after = (
            stale_after_sec if stale_after_sec is not None else max(60, interval_sec * 3)
        )
        self._instance_id = uuid.uuid4().hex[:8]
        self._pid = os.getpid()
        self._state = "starting"
        self._seq = 0
        self._started_at = datetime.now(_UTC_PLUS_8).isoformat(timespec="seconds")
        self._lock = threading.Lock()
        self._last_warn_monotonic = 0.0

    def start(self, **detail: object) -> None:
        """Write the initial ``starting`` beat."""
        self._state = "starting"
        self._write(stale_after_sec=None, detail=detail)

    def beat(
        self,
        *,
        state: str | None = None,
        stale_after_sec: int | None = None,
        **detail: object,
    ) -> None:
        """Write one beat; omitted ``state`` keeps the previous state.

        ``stale_after_sec`` is per-write: passing it widens (or narrows) the window
        for this beat only; the next beat without it returns to the default window.
        """
        if state is not None:
            self._state = state
        self._write(stale_after_sec=stale_after_sec, detail=detail)

    def stop(self, *, state: str = "stopped", **detail: object) -> None:
        """Write the terminal beat (``stopped`` by default, ``error`` on crash paths)."""
        self._state = state
        self._write(stale_after_sec=None, detail=detail)

    def _write(self, *, stale_after_sec: int | None, detail: dict[str, object]) -> None:
        with self._lock:
            now = datetime.now(_UTC_PLUS_8)
            self._seq += 1
            payload: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "component": self._component,
                "instance_id": self._instance_id,
                "pid": self._pid,
                "state": self._state,
                "seq": self._seq,
                "started_at": self._started_at,
                "updated_at": now.isoformat(timespec="seconds"),
                "updated_at_epoch": now.timestamp(),
                "interval_sec": self._interval_sec,
                "stale_after_sec": (
                    stale_after_sec if stale_after_sec is not None else self._default_stale_after
                ),
                "detail": dict(detail),
            }
            tmp = self._root / f".{self._component}.{self._pid}.{self._instance_id}.{self._seq}.tmp"
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self._path)
            except Exception as exc:  # noqa: BLE001 - heartbeat must never break business
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._warn_throttled(exc)

    def _warn_throttled(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_warn_monotonic < _WARN_THROTTLE_SECONDS:
            return
        self._last_warn_monotonic = now
        logger.warning(
            "heartbeat write failed",
            component=self._component,
            path=str(self._path),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
