"""LLM factory: build a provider-specific chat model bound to the given tools.

Replaces GenericAgent's NativeToolClient/mykey.py path with standard LangChain
constructors driven by environment variables (see .env.example).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Final

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from gacore.config import ConfigError
from gacore.llm_request_log import install_llm_logging

_SUPPORTED_PROVIDERS: Final = ("openai", "anthropic", "deepseek")
_DEFAULT_OPENAI_MODEL: Final = "gpt-4o"
_DEFAULT_ANTHROPIC_MODEL: Final = "claude-sonnet-4-5"
# DeepSeek is an OpenAI-compatible endpoint (GA's configure_mykey.py: "native_oai").
_DEFAULT_DEEPSEEK_MODEL: Final = "deepseek-v4-pro"
_DEFAULT_DEEPSEEK_BASE_URL: Final = "https://api.deepseek.com/v1"


class MissingApiKeyError(ValueError):
    """Raised when the active provider's API key is absent from the environment."""


def get_provider(env: Mapping[str, str] | None = None) -> str:
    """Return the lowercased LLM provider name from the environment.

    Raises ConfigError when LLM_PROVIDER is unset or not a supported provider.
    """
    source = os.environ if env is None else env
    provider = (source.get("LLM_PROVIDER") or "").strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER must be one of {_SUPPORTED_PROVIDERS}, got {provider!r}")
    return provider


def get_llm(
    tool_list: Sequence[BaseTool],
    env: Mapping[str, str] | None = None,
    *,
    bind_tools: bool = True,
) -> BaseChatModel:
    """Build a chat model for the env-named provider, optionally bound to the given tools.

    create_agent binds the tool list itself, so callers migrating to it should pass
    ``bind_tools=False`` to receive a plain (unbound) model.

    Raises MissingApiKeyError (a ValueError) when the provider's API key is missing.
    """
    source = os.environ if env is None else env
    provider = get_provider(source)
    match provider:  # noqa: MATCH_OK - provider is a validated str, not a closed union
        case "openai":
            api_key = source.get("OPENAI_API_KEY")
            if not api_key:
                raise MissingApiKeyError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
            llm: BaseChatModel = ChatOpenAI(
                model=source.get("OPENAI_MODEL") or _DEFAULT_OPENAI_MODEL,
                api_key=api_key,
                base_url=source.get("OPENAI_BASE_URL") or None,
                temperature=0,
            )
        case "anthropic":
            api_key = source.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise MissingApiKeyError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
            llm = ChatAnthropic(
                model=source.get("ANTHROPIC_MODEL") or _DEFAULT_ANTHROPIC_MODEL,
                api_key=api_key,
            )
        case "deepseek":
            api_key = source.get("DEEPSEEK_API_KEY")
            if not api_key:
                raise MissingApiKeyError("DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek")
            llm = ChatOpenAI(
                model=source.get("DEEPSEEK_MODEL") or _DEFAULT_DEEPSEEK_MODEL,
                api_key=api_key,
                base_url=source.get("DEEPSEEK_BASE_URL") or _DEFAULT_DEEPSEEK_BASE_URL,
                temperature=0,
            )
        case _:
            raise ConfigError(f"LLM_PROVIDER must be one of {_SUPPORTED_PROVIDERS}, got {provider!r}")
    # Attach request-body logging at the single get_llm choke point: every model built
    # here (main agent graph, scheduled jobs, QQ trivial-reply branch) is intercepted
    # so the full payload sent to the LLM lands in logs/<YYYY-MM-DD>/llm_requests.jsonl.
    _tracked = install_llm_logging(llm, provider)
    return _tracked.bind_tools(list(tool_list)) if bind_tools else _tracked
