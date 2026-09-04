"""LangChain tools for filesystem read/patch/write, mirroring GA's do_file_read/do_file_patch/do_file_write.

All three tools take a plain pathlib.Path-style argument and operate with UTF-8 encoding. No secrets
are ever written to logs. Return values: file_read returns text (or an error dict), file_patch and
file_write return status dicts.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any, Final

from langchain_core.tools import tool

_CONTEXT_RADIUS: Final = 2
_FENCE_RE: Final = re.compile(r"```[^\n]*\n([\s\S]*?)```")
_TAG_RE: Final = re.compile(r"<file_content[^>]*>(.*?)</file_content>", re.DOTALL)
_VALID_MODES: Final = frozenset({"overwrite", "append", "prepend"})


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, content: str) -> None:
    # newline="" disables the platform EOL translation so the content is written verbatim.
    path.write_text(content, encoding="utf-8", newline="")


@tool
def file_read(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    keyword: str | None = None,
    show_linenos: bool = True,
) -> str | dict[str, Any]:
    """Read a file as UTF-8 text, optionally sliced to an inclusive [start_line, end_line] line range.

    With keyword, return the matching lines (case-insensitive) plus two lines of context on each side,
    with line numbers. A missing file returns an error dict with a did-you-mean suggestion built from
    sibling files in the same directory.
    """
    target = Path(path)
    if not target.is_file():
        return _not_found(target)
    lines = _read_text(target).splitlines()
    start = start_line if start_line is not None else 1
    end = min(end_line, len(lines)) if end_line is not None else len(lines)
    if start > end:
        return ""
    numbered = list(enumerate(lines, start=1))[start - 1 : end]

    if keyword:
        matches = [i for i, text in numbered if keyword.lower() in text.lower()]
        if matches:
            selected = _spans(matches, start, end)
            numbered = [(i, lines[i - 1]) for i in selected]
    return _format(numbered, show_linenos)


def _not_found(target: Path) -> dict[str, Any]:
    suggestion = ""
    parent = target.parent
    if parent.is_dir():
        candidates = [entry.name for entry in parent.iterdir() if entry.is_file()]
        close = difflib.get_close_matches(target.name, candidates, n=1, cutoff=0.3)
        if close:
            suggestion = close[0]
    return {"error": "not_found", "suggestion": suggestion, "message": f"File not found: {target}"}


def _spans(matches: list[int], start: int, end: int) -> list[int]:
    """Merge overlapping keyword contexts into one selection, clamped to [start, end]."""
    spans: list[list[int]] = []
    for m in matches:
        lo, hi = max(start, m - _CONTEXT_RADIUS), min(end, m + _CONTEXT_RADIUS)
        if spans and lo <= spans[-1][1] + 1:
            spans[-1][1] = hi
        else:
            spans.append([lo, hi])
    selected: list[int] = []
    for lo, hi in spans:
        selected.extend(range(lo, hi + 1))
    return selected


def _format(numbered: list[tuple[int, str]], show_linenos: bool) -> str:
    if show_linenos:
        return "\n".join(f"{i}|{text}" for i, text in numbered)
    return "\n".join(text for _, text in numbered)


@tool
def file_patch(path: str, old_content: str, new_content: str) -> dict[str, Any]:
    """Replace a unique block of old_content with new_content in the given file.

    The file is left untouched unless old_content occurs exactly once. The detected line ending of
    the existing file (CRLF vs LF) is preserved when writing back.
    """
    target = Path(path)
    if not target.is_file():
        return {"error": "not_found", "msg": f"File not found: {target}"}
    if not old_content:
        return {"error": "not_found", "msg": "old_content is empty; provide the text to replace"}
    full_text = _read_text(target)
    count = full_text.count(old_content)
    if count == 0:
        return {"error": "not_found", "msg": "old_content not found in file; check the current content with file_read"}
    if count > 1:
        return {"error": "not_unique", "msg": f"old_content matches {count} places; provide a longer unique block", "count": count}
    eol = _detect_eol(target)
    updated = full_text.replace(old_content, new_content)
    if eol and "\n" in updated:
        updated = updated.replace("\n", eol)
    _write_text(target, updated)
    return {"status": "ok", "msg": "patched"}


def _detect_eol(path: Path) -> str:
    """Return the file's dominant EOL ('\\r\\n' or '\\n'), or '' when there are no newlines."""
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf > lf:
        return "\r\n"
    if lf > 0:
        return "\n"
    return ""


@tool
def file_write(
    path: str,
    content: str | None = None,
    mode: str = "overwrite",
    file_content: str | None = None,
) -> dict[str, Any]:
    """Write content to a file in overwrite / append / prepend mode.

    Content resolution order: the content argument wins; otherwise content is extracted from the
    file_content argument when it carries <file_content>...</file_content> tags or a fenced code block.
    Parent directories are created on demand.
    """
    if mode not in _VALID_MODES:
        return {"error": "invalid_mode", "msg": f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}"}
    resolved = _resolve_content(content, file_content)
    if resolved is None:
        return {"error": "no_content", "msg": "No content found. Put content in `content` or inside <file_content>...</file_content> tags."}

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        existing = _read_text(target) if target.is_file() else ""
        _write_text(target, existing + resolved)
    elif mode == "prepend":
        existing = _read_text(target) if target.is_file() else ""
        _write_text(target, resolved + existing)
    else:
        _write_text(target, resolved)
    return {"status": "ok", "wrote_bytes": len(resolved.encode("utf-8")), "mode": mode}


def _resolve_content(content: str | None, file_content: str | None) -> str | None:
    """Resolve the text to write, returning None when nothing usable was provided."""
    raw = content if content else file_content
    if raw is None:
        return None
    tags = _TAG_RE.findall(raw)
    if tags:
        return tags[-1].strip()
    blocks = _FENCE_RE.findall(raw)
    if blocks:
        return blocks[-1].strip()
    stripped = raw.strip()
    return stripped if stripped else None
