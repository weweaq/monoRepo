"""变化面（question surface，主动询问 3.0 P3b）：为归因式出题组织「哪里不对劲 + 同期发生了什么」。

设计：docs/proactive-inquiry-3.0-design.md §3.2。纯读，零 LLM——异常判定全部是确定性阈值；
LLM（composer，P3c）负责把异常 + 同期事件归因成试探式问题。

as-of 兼容：所有取数锚定 ``day``（重跑历史日不会读到未来的 daily_stats/kg_emotions/git/笔记/问答）。
输出 ``build_surface()`` 结构化 dict + ``render_surface()`` 给 prompt 的文本块。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from gacore.config import Config
from gacore.jsonl_logger import get_logger
from gacore.weekly_stats import _git_commit_counts

logger = get_logger("question_surface")

_TZ: Final = timezone(timedelta(hours=8))
_RECENT_DAYS: Final = 3        # 异常观察窗
_BASELINE_DAYS: Final = 30     # 基线窗（观察窗之前）
_MIN_BASELINE: Final = 5       # 基线样本下限：不足则该路不判（诚实留空）
_SLEEP_LATE_MIN: Final = 60    # 入睡比基线晚 ≥60 分钟记偏离
_SCREEN_SPIKE_X: Final = 1.8   # 近 3 天屏幕均值 ≥ 基线 1.8 倍
_SCREEN_DROP_X: Final = 0.5    # 近 3 天屏幕均值 ≤ 基线 0.5 倍
_COMMIT_SURGE_X: Final = 3.0   # 近 3 天日均提交 ≥ 基线日均 3 倍
_COMMIT_SURGE_MIN: Final = 5   # 且近 3 天单日峰值 ≥5 笔（防基线近 0 的噪声放大）
_MOOD_LOW_VALENCE: Final = -0.4
_ASOF_DAYS: Final = 14         # 已问过回看窗


def _day_offset(day: str, offset: int) -> str:
    return (datetime.fromisoformat(day) + timedelta(days=offset)).date().isoformat()


def _phone_daily(cfg: Config, start: str, end: str) -> dict[str, dict[str, Any]]:
    """daily_stats 只读：day → {screen_min, sleep_start_min, unlock, switch}；缺库/缺表 → {}。"""
    import json

    db = cfg.root / "data" / "langTrack.db"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT day, total_screen_ms, sleep_start_hhmm, unlock_count, switch_count "
            "FROM daily_stats WHERE day >= ? AND day <= ?",
            (start, end),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for day, screen_ms, sleep_hhmm, unlock, switch in rows:
        entry: dict[str, Any] = {
            "screen_min": round(float(screen_ms) / 60000.0) if screen_ms else 0,
            "unlock": unlock,
            "switch": switch,
        }
        if sleep_hhmm:
            try:
                hh, mm = str(sleep_hhmm).split(":")[:2]
                entry["sleep_start_min"] = int(hh) * 60 + int(mm)
                entry["sleep_hhmm"] = str(sleep_hhmm)
            except (ValueError, IndexError):
                pass
        out[str(day)] = entry
    _ = json  # 保留 import 位置：app_ranking 解码留给 composer 阶段
    return out


def _emotion_series(cfg: Config, start: str, end: str) -> dict[str, float]:
    """kg_emotions 日均效价（day → mean valence）；缺库/缺表 → {}。"""
    db = cfg.root / "data" / "langTrack.db"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT day, AVG(valence) FROM kg_emotions WHERE day >= ? AND day <= ? "
            "AND valence IS NOT NULL GROUP BY day",
            (start, end),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(day): round(float(v), 2) for day, v in rows}


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _fmt_min(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _norm_sleep_min(raw: int) -> int:
    """入睡时刻跨午夜归一：凌晨（<06:00）视为「前一天晚上 + 1440」，使 01:10 > 23:20 可比。"""
    return raw + 1440 if raw < 360 else raw


def _sleep_late(baseline: dict[str, dict[str, Any]], recent: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """入睡漂移：近 3 天中 ≥2 天入睡比基线均值晚 ≥_SLEEP_LATE_MIN（跨午夜归一后比较）。"""
    base = [_norm_sleep_min(v["sleep_start_min"]) for v in baseline.values() if "sleep_start_min" in v]
    if len(base) < _MIN_BASELINE:
        return None
    base_mean = _mean([float(v) for v in base])
    drifted: dict[str, str] = {}
    for d, v in recent.items():
        if "sleep_start_min" not in v:
            continue
        if _norm_sleep_min(v["sleep_start_min"]) - base_mean >= _SLEEP_LATE_MIN:
            drifted[d] = v.get("sleep_hhmm", _fmt_min(v["sleep_start_min"]))
    if len(drifted) < 2:
        return None
    days = sorted(drifted)
    return {
        "kind": "sleep_late",
        "days": days,
        "description": (
            f"近 {_RECENT_DAYS} 天入睡 {('/'.join(drifted[d] for d in days))}，"
            f"此前 {_BASELINE_DAYS} 天基线 {_fmt_min(int(base_mean) % 1440)}，"
            f"平均推迟约 {int(_mean([_norm_sleep_min(int(drifted[d][:2]) * 60 + int(drifted[d][3:5])) - base_mean for d in days]))} 分钟"
        ),
        "evidence": {"baseline_mean_min": round(base_mean), "drifted": drifted},
    }


def _screen_shift(baseline: dict[str, dict[str, Any]], recent: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """屏幕时长倍增/骤降：近 3 天均值 vs 基线均值。"""
    base = [v["screen_min"] for v in baseline.values()]
    if len(base) < _MIN_BASELINE:
        return None
    base_mean = _mean([float(v) for v in base])
    if base_mean <= 0:
        return None
    recent_mean = _mean([float(v["screen_min"]) for v in recent.values()])
    ratio = recent_mean / base_mean
    days = sorted(recent)
    if ratio >= _SCREEN_SPIKE_X:
        kind, label = "screen_spike", "屏幕时长显著上涨"
    elif ratio <= _SCREEN_DROP_X:
        kind, label = "screen_drop", "屏幕时长显著回落"
    else:
        return None
    return {
        "kind": kind,
        "days": days,
        "description": (
            f"{label}：近 {_RECENT_DAYS} 天日均 {recent_mean:.1f} 分钟，"
            f"基线日均 {base_mean:.1f} 分钟（{ratio:.1f} 倍）"
        ),
        "evidence": {"baseline_mean": round(base_mean, 1), "recent_mean": round(recent_mean, 1), "ratio": round(ratio, 2)},
    }


def _commit_surge(commits_by_day: dict[str, int], day: str) -> dict[str, Any] | None:
    """提交节奏突变：近 3 天日均 ≥ 基线（前 30 天，不含观察窗）3 倍且峰值 ≥_COMMIT_SURGE_MIN。"""
    recent_start = _day_offset(day, -(_RECENT_DAYS - 1))
    base_start = _day_offset(day, -(_RECENT_DAYS - 1) - _BASELINE_DAYS)
    baseline = {
        d: c
        for d, c in commits_by_day.items()
        if base_start <= d < recent_start
    }
    recent = {d: c for d, c in commits_by_day.items() if recent_start <= d <= day}
    if len(baseline) < _MIN_BASELINE or not recent:
        return None
    base_mean = _mean([float(v) for v in baseline.values()])
    recent_mean = _mean([float(v) for v in recent.values()])
    peak = max(recent.values())
    if base_mean <= 0 or recent_mean < base_mean * _COMMIT_SURGE_X or peak < _COMMIT_SURGE_MIN:
        return None
    return {
        "kind": "commit_surge",
        "days": sorted(recent),
        "description": (
            f"提交节奏突变：近 {_RECENT_DAYS} 天日均 {recent_mean:.1f} 笔"
            f"（峰值 {peak}），基线日均 {base_mean:.1f} 笔（{_COMMIT_SURGE_X:.0f} 倍阈值）"
        ),
        "evidence": {"baseline_mean": round(base_mean, 1), "recent_mean": round(recent_mean, 1), "peak": peak},
    }


def _mood_low(emotions: dict[str, float], day: str) -> dict[str, Any] | None:
    """情绪下探：近 3 天出现日均效价 ≤_MOOD_LOW_VALENCE（标注是否连跌）。"""
    recent = {d: v for d, v in emotions.items() if d > _day_offset(day, -_RECENT_DAYS)}
    lows = {d: v for d, v in recent.items() if v <= _MOOD_LOW_VALENCE}
    if not lows:
        return None
    ordered = sorted(recent)
    falling = all(
        recent[ordered[i]] > recent[ordered[i + 1]] for i in range(len(ordered) - 1)
    ) if len(ordered) >= 2 else False
    tail = "，且连续走低" if falling and lows == recent else ""
    return {
        "kind": "mood_low",
        "days": sorted(lows),
        "description": (
            f"情绪下探：{'/'.join(f'{d[5:]} {v:+.1f}' for d, v in sorted(lows.items()))}（效价 ≤{_MOOD_LOW_VALENCE}）{tail}"
        ),
        "evidence": {"lows": lows, "series": recent},
    }


def build_surface(cfg: Config, day: str, *, asked_limit: int = 6) -> dict[str, Any]:
    """组装目标日的变化面（纯读，as-of 锚定 ``day``）。

    返回 {day, anomalies, context{commits_by_day, notes, emotion_series}, asked_recent}。
    任何一路取数失败只记日志、该路留空，绝不抛出（出题链路 fail-open）。
    """
    recent_start = _day_offset(day, -(_RECENT_DAYS - 1))
    baseline_start = _day_offset(day, -(_RECENT_DAYS - 1) - _BASELINE_DAYS)
    asof_start = _day_offset(day, -_ASOF_DAYS)

    anomalies: list[dict[str, Any]] = []
    commits_by_day: dict[str, int] = {}
    notes = ""
    emotion_series: dict[str, float] = {}
    try:
        commits_by_day = {
            d: c for d, c in _git_commit_counts(cfg, asof_start, day).items() if d <= day
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("surface git failed", error_type=type(exc).__name__, error=str(exc))
    try:
        from gacore.tools.daily_notes import load_recent_daily_summaries

        notes = load_recent_daily_summaries(cfg, days=3, as_of=day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("surface notes failed", error_type=type(exc).__name__, error=str(exc))
    try:
        emotion_series = _emotion_series(cfg, recent_start, day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("surface emotions failed", error_type=type(exc).__name__, error=str(exc))

    try:
        phone = _phone_daily(cfg, baseline_start, day)
        baseline = {d: v for d, v in phone.items() if d < recent_start}
        recent = {d: v for d, v in phone.items() if d >= recent_start}
        for detector in (_sleep_late, _screen_shift):
            found = detector(baseline, recent)
            if found:
                anomalies.append(found)
    except Exception as exc:  # noqa: BLE001
        logger.warning("surface phone failed", error_type=type(exc).__name__, error=str(exc))
    try:
        found = _commit_surge(commits_by_day, day)
        if found:
            anomalies.append(found)
    except Exception as exc:  # noqa: BLE001
        logger.warning("surface commits failed", error_type=type(exc).__name__, error=str(exc))
    try:
        found = _mood_low(emotion_series, day)
        if found:
            anomalies.append(found)
    except Exception as exc:  # noqa: BLE001
        logger.warning("surface mood failed", error_type=type(exc).__name__, error=str(exc))

    asked_recent: list[dict[str, str]] = []
    try:
        from gacore import qa_store

        answered, open_ = qa_store.qa_brief(cfg, before_date=day, recent_days=_ASOF_DAYS)
        for row in answered + open_:
            asked_recent.append(
                {
                    "day": str(row.get("day", "")),
                    "question": str(row.get("question", ""))[:60],
                    "status": "已答" if row.get("status") == "answered" else "未答",
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("surface qa_brief failed", error_type=type(exc).__name__, error=str(exc))
    asked_recent = asked_recent[:asked_limit]

    surface: dict[str, Any] = {
        "day": day,
        "anomalies": anomalies,
        "context": {
            "commits_by_day": dict(sorted(commits_by_day.items())),
            "notes": notes,
            "emotion_series": emotion_series,
        },
        "asked_recent": asked_recent,
    }
    logger.info(
        "surface built",
        day=day,
        anomalies=[a["kind"] for a in anomalies],
        asked=len(asked_recent),
    )
    return surface


def render_surface(surface: dict[str, Any]) -> str:
    """变化面 → prompt 文本块；无异常时明示「退 L1 闲聊或不出题」。"""
    day = surface.get("day", "")
    lines = [f"〔变化面·{day}〕"]
    for i, a in enumerate(surface.get("anomalies") or [], 1):
        lines.append(f"- 异常{i}（{a['kind']}）：{a['description']}（涉及 {'、'.join(a['days'])}）")
    if not surface.get("anomalies"):
        lines.append("- 本期无显著偏离 → 只可出 0-1 条 L1 闲聊，或不出题")
    ctx = surface.get("context") or {}
    commits = ctx.get("commits_by_day") or {}
    if commits:
        series = " ".join(f"{d[5:]}:{c}" for d, c in commits.items())
        lines.append(f"- 近两周提交/日：{series}")
    emotions = ctx.get("emotion_series") or {}
    if emotions:
        series = " ".join(f"{d[5:]}:{v:+.1f}" for d, v in sorted(emotions.items()))
        lines.append(f"- 近 3 天情绪效价：{series}")
    asked = surface.get("asked_recent") or []
    if asked:
        items = "；".join(f"{a['question']}（{a['day'][5:]}·{a['status']}）" for a in asked)
        lines.append(f"- 近 {_ASOF_DAYS} 天已问过（一律不再问）：{items}")
    notes = (ctx.get("notes") or "").strip()
    if notes:
        lines.append(f"- 近 3 天日记摘要（归因素材）：\n{notes}")
    return "\n".join(lines)
