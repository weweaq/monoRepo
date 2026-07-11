"""LLM 意图提取入口。

用法：
    python -m profile.cli.extract_intents          # 提取所有未处理记录
    python -m profile.cli.extract_intents trae     # 只提取 trae
"""

import sys

from profile.config import LOG_DIR
from profile.db.init_db import init_db
from profile.llm.client import LLMClient
from profile.llm.intent_extractor import IntentExtractor
from profile.log import get_logger, setup

logger = get_logger("cli.extract_intents")


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    setup(log_dir=LOG_DIR / "extract_intents")
    init_db()

    source = argv[0] if argv else None
    client = LLMClient()

    logger.info("意图提取启动", extra={
        "extra": {
            "source": source or "all",
            "llm_available": client.is_available(),
            "llm_model": client.model,
            "llm_api_url": client.api_url,
        }
    })

    if not client.is_available():
        logger.error("LLM API Key 未配置，无法提取意图")
        return 1

    extractor = IntentExtractor(client=client)
    count = extractor.extract_all(source=source)
    logger.info("意图提取完成", extra={
        "extra": {"extracted_count": count, "source": source or "all"}
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
