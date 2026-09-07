"""Tests for gacore.scheduler: schedule parsing, due detection, job execution, and the run loop.

The scheduler's graph_runner is an injection seam — tests pass a fake callable instead of
building a real LLM-backed graph. This keeps tests fast and deterministic while still
exercising the scheduler's core logic: loading jobs, computing next-run times, detecting
due jobs, running them, persisting state, and writing outputs + daily notes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gacore.config import Config
from gacore.scheduler import (
    Job,
    JobState,
    ScheduleResult,
    _build_job_prompt,
    _deliver_email,
    _email_body_html,
    _is_incomplete_reply,
    _make_retry_prompt,
    _resolve_email_recipient,
    _sanitize_reply,
    is_due,
    load_jobs,
    load_state,
    next_run_time,
    run_job,
    run_loop,
    save_state,
)

# ---------- next_run_time: daily HH:MM ----------


class TestNextRunDaily:
    """Schedule spec 'HH:MM' — daily at that time."""

    def test_never_run_and_slot_passed_returns_today_slot(self) -> None:
        """Given 14:00 schedule and now=15:00, When never run, Then next_run is today 14:00 (already due)."""
        now = datetime(2026, 8, 3, 15, 0, 0, tzinfo=UTC)
        nxt = next_run_time("14:00", last_run=None, now=now)
        assert nxt == datetime(2026, 8, 3, 14, 0, 0, tzinfo=UTC)

    def test_never_run_and_slot_not_yet_returns_today_slot(self) -> None:
        """Given 14:00 schedule and now=10:00, When never run, Then next_run is today 14:00."""
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC)
        nxt = next_run_time("14:00", last_run=None, now=now)
        assert nxt == datetime(2026, 8, 3, 14, 0, 0, tzinfo=UTC)

    def test_already_ran_today_returns_tomorrow(self) -> None:
        """Given 14:00 schedule, last_run=today 14:05, When now=15:00, Then next_run is tomorrow 14:00."""
        now = datetime(2026, 8, 3, 15, 0, 0, tzinfo=UTC)
        last_run = datetime(2026, 8, 3, 14, 5, 0, tzinfo=UTC).isoformat()
        nxt = next_run_time("14:00", last_run=last_run, now=now)
        assert nxt == datetime(2026, 8, 4, 14, 0, 0, tzinfo=UTC)

    def test_ran_yesterday_and_slot_passed_returns_today_slot(self) -> None:
        """Given 14:00 schedule, last_run=yesterday, When now=15:00 today, Then next_run is today 14:00."""
        now = datetime(2026, 8, 3, 15, 0, 0, tzinfo=UTC)
        last_run = datetime(2026, 8, 2, 14, 0, 0, tzinfo=UTC).isoformat()
        nxt = next_run_time("14:00", last_run=last_run, now=now)
        assert nxt == datetime(2026, 8, 3, 14, 0, 0, tzinfo=UTC)


# ---------- next_run_time: interval ----------


class TestNextRunInterval:
    """Schedule spec 'every N<m|h|d>' — interval from last_run."""

    def test_every_30m_never_run_returns_now_plus_30m(self) -> None:
        """Given every 30m and never run, Then next_run is now + 30m."""
        now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
        nxt = next_run_time("every 30m", last_run=None, now=now)
        assert nxt == datetime(2026, 8, 3, 12, 30, 0, tzinfo=UTC)

    def test_every_6h_with_last_run_returns_last_plus_6h(self) -> None:
        """Given every 6h, last_run=06:00, When now=10:00, Then next_run is 12:00."""
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC)
        last_run = datetime(2026, 8, 3, 6, 0, 0, tzinfo=UTC).isoformat()
        nxt = next_run_time("every 6h", last_run=last_run, now=now)
        assert nxt == datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)

    def test_every_2d_walks_forward_when_overdue(self) -> None:
        """Given every 2d, last_run=3 days ago, When now, Then next_run walks forward to future."""
        now = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
        last_run = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC).isoformat()
        nxt = next_run_time("every 2d", last_run=last_run, now=now)
        # last + 2d = Aug 4 (past), + 2d again = Aug 6 (future)
        assert nxt == datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)

    def test_every_1h_case_insensitive(self) -> None:
        """Given 'every 1H' uppercase, When parsed, Then it works."""
        now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
        nxt = next_run_time("every 1H", last_run=None, now=now)
        assert nxt == datetime(2026, 8, 3, 13, 0, 0, tzinfo=UTC)


# ---------- next_run_time: invalid ----------


def test_next_run_time_returns_none_for_garbage() -> None:
    """Given an unparseable schedule, When next_run_time, Then None is returned."""
    now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
    assert next_run_time("not-a-schedule", last_run=None, now=now) is None
    # "25:00" matches the HH:MM regex but hour=25 is out of range → None
    assert next_run_time("25:00", last_run=None, now=now) is None
    assert next_run_time("12:60", last_run=None, now=now) is None


# ---------- is_due ----------


class TestIsDue:
    """is_due combines next_run_time with a >= now check."""

    def test_due_when_slot_passed_and_never_run(self) -> None:
        """Given 09:00 schedule, now=10:00, never run, Then is_due is True."""
        job = Job(name="test", schedule="09:00", prompt="hi")
        state = JobState()
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC)
        assert is_due(job, state, now) is True

    def test_not_due_when_slot_not_reached(self) -> None:
        """Given 09:00 schedule, now=08:00, never run, Then is_due is False."""
        job = Job(name="test", schedule="09:00", prompt="hi")
        state = JobState()
        now = datetime(2026, 8, 3, 8, 0, 0, tzinfo=UTC)
        assert is_due(job, state, now) is False

    def test_not_due_when_already_ran_today(self) -> None:
        """Given 09:00 schedule, ran at 09:05 today, now=10:00, Then is_due is False."""
        job = Job(name="test", schedule="09:00", prompt="hi")
        state = JobState(last_run=datetime(2026, 8, 3, 9, 5, 0, tzinfo=UTC).isoformat())
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC)
        assert is_due(job, state, now) is False

    def test_not_due_for_invalid_schedule(self) -> None:
        """Given garbage schedule, Then is_due is always False."""
        job = Job(name="test", schedule="garbage", prompt="hi")
        state = JobState()
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=UTC)
        assert is_due(job, state, now) is False


# ---------- load_jobs ----------


class TestLoadJobs:
    """load_jobs reads config/schedule.json and returns enabled Job objects."""

    def test_loads_enabled_jobs_from_json(self, tmp_path: Path) -> None:
        """Given a valid schedule.json, When load_jobs, Then enabled jobs are returned."""
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({
                "jobs": [
                    {"name": "job1", "schedule": "09:00", "prompt": "do task 1"},
                    {"name": "job2", "schedule": "every 1h", "prompt": "do task 2", "enabled": False},
                    {"name": "job3", "schedule": "every 30m", "prompt": "do task 3"},
                ]
            }),
            encoding="utf-8",
        )
        jobs = load_jobs(cfg)
        assert len(jobs) == 2
        assert jobs[0].name == "job1"
        assert jobs[1].name == "job3"

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        """Given no schedule.json, When load_jobs, Then an empty list is returned."""
        cfg = Config.for_tests(tmp_path)
        assert load_jobs(cfg) == []

    def test_returns_empty_on_invalid_json(self, tmp_path: Path) -> None:
        """Given a malformed JSON file, When load_jobs, Then an empty list is returned."""
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text("not json", encoding="utf-8")
        assert load_jobs(cfg) == []

    def test_skips_malformed_job_entries(self, tmp_path: Path) -> None:
        """Given a job missing required keys, When load_jobs, Then it is skipped."""
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({
                "jobs": [
                    {"name": "good", "schedule": "09:00", "prompt": "ok"},
                    {"name": "missing_prompt", "schedule": "09:00"},
                    "not-a-dict",
                ]
            }),
            encoding="utf-8",
        )
        jobs = load_jobs(cfg)
        assert len(jobs) == 1
        assert jobs[0].name == "good"


# ---------- load_state / save_state ----------


class TestStatePersistence:
    """save_state and load_state round-trip JobState dicts."""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """Given states saved, When loaded, Then the same data is returned."""
        cfg = Config.for_tests(tmp_path)
        states = {
            "job1": JobState(last_run="2026-08-03T09:00:00", run_count=5),
            "job2": JobState(last_run="2026-08-02T14:00:00", run_count=1),
        }
        save_state(cfg, states)
        loaded = load_state(cfg)
        assert loaded["job1"].last_run == "2026-08-03T09:00:00"
        assert loaded["job1"].run_count == 5
        assert loaded["job2"].run_count == 1

    def test_load_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        """Given no state file, When load_state, Then an empty dict is returned."""
        cfg = Config.for_tests(tmp_path)
        assert load_state(cfg) == {}


# ---------- run_job ----------


class TestBuildJobPrompt:
    """_build_job_prompt：日报 job 注入信息包 / 非日报隔离 / 信息包构建异常回退原 prompt。"""

    def test_daily_job_prepends_info_pack(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = Config.for_tests(tmp_path)
        captured: list[str] = []

        def fake_build(date, cfg=None) -> str:
            captured.append(date)
            return "〔当日信息包·TEST〕素材段"

        monkeypatch.setattr("gacore.daily_info_pack.build_info_pack", fake_build)
        job = Job(name="daily-report", schedule="09:00", prompt="原始写作指令")
        prompt = _build_job_prompt(job, cfg)
        assert prompt.startswith("〔当日信息包·TEST〕")
        assert "原始写作指令" in prompt  # 信息包作为前置段注入，原 prompt 保留在尾
        assert captured  # build_info_pack 确实被调用

    def test_non_daily_job_keeps_plain_prompt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = Config.for_tests(tmp_path)
        called = []

        def fake_build(date, cfg=None) -> str:
            called.append(date)
            return "不应出现的信息包"

        monkeypatch.setattr("gacore.daily_info_pack.build_info_pack", fake_build)
        job = Job(name="weekly-summary", schedule="every 7d", prompt="周报指令")
        prompt = _build_job_prompt(job, cfg)
        assert prompt == "周报指令"  # 非日报类：信息包完全不注入（隔离）
        assert called == []  # 且 build_info_pack 不被调用

    def test_info_pack_build_failure_falls_back_to_plain_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = Config.for_tests(tmp_path)

        def boom(date, cfg=None) -> str:
            raise RuntimeError("pack boom")

        monkeypatch.setattr("gacore.daily_info_pack.build_info_pack", boom)
        job = Job(name="daily-report", schedule="09:00", prompt="原始写作指令")
        prompt = _build_job_prompt(job, cfg)
        assert prompt == "原始写作指令"  # 信息包构建异常 → 回退原 prompt，job 照常可跑


class TestSanitizeReply:
    """_sanitize_reply：剥 <summary> 块、剥工具 DSL 残尾行、空值安全、幂等。"""

    def test_normal_text_unchanged(self) -> None:
        text = "# 今日状态\n- 今天状态不错\n"
        assert _sanitize_reply(text) == text.strip()

    def test_strips_complete_summary_block(self) -> None:
        raw = "正文前半段\n<summary>思考内容\n多行思考</summary>\n正文后半段"
        cleaned = _sanitize_reply(raw)
        assert "<summary>" not in cleaned
        assert "思考内容" not in cleaned
        assert "正文前半段" in cleaned
        assert "正文后半段" in cleaned

    def test_strips_unclosed_summary_and_following_content(self) -> None:
        # 未闭合的 <summary> 后面都是思考残渣，正文未产出
        raw = "正文开头\n<summary>未闭合的思考"
        cleaned = _sanitize_reply(raw)
        assert "<summary>" not in cleaned
        assert "未闭合的思考" not in cleaned
        assert "正文开头" in cleaned

    def test_strips_tool_dsl_closing_lines(self) -> None:
        raw = "# 今日归档\n- 完成了任务\n</｜DSML｜parameter>\n</｜DSML｜invoke>\n- 另一条"
        cleaned = _sanitize_reply(raw)
        assert "DSML" not in cleaned
        assert "完成了任务" in cleaned
        assert "另一条" in cleaned
        # 正常正文里的 parameter/invoke 不会被误删
        raw2 = "本文介绍 parameter 的用法\n- invoke 函数"
        cleaned2 = _sanitize_reply(raw2)
        assert "parameter" in cleaned2
        assert "invoke" in cleaned2

    def test_empty_and_none_safe(self) -> None:
        assert _sanitize_reply("") == ""
        assert _sanitize_reply(None) is None  # type: ignore[arg-type]

    def test_idempotent(self) -> None:
        raw = "# 标题\n<summary>思考</summary>\n正文\n</｜DSML｜tool_calls>"
        once = _sanitize_reply(raw)
        twice = _sanitize_reply(once)
        assert once == twice


class TestIsIncompleteReply:
    """_is_incomplete_reply：原始非空但清洗后为空 → 判定为残渣。"""

    def test_only_summary_is_incomplete(self) -> None:
        raw = "<summary>全是思考内容</summary>"
        cleaned = _sanitize_reply(raw)
        assert _is_incomplete_reply(raw, cleaned) is True

    def test_only_dsl_residue_is_incomplete(self) -> None:
        raw = "</｜DSML｜parameter>\n</｜DSML｜invoke>"
        cleaned = _sanitize_reply(raw)
        assert _is_incomplete_reply(raw, cleaned) is True

    def test_normal_reply_is_complete(self) -> None:
        raw = "# 今日状态\n- 正常正文"
        cleaned = _sanitize_reply(raw)
        assert _is_incomplete_reply(raw, cleaned) is False

    def test_empty_raw_is_not_incomplete(self) -> None:
        # 空原始回复走 EMPTY_REPLY，不是 INCOMPLETE
        assert _is_incomplete_reply("", "") is False


class TestRunJob:
    """run_job executes a job via the injected graph_runner and writes output + daily note."""

    def test_run_job_with_fake_runner_writes_output_and_daily_note(self, tmp_path: Path) -> None:
        """Given a due job and a fake runner, When run_job, Then output file and daily note are created."""
        cfg = Config.for_tests(tmp_path)
        job = Job(name="test-job", schedule="09:00", prompt="hello", max_turns=5)

        def fake_runner(prompt: str, cfg: Config, max_turns: int) -> str | None:
            return "CURRENT_TASK_DONE"

        result = run_job(job, cfg, graph_runner=fake_runner)

        assert isinstance(result, ScheduleResult)
        assert result.job_name == "test-job"
        assert result.exit_reason == "CURRENT_TASK_DONE"
        assert result.error is None
        assert result.output_path is not None
        assert Path(result.output_path).is_file()
        # Output file contains the prompt and reply
        output_text = Path(result.output_path).read_text(encoding="utf-8")
        assert "hello" in output_text
        assert "test reply" in output_text or "test-job" in output_text

    def test_run_job_catches_exception_and_reports_error(self, tmp_path: Path) -> None:
        """Given a runner that raises, When run_job, Then the error is captured and exit_reason is AGENT_ERROR."""
        cfg = Config.for_tests(tmp_path)
        job = Job(name="failing-job", schedule="09:00", prompt="boom")

        def exploding_runner(prompt: str, cfg: Config, max_turns: int) -> str | None:
            raise RuntimeError("LLM exploded")

        result = run_job(job, cfg, graph_runner=exploding_runner)
        assert result.exit_reason == "AGENT_ERROR"
        assert "LLM exploded" in (result.error or "")
        assert result.output_path is not None

    def test_run_job_writes_daily_note_bullet(self, tmp_path: Path) -> None:
        """Given a successful job, When run_job, Then a bullet is appended to today's daily note."""
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="summarize")

        def fake_runner(prompt: str, cfg: Config, max_turns: int) -> str | None:
            return "CURRENT_TASK_DONE"

        run_job(job, cfg, graph_runner=fake_runner)
        # Check today's daily note exists and contains the scheduled bullet
        from datetime import UTC, datetime
        today = datetime.now(UTC).astimezone().date().isoformat()
        note_path = cfg.memory_dir / "daily" / f"{today}.md"
        assert note_path.is_file()
        note = note_path.read_text(encoding="utf-8")
        assert "[scheduled:daily-report]" in note
        assert "OK" in note


