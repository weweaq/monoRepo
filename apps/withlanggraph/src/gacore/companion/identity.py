"""The single source of the per-user active character card.

The QQ frontend (``/角色`` command) and proactive outreach both need to know which
character card a user is currently playing. Before this module each path would have
had to parse ``data/active_cards.json`` itself; now they share one repository so the
two can never disagree about who the current character is.

Guarantees:
  - Same file and same format as before (``data/active_cards.json``, ``user -> card``);
  - Writes are atomic (temp file + ``os.replace``, with the Windows lock retry);
  - A missing / empty file is a normal first boot and reads as ``{}``;
  - A corrupt file reads as ``{}`` but is never overwritten — ``set()`` raises
    ``ActiveCardCorruptError`` so a hand-edited file is not silently destroyed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Final

from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("companion.identity")

_ACTIVE_CARDS_FILE: Final = "active_cards.json"
# Windows 上 os.replace 偶发 PermissionError（杀毒/索引器瞬时占用目标文件），重试几次即可。
_REPLACE_RETRIES: Final = 5
_REPLACE_BACKOFF_SECONDS: Final = 0.02

# 一个状态文件全进程一把锁：proactive 与 QQ 各自 new 一个 repository 时，两把实例锁
# 互不相斥，读-合并-写会丢更新。按 resolve 后的路径共享锁即可。
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """Return the process-wide lock guarding ``path`` (created on first use)."""
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
    return lock


class ActiveCardError(Exception):
    """Base error for active-card persistence problems."""


class ActiveCardCorruptError(ActiveCardError):
    """The state file is unusable (not JSON / not an object); do not overwrite it."""


def default_active_cards_path() -> Path:
    """Return ``<project root>/data/active_cards.json`` — the canonical state file."""
    return Config.default().root / "data" / _ACTIVE_CARDS_FILE


class ActiveCardRepository:
    """Read/modify the ``user openid -> active card id`` map on disk.

    Every instance over the same file shares one process-wide lock (``_lock_for``), so
    ``set()`` — a read-merge-write — cannot drop another instance's entry. Readers
    (proactive) always re-read the file, so they see QQ's latest switch.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else default_active_cards_path()
        self._lock = _lock_for(self._path)

    @property
    def path(self) -> Path:
        """The state file this repository owns."""
        return self._path

    def get(self, openid: str) -> str | None:
        """Return the user's active card id, or None when they play no character."""
        if not openid:
            return None
        return self.snapshot().get(openid)

    def snapshot(self) -> dict[str, str]:
        """Return a copy of the persisted map; corrupt/unreadable state reads as ``{}``."""
        return dict(self._read())

    def set(self, openid: str, card_id: str | None) -> None:
        """Bind ``openid`` to ``card_id`` (or unbind when None) and persist atomically.

        Raises ``ActiveCardCorruptError`` when the existing file is unusable, and
        ``OSError`` when the atomic replace cannot complete; in both cases the previous
        file content is left untouched.
        """
        if not openid:
            raise ValueError("openid must be non-empty")
        with self._lock:
            cards = self._read_for_update()
            if card_id is None:
                cards.pop(openid, None)
            else:
                cards[openid] = str(card_id)
            self._write(cards)

    # ------------------------------------------------------------------ internals

    def _read(self) -> dict[str, str]:
        """Read the map, degrading to ``{}`` for missing/empty/corrupt files."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning(
                "active card state unreadable, falling back to empty map",
                path=str(self._path),
                error_type=type(exc).__name__,
            )
            return {}
        cards = _parse_cards(raw)
        if cards is None:
            logger.warning(
                "active card state is not a valid card map, keeping file untouched",
                path=str(self._path),
            )
            return {}
        return cards

    def _read_for_update(self) -> dict[str, str]:
        """Read the map for a read-merge-write; a damaged file raises instead of {}."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ActiveCardError(f"cannot read {self._path}: {exc}") from exc
        cards = _parse_cards(raw)
        if cards is None:
            raise ActiveCardCorruptError(f"{self._path} is not a valid card map; refusing to overwrite it")
        return cards

    def _write(self, cards: dict[str, str]) -> None:
        """Atomically replace the state file with ``cards`` (temp file + os.replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 唯一临时名：同目录多个进程/实例同时写时，谁也不会吃掉对方的临时文件。
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, self._path)
                return
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))


def _parse_cards(raw: str) -> dict[str, str] | None:
    """Parse the state payload; None means "unusable" (corrupt or non-object)."""
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return {str(k): str(v) for k, v in payload.items() if isinstance(v, str)}
