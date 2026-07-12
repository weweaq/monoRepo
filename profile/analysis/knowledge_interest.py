"""知识兴趣光谱（内容消费画像维度之一）。

数据来源：bilibili 观看历史（raw_data.source='bilibili'）。
判断逻辑：用 bilibili_reader.classify 对标题做本地关键词分类，统计兴趣大类分布。

降级：无 LLM 语义归纳，仅关键词粗分；分类映射集中在 bilibili_reader.classify。
"""

from collections import Counter

from profile.db.store import query_raw_data
from profile.io.bilibili_reader import classify
from profile.log import get_logger
from profile.models import ChatRecord

logger = get_logger("analysis.knowledge_interest")


def from_db() -> dict:
    rows = query_raw_data(source="bilibili")
    logger.info("知识兴趣分析（bilibili）", extra={"extra": {"raw_count": len(rows)}})
    if not rows:
        return {"status": "无 bilibili 数据", "count": 0}
    cats = Counter()
    untitled = 0
    for r in rows:
        rj = r.get("raw_json") or {}
        title = rj.get("title") or r.get("content") or ""
        if not title:
            untitled += 1
            continue
        cats[classify(title)] += 1
    total = sum(cats.values())
    dist = {k: round(v / total * 100, 1) for k, v in cats.most_common()}
    return {
        "status": "ok",
        "样本量": total,
        "无标题条数": untitled,
        "兴趣分类分布": dist,
        "TOP兴趣": cats.most_common(5),
    }


def analyze(records: list[ChatRecord]) -> dict:
    logger.info("知识兴趣分析（规则版）", extra={"extra": {"records_count": len(records)}})
    if not records:
        return {"status": "样本不足", "count": 0}
    cats = Counter(classify(r.content) for r in records if r.content)
    total = sum(cats.values())
    dist = {k: round(v / total * 100, 1) for k, v in cats.most_common()}
    return {
        "status": "ok",
        "样本量": total,
        "兴趣分类分布": dist,
        "TOP兴趣": cats.most_common(5),
    }
