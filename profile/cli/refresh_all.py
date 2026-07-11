"""每周刷新入口。

用法：
    python -m profile.cli.refresh_all
    python -m profile.cli.refresh_all --days 7
"""

import argparse
import time

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
    t_start = time.monotonic()
    logger.info("刷新流水线启动", extra={
        "extra": {"days": args.days}
    })

    init_db()

    # 1. 读取旧画像（从 json 文件）
    logger.info("步骤1/5: 读取旧画像")
    old_profiles = {}
    for source in ["trae", "marvis"]:
        old = load_latest_json(f"个人画像-{source}")
        if old:
            old_profiles[source] = old
            logger.info("加载旧画像", extra={
                "extra": {"source": source, "keys": list(old.keys())}
            })
    old_global = load_latest_json("个人画像-综合")
    if old_global:
        logger.info("加载旧综合画像", extra={
            "extra": {"keys": list(old_global.keys())}
        })
    logger.info("旧画像读取完成", extra={
        "extra": {
            "channel_profiles": list(old_profiles.keys()),
            "has_global": old_global is not None,
        }
    })

    # 2. 增量入库
    logger.info("步骤2/5: 增量入库")
    ingest.main([])

    # 3. 提取意图（如果 LLM 配置了）
    logger.info("步骤3/5: 提取意图")
    client = LLMClient()
    if client.is_available():
        from profile.cli import extract_intents
        extract_intents.main([])
    else:
        logger.warning("LLM 未配置，跳过意图提取")

    # 4. 生成新画像（同时输出 md + json）
    logger.info("步骤4/5: 生成画像")
    generate_profiles.main(["--days", str(args.days)])

    # 5. diff：从新生成的 json 对比上次 json
    logger.info("步骤5/5: 生成变化报告")
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
            source_changes = diff_profiles(old, new, profile_type=source)
            logger.info("Diff 完成", extra={
                "extra": {
                    "source": source,
                    "changes_count": len(source_changes),
                    "change_types": [c.get("change_type") for c in source_changes],
                }
            })
            changes.extend(source_changes)
    if old_global and new_global:
        global_changes = diff_profiles(old_global, new_global, profile_type="global")
        logger.info("Diff 完成（综合画像）", extra={
            "extra": {
                "source": "global",
                "changes_count": len(global_changes),
                "change_types": [c.get("change_type") for c in global_changes],
            }
        })
        changes.extend(global_changes)

    if changes:
        report_path = write_change_report(changes)
        logger.info("变化报告已生成", extra={
            "extra": {
                "total_changes": len(changes),
                "report_path": str(report_path) if report_path else None,
                "change_summary": [
                    {"type": c.get("change_type"), "field": c.get("field_name"), "summary": c.get("change_summary", "")[:80]}
                    for c in changes[:10]
                ],
            }
        })
    else:
        logger.info("无显著变化")

    elapsed_ms = round((time.monotonic() - t_start) * 1000)
    logger.info("刷新流水线完成", extra={
        "extra": {
            "elapsed_ms": elapsed_ms,
            "total_changes": len(changes),
            "old_channels": list(old_profiles.keys()),
            "new_channels": list(new_profiles.keys()),
        }
    })
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
