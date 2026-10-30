"""Unit tests for gacore.tools.single_use: per-run tool call budget."""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from gacore.tools.single_use import REFUSAL_ERROR, SingleUseTool


def _inner() -> StructuredTool:
    def echo(message: str) -> str:
        return f"sent:{message}"

    return StructuredTool.from_function(func=echo, name="qq_push", description="fake push")


def test_first_call_delegates_to_the_inner_tool() -> None:
    tool = SingleUseTool.wrap(_inner(), max_calls=1)
    assert tool.invoke({"message": "hi"}) == "sent:hi"


def test_second_call_is_refused_with_a_structured_error() -> None:
    tool = SingleUseTool.wrap(_inner(), max_calls=1)
    assert tool.invoke({"message": "hi"}) == "sent:hi"

    refusal = tool.invoke({"message": "again"})

    assert refusal["error"] == REFUSAL_ERROR
    assert refusal["tool"] == "qq_push"
    assert "重复调用" in refusal["message"]


def test_budget_is_per_wrapper_instance() -> None:
    first = SingleUseTool.wrap(_inner(), max_calls=1)
    second = SingleUseTool.wrap(_inner(), max_calls=1)

    assert first.invoke({"message": "a"}) == "sent:a"
    assert second.invoke({"message": "b"}) == "sent:b"


def test_wrapper_keeps_the_inner_name_and_schema() -> None:
    wrapped = SingleUseTool.wrap(_inner(), max_calls=1)

    assert wrapped.name == "qq_push"
    assert wrapped.args_schema is not None
    assert "最多调用 1 次" in wrapped.description
