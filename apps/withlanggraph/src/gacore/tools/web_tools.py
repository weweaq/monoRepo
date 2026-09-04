"""Web tools for gacore: static-HTTP substitutes for GA's TMWebDriver-based tools.

Honesty note: the original GenericAgent drove a real browser via TMWebDriver. This
reimplementation has no browser driver, so web_scan fetches static HTML text over HTTP
and web_execute_js is an explicit no-op. Never raises on network failure; returns error
dicts instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final, TypedDict

import httpx
from langchain_core.tools import tool

_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")


class WebScanResult(TypedDict):
    """Successful scan: final URL after redirects, HTTP status, stripped text and truncation flag."""

    url: str
    status_code: int
    text: str
    truncated: bool


class WebScanError(TypedDict):
    """Failed scan: machine-readable error tag, message and the originally requested URL."""

    error: str
    message: str
    url: str | None


class UnsupportedToolResult(TypedDict):
    """A tool explicitly unsupported in this reimplementation; carries only the error tag."""

    error: str


def _strip_tags(text: str) -> str:
    """Remove HTML tags and collapse whitespace runs to single spaces."""
    without_tags = _TAG_PATTERN.sub(" ", text)
    return _WHITESPACE_PATTERN.sub(" ", without_tags).strip()


def _fetch(
    url: str, max_chars: int, client_factory: Callable[..., httpx.Client] = httpx.Client
) -> WebScanResult | WebScanError:
    """Fetch url as tag-stripped text; returns an error dict on any httpx failure (never raises)."""
    try:
        with client_factory(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return WebScanError(error="fetch_failed", message=str(exc), url=url)
    stripped = _strip_tags(response.text)
    return WebScanResult(
        url=str(response.url),
        status_code=response.status_code,
        text=stripped[:max_chars],
        truncated=len(stripped) > max_chars,
    )


@tool
def web_scan(url: str, max_chars: int = 8000) -> WebScanResult | WebScanError:
    """Fetch a page and return its text without HTML tags.

    HTTP substitute — GA used TMWebDriver real browser; this returns static HTML text only.
    """
    return _fetch(url, max_chars)


@tool
def web_execute_js(script: str, url: str | None = None) -> UnsupportedToolResult:
    """Execute JavaScript in a browser tab.

    Not supported in this reimplementation (TMWebDriver removed).
    """
    return UnsupportedToolResult(
        error="web_execute_js is not supported in this reimplementation (TMWebDriver removed)"
    )
