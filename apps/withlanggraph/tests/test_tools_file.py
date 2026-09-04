"""Tests for the file_read / file_patch / file_write LangChain tools in gacore.tools.file_tools."""

from pathlib import Path

import pytest

from gacore.tools.file_tools import file_patch, file_read, file_write


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.txt"
    path.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    return path


# --- file_read -------------------------------------------------------------------------------


def test_file_read_exact_range(sample_file: Path) -> None:
    result = file_read.invoke({"path": str(sample_file), "start_line": 2, "end_line": 4})
    assert result == "2|line2\n3|line3\n4|line4"


def test_file_read_no_linenos(sample_file: Path) -> None:
    result = file_read.invoke({"path": str(sample_file), "start_line": 1, "end_line": 3, "show_linenos": False})
    assert result == "line1\nline2\nline3"


def test_file_read_keyword_hit_with_context(sample_file: Path) -> None:
    result = file_read.invoke({"path": str(sample_file), "keyword": "line3"})
    assert "3|line3" in result
    # ±2 context lines around the match at line 3
    assert "1|line1" in result
    assert "5|line5" in result


def test_file_read_keyword_case_insensitive(sample_file: Path) -> None:
    result = file_read.invoke({"path": str(sample_file), "keyword": "LINE4"})
    assert "4|line4" in result


def test_file_read_missing_file_suggests_sibling(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "readme.md").write_text("hi", encoding="utf-8")
    result = file_read.invoke({"path": str(tmp_path / "config.josn")})
    assert isinstance(result, dict)
    assert result["error"] == "not_found"
    assert result["suggestion"] == "config.json"
    assert "config.josn" in result["message"]


def test_file_read_missing_file_no_suggestion(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    result = file_read.invoke({"path": str(tmp_path / "zzz_quite_different.py")})
    assert isinstance(result, dict)
    assert result["error"] == "not_found"
    assert result["suggestion"] == ""


def test_file_read_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    assert file_read.invoke({"path": str(path)}) == ""


# --- file_patch ------------------------------------------------------------------------------


def test_file_patch_unique_replace(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("abc\nxyz\nabc\n", encoding="utf-8")
    result = file_patch.invoke({"path": str(path), "old_content": "xyz", "new_content": "ZZZ"})
    assert result == {"status": "ok", "msg": "patched"}
    assert path.read_text(encoding="utf-8") == "abc\nZZZ\nabc\n"


def test_file_patch_not_found(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("abc\n", encoding="utf-8")
    result = file_patch.invoke({"path": str(path), "old_content": "missing", "new_content": "x"})
    assert result["error"] == "not_found"
    assert path.read_text(encoding="utf-8") == "abc\n"


def test_file_patch_not_unique(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("abc\nabc\n", encoding="utf-8")
    result = file_patch.invoke({"path": str(path), "old_content": "abc", "new_content": "x"})
    assert result["error"] == "not_unique"
    assert result["count"] == 2
    assert path.read_text(encoding="utf-8") == "abc\nabc\n"


def test_file_patch_preserves_crlf(tmp_path: Path) -> None:
    path = tmp_path / "win.txt"
    path.write_bytes(b"a\r\nb\r\nc\r\n")
    result = file_patch.invoke({"path": str(path), "old_content": "b", "new_content": "B"})
    assert result["status"] == "ok"
    assert path.read_bytes() == b"a\r\nB\r\nc\r\n"


def test_file_patch_missing_file(tmp_path: Path) -> None:
    result = file_patch.invoke({"path": str(tmp_path / "nope.txt"), "old_content": "x", "new_content": "y"})
    assert result["error"] == "not_found"


# --- file_write ------------------------------------------------------------------------------


def test_file_write_overwrite_creates(tmp_path: Path) -> None:
    path = tmp_path / "new.txt"
    result = file_write.invoke({"path": str(path), "content": "hello"})
    assert result == {"status": "ok", "wrote_bytes": 5, "mode": "overwrite"}
    assert path.read_text(encoding="utf-8") == "hello"


def test_file_write_overwrite_replaces(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("old", encoding="utf-8")
    result = file_write.invoke({"path": str(path), "content": "new"})
    assert result["status"] == "ok"
    assert path.read_text(encoding="utf-8") == "new"


def test_file_write_append(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("abc", encoding="utf-8")
    result = file_write.invoke({"path": str(path), "content": "def", "mode": "append"})
    assert result["status"] == "ok"
    assert result["mode"] == "append"
    assert path.read_text(encoding="utf-8") == "abcdef"


def test_file_write_prepend(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("abc", encoding="utf-8")
    result = file_write.invoke({"path": str(path), "content": "zz", "mode": "prepend"})
    assert result["status"] == "ok"
    assert result["mode"] == "prepend"
    assert path.read_text(encoding="utf-8") == "zzabc"


def test_file_write_content_from_tag(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    result = file_write.invoke({"path": str(path), "content": None, "file_content": "<file_content>hello from tag</file_content>"})
    assert result["status"] == "ok"
    assert path.read_text(encoding="utf-8") == "hello from tag"


def test_file_write_content_from_fence(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    result = file_write.invoke({"path": str(path), "content": "```\nhello from fence\n```"})
    assert result["status"] == "ok"
    assert path.read_text(encoding="utf-8") == "hello from fence"


def test_file_write_no_content(tmp_path: Path) -> None:
    result = file_write.invoke({"path": str(tmp_path / "f.txt"), "content": None, "file_content": None})
    assert result["error"] == "no_content"


def test_file_write_invalid_mode(tmp_path: Path) -> None:
    result = file_write.invoke({"path": str(tmp_path / "f.txt"), "content": "x", "mode": "truncate"})
    assert result["error"] == "invalid_mode"


def test_file_write_nested_dir_created(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c.txt"
    result = file_write.invoke({"path": str(path), "content": "deep"})
    assert result["status"] == "ok"
    assert path.read_text(encoding="utf-8") == "deep"
