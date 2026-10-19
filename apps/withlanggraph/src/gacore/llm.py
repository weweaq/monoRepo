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
from gacore.opencode_llm import (
    DEFAULT_MODEL as OPENCODE_DEFAULT_MODEL,
    DEFAULT_SERVER_URL as OPENCODE_DEFAULT_SERVER_URL,
    OpencodeChatModel,
)

_SUPPORTED_PROVIDERS: Final = ("openai", "anthropic", "deepseek", "zhipu", "opencode")
_DEFAULT_OPENAI_MODEL: Final = "gpt-4o"
_DEFAULT_ANTHROPIC_MODEL: Final = "claude-sonnet-4-5"
# DeepSeek is an OpenAI-compatible endpoint (GA's configure_mykey.py: "native_oai").
_DEFAULT_DEEPSEEK_MODEL: Final = "deepseek-v4-pro"
_DEFAULT_DEEPSEEK_BASE_URL: Final = "https://api.deepseek.com/v1"
# Zhipu (BigModel) is an OpenAI-compatible endpoint.
_DEFAULT_ZHIPU_MODEL: Final = "GLM-5.3-Flash"
_DEFAULT_ZHIPU_BASE_URL: Final = "https://open.bigmodel.cn/api/paas/v4"

# Per-request timeout and retry budget for every provider client. Without an explicit
# timeout the SDKs fall back to a very long default, and a stalled-but-alive connection
# can leave a single call pending far past it — that is what wedged the KG extract
# channels. Both are env-tunable (LLM_TIMEOUT / LLM_MAX_RETRIES).
_DEFAULT_LLM_TIMEOUT: Final = 300.0
_DEFAULT_LLM_MAX_RETRIES: Final = 1


class MissingApiKeyError(ValueError):
    """Raised when the active provider's API key is absent from the environment."""


def _parse_timeout(raw: str | None) -> float:
    """Parse LLM_TIMEOUT (seconds); missing/invalid/non-positive falls back to the default."""
    if raw is None:
        return _DEFAULT_LLM_TIMEOUT
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LLM_TIMEOUT
    return seconds if seconds > 0 else _DEFAULT_LLM_TIMEOUT


def _parse_max_retries(raw: str | None) -> int:
    """Parse LLM_MAX_RETRIES; missing/invalid falls back to the default, negatives clamp to 0."""
    if raw is None:
        return _DEFAULT_LLM_MAX_RETRIES
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_LLM_MAX_RETRIES


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
    timeout: float | None = None,
    max_retries: int | None = None,
    log_requests: bool = True,
) -> BaseChatModel:
    """Build a chat model for the env-named provider, optionally bound to the given tools.

    create_agent binds the tool list itself, so callers migrating to it should pass
    ``bind_tools=False`` to receive a plain (unbound) model.

    Every client is built with an explicit per-request ``timeout`` and ``max_retries``
    (LLM_TIMEOUT / LLM_MAX_RETRIES) so a stalled connection cannot hang a caller
    indefinitely. Callers whose workload differs from the interactive default — e.g.
    the KG extract channels, which run a reasoning model over a whole day of material —
    can pass ``timeout``/``max_retries`` to override the env values for that client only.

    ``log_requests=False`` skips the full request/response logger; the KG extract path
    uses it so whole-day private material stays out of ``llm_requests.jsonl`` (it keeps
    the per-attempt hash/length/usage audit instead).

    Raises MissingApiKeyError (a ValueError) when the provider's API key is missing.
    """
    source = os.environ if env is None else env
    provider = get_provider(source)
    timeout = _parse_timeout(source.get("LLM_TIMEOUT")) if timeout is None else timeout
    max_retries = (
        _parse_max_retries(source.get("LLM_MAX_RETRIES")) if max_retries is None else max(0, max_retries)
    )
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
                timeout=timeout,
                max_retries=max_retries,
            )
        case "anthropic":
            api_key = source.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise MissingApiKeyError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
            llm = ChatAnthropic(
                model=source.get("ANTHROPIC_MODEL") or _DEFAULT_ANTHROPIC_MODEL,
                api_key=api_key,
                timeout=timeout,
                max_retries=max_retries,
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
                timeout=timeout,
                max_retries=max_retries,
            )
        case "zhipu":
            api_key = source.get("ZHIPU_API_KEY")
            if not api_key:
                raise MissingApiKeyError("ZHIPU_API_KEY is required when LLM_PROVIDER=zhipu")
            llm = ChatOpenAI(
                model=source.get("ZHIPU_MODEL") or _DEFAULT_ZHIPU_MODEL,
                api_key=api_key,
                base_url=source.get("ZHIPU_BASE_URL") or _DEFAULT_ZHIPU_BASE_URL,
                temperature=0,
                timeout=timeout,
                max_retries=max_retries,
            )
        case "opencode":
            llm = OpencodeChatModel(
                server_url=source.get("OPENCODE_SERVER_URL") or OPENCODE_DEFAULT_SERVER_URL,
                model_name=source.get("OPENCODE_MODEL") or OPENCODE_DEFAULT_MODEL,
                provider_id=source.get("OPENCODE_PROVIDER_ID") or "opencode",
                agent=source.get("OPENCODE_AGENT") or None,
                timeout=timeout,
                max_retries=max_retries,
            )
        case _:
            raise ConfigError(f"LLM_PROVIDER must be one of {_SUPPORTED_PROVIDERS}, got {provider!r}")
    # Attach request-body logging at the single get_llm choke point: every model built
    # here (main agent graph, scheduled jobs, QQ trivial-reply branch) is intercepted
    # so the full payload sent to the LLM lands in logs/<YYYY-MM-DD>/llm_requests.jsonl.
    _tracked = install_llm_logging(llm, provider) if log_requests else llm
    return _tracked.bind_tools(list(tool_list)) if bind_tools else _tracked
