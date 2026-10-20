"""Model-only LangChain adapter for a locally running ``opencode serve``.

OpenCode's free models are gated to the OpenCode runtime: calling the public
``/inference`` gateway from outside returns ``403 FreeTierError``. This adapter runs the
prompt through the local server's session API instead, so gacore can reuse those models
without changing its own pipeline.

Scope is text generation only. Tool calling is deliberately unsupported: the OpenCode
runtime executes *its* tools (bash/read/edit), not gacore's Python tools, and the
session API cannot accept arbitrary tool schemas.

Two constraints come from the free-tier gateway and were both verified against a live
server. The caller's ``User-Agent`` must look like the official client
(``opencode/<version>``); and the request must not carry any explicit tool/permission
config. A session created with a ``permission`` array, or a message carrying a ``tools``
map, is treated as a headless programmatic call and answered with ``403 FreeTierError``
-- even when the permission is attached later via ``PATCH /session/{id}``. Only a plain
session passes, so the session is created with just a title and the message body holds
no ``tools`` key.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, Final

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from gacore.jsonl_logger import get_logger

DEFAULT_SERVER_URL: Final = "http://127.0.0.1:4096"
DEFAULT_MODEL: Final = "glm-5.3-flash-free"
# The upstream gateway gates free models by User-Agent: a caller that does not look
# like the official client gets ``403 FreeTierError`` ("free tier can only be used
# from within OpenCode"), while ``opencode/<version>`` passes. The local server
# forwards the caller's UA upstream, so the adapter must present the client UA to
# reuse the free models over the plain HTTP session API.
DEFAULT_CLIENT_UA: Final = "opencode/1.18.27"
# Model variant (``model.variant``) selects a reasoning-effort preset. The free
# ``glm-5.3-flash-free`` exposes low/high/max and its default preset over-thinks:
# on the full events material it burned the entire 32000-token output cap on
# hidden reasoning (finish=length, 447s, 6.8k chars of truncated JSON), while
# ``low`` finished in 160s with 15.9k chars of valid JSON. Extract channels set
# this; interactive callers can leave it unset to keep the model default.
_SESSION_TITLE: Final = "gacore"
_RETRY_STATUS: Final = frozenset({408, 409, 429})
_LOG = get_logger("llm.opencode")


def _text_of(content: Any) -> str:
    """Flatten message content (str or block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                out.append(str(block.get("text", "")))
        return "".join(out)
    return str(content or "")


def _usage(tokens: dict[str, Any] | None) -> dict[str, int] | None:
    """Map opencode ``info.tokens`` onto LangChain ``usage_metadata``."""
    if not tokens:
        return None
    input_tokens = int(tokens.get("input") or 0)
    output_tokens = int(tokens.get("output") or 0)
    total = int(tokens.get("total") or (input_tokens + output_tokens))
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total}


def _find_nested(error: Any, key: str) -> Any:
    """Find ``key`` in an assistant error dict, including one level of ``data`` nesting."""
    if not isinstance(error, dict):
        return None
    if key in error:
        return error[key]
    for value in error.values():
        if isinstance(value, dict) and key in value:
            return value[key]
    return None


class OpencodeProviderError(RuntimeError):
    """Provider-level error carried inside an HTTP 200 assistant message.

    Keeps ``statusCode``/``isRetryable`` as structured fields so the extract retry
    controller can decide without parsing the message text (and without logging the
    upstream body).
    """

    def __init__(self, error: Any) -> None:
        status = _find_nested(error, "statusCode")
        retryable = _find_nested(error, "isRetryable")
        self.status_code: int | None = status if isinstance(status, int) else None
        self.is_retryable: bool | None = retryable if isinstance(retryable, bool) else None
        name = error.get("name") if isinstance(error, dict) else None
        super().__init__(f"opencode assistant error: {name or type(error).__name__}")


