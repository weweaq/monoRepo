"""Tests for gacore.proactive: P0 proactive-outreach pipeline.

Covers the bounded pool, window/cooldown due gate, Job Guard (daily cap / hot-chat
cooldown / idle threshold), state persistence (atomic, East-8), load_jobs parsing of
the new proactive fields, run_loop dispatch, and the end-to-end run_proactive_job
flow with an injected fake headless runner.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore import proactive
from gacore.config import Config
from gacore.scheduler import Job, JobState, load_jobs, load_state, run_loop

_TZ = timezone(timedelta(hours=8))


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_TZ)


# ---------- window / parse_window ----------


class TestWindow:
    def test_in_window_inside(self) -> None:
        assert proactive.in_window("07:00-08:00", _dt(2026, 8, 27, 7, 30)) is True

    def test_in_window_outside(self) -> None:
        assert proactive.in_window("07:00-08:00", _dt(2026, 8, 27, 9, 0)) is False

    def test_in_window_closed_interval(self) -> None:
        # Both endpoints inclusive.
        assert proactive.in_window("07:00-08:00", _dt(2026, 8, 27, 7, 0)) is True
        assert proactive.in_window("07:00-08:00", _dt(2026, 8, 27, 8, 0)) is True

    def test_in_window_empty_always_true(self) -> None:
        assert proactive.in_window("", _dt(2026, 8, 27, 23, 0)) is True
        assert proactive.in_window("  ", _dt(2026, 8, 27, 23, 0)) is True

    def test_in_window_cross_midnight(self) -> None:
        assert proactive.in_window("22:00-01:00", _dt(2026, 8, 27, 23, 30)) is True
        assert proactive.in_window("22:00-01:00", _dt(2026, 8, 28, 0, 30)) is True
        assert proactive.in_window("22:00-01:00", _dt(2026, 8, 27, 12, 0)) is False

    def test_parse_window_invalid(self) -> None:
        assert proactive.parse_window("garbage") is None
        assert proactive.parse_window("25:00-08:00") is None
        assert proactive.parse_window("07:00") is None
        assert proactive.parse_window("") is None


# ---------- cooldown / proactive_due ----------


class TestCooldown:
    def test_zero_cooldown_always_ok(self) -> None:
        job = Job(name="p", schedule="every 1h", prompt="x", type="proactive")
        st = JobState(last_run="2026-08-27T07:20:00+08:00")
        assert proactive.cooldown_ok(job, st, _dt(2026, 8, 27, 7, 30)) is True

    def test_never_run_ok(self) -> None:
        job = Job(name="p", schedule="every 1h", prompt="x", type="proactive", cooldown_minutes=30)
        assert proactive.cooldown_ok(job, JobState(), _dt(2026, 8, 27, 7, 30)) is True

    def test_within_cooldown_blocked(self) -> None:
        job = Job(name="p", schedule="every 1h", prompt="x", type="proactive", cooldown_minutes=30)
        st = JobState(last_run="2026-08-27T07:20:00+08:00")  # 10 min ago
        assert proactive.cooldown_ok(job, st, _dt(2026, 8, 27, 7, 30)) is False

    def test_past_cooldown_ok(self) -> None:
        job = Job(name="p", schedule="every 1h", prompt="x", type="proactive", cooldown_minutes=30)
        st = JobState(last_run="2026-08-27T06:50:00+08:00")  # 40 min ago
        assert proactive.cooldown_ok(job, st, _dt(2026, 8, 27, 7, 30)) is True

    def test_proactive_due_gates(self) -> None:
        job = Job(
            name="p",
            schedule="every 1h",
            prompt="x",
            type="proactive",
            window="07:00-08:00",
            cooldown_minutes=30,
        )
        now = _dt(2026, 8, 27, 7, 30)
        assert proactive.proactive_due(job, JobState(), now) is True
        # Outside the window.
        assert proactive.proactive_due(job, JobState(), _dt(2026, 8, 27, 9, 0)) is False
        # In window but still cooling down.
        st = JobState(last_run="2026-08-27T07:20:00+08:00")
        assert proactive.proactive_due(job, st, now) is False


# ---------- Job Guard ----------


class TestJobGuard:
    def _morning_job(self) -> Job:
        return Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")

    def _idle_job(self) -> Job:
        return Job(name="proactive-idle", schedule="every 2h", prompt="失联问候", type="proactive")

    def test_allows_fresh_user(self) -> None:
        allowed, reason = proactive.job_guard_allows("1", self._morning_job(), {}, _dt(2026, 8, 27, 7, 30))
        assert (allowed, reason) == (True, "")

    def test_daily_cap_blocks(self) -> None:
        state = {
            "u_1": {
                "last_sent_date": "2026-08-27",
                "daily_count": 2,
                "last_active": "2026-08-27T01:00:00+08:00",
            }
        }
        allowed, reason = proactive.job_guard_allows("1", self._morning_job(), state, _dt(2026, 8, 27, 7, 30))
        assert (allowed, reason) == (False, "daily_cap")

    def test_daily_cap_resets_next_day(self) -> None:
        state = {
            "u_1": {
                "last_sent_date": "2026-08-26",
                "daily_count": 2,
                "last_active": "2026-08-26T23:00:00+08:00",
            }
        }
        allowed, _ = proactive.job_guard_allows("1", self._morning_job(), state, _dt(2026, 8, 27, 7, 30))
        assert allowed is True

    def test_hot_chat_blocks(self) -> None:
        # Active 10 minutes ago -> still hot, do not interrupt.
        state = {"u_1": {"last_active": "2026-08-27T07:20:00+08:00"}}
        allowed, reason = proactive.job_guard_allows("1", self._morning_job(), state, _dt(2026, 8, 27, 7, 30))
        assert (allowed, reason) == (False, "hot_chat")

    def test_hot_chat_past_passes(self) -> None:
        # Active 40 minutes ago -> not hot any more.
        state = {"u_1": {"last_active": "2026-08-27T06:50:00+08:00"}}
        allowed, _ = proactive.job_guard_allows("1", self._morning_job(), state, _dt(2026, 8, 27, 7, 30))
        assert allowed is True

    def test_idle_not_long_enough_blocks(self) -> None:
        # Last active 1.5h ago, idle threshold is 24h.
        state = {"u_1": {"last_active": "2026-08-27T06:00:00+08:00"}}
        allowed, reason = proactive.job_guard_allows("1", self._idle_job(), state, _dt(2026, 8, 27, 7, 30))
        assert (allowed, reason) == (False, "not_idle")

    def test_idle_after_threshold_allows(self) -> None:
        # Last active > 24h ago.
        state = {"u_1": {"last_active": "2026-08-26T07:00:00+08:00"}}
        allowed, reason = proactive.job_guard_allows("1", self._idle_job(), state, _dt(2026, 8, 27, 7, 30))
        assert (allowed, reason) == (True, "")

    def test_idle_monotonic_no_repeat(self) -> None:
        # Idle sent at 01:00 today, user has not been active since -> do not repeat.
        state = {
            "u_1": {
                "last_active": "2026-08-26T07:00:00+08:00",
                "last_sent": {"idle": "2026-08-27T01:00:00+08:00"},
            }
        }
        allowed, reason = proactive.job_guard_allows("1", self._idle_job(), state, _dt(2026, 8, 27, 7, 30))
        assert (allowed, reason) == (False, "idle_already_sent")

    def test_idle_monotonic_resets_after_user_active(self) -> None:
        # User became active again after the idle send -> a new idle cycle may start.
        # last_active sits after the previous idle send AND more than 24h before now,
        # so neither the monotonic rule nor the idle threshold blocks.
        state = {
            "u_1": {
                "last_active": "2026-08-26T05:00:00+08:00",
                "last_sent": {"idle": "2026-08-26T01:00:00+08:00"},
            }
        }
        allowed, reason = proactive.job_guard_allows("1", self._idle_job(), state, _dt(2026, 8, 27, 7, 30))
        assert (allowed, reason) == (True, "")

    def test_idle_no_last_active_allows(self) -> None:
        # Known user with no state yet -> treat as idle long enough, allow.
        allowed, reason = proactive.job_guard_allows("1", self._idle_job(), {}, _dt(2026, 8, 27, 7, 30))
        assert (allowed, reason) == (True, "")


# ---------- state persistence ----------


class TestState:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        proactive.save_state(cfg, {"u_1": {"last_active": "2026-08-27T07:00:00+08:00"}})
        state = proactive.load_state(cfg)
        assert state["u_1"]["last_active"] == "2026-08-27T07:00:00+08:00"

    def test_save_is_atomic_no_tmp_leftover(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        proactive.save_state(cfg, {"a": 1})
        target = cfg.root / "data" / "proactive_state.json"
        assert target.is_file()
        assert not target.with_suffix(".json.tmp").exists()

    def test_record_last_active_creates_and_updates(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        proactive.record_last_active("u1", cfg=cfg, now=_dt(2026, 8, 27, 7, 0))
        state = proactive.load_state(cfg)
        entry = state["u_u1"]
        assert entry["last_active"] == "2026-08-27T07:00:00+08:00"
        assert entry["created_at"] == "2026-08-27T07:00:00+08:00"
        assert entry["updated_at"] == "2026-08-27T07:00:00+08:00"
        # Second contact updates last_active / updated_at, keeps created_at.
        proactive.record_last_active("u1", cfg=cfg, now=_dt(2026, 8, 27, 8, 0))
        entry = proactive.load_state(cfg)["u_u1"]
        assert entry["last_active"] == "2026-08-27T08:00:00+08:00"
        assert entry["created_at"] == "2026-08-27T07:00:00+08:00"
        assert entry["updated_at"] == "2026-08-27T08:00:00+08:00"

    def test_record_last_active_ignores_placeholder_ids(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        proactive.record_last_active("unknown", cfg=cfg)
        proactive.record_last_active("", cfg=cfg)
        proactive.record_last_active("None", cfg=cfg)
        assert proactive.load_state(cfg) == {}


# ---------- bounded pool ----------


class TestBoundedExecutor:
    def test_rejects_when_queue_full(self) -> None:
        started = threading.Event()
        release = threading.Event()
        pool = proactive._BoundedExecutor(max_workers=1, queue_maxsize=2, thread_name_prefix="test-pro")

        def block() -> None:
            started.set()
            release.wait()

        try:
            first = pool.submit(block)
            assert first is not None
            # Deterministic: wait until the single worker actually picked up the task.
            assert started.wait(timeout=5), "worker did not pick up the blocking task"
            assert pool.submit(block) is not None   # queued (pending = 2)
            assert pool.submit(block) is None       # full: 1 running + 1 queued = maxsize 2
        finally:
            release.set()
            pool.shutdown(wait=True)


# ---------- load_jobs: new proactive fields ----------


class TestLoadJobs:
    def test_parses_proactive_fields_and_defaults(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "p-morning",
                            "schedule": "07:29",
                            "prompt": "早安问候",
                            "type": "proactive",
                            "window": "07:00-08:00",
                            "cooldown_minutes": "60",
                        },
                        {"name": "daily", "schedule": "09:00", "prompt": "日报"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        jobs = {j.name: j for j in load_jobs(cfg)}
        p = jobs["p-morning"]
        assert p.type == "proactive"
        assert p.window == "07:00-08:00"
        assert p.cooldown_minutes == 60  # string coerced to int
        d = jobs["daily"]
        assert d.type == "job"
        assert d.window == ""
        assert d.cooldown_minutes == 0

    def test_proactive_job_without_prompt_is_kept_with_empty_prompt(self, tmp_path: Path) -> None:
        # M2: proactive jobs are scene-driven — a missing prompt is allowed and falls
        # back to the scene-derived default, so the job must NOT be dropped.
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({"jobs": [{"name": "p-idle", "schedule": "every 2h", "type": "proactive"}]}),
            encoding="utf-8",
        )
        jobs = load_jobs(cfg)
        assert len(jobs) == 1
        assert jobs[0].type == "proactive"
        assert jobs[0].prompt == ""

    def test_parses_explicit_scene_field(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps({"jobs": [{"name": "p-custom", "schedule": "every 2h", "type": "proactive", "scene": "wind-down"}]}),
            encoding="utf-8",
        )
        jobs = load_jobs(cfg)
        assert len(jobs) == 1
        assert jobs[0].scene == "wind-down"


# ---------- run_loop dispatch ----------


class TestRunLoopDispatch:
    def _schedule(self, cfg: Config) -> None:
        schedule = cfg.asset_dir.parent / "schedule.json"
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "p-morning",
                            "schedule": "07:29",
                            "prompt": "早安",
                            "type": "proactive",
                            "window": "07:00-08:00",
                            "cooldown_minutes": 0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_run_loop_dispatches_proactive(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        self._schedule(cfg)
        calls: list[tuple[object, tuple]] = []

        class FakeFuture:
            def __init__(self) -> None:
                self.callbacks: list[object] = []

            def add_done_callback(self, cb: object) -> None:
                self.callbacks.append(cb)

        class FakePool:
            def submit(self, fn: object, *args: object) -> FakeFuture:
                calls.append((fn, args))
                return FakeFuture()

        monkeypatch.setattr("gacore.scheduler.PROACTIVE_POOL", FakePool())
        clock = lambda: _dt(2026, 8, 27, 7, 30)
        jobs_run = run_loop(cfg=cfg, max_iterations=1, clock=clock)
        assert jobs_run == 1
        assert len(calls) == 1
        submitted_job = calls[0][1][0]
        assert isinstance(submitted_job, Job)
        assert submitted_job.name == "p-morning"
        # schedule_state persisted so the job does not refire.
        states = load_state(cfg)
        assert "p-morning" in states
        assert states["p-morning"].run_count == 1

    def test_run_loop_registers_done_callback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # L1: every dispatched proactive future gets a done-callback so top-level
        # worker exceptions are logged instead of silently dropped.
        cfg = Config.for_tests(tmp_path)
        self._schedule(cfg)
        futures: list[FakeFuture] = []

        class FakeFuture:
            def __init__(self) -> None:
                self.callbacks: list[object] = []
                futures.append(self)

            def add_done_callback(self, cb: object) -> None:
                self.callbacks.append(cb)

        class FakePool:
            def submit(self, fn: object, *args: object) -> FakeFuture:
                return FakeFuture()

        monkeypatch.setattr("gacore.scheduler.PROACTIVE_POOL", FakePool())
        run_loop(cfg=cfg, max_iterations=1, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert len(futures) == 1
        assert len(futures[0].callbacks) == 1

    def test_run_loop_skips_outside_window(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        self._schedule(cfg)
        submitted: list[bool] = []

        class FakePool:
            def submit(self, fn: object, *args: object) -> object:
                submitted.append(True)
                return object()

        monkeypatch.setattr("gacore.scheduler.PROACTIVE_POOL", FakePool())
        # 09:00 is outside the 07:00-08:00 window even though the schedule slot passed.
        clock = lambda: _dt(2026, 8, 27, 9, 0)
        jobs_run = run_loop(cfg=cfg, max_iterations=1, clock=clock)
        assert jobs_run == 0
        assert submitted == []
        assert load_state(cfg) == {}

    def test_run_loop_skips_when_pool_full(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        self._schedule(cfg)

        class FakePool:
            def submit(self, fn: object, *args: object) -> None:
                return None  # bounded queue full -> skip this tick

        monkeypatch.setattr("gacore.scheduler.PROACTIVE_POOL", FakePool())
        clock = lambda: _dt(2026, 8, 27, 7, 30)
        jobs_run = run_loop(cfg=cfg, max_iterations=1, clock=clock)
        assert jobs_run == 0
        # No last_run persisted: a later tick may retry.
        assert load_state(cfg) == {}


# ---------- run_proactive_job end-to-end (fake headless) ----------


class TestRunProactiveJob:
    def test_sends_to_all_eligible_users(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}, "u2": {}})
        monkeypatch.setattr(
            proactive,
            "_headless_run",
            lambda job, cfg, prompt, max_turns: (
                "CURRENT_TASK_DONE",
                "韩立的早安",
                ['{"status": "sent", "ok": 1}'],
            ),
        )
        job = Job(
            name="proactive-morning",
            schedule="every 1h",
            prompt="早安问候",
            type="proactive",
            window="07:00-08:00",
            cooldown_minutes=30,
        )
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert result["attempted"] == 2
        assert result["sent"] == 2
        assert result["skipped"] == []
        state = proactive.load_state(cfg)
        assert state["u_u1"]["daily_count"] == 1
        assert state["u_u1"]["last_sent_date"] == "2026-08-27"
        assert state["u_u1"]["last_sent"]["morning"]
        assert state["u_u2"]["daily_count"] == 1

    def test_llm_no_push_skips_without_sent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        monkeypatch.setattr(
            proactive,
            "_headless_run",
            lambda job, cfg, prompt, max_turns: ("CURRENT_TASK_DONE", "", []),
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert result["attempted"] == 1
        assert result["sent"] == 0
        assert result["skipped"] == [{"user": "u1", "reason": "llm_no_push"}]
        assert proactive.load_state(cfg) == {}

    def test_push_failed_marks_failed_state(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        monkeypatch.setattr(
            proactive,
            "_headless_run",
            lambda job, cfg, prompt, max_turns: (
                "CURRENT_TASK_DONE",
                "",
                ['{"error": "no_recipients", "message": "no users"}'],
            ),
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert result["attempted"] == 1
        assert result["sent"] == 0
        assert result["skipped"][0]["reason"] == "push_failed"
        # M3: a failed push is persisted (last_failed timestamp + counter) so the job
        # is not retried endlessly inside the window.
        state = proactive.load_state(cfg)
        entry = state["u_u1"]
        assert entry["failed_count"] == 1
        assert entry["failed_date"] == "2026-08-27"
        assert entry["last_failed"] == "2026-08-27T07:30:00+08:00"

    def test_job_guard_blocks_all_and_no_headless_call(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        # Seed state so the daily cap already blocks this user.
        proactive.save_state(
            cfg,
            {
                "u_u1": {
                    "last_sent_date": "2026-08-27",
                    "daily_count": 2,
                    "last_active": "2026-08-27T01:00:00+08:00",
                }
            },
        )
        called: list[bool] = []

        def fake_headless(*args: object) -> tuple:
            called.append(True)
            return ("CURRENT_TASK_DONE", "", ['{"status": "sent"}'])

        monkeypatch.setattr(proactive, "_headless_run", fake_headless)
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert called == []
        assert result["attempted"] == 0
        assert result["skipped"] == [{"user": "u1", "reason": "daily_cap"}]

    def test_no_known_users(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", dict)
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert result["sent"] == 0
        assert result["skipped"] == [{"user": "", "reason": "no_known_users"}]

    def test_single_user_error_does_not_block_others(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}, "u2": {}})

        calls = {"n": 0}

        def flaky_headless(job: Job, cfg: Config, prompt: str, max_turns: int) -> tuple:
            # Distinguish users by call order (u1 first, u2 second).
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return ("CURRENT_TASK_DONE", "ok", ['{"status": "sent", "ok": 1}'])

        monkeypatch.setattr(proactive, "_headless_run", flaky_headless)
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert result["attempted"] == 2
        assert result["sent"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["user"] == "u1"
        assert result["errors"][0]["error"] == "RuntimeError: boom"
        state = proactive.load_state(cfg)
        assert "u_u1" not in state
        assert state["u_u2"]["daily_count"] == 1


# ---------- prompt assembly ----------


class TestPrompt:
    def test_prompt_requires_qq_push_and_marks_not_fact(self) -> None:
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安问候", type="proactive")
        text = proactive.build_proactive_prompt(job, "u1", _dt(2026, 8, 27, 7, 30))
        assert "qq_push(message=" in text
        assert "当前时间（东八区）：2026-08-27 07:30:00" in text
        assert "主动问候，不计入用户事实" in text
        assert "早安问候" in text

    def test_prompt_embeds_target_openid(self) -> None:
        # M5: the prompt pins the exact target openid so qq_push cannot mis-deliver.
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        text = proactive.build_proactive_prompt(job, "openid_abc123", _dt(2026, 8, 27, 7, 30))
        assert "openid_abc123" in text
        assert "to=<上面的目标 openid>" in text
        assert "不得改动、猜测或留空" in text

    def test_prompt_falls_back_to_scene_when_no_prompt(self) -> None:
        # M2 + L4: with no job.prompt, the explicit scene (or name-sniffed scene) is used.
        job = Job(name="p-custom", schedule="every 2h", prompt="", type="proactive", scene="wind-down")
        text = proactive.build_proactive_prompt(job, "u1", _dt(2026, 8, 27, 22, 0))
        assert "场景：wind-down" in text


# ---------- M1: in_window normalizes to East-8 ----------


class TestWindowEast8:
    _UTC = UTC

    def test_utc_input_inside_east8_window(self) -> None:
        # 07:30 East-8 == 23:30 UTC the previous day: a UTC clock must still match.
        utc_now = datetime(2026, 8, 26, 23, 30, tzinfo=self._UTC)
        assert proactive.in_window("07:00-08:00", utc_now) is True

    def test_utc_input_outside_east8_window(self) -> None:
        # 09:00 East-8 == 01:00 UTC: out of the 07:00-08:00 window.
        utc_now = datetime(2026, 8, 27, 1, 0, tzinfo=self._UTC)
        assert proactive.in_window("07:00-08:00", utc_now) is False

    def test_cross_midnight_window_with_utc_input(self) -> None:
        # 23:30 East-8 == 15:30 UTC: inside the 22:00-01:00 cross-midnight window.
        utc_now = datetime(2026, 8, 27, 15, 30, tzinfo=self._UTC)
        assert proactive.in_window("22:00-01:00", utc_now) is True


# ---------- M4: structured qq_push result判定 ----------


class TestQqPushSent:
    def test_parses_sent_status(self) -> None:
        assert proactive._qq_push_sent(['{"status": "sent", "ok": 1}']) is True

    def test_error_status_is_not_sent(self) -> None:
        assert proactive._qq_push_sent(['{"status": "error", "message": "nope"}']) is False

    def test_ok_flag_without_sent_status_is_not_sent(self) -> None:
        # {"ok": true, ...} alone is NOT a confirmation under M4 — status must be "sent".
        assert proactive._qq_push_sent(['{"ok": true, "sent": {"to": ["x"]}}']) is False

    def test_unparseable_payload_is_not_sent(self) -> None:
        assert proactive._qq_push_sent(["not-json"]) is False

    def test_empty_results_is_not_sent(self) -> None:
        assert proactive._qq_push_sent([]) is False

    def test_latest_result_wins(self) -> None:
        assert proactive._qq_push_sent(['{"status": "error"}', '{"status": "sent"}']) is True
        assert proactive._qq_push_sent(['{"status": "sent"}', '{"status": "error"}']) is False


# ---------- M3: failure retry cap ----------


class TestFailureCap:
    def test_exhausted_after_max_failures(self) -> None:
        entry = {"failed_date": "2026-08-27", "failed_count": 3}
        assert proactive._failure_exhausted(entry, _dt(2026, 8, 27, 7, 30)) is True

    def test_not_exhausted_below_cap(self) -> None:
        entry = {"failed_date": "2026-08-27", "failed_count": 2}
        assert proactive._failure_exhausted(entry, _dt(2026, 8, 27, 7, 30)) is False

    def test_stale_failure_date_resets_cap(self) -> None:
        # Yesterday's failures must not block today.
        entry = {"failed_date": "2026-08-26", "failed_count": 99}
        assert proactive._failure_exhausted(entry, _dt(2026, 8, 27, 7, 30)) is False

    def test_run_proactive_job_skips_user_after_exhausted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        proactive.save_state(
            cfg,
            {"u_u1": {"failed_date": "2026-08-27", "failed_count": 3}},
        )
        called: list[str] = []

        def fake_headless(job: Job, cfg: Config, prompt: str, max_turns: int) -> tuple:
            called.append(prompt)
            return ("CURRENT_TASK_DONE", "ok", ['{"status": "sent"}'])

        monkeypatch.setattr(proactive, "_headless_run", fake_headless)
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert called == []
        assert result["attempted"] == 0
        assert result["skipped"] == [{"user": "u1", "reason": "fail_retries_exhausted"}]

    def test_success_resets_failure_counters(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr(proactive, "load_known_users", lambda: {"u1": {}})
        proactive.save_state(
            cfg,
            {"u_u1": {"failed_date": "2026-08-27", "failed_count": 2, "last_failed": "2026-08-27T07:00:00+08:00"}},
        )
        monkeypatch.setattr(
            proactive,
            "_headless_run",
            lambda job, cfg, prompt, max_turns: ("CURRENT_TASK_DONE", "ok", ['{"status": "sent"}']),
        )
        job = Job(name="proactive-morning", schedule="every 1h", prompt="早安", type="proactive")
        result = proactive.run_proactive_job(job, cfg=cfg, clock=lambda: _dt(2026, 8, 27, 7, 30))
        assert result["sent"] == 1
        entry = proactive.load_state(cfg)["u_u1"]
        assert "failed_count" not in entry
        assert "last_failed" not in entry
        assert entry["daily_count"] == 1
