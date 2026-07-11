"""主题与项目分析。

- analyze(records): 规则版，按内容关键词和长度分类。
- analyze_llm(intents, client): LLM 版，语义理解后归纳主题和项目。
"""

import json
from collections import Counter
from datetime import datetime

from profile.config import LLM_FALLBACK_TO_RULES
from profile.llm.client import LLMClient
from profile.llm.prompts import MARVIS_PROFILE_PROMPT, MARVIS_PROFILE_SYSTEM
from profile.log import get_logger
from profile.models import ChatRecord

logger = get_logger("analysis.topic")

TECH_KEYWORDS = ["python", "代码", "报错", "api", "数据库", "前端", "后端", "模型", "llm", "agent", "docker"]
TOOL_KEYWORDS = ["obsidian", "飞书", "notion", "cursor", "trae", "vscode", "github", "git", "cli"]
LIFE_KEYWORDS = ["生活", "健康", "日程", "提醒", "计划", "家庭", "旅行"]


def analyze(records: list[ChatRecord]) -> dict:
    if len(records) < 1:
        return {"status": "样本不足", "count": len(records)}

    combined = " ".join(r.content.lower() for r in records)
    tech = sum(combined.count(kw.lower()) for kw in TECH_KEYWORDS)
    tool = sum(combined.count(kw.lower()) for kw in TOOL_KEYWORDS)
    life = sum(combined.count(kw.lower()) for kw in LIFE_KEYWORDS)
    total = len(records)

    words = _tokenize(records)
    top_words = Counter(words).most_common(10)

    return {
        "status": "ok",
        "主题分布": {
            "技术问题": round(tech / total * 100, 1),
            "工具使用": round(tool / total * 100, 1),
            "生活诉求": round(life / total * 100, 1),
            "其他": round((total - tech - tool - life) / total * 100, 1),
        },
        "TOP10关键词": [(w, c) for w, c in top_words],
        "讨论深度": _depth(records),
    }


def analyze_llm(intents: list[dict], client: LLMClient | None = None) -> dict:
    client = client or LLMClient()
    if not client.is_available():
        if LLM_FALLBACK_TO_RULES:
            logger.warning("LLM 不可用，主题分析回退到规则版")
            return analyze(_intents_to_records(intents))
        raise RuntimeError("LLM 不可用且未开启降级")

    prompt = MARVIS_PROFILE_PROMPT.format(
        intents_json=json.dumps(intents, ensure_ascii=False, indent=2),
    )
    result = client.chat_json(prompt, system_prompt=MARVIS_PROFILE_SYSTEM)
    daily = result.get("daily_needs", {})
    interaction = result.get("interaction_pattern", {})
    return {
        "status": "ok",
        "主题分布": daily.get("need_categories", {}),
        "主要诉求": daily.get("top_needs", []),
        "使用场景": interaction.get("primary_use_case", ""),
        "复杂程度": interaction.get("complexity_level", "未知"),
        "关键发现": result.get("key_findings", []),
        "raw": result,
    }


def _tokenize(records: list[ChatRecord]) -> list[str]:
    import re

    stopwords = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也",
                 "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
                 "它", "们", "那", "些", "什么", "怎么", "如何", "这个", "那个", "可以", "还是", "但是", "让",
                 "用", "做", "能", "想", "知道", "觉得", "应该", "需要", "怎么", "吗"}
    words = []
    for r in records:
        for w in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", r.content):
            if len(w) > 1 and w.lower() not in stopwords:
                words.append(w.lower())
    return words


def _depth(records: list[ChatRecord]) -> str:
    lengths = [len(r.content) for r in records]
    avg = sum(lengths) / len(lengths) if lengths else 0
    if avg > 150:
        return "深度讨论"
    elif avg > 60:
        return "中等复杂"
    return "简单查询"


def _intents_to_records(intents: list[dict]) -> list[ChatRecord]:
    records = []
    for i in intents:
        ts = i.get("raw_timestamp", i.get("extracted_at", "2026-01-01 00:00:00"))
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, OSError):
            t = datetime.now()
        records.append(ChatRecord(time=t, content=i.get("raw_content", i.get("intent_summary", "")), source=i.get("source", "")))
    return records
