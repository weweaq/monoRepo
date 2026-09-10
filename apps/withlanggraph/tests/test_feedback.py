"""Feedback-loop tests for the daily-report correction state machine.

Covers: intent sniffing, anchored-reference parsing, date resolution (incl. yesterday),
section-aware anchor stamping (idempotent), write-back to the delivered-report source,
and the pending-draft confirm flow. All run against Config.for_tests(tmp_path) so
nothing touches real memory/logs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gacore.config import Config
from gacore.feedback import (
    Feedback,
    FeedbackContext,
    analyze_feedback,
    apply_feedback,
    clarify_feedback,
    confirm_feedback,
    draft_from_context,
    feedback_route,
    is_feedback_intent,
    load_pending,
    merge_context,
    parse_feedback,
    redeliver_day,
    redeliver_latest,
    save_delivered,
    save_pending,
    stamp_report_bullets,
)

_UT8 = timezone(timedelta(hours=8))


def _reply() -> str:
    return (
        "# 今日状态\n"
        "- 状态一句话\n"
        "# 工作日志\n"
        "- git 9 提交\n"
        "- 上午收 9-08 日报尾巴\n"
        "# 个人观察\n"
        "- LPL 老线再加深\n"
    )


def _deliver(cfg: Config, date: str = "2026-09-09") -> Path:
    save_delivered(cfg, date, _reply())
    return cfg.logs_dir / "delivered_report" / f"{date}.md"


def test_is_feedback_intent():
    assert is_feedback_intent("昨天日报第2条不对")
    assert is_feedback_intent("补充一下工作日志")
    assert is_feedback_intent("今天的报告工作日志那条写错了")
    assert not is_feedback_intent("今天天气怎么样")
    assert not is_feedback_intent("帮我查下冰箱")


class TestParse:
    def test_parses_section_index_and_fix(self, tmp_path: Path):
        # pin "now" so 昨天 is deterministic (2026-09-08)
        now = datetime(2026, 9, 9, 12, 0, tzinfo=_UT8)
        fb = parse_feedback("昨天[工作日志-2] 那条其实跑了四段", Config.for_tests(tmp_path), now_tz=now)
        assert fb is not None
        assert fb.section == "工作日志"
        assert fb.index == 2
        assert fb.intent == "fix"
        assert fb.date == "2026-09-08"

    def test_parses_natural_number_reference(self, tmp_path: Path):
        fb = parse_feedback("个人观察第1条不对，改成关注IG", Config.for_tests(tmp_path))
        assert fb is not None
        assert fb.section == "个人观察"
        assert fb.index == 1
        assert fb.intent == "fix"

    def test_append_intent_detected(self, tmp_path: Path):
        fb = parse_feedback("工作日志-1 补充一句：其实晚上还看了一场", Config.for_tests(tmp_path))
        assert fb is not None
        assert fb.intent == "append"

    def test_defaults_to_today(self, tmp_path: Path):
        now = datetime(2026, 9, 10, 15, 0, tzinfo=_UT8)
        fb = parse_feedback("工作日志-2 改一下", Config.for_tests(tmp_path), now_tz=now)
        assert fb is not None
        assert fb.date == "2026-09-10"

    def test_no_reference_returns_none(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        assert parse_feedback("报告写得不错", cfg) is None
        assert parse_feedback("", cfg) is None

    def test_cross_year_short_date(self, tmp_path: Path):
        now = datetime(2026, 1, 2, 15, 0, tzinfo=_UT8)
        fb = parse_feedback("12-31 工作日志-1 订正", Config.for_tests(tmp_path), now_tz=now)
        assert fb is not None
        assert fb.date == "2026-12-31"


class TestStamp:
    def test_stamps_section_scoped_indexes(self):
        stamped = stamp_report_bullets(_reply())
        assert "- [工作日志-1] git 9 提交" in stamped
        assert "- [工作日志-2] 上午收 9-08 日报尾巴" in stamped
        assert "- [个人观察-1] LPL 老线再加深" in stamped

    def test_stamp_is_idempotent(self):
        once = stamp_report_bullets(_reply())
        twice = stamp_report_bullets(once)
        assert once == twice


class TestApply:
    def test_writes_correction_back(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        src = _deliver(cfg).read_text(encoding="utf-8")
        assert "- [工作日志-2] 上午收 9-08 日报尾巴" in src
        fb = Feedback(id="abc123", date="2026-09-09", section="工作日志",
                      index=2, intent="fix", content="其实是跑了四段", status="pending")
        res = apply_feedback(cfg, fb)
        assert res["status"] == "ok"
        body = (cfg.logs_dir / "delivered_report" / "2026-09-09.md").read_text(encoding="utf-8")
        assert "- [工作日志-2] 其实是跑了四段" in body
        assert "上午收 9-08 日报尾巴" not in body

    def test_appends_addition(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        fb = Feedback(id="abc124", date="2026-09-09", section="个人观察",
                      index=1, intent="append", content="其实昨晚也没睡", status="pending")
        res = apply_feedback(cfg, fb)
        assert res["status"] == "ok"
        body = (cfg.logs_dir / "delivered_report" / "2026-09-09.md").read_text(encoding="utf-8")
        assert "[反馈] 其实昨晚也没睡" in body
        assert "- [个人观察-1] LPL 老线再加深" in body

    def test_unknown_anchor_errors(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        fb = Feedback(id="abc125", date="2026-09-09", section="工作日志",
                      index=99, intent="fix", content="x", status="pending")
        res = apply_feedback(cfg, fb)
        assert res["status"] == "error"

    def test_missing_report_errors(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        fb = Feedback(id="abc126", date="2026-09-01", section="工作日志",
                      index=1, intent="fix", content="x", status="pending")
        res = apply_feedback(cfg, fb)
        assert res["status"] == "error"


class TestPendingFlow:
    def test_confirm_applies_and_marks_applied(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        fb = Feedback(id="draft01", date="2026-09-09", section="工作日志",
                      index=1, intent="fix", content="git 8 提交", status="pending")
        save_pending(cfg, fb)
        assert load_pending(cfg)[0].status == "pending"
        res = confirm_feedback(cfg, "draft01")
        assert res["status"] == "ok"
        drafts = load_pending(cfg)
        assert len(drafts) == 1
        assert drafts[0].status == "applied"
        assert drafts[0].applied_at

    def test_confirm_unknown_draft(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        res = confirm_feedback(cfg, "nope")
        assert res["status"] == "error"

    def test_pending_journal_json_lines(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        fb = Feedback(id="d1", date="2026-09-09", section="工作日志",
                      index=1, intent="fix", content="x", status="pending")
        save_pending(cfg, fb)
        p = cfg.logs_dir / "feedback_pending.jsonl"
        assert p.is_file()
        assert json.loads(p.read_text(encoding="utf-8").strip())["id"] == "d1"


class TestAnalyze:
    def test_full_edit(self, tmp_path: Path):
        ctx = analyze_feedback("[个人观察-1] 改成关注IG", Config.for_tests(tmp_path))
        assert ctx.section == "个人观察"
        assert ctx.index == 1
        assert ctx.intent == "fix"
        assert ctx.content == "改成关注IG"
        assert ctx.missing == []

    def test_vague_message_listed_as_missing(self, tmp_path: Path):
        ctx = analyze_feedback("日报有问题", Config.for_tests(tmp_path))
        assert ctx.section is None
        assert ctx.missing == ["section", "index", "content"]

    def test_section_without_index_not_a_ref(self, tmp_path: Path):
        # "工作日志那条" carries no numeric index, so _REF_RE can't bind it → both None.
        ctx = analyze_feedback("工作日志那条不对", Config.for_tests(tmp_path))
        assert ctx.section is None
        assert ctx.index is None
        assert ctx.missing == ["section", "index", "content"]

    def test_merge_fills_missing(self, tmp_path: Path):
        base = analyze_feedback("日报有问题", Config.for_tests(tmp_path))
        add = analyze_feedback("昨天[工作日志-2] 实际跑了两段", Config.for_tests(tmp_path), now_tz=datetime(2026, 9, 9, 12, tzinfo=_UT8))
        merged = merge_context(base, add)
        assert merged.section == "工作日志"
        assert merged.index == 2
        assert merged.date == "2026-09-08"
        assert merged.missing == []

    def test_draft_from_partial_none(self):
        ctx = FeedbackContext(date="2026-09-09", section="工作日志", index=2, intent=None, content=None)
        assert draft_from_context(ctx) is None

    def test_draft_from_complete(self):
        ctx = FeedbackContext(date="2026-09-09", section="工作日志", index=2, intent="fix", content="改内容")
        fb = draft_from_context(ctx)
        assert fb is not None
        assert fb.date == "2026-09-09"
        assert fb.content == "改内容"


class TestRoute:
    def test_edit_intent(self):
        assert feedback_route("日报第2条不对") == "edit"
        assert feedback_route("帮我把那个补充一下") == "edit"

    def test_confirm(self):
        assert feedback_route("确认") == "confirm"
        assert feedback_route("确认生效") == "confirm"
        assert feedback_route("确认订正") == "confirm"

    def test_redeliver_wins_over_confirm(self):
        assert feedback_route("确认重发") == "redeliver"
        assert feedback_route("重发") == "redeliver"

    def test_id_form_confirm(self):
        assert feedback_route("确认#ab12cd34") == "confirm"

    def test_confirm_prefix_does_not_mask_edit(self):
        # "确认日报第2条不对" is an edit, not a confirm — bare "确认" prefix must not hijack.
        assert feedback_route("确认日报第2条不对") == "edit"
        assert feedback_route("确认收到") is None

    def test_not_feedback(self):
        assert feedback_route("今天天气怎么样") is None
        assert feedback_route("") is None


class TestBatchRedeliver:
    def test_apply_does_not_resend(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        calls: list = []
        monkeypatch.setattr("gacore.scheduler._deliver", lambda *a, **k: calls.append(a))
        fb = Feedback(id="b1", date="2026-09-09", section="工作日志",
                      index=1, intent="fix", content="git 8 提交", status="pending")
        save_pending(cfg, fb)
        res = apply_feedback(cfg, fb)
        assert res["status"] == "ok"
        assert calls == []  # apply must NOT re-send on its own

    def test_redeliver_day_sends_then_noop(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        calls: list = []
        monkeypatch.setattr("gacore.scheduler._deliver", lambda job, cfgx, reply, err, **k: calls.append(reply))
        fb = Feedback(id="b2", date="2026-09-09", section="工作日志",
                      index=1, intent="fix", content="git 8 提交", status="pending")
        save_pending(cfg, fb)
        apply_feedback(cfg, fb)
        res = redeliver_day(cfg, "2026-09-09")
        assert res["status"] == "ok"
        assert len(calls) == 1
        # second call idempotent
        res2 = redeliver_day(cfg, "2026-09-09")
        assert res2["status"] == "noop"
        assert len(calls) == 1

    def test_redeliver_latest_chooses_edited_day(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg, "2026-09-08")
        _deliver(cfg)
        sent: list[str] = []
        monkeypatch.setattr("gacore.scheduler._deliver", lambda job, cfgx, reply, err, **k: sent.append("send"))
        fb = Feedback(id="b3", date="2026-09-09", section="工作日志",
                      index=1, intent="fix", content="x", status="pending")
        save_pending(cfg, fb)
        apply_feedback(cfg, fb)
        res = redeliver_latest(cfg)
        assert res["status"] == "ok"
        assert res["date"] == "2026-09-09"
        assert len(sent) == 1

    def test_noop_when_nothing_applied(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        res = redeliver_day(cfg, "2026-09-09")
        assert res["status"] == "noop"


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class TestClarify:
    async def test_returns_llm_reply(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        ctx = analyze_feedback("日报有问题", cfg)
        llm = _FakeLLM("你先说说想改哪个节？")
        reply = await clarify_feedback(llm, cfg, ctx, date="2026-09-09")
        assert reply == "你先说说想改哪个节？"

    async def test_fallback_when_llm_fails(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        ctx = analyze_feedback("日报有问题", cfg)
        reply = await clarify_feedback(_BoomLLM(), cfg, ctx, date="2026-09-09")
        assert "哪天" in reply

    async def test_fallback_pins_known_section(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        ctx = FeedbackContext(date="2026-09-09", section="工作日志", index=2, intent=None, content=None)
        reply = await clarify_feedback(_BoomLLM(), cfg, ctx, date="2026-09-09")
        assert "工作日志-2" in reply


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    async def ainvoke(self, _messages):
        return _FakeResp(self._content)


class _BoomLLM:
    async def ainvoke(self, _messages):
        raise RuntimeError("boom")