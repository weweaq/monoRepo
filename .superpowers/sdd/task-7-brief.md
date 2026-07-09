### Task 7: 诉求主题分析 + 测试

**Files:**
- Create: `profile/analysis/topic.py`
- Create: `tests/test_topic.py`

**Interfaces:**
- Produces: `analyze(records: list[ChatRecord]) -> dict`

TDD. Code in step 3 is authoritative. Fix bugs that cause test failures.

- [ ] **Step 1: Write failing tests**

```python
from datetime import datetime
from profile.analysis.topic import analyze
from profile.models import ChatRecord


def test_analyze_empty_returns_status():
    result = analyze([])
    assert result["status"] == "样本不足"


def test_analyze_returns_top_words():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 12, 0), content="DeepFace部署遇到问题", source="marvis"),
        ChatRecord(time=datetime(2026, 7, 1, 13, 0), content="DeepFace模型转换报错", source="marvis"),
        ChatRecord(time=datetime(2026, 7, 1, 14, 0), content="数据库查询优化", source="marvis"),
    ]
    result = analyze(records)
    assert result["status"] == "ok"
    assert len(result["主要诉求TOP10"]) > 0


def test_analyze_categorizes():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 12, 0), content="python报错怎么解决", source="marvis"),
        ChatRecord(time=datetime(2026, 7, 1, 13, 0), content="这个工具怎么用", source="marvis"),
    ]
    result = analyze(records)
    assert "诉求分类" in result
    assert "技术问题" in result["诉求分类"]
```

- [ ] **Step 2: Run tests, expect FAIL**

```powershell
python -m pytest tests/test_topic.py -v
```

- [ ] **Step 3: Implement topic.py**

```python
import re
from collections import Counter

from profile.models import ChatRecord


TECH_KEYWORDS = ["报错", "错误", "部署", "配置", "代码", "api", "数据库", "python", "bug",
                 "安装", "运行", "编译", "调试", "接口", "服务", "内存", "性能"]
TOOL_KEYWORDS = ["怎么用", "如何使用", "设置", "快捷键", "插件", "工具", "命令", "配置项"]
LIFE_KEYWORDS = ["天气", "吃什么", "作息", "时间", "提醒", "购物", "日程"]

MIN_SAMPLE_SIZE = 5
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
```

- [ ] **Step 4: Run tests, expect 3 PASSED**

```powershell
python -m pytest tests/test_topic.py -v
```

- [ ] **Step 5: Commit**

```bash
git add profile/analysis/topic.py tests/test_topic.py
git commit -m "feat: add topic/demand analysis with tests"
```