class TestRunJobRetry:
    """日报失败自动重试一次：INCOMPLETE/EMPTY 首次失败 → 换硬约束 prompt 重跑，二次失败才判败。"""

    def test_incomplete_then_retry_succeeds(self, tmp_path: Path) -> None:
        """首次仅残渣(INCOMPLETE_REPLY)，重试成功 → 最终正常完成、error=None、prompt 带硬约束。"""
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="build daily")
        prompts: list[str] = []

        def flaky_runner(prompt: str, c: Config, max_turns: int) -> str | None:
            prompts.append(prompt)
            return "CURRENT_TASK_DONE" if len(prompts) > 1 else "INCOMPLETE_REPLY"

        result = run_job(job, cfg, graph_runner=flaky_runner)
        assert len(prompts) == 2  # 触发了一次重试
        assert result.exit_reason == "CURRENT_TASK_DONE"
        assert result.error is None
        assert "[二次生成·硬约束]" in prompts[1]  # 重试用的是追加强约束的后缀 prompt

    def test_complete_first_no_retry(self, tmp_path: Path) -> None:
        """首次就成功 → 不重试，只调用一次。"""
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="build daily")
        calls: list[str] = []

        def good_runner(prompt: str, c: Config, max_turns: int) -> str | None:
            calls.append(prompt)
            return "CURRENT_TASK_DONE"

        result = run_job(job, cfg, graph_runner=good_runner)
        assert len(calls) == 1
        assert result.exit_reason == "CURRENT_TASK_DONE"
        assert result.error is None

    def test_incomplete_twice_still_failed(self, tmp_path: Path) -> None:
        """首、二次都只出残渣 → 最多重试一次，第二次仍失败按 INCOMPLETE 判败。"""
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="build daily")
        calls: list[str] = []

        def always_bad(prompt: str, c: Config, max_turns: int) -> str | None:
            calls.append(prompt)
            return "INCOMPLETE_REPLY"

        result = run_job(job, cfg, graph_runner=always_bad)
        assert len(calls) == 2  # 只重试一次，不无限循环
        assert result.exit_reason == "INCOMPLETE_REPLY"
        assert "INCOMPLETE_REPLY" in (result.error or "")

    def test_empty_then_retry_succeeds(self, tmp_path: Path) -> None:
        """首次空回复(EMPTY_REPLY) → 重试成功。"""
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="build daily")
        calls: list[str] = []

        def empty_then_good(prompt: str, c: Config, max_turns: int) -> str | None:
            calls.append(prompt)
            return "CURRENT_TASK_DONE" if len(calls) > 1 else "EMPTY_REPLY"

        result = run_job(job, cfg, graph_runner=empty_then_good)
        assert len(calls) == 2
        assert result.exit_reason == "CURRENT_TASK_DONE"
        assert result.error is None

    def test_non_daily_job_no_retry(self, tmp_path: Path) -> None:
        """非日报 job 首次 INCOMPLETE → 不重试（重试只对日报有意义，避免普通 job 双倍消耗）。"""
        cfg = Config.for_tests(tmp_path)
        job = Job(name="some-other-job", schedule="09:00", prompt="hi")
        calls: list[str] = []

        def bad_runner(prompt: str, c: Config, max_turns: int) -> str | None:
            calls.append(prompt)
            return "INCOMPLETE_REPLY"

        result = run_job(job, cfg, graph_runner=bad_runner)
        assert len(calls) == 1
        assert result.exit_reason == "INCOMPLETE_REPLY"


