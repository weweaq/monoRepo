"""数据目录：盘点手机采集（langTrack.db）与库外文件的原始数据资产。

/data 页（review_server）的数据层：纯读、只读 URI 连接、不触发 ETL、不写任何文件。
消费方映射（CONSUMERS / CONSUMER_SHORT）记录"哪些数据已被哪个源/工具/旁路消费"，
是 /data 页"未接数据自动浮出"的依据——新增信息包源、工具或旁路后必须同步更新，
否则页面会把已接数据误报为"未接"。

时间口径：东八区显式时区；近 7 日窗口从"今天"回推。
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Final

from gacore.config import Config

_TZ = datetime.timezone(datetime.timedelta(hours=8))
_DAYS_WINDOW: Final = 7
_PAYLOAD_MAX_CHARS: Final = 1000  # 单条 payload 展示截断（长载荷防刷屏）
_DAY_LIMIT_DEFAULT: Final = 500  # 单日 payload 取回条数默认值（防万级日渲染卡顿）
_DAY_LIMIT_MAX: Final = 20000
_HIST_EPOCH_MS: Final = 1767225600000  # 2026-01-01：早于该时间的事件 ts 视为脏数据不入直方图

# 事件类型 → 消费方完整描述（/data 右详情展示）
CONSUMERS: Final[dict[str, str]] = {
    "music_play": "_MEDIA 源（report._listen_music）",
    "location": "stays/trips ETL → _PHONE_PLACE 源 · fact_card · dashboard 地图",
    "session": "daily_stats ETL → _PHONE_USAGE 源 · fact_card · langTrack_stats",
    "usage": "daily_stats ETL → _PHONE_USAGE 源 · fact_card · langTrack_stats",
    "notification": "_PHONE_NOTIF 源（内容尾窗）+ daily_stats 聚合 → _PHONE_USAGE · fact_card",
    "accel": "无消费方",
    "battery": "无消费方",
    "network": "无消费方",
    "sms": "无消费方（隐私边界：仅本地日报+本人邮箱，不出网，军规7）",
    "input": "无消费方（隐私边界同军规7）",
    "clipboard": "无消费方（隐私边界同军规7）",
    "screen_content": "无消费方",
    "audio_env": "无消费方（fact_card 熬夜信号间接计数）",
    "audio_clip": "无消费方",
    "app_lifecycle": "无消费方",
    "snapshot": "无消费方",
}

# 事件类型 → 消费方短标（/data 左目录展示；未知类型回退"未接"）
CONSUMER_SHORT: Final[dict[str, str]] = {
    "music_play": "_MEDIA 源",
    "location": "_PHONE_PLACE·fact_card",
    "session": "_PHONE_USAGE·stats",
    "usage": "_PHONE_USAGE·stats",
    "notification": "_PHONE_NOTIF·聚合",
}

# 事件类型缺省映射兜底：不在 CONSUMERS 里的新类型按"未接"处理
_UNKNOWN_CONSUMER: Final = "无消费方"


def _hhmm_from_ms(ts_ms: int) -> str:
    return datetime.datetime.fromtimestamp(ts_ms / 1000, _TZ).strftime("%m-%d %H:%M")


def _day_range(conn: sqlite3.Connection, table: str, col: str) -> str:
    try:
        row = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM {table}").fetchone()
    except sqlite3.OperationalError:
        return "-"
    if not row or row[0] is None:
        return "-"
    lo, hi = row
    if col == "ts" or col.endswith("_ts"):  # ms epoch → MM-DD
        fmt = lambda v: _hhmm_from_ms(int(v))[:5]  # noqa: E731
    else:
        fmt = lambda v: str(v)[5:10] if len(str(v)) >= 10 else str(v)  # noqa: E731
    return f"{fmt(lo)} ~ {fmt(hi)}"


def _collect_tables(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        if name == "sqlite_sequence":
            continue
        n = int(conn.execute(f"SELECT COUNT(*) FROM '{name}'").fetchone()[0])
        cols = [c[1] for c in conn.execute(f"pragma table_info('{name}')")]
        ts_col = next((c for c in ("ts", "day") if c in cols), None)
        rng = _day_range(conn, name, ts_col) if ts_col and n else "-"
        if name.startswith("shadow_"):
            kind = "影子表"
        elif "backup" in name:
            kind = "备份"
        else:
            kind = "事实/过程" if n else "空表"
        tables.append({"name": name, "rows": n, "range": rng, "kind": kind})
    return tables


def _collect_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """事件类型盘点：总量/最新时间 + 全量按日直方图（右详情"按天"视图的数据源）。

    hist 为升序 [YYYY-MM-DD, count] 列表（ts 早于 2026 的脏数据不入图）；
    daily/days 为近 7 日窗口（左目录迷你柱状用），由 hist 派生。
    """
    grouped: dict[str, dict[str, int]] = {}
    for t, d, c in conn.execute(
        "SELECT type, strftime('%Y-%m-%d', ts/1000.0, 'unixepoch', '+8 hours') d, COUNT(*)"
        " FROM events WHERE ts >= ? GROUP BY type, d ORDER BY d",
        (_HIST_EPOCH_MS,),
    ):
        grouped.setdefault(t, {})[d] = int(c)
    today = datetime.datetime.now(_TZ).date()
    days = [
        (today - datetime.timedelta(days=i)).strftime("%m-%d")
        for i in range(_DAYS_WINDOW - 1, -1, -1)
    ]
    out: list[dict[str, Any]] = []
    for t, total, last_ts in conn.execute(
        "SELECT type, COUNT(*), MAX(ts) FROM events GROUP BY type ORDER BY COUNT(*) DESC"
    ):
        hist = sorted(grouped.get(t, {}).items())
        day_full = {d[-5:]: c for d, c in hist}  # MM-DD → count（近7日窗取用）
        consumer = CONSUMERS.get(t, _UNKNOWN_CONSUMER)
        out.append({
            "type": t,
            "total": int(total),
            "last": _hhmm_from_ms(int(last_ts)) if last_ts else "-",
            "daily": [day_full.get(d, 0) for d in days],
            "days": days,
            "hist": hist,
            "consumer": consumer,
            "short": CONSUMER_SHORT.get(t, "未接"),
        })
    return out


def events_for_day(cfg: Config, etype: str, day: str, limit: int = _DAY_LIMIT_DEFAULT) -> dict[str, Any]:
    """取某事件类型某天的全部 payload（/data 右详情"按天"取数，GET /api/data/events）。

    limit 默认 500、上限 20000（audio_env 曾有单日 1.3 万条突发，全量渲染需用户显式点击）。
    day/etype 非法抛 ValueError（路由层转 400）。
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError(f"bad day: {day!r}")
    if not re.fullmatch(r"[a-z_]{1,40}", etype):
        raise ValueError(f"bad type: {etype!r}")
    limit = max(1, min(int(limit), _DAY_LIMIT_MAX))
    start_ms = int(
        datetime.datetime.strptime(day, "%Y-%m-%d")
        .replace(tzinfo=_TZ)
        .timestamp()
        * 1000
    )
    end_ms = start_ms + 86_400_000
    db_path = cfg.root / "data" / "langTrack.db"
    if not db_path.is_file():
        raise ValueError("langTrack.db 不存在")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE type=? AND ts>=? AND ts<?",
                (etype, start_ms, end_ms),
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT ts, payload FROM events WHERE type=? AND ts>=? AND ts<?"
            " ORDER BY ts DESC LIMIT ?",
            (etype, start_ms, end_ms, limit),
        ).fetchall()
    finally:
        conn.close()
    samples = []
    for ts, payload in rows:
        try:
            text = json.dumps(json.loads(payload), ensure_ascii=False)
        except Exception:  # noqa: BLE001 — 非法 JSON 原样截断展示
            text = str(payload)
        if len(text) > _PAYLOAD_MAX_CHARS:
            text = text[:_PAYLOAD_MAX_CHARS] + "…"
        samples.append({
            "ts": datetime.datetime.fromtimestamp(int(ts) / 1000, _TZ).strftime("%H:%M:%S"),
            "payload": text,
        })
    return {
        "ok": True,
        "type": etype,
        "day": day,
        "total": total,
        "returned": len(samples),
        "truncated": len(samples) < total,
        "samples": samples,
    }


