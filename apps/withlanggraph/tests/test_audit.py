"""日报断言基线闸（audit）的封闭测试：临时目录语料，不碰真实 memory/ 与 logs/。"""

from __future__ import annotations

from gacore.audit import (
    audit_report,
    extract_claims,
    find_topic,
    is_conflict,
    last_seen,
    parse_duration_days,
)
from gacore.config import Config


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


# ---------------------------------------------------------------- Task 2: 主题与 last_seen


def test_find_topic_maps_synonyms():
    assert find_topic("硬件线半年沉寂后重新点火") == "硬件"
    assert find_topic("工学椅调研时隔两个月重启") == "工学椅"
    assert find_topic("今天吃了碗面") is None


def test_last_seen_picks_latest_before_date(tmp_path):
    cfg = Config.for_tests(tmp_path)
    daily = cfg.memory_dir / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-10-03.md").write_text("晚上追番刷硬件，拆了台旧电视", encoding="utf-8")
    (daily / "2026-09-20.md").write_text("复古硬件首次入镜", encoding="utf-8")
    got = last_seen(cfg, "硬件", "2026-10-20")
    assert got.found is True
    assert got.date == "2026-10-03"
    assert got.gap_days == 17
    assert got.source.startswith("memory/daily/")


def test_last_seen_returns_not_found_when_older_than_corpus(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)
    (cfg.memory_dir / "daily" / "2026-10-01.md").write_text("硬件", encoding="utf-8")
    got = last_seen(cfg, "硬件", "2026-09-30")
    assert got.found is False
    assert got.gap_days is None


def test_last_seen_handles_duplicate_rows(tmp_path):
    cfg = Config.for_tests(tmp_path)
    daily = cfg.memory_dir / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-10-03.md").write_text("硬件 A", encoding="utf-8")
    (cfg.memory_dir / "global_mem_insight.txt").write_text(
        "[2026-10-03] insight: 硬件 B\n", encoding="utf-8"
    )
    assert last_seen(cfg, "硬件", "2026-10-20").date == "2026-10-03"


# ------------------------------------------------------------ Task 3: 冲突判定与单篇审计


def test_is_conflict_tolerance_band():
    assert is_conflict(180, 17) is True
    assert is_conflict(60, 27) is True
    assert is_conflict(14, 13) is False
    assert is_conflict(20, 19) is False


def test_audit_report_flags_hard_conflict(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)
    (cfg.memory_dir / "daily" / "2026-10-03.md").write_text("硬件信号", encoding="utf-8")
    reports = cfg.logs_dir / "delivered_report"
    reports.mkdir(parents=True)
    (reports / "2026-10-20.md").write_text(
        "# 关键变化\n- [关键变化-1] 硬件线半年沉寂后重新点火\n", encoding="utf-8"
    )
    got = audit_report(cfg, "2026-10-20")
    assert got.score == 75
    assert len(got.findings) == 1
    assert got.findings[0].rule == "R2"
    assert got.findings[0].severity == "error"
    assert got.findings[0].actual_last_seen == "2026-10-03"


def test_audit_report_missing_file(tmp_path):
    cfg = Config.for_tests(tmp_path)
    got = audit_report(cfg, "2026-01-01")
    assert got.missing is True
    assert got.findings == ()
