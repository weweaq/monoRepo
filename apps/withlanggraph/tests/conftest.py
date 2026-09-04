"""Shared pytest fixtures for gacore graph tests (used by T13-T17).

Fake chat models come from langchain-core 1.5.3:
- GenericFakeChatModel: text-only; built from an iterator of str/AIMessage, advances once per call.
- FakeMessagesListChatModel: built from a list[BaseMessage], cycles through them in order, so it
  can emit AIMessages carrying tool_calls.

Both are subclassed here to support ``bind_tools``: create_agent binds the tool list
internally, so the fakes must accept (and ignore) that call and keep returning themselves.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
    GenericFakeChatModel,
)
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.pregel import Pregel

from gacore.config import Config
from gacore.tools import build_tool_list


class BindableFakeMessagesListChatModel(FakeMessagesListChatModel):
    """FakeMessagesListChatModel that tolerates create_agent's internal bind_tools call."""

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> BindableFakeMessagesListChatModel:
        return self


class BindableGenericFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel that tolerates create_agent's internal bind_tools call."""

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> BindableGenericFakeChatModel:
        return self


@pytest.fixture
def tmp_cfg(tmp_path: Path) -> Config:
    """A Config rooted at the pytest tmp dir so tests never touch real project dirs."""
    return Config.for_tests(tmp_path)


@pytest.fixture
def tool_list(tmp_cfg: Config) -> list[BaseTool]:
    """Every registered tool, built with the tmp cfg."""
    return build_tool_list(tmp_cfg)


@pytest.fixture
def scripted_llm() -> Callable[[list[str]], BindableGenericFakeChatModel]:
    """Factory: a text-only fake chat model that answers each call with the next string."""

    def _make(contents: list[str]) -> BindableGenericFakeChatModel:
        return BindableGenericFakeChatModel(messages=iter(contents))

    return _make


@pytest.fixture
def message_llm() -> Callable[[list[BaseMessage]], BindableFakeMessagesListChatModel]:
    """Factory: a fake chat model that cycles through BaseMessages, so it can emit tool_calls.

    Tool calls inside the AIMessages must use unique ids (call_1, call_2, ...) and a name/args
    pair matching a registered tool, or the ToolNode lookup will fail.
    """

    def _make(responses: list[BaseMessage]) -> BindableFakeMessagesListChatModel:
        return BindableFakeMessagesListChatModel(responses=responses)

    return _make


@pytest.fixture
def run_graph() -> Callable[..., dict]:
    """Invoke a compiled graph with a user_input string and return the final state.

    A fresh thread_id is used per call so parallel tests never share an interrupt
    checkpoint; recursion_limit defaults to 200.
    """

    def _run(graph: Pregel, user_input: str, *, recursion_limit: int = 200) -> dict:
        config = {
            "configurable": {"thread_id": f"run-graph-{uuid.uuid4().hex}"},
            "recursion_limit": recursion_limit,
        }
        return graph.invoke({"messages": [HumanMessage(content=user_input)]}, config)

    return _run
