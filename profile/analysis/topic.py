import re
from collections import Counter

from profile.models import ChatRecord


TECH_KEYWORDS = ["报错", "错误", "部署", "配置", "代码", "api", "数据库", "python", "bug",
                 "安装", "运行", "编译", "调试", "接口", "服务", "内存", "性能"]
TOOL_KEYWORDS = ["怎么用", "如何使用", "设置", "快捷键", "插件", "工具", "命令", "配置项"]
LIFE_KEYWORDS = ["天气", "吃什么", "作息", "时间", "提醒", "购物", "日程"]

MIN_SAMPLE_SIZE = 1
STOPWORDS = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
             "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
             "没有", "看", "好", "自己", "这", "这个", "那个", "什么", "怎么",
             "如何", "可以", "还是", "但是", "让", "用", "做", "能", "想",
             "知道", "觉得", "应该", "需要", "吗", "呢", "吧", "啊"}


def analyze(records: list[ChatRecord]) -> dict:
    if len(records) < MIN_SAMPLE_SIZE:
        return {"status": "样本不足", "count": len(records)}

    words = []
    for r in records:
        for w in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]+", r.content):
            if w.lower() not in STOPWORDS and len(w) >= 2:
                words.append(w.lower())

    word_counts = Counter(words)
    top10 = word_counts.most_common(10)

    tech = tool = life = 0
    combined = " ".join(words)
    for kw in TECH_KEYWORDS:
        tech += combined.count(kw)
    for kw in TOOL_KEYWORDS:
        tool += combined.count(kw)
    for kw in LIFE_KEYWORDS:
        life += combined.count(kw)

    total = tech + tool + life
    if total == 0:
        return {
            "status": "ok",
            "主要诉求TOP10": [(w, c) for w, c in top10],
            "诉求分类": {"其他": 100},
        }

    return {
        "status": "ok",
        "主要诉求TOP10": [(w, c) for w, c in top10],
        "诉求分类": {
            "技术问题": round(tech / total * 100, 1),
            "工具使用": round(tool / total * 100, 1),
            "生活诉求": round(life / total * 100, 1),
            "其他": round((total - tech - tool - life) / total * 100, 1),
        },
    }
