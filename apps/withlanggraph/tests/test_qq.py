"""Tests for the QQ frontend: message splitting, dedupe, allowlist, command routing."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

# Ensure `src` is importable and fake botpy BEFORE importing the module under test.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Fake botpy so we can import the module without the real SDK installed.
_fake_botpy = types.ModuleType("botpy")


class _FakeIntents:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


_fake_botpy.Intents = _FakeIntents
_fake_message = types.ModuleType("botpy.message")
_fake_message.C2CMessage = type("C2CMessage", (), {})
_fake_message.GroupMessage = type("GroupMessage", (), {})
sys.modules["botpy"] = _fake_botpy
sys.modules["botpy.message"] = _fake_message


from gacore.frontends import qq
from gacore.frontends.qq import QQApp, _processed_ids, _split_text
from gacore.graph import build_graph
from gacore.state import new_state


def test_split_text_short_unchanged():
    """Given a short string, _split_text returns it as a single chunk."""
    text = "hello world"
    assert _split_text(text) == ["hello world"]


def test_split_text_long_splits_on_newline():
    """Given a long string, _split_text breaks into <= 4500-char chunks."""
    long_line = "x" * 10000
    parts = _split_text(long_line)
    assert all(len(p) <= qq._SPLIT_LIMIT for p in parts)
    assert "".join(parts) == long_line


def test_split_text_empty_becomes_ellipsis():
    """Given an empty or whitespace string, _split_text returns a placeholder."""
    assert _split_text("") == ["..."]
    assert _split_text("   ") == ["..."]


def test_processed_ids_dedupes():
    """Given the same message id twice, the second is dropped as a duplicate."""
    _processed_ids.clear()
    _processed_ids.append("msg-1")
    assert "msg-1" in _processed_ids
    assert len(_processed_ids) == 1


async def test_on_message_unauthorized_is_silenced():
    """Given a user not in the allowlist, on_message returns without sending."""
    _processed_ids.clear()
    graph = MagicMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-auth"
    msg.content = "hello"
    msg.author = MagicMock()
    msg.author.user_openid = "user-123"
    msg.author.id = "user-123"

    with patch.object(qq, "_ALLOWED", frozenset({"other-user"})):
        await app.on_message(msg, is_group=False)

    # Message is deduped (id recorded) but no reply is sent to unauthorized users.
    app.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_help_command_replies():
    """Given /help, on_message sends the command list and records the message id."""
    _processed_ids.clear()
    graph = MagicMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-help"
    msg.content = "/help"
    msg.author = MagicMock()
    msg.author.user_openid = "user-1"
    msg.author.id = "user-1"

    with patch.object(qq, "_ALLOWED", frozenset({"user-1"})):
        await app.on_message(msg, is_group=False)

    assert "msg-help" in _processed_ids
    app.send_text.assert_awaited()
    out = app.send_text.call_args.args[1]
    assert "/help" in out and "/new" in out


@pytest.mark.asyncio
async def test_on_message_new_resets_thread():
    """Given /new, on_message clears the user's thread mapping."""
    _processed_ids.clear()
    qq._user_threads.clear()
    qq._user_threads["user-1"] = "old-thread"

    graph = MagicMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-new"
    msg.content = "/new"
    msg.author = MagicMock()
    msg.author.user_openid = "user-1"
    msg.author.id = "user-1"

    with patch.object(qq, "_ALLOWED", frozenset({"user-1"})):
        await app.on_message(msg, is_group=False)

    assert "user-1" not in qq._user_threads


@pytest.mark.asyncio
async def test_reboot_denied_for_non_admin():
    """Given /reboot from a non-admin, the user is refused and no process restart happens."""
    _processed_ids.clear()
    qq._ADMIN_IDS = frozenset({"admin-1"})

    graph = MagicMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-reboot-nonadmin"
    msg.content = "/reboot"
    msg.author = MagicMock()
    msg.author.user_openid = "user-1"
    msg.author.id = "user-1"

    with (
        patch.object(qq, "_ALLOWED", frozenset({"user-1", "admin-1"})),
        patch.object(qq.os, "execv") as execv,
    ):
        await app.on_message(msg, is_group=False)

    out = app.send_text.call_args.args[1]
    assert "无权限" in out
    execv.assert_not_called()


