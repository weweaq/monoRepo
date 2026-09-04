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
import sys

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
from gacore.jsonl_logger import get_logger
from gacore.scheduler import run_loop

logger = get_logger("start")


async def _main() -> None:
    """Start scheduler in a background thread, then run QQ Bot on the main loop."""
    cfg = Config.default()

    # Scheduler runs in a helper thread (run_loop is synchronous, uses time.sleep).
    scheduler_task = asyncio.create_task(asyncio.to_thread(run_loop, cfg))
    logger.info("Scheduler started (background thread)")

    # QQ Bot occupies the main asyncio event loop.
    graph = await build_config()
    app = QQApp(graph)
    logger.info("QQ frontend starting")
    try:
        await app.start()
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            logger.info("Scheduler stopped")


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
