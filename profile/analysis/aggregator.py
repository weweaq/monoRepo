"""综合画像融合器。

把各 channel 的画像聚合为一份综合画像，直接返回结果不写库。
"""

import json

from profile.config import CLAIMED_DIRECTION, LLM_FALLBACK_TO_RULES
from profile.llm.client import LLMClient
from profile.llm.prompts import GLOBAL_PROFILE_PROMPT, GLOBAL_PROFILE_SYSTEM
from profile.log import get_logger

logger = get_logger("analysis.aggregator")


def build(channel_profiles: dict, period_start: str, period_end: str, client: LLMClient | None = None) -> dict:
    """融合各 channel 画像为综合画像，返回 dict（不写库）。"""
    client = client or LLMClient()

    logger.info("开始融合综合画像", extra={
        "extra": {
            "channels": list(channel_profiles.keys()),
            "channel_count": len(channel_profiles),
            "period": f"{period_start} ~ {period_end}",
            "llm_available": client.is_available(),
            "fallback_enabled": LLM_FALLBACK_TO_RULES,
        }
    })

    if client.is_available():
        result = _build_llm(channel_profiles, client)
        mode = "llm"
    elif LLM_FALLBACK_TO_RULES:
        logger.warning("LLM 不可用，综合画像回退到规则拼接")
        result = _build_rule(channel_profiles)
        mode = "rule"
    else:
        raise RuntimeError("LLM 不可用且未开启降级")

    logger.info("综合画像生成完成", extra={
        "extra": {
            "mode": mode,
            "result_keys": list(result.keys()) if isinstance(result, dict) else None,
        }
    })
    return result


def _build_llm(channel_profiles: dict, client: LLMClient) -> dict:
    prompt = GLOBAL_PROFILE_PROMPT.format(
        channels_json=json.dumps(channel_profiles, ensure_ascii=False, indent=2),
        primary_direction=CLAIMED_DIRECTION.get("主", "Agent"),
        secondary_direction=CLAIMED_DIRECTION.get("次", "Memory"),
    )
    return client.chat_json(prompt, system_prompt=GLOBAL_PROFILE_SYSTEM)


def _build_rule(channel_profiles: dict) -> dict:
    summaries = []
    for source, profile in channel_profiles.items():
        summary = profile.get("summary") or profile.get("漂移度判断") or str(profile)[:60]
        summaries.append(f"- {source}: {summary}")

    result = {
        "direction_truth": {"description": "当前未启用 LLM，方向真实度待语义融合后评估。"},
        "knowledge_interest": {"description": "当前未启用 LLM，兴趣光谱待语义融合后评估。"},
        "activity_pattern": _extract_activity(channel_profiles),
        "decision_style": {"description": "当前未启用 LLM，决策模式待语义融合后评估。"},
        "emotional_tendency": {"description": "当前未启用 LLM，情绪倾向待语义融合后评估。"},
        "summary": "\n".join(summaries),
        "suggestions": ["配置 LLM 后重新生成可获得更准确的综合画像。"],
    }

    content = channel_profiles.get("content_consumption")
    if content:
        result["knowledge_interest"] = content.get("knowledge_interest", {})
        result["emotion_aesthetic"] = content.get("emotion_aesthetic", {})
    return result


def _extract_activity(channel_profiles: dict) -> dict:
    for source, profile in channel_profiles.items():
        if "24小时分布" in profile:
            return {
                "peak_hours": profile.get("峰值时段", "未知"),
                "low_hours": profile.get("低谷时段", "未知"),
                "daily_avg": str(profile.get("日均活跃次数", "未知")),
            }
    return {"peak_hours": "未知", "low_hours": "未知", "daily_avg": "未知"}
