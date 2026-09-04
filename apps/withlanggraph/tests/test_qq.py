"""Tests for the QQ frontend: message splitting, dedupe, allowlist, command routing."""

from __future__ import annotations

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
        patch.object(qq, "_save_user_cards"),
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
        patch.object(qq, "_save_user_cards") as save,
    ):
        await app.on_message(msg, is_group=False)

    assert qq._user_card.get("user-1") == "inoue"
    assert "user-1" not in qq._user_threads
    save.assert_called_once()
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
        patch.object(qq, "_save_user_cards"),
    ):
        await app.on_message(msg, is_group=False)

    assert "user-1" not in qq._user_card
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
