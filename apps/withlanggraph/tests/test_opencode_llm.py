"""Tests for gacore.opencode_llm: local OpenCode server adapter (no real server)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from gacore.llm import get_llm, get_provider
from gacore.opencode_llm import OpencodeChatModel, OpencodeEmptyResponse

_OPENCODE_ENV: Mapping[str, str] = {
    "LLM_PROVIDER": "opencode",
    "OPENCODE_SERVER_URL": "http://127.0.0.1:4999",
    "OPENCODE_MODEL": "deepseek-v4.1-flash-free",
    "OPENCODE_PROVIDER_ID": "opencode",
}


def _reply(text: str = "hello world", **info: Any) -> dict[str, Any]:
    return {
        "info": {
            "providerID": "opencode",
            "modelID": "glm-5.3-flash-free",
            "finish": "stop",
            "cost": 0,
            "tokens": {"input": 12, "output": 5, "total": 17},
            **info,
        },
        "parts": [{"type": "text", "text": text}] if text else [],
    }


def _handler(calls: list[tuple[str, str, bytes]], reply: dict[str, Any] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test"})
        if request.method == "POST" and request.url.path == "/session/ses_test/message":
            return httpx.Response(200, json=reply or _reply())
        if request.method == "DELETE" and request.url.path == "/session/ses_test":
            return httpx.Response(200, json=True)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handle)


def _messages() -> list[Any]:
    return [SystemMessage("sys prompt"), HumanMessage("user prompt")]


def test_get_provider_accepts_opencode() -> None:
    assert get_provider({"LLM_PROVIDER": "OpenCode"}) == "opencode"


def test_get_llm_opencode_uses_defaults() -> None:
    llm = get_llm([], {"LLM_PROVIDER": "opencode"})
    assert isinstance(llm, OpencodeChatModel)
    assert llm.server_url == "http://127.0.0.1:4096"
    assert llm.model_name == "glm-5.3-flash-free"
    assert llm.provider_id == "opencode"
    assert llm.client_ua == ""
    assert llm.timeout == 300.0
    assert llm.max_retries == 1


def test_get_llm_opencode_reads_env_and_explicit_budget() -> None:
    env = {**_OPENCODE_ENV, "OPENCODE_AGENT": "build"}
    llm = get_llm([], env, bind_tools=False, timeout=600.0, max_retries=0)
    assert isinstance(llm, OpencodeChatModel)
    assert llm.server_url == "http://127.0.0.1:4999"
    assert llm.model_name == "deepseek-v4.1-flash-free"
    assert llm.agent == "build"
    assert llm.timeout == 600.0
    assert llm.max_retries == 0


def test_invoke_sends_one_bare_session_then_deletes_it() -> None:
    calls: list[tuple[str, str, bytes]] = []
    llm = OpencodeChatModel(transport=_handler(calls))

    result = llm.invoke(_messages())

    assert isinstance(result, AIMessage)
    assert result.content == "hello world"
    assert result.usage_metadata == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
    }
    assert result.response_metadata["finish_reason"] == "stop"
    assert result.response_metadata["cost"] == 0

    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/session"),
        ("POST", "/session/ses_test/message"),
        ("DELETE", "/session/ses_test"),
    ]

    # A bare session: an explicit permission array makes opencode treat the run as a
    # headless call, and the free-tier gateway answers 403 FreeTierError.
    session_body = json.loads(calls[0][2])
    assert session_body == {"title": "gacore"}

    body = json.loads(calls[1][2])
    assert body["model"] == {"providerID": "opencode", "modelID": "glm-5.3-flash-free"}
    assert body["system"] == "sys prompt"
    assert body["parts"] == [{"type": "text", "text": "user prompt"}]
    # No tools map either: the same gateway 403s any message that carries one.
    assert "tools" not in body


async def test_ainvoke_sends_one_bare_session_then_deletes_it() -> None:
    calls: list[tuple[str, str, bytes]] = []
    llm = OpencodeChatModel(transport=_handler(calls))

    result = await llm.ainvoke(_messages())

    assert result.content == "hello world"
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/session"),
        ("POST", "/session/ses_test/message"),
        ("DELETE", "/session/ses_test"),
    ]
    assert "tools" not in json.loads(calls[1][2])
    assert json.loads(calls[0][2]) == {"title": "gacore"}


def test_invoke_surfaces_server_error_body_and_still_deletes() -> None:
    calls: list[tuple[str, str, bytes]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test"})
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        return httpx.Response(403, json={"error": {"type": "FreeTierError"}})

    llm = OpencodeChatModel(transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.HTTPStatusError, match="FreeTierError"):
        llm.invoke(_messages())
    assert calls[-1][:2] == ("DELETE", "/session/ses_test")


def test_invoke_retries_transient_error_with_a_fresh_session() -> None:
    calls: list[tuple[str, str, bytes]] = []
    attempts = {"message": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test"})
        if request.method == "POST" and request.url.path.endswith("/message"):
            attempts["message"] += 1
            if attempts["message"] == 1:
                return httpx.Response(500, json={"error": "transient"})
            return httpx.Response(200, json=_reply())
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        return httpx.Response(404, json={"error": "not found"})

    llm = OpencodeChatModel(transport=httpx.MockTransport(handle), max_retries=1)
    assert llm.invoke(_messages()).content == "hello world"
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/session"),
        ("POST", "/session/ses_test/message"),
        ("DELETE", "/session/ses_test"),
        ("POST", "/session"),
        ("POST", "/session/ses_test/message"),
        ("DELETE", "/session/ses_test"),
    ]


def test_invoke_raises_on_non_json_success() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test"})
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        return httpx.Response(200, text="<html>proxy error</html>")

    llm = OpencodeChatModel(transport=httpx.MockTransport(handle))
    with pytest.raises(ValueError, match="non-JSON"):
        llm.invoke(_messages())


def test_invoke_raises_on_empty_text() -> None:
    llm = OpencodeChatModel(transport=_handler([], reply=_reply(text="")))
    with pytest.raises(OpencodeEmptyResponse, match="no text"):
        llm.invoke(_messages())


def test_empty_text_is_flagged_retryable_with_finish_detail() -> None:
    """Free-tier flake: HTTP 200 with no text. Must be retryable, and carry finish/parts."""
    llm = OpencodeChatModel(transport=_handler([], reply=_reply(text="")))
    with pytest.raises(OpencodeEmptyResponse) as caught:
        llm.invoke(_messages())

    exc = caught.value
    assert isinstance(exc, RuntimeError)  # keeps the old contract for callers
    assert exc.is_retryable is True
    assert exc.finish_reason == "stop"
    assert exc.parts == 0
    assert "finish='stop'" in str(exc)


def test_invoke_retries_empty_text_when_retry_budget_allows() -> None:
    calls: list[tuple[str, str, bytes]] = []
    replies = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test"})
        if request.method == "POST" and request.url.path.endswith("/message"):
            replies["n"] += 1
            return httpx.Response(200, json=_reply(text="" if replies["n"] == 1 else "recovered"))
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        return httpx.Response(404, json={"error": "not found"})

    llm = OpencodeChatModel(transport=httpx.MockTransport(handle), max_retries=1)

    assert llm.invoke(_messages()).content == "recovered"
    assert replies["n"] == 2


def test_invoke_raises_on_assistant_error() -> None:
    llm = OpencodeChatModel(
        transport=_handler([], reply={"info": {"error": {"message": "boom"}}, "parts": []})
    )
    with pytest.raises(RuntimeError, match="assistant error"):
        llm.invoke(_messages())


def test_stop_sequences_are_rejected() -> None:
    llm = OpencodeChatModel(transport=_handler([]))
    with pytest.raises(NotImplementedError, match="stop"):
        llm.invoke(_messages(), stop=["END"])


def test_bind_tools_rejects_non_empty_tools() -> None:
    llm = OpencodeChatModel()
    with pytest.raises(NotImplementedError, match="model-only"):
        llm.bind_tools([object()])


def test_bind_tools_empty_returns_self() -> None:
    llm = OpencodeChatModel()
    assert llm.bind_tools([]) is llm


def test_resolve_llm_honors_extract_provider_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from gacore.langTrack.extract.runner import _resolve_llm

    monkeypatch.setenv("LLM_PROVIDER", "zhipu")
    monkeypatch.setenv("ZHIPU_API_KEY", "test.zhipu-secret")
    monkeypatch.setenv("KG_EXTRACT_LLM_PROVIDER", "opencode")
    monkeypatch.delenv("OPENCODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)

    llm = _resolve_llm(None)

    assert isinstance(llm, OpencodeChatModel)
    assert llm.model_name == "glm-5.3-flash-free"


def test_resolve_llm_keeps_main_provider_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_openai import ChatOpenAI

    from gacore.langTrack.extract.runner import _resolve_llm

    monkeypatch.setenv("LLM_PROVIDER", "zhipu")
    monkeypatch.setenv("ZHIPU_API_KEY", "test.zhipu-secret")
    monkeypatch.delenv("KG_EXTRACT_LLM_PROVIDER", raising=False)

    llm = _resolve_llm(None)

    assert isinstance(llm, ChatOpenAI)


def test_resolve_llm_defaults_opencode_variant_to_low(monkeypatch: pytest.MonkeyPatch) -> None:
    from gacore.langTrack.extract.runner import _resolve_llm

    monkeypatch.setenv("LLM_PROVIDER", "zhipu")
    monkeypatch.setenv("ZHIPU_API_KEY", "test.zhipu-secret")
    monkeypatch.setenv("KG_EXTRACT_LLM_PROVIDER", "opencode")
    monkeypatch.delenv("OPENCODE_VARIANT", raising=False)

    llm = _resolve_llm(None)

    assert isinstance(llm, OpencodeChatModel)
    assert llm.variant == "low"


def test_resolve_llm_honors_explicit_opencode_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    from gacore.langTrack.extract.runner import _resolve_llm

    monkeypatch.setenv("LLM_PROVIDER", "zhipu")
    monkeypatch.setenv("ZHIPU_API_KEY", "test.zhipu-secret")
    monkeypatch.setenv("KG_EXTRACT_LLM_PROVIDER", "opencode")
    monkeypatch.setenv("OPENCODE_VARIANT", "high")

    llm = _resolve_llm(None)

    assert isinstance(llm, OpencodeChatModel)
    assert llm.variant == "high"


def test_resolve_llm_defaults_variant_when_main_provider_is_opencode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gacore.langTrack.extract.runner import _resolve_llm

    monkeypatch.setenv("LLM_PROVIDER", "opencode")
    monkeypatch.delenv("KG_EXTRACT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENCODE_VARIANT", raising=False)

    llm = _resolve_llm(None)

    assert isinstance(llm, OpencodeChatModel)
    assert llm.variant == "low"


def test_invoke_sends_configured_client_user_agent_on_every_call() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test"})
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        return httpx.Response(200, json=_reply())

    llm = OpencodeChatModel(transport=httpx.MockTransport(handle), client_ua="opencode/1.18.27")
    assert llm.invoke(_messages()).content == "hello world"
    assert seen
    assert all(ua == "opencode/1.18.27" for ua in seen)


def test_invoke_omits_user_agent_override_by_default() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test"})
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        return httpx.Response(200, json=_reply())

    llm = OpencodeChatModel(transport=httpx.MockTransport(handle))
    llm.invoke(_messages())

    assert seen
    assert all("opencode/" not in ua.lower() for ua in seen)


def test_get_llm_opencode_reads_client_ua_env() -> None:
    env = {**_OPENCODE_ENV, "OPENCODE_CLIENT_UA": "opencode/9.9.9"}
    llm = get_llm([], env, bind_tools=False)
    assert isinstance(llm, OpencodeChatModel)
    assert llm.client_ua == "opencode/9.9.9"


def test_invoke_omits_variant_by_default() -> None:
    calls: list[tuple[str, str, bytes]] = []
    llm = OpencodeChatModel(transport=_handler(calls))

    llm.invoke(_messages())

    assert json.loads(calls[1][2])["model"] == {
        "providerID": "opencode",
        "modelID": "glm-5.3-flash-free",
    }


def test_invoke_sends_variant_when_set() -> None:
    calls: list[tuple[str, str, bytes]] = []
    llm = OpencodeChatModel(transport=_handler(calls), variant="low")

    llm.invoke(_messages())

    assert json.loads(calls[1][2])["model"] == {
        "providerID": "opencode",
        "modelID": "glm-5.3-flash-free",
        "variant": "low",
    }


def test_get_llm_opencode_reads_variant_env() -> None:
    env = {**_OPENCODE_ENV, "OPENCODE_VARIANT": "low"}
    llm = get_llm([], env, bind_tools=False)
    assert isinstance(llm, OpencodeChatModel)
    assert llm.variant == "low"

    assert get_llm([], _OPENCODE_ENV, bind_tools=False).variant is None
