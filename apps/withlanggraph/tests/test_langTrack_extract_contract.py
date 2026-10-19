"""Tests for the opencode extract contract: no questions, corrective retry, audit bits."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from gacore.langTrack.extract.contract import (
    NO_QUESTION_RULE,
    corrective_message,
    detect_question,
    fingerprint,
    max_attempts,
    retryable_error,
    with_no_question_rule,
)


def _request() -> httpx.Request:
    return httpx.Request("POST", "http://opencode.test/session")


def test_no_question_rule_is_appended_once() -> None:
    prompt = "system"
    once = with_no_question_rule(prompt)
    assert once.endswith(NO_QUESTION_RULE)
    assert with_no_question_rule(once) == once


def test_detect_question_markers_and_trailing_mark() -> None:
    assert detect_question("请确认后我再继续。") == "请确认"
    assert detect_question("结果如下。需要我继续吗？") == "需要我"
    assert detect_question("直接给结果") is None
    assert detect_question("你选哪个？") == "trailing_question_mark"


def test_detect_question_ignores_fenced_code() -> None:
    text = "```\n请确认\n```\n结果已给出。"
    assert detect_question(text) is None


def test_fingerprint_is_stable_and_short() -> None:
    first = fingerprint("abc")
    assert first == fingerprint("abc")
    assert len(first) == 16
    assert first != fingerprint("abd")


def test_max_attempts_opencode_default_env_and_clamp(monkeypatch) -> None:
    oc = SimpleNamespace(_llm_type="opencode")
    monkeypatch.delenv("OPENCODE_MAX_ATTEMPTS", raising=False)
    assert max_attempts(oc) == 3
    monkeypatch.setenv("OPENCODE_MAX_ATTEMPTS", "4")
    assert max_attempts(oc) == 4
    monkeypatch.setenv("OPENCODE_MAX_ATTEMPTS", "99")
    assert max_attempts(oc) == 5
    monkeypatch.setenv("OPENCODE_MAX_ATTEMPTS", "0")
    assert max_attempts(oc) == 1
    monkeypatch.setenv("OPENCODE_MAX_ATTEMPTS", "bad")
    assert max_attempts(oc) == 3


def test_max_attempts_non_opencode_keeps_two() -> None:
    assert max_attempts(SimpleNamespace(_llm_type="zhipu")) == 2


def test_corrective_message_names_violation() -> None:
    message = corrective_message("question:请确认")
    assert "输出不符合要求" in message
    assert "question:请确认" in message
    assert "不要反问" in message


def test_retryable_error_matrix() -> None:
    request = _request()

    def _status(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError(str(code), request=request, response=httpx.Response(code, request=request))

    assert retryable_error(_status(503))
    assert retryable_error(_status(429))
    assert not retryable_error(_status(400))
    assert not retryable_error(httpx.ReadTimeout("slow", request=request))
    assert retryable_error(httpx.ConnectError("down", request=request))


def test_retryable_error_reads_structured_provider_error() -> None:
    from gacore.opencode_llm import OpencodeProviderError

    forbidden = OpencodeProviderError({"name": "APIError", "data": {"statusCode": 403, "isRetryable": False}})
    transient = OpencodeProviderError({"name": "APIError", "data": {"statusCode": 503, "isRetryable": True}})
    status_only = OpencodeProviderError({"statusCode": 429})

    assert not retryable_error(forbidden)
    assert retryable_error(transient)
    assert retryable_error(status_only)
    assert not retryable_error(OpencodeProviderError({"message": "boom"}))
    # Status wins over a contradictory flag: a 5xx/429 is transient regardless.
    assert retryable_error(OpencodeProviderError({"statusCode": 503, "isRetryable": False}))
    assert retryable_error(OpencodeProviderError({"statusCode": 429, "isRetryable": False}))
    assert not retryable_error(OpencodeProviderError({"statusCode": 403, "isRetryable": True}))