class TestMakeRetryPrompt:
    def test_appends_hard_constraint_suffix(self) -> None:
        out = _make_retry_prompt("【信息包】... 正文要求")
        assert out.startswith("【信息包】... 正文要求")
        assert "[二次生成·硬约束]" in out
        assert "严禁输出 <summary>" in out
        assert "不要再调用任何工具" in out


class TestEmailBodyHtml:
    """_email_body_html：Markdown 子集 → 内联样式 HTML（修复邮件显示裸 #、**、- 源码）。"""

    def test_renders_headers_bullets_bold(self) -> None:
        reply = "# 今日状态\n- **白天**是代码手\n- 深夜是硬件学徒\n\n# 工作日志\n- 提交 v2"
        out = _email_body_html(reply, None)
        assert "<h1" in out and "今日状态" in out
        assert "<ul" in out and "<li" in out
        assert "<b>白天</b>" in out
        assert "<pre" not in out  # 不再是等宽裸文本
        assert "**" not in out  # 加粗记号被消费，不残留

    def test_consecutive_bullets_grouped_into_one_ul(self) -> None:
        out = _email_body_html("- a\n- b\n- c", None)
        assert out.count("<ul") == 1
        assert out.count("<li") == 3

    def test_sections_separated_by_blank_line_split_lists(self) -> None:
        reply = "# A\n- a1\n- a2\n\n# B\n- b1"
        out = _email_body_html(reply, None)
        assert out.count("<ul") == 2  # 空行分隔 → 两组列表
        assert out.count("<h1") == 2

    def test_escapes_html_tags(self) -> None:
        reply = "# 标题<script>alert(1)</script>\n- **x** <img src=x onerror=1>"
        out = _email_body_html(reply, None)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "<img src" not in out

    def test_plain_paragraph_line(self) -> None:
        out = _email_body_html("普通一行文字", None)
        assert "<p" in out and "普通一行文字" in out

    def test_inline_code_rendered(self) -> None:
        out = _email_body_html("- 用 `pytest` 跑测试", None)
        assert "<code" in out and "pytest" in out

    def test_empty_reply_placeholder(self) -> None:
        out = _email_body_html("", None)
        assert "(empty reply)" in out

    def test_error_banner_on_failed_job(self) -> None:
        out = _email_body_html("正文", "INCOMPLETE_REPLY: 残渣")
        assert "FAILED" in out
        assert "INCOMPLETE_REPLY" in out

    def test_header_levels_map_to_h_tags(self) -> None:
        out = _email_body_html("## 二级\n### 三级", None)
        assert "<h2" in out and "<h3" in out


