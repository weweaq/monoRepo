"""on_scheduler_done terminal-path tests (cancelled / died / stopped / unexpected)."""

from __future__ import annotations

import threading

import pytest

import gacore.scheduler as scheduler_module
from gacore.scheduler import on_scheduler_done


class _FakeTask:
    def __init__(self, *, cancelled: bool = False, exc: BaseException | None = None) -> None:
        self._cancelled = cancelled
        self._exc = exc

    def cancelled(self) -> bool:
        return self._cancelled

    def exception(self) -> BaseException | None:
        return self._exc


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []

    class _Recorder:
        def debug(self, message: str, **fields: object) -> None:
            captured.append((message, fields))

        def info(self, message: str, **fields: object) -> None:
            captured.append((message, fields))

        def warning(self, message: str, **fields: object) -> None:
            captured.append((message, fields))

        def error(self, message: str, **fields: object) -> None:
            captured.append((message, fields))

        def exception(self, message: str, *, exc: BaseException | None = None, **fields: object) -> None:
            captured.append((message, fields | {"exc": exc}))

    monkeypatch.setattr(scheduler_module, "logger", _Recorder())
    return captured


def test_logs_died_with_traceback(events) -> None:
    on_scheduler_done(_FakeTask(exc=RuntimeError("boom")), threading.Event())
    message, fields = events[-1]
    assert message == "Scheduler task died"
    assert isinstance(fields["exc"], RuntimeError)
    assert fields["exc"].__class__ is RuntimeError


def test_logs_stopped_when_stop_event_set(events) -> None:
    stop_event = threading.Event()
    stop_event.set()
    on_scheduler_done(_FakeTask(), stop_event)
    assert events[-1][0] == "Scheduler task stopped"


def test_logs_unexpected_when_no_exception_and_no_stop(events) -> None:
    on_scheduler_done(_FakeTask(), threading.Event())
    assert events[-1][0] == "Scheduler task exited unexpectedly"


def test_logs_cancelled_first(events) -> None:
    stop_event = threading.Event()
    stop_event.set()
    on_scheduler_done(_FakeTask(cancelled=True, exc=RuntimeError("ignored")), stop_event)
    assert events[-1][0] == "Scheduler task cancelled"
