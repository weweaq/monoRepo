"""LangChain tools for daily notes: a per-day log layer between working memory and long-term memory.

Daily notes are the "raw log" — what happened, what decisions were made, what was learned.
Each day gets one markdown file under memory/daily/{date}.md. This sits between the ephemeral
working checkpoint (state.working, per-turn) and the distilled long-term memory (global_mem.txt,
L2 facts + global_mem_insight.txt, L1 index):

    working checkpoint  →  daily notes  →  long-term memory
    (per-turn, in RAM)     (per-day, file)   (distilled, file)

Three tools mirror the strategy learned from another agent:
  - read_daily(date): read a specific day's note
  - edit_daily(date, old_str, new_str): precise string-replace edit (not append-only)
  - search_daily(query): keyword search across all daily notes
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from langchain_core.tools import tool

from gacore.config import Config

_DAILY_SUBDIR: Final = "daily"
_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_SEARCH_RESULTS: Final = 50

# Asia/Shanghai (UTC+8): the same canonical timezone the get_time tool, the
# middleware time guard and the [Current time] prompt anchor all use. Relative-date
# shortcuts ("today"/"yesterday") MUST be computed on this clock so a note written for
# the user's local (East-8) day never lands on the wrong date due to a server that
# runs UTC or another timezone.
_TZ: Final = timezone(timedelta(hours=8))


def _daily_dir(cfg: Config) -> Path:
    """Return the daily notes directory: cfg.memory_dir / daily."""
    return cfg.memory_dir / _DAILY_SUBDIR


def _daily_path(cfg: Config, date: str) -> Path:
    """Return the file path for a given ISO date string (YYYY-MM-DD)."""
    return _daily_dir(cfg) / f"{date}.md"


def _validate_date(date: str) -> str | None:
    """Return None if date matches YYYY-MM-DD, else an error message string."""
    if not _DATE_RE.match(date):
        return f"Invalid date format: {date!r}. Expected YYYY-MM-DD."
    return None


def _ensure_daily_dir(cfg: Config) -> Path:
    """Create the daily notes directory if missing and return it."""
    d = _daily_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _detect_eol(path: Path) -> str:
    """Return the file's dominant EOL ('\\r\\n' or '\\n'), or '\\n' when there are no newlines."""
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf > lf:
        return "\r\n"
    return "\n"


@tool
def read_daily(
    date: str,
    _cfg: Config | None = None,
) -> str | dict[str, Any]:
    """Read the daily note for a given date.

    Args:
        date: ISO date string (YYYY-MM-DD). Use "today" or "yesterday" as shortcuts.

    Returns:
        The full markdown content of the day's note, or an error dict if the note
        does not exist yet.
    """
    cfg = _cfg or Config.default()
    resolved = _resolve_date(date)
    err = _validate_date(resolved)
    if err:
        return {"error": "invalid_date", "msg": err}
    path = _daily_path(cfg, resolved)
    if not path.is_file():
        return {"error": "not_found", "date": resolved, "msg": f"No daily note for {resolved}. Use edit_daily to create one."}
    return path.read_text(encoding="utf-8", errors="replace")


