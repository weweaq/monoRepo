### Task 4: 活跃时段分析 + 测试

**Files:**
- Create: `profile/analysis/activity.py`
- Create: `tests/test_activity.py`

**Interfaces:**
- Produces: `analyze(records: list[ChatRecord]) -> dict`

Follow these steps exactly. Use TDD: write the failing test first, run it to see it fail, then implement to make it pass.

- [ ] **Step 1: 写失败的测试**

```python
from datetime import datetime
from profile.analysis.activity import analyze
from profile.models import ChatRecord


def test_analyze_empty_returns_status():
    result = analyze([])
    assert result["status"] == "样本不足"


def test_analyze_returns_hourly_distribution():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 22, 0), content="test", source="trae"),
        ChatRecord(time=datetime(2026, 7, 1, 22, 30), content="test", source="trae"),
        ChatRecord(time=datetime(2026, 7, 1, 23, 0), content="test", source="trae"),
        ChatRecord(time=datetime(2026, 7, 1, 8, 0), content="test", source="trae"),
    ]
    result = analyze(records)
    assert 22 in result["24小时分布"]
    assert 23 in result["24小时分布"]
    assert 8 in result["24小时分布"]
    assert result["24小时分布"][22] == 2
    assert result["24小时分布"][23] == 1


def test_analyze_finds_peak_hours():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, h, 0), content="test", source="trae")
        for h in [22, 22, 23, 23, 23, 8, 9]
    ]
    result = analyze(records)
    assert result["峰值时段"] == "22:00-00:00"


def test_analyze_returns_daily_average():
    records = [
        ChatRecord(time=datetime(2026, 7, i, 12, 0), content="test", source="trae")
        for i in range(1, 6)
    ]
    result = analyze(records)
    assert result["日均活跃次数"] == 1.0


def test_single_source_traces_through():
    records = [
        ChatRecord(time=datetime(2026, 7, 1, 14, 0), content="test", source="trae"),
        ChatRecord(time=datetime(2026, 7, 1, 14, 30), content="test", source="trae"),
    ]
    result = analyze(records)
    assert "source" in result
    sources = result["source"]
    assert "trae" in sources
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_activity.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'profile.analysis.activity'`

- [ ] **Step 3: 实现 activity.py**

```python
from collections import Counter
from datetime import datetime

from profile.models import ChatRecord


MIN_SAMPLE_SIZE = 3


def analyze(records: list[ChatRecord]) -> dict:
    if len(records) < MIN_SAMPLE_SIZE:
        return {"status": "样本不足", "count": len(records)}

    hours = Counter(r.time.hour for r in records)

    distribution = {h: 0 for h in range(24)}
    distribution.update(dict(hours))

    peak_start, peak_count = _find_best_window(hours)
    trough_start, trough_count = _find_worst_window(hours)

    days = _count_days(records)
    daily_avg = len(records) / max(days, 1)

    sources = Counter(r.source for r in records)

    return {
        "status": "ok",
        "24小时分布": distribution,
        "峰值时段": f"{peak_start:02d}:00-{(peak_start+3)%24:02d}:00",
        "低谷时段": f"{trough_start:02d}:00-{(trough_start+3)%24:02d}:00",
        "日均活跃次数": round(daily_avg, 1),
        "总记录数": len(records),
        "天数": days,
        "source": dict(sources),
    }


def _find_best_window(hours: Counter) -> tuple[int, int]:
    best_start = 0
    best_count = 0
    for h in range(24):
        total = sum(hours.get((h + i) % 24, 0) for i in range(3))
        if total > best_count:
            best_count = total
            best_start = h
    return best_start, best_count


def _find_worst_window(hours: Counter) -> tuple[int, int]:
    worst_start = 0
    worst_count = float("inf")
    for h in range(24):
        total = sum(hours.get((h + i) % 24, 0) for i in range(3))
        if total < worst_count:
            worst_count = total
            worst_start = h
    return worst_start, worst_count


def _count_days(records: list[ChatRecord]) -> int:
    dates = {r.time.date() for r in records}
    return len(dates)
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_activity.py -v
```
Expected: 5 PASSED

- [ ] **Step 5: Commit**

```bash
git add profile/analysis/activity.py tests/test_activity.py
git commit -m "feat: add activity analysis with tests"
```