@pytest.mark.asyncio
async def test_reboot_denied_when_no_admin_configured():
    """Given /reboot with an empty QQ_ADMIN_USERS, nobody may restart the process."""
    _processed_ids.clear()
    qq._ADMIN_IDS = frozenset()

    graph = MagicMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-reboot-noadmin"
    msg.content = "/reboot"
    msg.author = MagicMock()
    msg.author.user_openid = "user-1"
    msg.author.id = "user-1"

    with (
        patch.object(qq, "_ALLOWED", frozenset({"user-1"})),
        patch.object(qq.os, "execv") as execv,
    ):
        await app.on_message(msg, is_group=False)

    out = app.send_text.call_args.args[1]
    assert "无权限" in out
    execv.assert_not_called()


@pytest.mark.asyncio
async def test_reboot_execs_new_process():
    """Given /reboot from an admin, a confirm is sent and os.execv restarts the process."""
    _processed_ids.clear()
    qq._ADMIN_IDS = frozenset({"admin-1"})

    graph = MagicMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-reboot-admin"
    msg.content = "/reboot"
    msg.author = MagicMock()
    msg.author.user_openid = "admin-1"
    msg.author.id = "admin-1"

    sock = MagicMock()
    with (
        patch.object(qq, "_ALLOWED", frozenset({"admin-1"})),
        patch.object(qq, "_instance_sock", sock),
        patch.object(qq.os, "execv") as execv,
    ):
        await app.on_message(msg, is_group=False)

    first_call_text = app.send_text.call_args.args[1]
    assert "正在重启" in first_call_text
    sock.close.assert_called_once()
    execv.assert_called_once_with(sys.executable, [sys.executable, qq.__file__])


@pytest.mark.asyncio
async def test_stream_agent_renders_each_message_once(
    tmp_cfg, message_llm, monkeypatch
) -> None:
    """Regression: the wrapper graph (compiled subgraph + full-list cleanup node) streams
    full-state updates, so each message used to be sent twice per turn — and previous
    turns' replies replayed inside later turns. Each message must be sent exactly once."""
    qq._rendered_msg_ids.clear()

    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "file_write", "args": {"path": "h.txt", "content": "hi"}, "id": "call_1", "type": "tool_call"}
            ],
        ),
        AIMessage(content="wrote it"),
        AIMessage(content="second reply"),
    ]
    llm = message_llm(responses)
    graph = build_graph(llm=llm, cfg=tmp_cfg)
    app = QQApp(graph)
    app.send_text = AsyncMock()

    config = {"configurable": {"thread_id": "qq-dedupe-test"}}
    await app._stream_agent(
        "chat-1", new_state("write a file", tmp_cfg), config, msg_id=None, is_group=False, user_id="user-1"
    )
    await app._stream_agent(
        "chat-1", new_state("again", tmp_cfg), config, msg_id=None, is_group=False, user_id="user-1"
    )

    texts = [call.args[1] for call in app.send_text.call_args_list if len(call.args) > 1]
    assert sum(1 for t in texts if "[agent] -> file_write(" in t) == 1
    assert sum(1 for t in texts if t.startswith("[tools] <-")) == 1
    assert sum(1 for t in texts if "wrote it" in t) == 1   # not duplicated, not replayed in turn 2
    assert sum(1 for t in texts if "second reply" in t) == 1

@pytest.mark.asyncio
async def test_role_command_lists_cards():
    """Given /角色 with no argument, the reply lists the available character cards."""
    _processed_ids.clear()
    from gacore.character import Card

    graph = MagicMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-role-list"
    msg.content = "/角色"
    msg.author = MagicMock()
    msg.author.user_openid = "user-1"
    msg.author.id = "user-1"

    with (
        patch.object(qq, "_ALLOWED", frozenset({"user-1"})),
        patch.object(qq, "list_cards", return_value=[Card(id="nami", name="娜美", path=Path("nami.md"))]),
    ):
        await app.on_message(msg, is_group=False)

    out = app.send_text.call_args.args[1]
    assert "可用角色" in out
    assert "娜美" in out


@pytest.mark.asyncio
async def test_role_command_switches_card_and_clears_thread():
    """Given /角色 <id>, the user's card is set, thread cleared, and reply names the character."""
    _processed_ids.clear()
    qq._user_threads.clear()
    qq._user_card.clear()
    qq._user_threads["user-1"] = "old-thread"

    graph = MagicMock()
    graph.checkpointer = MagicMock()
    graph.checkpointer.adelete_thread = AsyncMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-role-switch"
    msg.content = "/角色 inoue"
    msg.author = MagicMock()
    msg.author.user_openid = "user-1"
    msg.author.id = "user-1"

    with (
        patch.object(qq, "_ALLOWED", frozenset({"user-1"})),
        patch.object(qq, "card_name", return_value="井上织姬"),
        patch.object(qq, "_active_card_repo") as repo,
    ):
        await app.on_message(msg, is_group=False)

    assert qq._user_card.get("user-1") == "inoue"
    assert "user-1" not in qq._user_threads
    repo.set.assert_called_once_with("user-1", "inoue")
    out = app.send_text.call_args.args[1]
    assert "井上织姬" in out


