"""周报数字聚合（weekly_stats）与评分捕获（weekly_eval）的封闭测试。"""

from __future__ import annotations

import json

from gacore.audit import audit_report
from gacore.config import Config
from gacore.weekly_stats import collect_week_numbers, week_of, week_window


def test_week_of_and_window():
    from datetime import date

    assert week_of(date(2026, 10, 25)) == "2026-W43"  # 周日
    assert week_of(date(2026, 10, 19)) == "2026-W43"  # 周一
    assert week_window("2026-W43") == ("2026-10-19", "2026-10-25")


def _seed_stats(cfg, day: str, screen_ms: int, sleep_min: int | None) -> None:
    import sqlite3

    db = cfg.root / "data" / "langTrack.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_stats ("
        "device_id TEXT NOT NULL DEFAULT 'unknown', day TEXT NOT NULL, "
        "total_screen_ms INTEGER NOT NULL DEFAULT 0, app_ranking_json TEXT, "
        "notification_count INTEGER NOT NULL DEFAULT 0, notification_clicked INTEGER NOT NULL DEFAULT 0, "
        "top_notification_apps_json TEXT, screen_on_count INTEGER NOT NULL DEFAULT 0, "
        "screen_off_count INTEGER NOT NULL DEFAULT 0, unlock_count INTEGER NOT NULL DEFAULT 0, "
        "switch_count INTEGER NOT NULL DEFAULT 0, location_count INTEGER NOT NULL DEFAULT 0, "
        "audio_clip_count INTEGER NOT NULL DEFAULT 0, sleep_start_hhmm TEXT, sleep_end_hhmm TEXT, "
        "sleep_duration_min INTEGER, time_app_json TEXT, "
        "updated_at TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO daily_stats (day, total_screen_ms, sleep_duration_min) VALUES (?,?,?)",
        (day, screen_ms, sleep_min),
    )
    conn.commit()
    conn.close()


