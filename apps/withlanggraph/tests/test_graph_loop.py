"""Loop-level tests for gacore.graph: the compiled GAState wiring, all 5 exit scenarios.

These prove the full GA agent-loop port (agent_loop.py:42-107 semantics): tool calls
route through the tools node and back, plain answers go through the final validator,
ask_user interrupts pause and resume, max_turns caps the loop, done_hooks continue the
conversation, empty responses retry, and an LLM failure exits cleanly. Every test
builds a fresh graph over a fresh fake LLM + MemorySaver and a unique thread so
checkpoints never collide.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from conftest import BindableFakeMessagesListChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.types import Command, Interrupt

from gacore.config import Config
from gacore.graph import build_graph, run_once
from gacore.state import new_state


class _RaisingFake(BindableFakeMessagesListChatModel):
    """A fake chat model whose invoke always raises, driving the AGENT_ERROR path."""

    def __init__(self) -> None:
        super().__init__(responses=[AIMessage(content="unused")])

    def invoke(self, prompt: object, **kwargs: object) -> AIMessage:
        raise RuntimeError("simulated provider outage")


def _tool_call(name: str, args: dict[str, object], call_id: str) -> AIMessage:
    """Build an AIMessage carrying a single tool_call to a registered tool."""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _thread_config() -> dict:
    """A fresh thread config with a high recursion limit so loops never cap spuriously."""
    return {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 200}


def _human_count(messages: list[BaseMessage]) -> int:
    """Count HumanMessages (the seed input plus any corrective/continuation prompts)."""
    return sum(isinstance(m, HumanMessage) for m in messages)


def test_tool_loop_then_final_answer_marks_task_done(
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel],
    tmp_cfg: Config,
    tmp_path: Path,
) -> None:
    """Given a scripted file_write tool_call then a plain answer, When the graph runs, Then the tool executes and the loop ends with CURRENT_TASK_DONE."""
    target = tmp_path / "notes.txt"
    responses: list[BaseMessage] = [
        _tool_call("file_write", {"path": str(target), "content": "hello"}, "call_1"),
        AIMessage(content="Done writing file"),
    ]
    graph = build_graph(llm=message_llm(responses), cfg=tmp_cfg)

    result = run_once(graph, "write a file")

    assert result["exit_reason"] == "CURRENT_TASK_DONE"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "hello"
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "call_1" for m in result["messages"])


def test_ask_user_abort_exits_loop(
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel], tmp_cfg: Config
) -> None:
    """Given an ask_user tool_call resumed with an abort word, When the loop resumes, Then exit_reason is EXITED."""
    responses: list[BaseMessage] = [
        _tool_call("ask_user", {"question": "continue?", "options": ["yes", "no"]}, "call_1"),
    ]
    graph = build_graph(llm=message_llm(responses), cfg=tmp_cfg)
    config = _thread_config()

    first = graph.invoke(new_state("proceed", tmp_cfg), config)

    assert "__interrupt__" in first
    (interrupt_value,) = first["__interrupt__"]
    assert isinstance(interrupt_value, Interrupt)
    assert interrupt_value.value == {"question": "continue?", "options": ["yes", "no"]}

    result = graph.invoke(Command(resume="abort"), config)

    assert result["exit_reason"] == "EXITED"
    tool_message = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "call_1")
    assert '"should_exit": true' in tool_message.content


def test_max_turns_guard_stops_endless_tool_loop(
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel], tmp_cfg: Config
) -> None:
    """Given an LLM that always emits tool_calls and max_turns=2, When the graph runs, Then it exits with MAX_TURNS_EXCEEDED."""
    responses: list[BaseMessage] = [
        _tool_call("update_working_checkpoint", {"key_info": "still going"}, f"call_{i}") for i in range(5)
    ]
    graph = build_graph(llm=message_llm(responses), cfg=tmp_cfg)

    result = run_once(graph, "loop forever", max_turns=2)

    assert result["exit_reason"] == "MAX_TURNS_EXCEEDED"
    assert result["current_turn"] == 2


def test_done_hooks_fire_before_completion(
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel], tmp_cfg: Config
) -> None:
    """Given two pending done_hooks behind three answers, When the graph runs, Then each hook fires once before CURRENT_TASK_DONE."""
    responses: list[BaseMessage] = [
        AIMessage(content="answer1"),
        AIMessage(content="answer2"),
        AIMessage(content="final answer"),
    ]
    graph = build_graph(llm=message_llm(responses), cfg=tmp_cfg)

    result = run_once(graph, "task", done_hooks=["hook1 text", "hook2 text"])

    assert result["exit_reason"] == "CURRENT_TASK_DONE"
    assert result["current_turn"] == 3
    hooks_fired = [m.content for m in result["messages"] if isinstance(m, HumanMessage) and "hook" in m.content]
    assert hooks_fired == ["hook1 text", "hook2 text"]


def test_empty_response_retries_then_completes(
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel], tmp_cfg: Config
) -> None:
    """Given an empty first answer, When validated, Then a corrective prompt is appended and the second answer completes the task."""
    responses: list[BaseMessage] = [
        AIMessage(content=""),
        AIMessage(content="good answer"),
    ]
    graph = build_graph(llm=message_llm(responses), cfg=tmp_cfg)

    result = run_once(graph, "task")

    assert result["exit_reason"] == "CURRENT_TASK_DONE"
    assert _human_count(result["messages"]) == 2
    assert result["retry_count"] == 0


def test_ask_user_resume_continue_loops_to_completion(
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel], tmp_cfg: Config
) -> None:
    """Given an ask_user tool_call resumed with a non-abort word, When the loop continues, Then the final answer completes with CURRENT_TASK_DONE."""
    responses: list[BaseMessage] = [
        _tool_call("ask_user", {"question": "continue?"}, "call_1"),
        AIMessage(content="final"),
    ]
    graph = build_graph(llm=message_llm(responses), cfg=tmp_cfg)
    config = _thread_config()

    graph.invoke(new_state("proceed", tmp_cfg), config)
    result = graph.invoke(Command(resume="go on"), config)

    assert result["exit_reason"] == "CURRENT_TASK_DONE"
    tool_message = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "call_1")
    assert "go on" in tool_message.content


def test_agent_llm_error_exits_with_agent_error(tmp_cfg: Config) -> None:
    """Given an LLM that raises, When the agent node runs, Then the loop exits cleanly with AGENT_ERROR."""
    graph = build_graph(llm=_RaisingFake(), cfg=tmp_cfg)

    result = run_once(graph, "task")

    assert result["exit_reason"] == "AGENT_ERROR"
