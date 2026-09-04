"""test_langTrack_routes.py —— Task 6 routes 坐标边界与 build_trips 参数化测试（§3.1/§3.3）。

覆盖：
- build_trips：LocationPoint 输入、阈值参数真正生效（min_duration/min_dist/max_infer_gap）；
- direction_polyline：origin/destination 按 coord_system 经 to_amap_coord 转换
  （wgs84→GCJ02；gcj02/unknown 原样）、unknown 警告模块级只提示一次、返回点序 (lat, lon)；
- incremental_encode_trips：成功标记 polyline_coord_system='gcj02'、按设备解析坐标制、
  配置错误拒绝补路、失败段不标记留待重试；
- encode_belt_pois：网格坐标来自高德 polyline 已是 GCJ02，原样放行（禁止二次偏移）。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from gacore.langTrack import etl, etl_config, routes
from gacore.langTrack import location_facts as lf
from gacore.langTrack.etl_config import CoordSystemConfigError
from gacore.langTrack.location_facts import LocationPoint
from gacore.langTrack.location_facts import to_amap_coord as _conv

_TZ = datetime.timezone(datetime.timedelta(hours=8))
BASE = int(datetime.datetime(2026, 8, 17, 8, 0, tzinfo=_TZ).timestamp() * 1000)
HOME = (31.992, 118.783)
WORK = (31.998, 118.790)


def _pt(device_id: str, ts: int, lat: float, lon: float) -> LocationPoint:
    return LocationPoint(device_id, ts, lat, lon, None, "gps", "unknown")


def _stay(device_id: str, start_ts: int, end_ts: int, lat: float, lon: float) -> tuple:
    """build_stays 产物同构 tuple：14 列（center 在 4/5 位）。"""
    gk = f"{round(lat * 1000) / 1000:.3f},{round(lon * 1000) / 1000:.3f}"
    return (device_id, start_ts, end_ts, end_ts - start_ts,
            lat, lon, lat, lon, lat, lon, 10, 30.0, gk, "2026-08-17")


# ---------------------------------------------------------------------------
# build_trips（§3.1 参数化）
# ---------------------------------------------------------------------------

class TestBuildTrips:
    def test_basic_trip_with_gap_points(self):
        points = [
            _pt("dev1", BASE + 3_900_000, 31.9935, 118.785),
            _pt("dev1", BASE + 4_350_000, 31.995, 118.7865),
            _pt("dev1", BASE + 4_800_000, 31.9965, 118.788),
        ]
        stays = [
            _stay("dev1", BASE, BASE + 3_600_000, *HOME),
            _stay("dev1", BASE + 5_100_000, BASE + 12_600_000, *WORK),
        ]
        trips = routes.build_trips(points, stays)
        assert len(trips) == 1
        t = trips[0]
        assert t[0] == "dev1"
        # 起终点取 gap 内首/末采样点
        assert t[1] == BASE + 3_900_000
        assert t[2] == BASE + 4_800_000
        assert t[3] == 900_000
        assert (t[4], t[5]) == (31.9935, 118.785)
        assert (t[6], t[7]) == (31.9965, 118.788)
        assert t[9] == 3
        assert t[10] == "2026-08-17"

    def test_infer_trip_within_max_gap_uses_stay_centers(self):
        # gap 内无采样点且 gap=1h <= max_infer_gap → 用停留中心推断
        stays = [
            _stay("dev1", BASE, BASE + 3_600_000, *HOME),
            _stay("dev1", BASE + 7_200_000, BASE + 10_800_000, *WORK),
        ]
        trips = routes.build_trips([], stays, max_infer_gap_ms=7_200_000)
        assert len(trips) == 1
        t = trips[0]
        assert (t[4], t[5]) == HOME
        assert (t[6], t[7]) == WORK
        assert t[9] == 0
        assert t[3] == 3_600_000

    def test_skip_infer_beyond_max_gap(self):
        # gap 超过 max_infer_gap（采集空窗）→ 不推断
        stays = [
            _stay("dev1", BASE, BASE + 3_600_000, *HOME),
            _stay("dev1", BASE + 10_800_001, BASE + 14_400_000, *WORK),
        ]
        assert routes.build_trips([], stays, max_infer_gap_ms=7_200_000) == []

    def test_min_duration_param_filters(self):
        # gap 60s 且有采样点：默认阈值放行，抬高 min_duration 后被滤掉
        points = [_pt("dev1", BASE + 20_000, 31.9925, 118.7835),
                  _pt("dev1", BASE + 50_000, 31.9975, 118.7895)]
        stays = [
            _stay("dev1", BASE, BASE + 10_000, *HOME),
            _stay("dev1", BASE + 60_000, BASE + 3_600_000, *WORK),
        ]
        assert len(routes.build_trips(points, stays, min_duration_ms=30_000)) == 1
        assert routes.build_trips(points, stays, min_duration_ms=120_000) == []

    def test_min_dist_param_filters(self):
        # 起终点直距 ~60m：默认 300m 阈值滤掉，放宽到 50m 放行
        points = [_pt("dev1", BASE + 3_900_000, 31.992, 118.783),
                  _pt("dev1", BASE + 4_500_000, 31.9925, 118.7835)]
        stays = [
            _stay("dev1", BASE, BASE + 3_600_000, *HOME),
            _stay("dev1", BASE + 5_100_000, BASE + 8_700_000, *WORK),
        ]
        assert routes.build_trips(points, stays, min_dist_m=300.0) == []
        assert len(routes.build_trips(points, stays, min_dist_m=50.0)) == 1

    def test_devices_isolated(self):
        # 多设备：各自 gap 各自配对，不跨设备拼接
        points = [_pt("dev1", BASE + 3_900_000, 31.9935, 118.785),
                  _pt("dev1", BASE + 4_800_000, 31.9965, 118.788),
                  _pt("dev2", BASE + 3_900_000, 31.9935, 118.785),
                  _pt("dev2", BASE + 4_800_000, 31.9965, 118.788)]
        stays = [
            _stay("dev1", BASE, BASE + 3_600_000, *HOME),
            _stay("dev1", BASE + 5_100_000, BASE + 8_700_000, *WORK),
            _stay("dev2", BASE, BASE + 3_600_000, *HOME),
            _stay("dev2", BASE + 5_100_000, BASE + 8_700_000, *WORK),
        ]
        trips = routes.build_trips(points, stays)
        assert sorted(t[0] for t in trips) == ["dev1", "dev2"]


# ---------------------------------------------------------------------------
# 高德 HTTP 入口（§3.3 统一转换）
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _direction_body() -> dict:
    return {
        "status": "1",
        "route": {"paths": [{"steps": [{"polyline": "118.785,31.977;118.79,31.98"}]}]},
    }


@pytest.fixture
def fake_direction(monkeypatch):
    """捕获全部请求 URL；路径规划一律返回成功双点路线。"""
    urls: list[str] = []

    def _open(url: str, timeout=None):
        urls.append(url)
        return _FakeResp(_direction_body())

    monkeypatch.setattr(routes.urllib.request, "urlopen", _open)
    monkeypatch.setenv("AMAP_KEY", "test-key")
    return urls


def _origin_dest(url: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """解析 direction URL 的 origin/destination 为 ((lat, lon), (lat, lon))。"""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    o_lon, o_lat = qs["origin"][0].split(",")
    d_lon, d_lat = qs["destination"][0].split(",")
    return (float(o_lat), float(o_lon)), (float(d_lat), float(d_lon))


class TestDirectionPolyline:
    def test_wgs84_converted(self, fake_direction):
        pts = routes.direction_polyline(31.98, 118.78, 31.99, 118.79, "k", coord_system="wgs84")
        assert pts == [(31.977, 118.785), (31.98, 118.79)]  # 返回 (lat, lon) 序
        o, d = _origin_dest(fake_direction[0])
        assert o == _conv(31.98, 118.78, "wgs84")
        assert d == _conv(31.99, 118.79, "wgs84")

    def test_gcj02_passthrough(self, fake_direction):
        routes.direction_polyline(31.98, 118.78, 31.99, 118.79, "k", coord_system="gcj02")
        o, d = _origin_dest(fake_direction[0])
        assert o == (31.98, 118.78)
        assert d == (31.99, 118.79)

    def test_unknown_passthrough_and_warns_once(
        self, fake_direction, monkeypatch, capsys
    ):
        monkeypatch.setattr(lf, "_unknown_coord_warned_scopes", set())
        routes.direction_polyline(31.98, 118.78, 31.99, 118.79, "k", coord_system="unknown")
        routes.direction_polyline(31.98, 118.78, 31.99, 118.79, "k", coord_system="unknown")
        out = capsys.readouterr().out
        assert out.count("坐标制为 unknown") == 1
        o, _ = _origin_dest(fake_direction[0])
        assert o == (31.98, 118.78)

    def test_known_system_no_warning(self, fake_direction, monkeypatch, capsys):
        monkeypatch.setattr(lf, "_unknown_coord_warned_scopes", set())
        routes.direction_polyline(31.98, 118.78, 31.99, 118.79, "k", coord_system="wgs84")
        assert "坐标制为 unknown" not in capsys.readouterr().out

    def test_mixed_endpoint_systems(self, fake_direction):
        """跨坐标制时段边界：起终点各自按自己的 coord_system 转换。"""
        routes.direction_polyline(
            31.98, 118.78, 31.99, 118.79, "k",
            coord_system="wgs84", coord_system_end="gcj02",
        )
        o, d = _origin_dest(fake_direction[0])
        assert o == _conv(31.98, 118.78, "wgs84")
        assert d == (31.99, 118.79)  # gcj02 原样，不被起点制二次偏移

    def test_end_system_defaults_to_start(self, fake_direction):
        routes.direction_polyline(31.98, 118.78, 31.99, 118.79, "k", coord_system="wgs84")
        o, d = _origin_dest(fake_direction[0])
        assert o == _conv(31.98, 118.78, "wgs84")
        assert d == _conv(31.99, 118.79, "wgs84")


# ---------------------------------------------------------------------------
# incremental_encode_trips（补路标记）
# ---------------------------------------------------------------------------

_COORD_CFG = {
    "default": "gcj02",
    "periods": [
        {"device_id": "dev1", "start_ts": 0, "end_ts": None, "source": "wgs84"},
    ],
}


@pytest.fixture
def trips_db(tmp_path):
    path = tmp_path / "lt.db"
    conn = sqlite3.connect(path)
    conn.executescript(etl._SCHEMA)
    conn.executemany(
        "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "
        "end_lat, end_lon, dist_m, n_points, day) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", 1000, 2000, 1000, 31.98, 118.78, 31.99, 118.79, 900.0, 2, "2026-08-17"),
            ("dev2", 3000, 4000, 1000, 31.98, 118.78, 31.99, 118.79, 900.0, 2, "2026-08-17"),
        ],
    )
    conn.commit()
    conn.close()
    return path


class TestIncrementalEncodeTrips:
    def test_marks_polyline_coord_system_gcj02(self, fake_direction, trips_db, monkeypatch):
        monkeypatch.setattr(etl_config, "load_coord_systems", lambda: _COORD_CFG)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        n = routes.incremental_encode_trips(trips_db)
        assert n == 2
        conn = sqlite3.connect(trips_db)
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT * FROM trips WHERE route_encoded_at IS NOT NULL"
        ):
            assert r["polyline_coord_system"] == "gcj02"
            assert json.loads(r["polyline"]) == [[31.977, 118.785], [31.98, 118.79]]
            assert r["route_mode"] == "walking"
        conn.close()

    def test_resolves_per_device_coord_system(self, fake_direction, trips_db, monkeypatch):
        monkeypatch.setattr(etl_config, "load_coord_systems", lambda: _COORD_CFG)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        routes.incremental_encode_trips(trips_db)
        # dev1（wgs84）转换、dev2（gcj02）原样
        got = {o for url in fake_direction for o, _ in [_origin_dest(url)]}
        assert got == {_conv(31.98, 118.78, "wgs84"), (31.98, 118.78)}

    def test_bad_coord_config_rejected(self, fake_direction, trips_db, monkeypatch):
        def _raise():
            raise CoordSystemConfigError("overlapping periods for device 'dev1'")

        monkeypatch.setattr(etl_config, "load_coord_systems", _raise)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        with pytest.raises(CoordSystemConfigError):
            routes.incremental_encode_trips(trips_db)
        assert fake_direction == []  # 配置错误时零请求

    def test_failed_request_not_marked(self, trips_db, monkeypatch):
        urls: list[str] = []

        def _open(url: str, timeout=None):
            urls.append(url)
            return _FakeResp({"status": "0", "info": "INVALID_USER_KEY"})

        monkeypatch.setattr(routes.urllib.request, "urlopen", _open)
        monkeypatch.setattr(etl_config, "load_coord_systems", lambda: _COORD_CFG)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        monkeypatch.setenv("AMAP_KEY", "test-key")
        n = routes.incremental_encode_trips(trips_db)
        assert n == 0
        conn = sqlite3.connect(trips_db)
        rows = conn.execute(
            "SELECT COUNT(*) FROM trips WHERE route_encoded_at IS NULL "
            "AND polyline IS NULL AND polyline_coord_system IS NULL"
        ).fetchone()[0]
        conn.close()
        assert rows == 2  # 失败段留待下次重试，不标记


# ---------------------------------------------------------------------------
# encode_belt_pois（网格坐标已是 GCJ02）
# ---------------------------------------------------------------------------

@pytest.fixture
def grids_db(tmp_path):
    path = tmp_path / "lt.db"
    conn = sqlite3.connect(path)
    conn.executescript(etl._SCHEMA)
    conn.execute(
        "INSERT INTO route_grids(device_id, day, grid_lat, grid_lon, n_pass, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("dev1", "2026-08-17", 31.99, 118.79, 3, 1),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def fake_around(monkeypatch):
    urls: list[str] = []

    def _open(url: str, timeout=None):
        urls.append(url)
        return _FakeResp({
            "status": "1",
            "pois": [{"name": "测试POI", "type": "餐饮服务;中餐厅;中餐馆", "distance": "120"}],
        })

    monkeypatch.setattr(routes.urllib.request, "urlopen", _open)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setenv("AMAP_KEY", "test-key")
    return urls


class TestEncodeBeltPois:
    def test_grid_gcj02_passthrough(self, fake_around, grids_db):
        n = routes.encode_belt_pois(grids_db)
        assert n == 1
        # 网格坐标来自高德 polyline 量化（已是 GCJ02）→ 原样放行，不二次偏移
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(fake_around[0]).query)
        lon_s, lat_s = qs["location"][0].split(",")
        assert (float(lat_s), float(lon_s)) == (31.99, 118.79)
        conn = sqlite3.connect(grids_db)
        row = conn.execute(
            "SELECT name, type FROM grid_pois WHERE grid_lat=31.99 AND grid_lon=118.79"
        ).fetchone()
        conn.close()
        assert row == ("测试POI", "餐饮服务;中餐厅;中餐馆")
