"""数据入库入口。

用法：
    python -m profile.cli.ingest              # 入所有可用数据源
    python -m profile.cli.ingest trae         # 只入 trae
    python -m profile.cli.ingest marvis       # 只入 marvis
    python -m profile.cli.ingest bilibili     # 只入 bilibili（含 view+tag 二次抓取）
"""

import os
import sys

from profile.config import LOG_DIR
from profile.db.init_db import init_db
from profile.io.registry import get_reader, list_reader_names
from profile.log import get_logger, setup

logger = get_logger("cli.ingest")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    setup(log_dir=LOG_DIR)
    init_db()

    if "--refresh" in argv:
        os.environ["BILI_FORCE_REFRESH"] = "1"
        os.environ["NETEASE_FORCE_REFRESH"] = "1"
        argv = [a for a in argv if a != "--refresh"]

    available = list_reader_names()
    if argv:
        names = [n for n in argv if n in available]
    else:
        names = available

    if not names:
        logger.error("无效的数据源名称", context={"argv": argv, "available": available})
        return 1

    logger.info("入库启动", extra={
        "extra": {"sources": names, "source_count": len(names)}
    })

    results = {}
    for name in names:
        reader = get_reader(name)
        if reader is None:
            logger.warning("数据源不可用，跳过", extra={"extra": {"source": name}})
            continue
        count = reader.ingest()
        results[name] = count

    total = sum(results.values())
    logger.info("全部入库完成", extra={
        "extra": {"total": total, "per_source": results}
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
