"""Tests for gacore.tools.bili_history — fully mocked, no real bili CLI / network."""

from __future__ import annotations

import json
import subprocess
import sys
import types

import gacore.tools.bili_history as bh_mod

# Ensure bh_mod is the real module, not the StructuredTool that @tool shadows in __init__.py.
if not isinstance(bh_mod, types.ModuleType):
    bh_mod = sys.modules["gacore.tools.bili_history"]

from gacore.tools.bili_history import bili_history

_FAKE_BILI = r"C:\fake\bili.exe"
_FAKE_ACCOUNT = "祁伟的小日常"
_FAKE_UID = "1710565685"


def _proc(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([_FAKE_BILI], rc, stdout, stderr)


def _status_json(authenticated: bool = True, name: str = _FAKE_ACCOUNT, uid: str = _FAKE_UID) -> str:
    return json.dumps(
        {
            "ok": True,
            "schema_version": "1",
            "data": {
                "authenticated": authenticated,
                "user": {"id": uid, "name": name, "username": name},
            },
        },
        ensure_ascii=False,
    )


def _history_json(entries: list[dict], page: int = 1, count: int | None = None) -> str:
    return json.dumps(
        {
            "ok": True,
            "schema_version": "1",
            "data": {
                "page": page,
                "count": count if count is not None else len(entries),
                "items": entries,
            },
        },
        ensure_ascii=False,
    )


def _entries(n: int = 3) -> list[dict]:
    return [
        {
            "id": f"BV{i}",
            "bvid": f"BV{i}",
            "title": f"测试视频{i}",
            "author": f"UP主{i}",
            "viewed_at": f"2026-08-02T16:2{i}:00",
        }
        for i in range(1, n + 1)
    ]


def _is_error(result: dict) -> bool:
    return "error" in result


def _is_success(result: dict) -> bool:
    return "entries" in result


def _patch(monkeypatch, find=None, run=None) -> None:
    """Point _find_bili/_run_cli at fakes; default: bili found, run left untouched."""
    monkeypatch.setattr(bh_mod, "_find_bili", find or (lambda: _FAKE_BILI))
    if run is not None:
        monkeypatch.setattr(bh_mod, "_run_cli", run)


# ---------------------------------------------------------------------------
# 输入校验（不触碰 CLI）
# ---------------------------------------------------------------------------


def test_limit_zero_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = bili_history.invoke({"limit": 0})
    assert _is_error(result)
    assert result["error"] == "invalid_limit"


def test_limit_too_large_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = bili_history.invoke({"limit": 101})
    assert _is_error(result)
    assert result["error"] == "invalid_limit"


def test_page_zero_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = bili_history.invoke({"page": 0})
    assert _is_error(result)
    assert result["error"] == "invalid_page"


def test_bili_not_found_returns_error(monkeypatch) -> None:
    _patch(monkeypatch, find=lambda: None)
    result = bili_history.invoke({})
    assert _is_error(result)
    assert result["error"] == "bili_not_found"


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_success_returns_entries_and_account(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "status" in args:
            return _proc(_status_json())
        return _proc(_history_json(_entries(3), count=3))

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({"limit": 3})

    assert _is_success(result)
    assert result["total"] == 3
    assert result["page"] == 1
    assert result["account"] == _FAKE_ACCOUNT
    assert result["uid"] == _FAKE_UID
    assert len(result["entries"]) == 3
    first = result["entries"][0]
    assert first["bvid"] == "BV1"
    assert first["title"] == "测试视频1"
    assert first["author"] == "UP主1"
    assert first["viewed_at"] == "2026-08-02T16:21:00"
    # CLI 参数正确传递
    assert any("history" in c and "-n" in c and "3" in c for c in calls)


def test_success_falls_back_to_id_when_bvid_missing(monkeypatch) -> None:
    entries = [{"id": "BV42", "title": "只有id", "author": "UP", "viewed_at": "2026-08-02T10:00:00"}]

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "status" in args:
            return _proc(_status_json())
        return _proc(_history_json(entries, count=1))

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({"limit": 1})
    assert _is_success(result)
    assert result["entries"][0]["bvid"] == "BV42"


def test_success_respects_page_parameter(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "status" in args:
            return _proc(_status_json())
        assert "-p" in args and "2" in args
        return _proc(_history_json(_entries(1), page=2, count=1))

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({"limit": 1, "page": 2})
    assert _is_success(result)
    assert result["page"] == 2


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_not_authenticated_returns_error(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert "status" in args
        return _proc(_status_json(authenticated=False))

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({})
    assert _is_error(result)
    assert result["error"] == "not_authenticated"


def test_status_throws_timeout_returns_error(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=args, timeout=60)

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({})
    assert _is_error(result)
    assert result["error"] == "status_failed"


def test_history_nonzero_exit_returns_error(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "status" in args:
            return _proc(_status_json())
        return _proc(stderr="boom", rc=1)

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({})
    assert _is_error(result)
    assert result["error"] == "history_failed"


def test_history_throws_timeout_returns_error(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "status" in args:
            return _proc(_status_json())
        raise subprocess.TimeoutExpired(cmd=args, timeout=60)

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({})
    assert _is_error(result)
    assert result["error"] == "history_failed"


def test_history_bad_json_returns_error(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "status" in args:
            return _proc(_status_json())
        return _proc("not json at all {{{")

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({})
    assert _is_error(result)
    assert result["error"] == "bad_response"


def test_history_ok_false_returns_error(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "status" in args:
            return _proc(_status_json())
        return _proc(json.dumps({"ok": False, "data": {}}))

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({})
    assert _is_error(result)
    assert result["error"] == "bad_response"


def test_empty_items_returns_empty_success(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "status" in args:
            return _proc(_status_json())
        return _proc(_history_json([], count=0))

    _patch(monkeypatch, run=fake_run)
    result = bili_history.invoke({})
    assert _is_success(result)
    assert result["entries"] == []
    assert result["total"] == 0