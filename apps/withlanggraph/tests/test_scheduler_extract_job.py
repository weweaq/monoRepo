"""Tests for the scheduler's KG-extraction job path (``type == "extract"``).

Covers the S5 wiring: ``load_jobs`` accepting a prompt-less extract job, ``run_loop``
dispatching it to EXTRACT_POOL (never the blocking run_job path), and ``run_extract_job``
running the deterministic extract subsystem without any agent graph, delivery, or daily
note. All external work is stubbed — no real LLM, no real DB, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gacore.config import Config
from gacore.langTrack.extract import RunResult
from gacore.scheduler import (
    Job,
    ScheduleResult,
    _log_extract_failure,
    load_jobs,
    load_state,
    run_extract_job,
    run_loop,
)


def _write_schedule(cfg: Config, jobs: list[dict]) -> None:
    schedule = cfg.asset_dir.parent / "schedule.json"
    schedule.parent.mkdir(parents=True, exist_ok=True)
    schedule.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _fake_result(day: str, *, status: str = "ok", rows: int = 7) -> RunResult:
    return RunResult(
        run_id="run1",
        day=day,
        status=status,
        raw_hash="hash",
        material_chars=1234,
        rows=rows,
        channels={"_material": {"chars": 1234}},
    )


# ---------- load_jobs ----------


class TestLoadJobsExtract:
    """load_jobs accepts type=extract without a prompt (no KeyError skip)."""

    def test_extract_job_loads_without_prompt(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_schedule(cfg, [{"name": "kg-extract", "schedule": "23:30", "type": "extract"}])

        jobs = load_jobs(cfg)

        assert len(jobs) == 1
        assert jobs[0].name == "kg-extract"
        assert jobs[0].type == "extract"
        assert jobs[0].prompt == ""

    def test_plain_job_without_prompt_still_skipped(self, tmp_path: Path) -> None:
        """Regression: the extract carve-out must not loosen the default contract."""
        cfg = Config.for_tests(tmp_path)
        _write_schedule(
            cfg,
            [
                {"name": "good", "schedule": "09:00", "prompt": "ok"},
                {"name": "no_prompt", "schedule": "09:00"},
            ],
        )

        jobs = load_jobs(cfg)

        assert [j.name for j in jobs] == ["good"]


# ---------- run_extract_job ----------


class _RecordingLogger:
    """Minimal structured-logger stand-in: captures each call's level/msg/kwargs."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def _record(self, level: str, msg: str, **kw: object) -> None:
        self.events.append({"level": level, "msg": msg, **kw})

    def info(self, msg: str, **kw: object) -> None:
        self._record("info", msg, **kw)

    def error(self, msg: str, **kw: object) -> None:
        self._record("error", msg, **kw)

    def exception(self, msg: str, *, exc: BaseException | None = None, **kw: object) -> None:
        derived = {} if exc is None else {"error_type": type(exc).__name__, "error_message": str(exc)}
        self._record("error", msg, exc=exc, **derived, **kw)

    def warning(self, msg: str, **kw: object) -> None:
        self._record("warning", msg, **kw)

    def debug(self, msg: str, **kw: object) -> None:
        self._record("debug", msg, **kw)

    def by_msg(self, msg: str) -> list[dict]:
        return [e for e in self.events if e["msg"] == msg]


