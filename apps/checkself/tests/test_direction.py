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
