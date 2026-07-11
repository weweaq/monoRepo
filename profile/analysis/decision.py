"""决策行动模式分析。

- analyze(records): 规则版，按 action 关键词分类。
- analyze_llm(intents, client): LLM 版，语义理解后判断决策模式。
"""

import json
from datetime import datetime

from profile.config import LLM_FALLBACK_TO_RULES
from profile.llm.client import LLMClient
from profile.llm.prompts import TRAE_PROFILE_PROMPT, TRAE_PROFILE_SYSTEM
from profile.models import ChatRecord

RESEARCH_KEYWORDS = ["搜索", "查找", "调研", "了解", "阅读", "查看", "研究", "浏览", "查询", "检索"]
BUILD_KEYWORDS = ["写", "创建", "实现", "安装", "运行", "修改", "部署", "构建", "开发", "编码", "配置", "搭建"]
DISCUSS_KEYWORDS = ["讨论", "分析", "确认", "询问", "审查", "评估", "规划"]


def analyze(records: list[ChatRecord]) -> dict:
    actionable = [r for r in records if r.actions]
    if len(actionable) < 1:
        return {"status": "样本不足", "count": len(actionable)}

    research = build = discuss = 0
    for r in actionable:
        for a in r.actions:
            a_lower = a.lower()
            if any(kw in a_lower for kw in RESEARCH_KEYWORDS):
                research += 1
            elif any(kw in a_lower for kw in BUILD_KEYWORDS):
                build += 1
            elif any(kw in a_lower for kw in DISCUSS_KEYWORDS):
                discuss += 1

    total = research + build + discuss
    if total == 0:
        return {"status": "样本不足", "count": len(actionable), "reason": "无匹配的 action 分类"}

    rp = round(research / total * 100, 1)
    bp = round(build / total * 100, 1)
    dp = round(discuss / total * 100, 1)

    if rp > bp * 1.5:
        pattern = "先调研后动手"
    elif bp > rp * 1.5:
        pattern = "先动手后查"
    else:
        pattern = "混合"

    return {
        "status": "ok",
        "处理路径分布": {"调研类": rp, "动手类": bp, "讨论类": dp},
        "模式判断": pattern,
        "想法到动手间隔": _idea_to_action_gap(actionable),
    }


def analyze_llm(intents: list[dict], client: LLMClient | None = None) -> dict:
    client = client or LLMClient()
    if not client.is_available():
        if LLM_FALLBACK_TO_RULES:
            print("[warn] LLM 不可用，决策分析回退到规则版")
            return analyze(_intents_to_records(intents))
        raise RuntimeError("LLM 不可用且未开启降级")

    from profile.config import CLAIMED_DIRECTION

    prompt = TRAE_PROFILE_PROMPT.format(
        intents_json=json.dumps(intents, ensure_ascii=False, indent=2),
        primary_direction=CLAIMED_DIRECTION.get("主", "Agent"),
        secondary_direction=CLAIMED_DIRECTION.get("次", "Memory"),
    )
    result = client.chat_json(prompt, system_prompt=TRAE_PROFILE_SYSTEM)
    decision = result.get("decision_pattern", {})
    return {
        "status": "ok",
        "处理路径分布": {
            "调研类": decision.get("research_ratio", "0"),
            "动手类": decision.get("build_ratio", "0"),
            "讨论类": decision.get("discuss_ratio", "0"),
        },
        "模式判断": decision.get("pattern", "未知"),
        "想法到动手间隔": decision.get("idea_to_action_gap", "暂无"),
        "raw": result,
    }


def _idea_to_action_gap(records: list[ChatRecord]) -> str | None:
    idea_records = []
    action_records = []
    for r in records:
        has_build = any(any(kw in a.lower() for kw in BUILD_KEYWORDS) for a in r.actions)
        if has_build:
            action_records.append(r)
        else:
            idea_records.append(r)

    gaps = []
    for idea_r in idea_records:
        for action_r in action_records:
            if action_r.content and action_r.content in idea_r.content and action_r.time > idea_r.time:
                gaps.append((action_r.time - idea_r.time).days)

    if not gaps:
        return None
    avg = sum(gaps) / len(gaps)
    return f"约 {avg:.0f} 天（基于{len(gaps)}个样本）"


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
