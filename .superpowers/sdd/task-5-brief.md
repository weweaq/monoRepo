### Task 5: 方向漂移检测 + 测试

**Files:**
- Create: `profile/analysis/direction.py`
- Create: `tests/test_direction.py`

**Interfaces:**
- Consumes: `profile.config.CLAIMED_DIRECTION`, `profile.models.ChatRecord`
- Produces: `analyze(records: list[ChatRecord], claimed: dict) -> dict`

Follow TDD exactly. The plan's implementation code below is the target — transcribe it verbatim, but fix any bugs you find that cause tests to fail.

- [ ] **Step 1: 写失败的测试**

```python
from datetime import datetime
from profile.analysis.direction import analyze
from profile.models import ChatRecord


def test_analyze_empty_returns_status():
    result = analyze([], {"主": "Agent"})
    assert result["status"] == "样本不足"


def test_analyze_counts_direction_keywords():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 12, 0), content="agent 实现", source="trae"),
        ChatRecord(time=datetime(2026, 7, 1, 13, 0), content="memory 模块设计", source="trae"),
        ChatRecord(time=datetime(2026, 7, 1, 14, 0), content="今天吃什么", source="trae"),
    ]
    result = analyze(records, {"主": "Agent", "次": "Memory"})
    assert result["status"] == "ok"
    assert result["方向提及占比"]["Agent相关"] > 0
    assert result["方向提及占比"]["Memory相关"] > 0


def test_analyze_weekly_trends():
    records = [
        ChatRecord(time=datetime(2026, 6, 29, 12, 0), content="agent 开发", source="trae"),
        ChatRecord(time=datetime(2026, 7, 6, 12, 0), content="memory 设计", source="trae"),
    ]
    result = analyze(records, {"主": "Agent"})
    assert len(result["每周趋势"]) >= 1


def test_drift_level_high_when_low_match():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 12, 0), content=f"topic{i}", source="trae")
        for i in range(30)
    ]
    result = analyze(records, {"主": "Agent"})
    assert result["漂移度判断"].startswith("高")
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_direction.py -v
```
Expected: FAIL

- [ ] **Step 3: 实现 direction.py**

```python
from collections import Counter
from datetime import datetime
import re

from profile.models import ChatRecord


AGENT_KEYWORDS = ["agent", "智能体", "mcp", "langgraph", "multi-agent", "tool", "function calling"]
MEMORY_KEYWORDS = ["memory", "记忆", "vector", "embedding", "rag", "knowledge graph"]

MIN_SAMPLE_SIZE = 10


def analyze(records: list[ChatRecord], claimed: dict) -> dict:
    if len(records) < MIN_SAMPLE_SIZE:
        return {"status": "样本不足", "count": len(records)}

    combined_text = " ".join(r.content.lower() for r in records)
    words = _tokenize(records)

    agent_count = sum(combined_text.count(kw.lower()) for kw in AGENT_KEYWORDS)
    memory_count = sum(combined_text.count(kw.lower()) for kw in MEMORY_KEYWORDS)
    total_keyword_hits = agent_count + memory_count
    other_count = max(len(records) - total_keyword_hits, 0)

    total = len(records) if total_keyword_hits == 0 else agent_count + memory_count + other_count
    agent_pct = round(agent_count / total * 100, 1) if total > 0 else 0
    memory_pct = round(memory_count / total * 100, 1) if total > 0 else 0

    if agent_pct > 20:
        drift = "低漂移"
    elif agent_pct > 10:
        drift = "中漂移"
    else:
        drift = "高漂移"

    top_words = Counter(words).most_common(10)

    weekly = _weekly_trends(records, claimed)

    return {
        "status": "ok",
        "声称方向": claimed,
        "实际TOP10主题": [(w, c) for w, c in top_words],
        "方向提及占比": {
            "Agent相关": agent_pct,
            "Memory相关": memory_pct,
            "其他": round(100 - agent_pct - memory_pct, 1),
        },
        "每周趋势": weekly,
        "漂移度判断": f"{drift}（Agent方向占比{agent_pct}%）",
    }


def _tokenize(records: list[ChatRecord]) -> list[str]:
    stopwords = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
                 "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
                 "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
                 "什么", "怎么", "如何", "这个", "那个", "可以", "还是", "但是",
                 "让", "用", "做", "能", "想", "知道", "觉得", "应该", "需要"}
    all_words = []
    for r in records:
        for w in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", r.content):
            if len(w) > 1 and w.lower() not in stopwords:
                all_words.append(w.lower())
    return all_words


def _weekly_trends(records: list[ChatRecord], claimed: dict) -> list[dict]:
    weeks: dict[str, dict] = {}
    for r in records:
        week_key = r.time.strftime("%Y-W%W")
        if week_key not in weeks:
            weeks[week_key] = {k: 0 for k in claimed.values()}
            weeks[week_key]["其他"] = 0
        text = r.content.lower()
        hit = False
        for kw in AGENT_KEYWORDS:
            if kw.lower() in text:
                first_key = list(claimed.values())[0] if len(claimed) > 0 else "方向"
                weeks[week_key][first_key] = weeks[week_key].get(first_key, 0) + 1
                hit = True
                break
        if not hit:
            for kw in MEMORY_KEYWORDS:
                if kw.lower() in text:
                    second_key = list(claimed.values())[1] if len(claimed) > 1 else "Memory"
                    weeks[week_key][second_key] = weeks[week_key].get(second_key, 0) + 1
                    hit = True
                    break
        if not hit:
            weeks[week_key]["其他"] += 1
    return [{"周": k, **v} for k, v in sorted(weeks.items())]
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_direction.py -v
```
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add profile/analysis/direction.py tests/test_direction.py
git commit -m "feat: add direction drift analysis with tests"
```
