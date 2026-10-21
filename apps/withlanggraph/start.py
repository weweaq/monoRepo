"""Unified entry point: starts QQ Bot frontend and scheduled-job runner in one process.

One fleet, one helm: ``python start.py`` launches both the QQ Bot and the scheduler
background thread. When future frontends (WeChat, etc.) are added, just register them
here — scheduler stays a single shared engine.

Usage:
    python start.py                    # QQ Bot + Scheduler
    python -m gacore.frontends.qq      # QQ Bot only (debug)
    python -m gacore.scheduler         # Scheduler only (debug)
"""

from __future__ import annotations

import asyncio
import functools
import sys
import threading

from gacore.config import Config, load_dotenv
from gacore.frontends.qq import (
    QQApp,
    _APP_ID,
    _APP_SECRET,
    _ensure_single_instance,
    _fix_encoding,
    _redirect_log,
    build_config,
)
from gacore.heartbeat import DEFAULT_INTERVAL_SECONDS, Heartbeat, run_dir
from gacore.jsonl_logger import get_logger
from gacore.scheduler import on_scheduler_done, run_loop

logger = get_logger("start")


async def _main() -> None:
    """Start scheduler in a background thread, then run QQ Bot on the main loop."""
    cfg = Config.default()

    # Main heartbeat: loop-driven via an asyncio task, so a wedged event loop
    # stops beating and is detected as stale instead of looking alive.
    heartbeat = Heartbeat("main", root=run_dir(cfg.root))
    heartbeat.start(launcher="start.py")
    heartbeat.beat(state="running", launcher="start.py")

    async def _beat_main() -> None:
        while True:
            await asyncio.sleep(DEFAULT_INTERVAL_SECONDS)
            heartbeat.beat(launcher="start.py")

    beat_task = asyncio.create_task(_beat_main())

    # Scheduler runs in a helper thread (run_loop is synchronous, uses time.sleep).
    # Graceful shutdown uses stop_event: asyncio cannot cancel the worker thread.
    stop_event = threading.Event()
    scheduler_task = asyncio.create_task(asyncio.to_thread(run_loop, cfg, stop_event=stop_event))
    scheduler_task.add_done_callback(functools.partial(on_scheduler_done, stop_event=stop_event))
    logger.info("Scheduler started (background thread)")

    try:
        # QQ Bot occupies the main asyncio event loop.
        graph = await build_config()
        app = QQApp(graph)
        logger.info("QQ frontend starting")
        await app.start()
    finally:
        stop_event.set()
        try:
            await scheduler_task
        except Exception:  # noqa: BLE001 — failure already logged by the done callback
            pass
        beat_task.cancel()
        try:
            await beat_task
        except asyncio.CancelledError:
            pass
        heartbeat.stop()


def main() -> None:
    """CLI entry point for ``python start.py``."""
    _fix_encoding()

    if not _APP_ID or not _APP_SECRET:
        logger.error("Please set QQ_APP_ID and QQ_APP_SECRET in .env")
        sys.exit(1)

    _ensure_single_instance()
    _redirect_log()

    load_dotenv()
    logger.info("Unified launcher starting (QQ Bot + Scheduler)")
    asyncio.run(_main())


if __name__ == "__main__":
    main()
