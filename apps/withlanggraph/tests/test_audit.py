"""日报断言基线闸（audit）的封闭测试：临时目录语料，不碰真实 memory/ 与 logs/。"""

from __future__ import annotations

import json
from pathlib import Path

from gacore.audit import (
    audit_report,
    backfill,
    cap_questions,
    cap_weekly,
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


def test_last_seen_tool_rejects_non_iso_date(tmp_path, monkeypatch):
    """非补零日期（2026-1-5）必须直接报 error，不能放行后崩在 fromisoformat。"""
    cfg = Config.for_tests(tmp_path)
    monkeypatch.setattr(Config, "default", classmethod(lambda cls: cfg))
    from gacore.tools.audit_tools import last_seen as last_seen_tool

    out = last_seen_tool.invoke({"topic": "硬件", "before_date": "2026-1-5"})
    assert out["found"] is False
    assert "error" in out


def test_daily_report_prompt_carries_audit_contract():
    """schedule.json 的 daily-report prompt 必须带查表契约与想问你节（P2 接入，防回归）。"""
    schedule = Path(__file__).resolve().parents[1] / "config" / "schedule.json"
    data = json.loads(schedule.read_text(encoding="utf-8"))
    prompt = next(j for j in data["jobs"] if j["name"] == "daily-report")["prompt"]
    assert "last_seen(topic)" in prompt
    assert "# 想问你" in prompt
    assert "0 条合法" in prompt


# ------------------------------------------------------------ P2 Task 4: R5 跨篇计数


def _seed_running_count_case(tmp_path, today_text: str):
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True, exist_ok=True)
    reports = cfg.logs_dir / "delivered_report"
    reports.mkdir(parents=True, exist_ok=True)
    _write_v1(cfg, "2026-10-19", "# 工作日志\n- [工作日志-1] 硬件教程连续 2 天在看\n")
    (reports / "2026-10-20.md").write_text(today_text, encoding="utf-8")
    return cfg


def test_r5_flags_discontinuous_running_count(tmp_path):
    cfg = _seed_running_count_case(tmp_path, "# 工作日志\n- [工作日志-1] 硬件教程连续第 4 天在看\n")
    got = audit_report(cfg, "2026-10-20")
    r5 = [f for f in got.findings if f.rule == "R5"]
    assert len(r5) == 1
    assert r5[0].severity == "warn"
    assert "昨日连续 2 天" in r5[0].evidence


def test_r5_continuous_and_fresh_start_pass(tmp_path):
    cfg = _seed_running_count_case(tmp_path, "# 工作日志\n- [工作日志-1] 硬件教程连续第 3 天在看\n")
    got = audit_report(cfg, "2026-10-20")
    assert [f for f in got.findings if f.rule == "R5"] == []

    cfg2 = _seed_running_count_case(tmp_path, "# 工作日志\n- [工作日志-1] 硬件教程连续第 1 天在看\n")
    got2 = audit_report(cfg2, "2026-10-20")
    assert [f for f in got2.findings if f.rule == "R5"] == []


def test_r5_topic_unrecognized_fails_open(tmp_path):
    """词表识别不出主题（两侧任一）就不判 R5（fail-open，不误报）。"""
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)
    reports = cfg.logs_dir / "delivered_report"
    reports.mkdir(parents=True)
    _write_v1(cfg, "2026-10-19", "# 工作日志\n- [工作日志-1] 肉鸽连续 2 天在刷\n")
    (reports / "2026-10-20.md").write_text("# 工作日志\n- [工作日志-1] 肉鸽连续第 5 天在刷\n", encoding="utf-8")
    got = audit_report(cfg, "2026-10-20")
    assert [f for f in got.findings if f.rule == "R5"] == []


# --------------------------------------------- P2 Task 5: 想问你硬闸 + 审计钩子


def test_cap_questions_truncates_and_drops_baseless():
    bullets = [f"- [想问你-{i}] 铺垫{i}？（依据：10-24 数据）" for i in range(1, 8)]
    bullets.append("- [想问你-8] 今天天气怎么样？")
    reply = "# 今日状态\n- [今日状态-1] 正文A\n\n# 想问你\n" + "\n".join(bullets) + "\n\n# 明日计划\n- [明日计划-1] B\n"
    capped, changed = cap_questions(reply)
    assert changed is True
    assert capped.count("- [想问你-") == 5
    assert "天气" not in capped
    # 其余正文逐字不动
    assert "- [今日状态-1] 正文A" in capped
    assert "- [明日计划-1] B" in capped


def test_cap_questions_no_section_unchanged():
    reply = "# 今日状态\n- [今日状态-1] 正文\n"
    out, changed = cap_questions(reply)
    assert out == reply
    assert changed is False


def test_cap_questions_all_dropped_removes_section():
    reply = "# 想问你\n- [想问你-1] 今天心情如何？\n\n# 明日计划\n- [明日计划-1] x\n"
    out, changed = cap_questions(reply)
    assert changed is True
    assert "# 想问你" not in out
    assert "[明日计划-1] x" in out


