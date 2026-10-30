"""Single-flight background runner for Companion reflection checks.

Every confirmed event write submits a cheap check; the model call itself never
runs on the QQ event loop. One in-flight check per ``(actor_id, principal_id)``
means two near-simultaneous events cannot spend two model calls on the same
batch, and duplicate events never submit at all (callers gate on
``AppendResult.inserted``).

Shutdown stops accepting new checks and waits a bounded time for running ones.
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Final

from gacore.companion.reflection import Runner, reflect_due
from gacore.companion.store import CompanionStore, companion_db_path
from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("companion.coordinator")

StoreFactory = Callable[[], CompanionStore]
ClockMs = Callable[[], int]

DEFAULT_SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0


class ReflectionCoordinator:
    """Submit reflection checks to one background worker with per-actor single-flight."""

    def __init__(
        self,
        *,
        store_factory: StoreFactory,
        runner: Runner | None = None,
        clock_ms: ClockMs | None = None,
        max_workers: int = 1,
    ) -> None:
        self._store_factory = store_factory
        self._runner = runner
        self._clock_ms = clock_ms
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="companion-reflection"
        )
        self._lock = threading.Lock()
        self._pending: set[tuple[str, str]] = set()
        # Keys that received a new event while a check was already running. Without
        # this flag that event would wait for the next external schedule() call
        # (lost wakeup); instead _finish resubmits once.
        self._rerun: set[tuple[str, str]] = set()
        self._futures: set[Future[None]] = set()
        self._closed = False

    def schedule(self, actor_id: str, principal_id: str) -> bool:
        """Submit one check; False means it was already in flight or the runner is closed."""
        key = (actor_id, principal_id)
        with self._lock:
            if self._closed:
                return False
            if key in self._pending:
                self._rerun.add(key)
                return False
            self._pending.add(key)
        try:
            future = self._executor.submit(self._run, actor_id, principal_id)
        except RuntimeError:
            with self._lock:
                self._pending.discard(key)
            return False
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(lambda done: self._finish(key, done))
        return True

    def shutdown(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Refuse new checks, cancel queued ones, and wait ``timeout`` for running ones."""
        with self._lock:
            self._closed = True
            self._rerun.clear()
            futures = list(self._futures)
        self._executor.shutdown(wait=False, cancel_futures=True)
        wait(futures, timeout=timeout)

    def _run(self, actor_id: str, principal_id: str) -> None:
        try:
            store = self._store_factory()
            reflect_due(
                store,
                actor_id,
                principal_id,
                runner=self._runner,
                now_ms=self._clock_ms() if self._clock_ms is not None else None,
            )
        except Exception:  # noqa: BLE001 — a memory check must never surface to the chat path
            logger.warning(
                "companion reflection check failed",
                actor_id=actor_id,
                principal_id=principal_id,
                stack_trace=traceback.format_exc(),
            )

    def _finish(self, key: tuple[str, str], _future: Future[None]) -> None:
        with self._lock:
            self._pending.discard(key)
            self._futures.discard(_future)
            rerun = key in self._rerun
            self._rerun.discard(key)
            closed = self._closed
        # An event arrived while this check was running: run one more check so the
        # newer event is never left unexamined until some unrelated future trigger.
        if rerun and not closed:
            self.schedule(key[0], key[1])


# One process-wide coordinator; the store is created lazily so importing this
# module never touches the database (tests and CLI imports stay side-effect free).
_stores: dict[Path, CompanionStore] = {}
_stores_lock = threading.Lock()
_coordinator: ReflectionCoordinator | None = None
_coordinator_lock = threading.Lock()


def _default_store() -> CompanionStore:
    cfg = Config.default()
    path = companion_db_path(cfg)
    with _stores_lock:
        store = _stores.get(path)
        if store is None:
            store = _stores[path] = CompanionStore.from_config(cfg)
        return store


def get_default_coordinator() -> ReflectionCoordinator:
    """Return the process-wide coordinator, creating it on first use."""
    global _coordinator
    with _coordinator_lock:
        if _coordinator is None:
            _coordinator = ReflectionCoordinator(store_factory=_default_store)
        return _coordinator


def schedule_reflection_check(actor_id: str, principal_id: str) -> None:
    """Best-effort submit; callers only invoke this for newly inserted events."""
    if not actor_id:
        return
    try:
        get_default_coordinator().schedule(actor_id, principal_id)
    except Exception:  # noqa: BLE001 — scheduling must never break the chat path
        logger.warning(
            "companion reflection scheduling failed",
            actor_id=actor_id,
            principal_id=principal_id,
            stack_trace=traceback.format_exc(),
        )


def shutdown_reflection_coordinator(timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
    """Stop accepting checks and wait a bounded time; safe to call more than once."""
    global _coordinator
    with _coordinator_lock:
        coordinator = _coordinator
        _coordinator = None
    if coordinator is not None:
        coordinator.shutdown(timeout=timeout)
