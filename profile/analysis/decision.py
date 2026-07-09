from datetime import datetime

from profile.models import ChatRecord


RESEARCH_KEYWORDS = ["搜索", "查找", "调研", "了解", "阅读", "查看", "研究", "浏览", "查询", "检索"]
BUILD_KEYWORDS = ["写", "创建", "实现", "安装", "运行", "修改", "部署", "构建", "开发", "编码", "配置", "搭建"]
DISCUSS_KEYWORDS = ["讨论", "分析", "确认", "询问", "审查", "评估", "规划"]

MIN_SAMPLE_SIZE = 1


def analyze(records: list[ChatRecord]) -> dict:
    actionable = [r for r in records if r.actions]
    if len(actionable) < MIN_SAMPLE_SIZE:
        return {"status": "样本不足", "count": len(actionable)}

    research = 0
    build = 0
    discuss = 0

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
        return {"status": "样本不足", "count": len(actionable), "reason": "无匹配的action分类"}

    research_pct = round(research / total * 100, 1)
    build_pct = round(build / total * 100, 1)
    discuss_pct = round(discuss / total * 100, 1)

    if research_pct > build_pct * 1.5:
        pattern = "先调研后动手"
    elif build_pct > research_pct * 1.5:
        pattern = "先动手后查"
    else:
        pattern = "混合"

    gap = _idea_to_action_gap(records)

    return {
        "status": "ok",
        "处理路径分布": {
            "调研类": research_pct,
            "动手类": build_pct,
            "讨论类": discuss_pct,
        },
        "模式判断": pattern,
        "想法到动手间隔": gap,
    }


def _idea_to_action_gap(records: list[ChatRecord]) -> str | None:
    idea_records: list[ChatRecord] = []
    action_records: list[ChatRecord] = []

    for r in records:
        has_build_action = any(
            any(kw in a.lower() for kw in BUILD_KEYWORDS) for a in r.actions
        )
        if has_build_action:
            action_records.append(r)
        elif r.actions:
            idea_records.append(r)

    gaps = []
    for idea_r in idea_records:
        for action_r in action_records:
            if action_r.content and action_r.content in idea_r.content and action_r.time > idea_r.time:
                gap_days = (action_r.time - idea_r.time).days
                gaps.append(gap_days)

    if not gaps:
        return None

    avg = sum(gaps) / len(gaps)
    return f"约 {avg:.0f} 天（基于{len(gaps)}个样本）"
