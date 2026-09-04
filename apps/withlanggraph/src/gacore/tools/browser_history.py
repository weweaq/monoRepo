"""Browser history tool for gacore: read Edge browsing history from its SQLite database.

Edge (Chromium kernel) stores history in a SQLite DB at
%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\Default\\History. This tool queries
that DB for url / title / visit_count / last_visit_time, with optional keyword,
domain and time-range filtering. When Edge is running the DB is locked, so we
copy it to a temp directory first.

Honesty note: only Edge is supported today. Chrome shares the same schema but
the path differs; Firefox uses a different schema (places.sqlite) and is out
of scope.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, TypedDict

from langchain_core.tools import tool

from gacore.jsonl_logger import get_logger

logger = get_logger("tools.browser_history")

# WebKit epoch: 1601-01-01 00:00:00 UTC
_WEBKIT_EPOCH_OFFSET: Final = 11644473600  # seconds between 1601-01-01 and 1970-01-01
_WEBKIT_MICROS_PER_SEC: Final = 1_000_000

_DEFAULT_FIELDS: Final = ("url", "title", "visit_count", "last_visit_time")
_VALID_FIELDS: Final = frozenset(_DEFAULT_FIELDS)
_MAX_LIMIT: Final = 500

_EDGE_HISTORY_PATH: Final = Path(
    os.environ.get("LOCALAPPDATA", "")
) / "Microsoft" / "Edge" / "User Data" / "Default" / "History"


class HistoryEntry(TypedDict, total=False):
    """A single history entry with configurable fields."""

    url: str
    title: str
    visit_count: int
    last_visit_time: str  # ISO 8601 UTC


class BrowserHistoryResult(TypedDict):
    """Successful query: matched entries, total count and the query parameters used."""

    entries: list[HistoryEntry]
    total: int
    browser: str
    profile: str


class BrowserHistoryError(TypedDict):
    """Failed query: machine-readable error tag, message and optional detail."""

    error: str
    message: str
    detail: str | None


def _webkit_to_iso(webkit_micros: int) -> str:
    """Convert a WebKit timestamp (microseconds since 1600-01-01) to ISO 8601 UTC string."""
    unix_seconds = webkit_micros / _WEBKIT_MICROS_PER_SEC - _WEBKIT_EPOCH_OFFSET
    dt = datetime.fromtimestamp(unix_seconds, tz=UTC)
    return dt.isoformat()


def _days_to_webkit(days: int) -> int:
    """Convert 'N days ago' to a WebKit timestamp threshold."""
    now_unix = time.time()
    threshold_unix = now_unix - (days * 86400)
    return int((threshold_unix + _WEBKIT_EPOCH_OFFSET) * _WEBKIT_MICROS_PER_SEC)


def _resolve_db_path() -> Path:
    """Return the Edge history DB path, raising FileNotFoundError if absent."""
    if not _EDGE_HISTORY_PATH.is_file():
        raise FileNotFoundError(
            f"Edge history DB not found at {_EDGE_HISTORY_PATH}. "
            "Ensure Edge has been used at least once."
        )
    return _EDGE_HISTORY_PATH


def _open_db(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection to the history DB, copying to temp if locked."""
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        # Edge is running and locked the DB — copy to temp and open that instead.
        tmp = Path(tempfile.gettempdir()) / f"gacore_edge_history_{os.getpid()}.db"
        shutil.copy2(db_path, tmp)
        logger.info("Edge DB locked, copied to temp", temp_path=str(tmp))
        return sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=5)


def _build_query(
    fields: tuple[str, ...],
    keyword: str | None,
    domain: str | None,
    days: int | None,
    limit: int,
) -> tuple[str, list]:
    """Build the SQL query and parameter list from filter criteria."""
    select_cols = ", ".join(fields)
    query = f"SELECT {select_cols} FROM urls WHERE 1=1"
    params: list = []

    if keyword:
        query += " AND (url LIKE ? OR title LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw])

    if domain:
        query += " AND url LIKE ?"
        params.append(f"%{domain}%")

    if days is not None:
        threshold = _days_to_webkit(days)
        query += " AND last_visit_time >= ?"
        params.append(threshold)

    query += " ORDER BY last_visit_time DESC LIMIT ?"
    params.append(limit)

    return query, params


def _row_to_entry(row: tuple, fields: tuple[str, ...]) -> HistoryEntry:
    """Convert a raw DB row into a HistoryEntry dict, converting timestamps."""
    entry: HistoryEntry = {}
    for i, field in enumerate(fields):
        value = row[i]
        if field == "last_visit_time" and isinstance(value, int):
            value = _webkit_to_iso(value)
        entry[field] = value  # type: ignore[literal-required]
    return entry


@tool
def browser_history(
    browser: Literal["edge"] = "edge",
    keyword: str | None = None,
    domain: str | None = None,
    days: int | None = None,
    limit: int = 50,
    fields: list[str] | None = None,
) -> BrowserHistoryResult | BrowserHistoryError:
    """Read Edge browsing history with optional filtering.

    Queries the Edge SQLite history database for visited URLs, returning
    url, title, visit count and last visit time. Supports filtering by
    keyword (url/title substring), domain, and time range. When Edge is
    running the database is copied to a temp file first to avoid lock errors.
    """
    # Validate fields
    requested = tuple(fields) if fields else _DEFAULT_FIELDS
    invalid = set(requested) - _VALID_FIELDS
    if invalid:
        return BrowserHistoryError(
            error="invalid_fields",
            message=f"Unsupported fields: {sorted(invalid)}. Valid: {sorted(_VALID_FIELDS)}",
            detail=None,
        )

    # Validate limit
    if limit < 1 or limit > _MAX_LIMIT:
        return BrowserHistoryError(
            error="invalid_limit",
            message=f"limit must be between 1 and {_MAX_LIMIT}, got {limit}",
            detail=None,
        )

    # Validate browser
    if browser != "edge":
        return BrowserHistoryError(
            error="unsupported_browser",
            message=f"Only 'edge' is supported, got {browser!r}",
            detail=None,
        )

    # Resolve DB path
    try:
        db_path = _resolve_db_path()
    except FileNotFoundError as exc:
        logger.error("browser_history: Edge DB not found", error_type="FileNotFoundError", stack_trace=str(exc))
        return BrowserHistoryError(error="db_not_found", message=str(exc), detail=None)

    # Open and query
    try:
        conn = _open_db(db_path)
    except sqlite3.Error as exc:
        logger.error(
            "browser_history: failed to open DB",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
            context={"db_path": str(db_path)},
        )
        return BrowserHistoryError(error="db_open_failed", message=str(exc), detail=None)

    try:
        query, params = _build_query(requested, keyword, domain, days, limit)
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        entries = [_row_to_entry(row, requested) for row in rows]
        logger.info(
            "browser_history query success",
            total=len(entries),
            keyword=keyword,
            domain=domain,
            days=days,
            limit=limit,
        )
        return BrowserHistoryResult(
            entries=entries,
            total=len(entries),
            browser=browser,
            profile="Default",
        )
    except sqlite3.Error as exc:
        logger.error(
            "browser_history: query failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
            context={"query": query, "params": str(params)},
        )
        return BrowserHistoryError(error="query_failed", message=str(exc), detail=None)
    finally:
        conn.close()
