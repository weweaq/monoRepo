"""Tests for gacore.tools.langTrack_tools — fully mocked, no real DB / ETL / network."""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gacore.tools.langTrack_tools as langTrack_mod

# Ensure we have the real module (not the StructuredTool shadowed in __init__.py).
if not isinstance(langTrack_mod, types.ModuleType):
    langTrack_mod = sys.modules["gacore.tools.langTrack_tools"]

from gacore.tools.langTrack_tools import langTrack_stats

_TZ = timezone(timedelta(hours=8))


def _ts(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=_TZ).timestamp() * 1000)


def _make_db(root: Path, day: str, with_stats: bool = True) -> None:
    """Build a minimal langTrack.db: daily_stats / places / events / stays / trips /
    anomalies / etl_state / contract_coverage for a day (device 'd')."""
    db = root / "data" / "langTrack.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE daily_stats (
          device_id TEXT, day TEXT, total_screen_ms INTEGER, app_ranking_json TEXT,
          notification_count INTEGER, notification_clicked INTEGER,
          top_notification_apps_json TEXT,
          screen_on_count INTEGER, screen_off_count INTEGER,
          unlock_count INTEGER, switch_count INTEGER,
          location_count INTEGER, audio_clip_count INTEGER
        );
        CREATE TABLE places (
          id INTEGER PRIMARY KEY, device_id TEXT, grid_key TEXT,
          lat REAL, lon REAL, label TEXT,
          first_seen INTEGER, last_seen INTEGER, visit_count INTEGER,
          is_primary INTEGER, poi TEXT, behavior TEXT, district TEXT, address TEXT
        );
        CREATE TABLE events (
          id INTEGER PRIMARY KEY, device_id TEXT, ts INTEGER,
          type TEXT, payload TEXT, received_at INTEGER,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE stays (
          id INTEGER PRIMARY KEY, device_id TEXT, grid_key TEXT,
          start_ts INTEGER, end_ts INTEGER, day TEXT
        );
        CREATE TABLE trips (
          id INTEGER PRIMARY KEY, device_id TEXT,
          start_ts INTEGER, end_ts INTEGER, dist_m INTEGER, day TEXT
        );
        CREATE TABLE anomalies (
          id INTEGER PRIMARY KEY, device_id TEXT, day TEXT,
          kind TEXT, poi TEXT, detail TEXT, ts INTEGER
        );
        CREATE TABLE etl_state (
          device_id TEXT PRIMARY KEY, last_event_ts INTEGER
        );
        CREATE TABLE contract_coverage (
          type TEXT, desc TEXT, status TEXT, last_seen_ts INTEGER, consumed INTEGER
        );
        """
    )
    if with_stats:
        conn.execute(
            "INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "d", day, 7_000_000,
                json.dumps([{"app": "微信", "ms": 2_000_000},
                            {"app": "飞书", "ms": 1_800_000}], ensure_ascii=False),
                10, 1, json.dumps([{"app": "微信", "n": 5}], ensure_ascii=False),
                3, 2, 3, 100, 50, 0,
            ),
        )
    conn.execute(
        "INSERT INTO places VALUES (1,'d','31.975,118.767',31.975,118.767,'公司',0,0,440,1,"
        "'河西商务楼','办公','建邺区','江东路1号')"
    )
    conn.execute(
        "INSERT INTO places VALUES (2,'d','31.993,118.783',31.993,118.783,'家',0,0,260,1,"
        "'小区','居住','鼓楼区','上海路9号')"
    )
    # 当日公司停留（覆盖白天）与一段移动
    day_start = _ts(2026, 8, 18, 0, 0)
    conn.execute(
        "INSERT INTO stays(id,device_id,grid_key,start_ts,end_ts,day) VALUES (?,?,?,?,?,?)",
        (1, "d", "31.975,118.767", _ts(2026, 8, 18, 9, 0), _ts(2026, 8, 18, 17, 30), day),
    )
    conn.execute(
        "INSERT INTO trips(id,device_id,start_ts,end_ts,dist_m,day) VALUES (?,?,?,?,?,?)",
        (1, "d", _ts(2026, 8, 18, 12, 0), _ts(2026, 8, 18, 12, 20), 800, day),
    )
    conn.execute(
        "INSERT INTO anomalies(id,device_id,day,kind,poi,detail,ts) VALUES (?,?,?,?,?,?,?)",
        (1, "d", day, "new_place", "德基广场", "新地点", _ts(2026, 8, 18, 12, 20)),
    )
    # ETL 水位：测试日 17:06
    conn.execute(
        "INSERT INTO etl_state(device_id,last_event_ts) VALUES (?,?)",
        ("d", _ts(2026, 8, 18, 17, 6)),
    )
    # 凌晨音频样本：6 条 → 触发"疑似熬夜"信号（2026-08-18 00:00 起，东八区）；
    # 仅在有 daily_stats 时插入，保证「无数据」场景 sleep_signal 保持"无数据"文案
    if with_stats:
        for i in range(6):
            conn.execute(
                "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
                (i + 1, "d", day_start + i * 60_000, "audio_env",
                 json.dumps({"is_silent": True}), day_start),
            )
    conn.commit()
    conn.close()


def test_registered_in_tool_names():
    """工具必须在 registry 中注册（外挂式：import + TOOL_NAMES + _TOOLS 三处）。"""
    from gacore.tools import TOOL_NAMES, build_tool_list
    assert "langTrack_stats" in TOOL_NAMES
    names = [t.name for t in build_tool_list(None)]
    assert "langTrack_stats" in names


def test_stats_returns_day_profile(monkeypatch, tmp_path):
    """正常路径：返回结构化画像（含新的事实卡片字段）。"""
    _make_db(tmp_path, "2026-08-18", with_stats=True)
    monkeypatch.setattr(langTrack_mod, "_DEFAULT_ROOT", tmp_path)
    # 跳过真实 ETL（子进程）
    monkeypatch.setattr(langTrack_mod, "_ensure_etl", lambda: True)

    r = langTrack_stats.invoke({"day": "2026-08-18"})
    assert r["available"] is True
    assert r["has_facts"] is True
    assert r["day"] == "2026-08-18"
    assert r["screen_hours"] == round(7_000_000 / 3600000, 2)
    assert r["top_apps"][0]["app"] == "微信"
    assert r["notification_count"] == 10
    assert "疑似熬夜" in r["sleep_signal"]  # 6 条凌晨音频样本触发
    assert r["places"][0]["label"] == "江东路1号〔公司〕"
    # FactCard 公共字段透传
    assert r["device_id"] == "d"
    assert r["ambiguous_device"] is False
    assert isinstance(r["etl_watermark_ms"], int)
    assert r["day_window_closed"] is False  # 历史日且 ETL 水位(17:06)未跨日末
    assert r["current_known"]["label"] == "江东路1号〔公司〕"
    assert r["stays"] and r["stays"][0]["label"] == "江东路1号〔公司〕"
    assert r["trips"] and r["trips"][0]["start_hhmm"] == "12:00"  # 嵌入 stay 内，不配 from_label
    assert isinstance(r["trips"][0]["from_label"], str)
    assert r["anomalies"] and r["anomalies"][0]["kind"] == "new_place"
    assert r.get("compact")
    assert r["compact_sections"] and r["compact_lines"]
    assert isinstance(r["compact_omitted"], dict)
    assert r["card_fp"]
    assert r["top_notification_apps"][0]["app"] == "微信"
    assert r["screen_on_count"] == 3


def test_stats_no_data_returns_unavailable(monkeypatch, tmp_path):
    """无当日数据：available=False + 说明。"""
    _make_db(tmp_path, "2026-08-18", with_stats=False)
    monkeypatch.setattr(langTrack_mod, "_DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(langTrack_mod, "_ensure_etl", lambda: True)

    r = langTrack_stats.invoke({"day": "2026-08-18"})
    assert r["available"] is False
    assert "无数据" in r["sleep_signal"]


def test_stats_missing_db_returns_unavailable(monkeypatch, tmp_path):
    """数据库不存在：available=False，不抛异常。"""
    monkeypatch.setattr(langTrack_mod, "_DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(langTrack_mod, "_ensure_etl", lambda: True)

    r = langTrack_stats.invoke({"day": "2026-08-18"})
    assert r["available"] is False
    assert "不存在" in r["sleep_signal"]


def test_stats_etl_failure_does_not_block(monkeypatch, tmp_path):
    """ETL 失败不阻塞：返回旧数据或 unavailable。"""
    _make_db(tmp_path, "2026-08-18", with_stats=True)
    monkeypatch.setattr(langTrack_mod, "_DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(langTrack_mod, "_ensure_etl", lambda: False)

    r = langTrack_stats.invoke({"day": "2026-08-18"})
    assert r["available"] is True  # ETL 失败但仍读到旧事实表


def test_stats_db_error_returns_message(monkeypatch, tmp_path):
    """DB 读取异常：返回错误信息而非抛出。"""
    monkeypatch.setattr(langTrack_mod, "_DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(langTrack_mod, "_ensure_etl", lambda: True)
    # 建一个损坏的 db 文件
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "langTrack.db").write_bytes(b"not a database")

    r = langTrack_stats.invoke({"day": "2026-08-18"})
    assert r["available"] is False
    assert "读取失败" in r["sleep_signal"]


def test_stats_multi_device_ambiguous(monkeypatch, tmp_path):
    """多设备且未指定 device_id：ambiguity 状态透传，不擅自选设备。"""
    _make_db(tmp_path, "2026-08-18", with_stats=True)
    # 第二台设备 dev2 的同日数据 → 设备歧义
    db = tmp_path / "data" / "langTrack.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "dev2", "2026-08-18", 1_000_000, "[]", 1, 0, "[]", 0, 0, 0, 0, 0, 0,
        ),
    )
    conn.execute(
        "INSERT INTO etl_state(device_id,last_event_ts) VALUES (?,?)",
        ("dev2", _ts(2026, 8, 18, 12, 0)),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(langTrack_mod, "_DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(langTrack_mod, "_ensure_etl", lambda: True)

    r = langTrack_stats.invoke({"day": "2026-08-18"})
    assert r["available"] is False
    assert r["ambiguous_device"] is True
    assert "dev2" in r["candidate_device_ids"]
    assert r["device_id"] == ""