class TestRunExtractJob:
    """run_extract_job runs the extract subsystem only — no graph, no delivery, no note."""

    def _patch_no_side_effects(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        calls: list[object] = []

        def _recorder(name: str):
            def _fn(*args: object, **kwargs: object) -> None:
                calls.append((name, args))

            return _fn

        for name in ("_build_job_prompt", "_deliver", "_write_daily_note", "_write_output"):
            monkeypatch.setattr(f"gacore.scheduler.{name}", _recorder(name))
        return calls

    def test_uses_today_when_for_day_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = Config.for_tests(tmp_path)
        seen: list[str] = []

        def fake_run_extract(day: str, cfg_: Config) -> RunResult:
            seen.append(day)
            return _fake_result(day)

        monkeypatch.setattr("gacore.scheduler.run_extract", fake_run_extract)
        rec = _RecordingLogger()
        monkeypatch.setattr("gacore.scheduler.logger", rec)
        calls = self._patch_no_side_effects(monkeypatch)
        job = Job(name="kg-extract", schedule="23:30", prompt="", type="extract")

        result = run_extract_job(job, cfg)

        assert seen == [datetime.now(UTC).astimezone().date().isoformat()]
        assert result is not None and result.status == "ok"
        assert calls == []  # no prompt build, no delivery, no output/daily-note writes
        finished = rec.by_msg("kg-extract finished")
        assert len(finished) == 1
        assert finished[0]["day"] == seen[0]
        assert finished[0]["status"] == "ok"
        assert finished[0]["rows"] == 7
        assert finished[0]["material_chars"] == 1234

    def test_for_day_overrides_target_date(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = Config.for_tests(tmp_path)
        seen: list[str] = []
        monkeypatch.setattr(
            "gacore.scheduler.run_extract",
            lambda day, cfg_: seen.append(day) or _fake_result(day),
        )
        calls = self._patch_no_side_effects(monkeypatch)
        job = Job(name="kg-extract", schedule="23:30", prompt="", type="extract")

        run_extract_job(job, cfg, for_day="2026-09-14")

        assert seen == ["2026-09-14"]
        assert calls == []  # the for_day path is side-effect-free too

    def test_failure_is_logged_and_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = Config.for_tests(tmp_path)
        rec = _RecordingLogger()

        def boom(day: str, cfg_: Config) -> RunResult:
            raise RuntimeError("provider down")

        monkeypatch.setattr("gacore.scheduler.run_extract", boom)
        monkeypatch.setattr("gacore.scheduler.logger", rec)
        job = Job(name="kg-extract", schedule="23:30", prompt="", type="extract")

        result = run_extract_job(job, cfg)  # must not raise

        assert result is None
        assert rec.by_msg("kg-extract finished") == []  # no success log on failure
        errs = rec.by_msg("kg-extract failed")
        assert len(errs) == 1
        assert errs[0]["level"] == "error"
        assert errs[0]["job"] == "kg-extract"
        assert errs[0]["error_type"] == "RuntimeError"


# ---------- run_loop dispatch ----------


class _FakeFuture:
    def __init__(self, store: list["_FakeFuture"]) -> None:
        self.callbacks: list[object] = []
        store.append(self)

    def add_done_callback(self, cb: object) -> None:
        self.callbacks.append(cb)


class _FakePool:
    def __init__(self, submitted: list[tuple[object, tuple]], futures: list[_FakeFuture]) -> None:
        self._submitted = submitted
        self._futures = futures

    def submit(self, fn: object, *args: object) -> _FakeFuture:
        self._submitted.append((fn, args))
        return _FakeFuture(self._futures)


class TestRunLoopExtractDispatch:
    """run_loop routes type=extract to EXTRACT_POOL, not the blocking run_job path."""

    def test_dispatches_to_extract_pool(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_schedule(cfg, [{"name": "kg-extract", "schedule": "00:01", "type": "extract"}])
        submitted: list[tuple[object, tuple]] = []
        futures: list[_FakeFuture] = []

        ran_job: list[object] = []
        monkeypatch.setattr("gacore.scheduler.EXTRACT_POOL", _FakePool(submitted, futures))
        monkeypatch.setattr("gacore.scheduler.run_job", lambda *a, **k: ran_job.append(a))

        jobs_run = run_loop(
            cfg=cfg,
            max_iterations=1,
            clock=lambda: datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC),
        )

        assert jobs_run == 1
        assert len(submitted) == 1
        fn, args = submitted[0]
        assert fn is run_extract_job
        assert isinstance(args[0], Job) and args[0].name == "kg-extract"
        assert ran_job == []  # never falls through to the LLM graph path
        # L1: the dispatched future gets a done-callback so worker exceptions are surfaced.
        assert len(futures) == 1
        assert futures[0].callbacks == [_log_extract_failure]
        # last_run persisted so the job does not refire on the next tick.
        assert load_state(cfg)["kg-extract"].run_count == 1

    def test_mixed_tick_splits_extract_and_plain_jobs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same tick: extract job → EXTRACT_POOL, plain job → run_job (no cross-contamination)."""
        cfg = Config.for_tests(tmp_path)
        _write_schedule(
            cfg,
            [
                {"name": "kg-extract", "schedule": "00:01", "type": "extract"},
                {"name": "plain", "schedule": "00:02", "prompt": "hi"},
            ],
        )
        submitted: list[tuple[object, tuple]] = []
        futures: list[_FakeFuture] = []
        ran_job: list[str] = []

        monkeypatch.setattr("gacore.scheduler.EXTRACT_POOL", _FakePool(submitted, futures))

        def fake_run_job(job: Job, *a: object, **k: object) -> ScheduleResult:
            ran_job.append(job.name)
            return ScheduleResult(
                job_name=job.name,
                exit_reason="done",
                output_path="",
                duration_seconds=0.1,
                error=None,
                reply="",
            )

        monkeypatch.setattr("gacore.scheduler.run_job", fake_run_job)

        jobs_run = run_loop(
            cfg=cfg,
            max_iterations=1,
            clock=lambda: datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC),
        )

        assert jobs_run == 2
        assert [s[1][0].name for s in submitted] == ["kg-extract"]
        assert ran_job == ["plain"]
