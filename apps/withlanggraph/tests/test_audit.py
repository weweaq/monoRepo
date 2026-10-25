"""日报断言基线闸（audit）的封闭测试：临时目录语料，不碰真实 memory/ 与 logs/。"""

from __future__ import annotations

import json
from pathlib import Path

from gacore.audit import (
    audit_report,
    backfill,
    extract_claims,
    find_topic,
    is_conflict,
    last_seen,
    parse_duration_days,
    render_baseline,
    write_audit,
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


def test_topic_table_external_config_overrides(tmp_path):
    """决策 28：config/audit_topics.json 存在时整体覆盖内置词表，新主题即改即生效。"""
    cfg = Config.for_tests(tmp_path)
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "audit_topics.json").write_text(
        json.dumps({"相机": ["相机", "镜头", "微单"]}, ensure_ascii=False), encoding="utf-8"
    )
    daily = cfg.memory_dir / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-10-01.md").write_text("入手了一支镜头", encoding="utf-8")
    got = last_seen(cfg, "相机", "2026-10-05")
    assert got.found is True
    assert got.date == "2026-10-01"


def _write_v1(cfg, day: str, report_text: str | None) -> None:
    audit_dir = cfg.logs_dir / "report_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    payload = {"date": day, "version": 1, "score": 100, "missing": False, "findings": []}
    if report_text is not None:
        payload["report_text"] = report_text
    (audit_dir / f"{day}.v1.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_last_seen_reads_v1_report_snapshots(tmp_path):
    """决策 23：历史日报作为证据源时只读 v1 冻结的 report_text。"""
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)  # 记忆三源无命中
    _write_v1(cfg, "2026-10-11", "# 工作日志\n- [工作日志-1] 晚上研究工学椅扶手高度\n")
    _write_v1(cfg, "2026-10-12", "# 今日状态\n- [今日状态-1] 无关内容\n")  # 不含主题
    got = last_seen(cfg, "工学椅", "2026-10-15")
    assert got.found is True
    assert got.date == "2026-10-11"
    assert got.source.startswith("logs/report_audit/2026-10-11.v1.json")


def test_last_seen_skips_snapshot_without_report_text(tmp_path):
    """无 report_text 的快照（含被订正后缺字段的历史产物）不作为证据源。"""
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)
    _write_v1(cfg, "2026-10-11", None)
    got = last_seen(cfg, "工学椅", "2026-10-15")
    assert got.found is False


def test_last_seen_ignores_reports_on_or_after_before_date(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)
    _write_v1(cfg, "2026-10-15", "- [工作日志-1] 工学椅到货\n")
    got = last_seen(cfg, "工学椅", "2026-10-15")
    assert got.found is False


# ------------------------------------------------------- P2 Task 2: last_seen 工具


def test_last_seen_tool_registered():
    from gacore.tools import TOOL_NAMES, build_tool_list

    assert "last_seen" in TOOL_NAMES
    names = [t.name for t in build_tool_list(Config.for_tests(Path(".")))]
    assert "last_seen" in names


def test_last_seen_tool_resolves_via_config_default(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    daily = cfg.memory_dir / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-10-03.md").write_text("硬件信号", encoding="utf-8")
    monkeypatch.setattr(Config, "default", classmethod(lambda cls: cfg))
    from gacore.tools.audit_tools import last_seen as last_seen_tool

    out = last_seen_tool.invoke({"topic": "硬件", "before_date": "2026-10-20"})
    assert out["found"] is True
    assert out["gap_days"] == 17
    assert out["date"] == "2026-10-03"


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


# --------------------------------------------------- Task 4: 版本化落盘与 backfill CLI


def test_write_audit_freezes_v1(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.logs_dir / "delivered_report").mkdir(parents=True)
    (cfg.logs_dir / "delivered_report" / "2026-10-20.md").write_text(
        "# 关键变化\n- [关键变化-1] 硬件线半年沉寂后重新点火\n", encoding="utf-8"
    )
    (cfg.memory_dir / "daily").mkdir(parents=True)
    (cfg.memory_dir / "daily" / "2026-10-03.md").write_text("硬件", encoding="utf-8")
    p1 = write_audit(cfg, audit_report(cfg, "2026-10-20"))
    p2 = write_audit(cfg, audit_report(cfg, "2026-10-20"))
    assert p1.name == "2026-10-20.v1.json"
    assert p2.name == "2026-10-20.v2.json"
    v1 = json.loads(p1.read_text(encoding="utf-8"))
    assert v1["score"] == 75
    # 决策 23：v1 冻结投递原文，last_seen 读历史日报一律走 v1 口径
    assert v1["report_text"] == "# 关键变化\n- [关键变化-1] 硬件线半年沉寂后重新点火\n"


def test_backfill_skips_existing_v1(tmp_path):
    cfg = Config.for_tests(tmp_path)
    reports = cfg.logs_dir / "delivered_report"
    reports.mkdir(parents=True)
    (reports / "2026-10-19.md").write_text("# 今日状态\n- [今日状态-1] 一切正常\n", encoding="utf-8")
    (reports / "2026-10-20.md").write_text("# 关键变化\n- [关键变化-1] 硬件线半年沉寂\n", encoding="utf-8")
    (cfg.memory_dir / "daily").mkdir(parents=True)
    (cfg.memory_dir / "daily" / "2026-10-03.md").write_text("硬件", encoding="utf-8")
    summary = backfill(cfg)
    assert summary["reports"] == 2
    assert summary["conflicts"] == 1
    assert (cfg.logs_dir / "report_audit" / "2026-10-20.v1.json").is_file()


# ------------------------------------------------------ Task 5: 基线报告


def test_render_baseline_lists_conflicts(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.logs_dir / "report_audit").mkdir(parents=True)
    (cfg.logs_dir / "report_audit" / "2026-10-20.v1.json").write_text(
        json.dumps(
            {
                "date": "2026-10-20", "version": 1, "score": 75, "missing": False,
                "findings": [
                    {
                        "rule": "R2", "severity": "error", "section": "关键变化",
                        "anchor": "关键变化-1", "claim": "硬件线半年沉寂",
                        "claimed_days": 180, "actual_last_seen": "2026-10-03",
                        "actual_gap_days": 17, "evidence": "memory/daily/2026-10-03.md",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    text = render_baseline(cfg)
    assert "2026-10-20" in text
    assert "硬件线半年沉寂" in text
    assert "R2" in text