# ---------- run_loop ----------


class TestRunLoop:
    """run_loop polls, fires due jobs, and persists state across iterations."""

    def test_run_loop_fires_due_job_and_persists_state(self, tmp_path: Path) -> None:
        """Given a job due now, When run_loop runs 1 iteration, Then the job fires and state is saved."""
        cfg = Config.for_tests(tmp_path)
        # Create schedule.json with a daily job at 00:01 (always in the past for today)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({
                "jobs": [
                    {"name": "test", "schedule": "00:01", "prompt": "hi", "max_turns": 3}
                ]
            }),
            encoding="utf-8",
        )
        # Use a fixed clock at 12:00 so 00:01 is definitely due
        clock = lambda: datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)

        fired: list[str] = []

        def fake_runner(prompt: str, cfg: Config, max_turns: int) -> str | None:
            fired.append(prompt)
            return "CURRENT_TASK_DONE"

        jobs_run = run_loop(
            cfg=cfg,
            graph_runner=fake_runner,
            max_iterations=1,
            clock=clock,
        )
        assert jobs_run == 1
        assert fired == ["hi"]
        # State persisted
        states = load_state(cfg)
        assert "test" in states
        assert states["test"].run_count == 1

    def test_run_loop_skips_not_due_job(self, tmp_path: Path) -> None:
        """Given a job not yet due, When run_loop runs 1 iteration, Then no job fires."""
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({
                "jobs": [
                    {"name": "future", "schedule": "23:59", "prompt": "hi"}
                ]
            }),
            encoding="utf-8",
        )
        clock = lambda: datetime(2026, 8, 3, 8, 0, 0, tzinfo=UTC)  # before 23:59

        jobs_run = run_loop(cfg=cfg, graph_runner=lambda *a: None, max_iterations=1, clock=clock)
        assert jobs_run == 0

    def test_run_loop_does_not_refire_after_running(self, tmp_path: Path) -> None:
        """Given a job that fired, When run_loop runs a second iteration, Then it does not fire again."""
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({
                "jobs": [
                    {"name": "once", "schedule": "00:01", "prompt": "hi"}
                ]
            }),
            encoding="utf-8",
        )
        clock = lambda: datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)

        fired: list[str] = []

        def fake_runner(prompt: str, cfg: Config, max_turns: int) -> str | None:
            fired.append(prompt)
            return "CURRENT_TASK_DONE"

        # First iteration fires
        run_loop(cfg=cfg, graph_runner=fake_runner, max_iterations=1, clock=clock)
        assert len(fired) == 1
        # Second iteration should not fire (state says already ran today)
        run_loop(cfg=cfg, graph_runner=fake_runner, max_iterations=1, clock=clock)
        assert len(fired) == 1

    def test_run_loop_handles_no_schedule_file(self, tmp_path: Path) -> None:
        """Given no schedule.json, When run_loop runs, Then zero jobs run and no crash."""
        cfg = Config.for_tests(tmp_path)
        clock = lambda: datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
        jobs_run = run_loop(cfg=cfg, graph_runner=lambda *a: None, max_iterations=1, clock=clock)
        assert jobs_run == 0


