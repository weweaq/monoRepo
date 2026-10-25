"""周报数字聚合（weekly_stats）与评分捕获（weekly_eval）的封闭测试。"""

from __future__ import annotations

import json

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
