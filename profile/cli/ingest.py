"""数据入库入口。

用法：
    python -m profile.cli.ingest          # 入所有可用数据源
    python -m profile.cli.ingest trae     # 只入 trae
    python -m profile.cli.ingest marvis   # 只入 marvis
"""

import sys

from profile.config import LOG_DIR
from profile.db.init_db import init_db
from profile.io.trae_reader import TraeReader
from profile.io.marvis_reader import MarvisReader
from profile.log import get_logger, setup

logger = get_logger("cli.ingest")

READERS = {
    "trae": TraeReader,
    "marvis": MarvisReader,
}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    setup(log_dir=LOG_DIR / "ingest")
    init_db()

    if argv:
        names = [n for n in argv if n in READERS]
    else:
        names = list(READERS.keys())

    if not names:
        logger.error("无效的数据源名称", context={"argv": argv, "available": list(READERS.keys())})
        return 1

    logger.info("入库启动", extra={
        "extra": {"sources": names, "source_count": len(names)}
    })

    results = {}
    for name in names:
        reader = READERS[name]()
        count = reader.ingest()
        results[name] = count

    total = sum(results.values())
    logger.info("全部入库完成", extra={
        "extra": {"total": total, "per_source": results}
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
