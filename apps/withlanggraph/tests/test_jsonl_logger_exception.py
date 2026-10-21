"""Logger.exception must emit a real traceback (not str(e)) for the P0 observability loop."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import pytest

from gacore.jsonl_logger import Logger, get_logger


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, Mapping[str, object]]]:
    """Capture `_emit` calls as (level, message, fields) without touching any sink."""
    captured: list[tuple[int, str, Mapping[str, object]]] = []

    def _fake_emit(self: Logger, level: int, message: str, fields: Mapping[str, object]) -> None:
        captured.append((level, message, fields))

    monkeypatch.setattr(Logger, "_emit", _fake_emit)
    return captured


def _last(records: list[tuple[int, str, Mapping[str, object]]]) -> Mapping[str, object]:
    return records[-1][2]


def test_exception_inside_except_records_real_traceback(records) -> None:
    log = get_logger("test_exception")
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        log.exception("job failed", exc=exc, job="daily")
    level, message, _ = records[-1]
    assert level == logging.ERROR
    assert message == "job failed"
    fields = _last(records)
    assert fields["error_type"] == "RuntimeError"
    assert fields["error_message"] == "boom"
    assert "Traceback (most recent call last)" in fields["stack_trace"]
    assert "RuntimeError: boom" in fields["stack_trace"]
    assert fields["job"] == "daily"


def test_exception_without_explicit_exc_uses_active_exception(records) -> None:
    log = get_logger("test_exception_active")
    try:
        raise ValueError("active")
    except ValueError:
        log.exception("active path")
    fields = _last(records)
    assert fields["error_type"] == "ValueError"
    assert fields["error_message"] == "active"
    assert "ValueError: active" in fields["stack_trace"]


def test_exception_without_active_exception_still_emits(records) -> None:
    log = get_logger("test_exception_none")
    log.exception("no active exception")
    fields = _last(records)
    assert fields["error_type"] is None
    assert fields["error_message"] is None
    assert fields["stack_trace"] is None


def test_error_accepts_error_message(records) -> None:
    log = get_logger("test_exception_biz")
    log.error("business failure", error_type="INCOMPLETE_REPLY", error_message="no body")
    fields = _last(records)
    assert fields["error_type"] == "INCOMPLETE_REPLY"
    assert fields["error_message"] == "no body"
