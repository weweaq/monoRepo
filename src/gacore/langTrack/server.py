"""FastAPI 应用：POST /ingest 接收上报，GET /health 健康检查，GET /dashboard 仪表盘。"""
from __future__ import annotations

import json
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
from fastapi.responses import FileResponse, HTMLResponse

from gacore.langTrack.dashboard import DB_PATH, build_map_day_data, render_dashboard_html
from gacore.langTrack.etl import canonical_device_id
from gacore.langTrack.schemas import IngestRequest
from gacore.langTrack.storage import Storage

logger = logging.getLogger("gacore.langTrack.server")

# 项目根目录：src/gacore/langTrack/server.py -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
# 服务端期望的客户端 App 版本（与 weiCheckApp app/build.gradle.kts versionName 保持一致）
# 每次客户端发版 Bump 时，这里要同步更新；latest_apk_url 指向可下载的 APK 路径
_EXPECTED_CLIENT_VERSION = "1.0"
_EXPECTED_CLIENT_APK_URL = ""
# 周期 ETL 间隔（秒），默认 30 分钟；可用环境变量覆盖
_ETL_INTERVAL_SECONDS = int(os.environ.get("LANGTRACK_ETL_INTERVAL_SECONDS", "1800"))
# 单次 ETL 超时（秒）
_ETL_TIMEOUT_SECONDS = int(os.environ.get("LANGTRACK_ETL_TIMEOUT_SECONDS", "120"))


def _run_etl_once() -> bool:
    """幂等重建事实表；失败只记日志不阻塞。返回是否成功（Task 12c）。"""
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
            return True
        logger.warning(
            "periodic ETL failed rc=%s stderr=%s",
            result.returncode,
            (result.stderr or "")[-2000:],
        )
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("periodic ETL error: %s", e)
        return False


# 手动/周期 ETL 共用的防重入状态（Task 12c：dashboard"立即转换"按钮）
_ETL_STATE_LOCK = threading.Lock()
_ETL_STATE: dict = {"running": False, "last_finished_at": None, "last_ok": None}


def _try_start_etl() -> bool:
    """非阻塞启动一次 ETL 后台任务；已有 ETL 在跑则返回 False（防重入）。

    周期线程与手动按钮共用同一守卫，杜绝两个 ETL 子进程并发写库。
    """
    with _ETL_STATE_LOCK:
        if _ETL_STATE["running"]:
            return False
        _ETL_STATE["running"] = True

    def _task() -> None:
        ok = False
        try:
            ok = _run_etl_once()
        finally:
            with _ETL_STATE_LOCK:
                _ETL_STATE["running"] = False
                _ETL_STATE["last_ok"] = bool(ok)
                _ETL_STATE["last_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    threading.Thread(target=_task, daemon=True, name="langTrack-etl-manual").start()
    return True


def _etl_loop(stop_event: threading.Event) -> None:
    while not stop_event.wait(_ETL_INTERVAL_SECONDS):
        try:
            _try_start_etl()
        except Exception:
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

    @app.get("/api/client/version")
    def client_version() -> dict:
        """客户端版本自查：服务端声明当前期望的 App 版本与 APK 下载链接。

        客户端（weiCheckApp 设置-关于）拉取此接口，与本机 versionName 对比，
        判断是否最新，解决"装的是不是最新版"。
        版本号来自 data/client_version.json（客户端打包脚本自动写入，见
        weiCheckApp build.ps1）；文件缺失时回退内置默认常量。
        """
        version = _EXPECTED_CLIENT_VERSION
        apk_url = _EXPECTED_CLIENT_APK_URL
        cfg = _PROJECT_ROOT / "data" / "client_version.json"
        if cfg.exists():
            try:
                obj = json.loads(cfg.read_text(encoding="utf-8"))
                version = obj.get("app_version", version)
                apk_url = obj.get("latest_apk_url", apk_url)
            except Exception as e:  # noqa: BLE001
                logger.warning("client_version.json unreadable: %s", e)
        return {"app_version": version, "latest_apk_url": apk_url}

    @app.get("/download/latest.apk")
    def download_latest_apk():
        """局域网内下载最新客户端 APK（build.ps1 打包后复制到 data/apk/）。"""
        apk = _PROJECT_ROOT / "data" / "apk" / "app-debug.apk"
        if not apk.exists():
            return HTMLResponse("APK not found — run weiCheckApp build.ps1 first.", status_code=404)
        return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="weiCheckApp.app-debug.apk")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(day: str | None = None) -> str:
        conn = sqlite3.connect(DB_PATH)
        try:
            return render_dashboard_html(conn, day)
        finally:
            conn.close()

    @app.post("/etl/run")
    def etl_run() -> dict:
        """手动触发一次 ETL（异步执行，立即返回；运行中则 busy，Task 12c）。"""
        started = _try_start_etl()
        return {"status": "started" if started else "busy"}

    @app.get("/etl/status")
    def etl_status() -> dict:
        with _ETL_STATE_LOCK:
            return dict(_ETL_STATE)

    @app.get("/api/map/day")
    def api_map_day(day: str | None = None, device_id: str | None = None) -> dict:
        """当日地图数据（点/停留/路线，GCJ02）——dashboard 地图卡前端数据源。"""
        target_day = day or time.strftime("%Y-%m-%d", time.localtime())
        conn = sqlite3.connect(DB_PATH)
        try:
            return build_map_day_data(conn, target_day, device_id)
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
