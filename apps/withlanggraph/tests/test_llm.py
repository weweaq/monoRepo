"""Tests for gacore.llm: provider resolution and tool-bound model construction.

All assertions are structural; no test makes a real API call. Keys are dummies.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.runnables import RunnableBinding
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from gacore.config import ConfigError
from gacore.llm import get_llm, get_provider

_OPENAI_ENV: Mapping[str, str] = {
    "LLM_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-test-123",
    "OPENAI_BASE_URL": "",
    "OPENAI_MODEL": "gpt-4o-mini",
}

_ANTHROPIC_ENV: Mapping[str, str] = {
    "LLM_PROVIDER": "anthropic",
    "ANTHROPIC_API_KEY": "sk-ant-test-123",
    "ANTHROPIC_MODEL": "claude-sonnet-4-5",
}

_DEEPSEEK_ENV: Mapping[str, str] = {
    "LLM_PROVIDER": "deepseek",
    "DEEPSEEK_API_KEY": "sk-test-123",
    "DEEPSEEK_MODEL": "deepseek-v4-pro",
    "DEEPSEEK_BASE_URL": "",
}


class _FakeTool(BaseTool):
    """A minimal BaseTool so bind_tools has a real tool to convert to schema."""

    name: str = "fake_tool"
    description: str = "A fake tool for structural tests."

    def _run(self, x: int) -> int:
        return x


def test_get_provider_returns_lowercased_provider_from_env() -> None:
    assert get_provider({"LLM_PROVIDER": "OpenAI"}) == "openai"


def test_get_provider_raises_when_unset() -> None:
    with pytest.raises(ConfigError):
        get_provider({})


@pytest.mark.parametrize("provider", ["gemini", "ollama", ""])
def test_get_provider_raises_on_unknown_provider(provider: str) -> None:
    with pytest.raises(ConfigError):
        get_provider({"LLM_PROVIDER": provider})


def test_get_llm_openai_uses_configured_model() -> None:
    bound = get_llm([], _OPENAI_ENV)
    assert isinstance(bound, RunnableBinding)
    assert isinstance(bound.bound, ChatOpenAI)
    assert bound.bound.model_name == "gpt-4o-mini"
    assert bound.bound.temperature == 0


def test_get_llm_openai_defaults_model_when_unset() -> None:
    bound = get_llm([], {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test-123"})
    assert bound.bound.model_name == "gpt-4o"


def test_get_llm_openai_empty_base_url_uses_default() -> None:
    bound = get_llm([], _OPENAI_ENV)
    assert bound.bound.openai_api_base is None


def test_get_llm_openai_passes_through_base_url() -> None:
    env = {**_OPENAI_ENV, "OPENAI_BASE_URL": "https://api.example.com/v1"}
    bound = get_llm([], env)
    assert bound.bound.openai_api_base == "https://api.example.com/v1"


def test_get_llm_openai_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        get_llm([], {"LLM_PROVIDER": "openai"})


def test_get_llm_anthropic_uses_configured_model() -> None:
    bound = get_llm([], _ANTHROPIC_ENV)
    assert isinstance(bound, RunnableBinding)
    assert isinstance(bound.bound, ChatAnthropic)
    assert bound.bound.model == "claude-sonnet-4-5"


def test_get_llm_anthropic_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_llm([], {"LLM_PROVIDER": "anthropic"})


def test_get_llm_deepseek_uses_configured_model() -> None:
    bound = get_llm([], _DEEPSEEK_ENV)
    assert isinstance(bound, RunnableBinding)
    assert isinstance(bound.bound, ChatOpenAI)
    assert bound.bound.model_name == "deepseek-v4-pro"
    assert bound.bound.temperature == 0


def test_get_llm_deepseek_defaults_model_and_base_url_when_unset() -> None:
    bound = get_llm([], {"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "sk-test-123"})
    assert bound.bound.model_name == "deepseek-v4-pro"
    assert bound.bound.openai_api_base == "https://api.deepseek.com/v1"


def test_get_llm_deepseek_passes_through_base_url() -> None:
    env = {**_DEEPSEEK_ENV, "DEEPSEEK_BASE_URL": "https://api.deepseek.com"}
    bound = get_llm([], env)
    assert bound.bound.openai_api_base == "https://api.deepseek.com"


def test_get_llm_deepseek_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        get_llm([], {"LLM_PROVIDER": "deepseek"})


def test_get_llm_binds_tool_schema() -> None:
    bound = get_llm([_FakeTool()], _OPENAI_ENV)
    tools = bound.kwargs["tools"]
    assert [tool["function"]["name"] for tool in tools] == ["fake_tool"]


def test_get_llm_empty_tool_list_still_binds() -> None:
    bound = get_llm([], _OPENAI_ENV)
    assert isinstance(bound, RunnableBinding)
    assert bound.kwargs.get("tools") == []
