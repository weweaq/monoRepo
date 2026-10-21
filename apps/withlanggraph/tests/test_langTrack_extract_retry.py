"""Retry/contract tests for the KG extract channel runner (no real LLM)."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage

import gacore.langTrack.extract.runner as runner
from gacore.langTrack.extract.contract import NO_QUESTION_RULE
from gacore.langTrack.extract.runner import _extract_channel, _resolve_llm
from gacore.langTrack.extract.types import ChannelSpec, ExtractContext
from gacore.opencode_llm import OpencodeEmptyResponse, OpencodeProviderError


@pytest.fixture(autouse=True)
def _clear_attempt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_MAX_ATTEMPTS", raising=False)


class FakeLLM:
    def __init__(self, replies: list[Any], llm_type: str = "opencode") -> None:
        self.replies = list(replies)
        self.calls: list[list[Any]] = []
        self._llm_type = llm_type

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(list(messages))
        item = self.replies.pop(0) if self.replies else ""
        if isinstance(item, BaseException):
            raise item
        return AIMessage(content=item)


def _spec() -> ChannelSpec:
    return ChannelSpec(
        key="test",
        prompt="test.md",
        sources=(),
        handle=lambda conn, ctx, payload: (1, {}),
    )


def _run(llm: FakeLLM):
    conn = sqlite3.connect(":memory:")
    try:
        outcome = asyncio.run(
            _extract_channel(
                _spec(),
                llm,
                "system prompt",
                "material text",
                conn,
                ExtractContext(day="2026-10-19", run_id="run1", prompt_ver="kg@v1"),
            )
        )
    finally:
        conn.close()
    return outcome


def test_opencode_question_is_retried_with_corrective_message() -> None:
    llm = FakeLLM(["你要哪个方案？", '{"nodes": [{"node_id": "n1"}]}'])

    outcome = _run(llm)

    assert outcome.status == "ok"
    assert outcome.rows == 1
    assert outcome.extra["attempts"] == 2
    assert outcome.extra["failure_reasons"] == ["question:你要哪个"]
    assert NO_QUESTION_RULE in llm.calls[0][0].content
    assert len(llm.calls[1]) == 3
    assert "输出不符合要求" in llm.calls[1][-1].content
    assert "question:你要哪个" in llm.calls[1][-1].content


def test_opencode_exhausts_attempts() -> None:
    llm = FakeLLM(["请确认后我再继续。"] * 3)

    outcome = _run(llm)

    assert outcome.status == "failed"
    assert outcome.extra["attempts"] == 3
    assert "attempts=3" in outcome.error
    assert "reasons=question:请确认" in outcome.error
    assert len(llm.calls) == 3


def test_valid_json_with_trailing_question_is_accepted() -> None:
    llm = FakeLLM(['{"nodes": []}\n需要我继续吗？'])

    outcome = _run(llm)

    assert outcome.status == "ok"
    assert outcome.extra["attempts"] == 1
    assert len(llm.calls) == 1


def test_non_opencode_keeps_single_retry_without_contract() -> None:
    llm = FakeLLM(["not json", "still not json"], llm_type="zhipu")

    outcome = _run(llm)

    assert outcome.status == "failed"
    assert outcome.extra["attempts"] == 2
    assert "json_invalid" in outcome.error
    assert len(llm.calls[1]) == 2
    assert NO_QUESTION_RULE not in llm.calls[0][0].content


def test_opencode_retries_5xx() -> None:
    request = httpx.Request("POST", "http://opencode.test/session")
    error = httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request))
    llm = FakeLLM([error, '{"nodes": []}'])

    outcome = _run(llm)

    assert outcome.status == "ok"
    assert outcome.extra["attempts"] == 2
    assert outcome.extra["failure_reasons"] == ["transport:HTTPStatusError"]


def test_opencode_retries_429() -> None:
    request = httpx.Request("POST", "http://opencode.test/session")
    error = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    llm = FakeLLM([error, '{"nodes": []}'])

    outcome = _run(llm)

    assert outcome.status == "ok"
    assert outcome.extra["attempts"] == 2
    assert outcome.extra["failure_reasons"] == ["transport:HTTPStatusError"]


def test_opencode_5xx_exhausts_attempts() -> None:
    request = httpx.Request("POST", "http://opencode.test/session")

    def _err() -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request))

    llm = FakeLLM([_err(), _err(), _err()])

    outcome = _run(llm)

    assert outcome.status == "failed"
    assert outcome.extra["attempts"] == 3
    assert "attempts=3" in outcome.error
    assert "transport:HTTPStatusError" in outcome.error
    assert len(llm.calls) == 3


def test_opencode_does_not_retry_403() -> None:
    error = OpencodeProviderError(
        {"name": "APIError", "data": {"statusCode": 403, "isRetryable": False}}
    )
    llm = FakeLLM([error])

    outcome = _run(llm)

    assert outcome.status == "failed"
    assert outcome.extra["attempts"] == 1
    assert outcome.extra["failure_reasons"] == ["transport:OpencodeProviderError"]
    assert "status=403" in outcome.error
    assert len(llm.calls) == 1


def test_opencode_retries_in_band_5xx() -> None:
    error = OpencodeProviderError(
        {"name": "APIError", "data": {"statusCode": 503, "isRetryable": True}}
    )
    llm = FakeLLM([error, '{"nodes": []}'])

    outcome = _run(llm)

    assert outcome.status == "ok"
    assert outcome.extra["attempts"] == 2
    assert outcome.extra["failure_reasons"] == ["transport:OpencodeProviderError"]


def test_opencode_retries_empty_response() -> None:
    """Empty assistant text is a transient free-tier flake, not a terminal failure."""
    llm = FakeLLM([OpencodeEmptyResponse("stop", 0), '{"nodes": []}'])

    outcome = _run(llm)

    assert outcome.status == "ok"
    assert outcome.extra["attempts"] == 2
    assert outcome.extra["failure_reasons"] == ["transport:OpencodeEmptyResponse"]


def test_transport_abort_logs_error_message_and_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed call must leave enough in app.jsonl to root-cause it without rerunning."""
    events: list[tuple[str, dict[str, Any]]] = []

    class Capture:
        def debug(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def info(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def warning(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def error(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

    monkeypatch.setattr(runner, "_LOG", Capture())
    llm = FakeLLM([RuntimeError("provider returned empty body")])

    outcome = _run(llm)

    assert outcome.status == "failed"
    assert "RuntimeError" in outcome.error
    assert "provider returned empty body" in outcome.error

    attempt = next(fields for event, fields in events if event == "kg_extract_attempt")
    assert attempt["action"] == "abort"
    assert attempt["error_type"] == "RuntimeError"
    assert attempt["error_message"] == "provider returned empty body"
    assert "provider returned empty body" in attempt["stack_trace"]
    assert attempt["context"]["channel"] == "test"

    channel = next(fields for event, fields in events if event == "kg_extract_channel")
    assert channel["status"] == "failed"
    assert channel["error_type"] == "RuntimeError"
    assert channel["error_message"] == "provider returned empty body"


def test_opencode_does_not_retry_timeout() -> None:
    request = httpx.Request("POST", "http://opencode.test/session")
    llm = FakeLLM([httpx.ReadTimeout("slow", request=request)])

    outcome = _run(llm)

    assert outcome.status == "failed"
    assert outcome.extra["attempts"] == 1
    assert outcome.extra["failure_reasons"] == ["transport:ReadTimeout"]
    assert "ReadTimeout" in outcome.error


def test_non_opencode_retries_5xx() -> None:
    request = httpx.Request("POST", "http://zhipu.test/v1")
    error = httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request))
    llm = FakeLLM([error, '{"nodes": []}'], llm_type="zhipu")

    outcome = _run(llm)

    assert outcome.status == "ok"
    assert outcome.extra["attempts"] == 2
    assert outcome.extra["failure_reasons"] == ["transport:HTTPStatusError"]


def test_non_opencode_does_not_retry_timeout() -> None:
    request = httpx.Request("POST", "http://zhipu.test/v1")
    llm = FakeLLM([httpx.ReadTimeout("slow", request=request)], llm_type="zhipu")

    outcome = _run(llm)

    assert outcome.status == "failed"
    assert outcome.extra["attempts"] == 1
    assert outcome.extra["failure_reasons"] == ["transport:ReadTimeout"]


def test_per_attempt_audit_records_action_and_base(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class Capture:
        def debug(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def info(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def warning(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def error(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

    monkeypatch.setattr(runner, "_LOG", Capture())
    llm = FakeLLM(["你要哪个方案？", '{"nodes": []}'])

    outcome = _run(llm)

    attempts = [fields for event, fields in events if event == "kg_extract_attempt"]
    assert outcome.status == "ok"
    assert attempts[0]["action"] == "retry"
    assert attempts[0]["day"] == "2026-10-19"
    assert attempts[0]["run_id"] == "run1"
    assert attempts[1]["action"] == "accept"


def test_resolve_llm_disables_full_request_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KG_EXTRACT_LLM_PROVIDER", "zhipu")
    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")

    llm = _resolve_llm(None)

    assert not getattr(llm, "_gacore_llm_log_installed", False)
