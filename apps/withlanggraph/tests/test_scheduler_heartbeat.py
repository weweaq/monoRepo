"""run_loop heartbeat + cooperative-stop tests (design docs/observability-design.md §6)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gacore.config import Config
from gacore.scheduler import run_loop


def _read_hb(root: Path) -> dict:
    return json.loads((root / "data" / "run" / "scheduler.json").read_text(encoding="utf-8"))


def _write_schedule(cfg: Config) -> None:
    schedule = cfg.asset_dir.parent / "schedule.json"
    schedule.parent.mkdir(parents=True, exist_ok=True)
    schedule.write_text(
        json.dumps({"jobs": [{"name": "test", "schedule": "00:01", "prompt": "hi", "max_turns": 3}]}),
        encoding="utf-8",
    )


def test_run_loop_writes_stopped_heartbeat(tmp_path: Path) -> None:
    cfg = Config.for_tests(tmp_path)
    jobs_run = run_loop(cfg=cfg, max_iterations=1)
    assert jobs_run == 0
    data = _read_hb(tmp_path)
    assert data["component"] == "scheduler"
    assert data["state"] == "stopped"
    assert data["detail"]["iterations"] == 1
    assert data["detail"]["jobs_run"] == 0


def test_run_loop_marks_busy_during_blocking_job(tmp_path: Path) -> None:
    cfg = Config.for_tests(tmp_path)
    _write_schedule(cfg)
    seen: dict = {}

    def fake_runner(prompt: str, cfg_: Config, max_turns: int) -> str | None:
        seen.update(_read_hb(tmp_path))
        return "CURRENT_TASK_DONE"

    clock = lambda: datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)  # noqa: E731
    jobs_run = run_loop(cfg=cfg, graph_runner=fake_runner, max_iterations=1, clock=clock)
    assert jobs_run == 1
    assert seen["state"] == "busy"
    assert seen["stale_after_sec"] == 1800
    assert seen["detail"] == {"job": "test", "job_type": "job"}
    assert _read_hb(tmp_path)["state"] == "stopped"


def test_run_loop_crash_sets_error_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config.for_tests(tmp_path)

    def boom(_cfg: Config):
        raise RuntimeError("boom")

    monkeypatch.setattr("gacore.scheduler.load_jobs", boom)
    with pytest.raises(RuntimeError):
        run_loop(cfg=cfg, max_iterations=1)
    data = _read_hb(tmp_path)
    assert data["state"] == "error"
    assert data["detail"]["error_type"] == "RuntimeError"
    assert data["detail"]["error_message"] == "boom"


def test_run_loop_stop_event_exits_and_writes_stopped(tmp_path: Path) -> None:
    cfg = Config.for_tests(tmp_path)
    stop_event = threading.Event()
    stop_event.set()
    jobs_run = run_loop(cfg=cfg, poll_interval=1, stop_event=stop_event)
    assert jobs_run == 0
    assert _read_hb(tmp_path)["state"] == "stopped"