def test_cap_questions_within_limit_untouched():
    reply = "# 想问你\n- [想问你-1] q1（依据：a）\n- [想问你-2] q2（依据：b）\n"
    out, changed = cap_questions(reply)
    assert out == reply
    assert changed is False


def test_scheduler_audit_delivered_writes_v1(tmp_path):
    from gacore import scheduler

    cfg = Config.for_tests(tmp_path)
    reports = cfg.logs_dir / "delivered_report"
    reports.mkdir(parents=True)
    (reports / "2026-10-20.md").write_text("# 今日状态\n- [今日状态-1] ok\n", encoding="utf-8")
    scheduler._audit_delivered(cfg, "2026-10-20")
    assert (cfg.logs_dir / "report_audit" / "2026-10-20.v1.json").is_file()


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
    # 第二次跑（全部命中已有 v1）汇总仍须与 baseline 一致（决策 38）
    again = backfill(cfg)
    assert again["conflicts"] == 1
    assert again["claims"] == summary["claims"]


def test_backfill_reports_counts_all_v1_not_just_dailies(tmp_path):
    """周报 v1 也在 report_audit 下：reports 口径 = 全部 v1 快照，dailies 单独计数。"""
    cfg = Config.for_tests(tmp_path)
    reports = cfg.logs_dir / "delivered_report"
    reports.mkdir(parents=True)
    (reports / "2026-10-19.md").write_text("# 今日状态\n- [今日状态-1] 一切正常\n", encoding="utf-8")
    audit_dir = cfg.logs_dir / "report_audit"
    audit_dir.mkdir(parents=True)
    (audit_dir / "2026-W42.v1.json").write_text(
        json.dumps({"date": "2026-W42", "claims": [], "findings": []}), encoding="utf-8"
    )
    summary = backfill(cfg)
    assert summary["dailies"] == 1
    assert summary["reports"] == 2


def test_last_seen_reports_alternative_hits(tmp_path):
    """回溯提及会把"最近提及"拉近；alt 带上次近日期供人判断（决策 37）。"""
    cfg = Config.for_tests(tmp_path)
    daily = cfg.memory_dir / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-09-15.md").write_text("工学椅又看了\n", encoding="utf-8")
    (cfg.memory_dir / "global_mem_facts.txt").write_text(
        "[2026-10-06] insight: 桌面升级（顺带提到工学椅）\n", encoding="utf-8"
    )
    seen = last_seen(cfg, "工学椅", "2026-10-12")
    assert seen.date == "2026-10-06" and seen.gap_days == 6
    assert seen.alt and seen.alt[0].startswith("2026-09-15")


def test_cap_weekly_drops_tail_sections_but_keeps_rating():
    body = (
        "# 本周一句话\n- 一句话\n\n"
        "# 三条变化\n" + "- 变化 " + "长" * 900 + "\n\n"
        "# 给这周打分\n- 回复「评分 4 3」\n"
    )
    gated, dropped = cap_weekly(body, limit=300)
    # 三条变化是核心节：宁可压缩正文也不整节删（dropped 只计整节删除）
    assert dropped == 0
    assert len(gated) <= 300
    assert "# 本周一句话" in gated
    assert "# 三条变化" in gated
    assert "# 给这周打分" in gated
    assert "评分 4 3" in gated


def test_cap_weekly_short_body_untouched():
    body = "# 本周一句话\n- 短\n"
    assert cap_weekly(body, limit=300) == (body, 0)


def test_cap_weekly_h2_headings_still_keep_interactive_sections():
    """模型把标题降级成 `##` 时也要按节删减，且三个交互节不能被删。"""
    body = (
        "## 本周一句话\n- 一句话\n\n"
        "## 三条变化\n" + "- 变化 " + "长" * 600 + "\n\n"
        "## 想问你\n- [想问你-1] 问题（依据：10-25）\n\n"
        "## 画像候选\n- [候选-1] 候选\n\n"
        "## 给这周打分\n- 回复「评分 4 3」\n"
    )
    gated, dropped = cap_weekly(body, limit=300)
    assert dropped == 0
    assert len(gated) <= 300
    assert "## 给这周打分" in gated
    assert "评分 4 3" in gated
    assert "想问你-1" in gated and "候选-1" in gated


def test_cap_weekly_hard_truncate_keeps_rating_within_limit():
    """首节自身超限时：压缩首节、保住打分节，且结果严格 <= limit（含省略号）。"""
    body = (
        "# 本周一句话\n" + "- " + "长" * 800 + "\n\n"
        "# 给这周打分\n- 回复「评分 4 3」\n"
    )
    gated, _dropped = cap_weekly(body, limit=200)
    assert len(gated) <= 200
    assert "# 给这周打分" in gated
    assert "评分 4 3" in gated


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