def _collect_files(cfg: Config) -> list[dict[str, Any]]:
    """库外文件资产：行数、最后写入、消费方映射（与信息包源/旁路对应）。"""
    specs: list[tuple[Path, str]] = [
        (cfg.memory_dir / "qq_chat_log.jsonl", "QQ 收发消息 → _CHAT 源"),
        (cfg.memory_dir / "ocr_history.jsonl", "无消费方"),
        (cfg.root / "data" / "weather_cache.json", "report.py 节奏分析（未进信息包）"),
        (cfg.memory_dir / "global_mem_insight.txt", "编年史 → _LONG_TERM 源"),
        (cfg.memory_dir / "global_mem_anchor.txt", "固定锚 → _LONG_TERM 源"),
    ]
    files: list[dict[str, Any]] = []
    for path, consumer in specs:
        if not path.is_file():
            continue
        try:
            n = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, _TZ).strftime(
            "%m-%d %H:%M"
        )
        rel = path.relative_to(cfg.root).as_posix()
        files.append({"name": rel, "lines": n, "mtime": mtime, "consumer": consumer})
    notes_dir = cfg.memory_dir / "daily"
    if notes_dir.is_dir():
        notes = sorted(x.name for x in notes_dir.glob("*.md"))
        files.append({
            "name": "memory/daily/*.md",
            "lines": len(notes),
            "mtime": notes[-1] if notes else "-",
            "consumer": "近2日摘要 → system prompt 旁路",
        })
    return files


def collect(cfg: Config) -> dict[str, Any]:
    """汇总数据目录。langTrack.db 缺失/损坏时返回空态，不抛异常（页面可降级渲染）。"""
    db_path = cfg.root / "data" / "langTrack.db"
    tables: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    db_note = ""
    if db_path.is_file():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                tables = _collect_tables(conn)
                events = _collect_events(conn)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            db_note = f"langTrack.db 读取失败：{exc}"
    else:
        db_note = "langTrack.db 不存在（手机采集未启用或数据目录未迁移）"
    return {
        "generated_at": datetime.datetime.now(_TZ).strftime("%Y-%m-%d %H:%M"),
        "db_note": db_note,
        "tables": tables,
        "events": events,
        "files": _collect_files(cfg),
    }
