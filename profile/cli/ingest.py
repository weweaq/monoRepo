"""数据入库入口。

用法：
    python -m profile.cli.ingest          # 入所有可用数据源
    python -m profile.cli.ingest trae     # 只入 trae
    python -m profile.cli.ingest marvis   # 只入 marvis
"""

import sys

from profile.db.init_db import init_db
from profile.io.trae_reader import TraeReader
from profile.io.marvis_reader import MarvisReader
from profile.log import get_logger, setup
from profile.config import LOG_DIR

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
        logger.error("无效的数据源名称", context={"argv": argv})
        return 1

    total = 0
    for name in names:
        reader = READERS[name]()
        total += reader.ingest()

    logger.info("全部入库完成", extra={"extra": {"total": total}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
