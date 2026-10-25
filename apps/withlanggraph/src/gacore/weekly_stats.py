"""周报数字聚合（P4，spec §5.4 铁律：数字由程序填，叙事由模型写）。

`collect_week_numbers` 把一周的确定性数字（git 提交/屏幕/睡眠/审计分/提问回答 + 环比）
算成一段只读 markdown 注入周报 prompt；模型只能引用，不能自己数。
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("weekly_stats")

_TZ = timezone(timedelta(hours=8))


def week_of(day: date | datetime) -> str:
    """ISO 周编号 `YYYY-Www`（周一为一周之始）。"""
    d = day.date() if isinstance(day, datetime) else day
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_window(week: str) -> tuple[str, str]:
    """`YYYY-Www` → (周一, 周日) ISO 日期对。"""
    year, num = week.split("-W")
    monday = date.fromisocalendar(int(year), int(num), 1)
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _days(week: str) -> list[str]:
    monday, _ = week_window(week)
    start = date.fromisoformat(monday)
    return [(start + timedelta(days=i)).isoformat() for i in range(7)]


def _prev_week(week: str) -> str:
    year, num = week.split("-W")
    monday = date.fromisocalendar(int(year), int(num), 1)
    return week_of(monday - timedelta(days=7))


def _git_commit_counts(cfg: Config, start: str, end: str) -> dict[str, int]:
    """窗口内 git 提交按日计数（repo = cfg.root 所在仓库；失败返回空，行标无数据）。"""
    try:
        out = subprocess.run(  # noqa: S603 - 固定参数调用本仓 git
            [
                "git", "-C", str(cfg.root), "log", "--all", "--no-merges",
                f"--since={start} 00:00:00", f"--until={end} 23:59:59",
                "--pretty=format:%ad", "--date=format:%Y-%m-%d",
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("weekly git log failed", error_type=type(exc).__name__)
        return {}
    return dict(Counter(line.strip() for line in out.splitlines() if line.strip()))


def _phone_stats(cfg: Config, days: list[str]) -> dict[str, tuple[float, int | None]]:
    """屏幕小时/睡眠分钟 per day（daily_stats 只读；缺库/缺表/缺行 → 空）。"""
    import sqlite3

    out: dict[str, tuple[float, int | None]] = {}
    db = cfg.root / "data" / "langTrack.db"
    if not db.is_file():
        return out
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        placeholders = ",".join("?" * len(days))
        rows = conn.execute(
            f"SELECT day, total_screen_ms, sleep_duration_min FROM daily_stats "
            f"WHERE day IN ({placeholders})",  # noqa: S608 - 占位符拼接白名单列
            days,
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    for day, screen_ms, sleep_min in rows:
        out[str(day)] = (round((screen_ms or 0) / 3_600_000, 1), sleep_min)
    return out


def _audit_scores(cfg: Config, days: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    audit_dir = cfg.logs_dir / "report_audit"
    if not audit_dir.is_dir():
        return out
    for day in days:
        f = audit_dir / f"{day}.v1.json"
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and not data.get("missing"):
            out[day] = int(data.get("score") or 0)
    return out


def _qa_counts(cfg: Config, days: list[str]) -> dict[str, int]:
    from gacore.qa_store import all_rows

    asked: Counter[str] = Counter()
    answered: Counter[str] = Counter()
    for r in all_rows(cfg):
        day = str(r.get("day", ""))
        if day in days:
            asked[day] += 1
            if r.get("status") == "answered":
                answered[day] += 1
    return {"asked": dict(asked), "answered": dict(answered)}


def collect_week_numbers(cfg: Config, week: str) -> str:
    """一周确定性数字的只读 markdown（含铁律声明与环比）；任何子源失败都降级为标注行。"""
    days = _days(week)
    prev_days = _days(_prev_week(week))
    commits = _git_commit_counts(cfg, days[0], days[-1])
    prev_commits = _git_commit_counts(cfg, prev_days[0], prev_days[-1])
    phone = _phone_stats(cfg, days)
    scores = _audit_scores(cfg, days)
    qa = _qa_counts(cfg, days)

    def _seq(fmt: Any, keys: list[str], missing: str = "—") -> str:
        return " ".join(str(fmt(k)) if fmt(k) is not None else missing for k in keys)

    commit_series = _seq(lambda d: commits.get(d), days)
    screen_series = _seq(lambda d: phone.get(d, (None, None))[0], days)
    sleep_series = _seq(lambda d: phone.get(d, (None, None))[1], days)
    score_series = _seq(lambda d: scores.get(d), days)

    total_commits = sum(commits.values())
    prev_total = sum(prev_commits.values())
    score_avg = round(sum(scores.values()) / len(scores), 1) if scores else None
    screens = [v[0] for v in phone.values() if v[0] is not None]
    screen_avg = round(sum(screens) / len(screens), 1) if screens else None
    answered_total = sum(qa["answered"].values())
    asked_total = sum(qa["asked"].values())

    def _delta(now: float | int | None, prev: float | int | None) -> str:
        if not now or not prev:
            return "—"
        pct = (now - prev) / prev * 100
        return f"{pct:+.0f}%"

    lines = [
        f"〔周报数字区·{week}（周一 {days[0]} ~ 周日 {days[-1]}）〕以下数字由程序计算，只读引用，"
        "禁止自行数数、改写或推算（铁律：数字由程序填，叙事由模型写）。",
        f"- git 提交/日：{commit_series}（周计 {total_commits}，上周 {prev_total}，环比 {_delta(total_commits, prev_total)}）",
        f"- 屏幕小时/日：{screen_series}（周均 {screen_avg if screen_avg is not None else '—'}h）",
        f"- 睡眠分钟/日：{sleep_series}",
        f"- 审计分/日（v1）：{score_series}（周均 {score_avg if score_avg is not None else '—'}）",
        f"- 想问你：本周提出 {asked_total} 条，已回答 {answered_total} 条",
    ]
    no_data = [name for name, series in (
        ("git 提交", commits), ("屏幕/睡眠", phone), ("审计分", scores),
    ) if not series]
    if no_data:
        lines.append(f"- 缺数据：{'、'.join(no_data)}（对应序列为 —，写作时如实标注，不要编造）")
    return "\n".join(lines)


def write_weekly(cfg: Config, week: str, text: str) -> Path:
    """周报正文存档 `logs/delivered_weekly/{week}.md`。"""
    out = cfg.logs_dir / "delivered_weekly"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{week}.md"
    path.write_text(text, encoding="utf-8")
    return path


def pending_candidates(cfg: Config, limit: int = 3) -> list[dict[str, Any]]:
    """candidates.jsonl 中 pending 的画像候选（新→旧，最多 limit 条），供周报渲染与回执配对。"""
    path = cfg.root / "data" / "qa" / "candidates.jsonl"
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("status") == "pending":
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def week_now() -> str:
    return week_of(datetime.now(tz=_TZ))
