"""python -m gacore.langTrack 启动接收服务。"""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from gacore.langTrack.server import create_app
from gacore.langTrack.storage import Storage

# 项目根目录：src/gacore/langTrack/__main__.py -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    parser = argparse.ArgumentParser(description="langTrack ingest server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    # 默认落到项目 data/ 目录，持久安全，不随启动目录变化
    parser.add_argument(
        "--db",
        default=str(_PROJECT_ROOT / "data" / "langTrack.db"),
        help="SQLite 文件路径",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    storage = Storage(db_path)
    app = create_app(storage)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
