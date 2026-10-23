"""Component heartbeat files for the P0 observability loop.

One JSON file per component under ``data/run/`` (override: ``GACORE_RUN_DIR``);
see ``docs/observability-design.md`` §5 for the file contract and state machine.

Deliberately loop-driven: ``Heartbeat`` never starts a thread of its own, so a
stuck component stops beating and ages out (stale) instead of appearing alive.
Every write is atomic (temp file + ``os.replace``); a transient filesystem error
(Windows ``WinError 5/32``) is retried with backoff before giving up. Remaining
failures are swallowed with a throttled WARNING — heartbeat trouble must never
break business.
"""

from __future__ import annotations

import errno
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
# Windows 上紧接着另一次替换/杀软扫描时 os.replace 会短暂 WinError 5（拒绝访问）；
# 退避重试能把这类瞬时冲突消化掉（2026-10-22 实测 29 次，集中在实例交替期）。
_WRITE_ATTEMPTS: Final = 3
_WRITE_RETRY_BACKOFF_SECONDS: Final = 0.02
# 只有"可能下一秒就好了"的错误才值得重试：Windows 的 ACCESS_DENIED(5) /
# SHARING_VIOLATION(32) / LOCK_VIOLATION(33) 与 POSIX 的 EACCES/EPERM/EBUSY/EAGAIN。
_TRANSIENT_ERRNOS: Final = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY, errno.EAGAIN})
_TRANSIENT_WINERRORS: Final = frozenset({5, 32, 33})
_UTC_PLUS_8: Final = timezone(timedelta(hours=8))


def _is_transient_fs_error(exc: OSError) -> bool:
    """True when retrying the write could plausibly succeed (lock/AV/instance handover)."""
    if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
        return True
    return exc.errno in _TRANSIENT_ERRNOS


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
            payload_text = json.dumps(payload, ensure_ascii=False, default=str)
            failure: Exception | None = None
            for attempt in range(1, _WRITE_ATTEMPTS + 1):
                try:
                    self._root.mkdir(parents=True, exist_ok=True)
                    tmp.write_text(payload_text, encoding="utf-8")
                    os.replace(tmp, self._path)
                    return
                except OSError as exc:  # 含 PermissionError(WinError 5)：多为瞬时冲突，退避重试
                    failure = exc
                    if attempt < _WRITE_ATTEMPTS and _is_transient_fs_error(exc):
                        time.sleep(_WRITE_RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    break
                except Exception as exc:  # noqa: BLE001 - heartbeat must never break business
                    failure = exc
                    break
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if failure is not None:
                self._warn_throttled(failure)

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
