"""test_langTrack_report_evidence.py —— Task 10 统一 report/persona 的证据边界。

覆盖（Task 10 清单）：
- 餐饮/医疗 POI 不输出“用餐/就医”的确定性陈述；
- 公司停留不直接输出“上班”；trip 数量不直接输出“通勤稳定”；
- 最后定位早于当前时间时只输出“最后记录到”（不冒充“此刻”）；
- report 场景输出使用 PlaceRef 安全文案（真名〔tag〕），不输出坐标、grid_key；
- detect_anomalies/new_place 使用 visit_episodes（停留段数）口径，detail 不写动机式“首次到访”。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from gacore.langTrack import etl, storage
from gacore.langTrack import report as rpt

_TZ = datetime.timezone(datetime.timedelta(hours=8))
DAY = "2026-08-17"
BASE = int(datetime.datetime(2026, 8, 17, 8, 0, tzinfo=_TZ).timestamp() * 1000)
HOME_GK = "31.992,118.783"
WORK_GK = "31.998,118.790"
FOOD_GK = "31.995,118.785"
MED_GK = "31.996,118.786"


def _ts(day: str, hh: int, mm: int) -> int:
    y, m, d = (int(x) for x in day.split("-"))
    dt = datetime.datetime(y, m, d, hh, mm, tzinfo=_TZ)
    return int(dt.timestamp() * 1000)


def _mk_db(path: Path) -> None:
    """最小库：daily_stats / sessions / events(location+notification) / places / stays。

    places 含：家、公司（label 确认）、餐饮（poi_type=餐饮服务）、医疗（医疗保健服务）。
    stays 含一个公司停留段；events 最后 GPS 定位落在 DAY 23:00（早于当前 2026-09-03）。
    """
    conn = sqlite3.connect(path)
    conn.executescript(storage._SCHEMA)
    conn.executescript(etl._SCHEMA)
    conn.execute(
        "INSERT INTO daily_stats(device_id, day, total_screen_ms, unlock_count, "
        "switch_count, notification_count, notification_clicked, screen_on_count, "
        "app_ranking_json, top_notification_apps_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("dev1", DAY, 3600000, 10, 20, 2, 1, 5,
         json.dumps([{"app": "微信", "ms": 3600000}]), "[]"),
    )
    conn.execute(
        "INSERT INTO sessions(device_id, day, pkg, app, activity, start_ms, end_ms, "
        "duration_ms) VALUES (?,?,?,?,?,?,?,?)",
        ("dev1", DAY, "pkg.wechat", "微信", None, _ts(DAY, 9, 0), _ts(DAY, 9, 30), 1800000),
    )
    # 事件：一条通知 + 三条 GPS 定位（最后一条 DAY 23:00，早于当前）
    conn.executemany(
        "INSERT INTO events(device_id, ts, type, payload, received_at) VALUES (?,?,?,?,?)",
        [
            ("dev1", _ts(DAY, 10, 0), "notification", json.dumps({"app": "wechat"}), _ts(DAY, 10, 0)),
            ("dev1", _ts(DAY, 9, 0), "location",
             json.dumps({"lat": 31.99201, "lon": 118.78301, "acc": 30, "provider": "gps"}), _ts(DAY, 9, 0)),
            ("dev1", _ts(DAY, 18, 0), "location",
             json.dumps({"lat": 31.99001, "lon": 118.78201, "acc": 30, "provider": "gps"}), _ts(DAY, 18, 0)),
            ("dev1", _ts(DAY, 23, 0), "location",
             json.dumps({"lat": 31.98901, "lon": 118.78101, "acc": 30, "provider": "gps"}), _ts(DAY, 23, 0)),
        ],
    )
    places = [
        ("dev1", HOME_GK, 31.992, 118.783, "家", "", "", "", "", "", 8, 1),
        ("dev1", WORK_GK, 31.998, 118.790, "公司", "", "", "", "", "", 9, 1),
        ("dev1", FOOD_GK, 31.995, 118.785, "未知", "沙县小吃", "餐饮服务", "用餐",
         "餐饮服务;中餐厅", "地址街1号", 5, 0),
        ("dev1", MED_GK, 31.996, 118.786, "未知", "市人民医院", "医疗保健服务", "就医",
         "医疗保健服务;综合医院", "地址街2号", 3, 0),
    ]
    for p in places:
        # 列序：dev, gk, lat, lon, label, poi, poi_type, behavior, poi_fallback,
        #       address, visit_count, is_primary
        conn.execute(
            "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, "
            "last_seen, visit_count, is_primary, poi, poi_type, behavior, "
            "poi_fallback, address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p[0], p[1], p[2], p[3], p[4], BASE, BASE + 86400000, p[10],
             p[11], p[5], p[6], p[7], p[8], p[9]),
        )
    # 公司停留段（上午 9:30-12:00）
    conn.execute(
        "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
        "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, "
        "grid_key, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", _ts(DAY, 9, 30), _ts(DAY, 12, 0), 9000000, 31.998, 118.790,
         31.998, 118.790, 31.998, 118.790, 9, 15.0, WORK_GK, DAY),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def isolated_db_path(monkeypatch, tmp_path):
    monkeypatch.setattr(rpt, "DB_PATH", tmp_path / "langTrack.db")
    return tmp_path / "lt.db"


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    """天气外呼替换为桩（不出网）；persona 走真实调用（P1-3 去除空转桩）。"""
    from gacore.langTrack import weather
    monkeypatch.setattr(weather, "get_weather", lambda day: {})
    return None


class TestReportEvidenceBoundary:
    def test_no_direct_activity_claim(self, isolated_db_path, capsys):
        """餐饮/医疗 POI 不输出“用餐/就医”；公司停留不输出“上班”；无“通勤稳定”。"""
        _mk_db(isolated_db_path)
        conn = sqlite3.connect(isolated_db_path)
        try:
            rpt.report(conn, DAY, device_id="dev1")
        finally:
            conn.close()
        out = capsys.readouterr().out
        # 确定性活动动词不得出现
        assert "用餐" not in out
        assert "就医" not in out
        assert "上班" not in out
        assert "通勤稳定" not in out
        # P1-3：persona 走真实构建（不再空转），画像区块须实打印，且不引入禁区词
        assert "■ 人物画像" in out
        assert "近 7 日无足够数据生成画像" not in out
        assert "上班" not in out and "通勤稳定" not in out
        # 场景分布 / 消费画像使用 PlaceRef 安全文案：真名/地址或〔tag〕，无坐标、无 grid_key
        # （v1 库无 name_confidence → resolve_place_name 以 address 兜底，属设计行为）
        assert "地址街1号" in out
        assert "地址街2号" in out
        assert "〔家〕" in out or "〔公司〕" in out or "家" in out
        for gk in (HOME_GK, WORK_GK, FOOD_GK, MED_GK):
            assert gk not in out
        # 餐饮/医疗附近"停留"的中性场景表述应出现
        assert "餐饮" in out and "医疗" in out

    def test_last_fix_before_now_prints_recorded_asof(self, isolated_db_path, capsys):
        """最后定位早于 now_override → 只输出“最后记录到”，不冒充“此刻”（P2-3 注入）。"""
        _mk_db(isolated_db_path)
        conn = sqlite3.connect(isolated_db_path)
        try:
            rpt.report(conn, DAY, device_id="dev1",
                       now_override=datetime.datetime(2026, 8, 18, 12, 0, tzinfo=_TZ))
        finally:
            conn.close()
        out = capsys.readouterr().out
        assert "最后定位记录到" in out
        assert "早于当前" in out

    def test_last_fix_near_now_prints_approximate(self, isolated_db_path, capsys):
        """最后定位与 now_override 同时刻 → 走“时间近似当前”分支（P2-3 注入）。"""
        _mk_db(isolated_db_path)
        conn = sqlite3.connect(isolated_db_path)
        try:
            # 最后定位 DAY 23:00，now_override 同刻 → 不早于覆盖时刻
            rpt.report(conn, DAY, device_id="dev1",
                       now_override=datetime.datetime(2026, 8, 17, 23, 0, tzinfo=_TZ))
        finally:
            conn.close()
        out = capsys.readouterr().out
        assert "时间近似当前" in out
        assert "早于当前" not in out
        assert "当前正在" not in out


class TestNewPlaceEvidence:
    def test_new_place_v1_detail_records_points_no_motive(self):
        """v1 库 visit_count 即原始定位点数：detail 写“记录到 N 点”，不伪称段数、不写动机。"""
        conn = sqlite3.connect(":memory:")
        conn.executescript(etl._SCHEMA)  # user_version=0 → v1
        now_ms = int(datetime.datetime(2026, 9, 3, 12, 0, tzinfo=_TZ).timestamp() * 1000)
        # 新地点：first_seen 落在回看窗口内，visit_count=2（v1 即定位点数）
        conn.execute(
            "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, "
            "last_seen, visit_count, is_primary) VALUES (?,?,?,?,?,?,?,?,?)",
            ("dev1", "31.9,118.9", 31.9, 118.9, "未知", now_ms - 1, now_ms, 2, 0),
        )
        conn.commit()
        try:
            etl.detect_anomalies(conn, lookback_days=7)
            rows = conn.execute(
                "SELECT kind, detail FROM anomalies WHERE kind='new_place'"
            ).fetchall()
            assert len(rows) >= 1
            for kind, detail in rows:
                assert "首次到访" not in detail
                # v1 无 stay 段数概念：只能如实写点数，严禁把点数伪称成“N 段”
                assert "记录到 2 点" in detail
                assert "（停留" not in detail
        finally:
            conn.close()

    def test_new_place_v2_detail_uses_stay_episodes_no_motive(self):
        """v2 库 visit_count 即 stay 段数（visit_episodes 口径）：detail 写“停留 N 段”。"""
        conn = sqlite3.connect(":memory:")
        conn.executescript(etl._SCHEMA)
        conn.execute("PRAGMA user_version=2")  # 进入 v2 分支
        # v2 places 表补充列（迁移后同款结构）：place_id / point_count / anomalies.place_id
        conn.execute("ALTER TABLE places ADD COLUMN place_id TEXT")
        conn.execute("ALTER TABLE places ADD COLUMN point_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE anomalies ADD COLUMN place_id TEXT")
        now_ms = int(datetime.datetime(2026, 9, 3, 12, 0, tzinfo=_TZ).timestamp() * 1000)
        # 新地点：point_count=2（原始点数为 2 ≤3），visit_count=2（stay 段数）
        conn.execute(
            "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, first_seen, "
            "last_seen, visit_count, point_count, is_primary) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("dev1", "plc_new", "31.9,118.9", 31.9, 118.9, "未知",
             now_ms - 1, now_ms, 2, 2, 0),
        )
        conn.commit()
        try:
            etl.detect_anomalies(conn, lookback_days=7)
            rows = conn.execute(
                "SELECT kind, detail FROM anomalies WHERE kind='new_place'"
            ).fetchall()
            assert len(rows) >= 1
            for kind, detail in rows:
                assert "首次到访" not in detail
                # v2 以停留段数（visit_episodes）口径展示，不写“访问 N 次”旧口径
                assert "停留 2 段" in detail
        finally:
            conn.close()
