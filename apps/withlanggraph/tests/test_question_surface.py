"""变化面（question_surface）的封闭测试：T-S1 各路异常 / T-S2 全基线内 / T-S3 as-of / 缺库诚实。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gacore.config import Config
from gacore.question_surface import build_surface, render_surface


def _mk_db(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "langTrack.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_stats (
          day TEXT NOT NULL,
          total_screen_ms INTEGER NOT NULL DEFAULT 0,
          sleep_start_hhmm TEXT,
          unlock_count INTEGER,
          switch_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS kg_emotions (
          emotion_id TEXT PRIMARY KEY,
          subject TEXT NOT NULL,
          emotion TEXT NOT NULL,
          valence REAL,
          day TEXT NOT NULL
        );
        """
    )
    return db


def _insert_phone(conn: sqlite3.Connection, day: str, screen_min: int, sleep_hhmm: str | None) -> None:
    conn.execute(
        "INSERT INTO daily_stats (day, total_screen_ms, sleep_start_hhmm, unlock_count, switch_count) "
        "VALUES (?, ?, ?, 10, 20)",
        (day, screen_min * 60000, sleep_hhmm),
    )


def _seed_qa(tmp_path: Path, day: str, question: str, status: str = "open") -> None:
    qa_dir = tmp_path / "data" / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "question_key": f"key-{abs(hash(question)) % 10**8:08d}",
        "question": question,
        "anchor": "想问你-1",
        "day": day,
        "status": status,
        "asks": 1,
    }
    if status == "answered":
        row["answer_day"] = day
    with (qa_dir / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# T-S1：晚睡 + 屏幕倍增 + 提交冲刺 + 情绪下探 各路都能点亮
def test_t_s1_anomalies_detected(tmp_path: Path, monkeypatch):
    conn = sqlite3.connect(_mk_db(tmp_path))
    # 基线 30 天：23:20（1410 分钟）入睡、屏幕 150 分钟
    for i in range(30, 3, -1):
        day = f"2026-10-{26 - i:02d}" if 26 - i >= 1 else f"2026-09-{30 - (i - 26):02d}"
        _insert_phone(conn, day, 150, "23:20")
    # 近 3 天（观察窗 10-24~10-26）：01:10（70 分钟）入睡、屏幕 300 分钟
    for day, screen in (("2026-10-24", 300), ("2026-10-25", 310), ("2026-10-26", 290)):
        _insert_phone(conn, day, screen, "01:10")
    # 情绪：近 3 天效价 -0.6/-0.5/-0.7
    for i, (day, v) in enumerate((("2026-10-24", -0.6), ("2026-10-25", -0.5), ("2026-10-26", -0.7))):
        conn.execute(
            "INSERT INTO kg_emotions (emotion_id, subject, emotion, valence, day) VALUES (?, 'u', '疲惫', ?, ?)",
            (f"e{i}", v, day),
        )
    conn.commit()
    conn.close()

    commits = {f"2026-10-{d:02d}": 15 for d in (24, 25, 26)}
    commits.update({f"2026-09-{d:02d}": 1 for d in range(24, 27)})  # 基线窗 09-24 起
    commits.update({f"2026-10-{d:02d}": 1 for d in range(1, 24)})
    monkeypatch.setattr("gacore.question_surface._git_commit_counts", lambda cfg, s, e: commits)
    _seed_qa(tmp_path, "2026-10-20", "工学家椅下单了吗", status="answered")
    notes_dir = tmp_path / "memory" / "daily"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "2026-10-26.md").write_text("# 2026-10-26\n\n- 决定直接把领导的活接下来\n", encoding="utf-8")

    cfg = Config.for_tests(tmp_path)
    surface = build_surface(cfg, "2026-10-26")

    kinds = {a["kind"] for a in surface["anomalies"]}
    assert {"sleep_late", "screen_spike", "commit_surge", "mood_low"} <= kinds
    sleep = next(a for a in surface["anomalies"] if a["kind"] == "sleep_late")
    assert "01:10" in sleep["description"] and "23:20" in sleep["description"]
    text = render_surface(surface)
    assert "〔变化面·2026-10-26〕" in text
    assert "异常" in text and "已问过" in text and "工学家椅" in text
    assert "近 3 天日记摘要" in text  # notes 上下文（可能为空块，但行存在）


# T-S2：全部在基线内 → 无异常，渲染明示退 L1 或不出题
def test_t_s2_no_anomaly_within_baseline(tmp_path: Path):
    conn = sqlite3.connect(_mk_db(tmp_path))
    for i in range(30, 0, -1):
        day = f"2026-10-{26 - i + 1:02d}" if 26 - i + 1 >= 1 else f"2026-09-{30 - (i - 26) + 1:02d}"
        _insert_phone(conn, day, 150, "23:20")
    conn.commit()
    conn.close()
    cfg = Config.for_tests(tmp_path)
    surface = build_surface(cfg, "2026-10-26")
    assert surface["anomalies"] == []
    text = render_surface(surface)
    assert "本期无显著偏离" in text and "L1" in text


# T-S3：as-of 锚定——目标日之后的数据一律不读
def test_t_s3_asof_ignores_future(tmp_path: Path, monkeypatch):
    conn = sqlite3.connect(_mk_db(tmp_path))
    for i in range(30, 0, -1):
        day = f"2026-10-{26 - i + 1:02d}" if 26 - i + 1 >= 1 else f"2026-09-{30 - (i - 26) + 1:02d}"
        _insert_phone(conn, day, 150, "23:20")
    # 未来日（10-27/10-28）：晚睡 + 屏幕爆表——若被读到就会误报
    for day, screen in (("2026-10-27", 900), ("2026-10-28", 900)):
        _insert_phone(conn, day, screen, "02:30")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "gacore.question_surface._git_commit_counts",
        lambda cfg, s, e: {"2026-10-25": 2, "2026-10-27": 99},  # 桩尊重区间语义：未来日靠断言兜底验证
    )
    cfg = Config.for_tests(tmp_path)
    surface = build_surface(cfg, "2026-10-26")
    assert surface["anomalies"] == []
    assert "2026-10-27" not in json.dumps(surface, ensure_ascii=False)


# 缺库诚实：langTrack.db 不存在 → 该路留空，不抛
def test_missing_db_honest_empty(tmp_path: Path, monkeypatch):
    # 桩掉 git 计数：--basetemp 指向仓库内 tmp_pytest_run，tmp_path 向上探测会命中 mono 历史
    monkeypatch.setattr(
        "gacore.question_surface._git_commit_counts",
        lambda cfg, s, e: {},
    )
    cfg = Config.for_tests(tmp_path)
    surface = build_surface(cfg, "2026-10-26")
    assert surface["anomalies"] == []
    assert surface["context"]["commits_by_day"] == {}
    text = render_surface(surface)
    assert "本期无显著偏离" in text


# 基线样本不足（<5 天）→ 该路不判，不误报
def test_insufficient_baseline_no_false_positive(tmp_path: Path):
    conn = sqlite3.connect(_mk_db(tmp_path))
    _insert_phone(conn, "2026-10-25", 300, "01:10")
    _insert_phone(conn, "2026-10-26", 300, "01:10")
    conn.commit()
    conn.close()
    cfg = Config.for_tests(tmp_path)
    surface = build_surface(cfg, "2026-10-26")
    assert all(a["kind"] not in ("sleep_late", "screen_spike") for a in surface["anomalies"])
