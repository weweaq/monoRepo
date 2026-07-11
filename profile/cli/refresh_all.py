"""每周刷新入口。

用法：
    python -m profile.cli.refresh_all
    python -m profile.cli.refresh_all --days 7
"""

import argparse

from profile.cli import generate_profiles, ingest
from profile.config import LOG_DIR
from profile.db.init_db import init_db
from profile.llm.client import LLMClient
from profile.log import get_logger, setup
from profile.output.writer import load_latest_json
from profile.refresh.diff import diff_profiles
from profile.refresh.reporter import write_change_report

logger = get_logger("cli.refresh_all")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="统计最近 N 天")
    args = parser.parse_args(argv)

    setup(log_dir=LOG_DIR / "refresh_all")
    init_db()

    # 1. 读取旧画像（从 json 文件）
    old_profiles = {}
    for source in ["trae", "marvis"]:
        old = load_latest_json(f"个人画像-{source}")
        if old:
            old_profiles[source] = old
    old_global = load_latest_json("个人画像-综合")

    # 2. 增量入库
    ingest.main([])

    # 3. 提取意图（如果 LLM 配置了）
    client = LLMClient()
    if client.is_available():
        from profile.cli import extract_intents
        extract_intents.main([])
    else:
        logger.warning("LLM 未配置，跳过意图提取")

    # 4. 生成新画像（同时输出 md + json）
    generate_profiles.main(["--days", str(args.days)])

    # 5. diff：从新生成的 json 对比上次 json
    new_profiles = {}
    for source in ["trae", "marvis"]:
        new = load_latest_json(f"个人画像-{source}")
        if new:
            new_profiles[source] = new
    new_global = load_latest_json("个人画像-综合")

    changes = []
    for source, new in new_profiles.items():
        old = old_profiles.get(source, {})
        if old:
            changes.extend(diff_profiles(old, new, profile_type=source))
    if old_global and new_global:
        changes.extend(diff_profiles(old_global, new_global, profile_type="global"))

    if changes:
        write_change_report(changes)
    else:
        logger.info("无显著变化")

    logger.info("刷新完成")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
