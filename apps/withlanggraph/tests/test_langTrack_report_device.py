"""test_langTrack_report_device.py —— Task 5d report 多设备隔离测试。

覆盖（计划 Task 5d 清单：report 多设备时要求选择 device，禁止合并）：
- devices_of_day：daily_stats / sessions / stays / anomalies 并集，最小 schema 容错；
- resolve_report_device：单设备自动采用；多设备未指定抛 MultiDeviceError；
  显式指定原样生效；
- report()：多设备未指定 → MultiDeviceError；指定后屏幕时间 / 通知高峰 /
  sessions 明细只含该设备；单设备库行为不变（自动采用，无需 --device）；
- 画像快照：多设备当日文件名带设备段不互相覆盖，单设备保持既有命名，
  JSON 含 device_id 字段；
- persona 收到 device_id；
- CLI main()：多设备缺 --device 退出码 2；--list-devices 列出当日设备。
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


def _ts(hh: int, mm: int) -> int:
    d = datetime.datetime(2026, 8, 17, hh, mm, tzinfo=_TZ)
    return int(d.timestamp() * 1000)


@pytest.fixture
def isolated_db_path(monkeypatch, tmp_path):
    """快照输出目录指向 tmp，不污染仓库 data/profiles。"""
    monkeypatch.setattr(rpt, "DB_PATH", tmp_path / "langTrack.db")
    return tmp_path / "lt.db"


def _mk_db(path: Path, devices: list[str]) -> None:
    """最小多设备库：daily_stats / sessions / places / stays / events / anomalies。"""
    conn = sqlite3.connect(path)
    conn.executescript(storage._SCHEMA)
    conn.executescript(etl._SCHEMA)
    for i, dev in enumerate(devices):
        screen = (i + 1) * 3600000  # dev1=1h, dev2=2h ...
        conn.execute(
            "INSERT INTO daily_stats(device_id, day, total_screen_ms, unlock_count, "
            "switch_count, notification_count, notification_clicked, screen_on_count, "
            "app_ranking_json, top_notification_apps_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (dev, DAY, screen, 10 + i, 20 + i, 0, 0, 5,
             json.dumps([{"app": f"app_{dev}", "ms": screen}]), "[]"),
        )
        conn.execute(
            "INSERT INTO sessions(device_id, day, pkg, app, activity, start_ms, end_ms, "
            "duration_ms) VALUES (?,?,?,?,?,?,?,?)",
            (dev, DAY, f"pkg.{dev}", f"app_{dev}", None, _ts(9 + i, 0),
             _ts(9 + i, 30), 1800000),
        )
        conn.execute(
            "INSERT INTO events(device_id, ts, type, payload, received_at) VALUES (?,?,?,?,?)",
            (dev, _ts(10 + i, 0), "notification",
             json.dumps({"app": "wechat"}), _ts(10 + i, 0)),
        )
        conn.execute(
            "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, "
            "last_seen, visit_count, is_primary) VALUES (?,?,?,?,?,?,?,?,?)",
            (dev, HOME_GK if i == 0 else WORK_GK, 31.99, 118.78, "未知", BASE, BASE,
             5, 1),
        )
        conn.execute(
            "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
            "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, "
            "grid_key, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dev, _ts(20, 0), _ts(23, 0), 10800000, 31.99, 118.78,
             31.99, 118.78, 31.99, 118.78, 5, 10.0, HOME_GK, DAY),
        )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """天气外呼与 persona 构建替换为桩：单测不出网、不依赖真实画像逻辑。"""
    from gacore.langTrack import weather
    monkeypatch.setattr(weather, "get_weather", lambda day: {})
    captured: dict = {}

    def fake_persona(conn=None, device_id=None, days=7, db_path=None):
        captured["device_id"] = device_id
        captured["days"] = days
        return {"available": False}

    monkeypatch.setattr(rpt, "build_persona", fake_persona)
    return captured


# ---------------------------------------------------------------------------
# devices_of_day / resolve_report_device
# ---------------------------------------------------------------------------

class TestResolveDevice:
    def test_single_device_auto(self, isolated_db_path):
        _mk_db(isolated_db_path, ["dev1"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            assert rpt.devices_of_day(conn, DAY) == ["dev1"]
            assert rpt.resolve_report_device(conn, DAY, None) == "dev1"
        finally:
            conn.close()

    def test_multi_device_requires_choice(self, isolated_db_path):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            assert rpt.devices_of_day(conn, DAY) == ["dev1", "dev2"]
            with pytest.raises(rpt.MultiDeviceError) as ei:
                rpt.resolve_report_device(conn, DAY, None)
            assert "dev1" in str(ei.value) and "dev2" in str(ei.value)
        finally:
            conn.close()

    def test_explicit_device_wins(self, isolated_db_path):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            assert rpt.resolve_report_device(conn, DAY, "dev2") == "dev2"
        finally:
            conn.close()

    def test_no_data_returns_none(self, isolated_db_path):
        _mk_db(isolated_db_path, ["dev1"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            assert rpt.resolve_report_device(conn, "2020-01-01", None) is None
        finally:
            conn.close()

    def test_minimal_schema_empty(self, tmp_path):
        """缺表/缺列的最小 schema 不抛错，返回空列表。"""
        p = tmp_path / "min.db"
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE daily_stats (day TEXT, total_screen_ms INTEGER)")
        conn.execute("INSERT INTO daily_stats VALUES ('2026-08-17', 1)")
        conn.commit()
        try:
            assert rpt.devices_of_day(conn, DAY) == []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# report() 设备隔离
# ---------------------------------------------------------------------------

class TestReportDeviceIsolation:
    def test_multi_device_without_choice_raises(self, isolated_db_path, capsys):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            with pytest.raises(rpt.MultiDeviceError):
                rpt.report(conn, DAY)
        finally:
            conn.close()

    def test_screen_time_only_selected_device(self, isolated_db_path, capsys):
        """指定 dev1：总屏幕时间为 dev1 的 1 小时，不含 dev2 的 2 小时。"""
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            rpt.report(conn, DAY, device_id="dev1")
        finally:
            conn.close()
        out = capsys.readouterr().out
        assert "1小时0分" in out
        assert "2小时0分" not in out
        assert "设备 dev1" in out

    def test_notification_peak_not_merged(self, isolated_db_path, capsys):
        """dev1 通知在 10 点、dev2 在 11 点：选 dev1 时高峰为 10 点。"""
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            rpt.report(conn, DAY, device_id="dev1")
        finally:
            conn.close()
        out = capsys.readouterr().out
        assert "通知高峰: 10:00" in out
        assert "通知高峰: 11:00" not in out

    def test_single_device_autodetect_unchanged(self, isolated_db_path, capsys):
        """单设备库无需 --device，自动采用唯一设备。"""
        _mk_db(isolated_db_path, ["dev1"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            rpt.report(conn, DAY)
        finally:
            conn.close()
        out = capsys.readouterr().out
        assert "设备 dev1" in out
        assert "1小时0分" in out

    def test_persona_receives_device_id(self, isolated_db_path, capsys, _no_network):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            rpt.report(conn, DAY, device_id="dev2")
        finally:
            conn.close()
        capsys.readouterr()
        assert _no_network["device_id"] == "dev2"
        assert _no_network["days"] == 7


# ---------------------------------------------------------------------------
# 快照文件名 / 内容
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_multi_device_files_not_overwritten(self, isolated_db_path, capsys):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        for dev in ("dev1", "dev2"):
            conn = sqlite3.connect(isolated_db_path)
            try:
                rpt.report(conn, DAY, device_id=dev)
            finally:
                conn.close()
            capsys.readouterr()
        profiles = isolated_db_path.parent / "profiles"
        names = sorted(p.name for p in profiles.glob("*.json"))
        assert names == [
            f"langTrack_profile_{DAY}_dev1.json",
            f"langTrack_profile_{DAY}_dev2.json",
        ]

    def test_single_device_keeps_legacy_name(self, isolated_db_path, capsys):
        _mk_db(isolated_db_path, ["dev1"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            rpt.report(conn, DAY)
        finally:
            conn.close()
        capsys.readouterr()
        profiles = isolated_db_path.parent / "profiles"
        names = [p.name for p in profiles.glob("*.json")]
        assert names == [f"langTrack_profile_{DAY}.json"]

    def test_profile_json_contains_device(self, isolated_db_path, capsys):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        conn = sqlite3.connect(isolated_db_path)
        try:
            rpt.report(conn, DAY, device_id="dev1")
        finally:
            conn.close()
        capsys.readouterr()
        p = isolated_db_path.parent / "profiles" / f"langTrack_profile_{DAY}_dev1.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["device_id"] == "dev1"
        assert data["date"] == DAY
        assert data["screen"]["total_screen_ms"] == 3600000


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_multi_device_missing_device_exit_2(self, isolated_db_path, monkeypatch, capsys):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        monkeypatch.setattr(
            sys, "argv",
            ["report", "--db", str(isolated_db_path), "--day", DAY],
        )
        with pytest.raises(SystemExit) as ei:
            rpt.main()
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "--device" in err

    def test_list_devices(self, isolated_db_path, monkeypatch, capsys):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        monkeypatch.setattr(
            sys, "argv",
            ["report", "--db", str(isolated_db_path), "--day", DAY, "--list-devices"],
        )
        rpt.main()
        out = capsys.readouterr().out
        assert "dev1" in out and "dev2" in out

    def test_device_flag_runs_report(self, isolated_db_path, monkeypatch, capsys):
        _mk_db(isolated_db_path, ["dev1", "dev2"])
        monkeypatch.setattr(
            sys, "argv",
            ["report", "--db", str(isolated_db_path), "--day", DAY, "--device", "dev2"],
        )
        rpt.main()
        out = capsys.readouterr().out
        assert "设备 dev2" in out
        assert "2小时0分" in out
