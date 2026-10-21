"""Shared pytest fixtures for gacore graph tests (used by T13-T17).

Fake chat models come from langchain-core 1.5.3:
- GenericFakeChatModel: text-only; built from an iterator of str/AIMessage, advances once per call.
- FakeMessagesListChatModel: built from a list[BaseMessage], cycles through them in order, so it
  can emit AIMessages carrying tool_calls.

Both are subclassed here to support ``bind_tools``: create_agent binds the tool list
internally, so the fakes must accept (and ignore) that call and keep returning themselves.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
    GenericFakeChatModel,
)
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.pregel import Pregel

if TYPE_CHECKING:
    from gacore.config import Config

os.environ["GACORE_LOG_ROOT"] = tempfile.mkdtemp(prefix="gacore-pytest-logs-")
# 心跳目录隔离：开发者 shell 里的 GACORE_RUN_DIR 会把心跳写进真实目录，测试一律清掉；
# 需要自定义目录的用例自行 monkeypatch.setenv("GACORE_RUN_DIR", ...)。
os.environ.pop("GACORE_RUN_DIR", None)

from gacore import jsonl_logger as _jsonl_logger  # noqa: E402

# 若在 conftest 生效前已有模块按旧环境建过 sink，显式重置成按测试根目录重建。
_jsonl_logger._sink = None


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_log_root():
    """会话结束后删掉临时日志目录，别在 %TEMP% 里越堆越多。"""
    root = os.environ["GACORE_LOG_ROOT"]
    yield
    shutil.rmtree(root, ignore_errors=True)


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
    from gacore.config import Config as _Config

    return _Config.for_tests(tmp_path)


@pytest.fixture
def tool_list(tmp_cfg: Config) -> list[BaseTool]:
    """Every registered tool, built with the tmp cfg."""
    from gacore.tools import build_tool_list

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


@pytest.fixture(autouse=True)
def _isolated_place_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离手工语义配置（place_semantics.json）：全测试封闭，绝不读真实 data/。

    需要语义叠加的用例自行 monkeypatch place_semantics.CONFIG_PATH 到 tmp 文件。
    """
    from gacore.langTrack import place_semantics as _ps

    monkeypatch.setattr(_ps, "CONFIG_PATH", tmp_path / "absent_place_semantics.json")
