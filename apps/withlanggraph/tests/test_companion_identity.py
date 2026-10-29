"""Step 1 of Companion Self-Memory V1: the shared active-card repository.

QQ (`frontends/qq.py`) and proactive outreach must read the same per-user active
character card, so persistence moves into ``gacore.companion.identity``. The file
format and location (``data/active_cards.json``) stay unchanged.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import gacore.companion.identity as identity
from gacore.companion.identity import (
    ActiveCardCorruptError,
    ActiveCardRepository,
    default_active_cards_path,
)


def test_missing_file_reads_empty(tmp_path: Path) -> None:
    """A first boot (no file yet) degrades to an empty map, never raises."""
    repo = ActiveCardRepository(tmp_path / "active_cards.json")

    assert repo.snapshot() == {}
    assert repo.get("user-1") is None


def test_empty_file_reads_empty(tmp_path: Path) -> None:
    """A zero-byte / whitespace-only file is 'no cards yet', not corruption."""
    path = tmp_path / "active_cards.json"
    path.write_text("  \n", encoding="utf-8")
    repo = ActiveCardRepository(path)

    assert repo.snapshot() == {}


def test_reads_existing_mapping(tmp_path: Path) -> None:
    """The persisted ``user_id -> card_id`` map is read verbatim."""
    path = tmp_path / "active_cards.json"
    path.write_text(json.dumps({"user-1": "linwan", "user-2": "nami"}), encoding="utf-8")
    repo = ActiveCardRepository(path)

    assert repo.snapshot() == {"user-1": "linwan", "user-2": "nami"}
    assert repo.get("user-2") == "nami"


def test_non_string_values_are_dropped(tmp_path: Path) -> None:
    """Legacy/hand-edited junk values are ignored instead of poisoning the cache."""
    path = tmp_path / "active_cards.json"
    path.write_text(json.dumps({"user-1": "linwan", "user-2": 3}), encoding="utf-8")

    assert ActiveCardRepository(path).snapshot() == {"user-1": "linwan"}


def test_set_persists_and_is_visible_to_new_instances(tmp_path: Path) -> None:
    """set() writes through to disk, so a fresh process (new instance) sees it."""
    path = tmp_path / "active_cards.json"
    repo = ActiveCardRepository(path)

    repo.set("user-1", "linwan")

    assert repo.get("user-1") == "linwan"
    assert json.loads(path.read_text(encoding="utf-8")) == {"user-1": "linwan"}
    assert ActiveCardRepository(path).snapshot() == {"user-1": "linwan"}


def test_set_none_removes_the_key(tmp_path: Path) -> None:
    """``/角色 off`` removes the mapping without disturbing other users."""
    path = tmp_path / "active_cards.json"
    path.write_text(json.dumps({"user-1": "linwan", "user-2": "nami"}), encoding="utf-8")
    repo = ActiveCardRepository(path)

    repo.set("user-1", None)

    assert repo.snapshot() == {"user-2": "nami"}
    assert json.loads(path.read_text(encoding="utf-8")) == {"user-2": "nami"}


def test_set_preserves_other_users(tmp_path: Path) -> None:
    """One user's switch never drops another user's card (read-merge-write)."""
    path = tmp_path / "active_cards.json"
    path.write_text(json.dumps({"user-2": "nami"}), encoding="utf-8")

    ActiveCardRepository(path).set("user-1", "linwan")

    assert json.loads(path.read_text(encoding="utf-8")) == {"user-1": "linwan", "user-2": "nami"}


def test_corrupt_json_is_not_overwritten(tmp_path: Path) -> None:
    """Corrupt JSON degrades to {} on read but set() refuses to destroy the file."""
    path = tmp_path / "active_cards.json"
    path.write_text("{not json", encoding="utf-8")
    repo = ActiveCardRepository(path)

    assert repo.snapshot() == {}
    assert repo.get("user-1") is None
    with pytest.raises(ActiveCardCorruptError):
        repo.set("user-1", "linwan")
    assert path.read_text(encoding="utf-8") == "{not json"


def test_non_dict_json_is_treated_as_corrupt(tmp_path: Path) -> None:
    """A JSON array/scalar in the state file is as unusable as broken JSON."""
    path = tmp_path / "active_cards.json"
    path.write_text("[]", encoding="utf-8")
    repo = ActiveCardRepository(path)

    assert repo.snapshot() == {}
    with pytest.raises(ActiveCardCorruptError):
        repo.set("user-1", "linwan")
    assert path.read_text(encoding="utf-8") == "[]"


def test_snapshot_is_a_copy(tmp_path: Path) -> None:
    """Mutating the returned snapshot must not leak into the persisted state."""
    path = tmp_path / "active_cards.json"
    repo = ActiveCardRepository(path)
    repo.set("user-1", "linwan")

    snapshot = repo.snapshot()
    snapshot["user-1"] = "nami"

    assert repo.get("user-1") == "linwan"


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    """Atomic write (temp + os.replace) must not leave scratch files behind."""
    path = tmp_path / "active_cards.json"
    repo = ActiveCardRepository(path)

    repo.set("user-1", "linwan")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["active_cards.json"]


def test_failed_replace_keeps_previous_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked target file (Windows PermissionError) must not truncate the old map."""
    path = tmp_path / "active_cards.json"
    repo = ActiveCardRepository(path)
    repo.set("user-1", "linwan")

    def _locked(src: object, dst: object) -> None:
        raise PermissionError("target is locked")

    monkeypatch.setattr(identity.os, "replace", _locked)
    with pytest.raises(PermissionError):
        repo.set("user-2", "nami")

    assert ActiveCardRepository(path).snapshot() == {"user-1": "linwan"}


def test_concurrent_sets_keep_every_update(tmp_path: Path) -> None:
    """Two writers on one repository must not drop each other's user entry."""
    path = tmp_path / "active_cards.json"
    repo = ActiveCardRepository(path)
    barrier = threading.Barrier(4)

    def _worker(user: str, card: str) -> None:
        barrier.wait()
        repo.set(user, card)

    threads = [
        threading.Thread(target=_worker, args=(f"user-{i}", f"card-{i}"))
        for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert repo.snapshot() == {f"user-{i}": f"card-{i}" for i in range(4)}


def test_default_path_is_project_data_dir() -> None:
    """Both QQ and proactive must resolve the one canonical state file."""
    path = default_active_cards_path()

    assert path.parts[-2:] == ("data", "active_cards.json")
    assert path.parent.parent.name == "withlanggraph"
