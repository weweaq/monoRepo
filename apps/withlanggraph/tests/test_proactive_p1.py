"""Tests for gacore.proactive P1: Topic Recall + jitter randomisation.

Covers the guard config loading (config/proactive.json), the probability-pass jitter
gate, the job-guard threshold override, the lightweight trivial gate, the open-question
extraction from a message list, the read-only main-thread recall (SqliteSaver via a
separate read-only connection), the yesterday-portrait injection, and the end-to-end
run_proactive_job wiring (jitter skip, prompt injection).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from gacore import proactive
from gacore.config import Config
from gacore.scheduler import Job

_TZ = timezone(timedelta(hours=8))


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_TZ)


def _write_guard(cfg: Config, guard: dict | None) -> None:
    (cfg.root / "config").mkdir(parents=True, exist_ok=True)
    (cfg.root / "config" / "proactive.json").write_text(
        json.dumps({"enabled": True, "guard": guard} if guard is not None else {}),
        encoding="utf-8",
    )


def _make_db(cfg: Config, thread_id: str, messages: list) -> Path:
    """Write a real langgraph checkpoint DB for the given thread."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    (cfg.root / "data").mkdir(parents=True, exist_ok=True)
    db = cfg.root / "data" / "gacore_chat.db"
    conn = sqlite3.connect(str(db))
    saver = SqliteSaver(conn)
    checkpoint = {
        "v": 1,
        "ts": "2026-08-27T10:00:00Z",
        "id": "1f1f0001-0000-0000-0000-000000000001",
        "channel_values": {"messages": messages},
        "channel_versions": {},
        "versions_seen": {},
    }
    metadata = {
        "source": "loop",
        "step": 1,
        "writes": {"messages": [("new", {"type": "ai", "content": ""})]},
        "parents": {},
    }
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    saver.put(config, checkpoint, metadata, {})
    conn.close()
    return db


# ---------- guard config ----------


class TestGuardConfig:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        assert proactive.load_guard_config(cfg) == {}

    def test_valid_file_returns_guard_section(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"jitter_minutes": 30, "max_per_day": 1})
        assert proactive.load_guard_config(cfg) == {"jitter_minutes": 30, "max_per_day": 1}

    def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        (cfg.root / "config").mkdir(parents=True, exist_ok=True)
        (cfg.root / "config" / "proactive.json").write_text("{not json", encoding="utf-8")
        assert proactive.load_guard_config(cfg) == {}

    def test_guard_not_dict_returns_empty(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        (cfg.root / "config").mkdir(parents=True, exist_ok=True)
        (cfg.root / "config" / "proactive.json").write_text(
            json.dumps({"guard": 42}), encoding="utf-8"
        )
        assert proactive.load_guard_config(cfg) == {}

    def test_guard_int_fallback(self) -> None:
        assert proactive._guard_int(None, "max_per_day", 2) == 2
        assert proactive._guard_int({}, "max_per_day", 2) == 2
        assert proactive._guard_int({"max_per_day": "3"}, "max_per_day", 2) == 3
        assert proactive._guard_int({"max_per_day": "abc"}, "max_per_day", 2) == 2
        assert proactive._guard_int({"max_per_day": -1}, "max_per_day", 2) == 2

    def test_jitter_minutes_defaults_to_zero(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        assert proactive._jitter_minutes(proactive.load_guard_config(cfg)) == 0

    def test_jitter_minutes_from_config(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"jitter_minutes": 30})
        assert proactive._jitter_minutes(proactive.load_guard_config(cfg)) == 30


# ---------- jitter gate ----------


class TestJitterAllows:
    def test_zero_disables_gate(self) -> None:
        allowed, reason = proactive.jitter_allows(object(), 0, rng=lambda: 0.999)
        assert allowed
        assert reason == ""

    def test_roll_zero_always_passes(self) -> None:
        allowed, reason = proactive.jitter_allows(object(), 30, rng=lambda: 0.0)
        assert allowed
        assert reason == ""

    def test_roll_high_skips(self) -> None:
        allowed, reason = proactive.jitter_allows(object(), 30, rng=lambda: 0.99)
        assert not allowed
        assert reason == "jitter_skip"

    def test_very_high_roll_skips_even_at_tiny_jitter(self) -> None:
        # jitter=1 -> p = 60/61 ~ 0.9836, a roll of 0.999 must still skip.
        allowed, reason = proactive.jitter_allows(object(), 1, rng=lambda: 0.999)
        assert not allowed
        assert reason == "jitter_skip"


# ---------- job guard override ----------


class TestJobGuardOverride:
    def test_max_per_day_override(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"max_per_day": 1})
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        state = {
            "u_u1": {
                "last_sent_date": "2026-08-27",
                "daily_count": 1,
                "last_active": "2026-08-27T01:00:00+08:00",
            }
        }
        allowed, reason = proactive.job_guard_allows(
            "u1", job, state, _dt(2026, 8, 27, 8, 0), guard=proactive.load_guard_config(cfg)
        )
        assert not allowed
        assert reason == "daily_cap"

    def test_hot_chat_override(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"hot_chat_minutes": 60})
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        state = {"u_u1": {"last_active": "2026-08-27T07:20:00+08:00"}}
        allowed, reason = proactive.job_guard_allows(
            "u1", job, state, _dt(2026, 8, 27, 8, 0), guard=proactive.load_guard_config(cfg)
        )
        assert not allowed
        assert reason == "hot_chat"

    def test_default_threshold_allows_same_input(self, tmp_path: Path) -> None:
        Config.for_tests(tmp_path)
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        state = {"u_u1": {"last_active": "2026-08-27T07:20:00+08:00"}}
        # Default hot_chat_minutes=30 -> 40min gap is fine.
        allowed, reason = proactive.job_guard_allows("u1", job, state, _dt(2026, 8, 27, 8, 0))
        assert allowed
        assert reason == ""


