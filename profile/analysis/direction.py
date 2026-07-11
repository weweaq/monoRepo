"""方向真实度分析。

提供两个入口：
- analyze(records, claimed): 规则版，按关键词统计。
- analyze_llm(intents, claimed, client): LLM 版，语义理解后判断方向漂移。
"""

import json
from collections import Counter

from profile.config import LLM_FALLBACK_TO_RULES
from profile.llm.client import LLMClient
from profile.llm.prompts import TRAE_PROFILE_PROMPT, TRAE_PROFILE_SYSTEM
from profile.log import get_logger
from profile.models import ChatRecord

logger = get_logger("analysis.direction")

AGENT_KEYWORDS = ["agent", "智能体", "mcp", "langgraph", "multi-agent", "tool", "function calling"]
MEMORY_KEYWORDS = ["memory", "记忆", "vector", "embedding", "rag", "knowledge graph"]


def analyze(records: list[ChatRecord], claimed: dict) -> dict:
    """规则版方向分析。"""
    if len(records) < 1:
        return {"status": "样本不足", "count": len(records)}

    combined = " ".join(r.content.lower() for r in records)
    agent_count = sum(combined.count(kw.lower()) for kw in AGENT_KEYWORDS)
    memory_count = sum(combined.count(kw.lower()) for kw in MEMORY_KEYWORDS)
    total = len(records)

    agent_pct = round(agent_count / total * 100, 1) if total > 0 else 0
    memory_pct = round(memory_count / total * 100, 1) if total > 0 else 0
    other_pct = round(100 - agent_pct - memory_pct, 1)

    if agent_pct > 20:
        drift = "低漂移"
    elif agent_pct > 10:
        drift = "中漂移"
    else:
        drift = "高漂移"

    words = _tokenize(records)
    top_words = Counter(words).most_common(10)

    return {
        "status": "ok",
        "声称方向": claimed,
        "实际TOP10主题": [(w, c) for w, c in top_words],
        "方向提及占比": {"Agent相关": agent_pct, "Memory相关": memory_pct, "其他": other_pct},
        "漂移度判断": f"{drift}（Agent方向占比{agent_pct}%）",
    }


def analyze_llm(intents: list[dict], claimed: dict, client: LLMClient | None = None) -> dict:
    """LLM 版方向分析。"""
    client = client or LLMClient()
    if not client.is_available():
        if LLM_FALLBACK_TO_RULES:
            logger.warning("LLM 不可用，方向分析回退到规则版")
            return analyze(_intents_to_records(intents), claimed)
        raise RuntimeError("LLM 不可用且未开启降级")

    prompt = TRAE_PROFILE_PROMPT.format(
        intents_json=json.dumps(intents, ensure_ascii=False, indent=2),
        primary_direction=claimed.get("主", "Agent"),
        secondary_direction=claimed.get("次", "Memory"),
    )
    result = client.chat_json(prompt, system_prompt=TRAE_PROFILE_SYSTEM)
    return {
        "status": "ok",
        "claimed_direction": claimed,
        "actual_top_topics": result.get("direction_analysis", {}).get("actual_top_topics", []),
        "direction_alignment": result.get("direction_analysis", {}).get("direction_alignment", "未知"),
        "drift_description": result.get("direction_analysis", {}).get("drift_description", ""),
        "agent_ratio": result.get("direction_analysis", {}).get("agent_ratio", "0"),
        "memory_ratio": result.get("direction_analysis", {}).get("memory_ratio", "0"),
        "key_findings": result.get("key_findings", []),
        "raw": result,
    }


def _tokenize(records: list[ChatRecord]) -> list[str]:
    import re

    stopwords = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也",
                 "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
                 "它", "们", "那", "些", "什么", "怎么", "如何", "这个", "那个", "可以", "还是", "但是", "让",
                 "用", "做", "能", "想", "知道", "觉得", "应该", "需要"}
    words = []
    for r in records:
        for w in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", r.content):
            if len(w) > 1 and w.lower() not in stopwords:
                words.append(w.lower())
    return words


def _intents_to_records(intents: list[dict]) -> list[ChatRecord]:
    from datetime import datetime

    records = []
    for i in intents:
        ts = i.get("raw_timestamp", i.get("extracted_at", "2026-01-01 00:00:00"))
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, OSError):
            t = datetime.now()
        records.append(ChatRecord(time=t, content=i.get("raw_content", i.get("intent_summary", "")), source=i.get("source", "")))
    return records