# ---------- deliver_to: email channel ----------


def _fake_send_sync(captured: dict[str, str]) -> object:
    """Return a _send_sync stand-in that records subject/to/body and reports success."""

    def fake(
        subject: str,
        to_addr: str,
        body: str,
        settings: object,
        image_paths: list[str] | None = None,
        attachment_paths: list[str] | None = None,
        smtp_factory: object = None,
    ) -> dict[str, object]:
        captured["subject"] = subject
        captured["to"] = to_addr
        captured["body"] = body
        return {"status": "sent", "to": to_addr, "subject": subject, "image_count": 0}

    return fake


class TestResolveEmailRecipient:
    """_resolve_email_recipient picks job.email_to > SMTP_TO > SMTP_USER."""

    def test_job_email_to_wins_over_env(self) -> None:
        """Given job.email_to set, When resolving, Then it wins over SMTP_TO / SMTP_USER."""
        job = Job(name="daily", schedule="09:00", prompt="hi", deliver_to="email", email_to="boss@example.com")
        env = {"SMTP_TO": "default@example.com", "SMTP_USER": "me@qq.com"}
        assert _resolve_email_recipient(job, env) == "boss@example.com"

    def test_smtp_to_fallback(self) -> None:
        """Given no email_to but SMTP_TO set, When resolving, Then SMTP_TO is used."""
        job = Job(name="daily", schedule="09:00", prompt="hi", deliver_to="email")
        env = {"SMTP_TO": "default@example.com", "SMTP_USER": "me@qq.com"}
        assert _resolve_email_recipient(job, env) == "default@example.com"

    def test_smtp_user_fallback(self) -> None:
        """Given neither email_to nor SMTP_TO, When resolving, Then SMTP_USER (self) is used."""
        job = Job(name="daily", schedule="09:00", prompt="hi", deliver_to="email")
        env = {"SMTP_USER": "me@qq.com"}
        assert _resolve_email_recipient(job, env) == "me@qq.com"

    def test_no_recipient_returns_empty(self) -> None:
        """Given no email_to / SMTP_TO / SMTP_USER, When resolving, Then empty string."""
        job = Job(name="daily", schedule="09:00", prompt="hi", deliver_to="email")
        assert _resolve_email_recipient(job, {}) == ""