@tool
def edit_daily(
    date: str,
    old_str: str,
    new_str: str,
    _cfg: Config | None = None,
) -> dict[str, Any]:
    """Edit a daily note using precise string replacement.

    The note file is memory/daily/{date}.md. Operations:
    - Create new note: pass old_str="" (empty) to write new_str as the initial content.
    - Append content: pass old_str as the last line of the existing file; new_str = that line + new content.
    - Modify content: pass old_str as the exact text to replace; new_str as the replacement.
    - Delete content: pass old_str as the text to remove; new_str as empty or the context to stitch.

    old_str must match exactly once in the file (including whitespace). If old_str is empty
    and the file does not exist, a new note is created with a date header.

    Args:
        date: ISO date string (YYYY-MM-DD), or "today" / "yesterday".
        old_str: The exact text to find in the file. Empty string creates a new file.
        new_str: The text to replace old_str with.

    Returns:
        A status dict with the date, operation performed, and path.
    """
    cfg = _cfg or Config.default()
    resolved = _resolve_date(date)
    err = _validate_date(resolved)
    if err:
        return {"error": "invalid_date", "msg": err}

    _ensure_daily_dir(cfg)
    path = _daily_path(cfg, resolved)

    # Create new file
    if not path.is_file():
        header = f"# Daily Note — {resolved}\n\n"
        content = header + new_str if new_str else header
        path.write_text(content, encoding="utf-8", newline="")
        return {"status": "created", "date": resolved, "path": str(path)}

    # Edit existing file
    if not old_str:
        return {"error": "empty_old_str", "msg": "old_str is empty but the file already exists. Provide the exact text to replace."}

    full_text = path.read_text(encoding="utf-8", errors="replace")
    count = full_text.count(old_str)
    if count == 0:
        return {"error": "not_found", "msg": "old_str not found in the daily note. Read the current content first with read_daily."}
    if count > 1:
        return {"error": "not_unique", "msg": f"old_str matches {count} places. Provide a longer unique block.", "count": count}

    eol = _detect_eol(path)
    updated = full_text.replace(old_str, new_str)
    if eol != "\n" and "\n" in updated:
        updated = updated.replace("\n", eol)
    path.write_text(updated, encoding="utf-8", newline="")
    return {"status": "updated", "date": resolved, "path": str(path)}


@tool
def search_daily(
    query: str,
    _cfg: Config | None = None,
) -> dict[str, Any]:
    """Search across all daily notes for a keyword or phrase (case-insensitive).

    Scans every *.md file under memory/daily/ and returns matching lines with their
    date and line number. Results are capped at 50 matches.

    Args:
        query: The keyword or phrase to search for.

    Returns:
        A dict with the query, match count, and a list of {date, line, text} entries.
    """
    cfg = _cfg or Config.default()
    daily_dir = _daily_dir(cfg)
    if not daily_dir.is_dir():
        return {"query": query, "matches": 0, "results": []}

    results: list[dict[str, Any]] = []
    query_lower = query.lower()
    for md_file in sorted(daily_dir.glob("*.md")):
        date = md_file.stem
        text = md_file.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), start=1):
            if query_lower in line.lower():
                results.append({"date": date, "line": i, "text": line.strip()})
                if len(results) >= _MAX_SEARCH_RESULTS:
                    return {"query": query, "matches": len(results), "truncated": True, "results": results}
    return {"query": query, "matches": len(results), "results": results}


def _resolve_date(date: str) -> str:
    """Resolve date shortcuts: 'today' / 'yesterday' to ISO date strings.

    Computed on the fixed Asia/Shanghai (UTC+8) clock so the resolved date always
    matches the East-8 user's perception of "now", regardless of the server's local
    timezone or UTC.
    """
    now = datetime.now(_TZ)
    if date == "today":
        return now.date().isoformat()
    if date == "yesterday":
        return (now - timedelta(days=1)).date().isoformat()
    return date


def load_recent_daily_summaries(cfg: Config, days: int = 2) -> str:
    """Load summaries (heading lines) from the most recent N days of daily notes.

    Used by context.build_system_prompt to inject a compact recap into the per-turn
    system prompt — restores cross-session continuity without dumping full notes.
    Only the first line of each bullet point (lines starting with '- ') is included
    to keep token cost low.

    Args:
        cfg: Runtime configuration (provides memory_dir).
        days: How many recent days to look back (default 2 = today + yesterday).

    Returns:
        A formatted string block, or empty string if no notes found.
    """
    daily_dir = _daily_dir(cfg)
    if not daily_dir.is_dir():
        return ""

    now = datetime.now(_TZ)
    blocks: list[str] = []
    for offset in range(days):
        date = (now - timedelta(days=offset)).date().isoformat()
        path = daily_dir / f"{date}.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Extract bullet-point headings (first 120 chars each) for a compact recap
        bullets = [
            line.strip()[:120]
            for line in text.splitlines()
            if line.strip().startswith("- ")
        ]
        if bullets:
            blocks.append(f"[{date}]\n" + "\n".join(bullets))
    if not blocks:
        return ""
    return "\n\n".join(blocks)
