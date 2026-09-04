"""End-to-end tests for gacore.graph: the full compiled loop driven by a scripted fake LLM.

These prove the whole stack works together — user input -> agent -> tools -> working memory ->
long-term memory -> final answer — with zero network calls. Every response comes from
FakeMessagesListChatModel (langchain-core 1.5.3), which cycles through BaseMessages in order,
so each agent turn can emit AIMessages carrying tool_calls with unique ids.

The file_write / update_working_checkpoint / start_long_term_update / ask_user tool calls use
exactly the args the registered tool schemas accept (tests/test_tools_memory.py pins them).

Known src seam (reported, not fixed): start_long_term_update's `_cfg` injection parameter is
excluded from its args schema, so a graph-driven tool.invoke can never pass a Config through —
the tool falls back to Config.default(). test_full_agent_session therefore overrides
GACORE_MEMORY_DIR (the config system's official env override) so the fallback resolves under
tmp_cfg.memory_dir instead of the project's real memory/ dir. Without it the test would write
into the repo.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command, Interrupt

from gacore.config import Config
from gacore.graph import build_graph
from gacore.state import new_state


def _tool_call(name: str, args: dict[str, object], call_id: str) -> AIMessage:
    """Build an AIMessage carrying a single tool_call to a registered tool."""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _thread_config() -> dict:
    """A fresh thread config with a high recursion limit so loops never cap spuriously."""
    return {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 200}


def test_full_agent_session(
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel],
    tmp_cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a 4-turn scripted session (file write, checkpoint, long-term memory, answer), When the compiled graph runs, Then the file, both memories, and exit reason all land."""
    monkeypatch.setenv("GACORE_MEMORY_DIR", str(tmp_cfg.memory_dir))
    target = tmp_path / "hello.txt"
    responses: list[BaseMessage] = [
        _tool_call(
            "file_write",
            {"path": str(target), "content": "Hello from gacore", "mode": "overwrite"},
            "call_1",
        ),
        _tool_call("update_working_checkpoint", {"key_info": "greeting file written"}, "call_2"),
        _tool_call("start_long_term_update", {"topic": "wrote greeting file"}, "call_3"),
        AIMessage(content="Done. The greeting file has been written."),
    ]
    graph = build_graph(llm=message_llm(responses), cfg=tmp_cfg)

    result = graph.invoke(new_state("write a greeting file", tmp_cfg), _thread_config())

    assert result["exit_reason"] == "CURRENT_TASK_DONE"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "Hello from gacore"
    assert result["working"]["key_info"] == "greeting file written"
    facts = (tmp_cfg.memory_dir / "global_mem.txt").read_text(encoding="utf-8")
    insights = (tmp_cfg.memory_dir / "global_mem_insight.txt").read_text(encoding="utf-8")
    assert "wrote greeting file" in facts
    assert "wrote greeting file" in insights
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 3
    assert {m.tool_call_id for m in tool_messages} == {"call_1", "call_2", "call_3"}


def test_ask_user_interrupt_roundtrip(
    message_llm: Callable[[list[BaseMessage]], FakeMessagesListChatModel],
    tmp_cfg: Config,
) -> None:
    """Given an ask_user tool_call resumed with a non-abort answer, When the loop resumes on the same thread, Then the answer flows into the ToolMessage and the task completes."""
    responses: list[BaseMessage] = [
        _tool_call("ask_user", {"question": "Proceed?", "options": ["yes", "no"]}, "call_1"),
        AIMessage(content="ok continuing"),
    ]
    graph = build_graph(llm=message_llm(responses), cfg=tmp_cfg)
    config = _thread_config()

    first = graph.invoke(new_state("ask me something", tmp_cfg), config)

    assert "__interrupt__" in first
    (interrupt_value,) = first["__interrupt__"]
    assert isinstance(interrupt_value, Interrupt)
    assert interrupt_value.value == {"question": "Proceed?", "options": ["yes", "no"]}

    result = graph.invoke(Command(resume="yes"), config)

    assert result["exit_reason"] == "CURRENT_TASK_DONE"
    tool_message = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "call_1")
    payload = json.loads(tool_message.content)
    assert payload["answer"] == "yes"
    assert payload["should_exit"] is False