# ---------- trivial gate ----------


class TestIsTrivial:
    def test_empty(self) -> None:
        assert proactive._is_trivial("")
        assert proactive._is_trivial("   ")

    def test_short_filler(self) -> None:
        assert proactive._is_trivial("嗯")
        assert proactive._is_trivial("好的")
        assert proactive._is_trivial("晚安")

    def test_intent_words_are_not_trivial(self) -> None:
        assert not proactive._is_trivial("帮我查下天气")
        assert not proactive._is_trivial("这篇文章讲了什么")

    def test_long_reaction_words_are_trivial(self) -> None:
        assert proactive._is_trivial("哈哈哈哈哈哈")

    def test_filler_with_intent_chars_is_trivial(self) -> None:
        # Audit M1: "在吗" / "嗯呢" contain the broad intent chars 吗/呢 but are
        # chit-chat, so they must be trivial — not open questions.
        assert proactive._is_trivial("在吗")
        assert proactive._is_trivial("在吗？")
        assert proactive._is_trivial("嗯呢")
        assert proactive._is_trivial("好的呀")

    def test_filler_with_real_content_fails_open(self) -> None:
        # Audit M1: a longer message that merely mentions a filler word (or asks a
        # real question) still fails open as non-trivial.
        assert not proactive._is_trivial("今天在吗？")
        assert not proactive._is_trivial("下班了吗")
        assert not proactive._is_trivial("在吗 帮我看看方案")


# ---------- open question extraction ----------


class TestOpenQuestion:
    def test_empty_list(self) -> None:
        assert proactive._open_question_from([]) == ""

    def test_lone_human(self) -> None:
        msgs = [HumanMessage(content="帮我看看") ]
        assert proactive._open_question_from(msgs) == "帮我看看"

    def test_last_human_wins(self) -> None:
        msgs = [HumanMessage(content="你好"), HumanMessage(content="帮我分析下方案")]
        assert proactive._open_question_from(msgs) == "帮我分析下方案"

    def test_answered_by_ai(self) -> None:
        msgs = [HumanMessage(content="帮我看看"), AIMessage(content="好的")]
        assert proactive._open_question_from(msgs) == ""

    def test_trailing_tool_message_still_open(self) -> None:
        msgs = [HumanMessage(content="帮我看看"), ToolMessage(content='{"status": "sent"}', tool_call_id="t1")]
        assert proactive._open_question_from(msgs) == "帮我看看"

    def test_only_ai(self) -> None:
        assert proactive._open_question_from([AIMessage(content="你好")]) == ""

    def test_trivial_human_not_open(self) -> None:
        assert proactive._open_question_from([HumanMessage(content="嗯")]) == ""

    def test_filler_last_human_not_open(self) -> None:
        # Audit M1: "在吗" as the trailing user turn is trivial, not an open question.
        msgs = [HumanMessage(content="在吗")]
        assert proactive._open_question_from(msgs) == ""
        assert proactive._open_question_from([HumanMessage(content="在吗？")]) == ""

    def test_real_question_last_human_still_open(self) -> None:
        # Audit M1: a genuine question mentioning 在吗 still extracts as open.
        assert proactive._open_question_from([HumanMessage(content="在吗 帮我看看方案")]) == "在吗 帮我看看方案"

    def test_dict_messages_answered(self) -> None:
        msgs = [{"type": "human", "content": "帮我看看"}, {"type": "ai", "content": "好的"}]
        assert proactive._open_question_from(msgs) == ""

    def test_dict_messages_open(self) -> None:
        msgs = [{"type": "human", "content": "帮我看看"}]
        assert proactive._open_question_from(msgs) == "帮我看看"


