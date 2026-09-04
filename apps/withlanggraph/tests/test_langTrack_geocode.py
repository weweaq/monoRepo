"""test_langTrack_geocode.py —— Task 6 高德入口坐标边界测试（§3.3）。

覆盖：
- _regeo_one / _regeo_request / around_search：最终 HTTP 请求 URL 中的坐标按
  coord_system 经 to_amap_coord 转换（wgs84→GCJ02；gcj02/unknown 原样）；
- unknown 坐标制警告模块级只提示一次；
- incremental_encode 按 (device_id, first_seen) 解析坐标制并按组调用，
  混合坐标系不共用一个转换参数；坐标制配置错误拒绝编码；
- enrich_business_area 逐点解析坐标制后请求。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from gacore.langTrack import etl, etl_config, geocode
from gacore.langTrack import location_facts as lf
from gacore.langTrack.etl_config import CoordSystemConfigError


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _regeo_body(n_points: int) -> dict:
    """构造 n 点成功响应（每点一个带 POI 的 regeocode，绕过 around 兜底）。

    同时带单点 regeocode 与批量 regeocodes：兼容 _regeo_one（读 regeocode）
    与 _regeo_request（读 regeocodes 列表）两种结构。
    """
    rc = {
        "formatted_address": "南京市测试路1号",
        "addressComponent": {"province": "江苏省", "city": "南京市", "district": "测试区",
                             "township": "测试街道"},
        "pois": [{"name": "测试POI", "type": "餐饮服务;中餐厅;中餐馆"}],
        "businessAreas": [{"name": "测试商圈"}],
    }
    rcs = [dict(rc) for _ in range(n_points)]
    return {"status": "1", "regeocode": rcs[0], "regeocodes": rcs}


def _around_body() -> dict:
    return {
        "status": "1",
        "pois": [{"name": "测试POI", "type": "餐饮服务;中餐厅;中餐馆",
                  "business_area": "测试商圈"}],
    }


@pytest.fixture
def fake_urlopen(monkeypatch):
    """捕获全部请求 URL；按接口类型与 location 点数返回匹配的成功响应。"""
    urls: list[str] = []

    def _open(url: str, timeout=None):
        urls.append(url)
        if url.startswith(geocode.AROUND_URL):
            return _FakeResp(_around_body())
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        n = len((qs.get("location") or [""])[0].split("|"))
        return _FakeResp(_regeo_body(n))

    monkeypatch.setattr(geocode.urllib.request, "urlopen", _open)
    monkeypatch.setattr(geocode, "_SUPPORT_BATCH", True)  # 跳过批量探测请求
    monkeypatch.setenv("AMAP_KEY", "test-key")
    return urls


def _location_coords(url: str) -> list[tuple[float, float]]:
    """从请求 URL 解析 location 参数为 [(lat, lon), ...]（高德序 lon,lat）。"""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    loc = (qs.get("location") or [""])[0]
    out = []
    for token in loc.split("|"):
        lon_s, lat_s = token.split(",")
        out.append((float(lat_s), float(lon_s)))
    return out


# ---------------------------------------------------------------------------
# 入口 URL 坐标转换
# ---------------------------------------------------------------------------

class TestRegeoUrlCoords:
    def test_regeo_one_wgs84_converted(self, fake_urlopen):
        out = geocode._regeo_one(31.98, 118.78, "k", coord_system="wgs84")
        assert out is not None and out["formatted"]
        expect = lf.to_amap_coord(31.98, 118.78, "wgs84")
        assert _location_coords(fake_urlopen[0]) == [expect]

    def test_regeo_one_gcj02_passthrough(self, fake_urlopen):
        geocode._regeo_one(31.98, 118.78, "k", coord_system="gcj02")
        assert _location_coords(fake_urlopen[0]) == [(31.98, 118.78)]

    def test_regeo_request_batch_converts_each_point(self, fake_urlopen):
        pts = [(31.98, 118.78), (31.99, 118.79)]
        out, failed = geocode._regeo_request(pts, "k", coord_system="wgs84")
        assert not failed and len(out) == 2
        expect = [lf.to_amap_coord(lat, lon, "wgs84") for lat, lon in pts]
        assert _location_coords(fake_urlopen[0]) == expect


class TestAroundUrlCoords:
    def test_around_wgs84_converted(self, fake_urlopen):
        got = geocode.around_search(31.98, 118.78, "k", coord_system="wgs84")
        assert got is not None
        expect = lf.to_amap_coord(31.98, 118.78, "wgs84")
        assert _location_coords(fake_urlopen[0]) == [expect]

    def test_around_gcj02_passthrough(self, fake_urlopen):
        geocode.around_search(31.98, 118.78, "k", coord_system="gcj02")
        assert _location_coords(fake_urlopen[0]) == [(31.98, 118.78)]


class TestUnknownWarning:
    def test_unknown_warns_once_per_module(self, fake_urlopen, monkeypatch, capsys):
        monkeypatch.setattr(lf, "_unknown_coord_warned_scopes", set())
        geocode._regeo_one(31.98, 118.78, "k", coord_system="unknown")
        geocode._regeo_one(31.99, 118.79, "k", coord_system="unknown")
        out = capsys.readouterr().out
        assert out.count("坐标制为 unknown") == 1
        # 坐标仍原样放行
        assert _location_coords(fake_urlopen[0]) == [(31.98, 118.78)]

    def test_known_system_no_warning(self, fake_urlopen, monkeypatch, capsys):
        monkeypatch.setattr(lf, "_unknown_coord_warned_scopes", set())
        geocode._regeo_one(31.98, 118.78, "k", coord_system="gcj02")
        assert "坐标制为 unknown" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# incremental_encode / enrich_business_area（调用方按点位解析坐标制）
# ---------------------------------------------------------------------------

_COORD_CFG = {
    "default": "gcj02",
    "periods": [
        {"device_id": "dev1", "start_ts": 0, "end_ts": None, "source": "wgs84"},
    ],
}


@pytest.fixture
def places_db(tmp_path):
    path = tmp_path / "lt.db"
    conn = sqlite3.connect(path)
    conn.executescript(etl._SCHEMA)
    conn.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, last_seen, "
        "visit_count, is_primary) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", "31.980,118.780", 31.980, 118.780, "未知", 2000, 2000, 3, 0),
            ("dev2", "31.990,118.790", 31.990, 118.790, "未知", 2000, 2000, 2, 0),
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def coord_cfg(monkeypatch):
    monkeypatch.setattr(etl_config, "load_coord_systems", lambda: _COORD_CFG)
    return _COORD_CFG


class TestIncrementalEncode:
    def test_groups_by_coord_system(self, fake_urlopen, places_db, coord_cfg, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda *_: None)
        n = geocode.incremental_encode(places_db)
        assert n == 2
        # dev1（wgs84）与 dev2（gcj02）分两组请求；每组一个点
        assert len(fake_urlopen) == 2
        got = {c for url in fake_urlopen for c in _location_coords(url)}
        expect = {
            lf.to_amap_coord(31.980, 118.780, "wgs84"),
            (31.990, 118.790),
        }
        assert got == expect
        conn = sqlite3.connect(places_db)
        rows = conn.execute(
            "SELECT device_id, geocoded_at IS NOT NULL FROM places ORDER BY device_id"
        ).fetchall()
        conn.close()
        assert all(done for _, done in rows)

    def test_bad_coord_config_rejected(self, fake_urlopen, places_db, monkeypatch):
        def _raise():
            raise CoordSystemConfigError("overlapping periods for device 'dev1'")

        monkeypatch.setattr(etl_config, "load_coord_systems", _raise)
        with pytest.raises(CoordSystemConfigError):
            geocode.incremental_encode(places_db)
        assert fake_urlopen == []  # 配置错误时零请求


class TestEnrichBusinessArea:
    def test_resolves_per_place(self, fake_urlopen, places_db, coord_cfg, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda *_: None)
        # 已编码 + business_area 空 + 行为非住宅/办公 → 两个点都触发
        conn = sqlite3.connect(places_db)
        conn.execute(
            "UPDATE places SET geocoded_at=1, business_area='', poi='x', behavior='用餐'"
        )
        conn.commit()
        conn.close()
        geocode.enrich_business_area(places_db)
        got = {c for url in fake_urlopen for c in _location_coords(url)}
        expect = {
            lf.to_amap_coord(31.980, 118.780, "wgs84"),
            (31.990, 118.790),
        }
        assert got == expect
