"""Tests for gacore.tools.daily_notes: read_daily, edit_daily, search_daily.

Covers the three-layer memory strategy: daily notes sit between the per-turn working
checkpoint and the distilled long-term memory. Tests verify file creation, precise
string replacement, keyword search, date shortcuts, and error handling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from gacore.config import Config
from gacore.tools.daily_notes import (
    edit_daily,
    load_recent_daily_summaries,
    read_daily,
    search_daily,
)

# Same canonical Asia/Shanghai (UTC+8) clock the tool resolves "today" on, so the
# test never drifts from the implementation regardless of the runner's local zone.
_TZ8: timezone = timezone(timedelta(hours=8))

# ---------- edit_daily: creation ----------


def test_edit_daily_creates_new_note_with_header(tmp_path: Path) -> None:
    """Given a non-existent date, When edit_daily with empty old_str, Then a new note is created with a date header."""
    cfg = Config.for_tests(tmp_path)
    result = edit_daily.func(date="2026-08-03", old_str="", new_str="- first entry", _cfg=cfg)
    assert result["status"] == "created"
    assert result["date"] == "2026-08-03"
    note = (cfg.memory_dir / "daily" / "2026-08-03.md").read_text(encoding="utf-8")
    assert "# Daily Note" in note
    assert "- first entry" in note


def test_edit_daily_creates_directory_if_missing(tmp_path: Path) -> None:
    """Given no daily/ directory, When edit_daily, Then the directory and file are created."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- test", _cfg=cfg)
    assert (cfg.memory_dir / "daily").is_dir()
    assert (cfg.memory_dir / "daily" / "2026-08-03.md").is_file()


# ---------- edit_daily: modification ----------


def test_edit_daily_replaces_unique_string(tmp_path: Path) -> None:
    """Given an existing note, When edit_daily with a unique old_str, Then the text is replaced."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- old content", _cfg=cfg)
    result = edit_daily.func(date="2026-08-03", old_str="- old content", new_str="- new content", _cfg=cfg)
    assert result["status"] == "updated"
    note = (cfg.memory_dir / "daily" / "2026-08-03.md").read_text(encoding="utf-8")
    assert "- new content" in note
    assert "- old content" not in note


def test_edit_daily_appends_via_last_line_replacement(tmp_path: Path) -> None:
    """Given an existing note, When old_str is the last line and new_str extends it, Then content is appended."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- first task", _cfg=cfg)
    edit_daily.func(date="2026-08-03", old_str="- first task", new_str="- first task\n- second task", _cfg=cfg)
    note = (cfg.memory_dir / "daily" / "2026-08-03.md").read_text(encoding="utf-8")
    assert "- first task" in note
    assert "- second task" in note


# ---------- edit_daily: error handling ----------


def test_edit_daily_rejects_empty_old_str_on_existing_file(tmp_path: Path) -> None:
    """Given an existing note, When old_str is empty, Then an error dict is returned."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- initial", _cfg=cfg)
    result = edit_daily.func(date="2026-08-03", old_str="", new_str="- more", _cfg=cfg)
    assert result["error"] == "empty_old_str"


def test_edit_daily_rejects_non_unique_old_str(tmp_path: Path) -> None:
    """Given a note with duplicate text, When old_str matches multiple places, Then a not_unique error is returned."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- dup\n- dup", _cfg=cfg)
    result = edit_daily.func(date="2026-08-03", old_str="- dup", new_str="- unique", _cfg=cfg)
    assert result["error"] == "not_unique"
    assert result["count"] == 2


def test_edit_daily_rejects_not_found_old_str(tmp_path: Path) -> None:
    """Given an existing note, When old_str is not in the file, Then a not_found error is returned."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- real content", _cfg=cfg)
    result = edit_daily.func(date="2026-08-03", old_str="- nonexistent", new_str="- replacement", _cfg=cfg)
    assert result["error"] == "not_found"


def test_edit_daily_rejects_invalid_date_format(tmp_path: Path) -> None:
    """Given a malformed date, When edit_daily, Then an invalid_date error is returned."""
    cfg = Config.for_tests(tmp_path)
    result = edit_daily.func(date="2026/08/03", old_str="", new_str="- test", _cfg=cfg)
    assert result["error"] == "invalid_date"


# ---------- read_daily ----------


def test_read_daily_returns_content_for_existing_note(tmp_path: Path) -> None:
    """Given an existing note, When read_daily, Then the full markdown content is returned."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- hello world", _cfg=cfg)
    content = read_daily.func(date="2026-08-03", _cfg=cfg)
    assert isinstance(content, str)
    assert "- hello world" in content


