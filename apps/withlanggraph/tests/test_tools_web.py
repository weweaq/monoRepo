"""Tests for gacore.tools.web_tools — fully mocked via an injected client factory, no real network."""

from __future__ import annotations

from collections.abc import Callable
from typing import Self

import httpx
import pytest

from gacore.tools.web_tools import _fetch, web_execute_js, web_scan

_URL = "https://example.com/start"
_FINAL_URL = "https://example.com/final"


class FakeResponse:
    """Minimal httpx.Response stand-in: exposes text, status_code, url and raise_for_status."""

    def __init__(self, text: str, status_code: int = 200, url: str = _FINAL_URL) -> None:
        self.text = text
        self.status_code = status_code
        self.url = url
        self.verified = False

    def raise_for_status(self) -> None:
        self.verified = True


class FakeClient:
    """Minimal httpx.Client stand-in usable as a context manager; records setup and requests."""

    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.kwargs: dict[str, object] = {}
        self.requested_url: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str) -> FakeResponse:
        self.requested_url = url
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise AssertionError("FakeClient needs a response or an error")
        return self._response


def make_factory(client: FakeClient) -> Callable[..., FakeClient]:
    """Return a client factory that records construction kwargs and hands back the fake."""

    def factory(**kwargs: object) -> FakeClient:
        client.kwargs = kwargs
        return client

    return factory


def test_fetch_strips_html_tags_when_body_contains_markup() -> None:
    given = FakeResponse(text="<html><body><p>Hello <b>World</b></p></body></html>")
    client = FakeClient(response=given)

    when = _fetch(_URL, max_chars=8000, client_factory=make_factory(client))

    assert "<" not in when["text"]
    assert "Hello World" in when["text"]
    assert when["status_code"] == 200
    assert when["url"] == _FINAL_URL
    assert when["truncated"] is False


def test_fetch_truncates_text_at_max_chars_when_body_is_long() -> None:
    body = "<body>" + "x" * 10_000 + "</body>"
    client = FakeClient(response=FakeResponse(text=body))

    when = _fetch(_URL, max_chars=100, client_factory=make_factory(client))

    assert when["truncated"] is True
    assert len(when["text"]) <= 100
    assert len(when["text"]) == 100


def test_fetch_returns_error_dict_when_client_raises_connect_error() -> None:
    client = FakeClient(error=httpx.ConnectError("connection refused"))

    when = _fetch(_URL, max_chars=8000, client_factory=make_factory(client))

    assert when["error"] == "fetch_failed"
    assert when["url"] == _URL
    assert isinstance(when["message"], str)
    assert when["message"]


def test_fetch_configures_client_and_verifies_request() -> None:
    given = FakeResponse(text="<p>ok</p>")
    client = FakeClient(response=given)

    _fetch(_URL, max_chars=8000, client_factory=make_factory(client))

    assert client.kwargs["follow_redirects"] is True
    assert client.kwargs["timeout"] == 20.0
    headers = client.kwargs["headers"]
    assert isinstance(headers, dict)
    ua = str(headers["User-Agent"])
    assert "Mozilla/5.0" in ua
    assert "Windows NT" in ua
    assert client.requested_url == _URL
    assert given.verified is True


def test_fetch_uses_final_url_after_redirect() -> None:
    given = FakeResponse(text="<p>landed</p>", url="https://example.com/final-page")
    client = FakeClient(response=given)

    when = _fetch("https://example.com/start", max_chars=8000, client_factory=make_factory(client))

    assert when["url"] == "https://example.com/final-page"


def test_web_scan_tool_delegates_to_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    canned: dict[str, object] = {"url": _URL, "status_code": 200, "text": "hello", "truncated": False}
    monkeypatch.setattr("gacore.tools.web_tools._fetch", lambda url, max_chars: canned)

    when = web_scan.invoke({"url": _URL})

    assert web_scan.name == "web_scan"
    assert when == canned


def test_web_execute_js_returns_exact_error_dict() -> None:
    when = web_execute_js.invoke({"script": "document.title"})

    assert web_execute_js.name == "web_execute_js"
    assert when == {"error": "web_execute_js is not supported in this reimplementation (TMWebDriver removed)"}
