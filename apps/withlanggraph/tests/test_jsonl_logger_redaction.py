"""JSONL formatter redaction: stack traces / messages must not carry credentials."""

from __future__ import annotations

import json
import logging

from gacore.jsonl_logger import _JsonlFormatter


def _record(**fields: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "gacore.test", logging.ERROR, __file__, 1, "etl failed key=abcdefgh12", (), None
    )
    record.gacore_module = "langTrack.etl"
    record.gacore_fields = fields
    return record


def test_stack_trace_and_message_are_redacted() -> None:
    payload = json.loads(
        _JsonlFormatter().format(
            _record(
                error_type="RuntimeError",
                error_message="bad url ?key=0123456789abcdef",
                stack_trace="Traceback ...\nRuntimeError: bad url ?key=0123456789abcdef",
            )
        )
    )
    assert "0123456789abcdef" not in payload["error_message"]
    assert "0123456789abcdef" not in payload["stack_trace"]
    assert "abcdefgh12" not in payload["message"]


def test_nested_context_strings_are_redacted() -> None:
    payload = json.loads(
        _JsonlFormatter().format(
            _record(context={"url": "https://api/x?token=tokentoken123", "stage": "geocode"})
        )
    )
    assert payload["context"]["url"] == "https://api/x?token=***"
    assert payload["context"]["stage"] == "geocode"


def test_email_recipients_are_masked_but_openids_kept() -> None:
    payload = json.loads(
        _JsonlFormatter().format(
            _record(to="boss@example.com", to_openid="openid_A", recipient="x@y.com")
        )
    )
    assert payload["to"] == "***"
    assert payload["recipient"] == "***"
    assert payload["to_openid"] == "openid_A"
