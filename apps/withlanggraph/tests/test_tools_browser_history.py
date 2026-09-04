"""Tests for gacore.tools.browser_history — fully mocked, no real Edge dependency."""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pydantic
import pytest

import gacore.tools.browser_history as bh_mod

# Ensure bh_mod is the real module, not the StructuredTool that @tool shadows in __init__.py.
if not isinstance(bh_mod, types.ModuleType):
    bh_mod = sys.modules["gacore.tools.browser_history"]

from gacore.tools.browser_history import (
    _days_to_webkit,
    _webkit_to_iso,
    browser_history,
)

# ---------------------------------------------------------------------------
# Helpers: in-memory SQLite DB that mimics the Edge urls table
# ---------------------------------------------------------------------------


class FakeDB:
    """Minimal sqlite3 connection stand-in that executes against an in-memory DB."""

    def __init__(self, rows: list[tuple]) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
            "visit_count INTEGER, last_visit_time INTEGER)"
        )
        self._conn.executemany(
            "INSERT INTO urls (url, title, visit_count, last_visit_time) VALUES (?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def execute(self, query: str, params: list | tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(query, list(params))

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_TS_8_1 = 13390000000000000  # approx 2026-08-01
_TS_7_25 = 13389400000000000  # approx 2026-07-25
_TS_7_15 = 13388500000000000  # approx 2026-07-15

_ROWS = [
    ("https://github.com/repo", "GitHub Repo", 5, _TS_8_1),
    ("https://docs.github.com/en", "GitHub Docs", 2, _TS_7_25),
    ("https://stackoverflow.com/questions", "Stack Overflow", 10, _TS_8_1),
    ("https://example.com/page", "Example Page", 1, _TS_7_15),
    ("https://raw.github.com/file", "Raw GitHub File", 3, _TS_7_25),
]


def _is_error(result: dict) -> bool:
    """Check if a result is an error dict (TypedDict doesn't support isinstance)."""
    return "error" in result


def _is_success(result: dict) -> bool:
    """Check if a result is a success dict."""
    return "entries" in result


# ---------------------------------------------------------------------------
# Tests: timestamp conversion
# ---------------------------------------------------------------------------


def test_webkit_to_iso_produces_valid_iso_format() -> None:
    result = _webkit_to_iso(_TS_8_1)
    assert isinstance(result, str)
    assert "T" in result
    assert result.endswith("+00:00")


def test_days_to_webkit_produces_smaller_value_for_larger_days() -> None:
    recent = _days_to_webkit(1)
    older = _days_to_webkit(30)
    assert recent > older


# ---------------------------------------------------------------------------
# Tests: input validation (no DB needed)
# ---------------------------------------------------------------------------


def test_invalid_fields_returns_error() -> None:
    result = browser_history.invoke({"fields": ["url", "invalid_field"]})
    assert _is_error(result)
    assert result["error"] == "invalid_fields"


def test_invalid_limit_zero_returns_error() -> None:
    result = browser_history.invoke({"limit": 0})
    assert _is_error(result)
    assert result["error"] == "invalid_limit"


def test_invalid_limit_too_large_returns_error() -> None:
    result = browser_history.invoke({"limit": 501})
    assert _is_error(result)
    assert result["error"] == "invalid_limit"


def test_unsupported_browser_raises_validation_error() -> None:
    """Pydantic rejects 'chrome' before the function body runs (Literal['edge'])."""
    with pytest.raises(pydantic.ValidationError):
        browser_history.invoke({"browser": "chrome"})


# ---------------------------------------------------------------------------
# Tests: DB not found
# ---------------------------------------------------------------------------


def test_db_not_found_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bh_mod, "_EDGE_HISTORY_PATH", Path("/nonexistent/path/History"))
    result = browser_history.invoke({})
    assert _is_error(result)
    assert result["error"] == "db_not_found"


# ---------------------------------------------------------------------------
# Tests: query with mocked DB
# ---------------------------------------------------------------------------


def _patch_db(monkeypatch: pytest.MonkeyPatch, rows: list[tuple] = _ROWS) -> None:
    """Patch module-level _resolve_db_path and _open_db to use an in-memory fake DB."""
    fake = FakeDB(rows)
    monkeypatch.setattr(bh_mod, "_resolve_db_path", lambda: Path("C:/fake/History"))
    monkeypatch.setattr(bh_mod, "_open_db", lambda path: fake)


def test_basic_query_returns_all_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    result = browser_history.invoke({"limit": 50})
    assert _is_success(result)
    assert result["total"] == 5
    assert result["browser"] == "edge"
    assert result["profile"] == "Default"
    # Ordered by last_visit_time DESC
    assert result["entries"][0]["url"] == "https://github.com/repo"


def test_keyword_filter_matches_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    result = browser_history.invoke({"keyword": "github"})
    assert _is_success(result)
    assert result["total"] == 3  # github.com, docs.github.com, raw.github.com
    for entry in result["entries"]:
        assert "github" in entry["url"].lower() or "github" in entry.get("title", "").lower()


def test_domain_filter_matches_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    result = browser_history.invoke({"domain": "github.com"})
    assert _is_success(result)
    assert result["total"] == 3
    for entry in result["entries"]:
        assert "github.com" in entry["url"]


def test_limit_caps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    result = browser_history.invoke({"limit": 2})
    assert _is_success(result)
    assert result["total"] == 2


def test_custom_fields_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    result = browser_history.invoke({"fields": ["url", "visit_count"]})
    assert _is_success(result)
    entry = result["entries"][0]
    assert "url" in entry
    assert "visit_count" in entry
    assert "title" not in entry
    assert "last_visit_time" not in entry


def test_last_visit_time_is_iso_formatted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    result = browser_history.invoke({"fields": ["url", "last_visit_time"]})
    assert _is_success(result)
    entry = result["entries"][0]
    assert isinstance(entry["last_visit_time"], str)
    assert "T" in entry["last_visit_time"]


def test_days_filter_excludes_old_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    # Use days=0 to filter out everything (all test data is old)
    result = browser_history.invoke({"days": 0})
    assert _is_success(result)
    assert result["total"] == 0


def test_combined_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    result = browser_history.invoke({"keyword": "github", "domain": "docs.github.com", "limit": 10})
    assert _is_success(result)
    assert result["total"] == 1
    assert result["entries"][0]["url"] == "https://docs.github.com/en"


def test_empty_result_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    result = browser_history.invoke({"keyword": "nonexistent-xyz-123"})
    assert _is_success(result)
    assert result["total"] == 0
    assert result["entries"] == []