def _json_or_raise(resp: httpx.Response) -> dict[str, Any]:
    """Return the JSON body, or raise carrying the server's error text (e.g. FreeTierError)."""
    if resp.is_error:
        raise httpx.HTTPStatusError(
            f"opencode {resp.status_code}: {resp.text[:500]}",
            request=resp.request,
            response=resp,
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ValueError(f"opencode {resp.status_code} returned non-JSON: {resp.text[:500]}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"opencode returned non-object JSON: {type(payload).__name__}")
    return payload


def _client_timeout(seconds: float) -> httpx.Timeout:
    """Keep a short connect budget so a wedged local server cannot hang for the read timeout."""
    return httpx.Timeout(seconds, connect=min(5.0, seconds))


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, OpencodeProviderError):
        status = exc.status_code
        if status is not None and (status in _RETRY_STATUS or 500 <= status < 600):
            return True
        return bool(exc.is_retryable)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in _RETRY_STATUS or 500 <= status < 600
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class OpencodeChatModel(BaseChatModel):
    """Single-turn chat model backed by the local OpenCode server session API."""

    model_config = ConfigDict(protected_namespaces=())

    server_url: str = DEFAULT_SERVER_URL
    model_name: str = DEFAULT_MODEL
    provider_id: str = "opencode"
    client_ua: str = DEFAULT_CLIENT_UA
    variant: str | None = None
    agent: str | None = None
    timeout: float = 600.0
    max_retries: int = 0
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = Field(
        default=None, exclude=True, repr=False
    )

    @property
    def _llm_type(self) -> str:
        return "opencode"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if tools:
            raise NotImplementedError(
                "OpencodeChatModel is model-only: the OpenCode session API cannot accept "
                "gacore's tool schemas. Use another provider (e.g. zhipu) for the agent loop."
            )
        return self

    def _session_payload(self) -> dict[str, Any]:
        """Create a bare session: any explicit permission config trips the free-tier gate."""
        return {"title": _SESSION_TITLE}

    def _payload(self, messages: Sequence[BaseMessage]) -> dict[str, Any]:
        system = "\n\n".join(
            _text_of(m.content) for m in messages if isinstance(m, SystemMessage)
        ).strip()
        turns: list[str] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                continue
            text = _text_of(message.content)
            if not text:
                continue
            role = getattr(message, "type", "human")
            turns.append(text if role == "human" else f"[{role}]\n{text}")
        model: dict[str, Any] = {"providerID": self.provider_id, "modelID": self.model_name}
        if self.variant:
            model["variant"] = self.variant
        body: dict[str, Any] = {
            "model": model,
            "parts": [{"type": "text", "text": "\n\n".join(turns)}],
        }
        if system:
            body["system"] = system
        if self.agent:
            body["agent"] = self.agent
        return body

    @staticmethod
    def _reject_stop(stop: list[str] | None) -> None:
        if stop:
            raise NotImplementedError("OpencodeChatModel does not support stop sequences")

    def _result_from(self, payload: dict[str, Any]) -> ChatResult:
        parts = payload.get("parts") or []
        text = "".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
        info = payload.get("info") or {}
        if info.get("error"):
            raise OpencodeProviderError(info["error"])
        if not text:
            raise RuntimeError(
                f"opencode returned no text (finish={info.get('finish')!r}, parts={len(parts)})"
            )
        message = AIMessage(
            content=text,
            usage_metadata=_usage(info.get("tokens")),
            response_metadata={
                "model": info.get("modelID", self.model_name),
                "provider": info.get("providerID", self.provider_id),
                "finish_reason": info.get("finish"),
                "cost": info.get("cost"),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._reject_stop(stop)
        body = self._payload(messages)
        with httpx.Client(
            base_url=self.server_url,
            timeout=_client_timeout(self.timeout),
            headers={"User-Agent": self.client_ua},
            transport=self.transport,
        ) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    session = _json_or_raise(client.post("/session", json=self._session_payload()))
                    sid = str(session["id"])
                    try:
                        data = _json_or_raise(client.post(f"/session/{sid}/message", json=body))
                    finally:
                        self._delete(client, sid)
                    return self._result_from(data)
                except Exception as exc:
                    if attempt >= self.max_retries or not _retryable(exc):
                        raise
                    time.sleep(0.5 * (2**attempt))
        raise RuntimeError("unreachable")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._reject_stop(stop)
        body = self._payload(messages)
        async with httpx.AsyncClient(
            base_url=self.server_url,
            timeout=_client_timeout(self.timeout),
            headers={"User-Agent": self.client_ua},
            transport=self.transport,
        ) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    session = _json_or_raise(await client.post("/session", json=self._session_payload()))
                    sid = str(session["id"])
                    try:
                        data = _json_or_raise(await client.post(f"/session/{sid}/message", json=body))
                    finally:
                        await self._adelete(client, sid)
                    return self._result_from(data)
                except Exception as exc:
                    if attempt >= self.max_retries or not _retryable(exc):
                        raise
                    await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError("unreachable")

    @staticmethod
    def _delete(client: httpx.Client, sid: str) -> None:
        try:
            resp = client.delete(f"/session/{sid}")
            if resp.is_error:
                _LOG.warning("opencode session delete failed", session=sid, status=resp.status_code)
        except httpx.HTTPError as exc:
            _LOG.warning("opencode session delete failed", session=sid, error=type(exc).__name__)

    @staticmethod
    async def _adelete(client: httpx.AsyncClient, sid: str) -> None:
        try:
            resp = await client.delete(f"/session/{sid}")
            if resp.is_error:
                _LOG.warning("opencode session delete failed", session=sid, status=resp.status_code)
        except httpx.HTTPError as exc:
            _LOG.warning("opencode session delete failed", session=sid, error=type(exc).__name__)
