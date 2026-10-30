"""P3 timing & adaptation: quiet hours, adaptive quota, source mute, "别问了".

Covers the design doc (proactive-inquiry-2.0 §5/§7) pieces that do not add a table or
a new send channel: the 24-dim activity histogram, the 1–4/day reply-rate quota, the
3h minimum gap, quiet hours, consecutive-ignore source mute (14 days), and the global
7-day mute triggered by an explicit "别问了 / 不想回答".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gacore import ops, proactive, qa_store
from gacore.config import Config
from gacore.scheduler import Job

_TZ = timezone(timedelta(hours=8))


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_TZ)


def _write_guard(cfg: Config, guard: dict) -> None:
    path = cfg.root / "config" / "proactive.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"guard": guard}), encoding="utf-8")


def _questions_job() -> Job:
    return Job(name="daily-questions", schedule="08:30", prompt="", scene="questions")


# --------------------------------------------------------------------------- quota


class TestQuota:
    def test_cap_tiers(self) -> None:
        assert proactive.quota_cap(None) is None
        assert proactive.quota_cap(0.0) == 1
        assert proactive.quota_cap(0.149) == 1
        assert proactive.quota_cap(0.15) == 2
        assert proactive.quota_cap(0.399) == 2
        assert proactive.quota_cap(0.40) == 3
        assert proactive.quota_cap(0.599) == 3
        assert proactive.quota_cap(0.60) == 4
        assert proactive.quota_cap(0.95) == 4
        assert proactive.quota_cap("bad") is None

    def test_questions_per_push(self) -> None:
        assert proactive.questions_per_push(None) == 2
        assert proactive.questions_per_push(0.05) == 1
        assert proactive.questions_per_push(0.20) == 2
        assert proactive.questions_per_push(0.55) == 2
        assert proactive.questions_per_push(0.80) == 3

    def test_adaptive_quota_low_rate_caps_at_one(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"adaptive_quota": True, "max_per_day": 2})
        state = {
            "u_u1": {
                "last_sent_date": "2026-10-27",
                "daily_count": 1,
                "last_active": "2026-10-27T01:00:00+08:00",
            }
        }
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), state, _dt(2026, 10, 27, 8, 30),
            guard=proactive.load_guard_config(cfg), reply_rate=0.1,
        )
        assert (allowed, reason) == (False, "daily_cap")

    def test_adaptive_quota_high_rate_allows_four(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"adaptive_quota": True, "max_per_day": 2})
        state = {
            "u_u1": {
                "last_sent_date": "2026-10-27",
                "daily_count": 1,
                "last_active": "2026-10-27T01:00:00+08:00",
            }
        }
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), state, _dt(2026, 10, 27, 8, 30),
            guard=proactive.load_guard_config(cfg), reply_rate=0.8,
        )
        assert (allowed, reason) == (True, "")

    def test_no_data_falls_back_to_static_cap(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"adaptive_quota": True, "max_per_day": 2})
        state = {
            "u_u1": {
                "last_sent_date": "2026-10-27",
                "daily_count": 1,
                "last_active": "2026-10-27T01:00:00+08:00",
            }
        }
        allowed, _ = proactive.job_guard_allows(
            "u1", _questions_job(), state, _dt(2026, 10, 27, 8, 30),
            guard=proactive.load_guard_config(cfg), reply_rate=None,
        )
        assert allowed is True

    def test_adaptive_quota_off_keeps_static_cap(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"max_per_day": 2})
        state = {
            "u_u1": {
                "last_sent_date": "2026-10-27",
                "daily_count": 2,
                "last_active": "2026-10-27T01:00:00+08:00",
            }
        }
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), state, _dt(2026, 10, 27, 8, 30),
            guard=proactive.load_guard_config(cfg), reply_rate=0.9,
        )
        assert (allowed, reason) == (False, "daily_cap")


# --------------------------------------------------------------------------- gates


class TestTimingGates:
    def test_quiet_hours_block_late_night(self) -> None:
        guard = {"quiet_hours": "23:30-07:30"}
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), {}, _dt(2026, 10, 27, 23, 45), guard=guard
        )
        assert (allowed, reason) == (False, "quiet_hours")

    def test_quiet_hours_allow_morning(self) -> None:
        guard = {"quiet_hours": "23:30-07:30"}
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), {}, _dt(2026, 10, 27, 8, 30), guard=guard
        )
        assert (allowed, reason) == (True, "")

    def test_min_gap_blocks_within_three_hours(self) -> None:
        state = {"u_u1": {"last_sent_time": "2026-10-27T06:30:00+08:00"}}
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), state, _dt(2026, 10, 27, 8, 30)
        )
        assert (allowed, reason) == (False, "min_gap")

    def test_min_gap_passes_after_three_hours(self) -> None:
        state = {"u_u1": {"last_sent_time": "2026-10-27T04:00:00+08:00"}}
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), state, _dt(2026, 10, 27, 8, 30)
        )
        assert (allowed, reason) == (True, "")

    def test_global_mute_blocks(self) -> None:
        state = {"u_u1": {"muted_until": "2026-11-03T08:30:00+08:00"}}
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), state, _dt(2026, 10, 27, 8, 30)
        )
        assert (allowed, reason) == (False, "globally_muted")

    def test_expired_global_mute_passes(self) -> None:
        state = {"u_u1": {"muted_until": "2026-10-26T08:30:00+08:00"}}
        allowed, reason = proactive.job_guard_allows(
            "u1", _questions_job(), state, _dt(2026, 10, 27, 8, 30)
        )
        assert (allowed, reason) == (True, "")


# --------------------------------------------------------------------------- mute command


class TestMuteRequest:
    @pytest.mark.parametrize(
        "text",
        ["别问了", "别问了！", "别问了行吗", "不想回答", "不想回答了", "别再问了", "不要问了"],
    )
    def test_recognised(self, text: str) -> None:
        assert proactive.is_mute_request(text) is True

    @pytest.mark.parametrize(
        "text",
        ["别问我这个代码怎么写", "我在回答上一个问题", "今天不想回答问题", "", "别问了，我在忙项目上线"],
    )
    def test_not_recognised(self, text: str) -> None:
        assert proactive.is_mute_request(text) is False

    def test_mute_global_writes_seven_days(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        until = proactive.mute_global(cfg, "u1", now=_dt(2026, 10, 27, 9, 0))
        entry = proactive.load_state(cfg)["u_u1"]
        assert entry["muted_at"] == "2026-10-27T09:00:00+08:00"
        assert entry["muted_until"] == "2026-11-03T09:00:00+08:00"
        assert until == entry["muted_until"]

    def test_mute_global_ignores_placeholder(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        assert proactive.mute_global(cfg, "unknown") == ""
        assert proactive.load_state(cfg) == {}


# --------------------------------------------------------------------------- activity


class TestActiveHours:
    def test_record_last_active_bumps_histogram(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        proactive.record_last_active("u1", cfg=cfg, now=_dt(2026, 10, 27, 9, 15))
        entry = proactive.load_state(cfg)["u_u1"]
        hours = entry["active_hours"]
        assert len(hours) == 24
        assert hours[9] == 1.0
        assert sum(hours) == 1.0

    def test_histogram_decays_with_half_life(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        proactive.record_last_active("u1", cfg=cfg, now=_dt(2026, 10, 1, 9, 0))
        proactive.record_last_active("u1", cfg=cfg, now=_dt(2026, 10, 31, 10, 0))
        hours = proactive.load_state(cfg)["u_u1"]["active_hours"]
        assert hours[9] == pytest.approx(0.5, abs=1e-3)
        assert hours[10] == pytest.approx(1.0, abs=1e-3)

    def test_top_active_hours_cold_start_returns_empty(self) -> None:
        entry = {"active_hours": [0.0] * 24, "active_hours_updated": "2026-10-27T08:00:00+08:00"}
        entry["active_hours"][9] = 3.0
        assert proactive.top_active_hours(entry, now=_dt(2026, 10, 27, 9)) == []

    def test_top_active_hours_picks_two(self) -> None:
        hours = [0.0] * 24
        hours[9] = 1.0
        hours[21] = 6.0
        hours[22] = 5.0
        entry = {"active_hours": hours, "active_hours_updated": "2026-10-27T08:00:00+08:00"}
        assert proactive.top_active_hours(entry, now=_dt(2026, 10, 27, 9)) == [21, 22]

    def test_active_slot_gate_is_opt_in(self) -> None:
        hours = [0.0] * 24
        hours[21] = 12.0
        entry = {"active_hours": hours, "active_hours_updated": "2026-10-27T08:00:00+08:00"}
        # Default off: midday passes regardless of the histogram.
        assert proactive.active_slot_allows(entry, _dt(2026, 10, 27, 12)) == (True, "")

    def test_active_slot_blocks_outside_busy_hour(self) -> None:
        hours = [0.0] * 24
        hours[21] = 12.0
        entry = {"active_hours": hours, "active_hours_updated": "2026-10-27T08:00:00+08:00"}
        guard = {"active_slot_enabled": True}
        assert proactive.active_slot_allows(entry, _dt(2026, 10, 27, 21, 20), guard) == (True, "")
        assert proactive.active_slot_allows(entry, _dt(2026, 10, 27, 12, 0), guard) == (
            False,
            "outside_active_slot",
        )

    def test_active_slot_cross_midnight(self) -> None:
        hours = [0.0] * 24
        hours[23] = 12.0
        entry = {"active_hours": hours, "active_hours_updated": "2026-10-27T08:00:00+08:00"}
        guard = {"active_slot_enabled": True}
        assert proactive.active_slot_allows(entry, _dt(2026, 10, 28, 0, 10), guard) == (True, "")

    def test_active_slot_cold_start_passes(self) -> None:
        entry = {"active_hours": [0.0] * 24, "active_hours_updated": "2026-10-27T08:00:00+08:00"}
        guard = {"active_slot_enabled": True}
        assert proactive.active_slot_allows(entry, _dt(2026, 10, 27, 12), guard) == (True, "")


# --------------------------------------------------------------------------- source mute


_DISTINCT_QUESTIONS = (
    "工学椅后来下单了吗",
    "周末去爬山还是在家休息",
    "最近读的那本书值得推荐吗",
    "晚饭一般几点吃",
    "显示器支架用着顺手吗",
    "早上几点出门通勤",
    "那个课程学到第几章了",
    "新键盘的手感还习惯吗",
    "阳台的植物长得怎么样",
    "睡前还听播客吗",
)


def _make_ignored_source(cfg: Config, *, source: str, count: int, day: str, base: datetime) -> list[str]:
    """Seed ``count`` candidate questions from ``source`` and mark them all ignored.

    Questions are intentionally unrelated sentences: ``seed_candidates`` drops
    same-day rows whose 2-gram Jaccard similarity is too high, so texts that only
    differ by one character would collapse into a single row and the consecutive
    ignore chain would never reach the mute threshold.
    """
    qa_store.seed_candidates(
        cfg,
        [
            {
                "question": f"{_DISTINCT_QUESTIONS[i]}（依据：{day}）",
                "source": source,
                "topic": f"{source}-t{i}",
                "ref": f"{source}:{i}",
                "evidence": [day],
                "freshness": day,
                "gain": 0.6,
            }
            for i in range(count)
        ],
        day=day,
    )
    keys = [
        r["question_key"]
        for r in qa_store.open_questions(cfg)
        if str(r.get("source") or "qa_open") == source
    ]
    qa_store.mark_pushed(cfg, keys, now=base)
    qa_store.mark_ignored(cfg, now=base + timedelta(hours=25))
    return keys


class TestSourceMute:
    def test_two_consecutive_ignores_mute_source(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        base = _dt(2026, 10, 20, 8, 0)
        _make_ignored_source(cfg, source="kg_todo", count=2, day="2026-10-20", base=base)
        muted = qa_store.muted_sources(cfg, now=base + timedelta(hours=30))
        assert "kg_todo" in muted
        until = datetime.strptime(muted["kg_todo"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ)
        assert until - base >= timedelta(days=14)

    def test_single_ignore_does_not_mute(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        base = _dt(2026, 10, 20, 8, 0)
        _make_ignored_source(cfg, source="kg_todo", count=1, day="2026-10-20", base=base)
        assert qa_store.muted_sources(cfg, now=base + timedelta(hours=30)) == {}

    def test_answer_breaks_consecutive_chain(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        base = _dt(2026, 10, 20, 8, 0)
        keys = _make_ignored_source(
            cfg, source="kg_todo", count=2, day="2026-10-20", base=base
        )
        # Answer the newest row: the consecutive-ignore chain is broken.
        rows = [r for r in qa_store.open_questions(cfg) if r["question_key"] in keys]
        newest = max(rows, key=lambda r: str(r.get("delivered_at") or ""))
        qa_store.record_answer(
            cfg, newest["question_key"], "答了", method="rule", now=base + timedelta(hours=26)
        )
        assert qa_store.muted_sources(cfg, now=base + timedelta(hours=30)) == {}

    def test_mute_expires_after_fourteen_days(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        base = _dt(2026, 10, 20, 8, 0)
        _make_ignored_source(cfg, source="kg_todo", count=2, day="2026-10-20", base=base)
        # mark_ignored stamps updated_at with the real clock, so expiry is measured
        # from "now + 15 days" rather than from the simulated delivery date.
        assert qa_store.muted_sources(cfg, now=datetime.now(_TZ) + timedelta(days=15)) == {}


class TestSourceMuteWiring:
    def test_questions_scene_skips_muted_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = Config.for_tests(tmp_path)
        base = _dt(2026, 10, 20, 8, 0)
        _make_ignored_source(cfg, source="qa_open", count=2, day="2026-10-20", base=base)
        monkeypatch.setattr(proactive, "_target_users", lambda job: ["u1"])

        def boom(*_a, **_k):
            raise AssertionError("muted source must be skipped before any push")

        monkeypatch.setattr(proactive, "_headless_run", boom)

        result = proactive.run_proactive_job(
            _questions_job(), cfg, clock=lambda: base + timedelta(hours=30)
        )
        assert result["sent"] == 0
        assert result["skipped"] == [{"user": "", "reason": "source_muted"}]
        # 静音来源的未答问题同步归档，静音到期后不会把两周前的旧问题再翻出来。
        assert qa_store.open_questions(cfg) == []

    def test_adaptive_push_cap_limits_questions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = Config.for_tests(tmp_path)
        _write_guard(cfg, {"adaptive_quota": True})
        # One asked+ignored question gives a 0% 7-day answer rate -> 1 question/push.
        base = _dt(2026, 10, 20, 8, 0)
        _make_ignored_source(cfg, source="qa_open", count=1, day="2026-10-20", base=base)
        qa_store.seed_questions(
            cfg,
            "2026-10-21",
            [f"- [想问你-{i}] 新问题{name}（依据：10-21）" for i, name in enumerate("甲乙丙", 1)],
        )
        monkeypatch.setattr(proactive, "_target_users", lambda job: ["u1"])
        captured: dict = {}

        def fake_headless(job, cfg_, prompt, max_turns, active_card=None):
            captured["prompt"] = prompt
            return "END", "转述完成", [json.dumps({"status": "sent", "to": ["u1"]})]

        monkeypatch.setattr(proactive, "_headless_run", fake_headless)
        monkeypatch.setattr(proactive, "_write_back_proactive", lambda *a, **k: False)
        result = proactive.run_proactive_job(
            _questions_job(), cfg, clock=lambda: _dt(2026, 10, 21, 8, 30)
        )
        assert result["sent"] == 1
        assert "[Q1]" in captured["prompt"]
        assert "[Q2]" not in captured["prompt"]


# --------------------------------------------------------------------------- ops


class TestOpsSummary:
    def test_summary_reports_muted_sources_and_global_mute(self, tmp_path: Path) -> None:
        cfg = Config.for_tests(tmp_path)
        base = _dt(2026, 10, 20, 8, 0)
        _make_ignored_source(cfg, source="kg_todo", count=2, day="2026-10-20", base=base)
        now = base + timedelta(hours=30)
        proactive.mute_global(cfg, "u1", now=now)

        summary = ops._proactive_summary(tmp_path, now)
        assert "kg_todo" in summary["muted_sources"]
        assert summary["global_muted_users"] == 1