def test_read_daily_returns_not_found_for_missing_note(tmp_path: Path) -> None:
    """Given no note for the date, When read_daily, Then a not_found error dict is returned."""
    cfg = Config.for_tests(tmp_path)
    result = read_daily.func(date="2026-01-01", _cfg=cfg)
    assert isinstance(result, dict)
    assert result["error"] == "not_found"
    assert result["date"] == "2026-01-01"


def test_read_daily_rejects_invalid_date(tmp_path: Path) -> None:
    """Given a malformed date, When read_daily, Then an invalid_date error is returned."""
    cfg = Config.for_tests(tmp_path)
    result = read_daily.func(date="not-a-date", _cfg=cfg)
    assert isinstance(result, dict)
    assert result["error"] == "invalid_date"


# ---------- search_daily ----------


def test_search_daily_finds_matches_across_files(tmp_path: Path) -> None:
    """Given multiple daily notes, When search_daily, Then all matching lines are returned with date and line number."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-02", old_str="", new_str="- learned about LangGraph", _cfg=cfg)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- built LangGraph agent\n- ate lunch", _cfg=cfg)
    result = search_daily.func(query="LangGraph", _cfg=cfg)
    assert result["matches"] == 2
    dates = {r["date"] for r in result["results"]}
    assert dates == {"2026-08-02", "2026-08-03"}


def test_search_daily_is_case_insensitive(tmp_path: Path) -> None:
    """Given mixed-case content, When search_daily with lowercase query, Then matches are found."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- Python is great\n- python is fun", _cfg=cfg)
    result = search_daily.func(query="python", _cfg=cfg)
    assert result["matches"] == 2


def test_search_daily_returns_empty_when_no_directory(tmp_path: Path) -> None:
    """Given no daily/ directory, When search_daily, Then an empty result set is returned."""
    cfg = Config.for_tests(tmp_path)
    result = search_daily.func(query="anything", _cfg=cfg)
    assert result["matches"] == 0
    assert result["results"] == []


def test_search_daily_returns_empty_when_no_matches(tmp_path: Path) -> None:
    """Given notes without the keyword, When search_daily, Then zero matches are returned."""
    cfg = Config.for_tests(tmp_path)
    edit_daily.func(date="2026-08-03", old_str="", new_str="- unrelated content", _cfg=cfg)
    result = search_daily.func(query="LangGraph", _cfg=cfg)
    assert result["matches"] == 0


# ---------- load_recent_daily_summaries ----------


def test_load_recent_daily_summaries_returns_bullet_lines(tmp_path: Path) -> None:
    """Given today's note with bullets, When load_recent_daily_summaries, Then bullet headings are returned."""
    cfg = Config.for_tests(tmp_path)
    today = datetime.now(_TZ8).date().isoformat()
    daily_dir = cfg.memory_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / f"{today}.md").write_text(
        f"# Daily Note — {today}\n\n- task A\n- task B\nsome non-bullet line\n",
        encoding="utf-8",
    )
    summary = load_recent_daily_summaries(cfg, days=2)
    assert f"[{today}]" in summary
    assert "- task A" in summary
    assert "- task B" in summary
    assert "some non-bullet line" not in summary


def test_load_recent_daily_summaries_returns_empty_when_no_notes(tmp_path: Path) -> None:
    """Given no daily notes, When load_recent_daily_summaries, Then an empty string is returned."""
    cfg = Config.for_tests(tmp_path)
    assert load_recent_daily_summaries(cfg, days=2) == ""


# ---------- tool args schema ----------


def test_tool_args_schemas_expose_fields_but_exclude_underscore_cfg() -> None:
    """Given the @tool decorators, When the args schemas are inspected, Then public fields are present and _cfg is not."""
    read_props = read_daily.args_schema.model_json_schema()["properties"]
    assert "date" in read_props
    assert "_cfg" not in read_props

    edit_props = edit_daily.args_schema.model_json_schema()["properties"]
    assert "date" in edit_props
    assert "old_str" in edit_props
    assert "new_str" in edit_props
    assert "_cfg" not in edit_props

    search_props = search_daily.args_schema.model_json_schema()["properties"]
    assert "query" in search_props
    assert "_cfg" not in search_props
