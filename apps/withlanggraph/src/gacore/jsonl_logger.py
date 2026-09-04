"""JSONL structured logging for gacore per the project logging spec (AGENTS.md).

One JSON object per line in logs/<YYYY-MM-DD>/app.jsonl. All runs on the same day share
one log directory and append to the same file; every Logger instance returned by
get_logger shares it.

Enhancements over the baseline spec:
- Timestamps in Asia/Shanghai (UTC+8) with offset suffix, e.g. 2026-08-02T21:34:08.123+08:00.
- A short session ID is generated per process and attached to every line, so
  multiple runs sharing one daily file can be distinguished.
- PID is attached to every line for the same reason.
- DEBUG / ERROR lines include the caller function name for faster triage.
- Values of well-known secret keys (api_key, password, token, secret, ...) are
  masked before write, per the AGENTS.md data-masking rule.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Final, final

from gacore.config import Config, ConfigError

_LOG_FILENAME: Final = "app.jsonl"
_LOG_DIR_FORMAT: Final = "%Y-%m-%d"
_LOG_TIMEZONE: Final = "Asia/Shanghai"
_LOG_UTC_OFFSET: Final = timedelta(hours=8)

# Keys whose values are replaced with "***" before write (case-insensitive match).
_SECRET_KEYS: Final = frozenset({
    "api_key",
    "apikey",
    "password",
    "passwd",
    "token",
    "secret",
    "access_token",
    "refresh_token",
    "authorization",
    "email",
    "phone",
    "mobile",
})

# Per-process session id: generated once, attached to every line.
_SESSION_ID: Final = uuid.uuid4().hex[:8]
_PID: Final = os.getpid()


def _mask_value(key: str, value: object) -> object:
    """Mask sensitive values; pass everything else through unchanged."""
    if key.lower() in _SECRET_KEYS and isinstance(value, str):
        return "***"
    return value


def _mask_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """Return a copy of fields with any secret values masked."""
    return {k: _mask_value(k, v) for k, v in fields.items()}


class _JsonlFormatter(logging.Formatter):
    """Serialize a LogRecord as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        # Locate the real caller: logging infrastructure adds 2 frames.
        # Fall back to record.funcName if findCaller is unavailable.
        caller = record.funcName
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.__dict__.get("gacore_module", record.module),
            "message": record.getMessage(),
            "session": _SESSION_ID,
            "pid": _PID,
        }
        if record.levelno >= logging.DEBUG:
            payload["caller"] = caller
        payload.update(_mask_fields(record.__dict__.get("gacore_fields", {})))
        return json.dumps(payload, ensure_ascii=False, default=str)


class _ConsoleFormatter(logging.Formatter):
    """Human-readable one-liner for terminal output; the file sink stays JSONL."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone(_LOG_UTC_OFFSET)).strftime("%H:%M:%S")
        module = record.__dict__.get("gacore_module", record.module)
        fields = _mask_fields(record.__dict__.get("gacore_fields", {}))
        suffix = f" {fields}" if fields else ""
        return f"[{ts}] [{record.levelname:<7}] [{module}] {record.getMessage()}{suffix}"


class _ConsoleSink(logging.StreamHandler):
    """Console handler that resolves sys.stdout at emit time.

    logging.StreamHandler binds its stream once at construction; pytest's capsys swaps
    sys.stdout per test, so a fixed reference would bypass capture and leak test output
    to the real terminal. Resolving on every emit keeps console logging observable under
    capsys while behaving identically in production.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            sys.stdout.write(message + self.terminator)
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


@final
class Logger:
    """Module-scoped structured logger; all instances share the process's log file."""

    __slots__ = ("_logger", "_module")

    def __init__(self, module: str) -> None:
        self._module = module
        self._logger = logging.getLogger(f"gacore.{module}")
        if not self._logger.handlers:
            self._logger.addHandler(_get_sink())
            self._logger.addHandler(_get_console_sink())
            self._logger.setLevel(logging.DEBUG)
            self._logger.propagate = False

    def debug(self, message: str, **fields: object) -> None:
        """Emit a DEBUG line; extra kwargs become JSON fields."""
        self._emit(logging.DEBUG, message, fields)

    def info(self, message: str, **fields: object) -> None:
        """Emit an INFO line; extra kwargs become JSON fields."""
        self._emit(logging.INFO, message, fields)

    def warning(self, message: str, **fields: object) -> None:
        """Emit an WARNING line; extra kwargs become JSON fields."""
        self._emit(logging.WARNING, message, fields)

    def error(
        self,
        message: str,
        *,
        error_type: str | None = None,
        stack_trace: str | None = None,
        context: Mapping[str, object] | None = None,
        **fields: object,
    ) -> None:
        """Emit an ERROR line carrying error_type, stack_trace and context alongside any fields."""
        self._emit(
            logging.ERROR,
            message,
            {"error_type": error_type, "stack_trace": stack_trace, "context": context, **fields},
        )

    def _emit(self, level: int, message: str, fields: Mapping[str, object]) -> None:
        self._logger.log(level, message, extra={"gacore_module": self._module, "gacore_fields": dict(fields)})


_sink: logging.Handler | None = None
_console_sink: logging.Handler | None = None


def _build_sink() -> logging.Handler:
    """Create the process-wide file handler; degrades to a no-op sink if setup fails."""
    try:
        config = Config.default()
        log_dir = config.logs_dir / time.strftime(_LOG_DIR_FORMAT)
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / _LOG_FILENAME, mode="a", encoding="utf-8")
        handler.setFormatter(_JsonlFormatter())
        return handler
    except (OSError, ConfigError):
        handler = logging.NullHandler()
        handler.setFormatter(_JsonlFormatter())
        return handler


def _get_sink() -> logging.Handler:
    """Return the process-wide sink, building it once on first use."""
    global _sink
    if _sink is None:
        _sink = _build_sink()
    return _sink


def _get_console_sink() -> logging.Handler:
    """Return the process-wide console (stdout) handler, building it once on first use."""
    global _console_sink
    if _console_sink is None:
        sink = _ConsoleSink()
        sink.setFormatter(_ConsoleFormatter())
        _console_sink = sink
    return _console_sink


def get_logger(module: str) -> Logger:
    """Return a JSONL logger scoped to a module, sharing the process's log file."""
    return Logger(module)
