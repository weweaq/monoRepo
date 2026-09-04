"""Tests for gacore.tools.ask_user: the interrupt-driven human-in-the-loop tool.

The tool itself is pure; interrupt() only works inside a compiled graph with a checkpointer.
These tests prove the semantics with a mini-graph: a StateGraph over a single messages
channel plus one ToolNode running ask_user, compiled with a MemorySaver. Each test uses a
fresh thread so interrupts never leak across invocations.
"""

from __future__ import annotations

import json
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.pregel import Pregel
from langgraph.types import Command, Interrupt

from gacore.tools.ask_user import ask_user


class _MiniState(TypedDict):
    """Mini-graph state: a single append-only messages channel."""

    messages: Annotated[list[BaseMessage], add_messages]


def _tool_call(question: str, tool_call_id: str, options: list[str] | None = None) -> AIMessage:
    """Build an AIMessage whose single tool_call targets ask_user."""
    args: dict[str, object] = {"question": question}
    if options is not None:
        args["options"] = options
    return AIMessage(
        content="",
        tool_calls=[{"name": "ask_user", "args": args, "id": tool_call_id, "type": "tool_call"}],
    )


def _tool_messages_by_id(state: dict) -> dict[str, dict]:
    """Map each ToolMessage's tool_call_id to the parsed JSON payload of its content."""
    return {m.tool_call_id: json.loads(m.content) for m in state["messages"] if isinstance(m, ToolMessage)}


@pytest.fixture
def graph() -> Pregel:
    """A mini-graph whose only node executes ask_user tool calls, checkpointer enabled."""
    builder = StateGraph(_MiniState)
    builder.add_node("tools", ToolNode([ask_user]))
    builder.add_edge(START, "tools")
    return builder.compile(checkpointer=MemorySaver())


@pytest.fixture
def thread_id() -> str:
    """A unique thread id so parallel test runs cannot share an interrupt checkpoint."""
    return "interrupt-thread"


def test_first_invoke_interrupts_and_exposes_question(graph: Pregel, thread_id: str) -> None:
    """Given a tool_call to ask_user, the first invoke must halt with the question as the interrupt payload."""
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke({"messages": [_tool_call("continue?", "call_1", options=["yes", "no"])]}, config)

    assert "__interrupt__" in result
    (interrupt_value,) = result["__interrupt__"]
    assert isinstance(interrupt_value, Interrupt)
    assert interrupt_value.value == {"question": "continue?", "options": ["yes", "no"]}
    assert graph.get_state(config).next == ("tools",)


def test_resume_answer_flows_into_tool_message(graph: Pregel, thread_id: str) -> None:
    """Given a resume with a plain answer, the ToolMessage must carry it as the parsed dict."""
    config = {"configurable": {"thread_id": thread_id}}
    graph.invoke({"messages": [_tool_call("continue?", "call_1")]}, config)

    result = graph.invoke(Command(resume="continue working"), config)

    by_id = _tool_messages_by_id(result)
    assert by_id == {
        "call_1": {
            "answer": "continue working",
            "question": "continue?",
            "options": [],
            "should_exit": False,
        }
    }


def test_abort_answer_sets_should_exit(graph: Pregel, thread_id: str) -> None:
    """Given a resume with an abort word, the ToolMessage payload must flag should_exit True."""
    config = {"configurable": {"thread_id": thread_id}}
    graph.invoke({"messages": [_tool_call("keep going?", "call_1")]}, config)

    result = graph.invoke(Command(resume="abort"), config)

    payload = _tool_messages_by_id(result)["call_1"]
    assert payload["answer"] == "abort"
    assert payload["should_exit"] is True


def test_resume_abort_word_is_case_insensitive(graph: Pregel, thread_id: str) -> None:
    """Given a resume with uppercase and surrounding whitespace, should_exit must still be True."""
    config = {"configurable": {"thread_id": thread_id}}
    graph.invoke({"messages": [_tool_call("keep going?", "call_1")]}, config)

    result = graph.invoke(Command(resume="  QUIT  "), config)

    payload = _tool_messages_by_id(result)["call_1"]
    assert payload["should_exit"] is True


def test_unique_tool_call_ids_pair_their_tool_messages(graph: Pregel, thread_id: str) -> None:
    """Given two tool_calls with unique ids across rounds, each ToolMessage must pair to its own id."""
    config = {"configurable": {"thread_id": thread_id}}
    graph.invoke({"messages": [_tool_call("first?", "call_1")]}, config)
    graph.invoke(Command(resume="first answer"), config)

    graph.invoke({"messages": [_tool_call("second?", "call_2")]}, config)
    result = graph.invoke(Command(resume="abort"), config)

    by_id = _tool_messages_by_id(result)
    assert by_id["call_1"]["answer"] == "first answer"
    assert by_id["call_1"]["question"] == "first?"
    assert by_id["call_1"]["should_exit"] is False
    assert by_id["call_2"]["answer"] == "abort"
    assert by_id["call_2"]["question"] == "second?"
    assert by_id["call_2"]["should_exit"] is True


def test_args_schema_requires_question_and_makes_options_optional() -> None:
    """Given the tool's args schema, question must be required and options optional."""
    schema = ask_user.args_schema

    assert "question" in schema.model_fields
    assert schema.model_fields["question"].is_required()
    assert "options" in schema.model_fields
    assert not schema.model_fields["options"].is_required()
