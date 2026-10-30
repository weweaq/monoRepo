"""Bounded, redacted summaries for Companion events.

Events never store raw text: the self-memory only needs enough to know what the
character really received or said. Summaries are single-line, credential-free
and capped so a pasted log or key cannot bloat (or leak into) the fact source.
"""

from __future__ import annotations

import re
from typing import Final

from gacore.redact import redact_text

DEFAULT_SUMMARY_CHARS: Final = 200
_ELLIPSIS: Final = "…"
_WHITESPACE: Final = re.compile(r"\s+")


def minimize_user_message(text: str, *, max_chars: int = DEFAULT_SUMMARY_CHARS) -> str:
    """Collapse whitespace, redact credentials, then cap at ``max_chars`` characters."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    flat = _WHITESPACE.sub(" ", text or "").strip()
    flat = redact_text(flat)
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1] + _ELLIPSIS
