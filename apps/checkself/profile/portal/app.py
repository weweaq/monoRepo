"""FastAPI 应用入口。

用法:
    python -m profile.portal.app
    python -m profile.portal.app --port 8080
"""

import argparse
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from profile.db.init_db import init_db
from profile.config import LOG_DIR, ensure_dirs
from profile.log import setup
from profile.portal.db_store import recover_stale_tasks
from profile.portal.routes import dashboard, tasks, data, llm, profiles

app = FastAPI(title="checkSelf Portal")

# 静态文件
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def on_startup():
    """启动时初始化。"""
    setup(log_dir=LOG_DIR)
    ensure_dirs()
    init_db()
    recover_stale_tasks()


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# 注册路由
app.include_router(dashboard.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(data.router, prefix="/api")
app.include_router(llm.router, prefix="/api")
app.include_router(profiles.router, prefix="/api")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="checkSelf Portal")
    parser.add_argument("--port", type=int, default=8000, help="端口号")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if not args.no_browser:
        webbrowser.open(f"http://{args.host}:{args.port}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
