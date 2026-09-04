"""Netease Cloud Music tools for gacore: query songs, lyrics, playlists via the `ncm` CLI.

Wraps the `ncm` CLI (Go binary from the ncm-cli project) the same way bili_history
wraps the `bili` CLI. Requires an authenticated session — run ``ncm login`` once
(QR code scan) on this machine; the credential is persisted locally and reused by
every subcommand.

Subcommands used (all return JSON):

- ``ncm me --json``                         -> account profile (null when not logged in)
- ``ncm search song <kw> --json``           -> song search results
- ``ncm song <id> --json``                  -> single song metadata
- ``ncm lyric <id>``                        -> lyrics (JSON by default; no --json flag)
- ``ncm playlist list --json``              -> current user's playlists
- ``ncm playlist show <id> --json``         -> playlist detail incl. tracks

Parsing stays dependency-free via the stdlib ``json`` module. On Windows the CLI
output is UTF-8, so no console codepage fixing is needed (Go binaries write UTF-8).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from typing import Final, Literal, TypedDict

from langchain_core.tools import tool

from gacore.jsonl_logger import get_logger

from .email_tools import _resolve_settings, _send_sync

logger = get_logger("tools.ncm_tools")

_TIMEOUT_SECONDS: Final = 60

_MIN_LIMIT: Final = 1
_MAX_LIMIT: Final = 200

_NCM_CONFIG_ENV: Final = "NCM_CONFIG_DIR"
_QR_PNG_NAME: Final = "qr-login.png"
_QR_WAIT_SECONDS: Final = 20.0
_QR_POLL_SECONDS: Final = 0.5


class NcmSongEntry(TypedDict):
    """One song from search / playlist / detail output."""

    song_id: int
    name: str
    artists: str  # " / "-joined artist names
    album: str
    duration_ms: int
    fee: int  # 0=free, 1=VIP, 4=付费, 8=数字专辑


class NcmPlaylistEntry(TypedDict):
    """One playlist from `ncm playlist list`."""

    playlist_id: int
    name: str
    track_count: int
    subscribed: bool
    privacy: int
    creator: str | None


class NcmSearchSongResult(TypedDict):
    """Successful song search."""

    keyword: str
    total: int
    songs: list[NcmSongEntry]


class NcmSongResult(TypedDict):
    """Successful single-song query."""

    song: NcmSongEntry


class NcmLyricResult(TypedDict):
    """Successful lyric query."""

    song_id: int
    lrc: str
    tlyric: str  # translation time-synced lyrics, may be empty


class NcmPlaylistListResult(TypedDict):
    """Successful playlist list."""

    uid: int | None
    inherited: bool
    playlists: list[NcmPlaylistEntry]


class NcmPlaylistDetailResult(TypedDict):
    """Successful playlist detail query."""

    playlist_id: int
    name: str
    total: int
    tracks: list[NcmSongEntry]


class NcmMeResult(TypedDict):
    """`ncm me --json` shape: profile is null when not logged in."""

    code: int
    user_id: int | None
    nickname: str | None
    avatar_url: str | None


class NcmToolError(TypedDict):
    """Failed call: machine-readable error tag, message and optional detail."""

    error: str
    message: str
    detail: str | None


def _find_ncm() -> str | None:
    """Locate the `ncm` executable on PATH."""
    return shutil.which("ncm")


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the ncm CLI; Go writes UTF-8 to stdout regardless of console codepage."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,  # callers inspect returncode / JSON payload
    )


def _load_json(stdout: str) -> dict | None:
    """Parse CLI stdout as JSON, returning None on any decode failure."""
    try:
        data = json.loads(stdout)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_song(song: dict) -> NcmSongEntry | None:
    if not isinstance(song, dict):
        return None
    ar = song.get("ar") or song.get("artists") or []
    artists = " / ".join(str(a.get("name") or "") for a in ar if isinstance(a, dict) and a.get("name"))
    al = song.get("al") or song.get("album") or {}
    album = str(al.get("name") or "") if isinstance(al, dict) else ""
    return NcmSongEntry(
        song_id=int(song.get("id") or 0),
        name=str(song.get("name") or ""),
        artists=artists,
        album=album,
        duration_ms=int(song.get("dt") or 0),
        fee=int(song.get("fee") or 0),
    )


def _parse_playlist(pl: dict) -> NcmPlaylistEntry | None:
    if not isinstance(pl, dict):
        return None
    creator = None
    creator_obj = pl.get("creator")
    if isinstance(creator_obj, dict):
        creator = creator_obj.get("nickname") or creator_obj.get("userId")
        creator = str(creator) if creator is not None else None
    return NcmPlaylistEntry(
        playlist_id=int(pl.get("id") or 0),
        name=str(pl.get("name") or ""),
        track_count=int(pl.get("trackCount") or 0),
        subscribed=bool(pl.get("subscribed")),
        privacy=int(pl.get("privacy") or 0),
        creator=creator,
    )


def _auth_error() -> NcmToolError:
    return NcmToolError(
        error="not_authenticated",
        message="网易云未登录或登录态已失效，请先运行 `ncm login` 扫码登录。",
        detail=None,
    )


@tool
def ncm_me() -> NcmMeResult | NcmToolError:
    """获取当前网易云登录账号信息（用户ID、昵称、头像）。

    需要本机已通过 `ncm login` 扫码登录过网易云音乐。当未登录或登录态失效时，
    profile 为 null，调用方应据此提示用户先执行登录。用作其他 ncm_* 工具的
    登录态前置检查。

    Returns:
        成功: {"code": 200, "user_id": n, "nickname": "xxx", "avatar_url": "..."}
        未登录: {"code": 200, "user_id": null, "nickname": null, "avatar_url": null}
        失败: {"error": ..., "message": ..., "detail": ...}
    """
    ncm = _find_ncm()
    if ncm is None:
        return NcmToolError(
            error="ncm_not_found",
            message="ncm CLI not found on PATH. Install it from the ncm-cli project "
            "and run `ncm login` once to authenticate.",
            detail=None,
        )
    try:
        proc = _run_cli([ncm, "me", "--json"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ncm_me: call failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return NcmToolError(error="command_failed", message=f"ncm me failed: {exc}", detail=None)
    if proc.returncode != 0:
        return NcmToolError(
            error="command_failed",
            message=f"ncm me exited with code {proc.returncode}",
            detail=(proc.stderr or proc.stdout)[-500:] or None,
        )
    data = _load_json(proc.stdout)
    if data is None or data.get("code") != 200:
        return NcmToolError(
            error="bad_response",
            message="ncm me returned an unparseable or error response",
            detail=proc.stdout[:500] or None,
        )
    profile = data.get("profile")
    if not isinstance(profile, dict):
        return NcmMeResult(code=200, user_id=None, nickname=None, avatar_url=None)
    return NcmMeResult(
        code=200,
        user_id=int(profile.get("userId") or 0) or None,
        nickname=profile.get("nickname"),
        avatar_url=profile.get("avatarUrl"),
    )


def _require_login(ncm: str) -> NcmToolError | None:
    """Check login via `ncm me`; return an auth error dict or None when OK."""
    try:
        proc = _run_cli([ncm, "me", "--json"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ncm auth check failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return NcmToolError(error="command_failed", message=f"ncm me failed: {exc}", detail=None)
    if proc.returncode != 0:
        return NcmToolError(
            error="command_failed",
            message=f"ncm me exited with code {proc.returncode}",
            detail=(proc.stderr or proc.stdout)[-500:] or None,
        )
    data = _load_json(proc.stdout)
    profile = (data or {}).get("profile") if isinstance(data, dict) else None
    if not isinstance(profile, dict) or not profile.get("userId"):
        return _auth_error()
    return None


def _fetch_profile(ncm: str) -> dict | None:
    """Return {"nickname": str, "user_id": int} for the current session, or None when not logged in."""
    try:
        proc = _run_cli([ncm, "me", "--json"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ncm profile fetch failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return None
    if proc.returncode != 0:
        return None
    data = _load_json(proc.stdout)
    profile = (data or {}).get("profile") if isinstance(data, dict) else None
    if not isinstance(profile, dict) or not profile.get("userId"):
        return None
    return {
        "nickname": profile.get("nickname"),
        "user_id": int(profile.get("userId") or 0) or None,
    }


@tool
def ncm_search_song(keyword: str, limit: int = 30, offset: int = 0) -> NcmSearchSongResult | NcmToolError:
    """搜索网易云歌曲，按相关度返回匹配的歌曲列表。

    当需要根据关键词（歌名、艺人、专辑）定位歌曲 ID，或查看某首歌的曲库信息时使用。
    返回的 song_id 可传给 ncm_song / ncm_lyric 继续查询。

    Args:
        keyword: 搜索关键词，如歌名、艺人名。
        limit: 返回条数，1-200，默认 30。
        offset: 分页偏移，默认 0。

    Returns:
        成功: {"keyword": "...", "total": n, "songs": [{"song_id","name","artists","album","duration_ms","fee"}]}
        失败: {"error": ..., "message": ..., "detail": ...}
    """
    if not keyword or not keyword.strip():
        return NcmToolError(error="invalid_keyword", message="keyword 不能为空", detail=None)
    if limit < _MIN_LIMIT or limit > _MAX_LIMIT:
        return NcmToolError(
            error="invalid_limit",
            message=f"limit must be between {_MIN_LIMIT} and {_MAX_LIMIT}, got {limit}",
            detail=None,
        )
    if offset < 0:
        return NcmToolError(error="invalid_offset", message=f"offset must be >= 0, got {offset}", detail=None)

    ncm = _find_ncm()
    if ncm is None:
        return NcmToolError(
            error="ncm_not_found",
            message="ncm CLI not found on PATH. Install it from the ncm-cli project.",
            detail=None,
        )
    try:
        proc = _run_cli([ncm, "search", "song", keyword.strip(), "--limit", str(limit), "--offset", str(offset), "--json"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ncm_search_song: call failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return NcmToolError(error="command_failed", message=f"ncm search song failed: {exc}", detail=None)
    if proc.returncode != 0:
        return NcmToolError(
            error="command_failed",
            message=f"ncm search song exited with code {proc.returncode}",
            detail=(proc.stderr or proc.stdout)[-500:] or None,
        )
    data = _load_json(proc.stdout)
    if data is None or data.get("code") != 200:
        return NcmToolError(
            error="bad_response",
            message="ncm search song returned an unparseable or error response",
            detail=proc.stdout[:500] or None,
        )
    result = data.get("result") or {}
    raw_songs = result.get("songs") or []
    songs = [s for s in (_parse_song(x) for x in raw_songs) if s is not None]
    logger.info("ncm_search_song success", keyword=keyword, total=songs and len(songs) or 0)
    return NcmSearchSongResult(keyword=keyword.strip(), total=len(songs), songs=songs)


def _song_id_arg(song_id: str) -> int | NcmToolError:
    if not isinstance(song_id, str) or not song_id.strip().isdigit():
        return NcmToolError(error="invalid_song_id", message=f"无效的歌曲ID: {song_id!r}", detail=None)
    return int(song_id.strip())


@tool
def ncm_song(song_id: str) -> NcmSongResult | NcmToolError:
    """获取网易云单曲的元数据（ID、名称、艺人、专辑、时长、版权）。

    当需要确认一首歌的完整曲库信息或播放权限（fee：0=免费，1=VIP，4=付费，8=数字专辑）时使用。

    Args:
        song_id: 歌曲ID（数字字符串），可从 ncm_search_song 结果获取。

    Returns:
        成功: {"song": {"song_id","name","artists","album","duration_ms","fee"}}
        失败: {"error": ..., "message": ..., "detail": ...}
    """
    sid = _song_id_arg(song_id)
    if isinstance(sid, dict):
        return sid

    ncm = _find_ncm()
    if ncm is None:
        return NcmToolError(
            error="ncm_not_found",
            message="ncm CLI not found on PATH. Install it from the ncm-cli project.",
            detail=None,
        )
    try:
        proc = _run_cli([ncm, "song", str(sid), "--json"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ncm_song: call failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return NcmToolError(error="command_failed", message=f"ncm song failed: {exc}", detail=None)
    if proc.returncode != 0:
        return NcmToolError(
            error="command_failed",
            message=f"ncm song exited with code {proc.returncode}",
            detail=(proc.stderr or proc.stdout)[-500:] or None,
        )
    data = _load_json(proc.stdout)
    if data is None or data.get("code") != 200:
        return NcmToolError(
            error="bad_response",
            message="ncm song returned an unparseable or error response",
            detail=proc.stdout[:500] or None,
        )
    raw = (data.get("songs") or [])
    if not raw:
        return NcmToolError(error="song_not_found", message=f"未找到歌曲 {sid}", detail=None)
    song = _parse_song(raw[0])
    if song is None:
        return NcmToolError(error="bad_response", message="ncm song returned an invalid song entry", detail=None)
    return NcmSongResult(song=song)


@tool
def ncm_lyric(song_id: str) -> NcmLyricResult | NcmToolError:
    """获取网易云歌曲的歌词（原文 LRC 与翻译 LRC）。

    当需要查看某首歌的完整歌词（含时间轴）或翻译歌词时使用。song_id 可从
    ncm_search_song 结果获取。

    Args:
        song_id: 歌曲ID（数字字符串）。

    Returns:
        成功: {"song_id": n, "lrc": "LRC 歌词文本", "tlyric": "翻译歌词（可能为空）"}
        失败: {"error": ..., "message": ..., "detail": ...}
    """
    sid = _song_id_arg(song_id)
    if isinstance(sid, dict):
        return sid

    ncm = _find_ncm()
    if ncm is None:
        return NcmToolError(
            error="ncm_not_found",
            message="ncm CLI not found on PATH. Install it from the ncm-cli project.",
            detail=None,
        )
    # ncm lyric 无 --json 标志，默认即为 JSON 输出。
    try:
        proc = _run_cli([ncm, "lyric", str(sid)])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ncm_lyric: call failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return NcmToolError(error="command_failed", message=f"ncm lyric failed: {exc}", detail=None)
    if proc.returncode != 0:
        return NcmToolError(
            error="command_failed",
            message=f"ncm lyric exited with code {proc.returncode}",
            detail=(proc.stderr or proc.stdout)[-500:] or None,
        )
    data = _load_json(proc.stdout)
    if data is None or data.get("code") != 200:
        return NcmToolError(
            error="bad_response",
            message="ncm lyric returned an unparseable or error response",
            detail=proc.stdout[:500] or None,
        )
    lrc_obj = data.get("lrc") or {}
    tlr_obj = data.get("tlyric") or {}
    lrc = str(lrc_obj.get("lyric") or "") if isinstance(lrc_obj, dict) else ""
    tlyric = str(tlr_obj.get("lyric") or "") if isinstance(tlr_obj, dict) else ""
    return NcmLyricResult(song_id=sid, lrc=lrc, tlyric=tlyric)


@tool
def ncm_playlist_list(uid: str | None = None, limit: int = 100, offset: int = 0) -> NcmPlaylistListResult | NcmToolError:
    """列出网易云账号的歌单（自建 + 收藏）。

    当需要了解用户有哪些歌单、歌单 ID 与歌曲规模时使用。歌单 ID 可传给
    ncm_playlist_detail 查看歌单内的歌曲。

    Args:
        uid: 用户ID（数字字符串），默认当前登录用户。
        limit: 返回条数，1-200，默认 100。
        offset: 分页偏移，默认 0。

    Returns:
        成功: {"uid": n, "inherited": bool, "playlists": [{"playlist_id","name","track_count","subscribed","privacy","creator"}]}
        失败: {"error": ..., "message": ..., "detail": ...}
    """
    if limit < _MIN_LIMIT or limit > _MAX_LIMIT:
        return NcmToolError(
            error="invalid_limit",
            message=f"limit must be between {_MIN_LIMIT} and {_MAX_LIMIT}, got {limit}",
            detail=None,
        )
    if offset < 0:
        return NcmToolError(error="invalid_offset", message=f"offset must be >= 0, got {offset}", detail=None)
    if uid is not None and (not str(uid).strip().isdigit()):
        return NcmToolError(error="invalid_uid", message=f"无效的用户ID: {uid!r}", detail=None)

    ncm = _find_ncm()
    if ncm is None:
        return NcmToolError(
            error="ncm_not_found",
            message="ncm CLI not found on PATH. Install it from the ncm-cli project.",
            detail=None,
        )
    auth_err = _require_login(ncm)
    if auth_err is not None:
        return auth_err

    args = [ncm, "playlist", "list", "--limit", str(limit), "--offset", str(offset)]
    if uid is not None:
        args += ["--uid", str(uid).strip()]
    args.append("--json")
    try:
        proc = _run_cli(args)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ncm_playlist_list: call failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return NcmToolError(error="command_failed", message=f"ncm playlist list failed: {exc}", detail=None)
    if proc.returncode != 0:
        return NcmToolError(
            error="command_failed",
            message=f"ncm playlist list exited with code {proc.returncode}",
            detail=(proc.stderr or proc.stdout)[-500:] or None,
        )
    data = _load_json(proc.stdout)
    if data is None or data.get("code") != 200:
        return NcmToolError(
            error="bad_response",
            message="ncm playlist list returned an unparseable or error response",
            detail=proc.stdout[:500] or None,
        )
    raw = data.get("playlist") or []
    playlists = [p for p in (_parse_playlist(x) for x in raw) if p is not None]
    return NcmPlaylistListResult(uid=int(uid) if uid else None, inherited=bool(data.get("inherited")), playlists=playlists)


@tool
def ncm_playlist_detail(playlist_id: str, limit: int = 1000) -> NcmPlaylistDetailResult | NcmToolError:
    """获取网易云歌单的详情与歌曲列表。

    当需要查看歌单内包含哪些歌曲（每一首的 ID、名称、艺人、专辑、时长）时使用。

    Args:
        playlist_id: 歌单ID（数字字符串），可从 ncm_playlist_list 结果获取。
        limit: 返回歌曲条数，1-2000，默认 1000。

    Returns:
        成功: {"playlist_id": n, "name": "...", "total": n, "tracks": [{"song_id","name","artists","album","duration_ms","fee"}]}
        失败: {"error": ..., "message": ..., "detail": ...}
    """
    if not isinstance(playlist_id, str) or not playlist_id.strip().isdigit():
        return NcmToolError(error="invalid_playlist_id", message=f"无效的歌单ID: {playlist_id!r}", detail=None)
    if limit < _MIN_LIMIT or limit > 2000:
        return NcmToolError(
            error="invalid_limit",
            message=f"limit must be between {_MIN_LIMIT} and 2000, got {limit}",
            detail=None,
        )

    ncm = _find_ncm()
    if ncm is None:
        return NcmToolError(
            error="ncm_not_found",
            message="ncm CLI not found on PATH. Install it from the ncm-cli project.",
            detail=None,
        )
    try:
        proc = _run_cli([ncm, "playlist", "show", playlist_id.strip(), "--limit", str(limit), "--json"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ncm_playlist_detail: call failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return NcmToolError(error="command_failed", message=f"ncm playlist show failed: {exc}", detail=None)
    if proc.returncode != 0:
        return NcmToolError(
            error="command_failed",
            message=f"ncm playlist show exited with code {proc.returncode}",
            detail=(proc.stderr or proc.stdout)[-500:] or None,
        )
    data = _load_json(proc.stdout)
    if data is None or data.get("code") != 200:
        return NcmToolError(
            error="bad_response",
            message="ncm playlist show returned an unparseable or error response",
            detail=proc.stdout[:500] or None,
        )
    pl = data.get("playlist")
    if not isinstance(pl, dict):
        return NcmToolError(error="playlist_not_found", message=f"未找到歌单 {playlist_id}", detail=None)
    raw_tracks = pl.get("tracks") or []
    tracks = [t for t in (_parse_song(x) for x in raw_tracks) if t is not None]
    name = str(pl.get("name") or "")
    logger.info("ncm_playlist_detail success", playlist_id=playlist_id, total=tracks and len(tracks) or 0)
    return NcmPlaylistDetailResult(
        playlist_id=int(playlist_id.strip()),
        name=name,
        total=len(tracks),
        tracks=tracks,
    )


class NcmLoginResult(TypedDict):
    """ncm_login outcome: either a QR email was sent, or the user is already logged in."""

    status: Literal["qr_sent", "already_logged_in"]
    nickname: str | None
    user_id: int | None
    email_to: str | None
    note: str


def _session_dir() -> str:
    """Resolve the ncm-cli session dir the same way the Go binary does.

    NCM_CONFIG_DIR overrides the default %APPDATA%\\ncm-cli; the QR PNG lives in
    <config-dir>/session/qr-login.png.
    """
    config_dir = os.environ.get(_NCM_CONFIG_ENV, "").strip()
    if not config_dir:
        config_dir = os.path.join(os.environ.get("APPDATA", ""), "ncm-cli")
    return os.path.join(config_dir, "session")


def _qr_png_path() -> str:
    return os.path.join(_session_dir(), _QR_PNG_NAME)


def _wait_for_qr_png(timeout: float = _QR_WAIT_SECONDS) -> str | None:
    """Poll until the QR PNG exists and is non-empty; return its path or None on timeout."""
    path = _qr_png_path()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if os.path.getsize(path) > 0:
                return path
        except OSError:
            pass
        time.sleep(_QR_POLL_SECONDS)
    return None


def _start_login_proc(ncm: str) -> None:
    """Launch `ncm login` in the background; it polls the scan status and saves the
    session itself (up to 10 minutes, refreshing an expired QR automatically)."""
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    subprocess.Popen(
        [ncm, "login"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


@tool
def ncm_login(to: str | None = None, _env: Mapping[str, str] | None = None) -> NcmLoginResult | NcmToolError:
    """发起网易云扫码登录：生成登录二维码并以邮件发送给你，后台等待扫码确认。

    当 ncm_me 显示未登录或登录态失效时调用本工具。流程：
    1. 后台启动 `ncm login` 生成二维码（PNG 保存在 ncm-cli session 目录）；
    2. 将二维码图片通过邮件发送到收件箱（收件人取 SMTP_TO，或传入 to 参数）；
    3. 工具立即返回，后台进程继续轮询扫码结果（最长 10 分钟）；
    4. 用手机 App 扫码并确认后，登录态自动保存；随后可用 ncm_me 验证账号。

    Args:
        to: 接收二维码的邮箱地址；为空时使用 SMTP_TO 环境变量配置的默认收件人。

    Returns:
        已登录: {"status": "already_logged_in", "nickname": "...", "user_id": n, ...}
        已发码: {"status": "qr_sent", "email_to": "...", "note": "请查收邮件并扫码..."}
        失败: {"error": ..., "message": ..., "detail": ...}
    """
    ncm = _find_ncm()
    if ncm is None:
        return NcmToolError(
            error="ncm_not_found",
            message="ncm CLI not found on PATH. Install it from the ncm-cli project.",
            detail=None,
        )

    # 已登录则直接返回，避免重复扫码。
    profile = _fetch_profile(ncm)
    if profile is not None:
        return NcmLoginResult(
            status="already_logged_in",
            nickname=profile["nickname"],
            user_id=profile["user_id"],
            email_to=None,
            note="已登录，无需扫码。若需切换账号，请先清理 ncm-cli session 后再调用。",
        )

    # SMTP 凭据必须在启动后台进程前校验：发码通道不可用时不要留下轮询进程。
    env = os.environ if _env is None else _env
    settings = _resolve_settings(env)
    if not settings.user or not settings.password:
        logger.warning("ncm_login skipped: SMTP credentials not configured")
        return NcmToolError(
            error="smtp_not_configured",
            message="SMTP_USER / SMTP_PASSWORD 未配置，无法发送二维码邮件。",
            detail=None,
        )
    if not settings.host:
        logger.warning("ncm_login skipped: no SMTP host and provider unknown")
        return NcmToolError(
            error="smtp_not_configured",
            message="无法从 SMTP_USER 解析 SMTP_HOST。",
            detail=None,
        )
    recipient = (to or "").strip() or settings.default_to
    if not recipient:
        return NcmToolError(
            error="recipient_required",
            message="未指定收件邮箱：请传 to 参数，或在环境中配置 SMTP_TO。",
            detail=None,
        )

    try:
        _start_login_proc(ncm)
    except OSError as exc:
        logger.error("ncm_login: failed to spawn ncm login", error_type=type(exc).__name__, stack_trace=str(exc))
        return NcmToolError(error="command_failed", message=f"failed to start `ncm login`: {exc}", detail=None)

    qr_png = _wait_for_qr_png()
    if qr_png is None:
        return NcmToolError(
            error="qr_timeout",
            message=f"等待二维码生成超时（{_QR_WAIT_SECONDS:.0f}s），请重试。",
            detail=None,
        )

    email_result = _send_sync(
        subject="网易云扫码登录",
        to_addr=recipient,
        body=(
            "<p>请用网易云音乐 App 扫描下方二维码完成登录。</p>"
            "<p>二维码约 3 分钟内有效；若失效，请重新调用 ncm_login 获取新码。</p>"
            "<p>扫码并确认后无需回复本邮件，登录状态会自动保存。</p>"
        ),
        settings=settings,
        image_paths=[qr_png],
    )
    if isinstance(email_result, dict) and email_result.get("error"):
        return NcmToolError(
            error=str(email_result.get("error")),
            message=str(email_result.get("message") or "发送二维码邮件失败"),
            detail=None,
        )

    return NcmLoginResult(
        status="qr_sent",
        nickname=None,
        user_id=None,
        email_to=recipient,
        note="二维码邮件已发送，请查收后用网易云音乐 App 扫码并确认登录；完成后调用 ncm_me 验证。",
    )