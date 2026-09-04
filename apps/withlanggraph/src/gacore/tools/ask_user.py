"""Human-in-the-loop tool: ask the user a question via a LangGraph interrupt.

Maps to GA's do_ask_user (INTERRUPT / HUMAN_INTERVENTION). The tool is pure: the first call
raises a resumable interrupt carrying the question, and once the graph resumes with the
human's answer the tool returns a Command whose update pairs a JSON ToolMessage to the
originating tool_call_id and — for abort answers — sets exit_reason="EXITED". No goto is
used: routing is driven solely by the agent node's conditional edge reading exit_reason.
"""

from __future__ import annotations

import json
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command, interrupt

_ABORT_WORDS: frozenset[str] = frozenset({"abort", "exit", "quit", "stop", "cancel"})


@tool
def ask_user(
    question: str,
    options: list[str] | None = None,
    tool_call_id: Annotated[str | None, InjectedToolCallId] = None,
) -> Command:
    """Ask the user a question and wait for their answer.

    Pauses the graph with interrupt() so a human can respond; the resumed value becomes the
    returned answer. The ToolMessage content is a JSON payload {answer, question, options,
    should_exit}; when the answer is an abort/exit word the update also sets
    exit_reason="EXITED", terminating the turn loop.
    """
    answer = interrupt({"question": question, "options": options})
    should_exit = answer is None or bool(answer.strip().lower() in _ABORT_WORDS)
    payload = {
        "answer": answer,
        "question": question,
        "options": options or [],
        "should_exit": should_exit,
    }
    update: dict[str, object] = {
        "messages": [ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=tool_call_id)]
    }
    if should_exit:
        update["exit_reason"] = "EXITED"
    return Command(update=update)
