"""历史日重跑栅栏（runtime.as_of_context）的封闭测试。

背景（2026-10-25 事故）：rerun --day 2026-10-23 在周日中午执行，信息包按 10-23
锚定，但时钟锚/每日笔记注入/get_time/事实卡全是"活的今天"，模型把周五日报写成了
周日故事。栅栏把整个运行的"今天"切到目标日 23:59：时钟锚模拟、get_time 模拟、
笔记注入窗口 ≤ 目标日、记忆写入 no-op、前日日报只取目标日之前的落盘。
"""

from __future__ import annotations

from pathlib import Path

from gacore.config import Config
from gacore.context import build_system_prompt
from gacore.runtime import as_of_context, as_of_day, week_sunday
from gacore.state import new_state


def _mk_daily(cfg: Config, day: str, marker: str) -> None:
    d = cfg.memory_dir / "daily"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.md").write_text(f"# {day}\n\n- {marker}\n", encoding="utf-8")


def test_week_sunday_mapping():
    assert week_sunday("2026-W01") == "2026-01-04"  # 2026-01-01 是周四，W01 的周日是 01-04
    assert week_sunday(None) is None


def test_get_time_simulated_under_fence():
    from gacore.tools.get_time import get_time

    with as_of_context("2026-10-23"):
        out = get_time.func()
    assert "2026-10-23" in out and "23:59:59" in out and "星期五" in out
    assert "模拟视角" in out
    # 无栅栏：真实时钟，无模拟标记
    assert "模拟视角" not in get_time.func()


def test_time_anchor_simulated_in_system_prompt(tmp_path: Path):
    cfg = Config.for_tests(tmp_path)
    state = new_state("hi", cfg)
    with as_of_context("2026-10-23"):
        prompt = build_system_prompt(state, cfg)
    assert "【当前真实时间】2026-10-23 23:59:59 星期五" in prompt
    assert "历史日重跑·模拟视角" in prompt
    # 退出栅栏后恢复真实时钟
    assert "历史日重跑·模拟视角" not in build_system_prompt(state, cfg)


def test_recent_daily_summaries_as_of_window(tmp_path: Path):
    from gacore.tools.daily_notes import load_recent_daily_summaries

    cfg = Config.for_tests(tmp_path)
    for day in ("2026-10-21", "2026-10-22", "2026-10-23"):
        _mk_daily(cfg, day, f"标记-{day[-2:]}")
    _mk_daily(cfg, "2026-10-24", "标记-24")  # 目标日的未来：不得注入
    _mk_daily(cfg, "2026-10-25", "标记-25")  # 目标日的未来：不得注入
    out = load_recent_daily_summaries(cfg, as_of="2026-10-23")
    assert "标记-23" in out and "标记-22" in out
    assert "标记-24" not in out and "标记-25" not in out


def test_resolve_date_under_fence():
    from gacore.tools.daily_notes import _resolve_date

    with as_of_context("2026-10-23"):
        assert _resolve_date("today", as_of_day()) == "2026-10-23"
        assert _resolve_date("yesterday", as_of_day()) == "2026-10-22"
        assert _resolve_date("2026-10-20", as_of_day()) == "2026-10-20"


def test_edit_daily_skipped_under_fence(tmp_path: Path):
    from gacore.tools.daily_notes import edit_daily

    cfg = Config.for_tests(tmp_path)
    _mk_daily(cfg, "2026-10-23", "原始内容")
    with as_of_context("2026-10-23"):
        res = edit_daily.func(date="today", old_str="", new_str="重跑写入", _cfg=cfg)
    assert res.get("skipped") == "rerun"
    note = (cfg.memory_dir / "daily" / "2026-10-23.md").read_text(encoding="utf-8")
    assert "重跑写入" not in note and "原始内容" in note


def test_start_long_term_update_skipped_under_fence(tmp_path: Path):
    from gacore.tools.memory_tools import start_long_term_update

    cfg = Config.for_tests(tmp_path)
    with as_of_context("2026-10-23"):
        res = start_long_term_update.func(topic="测试画像", _cfg=cfg)
    assert res.get("skipped") == "rerun"


def test_latest_prev_report_never_crosses_before(tmp_path: Path):
    from gacore.daily_info_pack import _latest_prev_report

    cfg = Config.for_tests(tmp_path)
    sched = cfg.logs_dir / "scheduled"
    sched.mkdir(parents=True)
    for fname, day in (
        ("daily-report_20261022_230000.md", "22"),
        ("daily-report_20261023_235500.md", "23"),
        ("daily-report_20261024_235700.md", "24"),
    ):
        (sched / fname).write_text(
            f"- time: 2026-10-{day}T23:00:00+08:00\n\n## Reply\n\n第{day}天的正文\n",
            encoding="utf-8",
        )
    prev = _latest_prev_report(cfg, before="2026-10-24")
    assert prev is not None and "第23天的正文" in prev[1]
    prev2 = _latest_prev_report(cfg, before="2026-10-23")
    assert prev2 is not None and "第22天的正文" in prev2[1]
    assert _latest_prev_report(cfg, before="2026-10-21") is None


def test_run_job_wraps_historical_run_in_fence(tmp_path: Path):
    from gacore.scheduler import Job, run_job

    cfg = Config.for_tests(tmp_path)
    seen: dict[str, object] = {}

    def fake_runner(prompt, cfg_arg, max_turns):
        seen["as_of"] = as_of_day()
        seen["rerun_rule"] = "重跑铁律" in prompt and "2026-10-23" in prompt
        return "CURRENT_TASK_DONE"

    job = Job(name="one-off", schedule="23:50", prompt="测试指令")
    run_job(job, cfg, graph_runner=fake_runner, for_day="2026-10-23", deliver=False)
    assert seen["as_of"] == "2026-10-23"
    assert seen["rerun_rule"] is True
    assert as_of_day() is None  # 栅栏随 run_job 结束复位


def test_weekly_prompt_gets_rerun_rule(tmp_path: Path):
    from gacore.scheduler import Job, _build_job_prompt

    cfg = Config.for_tests(tmp_path)
    job = Job(name="weekly-report", schedule="Sun 20:30", prompt="周报指令")
    prompt = _build_job_prompt(job, cfg, None, for_week="2026-W43")
    assert "重跑铁律" in prompt and "周日" in prompt