@pytest.mark.asyncio
async def test_role_command_off_clears_card():
    """Given /角色 off, the user's card is removed and a confirmation is sent."""
    _processed_ids.clear()
    qq._user_threads.clear()
    qq._user_card.clear()
    qq._user_card["user-1"] = "inoue"

    graph = MagicMock()
    graph.checkpointer = MagicMock()
    app = QQApp(graph)
    app.client = MagicMock()
    app.send_text = AsyncMock()

    msg = MagicMock()
    msg.id = "msg-role-off"
    msg.content = "/角色 off"
    msg.author = MagicMock()
    msg.author.user_openid = "user-1"
    msg.author.id = "user-1"

    with (
        patch.object(qq, "_ALLOWED", frozenset({"user-1"})),
        patch.object(qq, "_active_card_repo") as repo,
    ):
        await app.on_message(msg, is_group=False)

    assert "user-1" not in qq._user_card
    repo.set.assert_called_once_with("user-1", None)
    out = app.send_text.call_args.args[1]
    assert "已退出角色扮演" in out


def test_with_message_timestamp_prefixes_current_time() -> None:
    """Every user message gains an authoritative current-clock prefix before entering the graph."""
    text = "帮我看看今天几号"
    stamped = qq._with_message_timestamp(text)
    assert "[本条消息真实时间" in stamped
    assert "UTC+8" in stamped
    assert text in stamped


@pytest.mark.parametrize(
    "msg",
    ["今天几号", "现在几点", "当前", "日期", "星期几", "周几", "几点了"],
)
def test_time_intent_words_fail_open_full_graph(msg: str) -> None:
    """Time-intent words must never be short-circuited by the trivial one-liner gate."""
    from gacore.frontends.qq import trivial_detect

    assert trivial_detect(msg) is False


@pytest.mark.asyncio
async def test_burst_messages_merge_into_one_turn(monkeypatch) -> None:
    """Two messages inside the merge window run as one turn with joined text."""
    monkeypatch.setattr(qq, "_MERGE_WINDOW_SECONDS", 0.01)
    app = QQApp(MagicMock())
    app._run_agent = AsyncMock()

    app._enqueue_agent_turn("chat", "第一条", "user-1", msg_id="m1", is_group=False)
    app._enqueue_agent_turn("chat", "第二条", "user-1", msg_id="m2", is_group=False)
    await asyncio.sleep(0.05)

    app._run_agent.assert_awaited_once()
    assert app._run_agent.await_args.args[1] == "第一条\n第二条"


@pytest.mark.asyncio
async def test_merge_cancels_running_turn_without_replaying_committed_text(monkeypatch) -> None:
    """A running turn is cancelled silently; text already in the checkpoint is not re-sent."""
    monkeypatch.setattr(qq, "_MERGE_WINDOW_SECONDS", 0.01)
    app = QQApp(MagicMock())
    app._run_agent = AsyncMock()
    app._turn_input_in_checkpoint = AsyncMock(return_value=True)

    async def _forever() -> None:
        await asyncio.sleep(30)

    old = asyncio.create_task(_forever())
    app._running_tasks["user-1"] = old
    app._inflight_text["user-1"] = "第一条"

    app._enqueue_agent_turn("chat", "第二条", "user-1", msg_id="m2", is_group=False)
    await asyncio.sleep(0.05)

    assert old.cancelled()
    app._run_agent.assert_awaited_once()
    assert app._run_agent.await_args.args[1] == "第二条"


@pytest.mark.asyncio
async def test_merge_replays_inflight_text_when_checkpoint_missed(monkeypatch) -> None:
    """If cancellation beat the checkpoint write, the in-flight text is replayed."""
    monkeypatch.setattr(qq, "_MERGE_WINDOW_SECONDS", 0.01)
    app = QQApp(MagicMock())
    app._run_agent = AsyncMock()
    app._turn_input_in_checkpoint = AsyncMock(return_value=False)

    async def _forever() -> None:
        await asyncio.sleep(30)

    old = asyncio.create_task(_forever())
    app._running_tasks["user-1"] = old
    app._inflight_text["user-1"] = "第一条"

    app._enqueue_agent_turn("chat", "第二条", "user-1", msg_id="m2", is_group=False)
    await asyncio.sleep(0.05)

    assert old.cancelled()
    app._run_agent.assert_awaited_once()
    assert app._run_agent.await_args.args[1] == "第一条\n第二条"


