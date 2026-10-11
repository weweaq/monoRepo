"""v3.6 人工修正与地点合并测试（封闭：tmp sqlite + 假高德）。

覆盖四层：
1. location_reader：v3.6 列迁移幂等 / 候选池 JSON 解析 / place_correction 读取；
2. location_facts：人工地名优先级（poi_override > 自动；address_override 兜底）、
   effective_coord 坐标修正优先；
3. place_semantics：save_place_correction（写/清空/坐标修正/非法值）、merge_preview、
   merge_places（cells/stays/trips/anomalies 改写 + 统计合并 + 校验拒绝）；
4. review_server：GET /api/places/name_candidates、POST /api/places/correction、
   POST /api/places/merge（含 token 鉴权与参数错误）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.config import Config  # noqa: E402
from gacore.langTrack import place_semantics as ps  # noqa: E402
from gacore.langTrack.location_facts import effective_coord, resolve_place_name  # noqa: E402
from gacore.langTrack.location_reader import (  # noqa: E402
    CORRECTION_COLUMNS,
    ensure_correction_columns,
    parse_candidates,
    place_correction,
    read_places,
)
from gacore.review_server import create_app  # noqa: E402

TOKEN = "test-token"
DEV = "dev1"


# --------------------------------------------------------------------------- fixtures


def _make_db(tmp_path: Path, name: str = "lt.db") -> Path:
    """v2 位置库（places/stays/trips/anomalies/place_cells），未含 v3.6 列。"""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE places (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
          grid_key TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
          label TEXT DEFAULT '未知', first_seen INTEGER, last_seen INTEGER,
          point_count INTEGER DEFAULT 0, visit_count INTEGER DEFAULT 0,
          stay_ms INTEGER DEFAULT 0, is_primary INTEGER DEFAULT 0,
          address TEXT, poi TEXT, poi_fallback TEXT, district TEXT, township TEXT,
          name_confidence REAL DEFAULT 0, name_evidence TEXT, place_id TEXT
        );
        CREATE TABLE place_cells (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, place_id TEXT, grid_key TEXT
        );
        CREATE TABLE stays (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, place_id TEXT, grid_key TEXT,
          start_ts INTEGER, end_ts INTEGER, duration_ms INTEGER,
          center_lat REAL, center_lon REAL, day TEXT
        );
        CREATE TABLE trips (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, from_place_id TEXT,
          to_place_id TEXT, start_ts INTEGER, end_ts INTEGER, day TEXT
        );
        CREATE TABLE anomalies (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, place_id TEXT, day TEXT,
          kind TEXT, ts INTEGER
        );
        """
    )
    conn.execute("PRAGMA user_version = 2")
    # 两个"同名歧义"常驻点：张垛主簇 500m 内 + 张垛小簇
    conn.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, last_seen,"
        "point_count, visit_count, stay_ms, address, poi, district, township,"
        "name_confidence, name_evidence, place_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (DEV, "31.334,118.671", 31.334227, 118.670908, "张威的老家", 1000, 9000,
             345, 5, 172434492, "安徽省马鞍山市当涂县乌溪镇张垛", "张垛", "当涂县", "乌溪镇",
             0.8, "regeo:POI", "p_keep"),
            (DEV, "31.334,118.673", 31.333896, 118.673215, "张威的老家", 8000, 9500,
             4, 1, 767744, "安徽省马鞍山市当涂县乌溪镇张垛", "张垛", "当涂县", "乌溪镇",
             0.8, "regeo:POI", "p_drop"),
        ],
    )
    conn.executemany(
        "INSERT INTO place_cells(device_id, place_id, grid_key) VALUES (?,?,?)",
        [(DEV, "p_keep", "31.334,118.671"), (DEV, "p_drop", "31.334,118.673")],
    )
    conn.executemany(
        "INSERT INTO stays(device_id, place_id, grid_key, start_ts, end_ts, duration_ms,"
        "center_lat, center_lon, day) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (DEV, "p_keep", "31.334,118.671", 1000, 5000, 4000, 31.3342, 118.6709, "2026-10-03"),
            (DEV, "p_keep", "31.334,118.671", 6000, 9000, 3000, 31.3342, 118.6709, "2026-10-04"),
            (DEV, "p_drop", "31.334,118.673", 9100, 9800, 700, 31.3339, 118.6732, "2026-10-05"),
        ],
    )
    conn.executemany(
        "INSERT INTO trips(device_id, from_place_id, to_place_id, start_ts, end_ts, day)"
        " VALUES (?,?,?,?,?,?)",
        [
            (DEV, "p_keep", "p_drop", 9000, 9100, "2026-10-05"),
            (DEV, "p_drop", "p_other", 9800, 9900, "2026-10-05"),
        ],
    )
    conn.execute(
        "INSERT INTO anomalies(device_id, place_id, day, kind, ts) VALUES (?,?,?,?,?)",
        (DEV, "p_drop", "2026-10-05", "new_place", 9100),
    )
    conn.commit()
    conn.close()
    return path


