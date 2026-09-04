"""Tests for the standard langgraph ToolNode running gacore's tool registry.

The hand-written GAStatefulToolNode was replaced by langgraph.prebuilt.ToolNode (refactor
decision: use LangGraph primitives instead of reimplementing them). In langgraph 1.2.10 a
ToolNode cannot be invoked standalone — it needs a compiled graph to supply the runtime
config — so these tests run it as a one-node graph (START -> tools). They pin the
integration contract between the registered tools and the stock node: side effects run,
each ToolMessage pairs to its tool_call_id, unknown tools and tool exceptions become error
ToolMessages. Command-returning tools (ask_user / update_working_checkpoint) are exercised
at the graph level in test_graph_loop.py and directly in test_tools_memory.py.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode

from gacore.config import Config
from gacore.state import GAState
from gacore.tools import build_tool_list


def _tool_call(name: str, args: dict[str, object], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def _toolnode(tmp_cfg: Config) -> ToolNode:
    # handle_tool_errors=True matches build_graph: tool failures become error ToolMessages.
    return ToolNode(build_tool_list(tmp_cfg), handle_tool_errors=True)


def _run_toolnode(tool_node: ToolNode, messages: list[object]) -> list[object]:
    """Run the ToolNode inside a minimal compiled graph and return the appended messages."""
    builder = StateGraph(GAState)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "tools")
    return builder.compile().invoke({"messages": messages})["messages"]


def test_toolnode_file_write_executes_and_pairs_message(tmp_cfg: Config, tmp_path: Path) -> None:
    """Given a file_write tool_call, When the node runs, Then the file is written and a paired ToolMessage returned."""
    target = tmp_path / "out.txt"

    msgs = _run_toolnode(_toolnode(tmp_cfg), [_tool_call("file_write", {"path": str(target), "content": "hi"}, "call_1")])

    (msg,) = [m for m in msgs if isinstance(m, ToolMessage)]
    assert msg.tool_call_id == "call_1"
    assert '"status": "ok"' in msg.content
    assert target.read_text(encoding="utf-8") == "hi"


def test_toolnode_unknown_tool_returns_error_message(tmp_cfg: Config) -> None:
    """Given a tool_call for an unregistered name, When the node runs, Then it returns an unknown-tool error ToolMessage."""
    msgs = _run_toolnode(_toolnode(tmp_cfg), [_tool_call("no_such_tool", {}, "call_1")])

    (msg,) = [m for m in msgs if isinstance(m, ToolMessage)]
    assert msg.status == "error"
    assert msg.content.startswith("Error: no_such_tool is not a valid tool")
    assert msg.tool_call_id == "call_1"


def test_toolnode_tool_exception_becomes_error_message(tmp_cfg: Config, tmp_path: Path) -> None:
    """Given a tool_call whose tool raises, When the node runs, Then the error is surfaced in an error ToolMessage."""
    blocked = tmp_path / "adir"
    blocked.mkdir()
    msgs = _run_toolnode(_toolnode(tmp_cfg), [_tool_call("file_write", {"path": str(blocked), "content": "x"}, "call_1")])

    (msg,) = [m for m in msgs if isinstance(m, ToolMessage)]
    assert msg.status == "error"
    assert "Error" in msg.content
    assert msg.tool_call_id == "call_1"


def test_toolnode_pairs_multiple_tool_call_ids(tmp_cfg: Config, tmp_path: Path) -> None:
    """Given two tool_calls with distinct ids, When the node runs, Then each ToolMessage pairs to its own id."""
    target = tmp_path / "pair.txt"
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "file_write", "args": {"path": str(target), "content": "a"}, "id": "call_1", "type": "tool_call"},
            {"name": "no_such_tool", "args": {}, "id": "call_2", "type": "tool_call"},
        ],
    )

    msgs = _run_toolnode(_toolnode(tmp_cfg), [ai])

    by_id = {m.tool_call_id: m for m in msgs if isinstance(m, ToolMessage)}
    assert set(by_id) == {"call_1", "call_2"}
    assert '"status": "ok"' in by_id["call_1"].content
    assert by_id["call_2"].status == "error"


def test_toolnode_ai_message_without_tool_calls_returns_no_messages(tmp_cfg: Config) -> None:
    """Given a last AIMessage with no tool_calls, When the node runs, Then no ToolMessages are produced."""
    msgs = _run_toolnode(_toolnode(tmp_cfg), [AIMessage(content="no tools here")])

    assert [m for m in msgs if isinstance(m, ToolMessage)] == []
