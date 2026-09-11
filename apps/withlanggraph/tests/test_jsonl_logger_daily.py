"""Regression: _DailyFileHandler routes records to logs/<emitting-day>/app.jsonl and
rolls across midnight on a single process (never pinned to the start date)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from gacore.jsonl_logger import _DailyFileHandler


def _make_record(day: str, hhmmss: str) -> logging.LogRecord:
    dt = datetime.fromisoformat(f"{day}T{hhmmss}+08:00")
    r = logging.LogRecord("gacore.test", logging.INFO, __file__, 1, f"event@{day}", (), None)
    r.created = dt.timestamp()
    return r


def test_rolls_to_emitting_day(tmp_path: Path) -> None:
    handler = _DailyFileHandler(tmp_path / "logs")
    try:
        for day, hm in [("2026-07-01", "23:59:59"), ("2026-07-02", "00:00:01")]:
            handler.emit(_make_record(day, hm))
    finally:
        handler.close()

    d1 = tmp_path / "logs" / "2026-07-01" / "app.jsonl"
    d2 = tmp_path / "logs" / "2026-07-02" / "app.jsonl"
    assert d1.exists() and d1.read_text(encoding="utf-8").count("\n") == 1
    assert d2.exists() and d2.read_text(encoding="utf-8").count("\n") == 1
    assert "event@2026-07-01" in d1.read_text(encoding="utf-8")
    assert "event@2026-07-02" in d2.read_text(encoding="utf-8")


def test_same_day_append_same_file(tmp_path: Path) -> None:
    handler = _DailyFileHandler(tmp_path / "logs")
    try:
        for i, hm in enumerate(["10:00:00", "12:30:00", "18:00:00"]):
            handler.emit(_make_record("2026-07-01", hm))
    finally:
        handler.close()

    d = tmp_path / "logs" / "2026-07-01" / "app.jsonl"
    assert d.exists()
    assert d.read_text(encoding="utf-8").count("\n") == 3


def test_records_carry_formatted_ts(tmp_path: Path) -> None:
    handler = _DailyFileHandler(tmp_path / "logs")
    try:
        handler.emit(_make_record("2026-07-01", "09:05:07"))
    finally:
        handler.close()

    line = (tmp_path / "logs" / "2026-07-01" / "app.jsonl").read_text(encoding="utf-8")
    assert '"ts": "2026-07-01T09:05:07.000+08:00"' in line