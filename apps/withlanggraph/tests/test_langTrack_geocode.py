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
    # 说明：故意不带 aois / distance，既覆盖"无 AOI 走 POI"的老路径，
    # 也覆盖"高德未返回 distance"的兜底（_nearest 取首个）
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


# ---------------------------------------------------------------------------
# 选名规则（v3.6）：点落在 AOI 院内取 AOI，否则取最近 POI
# ---------------------------------------------------------------------------


def _rc(pois=None, aois=None, roads=None) -> dict:
    """按需构造 regeocode 片段（带 distance/location 的真实结构）。"""
    return {
        "formatted_address": "南京市某路1号",
        "addressComponent": {"district": "测试区", "township": "测试街道"},
        "pois": pois or [],
        "aois": aois or [],
        "roads": roads or [],
        "businessAreas": [],
    }


class TestPickNameRule:
    def test_aoi_inside_wins_over_nearer_poi(self):
        """家在「观云润府」院内（AOI 0m），隔壁小区 POI 142m → 取 AOI 名。"""
        info = geocode._parse_regeocode(_rc(
            pois=[{"name": "彰泰·观南府", "type": "商务住宅;住宅区;住宅小区",
                   "distance": "142", "location": "118.737433,31.955167"}],
            aois=[{"name": "观云润府", "type": "商务住宅;住宅区;住宅小区",
                   "distance": "0", "location": "118.736086,31.952979"}],
        ))
        assert info["poi"] == "观云润府"
        assert info["matched_level"] == "AOI"
        assert info["name_confidence"] == 0.65
        assert info["name_evidence"] == "regeo:AOI"
        assert info["poi_distance_m"] == 0.0
        # AOI 类型并入 type 首段，三级语义按 AOI 大类解析（行为语义在编码时另算）
        assert info["poi_type"].startswith("AOI;")
        assert info["poi_l1"] == "AOI" and info["poi_l2"] == "商务住宅"

    def test_nearer_poi_kept_when_aoi_far(self):
        """POI 30m、AOI 200m → 仍取 POI（AOI 没把点包住）。"""
        info = geocode._parse_regeocode(_rc(
            pois=[{"name": "某大厦", "type": "商务住宅;楼宇;商务写字楼", "distance": "30"}],
            aois=[{"name": "远处院区", "type": "商务住宅;住宅区;住宅小区", "distance": "200"}],
        ))
        assert info["poi"] == "某大厦" and info["matched_level"] == "POI"

    def test_aoi_used_when_no_poi(self):
        info = geocode._parse_regeocode(_rc(
            aois=[{"name": "某院区", "type": "商务住宅;住宅区;住宅小区", "distance": "12"}],
        ))
        assert info["poi"] == "某院区" and info["matched_level"] == "AOI"

    def test_road_and_district_levels_kept(self):
        info = geocode._parse_regeocode(_rc(roads=[{"name": "某路"}]))
        assert info["matched_level"] == "道路" and info["poi"] == ""
        info2 = geocode._parse_regeocode(_rc())
        assert info2["matched_level"] == "行政区"

    def test_missing_distance_falls_back_to_first_and_keeps_poi(self):
        """高德不返回 distance 时仍取 pois[0]（否则会丢名、退化到 around 多打一次）。"""
        info = geocode._parse_regeocode(_rc(
            pois=[{"name": "首个POI", "type": "餐饮服务;中餐厅;中餐馆"}],
        ))
        assert info["poi"] == "首个POI" and info["matched_level"] == "POI"
        assert info["poi_distance_m"] is None

    def test_candidates_recorded_for_editor(self):
        info = geocode._parse_regeocode(_rc(
            pois=[{"name": "P1", "distance": "10"}, {"name": "P2", "distance": "20"}],
            aois=[{"name": "A1", "distance": "0"}],
        ))
        got = [(c["name"], c["kind"], c["distance_m"]) for c in info["poi_candidates"]]
        assert got == [("P1", "POI", 10.0), ("P2", "POI", 20.0), ("A1", "AOI", 0.0)]
        assert info["poi"] == "A1"


class TestCandidateQueries:
    def test_regeo_candidates_sorted_and_distances_recomputed(
        self, fake_urlopen, monkeypatch
    ):
        body = {
            "status": "1",
            "regeocode": {
                "formatted_address": "x",
                "addressComponent": {},
                "pois": [{"name": "远POI", "distance": "300",
                          "location": "118.7900,31.9800"}],
                "aois": [{"name": "近AOI", "distance": "5",
                          "location": "118.7801,31.9800"}],
            },
        }
        monkeypatch.setattr(
            geocode.urllib.request, "urlopen", lambda url, timeout=None: _FakeResp(body)
        )
        out = geocode.regeo_candidates(31.98, 118.78, "k", coord_system="gcj02")
        assert [c["name"] for c in out] == ["近AOI", "远POI"]
        assert out[0]["distance_m"] < out[1]["distance_m"]

    def test_search_place_candidates_sorted_by_distance(self, fake_urlopen, monkeypatch):
        body = {
            "status": "1",
            "pois": [
                {"name": "甲", "location": "118.8000,31.9800", "distance": ""},
                {"name": "乙", "location": "118.7801,31.9800", "distance": ""},
            ],
        }
        monkeypatch.setattr(
            geocode.urllib.request, "urlopen", lambda url, timeout=None: _FakeResp(body)
        )
        out = geocode.search_place_candidates("小区", 31.98, 118.78, "k")
        assert [c["name"] for c in out] == ["乙", "甲"]
        assert all(c["source"] == "search" for c in out)

    def test_search_empty_keyword_returns_empty(self, fake_urlopen):
        assert geocode.search_place_candidates("", 31.98, 118.78, "k") == []


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
