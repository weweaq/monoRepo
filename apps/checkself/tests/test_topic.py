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
