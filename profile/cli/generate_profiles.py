"""画像生成入口。

用法：
    python -m profile.cli.generate_profiles
    python -m profile.cli.generate_profiles --days 7
"""

import argparse
from datetime import datetime, timedelta

from profile.analysis import activity, aggregator, decision, direction, topic
from profile.config import CLAIMED_DIRECTION, LOG_DIR
from profile.db.init_db import init_db
from profile.db.store import query_intents, query_raw_data
from profile.io.registry import get_reader, channel_reader_names, consumption_reader_names
from profile.llm.client import LLMClient
from profile.log import get_logger, setup, log_error
from profile.models import ChatRecord
from profile.output.writer import write_channel, write_global

logger = get_logger("cli.generate_profiles")


def _period(args) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=args.days)
    return start.date().isoformat(), end.date().isoformat()


def _raw_to_records(raw_rows: list[dict]) -> list[ChatRecord]:
    import json as _json

    records = []
    for row in raw_rows:
        ts = row.get("timestamp", "2026-01-01 00:00:00")
        try:
            t = datetime.fromisoformat(str(ts).replace(" ", "T").replace("Z", "+00:00"))
        except (ValueError, OSError):
            t = datetime.now()

        actions = row.get("actions")
        if isinstance(actions, str):
            try:
                actions = _json.loads(actions)
            except _json.JSONDecodeError:
                actions = []

        records.append(ChatRecord(
            time=t,
            content=row.get("content", ""),
            actions=actions or [],
            outcome=row.get("outcome", ""),
            learned=row.get("learned") or [],
            source=row.get("source", ""),
        ))
    return records


def _intents_to_records(intents: list[dict]) -> list[ChatRecord]:
    records = []
    for i in intents:
        ts = i.get("raw_timestamp", i.get("extracted_at", "2026-01-01 00:00:00"))
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, OSError):
            t = datetime.now()
        records.append(ChatRecord(
            time=t,
            content=i.get("raw_content", i.get("intent_summary", "")),
            source=i.get("source", ""),
        ))
    return records


def generate_channel(source: str, intents: list[dict], raw_rows: list[dict],
                     client: LLMClient, period_start: str, period_end: str) -> dict:
    use_llm = bool(intents) and client.is_available()
    records = _intents_to_records(intents) if use_llm else _raw_to_records(raw_rows)

    analysis_type = "topic"
    reader = get_reader(source)
    if reader is not None:
        analysis_type = reader.analysis_type

    logger.info("开始 channel 画像生成", extra={
        "extra": {
            "source": source,
            "analysis_type": analysis_type,
            "mode": "llm" if use_llm else "rule",
            "intents_count": len(intents),
            "raw_rows_count": len(raw_rows),
            "records_count": len(records),
            "period": f"{period_start} ~ {period_end}",
        }
    })

    if not use_llm and not intents:
        logger.warning("无意图数据，回退到规则分析", extra={
            "extra": {"source": source}
        })

    if analysis_type == "agentic":
        if use_llm:
            direction_result = direction.analyze_llm(intents, CLAIMED_DIRECTION, client=client)
            decision_result = decision.analyze_llm(intents, client=client)
        else:
            direction_result = direction.analyze(records, CLAIMED_DIRECTION)
            decision_result = decision.analyze(records)
        activity_result = activity.analyze(records)
        profile = {
            "source": source,
            "period": {"start": period_start, "end": period_end},
            "direction": direction_result,
            "decision": decision_result,
            "activity": activity_result,
        }
    else:
        if use_llm:
            topic_result = topic.analyze_llm(intents, client=client)
        else:
            topic_result = topic.analyze(records)
        activity_result = activity.analyze(records)
        profile = {
            "source": source,
            "period": {"start": period_start, "end": period_end},
            "topic": topic_result,
            "activity": activity_result,
        }

    # 输出 md + json
    md_path, json_path = write_channel(profile, source)
    logger.info("channel 画像生成完成", extra={
        "extra": {
            "source": source,
            "mode": "llm" if use_llm else "rule",
            "md_file": str(md_path),
            "json_file": str(json_path),
            "md_size_bytes": md_path.stat().st_size if md_path.exists() else 0,
            "json_size_bytes": json_path.stat().st_size if json_path.exists() else 0,
            "profile_keys": list(profile.keys()),
        }
    })
    return profile


