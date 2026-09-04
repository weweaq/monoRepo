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