def _ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------ location_reader


class TestCorrectionColumns:
    def test_migration_idempotent_and_user_version_untouched(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        conn = sqlite3.connect(db)
        added = ensure_correction_columns(conn)
        assert set(added) == {name for name, _t in CORRECTION_COLUMNS}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2  # 不动 v2 版本号
        assert ensure_correction_columns(conn) == []  # 幂等
        conn.close()

    def test_read_places_exposes_correction_fields(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        conn = _ro(db)
        rows = read_places(conn, device_id=DEV, order_by_visit=False)
        conn.close()
        assert len(rows) == 2
        for r in rows:  # 缺列库也必须有稳定键（NULL 归一）
            assert r["poi_override"] == "" and r["poi_candidates"] == ""
            assert r["lat_override"] is None and r["lon_override"] is None

    def test_parse_candidates_tolerates_bad_json(self) -> None:
        assert parse_candidates(None) == []
        assert parse_candidates("{bad") == []
        assert parse_candidates('{"a":1}') == []
        assert parse_candidates('[{"name":"张垛"},{"x":1},"junk"]') == [{"name": "张垛"}]


# ------------------------------------------------------------------ 显示契约覆盖


class TestNameOverride:
    def test_poi_override_wins_over_auto_confidence(self) -> None:
        name, src, gran = resolve_place_name(
            poi="彰泰·观南府", address="江苏省南京市雨花台区彰泰·观南府(西门)",
            district="雨花台区", name_confidence=0.8, label="家",
            poi_override="观云润府",
        )
        assert (name, src, gran) == ("观云润府", "override", "venue")

    def test_override_blank_falls_back_to_auto(self) -> None:
        name, src, _ = resolve_place_name(
            poi="彰泰·观南府", district="雨花台区", name_confidence=0.8,
            poi_override="   ",
        )
        assert name == "彰泰·观南府" and src == "poi"

    def test_address_override_used_when_no_poi(self) -> None:
        name, src, gran = resolve_place_name(
            poi="", address="高德地址串", district="雨花台区", name_confidence=0.0,
            address_override="我自己写的地址",
        )
        assert (name, src, gran) == ("我自己写的地址", "override_address", "address")

    def test_effective_coord_prefers_override(self) -> None:
        assert effective_coord(31.956003, 118.731643, 31.9521, 118.7310) == (31.9521, 118.7310)
        assert effective_coord(31.956003, 118.731643, None, None) == (31.956003, 118.731643)
        assert effective_coord(31.956003, 118.731643, "bad", None) == (31.956003, 118.731643)


# ------------------------------------------------------------------ 修正写库


class TestSaveCorrection:
    def test_write_and_clear_override(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        res = ps.save_place_correction(db, {
            "place_id": "p_keep", "poi_override": "观云润府",
            "address_override": "南京市雨花台区观云润府(西门)",
        })
        assert res["updated"] == 1 and "poi_override=观云润府" in res["changes"]
        conn = _ro(db)
        got = place_correction(conn, "p_keep")
        conn.close()
        assert got["poi_override"] == "观云润府"
        assert got["address_override"] == "南京市雨花台区观云润府(西门)"
        # 再次保存留空 → 回自动（列写 NULL）
        ps.save_place_correction(db, {"place_id": "p_keep", "poi_override": ""})
        conn = _ro(db)
        assert place_correction(conn, "p_keep")["poi_override"] == ""
        conn.close()

    def test_coord_override_and_reset(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        ps.save_place_correction(db, {"place_id": "p_keep", "lat": 31.95, "lon": 118.73})
        conn = _ro(db)
        got = place_correction(conn, "p_keep")
        conn.close()
        assert (got["lat_override"], got["lon_override"]) == (31.95, 118.73)
        ps.save_place_correction(db, {"place_id": "p_keep", "reset_coord": True})
        conn = _ro(db)
        got = place_correction(conn, "p_keep")
        conn.close()
        assert got["lat_override"] is None and got["lon_override"] is None

    def test_invalid_inputs_raise(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        with pytest.raises(ValueError):
            ps.save_place_correction(db, {"place_id": ""})
        with pytest.raises(ValueError):
            ps.save_place_correction(db, {"place_id": "nope", "poi_override": "x"})
        with pytest.raises(ValueError):
            ps.save_place_correction(db, {"place_id": "p_keep", "lat": 999.0, "lon": 0.0})
        with pytest.raises(ValueError):
            ps.save_place_correction(db, {"place_id": "p_keep", "lat": "x", "lon": "y"})

    def test_editor_data_carries_override_and_effective_coord(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        ps.save_place_correction(db, {
            "place_id": "p_keep", "poi_override": "观云润府",
            "lat": 31.9501, "lon": 118.7302,
        })
        d = ps.editor_data(db_path=db, config_path=tmp_path / "sem.json")
        keep = next(p for p in d["places"] if p["place_id"] == "p_keep")
        assert keep["poi_override"] == "观云润府"
        assert (keep["eff_lat"], keep["eff_lon"]) == (31.9501, 118.7302)
        assert keep["has_coord_override"] is True
        assert keep["lat"] != keep["eff_lat"]  # 原始聚类中心保留显示


# ------------------------------------------------------------------ 合并


class TestMergePlaces:
    def test_preview_rejects_unknown_and_same(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert ps.merge_preview(db, "nope", "p_keep")["ok"] is False
        assert ps.merge_preview(db, "p_keep", "p_keep")["ok"] is False
        prev = ps.merge_preview(db, "p_keep", "p_drop")
        assert prev["ok"] is True
        assert prev["merged"]["visit_count"] == 6
        assert prev["merged"]["stay_ms"] == 172434492 + 767744
        assert prev["merged"]["grid_keys"] == ["31.334,118.671", "31.334,118.673"]
        assert prev["merged"]["label"] == "张威的老家"

    def test_merge_moves_refs_and_deletes_drop(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        res = ps.merge_places(db, "p_keep", "p_drop")
        assert res["ok"] is True
        assert res["moved"]["cells"] == 1
        assert res["moved"]["stays"] == 1
        assert res["moved"]["trips"] == 2
        assert res["moved"]["anomalies"] == 1
        conn = _ro(db)
        assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM stays WHERE place_id='p_keep'").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM place_cells WHERE place_id='p_keep'").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM trips WHERE from_place_id='p_drop' OR to_place_id='p_drop'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM anomalies WHERE place_id='p_drop'").fetchone()[0] == 0
        row = conn.execute(
            "SELECT point_count, visit_count, stay_ms, first_seen, last_seen, lat, lon, label "
            "FROM places WHERE place_id='p_keep'").fetchone()
        conn.close()
        assert row["visit_count"] == 6 and row["point_count"] == 349
        assert row["stay_ms"] == 172434492 + 767744
        assert row["first_seen"] == 1000 and row["last_seen"] == 9500
        # 时长加权中心：向长停留（4000+3000ms @31.3342）偏移，不等于小簇坐标
        assert abs(row["lat"] - 31.3342) < abs(row["lat"] - 31.333896)
        assert row["label"] == "张威的老家"

    def test_cross_device_rejected(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE places SET device_id='dev2' WHERE place_id='p_drop'")
        conn.commit()
        conn.close()
        res = ps.merge_places(db, "p_keep", "p_drop")
        assert res["ok"] is False and "设备" in res["error"]
        conn = _ro(db)
        assert conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 2  # 未改动
        conn.close()

    def test_merge_inherits_label_and_note_when_keep_empty(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE places SET label='未知' WHERE place_id='p_keep'")
        conn.commit()
        conn.close()
        ps.save_place_labels(db, [{"place_id": "p_drop", "label": "张威的老家", "note": "毛球在老家"}])
        ps.merge_places(db, "p_keep", "p_drop")
        conn = _ro(db)
        row = conn.execute(
            "SELECT label, note FROM places WHERE place_id='p_keep'").fetchone()
        conn.close()
        assert row["label"] == "张威的老家" and row["note"] == "毛球在老家"


# ------------------------------------------------------------------ 高德候选（假 HTTP）


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_regeo_body() -> dict:
    return {
        "status": "1",
        "regeocode": {
            "formatted_address": "江苏省南京市雨花台区铁心桥街道机场二通道辅路观云润府",
            "addressComponent": {"district": "雨花台区", "township": "铁心桥街道"},
            "pois": [
                {"name": "彰泰·观南府", "type": "商务住宅;住宅区;住宅小区",
                 "distance": "142", "location": "118.737433,31.955167"},
            ],
            "aois": [
                {"name": "观云润府", "type": "商务住宅;住宅区;住宅小区",
                 "distance": "0", "location": "118.736086,31.952979"},
            ],
        },
    }


@pytest.fixture
def fake_amap(monkeypatch):
    from gacore.langTrack import geocode

    calls: list[str] = []

    def _open(url, timeout=None):
        calls.append(url)
        if "place/text" in url:
            return _FakeResp({
                "status": "1",
                "pois": [{"name": "观云润府", "type": "商务住宅;住宅区;住宅小区",
                          "location": "118.736086,31.952979", "distance": ""}],
            })
        return _FakeResp(_fake_regeo_body())

    monkeypatch.setattr(geocode.urllib.request, "urlopen", _open)
    monkeypatch.setenv("AMAP_KEY", "test-key")
    return calls


class TestSuggestNames:
    def test_candidates_merge_regeo_and_search(self, fake_amap) -> None:
        res = ps.suggest_place_names(31.956003, 118.731643, coord_system="wgs84")
        assert res["error"] == ""
        names = {(c["name"], c["kind"], c["source"]) for c in res["candidates"]}
        assert ("观云润府", "AOI", "regeo") in names
        assert ("彰泰·观南府", "POI", "regeo") in names
        # 关键词搜索：同名 AOI 已存在 → 不重复加入
        res2 = ps.suggest_place_names(
            31.956003, 118.731643, keyword="观云润府", coord_system="wgs84"
        )
        assert res2["error"] == ""
        assert sum(1 for c in res2["candidates"] if c["name"] == "观云润府") == 1

    def test_missing_key_returns_error_not_raise(self, monkeypatch, tmp_path: Path) -> None:
        from gacore.langTrack import geocode

        def _boom():
            raise SystemExit("no key")

        monkeypatch.setattr(geocode, "_amap_key", _boom)
        res = ps.suggest_place_names(31.9, 118.7)
        assert res["candidates"] == [] and "AMAP_KEY" in res["error"]


# ------------------------------------------------------------------ API 端点


def _client(cfg: Config, monkeypatch, token: str | None = TOKEN) -> TestClient:
    monkeypatch.setattr("gacore.review_server.load_dotenv", lambda: None)
    if token is None:
        monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    else:
        monkeypatch.setenv("REVIEW_TOKEN", token)
    return TestClient(create_app(cfg))


def _seed_cfg(tmp_path: Path) -> Config:
    cfg = Config.for_tests(tmp_path)
    (cfg.root / "data").mkdir(parents=True, exist_ok=True)
    _make_db(cfg.root / "data", "langTrack.db")
    return cfg


class TestPlacesCorrectionApi:
    def test_correction_endpoint_writes(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _seed_cfg(tmp_path)
        c = _client(cfg, monkeypatch)
        r = c.post("/api/places/correction", headers={"X-Review-Token": TOKEN}, json={
            "place_id": "p_keep", "poi_override": "观云润府", "lat": 31.95, "lon": 118.73})
        assert r.status_code == 200 and r.json()["ok"] is True
        conn = _ro(cfg.root / "data" / "langTrack.db")
        got = place_correction(conn, "p_keep")
        conn.close()
        assert got["poi_override"] == "观云润府"
        assert (got["lat_override"], got["lon_override"]) == (31.95, 118.73)

    def test_correction_requires_token_and_valid_id(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _seed_cfg(tmp_path)
        c = _client(cfg, monkeypatch, token=None)
        assert c.post("/api/places/correction", json={"place_id": "p_keep"}).status_code == 401
        c2 = _client(cfg, monkeypatch)
        r = c2.post("/api/places/correction", headers={"X-Review-Token": TOKEN},
                    json={"place_id": "nope"})
        assert r.status_code == 400

    def test_merge_endpoint(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _seed_cfg(tmp_path)
        c = _client(cfg, monkeypatch)
        r = c.post("/api/places/merge", headers={"X-Review-Token": TOKEN},
                   json={"keep_id": "p_keep", "drop_id": "p_drop"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and body["moved"]["stays"] == 1
        # 再合并一次：被并点已不存在 → 400
        r2 = c.post("/api/places/merge", headers={"X-Review-Token": TOKEN},
                    json={"keep_id": "p_keep", "drop_id": "p_drop"})
        assert r2.status_code == 400

    def test_name_candidates_endpoint(self, tmp_path: Path, monkeypatch, fake_amap) -> None:
        cfg = _seed_cfg(tmp_path)
        c = _client(cfg, monkeypatch)
        r = c.get("/api/places/name_candidates", params={"place_id": "p_keep"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert any(x["name"] == "观云润府" for x in body["candidates"])
        # 未知 place_id → 400（不抛 500）
        r2 = c.get("/api/places/name_candidates", params={"place_id": "nope"})
        assert r2.status_code == 400

    def test_places_data_endpoint(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _seed_cfg(tmp_path)
        c = _client(cfg, monkeypatch)
        body = c.get("/api/places/data").json()
        assert body["ok"] and len(body["places"]) == 2
        assert body["places"][0]["eff_lat"] is not None
