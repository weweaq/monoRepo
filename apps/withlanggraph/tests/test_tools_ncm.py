"""Tests for gacore.tools.ncm_tools — fully mocked, no real ncm CLI / network / SMTP."""

from __future__ import annotations

import json
import subprocess
import sys
import types

import gacore.tools.ncm_tools as ncm_mod

# Ensure ncm_mod is the real module, not the StructuredTool that @tool shadows in __init__.py.
if not isinstance(ncm_mod, types.ModuleType):
    ncm_mod = sys.modules["gacore.tools.ncm_tools"]

from gacore.tools.email_tools import _SmtpSettings
from gacore.tools.ncm_tools import (
    ncm_login,
    ncm_lyric,
    ncm_me,
    ncm_playlist_detail,
    ncm_playlist_list,
    ncm_search_song,
    ncm_song,
)

_FAKE_NCM = r"C:\fake\ncm.exe"
_FAKE_QR = r"C:\fake\qr-login.png"
_FAKE_UID = "323355696"
_FAKE_NAME = "测试用户"

_UNSET = object()


def _proc(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([_FAKE_NCM], rc, stdout, stderr)


def _me_json(profile: dict | None) -> str:
    return json.dumps({"code": 200, "profile": profile}, ensure_ascii=False)


def _song_dict(song_id: int = 1001, name: str = "测试歌曲", fee: int = 0) -> dict:
    return {
        "id": song_id,
        "name": name,
        "ar": [{"id": 1, "name": "艺人A"}, {"id": 2, "name": "艺人B"}],
        "al": {"id": 10, "name": "测试专辑"},
        "dt": 240000,
        "fee": fee,
    }


def _search_json(songs: list[dict], count: int) -> str:
    return json.dumps({"code": 200, "result": {"songCount": count, "songs": songs}}, ensure_ascii=False)


def _playlist_dict(playlist_id: int = 5001, name: str = "测试歌单", track_count: int = 3) -> dict:
    return {
        "id": playlist_id,
        "name": name,
        "trackCount": track_count,
        "subscribed": True,
        "privacy": 0,
        "creator": {"nickname": "创建者"},
    }


def _playlist_list_json(playlists: list[dict]) -> str:
    return json.dumps({"code": 200, "more": False, "playlist": playlists}, ensure_ascii=False)


def _is_error(result: dict) -> bool:
    return "error" in result


def _is_success(result: dict) -> bool:
    return "error" not in result


def _patch(monkeypatch, find=None, run=None) -> None:
    """Point _find_ncm/_run_cli at fakes; default: ncm found, run left untouched."""
    monkeypatch.setattr(ncm_mod, "_find_ncm", find or (lambda: _FAKE_NCM))
    if run is not None:
        monkeypatch.setattr(ncm_mod, "_run_cli", run)


def _auth_ok_json() -> str:
    return _me_json({"userId": 323355696, "nickname": "西瓜是真好吃", "avatarUrl": "https://x"})


# ---------------------------------------------------------------------------
# ncm_me
# ---------------------------------------------------------------------------


def test_me_not_logged_in_returns_null_profile(monkeypatch) -> None:
    _patch(monkeypatch, run=lambda args: _proc(_me_json(None)))
    result = ncm_me.invoke({})
    assert _is_success(result)
    assert result["code"] == 200
    assert result["user_id"] is None
    assert result["nickname"] is None


def test_me_logged_in_returns_profile(monkeypatch) -> None:
    _patch(monkeypatch, run=lambda args: _proc(_auth_ok_json()))
    result = ncm_me.invoke({})
    assert _is_success(result)
    assert result["user_id"] == 323355696
    assert result["nickname"] == "西瓜是真好吃"


def test_me_ncm_not_found(monkeypatch) -> None:
    _patch(monkeypatch, find=lambda: None)
    result = ncm_me.invoke({})
    assert _is_error(result)
    assert result["error"] == "ncm_not_found"


def test_me_nonzero_exit_returns_error(monkeypatch) -> None:
    _patch(monkeypatch, run=lambda args: _proc(stderr="boom", rc=1))
    result = ncm_me.invoke({})
    assert _is_error(result)
    assert result["error"] == "command_failed"


def test_me_bad_json_returns_error(monkeypatch) -> None:
    _patch(monkeypatch, run=lambda args: _proc("not json {{{"))
    result = ncm_me.invoke({})
    assert _is_error(result)
    assert result["error"] == "bad_response"


def test_me_code_non_200_returns_error(monkeypatch) -> None:
    _patch(monkeypatch, run=lambda args: _proc(json.dumps({"code": 301})))
    result = ncm_me.invoke({})
    assert _is_error(result)
    assert result["error"] == "bad_response"


# ---------------------------------------------------------------------------
# ncm_search_song
# ---------------------------------------------------------------------------


def test_search_empty_keyword_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = ncm_search_song.invoke({"keyword": "  "})
    assert _is_error(result)
    assert result["error"] == "invalid_keyword"


def test_search_invalid_limit_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = ncm_search_song.invoke({"keyword": "x", "limit": 0})
    assert _is_error(result)
    assert result["error"] == "invalid_limit"


def test_search_invalid_offset_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = ncm_search_song.invoke({"keyword": "x", "offset": -1})
    assert _is_error(result)
    assert result["error"] == "invalid_offset"


def test_search_success_parses_songs_and_passes_args(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _proc(_search_json([_song_dict(1001, "晴天"), {"id": 1002, "name": "雨天", "ar": None, "al": None}], 2))

    _patch(monkeypatch, run=fake_run)
    result = ncm_search_song.invoke({"keyword": "晴天", "limit": 20, "offset": 10})

    assert _is_success(result)
    assert result["keyword"] == "晴天"
    assert result["total"] == 2
    first = result["songs"][0]
    assert first["song_id"] == 1001
    assert first["name"] == "晴天"
    assert first["artists"] == "艺人A / 艺人B"
    assert first["album"] == "测试专辑"
    assert first["duration_ms"] == 240000
    assert any("search" in c and "song" in c and "--limit" in c and "20" in c and "--offset" in c and "--json" in c for c in calls)


def test_search_ncm_not_found(monkeypatch) -> None:
    _patch(monkeypatch, find=lambda: None)
    result = ncm_search_song.invoke({"keyword": "x"})
    assert _is_error(result)
    assert result["error"] == "ncm_not_found"


# ---------------------------------------------------------------------------
# ncm_song
# ---------------------------------------------------------------------------


def test_song_invalid_id_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = ncm_song.invoke({"song_id": "abc"})
    assert _is_error(result)
    assert result["error"] == "invalid_song_id"


def test_song_success(monkeypatch) -> None:
    _patch(monkeypatch, run=lambda args: _proc(json.dumps({"code": 200, "songs": [_song_dict(1001)]})))
    result = ncm_song.invoke({"song_id": "1001"})
    assert _is_success(result)
    assert result["song"]["song_id"] == 1001
    assert result["song"]["fee"] == 0


def test_song_not_found(monkeypatch) -> None:
    _patch(monkeypatch, run=lambda args: _proc(json.dumps({"code": 200, "songs": []})))
    result = ncm_song.invoke({"song_id": "9999"})
    assert _is_error(result)
    assert result["error"] == "song_not_found"


# ---------------------------------------------------------------------------
# ncm_lyric
# ---------------------------------------------------------------------------


def test_lyric_invalid_id_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = ncm_lyric.invoke({"song_id": "xyz"})
    assert _is_error(result)
    assert result["error"] == "invalid_song_id"


def test_lyric_success_parses_lrc_and_tlyric(monkeypatch) -> None:
    raw = json.dumps(
        {"code": 200, "lrc": {"lyric": "[00:00.00]测试歌词"}, "tlyric": {"lyric": "[00:00.00]Test Lyrics"}},
        ensure_ascii=False,
    )

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        # ncm lyric 无 --json 标志。
        assert "--json" not in args
        return _proc(raw)

    _patch(monkeypatch, run=fake_run)
    result = ncm_lyric.invoke({"song_id": "1001"})
    assert _is_success(result)
    assert result["song_id"] == 1001
    assert "[00:00.00]测试歌词" in result["lrc"]
    assert "[00:00.00]Test Lyrics" in result["tlyric"]


def test_lyric_missing_translation_returns_empty_tlyric(monkeypatch) -> None:
    raw = json.dumps({"code": 200, "lrc": {"lyric": "[00:00.00]只有原文"}}, ensure_ascii=False)
    _patch(monkeypatch, run=lambda args: _proc(raw))
    result = ncm_lyric.invoke({"song_id": "1001"})
    assert _is_success(result)
    assert result["tlyric"] == ""


# ---------------------------------------------------------------------------
# ncm_playlist_list
# ---------------------------------------------------------------------------


def test_playlist_list_invalid_uid_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = ncm_playlist_list.invoke({"uid": "not-a-number"})
    assert _is_error(result)
    assert result["error"] == "invalid_uid"


def test_playlist_list_not_authenticated(monkeypatch) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "me" in args:
            return _proc(_me_json(None))
        return _proc(_playlist_list_json([_playlist_dict()]))

    _patch(monkeypatch, run=fake_run)
    result = ncm_playlist_list.invoke({})
    assert _is_error(result)
    assert result["error"] == "not_authenticated"


def test_playlist_list_success(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "me" in args:
            return _proc(_auth_ok_json())
        return _proc(_playlist_list_json([_playlist_dict(5001), _playlist_dict(5002, "收藏")]))

    _patch(monkeypatch, run=fake_run)
    result = ncm_playlist_list.invoke({})

    assert _is_success(result)
    assert len(result["playlists"]) == 2
    first = result["playlists"][0]
    assert first["playlist_id"] == 5001
    assert first["name"] == "测试歌单"
    assert first["track_count"] == 3
    assert first["subscribed"] is True
    assert first["creator"] == "创建者"
    assert any("playlist" in c and "list" in c and "--json" in c for c in calls)


# ---------------------------------------------------------------------------
# ncm_playlist_detail
# ---------------------------------------------------------------------------


def test_playlist_detail_invalid_id_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = ncm_playlist_detail.invoke({"playlist_id": "abc"})
    assert _is_error(result)
    assert result["error"] == "invalid_playlist_id"


def test_playlist_detail_invalid_limit_returns_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = ncm_playlist_detail.invoke({"playlist_id": "5001", "limit": 2001})
    assert _is_error(result)
    assert result["error"] == "invalid_limit"


def test_playlist_detail_success(monkeypatch) -> None:
    raw = json.dumps(
        {"code": 200, "playlist": {"id": 5001, "name": "我的歌单", "tracks": [_song_dict(1001), _song_dict(1002, "第二首")]}},
        ensure_ascii=False,
    )

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert "playlist" in args and "show" in args
        return _proc(raw)

    _patch(monkeypatch, run=fake_run)
    result = ncm_playlist_detail.invoke({"playlist_id": "5001", "limit": 50})

    assert _is_success(result)
    assert result["playlist_id"] == 5001
    assert result["name"] == "我的歌单"
    assert result["total"] == 2
    assert result["tracks"][1]["name"] == "第二首"


def test_playlist_detail_playlist_missing(monkeypatch) -> None:
    _patch(monkeypatch, run=lambda args: _proc(json.dumps({"code": 200, "playlist": None})))
    result = ncm_playlist_detail.invoke({"playlist_id": "5001"})
    assert _is_error(result)
    assert result["error"] == "playlist_not_found"


# ---------------------------------------------------------------------------
# ncm_login
# ---------------------------------------------------------------------------


class _FakeSendSync:
    """Stand-in for email_tools._send_sync: records the call and returns a fixed dict."""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self.result


def _login_settings(**overrides: object) -> _SmtpSettings:
    """A fully-configured SMTP settings object (qq provider defaults) with optional overrides."""
    fields: dict[str, object] = {
        "host": "smtp.qq.com",
        "port": 465,
        "ssl": True,
        "user": "sender@qq.com",
        "password": "auth-code",
        "default_to": "me@example.com",
        "timeout": 10,
        **overrides,
    }
    return _SmtpSettings(**fields)  # type: ignore[arg-type]


def _patch_login(monkeypatch, profile=None, settings=None, send_result=None, span_proc=None, wait_qr: object = _UNSET) -> _FakeSendSync:
    """Wire every seam ncm_login touches; returns the fake _send_sync for assertions."""
    monkeypatch.setattr(ncm_mod, "_find_ncm", lambda: _FAKE_NCM)
    monkeypatch.setattr(
        ncm_mod, "_fetch_profile", (lambda ncm: profile) if profile is not None else (lambda ncm: None)
    )
    if span_proc is None:
        monkeypatch.setattr(ncm_mod, "_start_login_proc", lambda ncm: None)
    else:
        monkeypatch.setattr(ncm_mod, "_start_login_proc", span_proc)
    if wait_qr is _UNSET:
        monkeypatch.setattr(ncm_mod, "_wait_for_qr_png", lambda: _FAKE_QR)
    else:
        monkeypatch.setattr(ncm_mod, "_wait_for_qr_png", lambda: wait_qr)
    monkeypatch.setattr(ncm_mod, "_resolve_settings", lambda env: settings if settings is not None else _login_settings())
    fake_send = _FakeSendSync(send_result or {"status": "sent", "to": "me@example.com", "subject": "s", "image_count": 1})
    monkeypatch.setattr(ncm_mod, "_send_sync", fake_send)
    return fake_send


def test_login_ncm_not_found(monkeypatch) -> None:
    monkeypatch.setattr(ncm_mod, "_find_ncm", lambda: None)
    result = ncm_login.invoke({})
    assert _is_error(result)
    assert result["error"] == "ncm_not_found"


def test_login_already_logged_in_returns_without_email(monkeypatch) -> None:
    fake_send = _patch_login(monkeypatch, profile={"nickname": _FAKE_NAME, "user_id": int(_FAKE_UID)})
    result = ncm_login.invoke({})
    assert _is_success(result)
    assert result["status"] == "already_logged_in"
    assert result["nickname"] == _FAKE_NAME
    assert result["user_id"] == int(_FAKE_UID)
    assert fake_send.calls == []


def test_login_smtp_credentials_missing_does_not_spawn(monkeypatch) -> None:
    spawned: list[str] = []
    fake_send = _patch_login(monkeypatch, settings=_login_settings(user="", password=""), span_proc=lambda ncm: spawned.append(ncm))
    result = ncm_login.invoke({"to": "me@example.com"})
    assert _is_error(result)
    assert result["error"] == "smtp_not_configured"
    # 关键回归：凭据缺失时绝不能启动后台轮询进程。
    assert spawned == []
    assert fake_send.calls == []


def test_login_smtp_host_unresolvable_does_not_spawn(monkeypatch) -> None:
    spawned: list[str] = []
    fake_send = _patch_login(monkeypatch, settings=_login_settings(host=""), span_proc=lambda ncm: spawned.append(ncm))
    result = ncm_login.invoke({"to": "me@example.com"})
    assert _is_error(result)
    assert result["error"] == "smtp_not_configured"
    assert spawned == []
    assert fake_send.calls == []


def test_login_recipient_required_when_no_to_and_no_default(monkeypatch) -> None:
    _patch_login(monkeypatch, settings=_login_settings(default_to=""))
    result = ncm_login.invoke({})
    assert _is_error(result)
    assert result["error"] == "recipient_required"


def test_login_spawn_failure_returns_error(monkeypatch) -> None:
    def boom(ncm: str) -> None:
        raise OSError("cannot create process")

    _patch_login(monkeypatch, span_proc=boom)
    result = ncm_login.invoke({"to": "me@example.com"})
    assert _is_error(result)
    assert result["error"] == "command_failed"


def test_login_qr_timeout_returns_error(monkeypatch) -> None:
    _patch_login(monkeypatch, wait_qr=None)
    result = ncm_login.invoke({"to": "me@example.com"})
    assert _is_error(result)
    assert result["error"] == "qr_timeout"


def test_login_sends_email_with_qr_image(monkeypatch) -> None:
    fake_send = _patch_login(monkeypatch)
    result = ncm_login.invoke({"to": "me@example.com"})

    assert _is_success(result)
    assert result["status"] == "qr_sent"
    assert result["email_to"] == "me@example.com"
    assert len(fake_send.calls) == 1
    call = fake_send.calls[0]
    assert call["to_addr"] == "me@example.com"
    assert call["image_paths"] == [_FAKE_QR]
    assert "网易云扫码登录" in str(call["subject"])


def test_login_uses_smtp_to_default_when_to_omitted(monkeypatch) -> None:
    fake_send = _patch_login(monkeypatch, settings=_login_settings(default_to="default@example.com"))
    result = ncm_login.invoke({})
    assert _is_success(result)
    assert result["email_to"] == "default@example.com"
    assert fake_send.calls[0]["to_addr"] == "default@example.com"


def test_login_forwards_email_error(monkeypatch) -> None:
    _patch_login(monkeypatch, send_result={"error": "smtp_failed", "message": "auth failed", "to": "me@example.com"})
    result = ncm_login.invoke({"to": "me@example.com"})
    assert _is_error(result)
    assert result["error"] == "smtp_failed"
    assert "auth failed" in result["message"]


def test_login_schema_exposes_to_but_excludes_env_seam() -> None:
    props = ncm_login.args_schema.model_json_schema()["properties"]
    assert "to" in props
    assert "_env" not in props