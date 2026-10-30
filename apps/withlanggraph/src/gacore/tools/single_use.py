"""Per-run call budget for tools that must fire at most once.

A proactive run should never be able to call ``qq_push`` twice (or reach for file,
shell or web tools). The prompt alone cannot guarantee that, so the graph is built
with a wrapped tool whose counter lives on the wrapper instance: one graph per
proactive run, one budget per graph.
"""

from __future__ import annotations

from typing import Any, Final

from langchain_core.tools import BaseTool, StructuredTool

REFUSAL_ERROR: Final = "tool_budget_exhausted"


class SingleUseTool(StructuredTool):
    """``StructuredTool`` wrapper that refuses calls beyond ``max_calls``."""

    @classmethod
    def wrap(cls, tool: BaseTool, *, max_calls: int = 1) -> SingleUseTool:
        calls = 0

        def _guarded(**kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls > max_calls:
                return {
                    "error": REFUSAL_ERROR,
                    "message": f"{tool.name} 本轮最多调用 {max_calls} 次，重复调用已拒绝",
                    "tool": tool.name,
                }
            return tool.invoke(kwargs)

        return cls.from_function(
            func=_guarded,
            name=tool.name,
            description=f"{tool.description}\n\n本轮最多调用 {max_calls} 次，重复调用会被拒绝。",
            args_schema=tool.args_schema,
        )