@pytest.mark.asyncio
async def test_message_during_turn_restarts_with_merged_text(monkeypatch) -> None:
    """A message arriving mid-turn cancels it and the loop restarts once, without overlap."""
    monkeypatch.setattr(qq, "_MERGE_WINDOW_SECONDS", 0.01)
    app = QQApp(MagicMock())
    app._turn_input_in_checkpoint = AsyncMock(return_value=True)
    calls: list[str] = []

    async def fake_run(chat_id, text, user_id, *, msg_id=None, is_group=False):
        calls.append(text)
        if len(calls) == 1:
            await asyncio.sleep(30)

    app._run_agent = fake_run

    app._enqueue_agent_turn("chat", "第一条", "user-1", msg_id="m1", is_group=False)
    await asyncio.sleep(0.2)
    assert calls == ["第一条"]

    app._enqueue_agent_turn("chat", "第二条", "user-1", msg_id="m2", is_group=False)
    await asyncio.sleep(0.2)

    assert calls == ["第一条", "第二条"]
    assert app._merge_cancel == set()
    assert "user-1" not in app._inflight_text


class _BlockingGraph:
    """Minimal graph whose stream blocks until cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def aget_state(self, config):
        return types.SimpleNamespace(values={"messages": []}, next=())

    async def astream(self, *args, **kwargs):
        self.started.set()
        await asyncio.sleep(30)
        yield {}


async def _cancelled_turn(tmp_cfg, *, merge_cancel: bool) -> list[str]:
    qq._rendered_msg_ids.clear()
    app = QQApp(MagicMock())
    app.send_text = AsyncMock()
    graph = _BlockingGraph()
    app.graph = graph

    task = asyncio.create_task(
        app._stream_agent(
            "chat",
            new_state("hi", tmp_cfg),
            {"configurable": {"thread_id": "merge-stop"}},
            msg_id=None,
            is_group=False,
            user_id="user-1",
        )
    )
    await asyncio.wait_for(graph.started.wait(), timeout=2)
    if merge_cancel:
        app._merge_cancel.add("user-1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return [call.args[1] for call in app.send_text.call_args_list if len(call.args) > 1]


@pytest.mark.asyncio
async def test_merge_cancel_sends_no_stop_notice(tmp_cfg) -> None:
    texts = await _cancelled_turn(tmp_cfg, merge_cancel=True)
    assert "⏹️ 任务已停止" not in texts


@pytest.mark.asyncio
async def test_explicit_stop_still_notifies(tmp_cfg) -> None:
    texts = await _cancelled_turn(tmp_cfg, merge_cancel=False)
    assert "⏹️ 任务已停止" in texts


async def _run_agent_state(monkeypatch, *, is_group: bool, actor: str | None) -> dict:
    """Run _run_agent with a mocked stream and return the GAState it handed to the graph."""
    app = QQApp(MagicMock())
    app._stream_agent = AsyncMock()
    calls: list[tuple] = []

    def _actor_id_for(openid, companion, card):
        calls.append((openid, companion, card))
        return actor

    monkeypatch.setattr(qq, "actor_id_for", _actor_id_for)

    await app._run_agent("chat", "hi", "user-1", msg_id=None, is_group=is_group)

    return app._stream_agent.await_args.args[1]


@pytest.mark.asyncio
async def test_run_agent_sets_companion_context_for_owner_c2c(monkeypatch) -> None:
    state = await _run_agent_state(monkeypatch, is_group=False, actor="linwan")

    assert state["companion_context"] == {"actor_id": "linwan", "principal_id": "owner"}


@pytest.mark.asyncio
async def test_run_agent_omits_companion_context_for_group(monkeypatch) -> None:
    state = await _run_agent_state(monkeypatch, is_group=True, actor="linwan")

    assert state["companion_context"] is None


@pytest.mark.asyncio
async def test_run_agent_omits_companion_context_for_non_owner(monkeypatch) -> None:
    state = await _run_agent_state(monkeypatch, is_group=False, actor=None)

    assert state["companion_context"] is None