class TestDeliverEmail:
    """_deliver_email builds subject/body and hands off to send_email (via _send_sync)."""

    def test_sends_to_job_email_to(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}
        monkeypatch.setattr("gacore.tools.email_tools._send_sync", _fake_send_sync(captured))
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="hi", deliver_to="email", email_to="boss@example.com")
        env = {"SMTP_USER": "me@qq.com", "SMTP_PASSWORD": "pw", "SMTP_TO": "default@example.com"}

        _deliver_email(job, cfg, "## 今日归档\n- 工作 8h", None, env=env)

        assert captured["to"] == "boss@example.com"
        assert "daily-report" in captured["subject"]
        assert "今日归档" in captured["body"]

    def test_sends_to_smtp_to_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}
        monkeypatch.setattr("gacore.tools.email_tools._send_sync", _fake_send_sync(captured))
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="hi", deliver_to="email")
        env = {"SMTP_USER": "me@qq.com", "SMTP_PASSWORD": "pw", "SMTP_TO": "default@example.com"}

        _deliver_email(job, cfg, "reply", None, env=env)

        assert captured["to"] == "default@example.com"

    def test_failed_job_subject_marks_failed_and_includes_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}
        monkeypatch.setattr("gacore.tools.email_tools._send_sync", _fake_send_sync(captured))
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="hi", deliver_to="email")
        env = {"SMTP_USER": "me@qq.com", "SMTP_PASSWORD": "pw"}

        _deliver_email(job, cfg, "", "LLM exploded", env=env)

        assert "[FAILED]" in captured["subject"]
        assert "LLM exploded" in captured["body"]

    def test_skips_when_smtp_not_configured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}
        monkeypatch.setattr("gacore.tools.email_tools._send_sync", _fake_send_sync(captured))
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="hi", deliver_to="email")
        env = {"SMTP_USER": "me@qq.com"}  # no SMTP_PASSWORD → send_email refuses

        _deliver_email(job, cfg, "reply", None, env=env)

        assert captured == {}

    def test_skips_when_no_recipient(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}
        monkeypatch.setattr("gacore.tools.email_tools._send_sync", _fake_send_sync(captured))
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="hi", deliver_to="email")

        _deliver_email(job, cfg, "reply", None, env={})

        assert captured == {}

    def test_smtp_failure_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing_send_sync(
            subject: str,
            to_addr: str,
            body: str,
            settings: object,
            image_paths: list[str] | None = None,
            attachment_paths: list[str] | None = None,
            smtp_factory: object = None,
        ) -> dict[str, str]:
            return {"error": "smtp_failed", "message": "boom", "to": to_addr}

        monkeypatch.setattr("gacore.tools.email_tools._send_sync", failing_send_sync)
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="hi", deliver_to="email")
        env = {"SMTP_USER": "me@qq.com", "SMTP_PASSWORD": "pw"}

        _deliver_email(job, cfg, "reply", None, env=env)  # must not raise


