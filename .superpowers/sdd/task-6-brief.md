### Task 6: 决策模式分析 + 测试

**Files:**
- Create: `profile/analysis/decision.py`
- Create: `tests/test_decision.py`

**Interfaces:**
- Produces: `analyze(records: list[ChatRecord]) -> dict`

TDD. The code in step 3 is authoritative — transcribe it and fix any bugs that cause tests to fail.

- [ ] **Step 1: Write failing tests**

```python
from datetime import datetime
from profile.analysis.decision import analyze
from profile.models import ChatRecord


def test_analyze_empty_returns_status():
    result = analyze([])
    assert result["status"] == "样本不足"


def test_analyze_classifies_actions():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 12, 0), content="实现某个功能",
                   actions=["搜索文档", "写代码", "运行测试"], source="trae"),
        ChatRecord(time=datetime(2026, 7, 1, 14, 0), content="研究新框架",
                   actions=["阅读官网", "查看示例"], source="trae"),
    ]
    result = analyze(records)
    assert result["status"] == "ok"
    assert "调研类" in result["处理路径分布"]
    assert "动手类" in result["处理路径分布"]


def test_analyze_detects_pattern():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 12, 0), content="test",
                   actions=["搜索文档", "搜索API"], source="trae"),
    ]
    result = analyze(records)
    assert result["模式判断"] == "先调研后动手"


def test_analyze_calculates_idea_to_action_gap():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 12, 0), content="想做个画像系统",
                   actions=["搜索"], source="trae"),
        ChatRecord(time=datetime(2026, 7, 3, 12, 0), content="画像系统",
                   actions=["写代码"], source="trae"),
    ]
    result = analyze(records)
    assert result["想法到动手间隔"] is not None
```

- [ ] **Step 2: Run tests, expect FAIL**

```powershell
python -m pytest tests/test_decision.py -v
```

- [ ] **Step 3: Implement decision.py**

```python
from datetime import datetime

from profile.models import ChatRecord


RESEARCH_KEYWORDS = ["搜索", "查找", "调研", "了解", "阅读", "查看", "研究", "浏览", "查询", "检索"]
BUILD_KEYWORDS = ["写", "创建", "实现", "安装", "运行", "修改", "部署", "构建", "开发", "编码", "配置", "搭建"]
DISCUSS_KEYWORDS = ["讨论", "分析", "确认", "询问", "审查", "评估", "规划"]

MIN_SAMPLE_SIZE = 5


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
    idea_dates: dict[str, datetime] = {}
    action_dates: dict[str, datetime] = {}

    for r in records:
        text = r.content
        has_build_action = any(
            any(kw in a for kw in BUILD_KEYWORDS) for a in r.actions
        )

        for kw in BUILD_KEYWORDS:
            if kw in text.lower():
                idea_dates.setdefault(text[:20], r.time)

        if has_build_action:
            action_dates.setdefault(text[:20], r.time)

    gaps = []
    for key in idea_dates:
        if key in action_dates:
            gap_days = (action_dates[key] - idea_dates[key]).days
            gaps.append(gap_days)

    if not gaps:
        return None

    avg = sum(gaps) / len(gaps)
    return f"约 {avg:.0f} 天（基于{len(gaps)}个样本）"
```

- [ ] **Step 4: Run tests, expect 4 PASSED**

```powershell
python -m pytest tests/test_decision.py -v
```

- [ ] **Step 5: Commit**

```bash
git add profile/analysis/decision.py tests/test_decision.py
git commit -m "feat: add decision pattern analysis with tests"
```
