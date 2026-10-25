"""日报断言基线闸（audit）的封闭测试：临时目录语料，不碰真实 memory/ 与 logs/。"""

from __future__ import annotations

from gacore.audit import extract_claims, parse_duration_days


def test_parse_duration_days_basic():
    assert parse_duration_days("半年") == 180
    assert parse_duration_days("两个月") == 60
    assert parse_duration_days("三周") == 21
    assert parse_duration_days("4 日") == 4
    assert parse_duration_days("一年") == 365
    assert parse_duration_days("几周") is None


def test_extract_claims_ignores_quoted_and_clock_text():
    report = (
        "# 工作日志\n"
        "- [工作日志-1] 11:56 城事堂养老口号 40 年，话题首次探到社会民生\n"
        "- [工作日志-2] **十小时朱元璋创业史** 第 3 期收官\n"
        "- [工作日志-3] 《入手二十年前的索尼电视》看完\n"
    )
    claims = extract_claims(report)
    assert claims == []


def test_extract_claims_marks_anchor_and_bare():
    report = (
        "# 关键变化\n"
        "- [关键变化-1] 硬件线半年沉寂后重新点火\n"
        "# 明日计划\n"
        "- [明日计划-1] 代码连续第 3 日无提交（10-09 至 10-11）\n"
    )
    claims = extract_claims(report)
    assert len(claims) == 2
    assert claims[0].claimed_days == 180
    assert claims[0].claim_word == "沉寂"
    assert claims[0].anchored is False
    assert claims[1].anchored is True


def test_extract_claims_old_format_without_headings():
    report = "- 工学椅连续 2 天密集调研，仍未定型号\n"
    claims = extract_claims(report)
    assert len(claims) == 1
    assert claims[0].section == ""
    assert claims[0].anchor is None
    assert claims[0].anchored is False
