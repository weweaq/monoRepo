"""Tests for gacore.state: the GAState schema, its reducers, and new_state seeding."""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Required, get_args, get_origin, get_type_hints

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.pregel import Pregel

from gacore.config import Config
from gacore.state import DEFAULT_MAX_TURNS, EXIT_REASONS, GAState, new_state


@pytest.fixture()
def compiled() -> Pregel:
    """A minimal graph over GAState whose node writes nothing, for exercising reducers."""
    graph = StateGraph(GAState)
    graph.add_node("noop", lambda state: {})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    return graph.compile(checkpointer=MemorySaver())


def test_new_state_seeds_all_channels(tmp_path: object) -> None:
    """Given a user input and a test cfg, new_state seeds every channel with its initial value."""
    cfg = Config.for_tests(tmp_path)  # type: ignore[arg-type]

    state = new_state("hello world", cfg)

    assert isinstance(state, dict)
    assert [m.content for m in state["messages"]] == ["hello world"]
    assert isinstance(state["messages"][0], HumanMessage)
    assert state["working"] == {}
    assert state["current_turn"] == 0
    assert state["max_turns"] == cfg.max_turns
    assert state["done_hooks"] == []
    assert state["retry_count"] == 0
    assert state["exit_reason"] is None


def test_messages_channel_is_annotated_with_add_messages_reducer() -> None:
    """Given the GAState schema, the messages channel must declare add_messages as its reducer."""
    hints = get_type_hints(GAState, include_extras=True)
    hint = hints["messages"]
    # GAState inherits AgentState, whose messages is declared Required[Annotated[..., add_messages]];
    # unwrap the Required wrapper before checking the reducer annotation.
    if get_origin(hint) is Required:
        (hint,) = get_args(hint)

    assert get_origin(hint) is Annotated
    assert get_args(hint)[1] is add_messages


def test_working_channel_has_no_reducer() -> None:
    """Given the GAState schema, the working channel must be a plain overwrite channel."""
    hints = get_type_hints(GAState, include_extras=True)

    assert hints["working"] is dict


def test_messages_accumulate_across_invokes(compiled: StateGraph.compile) -> None:
    """Given two sequential invokes each adding a message, messages must accumulate in order."""
    config = {"configurable": {"thread_id": "accumulate"}}

    compiled.invoke({"messages": [HumanMessage(content="first")]}, config=config)
    result = compiled.invoke({"messages": [AIMessage(content="second")]}, config=config)

    assert [m.content for m in result["messages"]] == ["first", "second"]


def test_working_overwrites_across_invokes(compiled: StateGraph.compile) -> None:
    """Given two sequential invokes each setting working, the latest dict must replace the prior one."""
    config = {"configurable": {"thread_id": "overwrite"}}

    compiled.invoke({"working": {"key_info": "first", "kept": True}}, config=config)
    result = compiled.invoke({"working": {"key_info": "second"}}, config=config)

    assert result["working"] == {"key_info": "second"}


def test_exit_reasons_and_defaults() -> None:
    """Given the module constants, EXIT_REASONS and DEFAULT_MAX_TURNS must match the contract."""
    assert EXIT_REASONS == (
        "CURRENT_TASK_DONE",
        "EXITED",
        "MAX_TURNS_EXCEEDED",
        "AGENT_ERROR",
        "TIME_GUARD_EXCEEDED",
    )
    assert DEFAULT_MAX_TURNS == 40


def test_time_guard_defaults_in_new_state(tmp_path: object) -> None:
    """Given a fresh state seed, time_guard_retries must start at 0 and exit_reason at None."""
    cfg = Config.for_tests(tmp_path)  # type: ignore[arg-type]

    state = new_state("hi", cfg)

    assert state["time_guard_retries"] == 0
    assert state["exit_reason"] is None


def test_max_turns_comes_from_cfg(tmp_path: object) -> None:
    """Given a cfg with a non-default max_turns, new_state must adopt it."""
    cfg = replace(Config.for_tests(tmp_path), max_turns=7)  # type: ignore[arg-type]

    state = new_state("hi", cfg)

    assert state["max_turns"] == 7
