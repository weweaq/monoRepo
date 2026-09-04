"""Bilibili watch-history tool for gacore: fetch the logged-in user's Bilibili viewing history.

Wraps the `bili` CLI (pip package ``bili-cli``, entry point ``bili``) that the
GenericAgent project already relies on for Bilibili automation. Requires an
authenticated session — run ``bili login`` once (QR code scan) on this machine;
the credential is persisted locally and validated on each call.

Two subcommands are used, both with ``--json`` so parsing stays dependency-free
via the stdlib ``json`` module:

- ``bili status --json``  -> account name + authenticated flag
- ``bili history -n N -p P --json`` -> the watch history entries

Known pitfall preserved from GA: on Windows the CLI help/errors crash with
``UnicodeEncodeError: 'gbk' codec`` when emoji hit the console, so
``PYTHONIOENCODING=utf-8`` is forced into the subprocess environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Final, TypedDict

from langchain_core.tools import tool

from gacore.jsonl_logger import get_logger

logger = get_logger("tools.bili_history")

_MIN_LIMIT: Final = 1
_MAX_LIMIT: Final = 100
_TIMEOUT_SECONDS: Final = 60

_ENV: Final = {**os.environ, "PYTHONIOENCODING": "utf-8"}


class BiliHistoryEntry(TypedDict):
    """One watched video from the history feed."""

    bvid: str
    title: str
    author: str
    viewed_at: str  # ISO 8601 本地时间
    video_duration_seconds: int | None  # 视频本身的总时长（秒），非用户观看时长，仅 include_duration=True 时有值


class BiliHistoryResult(TypedDict):
    """Successful fetch: entries, pagination info and the authenticated account."""

    entries: list[BiliHistoryEntry]
    total: int
    page: int
    account: str | None  # 当前登录的 Bilibili 账号名
    uid: str | None


class BiliHistoryError(TypedDict):
    """Failed fetch: machine-readable error tag, message and optional detail."""

    error: str
    message: str
    detail: str | None


def _find_bili() -> str | None:
    """Locate the `bili` executable on PATH."""
    return shutil.which("bili")


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the bili CLI with UTF-8 forced to dodge the Windows GBK console bug."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_TIMEOUT_SECONDS,
        env=_ENV,
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


def _fetch_one_duration(bili: str, bvid: str) -> int | None:
    """Fetch the video_duration_seconds for a single video via ``bili video --json``.

    Returns None when the CLI call fails (timeout, bad json, missing field, etc.).
    """
    try:
        proc = _run_cli([bili, "video", bvid, "--json"])
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    data = _load_json(proc.stdout)
    if data is None:
        return None
    video = (data.get("data") or {}).get("video")
    if not isinstance(video, dict):
        return None
    ds = video.get("duration_seconds")
    return int(ds) if isinstance(ds, (int, float)) else None


@tool
def bili_history(limit: int = 30, page: int = 1, include_duration: bool = False) -> BiliHistoryResult | BiliHistoryError:
    """获取当前登录 Bilibili 账号的观看历史记录（最近看过的视频列表）。

    需要本机已通过 `bili login` 扫码登录过 Bilibili（登录态持久保存在本地）。
    返回最近观看的视频条目：BV 号、标题、UP 主、观看时间（ISO 8601），
    按观看时间倒序排列。当需要了解用户最近在 B 站看了什么、复盘观看行为时使用。

    Args:
        limit: 返回条数，1-100，默认 30。
        page: 页码，默认 1。
        include_duration: 是否并发拉取每条视频的本身总时长（video_duration_seconds，秒），
            非用户实际观看时长。开启后每条视频调用 ``bili video`` 补充时长，
            用于判断长视频兴趣信号（视频本身 >20min 说明用户对该话题有兴趣）。

    Returns:
        成功: {"entries": [{"bvid","title","author","viewed_at","video_duration_seconds"}...],
               "total": n, "page": p, "account": "账号名", "uid": "uid"}
        失败: {"error": "错误标签", "message": "说明", "detail": 详细或 null}
    """
    if limit < _MIN_LIMIT or limit > _MAX_LIMIT:
        return BiliHistoryError(
            error="invalid_limit",
            message=f"limit must be between {_MIN_LIMIT} and {_MAX_LIMIT}, got {limit}",
            detail=None,
        )
    if page < 1:
        return BiliHistoryError(
            error="invalid_page",
            message=f"page must be >= 1, got {page}",
            detail=None,
        )

    bili = _find_bili()
    if bili is None:
        return BiliHistoryError(
            error="bili_not_found",
            message="bili CLI not found on PATH. Install it with `pip install bili-cli` "
            "then run `bili login` once to authenticate.",
            detail=None,
        )

    # 登录状态 + 账号名
    account: str | None = None
    uid: str | None = None
    try:
        status = _run_cli([bili, "status", "--json"])
        status_data = _load_json(status.stdout)
        if status_data and status_data.get("ok"):
            sdata = status_data.get("data") or {}
            if not sdata.get("authenticated"):
                return BiliHistoryError(
                    error="not_authenticated",
                    message="Bilibili 未登录或登录态已失效，请先运行 `bili login` 扫码登录。",
                    detail="authenticated=false from `bili status`",
                )
            user = sdata.get("user") or {}
            account = user.get("name") or user.get("username")
            uid = user.get("id")
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error(
            "bili_history: status check failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
        )
        return BiliHistoryError(error="status_failed", message=f"bili status failed: {exc}", detail=None)

    # 拉取观看历史
    try:
        proc = _run_cli([bili, "history", "-n", str(limit), "-p", str(page), "--json"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error(
            "bili_history: history call failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
        )
        return BiliHistoryError(error="history_failed", message=f"bili history failed: {exc}", detail=None)

    if proc.returncode != 0:
        return BiliHistoryError(
            error="history_failed",
            message=f"bili history exited with code {proc.returncode}",
            detail=(proc.stderr or proc.stdout)[-500:] or None,
        )

    data = _load_json(proc.stdout)
    if data is None or not data.get("ok"):
        return BiliHistoryError(
            error="bad_response",
            message="bili history returned an unparseable or error response",
            detail=proc.stdout[:500] or None,
        )

    d = data.get("data") or {}
    items = d.get("items") or []
    entries = [
        BiliHistoryEntry(
            bvid=str(it.get("bvid") or it.get("id") or ""),
            title=str(it.get("title") or ""),
            author=str(it.get("author") or ""),
            viewed_at=str(it.get("viewed_at") or ""),
        )
        for it in items
        if isinstance(it, dict)
    ]
    total = int(d.get("count") or len(entries))
    got_page = int(d.get("page") or page)

    # 并发拉取每条视频的时长
    if include_duration and entries:
        bvids = [e["bvid"] for e in entries if e["bvid"]]
        durations: dict[str, int | None] = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_one_duration, bili, bv): bv for bv in bvids}
            for future in as_completed(futures):
                bv = futures[future]
                try:
                    durations[bv] = future.result()
                except Exception:
                    durations[bv] = None
        for entry in entries:
            entry["video_duration_seconds"] = durations.get(entry["bvid"])

    logger.info("bili_history success", total=total, page=got_page, account=account, uid=uid)
    return BiliHistoryResult(entries=entries, total=total, page=got_page, account=account, uid=uid)