class TestDeliverRouting:
    """run_job wires deliver_to through _deliver to the right channel."""

    def test_run_job_email_channel_calls_deliver_email(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[object, str, str | None]] = []
        monkeypatch.setattr(
            "gacore.scheduler._deliver_email",
            lambda job, cfg, reply, error, **kwargs: calls.append((job, reply, error)),
        )
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="hi", deliver_to="email")

        run_job(job, cfg, graph_runner=lambda p, c, m: "CURRENT_TASK_DONE")

        assert len(calls) == 1
        assert calls[0][0].deliver_to == "email"
        assert "test reply" in calls[0][1]

    def test_run_job_default_file_channel_does_not_deliver_email(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[object] = []
        monkeypatch.setattr("gacore.scheduler._deliver_email", lambda *a, **k: calls.append(a))
        cfg = Config.for_tests(tmp_path)
        job = Job(name="plain", schedule="09:00", prompt="hi")  # deliver_to defaults to "file"

        run_job(job, cfg, graph_runner=lambda p, c, m: "CURRENT_TASK_DONE")

        assert calls == []

    def test_unsupported_channel_falls_back_to_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[object] = []
        monkeypatch.setattr("gacore.scheduler._deliver_email", lambda *a, **k: calls.append(a))
        cfg = Config.for_tests(tmp_path)
        job = Job(name="odd", schedule="09:00", prompt="hi", deliver_to="feishu")

        run_job(job, cfg, graph_runner=lambda p, c, m: "CURRENT_TASK_DONE")

        assert calls == []  # falls back to file, no email attempted, no crash

    def test_run_job_forwards_for_day_to_deliver_email(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """补跑路径：run_job 的 for_day 必须透传给 _deliver_email（轨迹图/行程按历史天渲染）。"""
        seen: list[dict] = []
        monkeypatch.setattr(
            "gacore.scheduler._deliver_email",
            lambda job, cfg, reply, error, env=None, for_day=None: seen.append({"for_day": for_day}),
        )
        cfg = Config.for_tests(tmp_path)
        job = Job(name="daily-report", schedule="09:00", prompt="hi", deliver_to="email")

        run_job(job, cfg, graph_runner=lambda p, c, m: "CURRENT_TASK_DONE", for_day="2026-09-05")

        assert seen[0]["for_day"] == "2026-09-05"


class TestLoadJobsEmail:
    """load_jobs parses deliver_to and email_to from schedule.json."""

    def test_loads_deliver_to_and_email_to(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({
                "jobs": [
                    {"name": "job1", "schedule": "09:00", "prompt": "t", "deliver_to": "email", "email_to": "a@b.com"},
                ]
            }),
            encoding="utf-8",
        )

        jobs = load_jobs(cfg)

        assert jobs[0].deliver_to == "email"
        assert jobs[0].email_to == "a@b.com"

    def test_defaults_deliver_to_file_and_empty_email_to(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({"jobs": [{"name": "job1", "schedule": "09:00", "prompt": "t"}]}),
            encoding="utf-8",
        )

        jobs = load_jobs(cfg)

        assert jobs[0].deliver_to == "file"
        assert jobs[0].email_to == ""