def build_content_consumption(client: LLMClient, period_start: str, period_end: str) -> dict | None:
    # 遍历所有 consumption 型 reader，按其 consumption_slot 自动接入，不再硬编码 bilibili/netease。
    profile: dict = {
        "source": "content_consumption",
        "period": {"start": period_start, "end": period_end},
    }
    any_ok = False
    for name in consumption_reader_names():
        reader = get_reader(name)
        if reader is None:
            continue
        slot = reader.consumption_slot
        if not slot:
            continue
        try:
            result = reader.analyze_consumption()
        except Exception as e:
            log_error(logger, f"{name} 消费画像分析失败", exc=e, context={})
            continue
        if result and result.get("status") == "ok":
            any_ok = True
        profile[slot] = result

    slots = [k for k in profile if k not in ("source", "period")]
    if not any_ok:
        logger.warning("无内容消费数据，跳过内容消费画像", extra={"extra": {"slots": slots}})
        return None

    logger.info("开始内容消费画像生成", extra={"extra": {"slots": slots}})
    md_path, json_path = write_channel(profile, "content_consumption")
    logger.info("内容消费画像生成完成", extra={
        "extra": {
            "md_file": str(md_path),
            "json_file": str(json_path),
            "md_size_bytes": md_path.stat().st_size if md_path.exists() else 0,
        }
    })
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="统计最近 N 天")
    args = parser.parse_args(argv)

    setup(log_dir=LOG_DIR)
    init_db()
    client = LLMClient()

    period_start, period_end = _period(args)
    logger.info("画像生成启动", extra={
        "extra": {
            "period_start": period_start,
            "period_end": period_end,
            "days": args.days,
            "llm_available": client.is_available(),
            "llm_model": client.model,
        }
    })

    channel_profiles = {}
    for source in channel_reader_names():
        intents = query_intents(source=source, start_date=period_start, end_date=period_end)
        raw_rows = query_raw_data(source=source, start_date=period_start, end_date=period_end)
        logger.info("数据查询完成", extra={
            "extra": {
                "source": source,
                "intents_count": len(intents),
                "raw_rows_count": len(raw_rows),
            }
        })
        if not intents and not raw_rows:
            logger.info("无数据，跳过", extra={"extra": {"source": source}})
            continue
        channel_profiles[source] = generate_channel(source, intents, raw_rows, client, period_start, period_end)

    # 内容消费画像（由各 consumption 型 reader 的 analyze_consumption 自动聚合），直接读 raw_data
    content_profile = build_content_consumption(client, period_start, period_end)
    if content_profile:
        channel_profiles["content_consumption"] = content_profile

    if not channel_profiles:
        logger.warning("无任何 channel 数据，跳过综合画像")
        return 1

    # 生成综合画像
    logger.info("开始生成综合画像", extra={
        "extra": {
            "channels": list(channel_profiles.keys()),
            "channel_count": len(channel_profiles),
        }
    })
    global_profile = aggregator.build(channel_profiles, period_start=period_start, period_end=period_end, client=client)
    md_path, json_path = write_global(global_profile)

    logger.info("画像生成全部完成", extra={
        "extra": {
            "channels_generated": list(channel_profiles.keys()),
            "global_md": str(md_path),
            "global_json": str(json_path),
            "global_md_size": md_path.stat().st_size if md_path.exists() else 0,
            "global_json_size": json_path.stat().st_size if json_path.exists() else 0,
        }
    })
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
