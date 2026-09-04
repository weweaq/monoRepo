"""Tool registry for gacore: the canonical set of tools bound into the graph.

This package is the single source of truth for which tools exist. The implementations
live in sibling modules; this __init__ only imports them and exposes them as a stable,
ordered TOOL_NAMES tuple plus build_tool_list() so the LLM binding and the graph builder
always agree on the exact same set.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from gacore.config import Config

from .ask_user import ask_user
from .bili_history import bili_history
from .browser_history import browser_history
from .code_run import code_run
from .daily_notes import edit_daily, read_daily, search_daily
from .email_tools import send_email
from .file_tools import file_patch, file_read, file_write
from .get_time import get_time
from .memory_tools import start_long_term_update, update_working_checkpoint
from .ncm_tools import (
    ncm_login,
    ncm_lyric,
    ncm_me,
    ncm_playlist_detail,
    ncm_playlist_list,
    ncm_search_song,
    ncm_song,
)
from .ocr_tools import ocr_image, ocr_screen
from .qq_tools import qq_push
from .web_tools import web_execute_js, web_scan
from .langTrack_tools import langTrack_stats

__all__ = ["TOOL_NAMES", "build_tool_list"]

TOOL_NAMES: tuple[str, ...] = (
    "get_time",
    "code_run",
    "file_read",
    "file_patch",
    "file_write",
    "web_scan",
    "web_execute_js",
    "browser_history",
    "bili_history",
    "ncm_me",
    "ncm_login",
    "ncm_search_song",
    "ncm_song",
    "ncm_lyric",
    "ncm_playlist_list",
    "ncm_playlist_detail",
    "update_working_checkpoint",
    "start_long_term_update",
    "read_daily",
    "edit_daily",
    "search_daily",
    "ask_user",
    "ocr_image",
    "ocr_screen",
    "qq_push",
    "send_email",
    "langTrack_stats",
)

_TOOLS: tuple[BaseTool, ...] = (
    get_time,
    code_run,
    file_read,
    file_patch,
    file_write,
    web_scan,
    web_execute_js,
    browser_history,
    bili_history,
    ncm_me,
    ncm_login,
    ncm_search_song,
    ncm_song,
    ncm_lyric,
    ncm_playlist_list,
    ncm_playlist_detail,
    update_working_checkpoint,
    start_long_term_update,
    read_daily,
    edit_daily,
    search_daily,
    ask_user,
    ocr_image,
    ocr_screen,
    qq_push,
    send_email,
    langTrack_stats,
)


def build_tool_list(cfg: Config) -> list[BaseTool]:
    """Return every registered tool in canonical TOOL_NAMES order.

    cfg is accepted for signature symmetry with test configuration, but each tool
    self-configures at runtime (Config.default() fallback); the registry needs no
    configuration to build.
    """
    return list(_TOOLS)