def _seed_audit_v1(cfg, day: str, score: int) -> None:
    d = cfg.logs_dir / "report_audit"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.v1.json").write_text(
        json.dumps({"date": day, "version": 1, "score": score, "missing": False,
                    "findings": [], "report_text": "- ok"}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_collect_week_numbers_renders_series(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    _seed_stats(cfg, "2026-10-19", 3_600_000, 420)
    _seed_stats(cfg, "2026-10-20", 7_200_000, 450)
    _seed_audit_v1(cfg, "2026-10-19", 95)
    _seed_audit_v1(cfg, "2026-10-20", 75)

    def fake_git_counts(cfg_, start, end):
        return {"2026-10-19": 2, "2026-10-20": 5}

    monkeypatch.setattr("gacore.weekly_stats._git_commit_counts", fake_git_counts)
    text = collect_week_numbers(cfg, "2026-W43")
    assert "2026-W43" in text
    assert "只读" in text  # 铁律声明在数字区头部
    assert "1.0 2.0" in text  # 屏幕小时序列（周均行带 h 后缀）
    assert "420" in text and "450" in text  # 睡眠分钟
    assert "95" in text and "75" in text  # 审计分
    assert "85.0" in text  # 周均 (95+75)/2
    assert "提交" in text


def test_collect_week_numbers_tolerates_missing_data(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    monkeypatch.setattr("gacore.weekly_stats._git_commit_counts", lambda *a, **k: {})
    text = collect_week_numbers(cfg, "2026-W43")
    assert "无数据" in text or "缺" in text  # 缺行诚实标注，不崩


# ------------------------------------------------ P4 Task 2: 周报调度与审计


def test_weekday_schedule_next_due():
    from datetime import datetime

    from gacore.scheduler import next_run_time

    # 周一 09:00 距周日 20:30 还有 6 天
    got = next_run_time("Sun 20:30", None, datetime(2026, 10, 19, 9, 0))
    assert got == datetime(2026, 10, 25, 20, 30)
    # 周日 19:00 → 今天 20:30
    got = next_run_time("周日 20:30", None, datetime(2026, 10, 25, 19, 0))
    assert got == datetime(2026, 10, 25, 20, 30)
    # 周日 21:00（槽位已过）→ 下周
    got = next_run_time("Sun 20:30", None, datetime(2026, 10, 25, 21, 0))
    assert got == datetime(2026, 11, 1, 20, 30)
    # 本周已跑过 → 下周
    got = next_run_time("Sun 20:30", "2026-10-25T20:30:05", datetime(2026, 10, 25, 21, 0))
    assert got == datetime(2026, 11, 1, 20, 30)
    # 既有形态不受影响
    assert next_run_time("23:30", None, datetime(2026, 10, 25, 9, 0)) == datetime(2026, 10, 25, 23, 30)


def test_audit_report_reads_weekly_path(tmp_path):
    cfg = Config.for_tests(tmp_path)
    (cfg.memory_dir / "daily").mkdir(parents=True)
    weekly = cfg.logs_dir / "delivered_weekly"
    weekly.mkdir(parents=True)
    (weekly / "2026-W43.md").write_text(
        "# 关键变化\n- [关键变化-1] 硬件线半年沉寂后重新点火\n", encoding="utf-8"
    )
    # 周报审计的 last_seen 锚点 = 周日（2026-10-25），早于它的记忆可作基线
    (cfg.memory_dir / "daily" / "2026-10-20.md").write_text("硬件", encoding="utf-8")
    got = audit_report(cfg, "2026-W43")
    assert got.missing is False
    assert got.findings[0].rule == "R2"
    assert got.findings[0].actual_last_seen == "2026-10-20"


def test_audit_report_weekly_missing(tmp_path):
    cfg = Config.for_tests(tmp_path)
    got = audit_report(cfg, "2026-W43")
    assert got.missing is True


# ---------------------------------------------- P4 Task 3: weekly_eval 捕获


def _set_pending_with_one_candidate(tmp_path):
    from gacore.qa_store import record_answer, seed_questions
    from gacore.weekly_eval import set_pending_weekly

    cfg = Config.for_tests(tmp_path)
    seed_questions(cfg, "2026-10-20", ["- [想问你-1] 工学椅还买吗？（依据：长期画像）"])
    record_answer(cfg, _open_key(cfg), "先不买了")
    pending = set_pending_weekly(cfg, "2026-W43")
    assert len(pending["candidates"]) == 1
    return cfg


def _open_key(cfg):
    from gacore.qa_store import open_questions

    return open_questions(cfg)[0]["question_key"]


def test_rating_recorded_with_rubric_version(tmp_path):
    from gacore.weekly_eval import try_weekly_interaction

    cfg = _set_pending_with_one_candidate(tmp_path)
    reply = try_weekly_interaction(cfg, "4 3")
    assert reply is not None and "4" in reply
    data = json.loads((cfg.root / "data" / "eval" / "human" / "2026-W43.json").read_text(encoding="utf-8"))
    assert data["alive"] == 4
    assert data["useful"] == 3
    assert data["rubric_version"] == 1
    # 已评分后重复评分不再吞
    assert try_weekly_interaction(cfg, "5 5") is None


def test_rating_with_least_and_free_text(tmp_path):
    from gacore.weekly_eval import try_weekly_interaction

    cfg = _set_pending_with_one_candidate(tmp_path)
    assert try_weekly_interaction(cfg, "4 3 2 数字准但人味不足") is not None
    data = json.loads((cfg.root / "data" / "eval" / "human" / "2026-W43.json").read_text(encoding="utf-8"))
    assert data["least_like_me"] == 2
    assert "数字准" in data["raw"]


def test_candidate_confirm_writes_facts(tmp_path):
    from gacore.weekly_eval import try_weekly_interaction

    cfg = _set_pending_with_one_candidate(tmp_path)
    reply = try_weekly_interaction(cfg, "1")
    assert reply is not None and "确认" in reply
    cands = (cfg.root / "data" / "qa" / "candidates.jsonl").read_text(encoding="utf-8")
    assert '"confirmed"' in cands
    facts = (cfg.memory_dir / "global_mem_facts.txt").read_text(encoding="utf-8")
    assert "周报候选确认" in facts and "先不买了" in facts


def test_candidate_reject_and_zero(tmp_path):
    from gacore.weekly_eval import set_pending_weekly, try_weekly_interaction

    cfg = _set_pending_with_one_candidate(tmp_path)
    assert try_weekly_interaction(cfg, "-1") is not None
    cands = (cfg.root / "data" / "qa" / "candidates.jsonl").read_text(encoding="utf-8")
    assert '"rejected"' in cands

    set_pending_weekly(cfg, "2026-W44")
    assert try_weekly_interaction(cfg, "0") is not None


def test_weekly_interaction_none_without_pending(tmp_path):
    from gacore.weekly_eval import try_weekly_interaction

    cfg = Config.for_tests(tmp_path)
    assert try_weekly_interaction(cfg, "4 3") is None
    assert try_weekly_interaction(cfg, "1") is None
