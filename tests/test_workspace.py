"""Structural smoke tests for the workspace itself.

These enforce member conventions (R2/R12) for every project under
apps/ or packages/, so violations surface in the normal quality gate
instead of at migration time.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMBER_BASES = (ROOT / "apps", ROOT / "packages")


def _member_pyprojects() -> list[Path]:
    found: list[Path] = []
    for base in MEMBER_BASES:
        if base.is_dir():
            found.extend(sorted(base.glob("*/pyproject.toml")))
    return found


def _load(pyproject: Path) -> dict:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))


def test_every_member_declares_project_name_and_version() -> None:
    members = _member_pyprojects()
    for pyproject in members:
        project = _load(pyproject).get("project", {})
        assert project.get("name"), f"{pyproject} missing project.name"
        assert project.get("version"), f"{pyproject} missing project.version"


def test_member_names_are_unique() -> None:
    names = [_load(p).get("project", {}).get("name", "") for p in _member_pyprojects()]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"duplicate member names: {duplicates}"
