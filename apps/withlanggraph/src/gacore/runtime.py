"""Run-scope ambient state: the historical-day fence for reruns ("as-of" anchoring).

A scheduled rerun (``rerun --day X`` / ``--week W``) must see the world as of
X 23:59: the live "today" (memory notes injection, fact card, clock anchor,
memory writes) would otherwise leak post-X events into the regenerated report
(2026-10-25 incident: the 10-23 rerun written as a Sunday story).

Tools and context layers read the ambient value via :func:`as_of_day`;
:func:`gacore.scheduler.run_job` wraps every historical run in
:func:`as_of_context` (a ContextVar, same pattern as ``graph._memory_cfg``),
so nothing needs cfg threading and ``build_tool_list``'s self-configuring
tools stay untouched.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from typing import Iterator

_as_of_day: ContextVar[str | None] = ContextVar("gacore_as_of_day", default=None)


def as_of_day() -> str | None:
    """Historical day (YYYY-MM-DD) this run is anchored to; None = live today."""
    return _as_of_day.get()


@contextmanager
def as_of_context(day: str | None) -> Iterator[None]:
    """Scope the ambient "today" to ``day`` for the duration of the block."""
    token = _as_of_day.set(day)
    try:
        yield
    finally:
        _as_of_day.reset(token)


def week_sunday(week: str | None) -> str | None:
    """Map a ``YYYY-Www`` week id to its ISO Sunday (the weekly report's writing day).

    None passes through so callers can chain ``for_day or week_sunday(for_week)``.
    """
    if not week:
        return None
    year, w = week.split("-W")
    return date.fromisocalendar(int(year), int(w), 7).isoformat()
