"""System time tool for gacore: the authoritative clock the model must use for "what time is it".

Intent: kill time hallucination at the root. The only valid sources for the present moment are
this tool's return value and the [Current time] line injected into the system prompt. No other
time/number in the conversation (user messages, OCR text, image descriptions) is clock evidence.

Returned as a plain string the model can read directly; no I/O, no async, no side effects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

from langchain_core.tools import tool

_TZ: Final = timezone(timedelta(hours=8))
"""Asia/Shanghai, UTC+8 — the project's canonical timezone."""

_WEEKDAYS: Final = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)


@tool
def get_time() -> str:
    """获取当前系统时间（唯一权威时钟来源）。

    返回当前系统时钟的“日期 + 星期 + 时分秒 + 时区”，例如
    "2026-08-26 星期三 14:05:33 (Asia/Shanghai, UTC+8)"。

    需要回答“现在几点、今天几号、今天星期几、过了多久”等时间问题，或要拿时间当论据
    时，必须先调用本工具拿系统时间再作答，不要凭记忆/上下文猜当下时刻。此返回值与系统
    注入的 [Current time] 同源，是时间铁律认定的唯一合法依据。
    """
    now = datetime.now(_TZ)
    return (
        f"{now.strftime('%Y-%m-%d')} {_WEEKDAYS[now.weekday()]} "
        f"{now.strftime('%H:%M:%S')} (Asia/Shanghai, UTC+8)"
    )
