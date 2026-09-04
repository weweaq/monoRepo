"""Character card registry for gacore.

Cards are pure data assets: each ``<id>.md`` under ``config/assets/characters/``
holds a display name (the first ``# `` heading line) plus the system-prompt
personality text that becomes the active persona while the card is enabled.

This module only scans and reads files — it never touches the graph, middleware
or any frontend. Adding a card never requires code changes: drop a new .md file
into the cards directory and it becomes switchable. Card id = file stem (ASCII
recommended), display name = first ``# `` heading, prompt body = everything after
that heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gacore.config import Config

_CARD_DIR_NAME: Final = "characters"
_HEADING_RE: Final = re.compile(r"^[ \t]*#[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Card:
    """A discovered character card: identity metadata plus its source file."""

    id: str
    name: str
    path: Path


def card_dir(cfg: Config) -> Path:
    """Directory holding the card assets: <asset_dir>/characters."""
    return cfg.asset_dir / _CARD_DIR_NAME


def _read_heading(path: Path) -> str | None:
    """Return the first ``# `` heading of a card file, or None when unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _HEADING_RE.search(text)
    return m.group(1).strip() if m else None


def list_cards(cfg: Config) -> list[Card]:
    """Return every card asset under the characters directory, sorted by id."""
    d = card_dir(cfg)
    if not d.is_dir():
        return []
    cards: list[Card] = []
    for path in sorted(d.glob("*.md")):
        cards.append(Card(id=path.stem, name=_read_heading(path) or path.stem, path=path))
    return cards


def card_prompt(cfg: Config, card_id: str) -> str | None:
    """Return the personality text of a card (everything after its heading) or None.

    None means the card is missing or unreadable — callers should fall back to the
    default assistant persona instead of crashing.
    """
    path = card_dir(cfg) / f"{card_id}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _HEADING_RE.search(text)
    if m:
        # Strip the leading '# 显示名' heading line; keep the rest verbatim.
        text = text[m.end() :]
    text = text.strip()
    return text or None


def card_name(cfg: Config, card_id: str) -> str | None:
    """Return a card's display name, or None when the card does not exist."""
    path = card_dir(cfg) / f"{card_id}.md"
    if not path.is_file():
        return None
    return _read_heading(path) or path.stem
