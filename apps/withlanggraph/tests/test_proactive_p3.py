"""P3 tests: proactive write-back into the user's main thread checkpoint.

Covers the alternation-guarded write-back of a delivered proactive turn into the
user's main-thread langgraph checkpoint (separate writable sqlite connection + sync
SqliteSaver + fresh compiled graph), the trigger-side scene label extraction, the
full-prompt archive inside additional_kwargs, and the best-effort failure paths
(no thread mapping / no DB / corrupt snapshot). Also wires the end-to-end
run_proactive_job path to prove a successfully delivered outreach lands in the
main thread.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from gacore import proactive
from gacore.config import Config
from gacore.scheduler import Job

_PROMPT = (
    "目标：主动给主人发一条 QQ 私聊消息（≤200字，韩立口吻，克制不啰嗦）。\n"
    "目标收件人 openid：u1（调用 qq_push 时必须把该 openid 原样填入 to 参数）。\n"
    "场景说明：晨安问候\n"
    "当前时间（东八区）：2026-08-30 07:00:00\n"
    "动作：内容想好后，必须调用 qq_push(message=..., to=u1) 工具把这条消息主动发给主人。\n"
)


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0):
    from datetime import datetime, timedelta, timezone

    return datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(hours=8)))


def _make_thread_mapping(cfg: Config, user_id: str, thread_id: str) -> None:
    """Write data/qq_user_threads.json so _thread_id_of resolves for the user."""
    path = cfg.root / "data" / "qq_user_threads.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping = {}
    if path.is_file():
        mapping = json.loads(path.read_text(encoding="utf-8"))
    mapping[user_id] = thread_id
    path.write_text(json.dumps(mapping), encoding="utf-8")


def _make_db(cfg: Config, thread_id: str, messages: list) -> Path:
    """Write a real langgraph checkpoint DB for the given thread (tail = messages[-1])."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    (cfg.root / "data").mkdir(parents=True, exist_ok=True)
    db = cfg.root / "data" / "gacore_chat.db"
    conn = sqlite3.connect(str(db))
    saver = SqliteSaver(conn)
    checkpoint = {
        "v": 1,
        "ts": "2026-08-27T10:00:00Z",
        # checkpoint_id 前缀是时间戳，get_tuple 按 checkpoint_id DESC 取最新；
        # 用更早的时间戳前缀，避免与 update_state 生成的真实新 id 排序冲突。
        "id": "1f000000-0000-0000-0000-000000000001",
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


def _read_messages(db: Path, thread_id: str) -> list:
    """Read the latest snapshot messages via a throwaway read-only connection."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    uri = f"file:{db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        saver = SqliteSaver(conn)
        snapshot = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    finally:
        conn.close()
    if snapshot is None:
        return []
    values = (snapshot.checkpoint or {}).get("channel_values") or {}
    return list(values.get("messages") or [])


# --------------------------------------------------------------------------- scene label


class TestProactiveSceneText:
    def test_extracts_scene_line(self) -> None:
        assert proactive._proactive_scene_text(_PROMPT) == "晨安问候"

    def test_missing_scene_falls_back(self) -> None:
        prompt = "目标：随便发一条\n动作：调 qq_push 发送。\n"
        assert proactive._proactive_scene_text(prompt) == "主动问候"

    def test_empty_scene_falls_back(self) -> None:
        prompt = "场景说明：\n"
        assert proactive._proactive_scene_text(prompt) == "主动问候"


# --------------------------------------------------------------------------- write-back


class TestWriteBackProactive:
    def _run_writeback(self, cfg: Config, user_id: str, thread_id: str, messages: list) -> bool:
        _make_thread_mapping(cfg, user_id, thread_id)
        _make_db(cfg, thread_id, messages)
        # build_graph inside _write_back_proactive initializes the LLM via
        # gacore.graph.get_llm — swap it for a bindable fake in tests.
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        class _Fake(GenericFakeChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gacore.graph.get_llm", lambda tools, bind_tools=False: _Fake(messages=iter(["ok"])))
            return proactive._write_back_proactive(cfg, user_id, _PROMPT, "早呀船长")

    def test_tail_ai_inserts_seed_then_reply(self, tmp_path: Path) -> None:
        # Main thread ends with AI (韩立 just replied); write-back must insert a
        # trigger-side HumanMessage before the AIMessage so the thread never ends AI,AI.
        cfg = Config.for_tests(tmp_path)
        assert self._run_writeback(cfg, "u1", "t1", [HumanMessage(content="在吗"), AIMessage(content="你好")]) is True
        msgs = _read_messages(cfg.root / "data" / "gacore_chat.db", "t1")
        assert [proactive._msg_type(m) for m in msgs] == ["human", "ai", "human", "ai"]
        seed, reply = msgs[-2], msgs[-1]
        assert seed.content == "【定时任务】晨安问候"
        assert seed.additional_kwargs.get("proactive") is True
        # The full outreach prompt is archived for programmatic review.
        assert seed.additional_kwargs.get("proactive_prompt") == _PROMPT
        assert reply.content == "早呀船长"
        assert reply.additional_kwargs.get("proactive") is True

    def test_tail_human_appends_reply_only(self, tmp_path: Path) -> None:
        # Main thread ends with Human (user asked, 韩立 hasn't replied yet): only the
        # AIMessage is appended, keeping the natural alternation intact.
        cfg = Config.for_tests(tmp_path)
        assert self._run_writeback(cfg, "u1", "t1", [HumanMessage(content="在吗")]) is True
        msgs = _read_messages(cfg.root / "data" / "gacore_chat.db", "t1")
        assert [proactive._msg_type(m) for m in msgs] == ["human", "ai"]
        assert msgs[-1].content == "早呀船长"

    def test_empty_thread_inserts_seed_then_reply(self, tmp_path: Path) -> None:
        # Empty thread (no messages yet): tail is None -> the guard treats it as "not a
        # human turn" and seeds the trigger-side HumanMessage before the AI reply, so a
        # bare thread never ends up starting with AI either.
        cfg = Config.for_tests(tmp_path)
        assert self._run_writeback(cfg, "u1", "t1", []) is True
        msgs = _read_messages(cfg.root / "data" / "gacore_chat.db", "t1")
        assert [proactive._msg_type(m) for m in msgs] == ["human", "ai"]
        assert msgs[-2].content == "【定时任务】晨安问候"
        assert msgs[-2].additional_kwargs.get("proactive") is True
        assert msgs[-1].content == "早呀船长"
        assert msgs[-1].additional_kwargs.get("proactive") is True


    def test_no_thread_mapping_returns_false(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        # No qq_user_threads.json at all -> no mapping.
        assert proactive._write_back_proactive(cfg, "u1", _PROMPT, "早呀") is False

    def test_missing_db_returns_false(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _make_thread_mapping(cfg, "u1", "t1")
        # Mapping exists but no gacore_chat.db was ever created.
        assert proactive._write_back_proactive(cfg, "u1", _PROMPT, "早呀") is False


# --------------------------------------------------------------------------- end-to-end wiring


def _sent_headless(captured: list[str]) -> object:
    def fake(job: Job, cfg: Config, prompt: str, max_turns: int) -> tuple:
        captured.append(prompt)
        return ("CURRENT_TASK_DONE", "早呀船长", ['{"status": "sent", "ok": 1}'])

    return fake


class TestRunProactiveJobWriteBack:
    def test_delivered_outreach_lands_in_main_thread(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # A successfully delivered proactive job must write back into the user's main
        # thread: the checkpoint now carries the trigger seed + the proactive reply.
        cfg = Config.for_tests(tmp_path)
        _make_thread_mapping(cfg, "u1", "t1")
        _make_db(cfg, "t1", [HumanMessage(content="在吗"), AIMessage(content="你好船长")])

        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        prompts: list[str] = []
        monkeypatch.setattr(proactive, "_headless_run", _sent_headless(prompts))
        monkeypatch.setattr(
            proactive,
            "recall_topic",
            lambda cfg_, uid, now: {"kind": "none", "text": "", "thread_id": ""},
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 30, 7))
        assert result["sent"] == 1

        msgs = _read_messages(cfg.root / "data" / "gacore_chat.db", "t1")
        types = [proactive._msg_type(m) for m in msgs]
        assert types == ["human", "ai", "human", "ai"]
        # Trigger seed carries the scene label (extracted from the real headless
        # prompt) + archived prompt; reply is the AI text.
        expected_scene = proactive._proactive_scene_text(prompts[0])
        assert msgs[-2].content == f"【定时任务】{expected_scene}"
        assert msgs[-2].additional_kwargs.get("proactive_prompt") == prompts[0]
        assert msgs[-1].content == "早呀船长"

    def test_failed_delivery_does_not_write_back(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Delivery not confirmed -> no write-back happens at all.
        cfg = Config.for_tests(tmp_path)
        _make_thread_mapping(cfg, "u1", "t1")
        _make_db(cfg, "t1", [HumanMessage(content="在吗"), AIMessage(content="你好船长")])

        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        monkeypatch.setattr(proactive, "recall_topic", lambda cfg_, uid, now: {"kind": "none", "text": "", "thread_id": ""})

        def _fail_headless(job: Job, cfg: Config, prompt: str, max_turns: int) -> tuple:
            return ("CURRENT_TASK_DONE", "回复", ['{"status": "error"}'])

        monkeypatch.setattr(proactive, "_headless_run", _fail_headless)
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 30, 7))
        assert result["sent"] == 0
        assert result["skipped"][0]["reason"] == "push_failed"
        msgs = _read_messages(cfg.root / "data" / "gacore_chat.db", "t1")
        assert [proactive._msg_type(m) for m in msgs] == ["human", "ai"]
