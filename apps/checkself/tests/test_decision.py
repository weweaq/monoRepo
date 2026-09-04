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
