"""FastAPI 应用：POST /ingest 接收上报，GET /health 健康检查，GET /dashboard 仪表盘。"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from gacore.langTrack.dashboard import DB_PATH, render_dashboard_html
from gacore.langTrack.etl import canonical_device_id
from gacore.langTrack.schemas import IngestRequest
from gacore.langTrack.storage import Storage

logger = logging.getLogger("gacore.langTrack.server")

# 项目根目录：src/gacore/langTrack/server.py -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
# 周期 ETL 间隔（秒），默认 30 分钟；可用环境变量覆盖
_ETL_INTERVAL_SECONDS = int(os.environ.get("LANGTRACK_ETL_INTERVAL_SECONDS", "1800"))
# 单次 ETL 超时（秒）
_ETL_TIMEOUT_SECONDS = int(os.environ.get("LANGTRACK_ETL_TIMEOUT_SECONDS", "120"))


def _run_etl_once() -> None:
    """幂等重建事实表；失败只记日志不阻塞。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "gacore.langTrack.etl"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            timeout=_ETL_TIMEOUT_SECONDS,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode == 0:
            logger.info("periodic ETL ok")
        else:
            logger.warning(
                "periodic ETL failed rc=%s stderr=%s",
                result.returncode,
                (result.stderr or "")[-2000:],
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("periodic ETL error: %s", e)


def _etl_loop(stop_event: threading.Event) -> None:
    while not stop_event.wait(_ETL_INTERVAL_SECONDS):
        try:
            _run_etl_once()
        except Exception:  # noqa: BLE001
            logger.exception("periodic ETL loop crashed")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_etl_loop, args=(stop_event,), daemon=True, name="langTrack-etl"
    )
    thread.start()
    logger.info("periodic ETL thread started (interval=%ss)", _ETL_INTERVAL_SECONDS)
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=5)


def create_app(storage: Storage) -> FastAPI:
    app = FastAPI(title="langTrack ingest", lifespan=_lifespan)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(day: str | None = None) -> str:
        conn = sqlite3.connect(DB_PATH)
        try:
            return render_dashboard_html(conn, day)
        finally:
            conn.close()

    @app.post("/ingest")
    def ingest(req: IngestRequest) -> dict:
        received_at = int(time.time() * 1000)
        # 入库即归一别名设备（Task 12b）：原始层只存主设备，ETL 归并仅兜底历史行
        device_id = canonical_device_id(req.device_id)
        storage.upsert_device(device_id, req.client_ts)

        inserted = storage.ingest_batch(
            req.batch_id,
            device_id,
            received_at,
            [(ev.ts, ev.type, ev.data) for ev in req.events],
        )
        if not inserted:
            return {"status": "ok", "inserted": 0, "deduplicated": True}

        return {"status": "ok", "inserted": len(req.events), "deduplicated": False}

    return app