# ---------- recall_topic (mocked parts) ----------


class TestRecallTopic:
    def test_open_question_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "_thread_id_of", lambda cfg_, uid: "t1")
        monkeypatch.setattr(
            proactive, "_read_thread_messages", lambda cfg_, tid: [HumanMessage(content="帮我看看那个方案")]
        )
        monkeypatch.setattr(proactive, "_yesterday_daily_text", lambda cfg_, now: "昨日总结")
        recall = proactive.recall_topic(cfg, "u1", _dt(2026, 8, 27, 7, 30))
        assert recall["kind"] == "open_question"
        assert recall["text"] == "帮我看看那个方案"
        assert recall["thread_id"] == "t1"

    def test_falls_back_to_daily_note(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "_thread_id_of", lambda cfg_, uid: "t1")
        monkeypatch.setattr(
            proactive,
            "_read_thread_messages",
            lambda cfg_, tid: [HumanMessage(content="帮我看看"), AIMessage(content="好的")],
        )
        monkeypatch.setattr(proactive, "_yesterday_daily_text", lambda cfg_, now: "昨日总结")
        recall = proactive.recall_topic(cfg, "u1", _dt(2026, 8, 27, 7, 30))
        assert recall["kind"] == "daily_note"
        assert recall["text"] == "昨日总结"
        assert recall["thread_id"] == "t1"

    def test_no_thread_no_insight_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "_thread_id_of", lambda cfg_, uid: "")
        monkeypatch.setattr(proactive, "_yesterday_daily_text", lambda cfg_, now: "")
        recall = proactive.recall_topic(cfg, "u1", _dt(2026, 8, 27, 7, 30))
        assert recall["kind"] == "none"
        assert recall["text"] == ""

    def test_daily_note_without_thread(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "_thread_id_of", lambda cfg_, uid: "")
        monkeypatch.setattr(proactive, "_yesterday_daily_text", lambda cfg_, now: "昨日画像")
        recall = proactive.recall_topic(cfg, "u1", _dt(2026, 8, 27, 7, 30))
        assert recall["kind"] == "daily_note"
        assert recall["text"] == "昨日画像"


# ---------- read-only main thread (real SqliteSaver) ----------


