"""ETL 运行摘要日志 + 表计数辅助（P0 可观测性 Task 5）。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from gacore.langTrack import etl


def _read_logs() -> list[dict]:
    root = Path(os.environ["GACORE_LOG_ROOT"])
    out = []
    for path in root.glob("*/app.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_run_logs_started_and_finished(tmp_path, monkeypatch):
    monkeypatch.setattr(etl, "_run_impl", lambda *a, **k: {"events": 3, "sessions": 1})
    etl.run(db_path=tmp_path / "x.db", run_geocode=False, run_route=False, run_poi=False)
    # 只看本次 db 的行：共享日志目录里别的用例也会写 etl.run.*。
    logs = [
        x
        for x in _read_logs()
        if x.get("module") == "langTrack.etl" and x.get("db_path") == str(tmp_path / "x.db")
    ]
    started = [x for x in logs if x["message"] == "etl.run.started"][-1]
    finished = [x for x in logs if x["message"] == "etl.run.finished"][-1]
    assert started["run_id"] == finished["run_id"]
    assert started["mode"] == "full"
    assert finished["status"] == "ok"
    assert finished["events"] == 3
    assert finished["duration_ms"] >= 0


def test_run_logs_failure_with_traceback(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(etl, "_run_impl", boom)
    with pytest.raises(RuntimeError):
        etl.run(db_path=tmp_path / "x.db")
    failed = [
        x
        for x in _read_logs()
        if x["message"] == "etl.run.failed" and x.get("db_path") == str(tmp_path / "x.db")
    ][-1]
    assert failed["error_type"] == "RuntimeError"
    assert failed["error_message"] == "boom"
    assert "Traceback (most recent call last)" in failed["stack_trace"]


def test_collect_table_counts_tolerates_missing_tables(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("CREATE TABLE events (id INTEGER)")
    conn.executemany("INSERT INTO events VALUES (?)", [(1,), (2,)])
    conn.commit()
    counts = etl._collect_table_counts(conn)
    conn.close()
    assert counts["events"] == 2
    assert counts["sessions"] is None
