"""LangChain tools for report auditing: the last_seen lookup the daily-report
prompt contract requires (数字查表替代语感, spec §5.2).

Thin wrapper over ``gacore.audit.last_seen``: self-configures via ``Config.default()``
like every other registered tool, so the registry needs no configuration to build.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from langchain_core.tools import tool

from gacore.audit import last_seen as _last_seen
from gacore.config import Config
from gacore.runtime import as_of_day

_TZ = timezone(timedelta(hours=8))


@tool
def last_seen(topic: str, before_date: str = "") -> dict:
    """查一个主题最近一次出现在记忆/历史日报里的日期与间隔天数。

    用途（日报硬规则）：凡要写「N 天/周/月 + 沉寂/时隔/连续/首次/重新…」这类时间量级断言，
    必须先调本工具查表，再按结果三选一：正文带（上次：YYYY-MM-DD，间隔 N 天）、
    改写成问句放进「想问你」节、或删掉量级只留观察。禁止凭语感直接写量级。

    Args:
        topic: 主题词（如「硬件」「工学椅」；同义词自动匹配）。
        before_date: 基准日 `YYYY-MM-DD`，缺省=历史重跑的 as-of 目标日，再缺省=今天（东八区）。
            查的是严格早于该日的最近一次；格式不合法直接返回 error（不猜）。

    Returns:
        {found, date, gap_days, source, snippet, alt}；`found=False` 表示查不到先例
        （此时「首次」类断言无基线可对，考虑改成问句或删量级）。`alt` 是更早的 1–2 个
        命中日期（回溯提及会把最近一次拉近，供人判断“提及”与“事件”）。带 `error` 表示
        入参不合法，**不要**当成「无先例」。
    """
    day = (before_date or "").strip()
    if day:
        try:
            # 严格 ISO：strptime 的 %m/%d 接受 1 位数字，会把 2026-1-5 放行后崩在 fromisoformat
            day = date.fromisoformat(day).isoformat()
        except ValueError:
            return {"found": False, "error": f"before_date 需为 YYYY-MM-DD，收到 {before_date!r}"}
    else:
        # 历史重跑按 as-of 目标日查表（决策 36）：否则会以真实今天为基准，把未来事件当"上次"
        day = as_of_day() or datetime.now(tz=_TZ).date().isoformat()
    cfg = Config.default()
    seen = _last_seen(cfg, topic, day)
    return {
        "found": seen.found,
        "date": seen.date,
        "gap_days": seen.gap_days,
        "source": seen.source,
        "snippet": seen.snippet,
        "alt": list(seen.alt),
    }