class TestReadThreadSqlite:
    def test_reads_answered_thread(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _make_db(cfg, "t1", [HumanMessage(content="帮我看看"), AIMessage(content="好的")])
        msgs = proactive._read_thread_messages(cfg, "t1")
        assert len(msgs) == 2
        assert proactive._open_question_from(msgs) == ""

    def test_reads_open_question_thread(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _make_db(cfg, "t1", [HumanMessage(content="帮我看看那个方案")])
        msgs = proactive._read_thread_messages(cfg, "t1")
        assert proactive._open_question_from(msgs) == "帮我看看那个方案"

    def test_missing_db_returns_empty(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        assert proactive._read_thread_messages(cfg, "t1") == []

    def test_empty_thread_id_returns_empty(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        assert proactive._read_thread_messages(cfg, "") == []

    def test_thread_id_of_mapping(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        (cfg.root / "data").mkdir(parents=True, exist_ok=True)
        (cfg.root / "data" / "qq_user_threads.json").write_text(
            json.dumps({"u1": "t-abc"}), encoding="utf-8"
        )
        assert proactive._thread_id_of(cfg, "u1") == "t-abc"
        assert proactive._thread_id_of(cfg, "u2") == ""

    def test_corrupt_thread_mapping_returns_empty(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        (cfg.root / "data").mkdir(parents=True, exist_ok=True)
        (cfg.root / "data" / "qq_user_threads.json").write_text("{bad", encoding="utf-8")
        assert proactive._thread_id_of(cfg, "u1") == ""


# ---------- yesterday portrait ----------


class TestYesterdayDaily:
    def test_onboard_pack_yesterday(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        (cfg.root / "data").mkdir(parents=True, exist_ok=True)
        (cfg.root / "data" / "onboard_pack.json").write_text(
            json.dumps(
                {
                    "date": "2026-08-26",
                    "payload": {"daily_summary_md": "昨天在忙重构进度推进\n第二行"},
                }
            ),
            encoding="utf-8",
        )
        text = proactive._yesterday_daily_text(cfg, _dt(2026, 8, 27, 7, 30))
        assert "重构" in text

    def test_onboard_pack_today_ignored(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        (cfg.root / "data").mkdir(parents=True, exist_ok=True)
        (cfg.root / "data" / "onboard_pack.json").write_text(
            json.dumps({"date": "2026-08-27", "payload": {"daily_summary_md": "今天的"}}),
            encoding="utf-8",
        )
        assert proactive._yesterday_daily_text(cfg, _dt(2026, 8, 27, 7, 30)) == ""

    def test_corrupt_onboard_pack_ignored(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        (cfg.root / "data").mkdir(parents=True, exist_ok=True)
        (cfg.root / "data" / "onboard_pack.json").write_text("{bad", encoding="utf-8")
        assert proactive._yesterday_daily_text(cfg, _dt(2026, 8, 27, 7, 30)) == ""

    def test_daily_notes_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        yesterday = (_dt(2026, 8, 27, 7, 30) - timedelta(days=1)).date().isoformat()

        def fake_load(cfg_: Config, days: int) -> str:
            return f"[{yesterday}]\n- 昨日 bullet 推进\n\n[2026-08-27]\n- 今日 bullet"

        monkeypatch.setattr(
            "gacore.tools.daily_notes.load_recent_daily_summaries", fake_load
        )
        text = proactive._yesterday_daily_text(cfg, _dt(2026, 8, 27, 7, 30))
        assert "昨日 bullet" in text

    def test_no_yesterday_block_returns_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(
            "gacore.tools.daily_notes.load_recent_daily_summaries", lambda cfg_, days: "[2026-08-27]\n- 今日"
        )
        assert proactive._yesterday_daily_text(cfg, _dt(2026, 8, 27, 7, 30)) == ""


# ---------- run_proactive_job P1 wiring ----------


class TestRunProactiveJobP1:
    def test_jitter_skip_skips_whole_tick(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"jitter_minutes": 30})
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        called: list[bool] = []
        monkeypatch.setattr(
            proactive, "_headless_run", lambda *a, **k: called.append(True) or ("CURRENT_TASK_DONE", "", [])
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30), rng=lambda: 0.999)
        assert called == []
        assert result["attempted"] == 0
        assert result["skipped"] == [{"user": "", "reason": "jitter_skip"}]

    def test_jitter_pass_sends_normally(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"jitter_minutes": 30})
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        monkeypatch.setattr(
            proactive,
            "_headless_run",
            lambda job, cfg, prompt, max_turns: ("CURRENT_TASK_DONE", "韩立的早安", ['{"status": "sent", "ok": 1}']),
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30), rng=lambda: 0.0)
        assert result["attempted"] == 1
        assert result["sent"] == 1
        assert result["skipped"] == []

    def test_no_config_keeps_p0_behaviour(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        monkeypatch.setattr(
            proactive,
            "_headless_run",
            lambda job, cfg, prompt, max_turns: ("CURRENT_TASK_DONE", "韩立的早安", ['{"status": "sent", "ok": 1}']),
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30), rng=lambda: 0.999)
        assert result["attempted"] == 1
        assert result["sent"] == 1

    def test_topic_recall_injected_into_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        prompts: list[str] = []

        def fake_headless(job: Job, cfg: Config, prompt: str, max_turns: int) -> tuple:
            prompts.append(prompt)
            return ("CURRENT_TASK_DONE", "接话题", ['{"status": "sent", "ok": 1}'])

        monkeypatch.setattr(proactive, "_headless_run", fake_headless)
        monkeypatch.setattr(
            proactive,
            "recall_topic",
            lambda cfg_, uid, now: {"kind": "open_question", "text": "上次那个方案改好了吗", "thread_id": "t1"},
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert result["sent"] == 1
        assert len(prompts) == 1
        assert "最近话题" in prompts[0]
        assert "上次那个方案改好了吗" in prompts[0]

    def test_daily_note_injected_into_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        prompts: list[str] = []

        def fake_headless(job: Job, cfg: Config, prompt: str, max_turns: int) -> tuple:
            prompts.append(prompt)
            return ("CURRENT_TASK_DONE", "问候", ['{"status": "sent", "ok": 1}'])

        monkeypatch.setattr(proactive, "_headless_run", fake_headless)
        monkeypatch.setattr(
            proactive,
            "recall_topic",
            lambda cfg_, uid, now: {"kind": "daily_note", "text": "昨天在忙重构进度", "thread_id": ""},
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert result["sent"] == 1
        assert "昨日画像线索" in prompts[0]
        assert "昨天在忙重构进度" in prompts[0]
