"""Tests for gacore.cli.run_repl: the interactive streaming REPL over the compiled graph.

The REPL is driven by an injected input_func so tests never touch real stdin or the
network. Every test builds a fresh graph over a fake LLM (bindable adapter over the
conftest fakes) and asserts the observable outcomes: the returned exit_reason, the
prompts the REPL handed to input_func, and — via capsys — the streamed node output the
user actually sees (agent tool calls, tool results, ask_user prompts, slash commands).
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, BaseMessage

from gacore.cli import run_repl
from gacore.config import Config


class _FakeInput:
    """A queue-based input_func: yields queued lines, records prompts, EOF when empty."""

    def __init__(self, *lines: str) -> None:
        self._lines = list(lines)
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


def _tool_call(name: str, args: dict[str, object], call_id: str) -> AIMessage:
    """Build an AIMessage carrying a single tool_call to a registered tool."""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def test_repl_plain_answer_returns_current_task_done(
    tmp_cfg: Config,
    scripted_llm: Callable[[list[str]], GenericFakeChatModel],
    capsys,
) -> None:
    """Given a text-only fake LLM, When the user sends one plain message, Then run_repl returns CURRENT_TASK_DONE and streams the reply."""
    llm = scripted_llm(["hello world"])
    fake_input = _FakeInput("hello world")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    assert reason == "CURRENT_TASK_DONE"
    assert "hello world" in capsys.readouterr().out


def test_quit_breaks_immediately(
    tmp_cfg: Config,
    scripted_llm: Callable[[list[str]], GenericFakeChatModel],
) -> None:
    """Given a /quit first input, When run_repl starts, Then it returns None without running the graph."""
    llm = scripted_llm(["unused"])
    fake_input = _FakeInput("/quit")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    assert reason is None


def test_streaming_prints_tool_activity(
    tmp_cfg: Config,
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel],
    capsys,
) -> None:
    """Given an agent that calls file_write then answers, When run_repl streams, Then stdout shows the tool call and its result."""
    responses = [
        _tool_call("file_write", {"path": str(tmp_cfg.temp_dir / "hello.txt"), "content": "hi"}, "call_1"),
        AIMessage(content="wrote it"),
    ]
    llm = message_llm(responses)
    fake_input = _FakeInput("write a file")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    out = capsys.readouterr().out
    assert reason == "CURRENT_TASK_DONE"
    assert "[agent] -> file_write(" in out
    assert "[tools] <-" in out
    assert "wrote it" in out


def test_streaming_renders_each_message_once(
    tmp_cfg: Config,
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel],
    capsys,
) -> None:
    """Regression: the wrapper graph (compiled subgraph + full-list cleanup node) streams
    full-state updates, so each message used to render twice per turn — and the previous
    turn's reply replayed inside the next turn. Each message must render exactly once."""
    responses = [
        _tool_call("file_write", {"path": str(tmp_cfg.temp_dir / "h.txt"), "content": "hi"}, "call_1"),
        AIMessage(content="wrote it"),
        AIMessage(content="second reply"),
    ]
    llm = message_llm(responses)
    fake_input = _FakeInput("write a file", "again")

    run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    out = capsys.readouterr().out
    # each message appears exactly once across both turns, despite full-state chunks
    assert out.count("wrote it") == 1
    assert out.count("second reply") == 1
    assert out.count("[agent] -> file_write(") == 1
    assert out.count("[tools] <-") == 1


def test_interrupt_prompts_human_and_resumes(
    tmp_cfg: Config,
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel],
    capsys,
) -> None:
    """Given an ask_user interrupt then a final answer, When the human answers, Then the graph completes and the question is printed once."""
    responses = [
        _tool_call("ask_user", {"question": "continue?", "options": ["yes", "no"]}, "call_1"),
        AIMessage(content="final answer"),
    ]
    llm = message_llm(responses)
    fake_input = _FakeInput("proceed", "go on")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    out = capsys.readouterr().out
    assert reason == "CURRENT_TASK_DONE"
    assert out.count("continue?") == 1  # printed exactly once, not echoed in the input prompt
    assert any(call.startswith("Your answer: ") for call in fake_input.calls)


def test_abort_answer_exits_repl(
    tmp_cfg: Config,
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel],
    capsys,
) -> None:
    """Given an ask_user interrupt, When the human answers an abort word, Then run_repl stops and returns EXITED."""
    responses = [_tool_call("ask_user", {"question": "keep going?"}, "call_1")]
    llm = message_llm(responses)
    fake_input = _FakeInput("proceed", "abort")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    assert reason == "EXITED"
    assert "[EXITED]" in capsys.readouterr().out


def test_help_lists_commands(
    tmp_cfg: Config,
    scripted_llm: Callable[[list[str]], GenericFakeChatModel],
    capsys,
) -> None:
    """Given a /help input, When run_repl handles it, Then all commands are listed."""
    llm = scripted_llm(["unused"])
    fake_input = _FakeInput("/help", "/quit")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    out = capsys.readouterr().out
    assert reason is None
    assert "/working" in out and "/memory" in out and "/reset" in out and "/quit" in out


def test_working_command_shows_checkpoint(
    tmp_cfg: Config,
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel],
    capsys,
) -> None:
    """Given a turn that updates the working checkpoint, When the user asks /working, Then the checkpoint is shown."""
    responses = [
        _tool_call("update_working_checkpoint", {"key_info": "need data", "related_sop": "fetch"}, "call_1"),
        AIMessage(content="ok"),
    ]
    llm = message_llm(responses)
    fake_input = _FakeInput("start task", "/working", "/quit")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    out = capsys.readouterr().out
    assert reason == "CURRENT_TASK_DONE"
    assert "need data" in out


def test_memory_command_lists_files(
    tmp_cfg: Config,
    scripted_llm: Callable[[list[str]], GenericFakeChatModel],
    capsys,
) -> None:
    """Given memory files on disk, When the user asks /memory, Then their contents are shown."""
    tmp_cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    (tmp_cfg.memory_dir / "global_mem.txt").write_text("remember apples", encoding="utf-8")
    llm = scripted_llm(["unused"])
    fake_input = _FakeInput("/memory", "/quit")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    out = capsys.readouterr().out
    assert reason is None
    assert "remember apples" in out


def test_reset_command_starts_fresh_thread(
    tmp_cfg: Config,
    scripted_llm: Callable[[list[str]], GenericFakeChatModel],
    capsys,
) -> None:
    """Given a /reset input, When run_repl handles it, Then it confirms and keeps looping."""
    llm = scripted_llm(["first answer", "second answer"])
    fake_input = _FakeInput("hello", "/reset", "again", "/quit")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    out = capsys.readouterr().out
    assert reason == "CURRENT_TASK_DONE"
    assert "Conversation reset." in out
    assert "first answer" in out and "second answer" in out


def test_unknown_command_is_reported(
    tmp_cfg: Config,
    scripted_llm: Callable[[list[str]], GenericFakeChatModel],
    capsys,
) -> None:
    """Given an unknown slash command, When run_repl handles it, Then a hint is printed and the loop continues."""
    llm = scripted_llm(["hello there"])
    fake_input = _FakeInput("/nope", "hi", "/quit")

    reason = run_repl(cfg=tmp_cfg, llm=llm, input_func=fake_input)

    out = capsys.readouterr().out
    assert reason == "CURRENT_TASK_DONE"
    assert "Unknown command" in out
    assert "hello there" in out
