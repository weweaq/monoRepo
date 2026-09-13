"""Tests for scan_mmds() (the /api/mmds listing logic)."""

from __future__ import annotations

from viewer_server import ROOT, scan_mmds


def test_scan_collects_supported_extensions(tmp_path):
    (tmp_path / "apps" / "demo" / "docs").mkdir(parents=True)
    (tmp_path / "apps" / "demo" / "docs" / "a.mmd").write_text("flowchart LR\n")
    (tmp_path / "apps" / "demo" / "docs" / "b.mermaid").write_text("x")
    (tmp_path / "apps" / "demo" / "docs" / "c.txt").write_text("y")
    (tmp_path / "apps" / "demo" / "docs" / "d.png").write_text("z")

    found = scan_mmds(tmp_path, skip=set())
    assert "apps/demo/docs/a.mmd" in found
    assert "apps/demo/docs/b.mermaid" in found
    # non-diagram files (.txt/.png) must NOT show up in the picker list
    assert "apps/demo/docs/c.txt" not in found
    assert "apps/demo/docs/d.png" not in found


def test_scan_skips_junk_dirs(tmp_path):
    (tmp_path / "apps").mkdir()
    (tmp_path / "vendor" / "keep.mmd").mkdir(parents=True)
    (tmp_path / ".git" / "keep.mmd").mkdir(parents=True)
    (tmp_path / "data" / "keep.mmd").mkdir(parents=True)
    (tmp_path / "apps" / "keep2.mmd").write_text("x")

    found = scan_mmds(tmp_path)
    assert "apps/keep2.mmd" in found
    assert not any(p.startswith(("vendor/", ".git/", "data/")) for p in found)


def test_scan_uses_slash_separators(tmp_path):
    (tmp_path / "sub" / "dir").mkdir(parents=True)
    (tmp_path / "sub" / "dir" / "x.mmd").write_text("x")
    found = scan_mmds(tmp_path, skip=set())
    assert found == ["sub/dir/x.mmd"]


def test_scan_repo_root_contains_mmd():
    # Sanity: the real repo root must contain at least one .mmd for the UI to list.
    found = scan_mmds(ROOT)
    assert "apps/mermaid-viewer/docs/architecture-flow.mmd" in found
