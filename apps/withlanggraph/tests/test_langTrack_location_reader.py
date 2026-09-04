"""test_langTrack_location_reader.py —— v1/v2 双读层测试（Task 5）。

同一组语义数据分别构建 v1 正式库与 v2 正式库（手动建表 + PRAGMA
user_version=2 模拟激活后形态，不执行 activate），跑同一组断言：

- 统一行结构：place/stay/trip/anomaly 字段集在 v1/v2 完全一致（只增不删）；
- v1 兼容映射：place_id/from_place_id/to_place_id 恒 None，
  point_count/visit_episodes 映射 visit_count；
- v2 关联键：stay.grid_key 是成员网格（≠ place 代表网格）时，
  内嵌 place 字段必须来自 place_id JOIN 而非 grid_key；
- 设备隔离：dev1/dev2 同网格各自成行。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from gacore.langTrack import etl
from gacore.langTrack import location_migration as lm
from gacore.langTrack import location_reader as lr

HOME_GK = "31.992,118.783"
WORK_GK = "31.998,118.790"
# v2 下 home place 的第二个成员网格（stay 发生在这里，但 place 代表网格仍是 HOME_GK）
HOME_MEMBER_GK = "31.993,118.784"

DAY = "2026-08-17"
DAY_START = 1_000
DAY_END = 90_000

# 统一断言的字段集（兼容契约：v1/v2 必须同时具备，只增不删）
PLACE_FIELDS = {
    "id", "device_id", "place_id", "grid_key", "lat", "lon", "label",
    "first_seen", "last_seen", "visit_count", "visit_episodes", "point_count",
    "stay_ms", "is_primary", "address", "poi", "poi_fallback", "district",
    "township", "business_area", "poi_type", "behavior", "matched_level",
    "candidate_label", "confidence_home", "confidence_work", "geocoded_at",
    "name_confidence", "name_evidence", "parent_poi",
    "poi_l1", "poi_l2", "poi_l3",
}
STAY_FIELDS = {
    "device_id", "place_id", "start_ts", "end_ts", "duration_ms",
    "center_lat", "center_lon", "min_lat", "min_lon", "max_lat", "max_lon",
    "n_points", "radius_m", "grid_key", "day", "avg_accuracy_m",
    "place_label", "place_poi", "place_poi_fallback", "place_address",
    "place_behavior", "place_district",
}
TRIP_FIELDS = {
    "device_id", "from_place_id", "to_place_id", "start_ts", "end_ts",
    "duration_ms", "start_lat", "start_lon", "end_lat", "end_lon", "dist_m",
    "n_points", "day", "polyline", "route_key", "route_mode", "route_encoded_at",
    "route_dist_m",
}
ANOMALY_FIELDS = {"day", "kind", "device_id", "place_id", "grid_key", "poi", "detail", "ts"}


def _seed_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(etl._SCHEMA)
    conn.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, last_seen, "
        "visit_count, is_primary, poi, address) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", HOME_GK, 31.992, 118.783, "家", DAY_START, DAY_END, 30, 1, "甲小区南门", "某某路1号"),
            ("dev1", WORK_GK, 31.998, 118.790, "公司", DAY_START, DAY_END, 25, 1, "乙大厦", "某某路2号"),
            ("dev2", HOME_GK, 31.992, 118.783, "家", DAY_START, DAY_END, 5, 0, None, None),
        ],
    )
    # v1：stay.grid_key 即 place 的 grid_key
    conn.executemany(
        "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, center_lon, "
        "min_lat, min_lon, max_lat, max_lon, n_points, radius_m, grid_key, day) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", DAY_START, 8_000, 7_000, 31.992, 118.783, 31.991, 118.782, 31.993, 118.784,
             20, 50.0, HOME_GK, DAY),
            ("dev1", 9_000, 20_000, 11_000, 31.998, 118.790, 31.997, 118.789, 31.999, 118.791,
             30, 60.0, WORK_GK, DAY),
            ("dev2", DAY_START, 6_000, 5_000, 31.992, 118.783, 31.991, 118.782, 31.993, 118.784,
             15, 45.0, HOME_GK, DAY),
        ],
    )
    conn.execute(
        "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "
        "end_lat, end_lon, dist_m, n_points, day) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", 8_000, 9_000, 1_000, 31.992, 118.783, 31.998, 118.790, 900.0, 3, DAY),
    )
    conn.execute(
        "INSERT INTO anomalies(day, kind, device_id, grid_key, poi, detail, ts) VALUES (?,?,?,?,?,?,?)",
        (DAY, "new_place", "dev1", HOME_GK, "甲小区南门", "访问 1 次", 12_000),
    )
    conn.commit()


def _seed_v2(conn: sqlite3.Connection) -> None:
    """SCHEMA_V2 建 *_v2 表 → rename 正式名 → user_version=2（不执行 activate）。"""
    conn.executescript(etl._SCHEMA)  # 先建 v1 表（后续 rename 覆盖，模拟真实激活形态）
    conn.executescript(lm.SCHEMA_V2)
    for t in lm.V2_FACT_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute(f"ALTER TABLE {t}_v2 RENAME TO {t}")
    # 其余 v1 表清掉避免干扰
    for t in ("places",):
        pass  # 已被 v2 表覆盖
    conn.executemany(
        "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, first_seen, "
        "last_seen, point_count, visit_count, stay_ms, is_primary, poi, address, "
        "name_confidence, name_evidence, parent_poi) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # home place：代表网格 HOME_GK，成员含 HOME_MEMBER_GK；visit_count=段数2
            ("dev1", "pid_home", HOME_GK, 31.992, 118.783, "家", DAY_START, DAY_END,
             100, 2, 8_600_000, 1, "甲小区南门", "某某路1号", 0.9, "regeo_poi", "某某商圈"),
            ("dev1", "pid_work", WORK_GK, 31.998, 118.790, "公司", DAY_START, DAY_END,
             80, 1, 11_000, 1, "乙大厦", "某某路2号", 0.85, "regeo_poi", ""),
            ("dev2", "pid_home2", HOME_GK, 31.992, 118.783, "家", DAY_START, DAY_END,
             20, 1, 5_000, 0, None, None, 0.0, "", ""),
        ],
    )
    conn.executemany(
        "INSERT INTO place_cells(device_id, place_id, grid_key) VALUES (?,?,?)",
        [
            ("dev1", "pid_home", HOME_GK),
            ("dev1", "pid_home", HOME_MEMBER_GK),
            ("dev1", "pid_work", WORK_GK),
            ("dev2", "pid_home2", HOME_GK),
        ],
    )
    # v2 关键差异：dev1 home stay 发生在成员网格 HOME_MEMBER_GK（≠ place 代表网格）
    conn.executemany(
        "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, center_lon, "
        "min_lat, min_lon, max_lat, max_lon, n_points, radius_m, grid_key, place_id, day, "
        "avg_accuracy_m) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", DAY_START, 8_000, 7_000, 31.993, 118.784, 31.992, 118.783, 31.994, 118.785,
             20, 50.0, HOME_MEMBER_GK, "pid_home", DAY, 12.5),
            ("dev1", 9_000, 20_000, 11_000, 31.998, 118.790, 31.997, 118.789, 31.999, 118.791,
             30, 60.0, WORK_GK, "pid_work", DAY, 20.0),
            ("dev2", DAY_START, 6_000, 5_000, 31.992, 118.783, 31.991, 118.782, 31.993, 118.784,
             15, 45.0, HOME_GK, "pid_home2", DAY, None),
        ],
    )
    conn.execute(
        "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "
        "end_lat, end_lon, from_place_id, to_place_id, dist_m, n_points, day) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", 8_000, 9_000, 1_000, 31.992, 118.783, 31.998, 118.790,
         "pid_home", "pid_work", 900.0, 3, DAY),
    )
    conn.execute(
        "INSERT INTO anomalies(day, kind, device_id, place_id, grid_key, poi, detail, ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (DAY, "new_place", "dev1", "pid_home", HOME_GK, "甲小区南门", "访问 1 次", 12_000),
    )
    # v2 独有：dev1 两条 tag 冲突、dev2 一条（read_tag_conflict_count 用）
    conn.executemany(
        "INSERT INTO place_tag_conflicts(device_id, new_place_id, old_place_id, tag, reason) "
        "VALUES (?,?,?,?,?)",
        [
            ("dev1", "pid_home", "pid_a", "家", "merge"),
            ("dev1", "pid_work", "pid_b", "公司", "merge"),
            ("dev2", "pid_home2", "pid_c", "家", "merge"),
        ],
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()


@pytest.fixture(params=["v1", "v2"])
def loc_db(request):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    (_seed_v1 if request.param == "v1" else _seed_v2)(conn)
    yield conn
    conn.close()


class TestSchemaVersion:
    def test_version_detected(self, loc_db):
        expect = 2 if lr.is_v2(loc_db) else 0
        assert lr.schema_version(loc_db) == expect


class TestReadPlaces:
    def test_field_set_identical_across_versions(self, loc_db):
        rows = lr.read_places(loc_db)
        assert rows
        for r in rows:
            assert set(r) == PLACE_FIELDS

    def test_label_filter_and_order(self, loc_db):
        rows = lr.read_places(loc_db, device_id="dev1", label_in=("家", "公司"))
        assert [r["label"] for r in rows] == ["家", "公司"]  # visit 30 > 25

    def test_device_isolation(self, loc_db):
        rows = lr.read_places(loc_db, label_in=("家",))
        assert {r["device_id"] for r in rows} == {"dev1", "dev2"}
        assert len({(r["device_id"], r["grid_key"]) for r in rows}) == 2

    def test_v1_compat_mapping(self, loc_db):
        home = next(r for r in lr.read_places(loc_db, device_id="dev1") if r["label"] == "家")
        if not lr.is_v2(loc_db):
            assert home["place_id"] is None
            assert home["point_count"] == home["visit_count"] == 30
            assert home["visit_episodes"] == 30
            assert home["stay_ms"] == 0
        else:
            assert home["place_id"] == "pid_home"
            assert home["point_count"] == 100
            assert home["visit_count"] == home["visit_episodes"] == 2
            assert home["stay_ms"] == 8_600_000

    def test_candidate_only(self, loc_db):
        conn = loc_db
        if lr.is_v2(conn):
            conn.execute("UPDATE places SET candidate_label='家', label='未知' WHERE place_id='pid_home2'")
        else:
            conn.execute("UPDATE places SET candidate_label='家', label='未知' WHERE device_id='dev2'")
        conn.commit()
        rows = lr.read_places(loc_db, candidate_only=True)
        assert len(rows) == 1
        assert rows[0]["candidate_label"] == "家"

    def test_name_evidence_fields(self, loc_db):
        """v2 透传命名证据（PlaceRef 用）；v1/缺列归一为默认值。"""
        home = next(r for r in lr.read_places(loc_db, device_id="dev1") if r["label"] == "家")
        if lr.is_v2(loc_db):
            assert home["name_confidence"] == 0.9
            assert home["name_evidence"] == "regeo_poi"
            assert home["parent_poi"] == "某某商圈"
        else:
            assert home["name_confidence"] == 0.0
            assert home["name_evidence"] == ""
            assert home["parent_poi"] == ""


class TestReadStays:
    def test_field_set_identical_across_versions(self, loc_db):
        rows = lr.read_stays(loc_db)
        assert rows
        for r in rows:
            assert set(r) == STAY_FIELDS

    def test_place_join_by_place_id_not_grid(self, loc_db):
        """v2 成员网格 ≠ 代表网格：内嵌 place 字段必须来自 place_id JOIN。"""
        home_stay = next(
            r for r in lr.read_stays(loc_db, device_id="dev1") if r["end_ts"] == 8_000
        )
        if not lr.is_v2(loc_db):
            assert home_stay["place_id"] is None
            assert home_stay["grid_key"] == HOME_GK
        else:
            # stay 网格是成员网格，仍要命中 home place（place_id JOIN）
            assert home_stay["grid_key"] == HOME_MEMBER_GK
            assert home_stay["place_id"] == "pid_home"
            # 若按 grid_key JOIN，这里会得到 None
            assert home_stay["place_label"] == "家"
            assert home_stay["place_poi"] == "甲小区南门"

    def test_overlap_window(self, loc_db):
        rows = lr.read_stays(loc_db, device_id="dev1", overlap=(5_000, 10_000))
        assert {r["end_ts"] for r in rows} == {8_000, 20_000}

    def test_day_filter(self, loc_db):
        assert len(lr.read_stays(loc_db, day=DAY)) == 3
        assert lr.read_stays(loc_db, day="2020-01-01") == []

    def test_without_place(self, loc_db):
        rows = lr.read_stays(loc_db, with_place=False)
        assert rows
        for r in rows:
            assert r["place_label"] is None
            assert r["place_poi"] is None

    def test_avg_accuracy_m(self, loc_db):
        """v2 透传 stay 中心精度（stays_v2.avg_accuracy_m）；v1 恒 None。"""
        home_stay = next(
            r for r in lr.read_stays(loc_db, device_id="dev1") if r["end_ts"] == 8_000
        )
        if lr.is_v2(loc_db):
            assert home_stay["avg_accuracy_m"] == 12.5
        else:
            assert home_stay["avg_accuracy_m"] is None

    def test_avg_accuracy_m_null_passthrough(self, loc_db):
        """未知精度的 stay 透传 None（不归一为 0）。"""
        dev2_stay = lr.read_stays(loc_db, device_id="dev2")[0]
        assert dev2_stay["avg_accuracy_m"] is None


class TestReadTrips:
    def test_field_set_identical_across_versions(self, loc_db):
        rows = lr.read_trips(loc_db)
        assert rows
        for r in rows:
            assert set(r) == TRIP_FIELDS

    def test_place_refs(self, loc_db):
        t = lr.read_trips(loc_db, device_id="dev1")[0]
        if not lr.is_v2(loc_db):
            assert t["from_place_id"] is None and t["to_place_id"] is None
        else:
            assert (t["from_place_id"], t["to_place_id"]) == ("pid_home", "pid_work")

    def test_overlap(self, loc_db):
        assert len(lr.read_trips(loc_db, overlap=(8_500, 8_600))) == 1
        assert lr.read_trips(loc_db, overlap=(50_000, 60_000)) == []


class TestReadAnomalies:
    def test_field_set_identical_across_versions(self, loc_db):
        rows = lr.read_anomalies(loc_db)
        assert rows
        for r in rows:
            assert set(r) == ANOMALY_FIELDS

    def test_place_id_semantics(self, loc_db):
        a = lr.read_anomalies(loc_db, day=DAY)[0]
        if not lr.is_v2(loc_db):
            assert a["place_id"] is None
        else:
            assert a["place_id"] == "pid_home"


class TestMissingTables:
    def test_empty_db_returns_empty(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        assert lr.read_places(conn) == []
        assert lr.read_stays(conn) == []
        assert lr.read_trips(conn) == []
        assert lr.read_anomalies(conn) == []
        conn.close()


class TestMinimalSchema:
    """最小 schema（旧测试库/裁剪库）容错：缺失列补 NULL，行结构与字段集不变。"""

    @pytest.fixture()
    def min_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE places(id INTEGER PRIMARY KEY, device_id TEXT, grid_key TEXT,
                label TEXT, visit_count INTEGER, poi TEXT, behavior TEXT,
                district TEXT, address TEXT);
            CREATE TABLE stays(id INTEGER PRIMARY KEY, device_id TEXT, grid_key TEXT,
                start_ts INTEGER, end_ts INTEGER, day TEXT);
            CREATE TABLE trips(id INTEGER PRIMARY KEY, device_id TEXT, start_ts INTEGER,
                end_ts INTEGER, dist_m INTEGER, day TEXT);
            CREATE TABLE anomalies(id INTEGER PRIMARY KEY, device_id TEXT, day TEXT,
                kind TEXT, poi TEXT, detail TEXT, ts INTEGER);
            INSERT INTO places(device_id, grid_key, label, visit_count, poi)
                VALUES ('dev1', 'g1', '家', 3, '甲小区南门');
            INSERT INTO stays(device_id, grid_key, start_ts, end_ts, day)
                VALUES ('dev1', 'g1', 1_000, 8_000, '2026-08-17');
            INSERT INTO trips(device_id, start_ts, end_ts, dist_m, day)
                VALUES ('dev1', 8_000, 9_000, 500, '2026-08-17');
            INSERT INTO anomalies(device_id, day, kind, poi, detail, ts)
                VALUES ('dev1', '2026-08-17', 'new_place', '甲小区南门', '访问 1 次', 12_000);
            """
        )
        yield conn
        conn.close()

    def test_places_field_set_stable(self, min_db):
        rows = lr.read_places(min_db, device_id="dev1")
        assert len(rows) == 1
        assert set(rows[0]) == PLACE_FIELDS
        assert rows[0]["label"] == "家"
        assert rows[0]["place_id"] is None  # v1 兼容映射不受列缺失影响

    def test_stays_join_and_field_set_stable(self, min_db):
        rows = lr.read_stays(min_db, device_id="dev1")
        assert len(rows) == 1
        assert set(rows[0]) == STAY_FIELDS
        # grid_key JOIN 在最小 schema 上照常工作
        assert rows[0]["place_label"] == "家"
        assert rows[0]["place_poi"] == "甲小区南门"
        assert rows[0]["duration_ms"] == 0  # 缺失数值列归一为 0
        assert rows[0]["center_lat"] is None

    def test_trips_field_set_stable(self, min_db):
        rows = lr.read_trips(min_db, device_id="dev1")
        assert len(rows) == 1
        assert set(rows[0]) == TRIP_FIELDS
        assert rows[0]["from_place_id"] is None

    def test_anomalies_field_set_stable(self, min_db):
        rows = lr.read_anomalies(min_db, device_id="dev1")
        assert len(rows) == 1
        assert set(rows[0]) == ANOMALY_FIELDS
        assert rows[0]["place_id"] is None


class TestReadPlaceCells:
    def test_v2_place_id_filter(self, loc_db):
        if not lr.is_v2(loc_db):
            assert lr.read_place_cells(loc_db, device_id="dev1", place_id="pid_home") == []
            return
        rows = lr.read_place_cells(loc_db, device_id="dev1", place_id="pid_home")
        assert {r["grid_key"] for r in rows} == {HOME_GK, HOME_MEMBER_GK}
        assert all(r["place_id"] == "pid_home" for r in rows)

    def test_device_isolation(self, loc_db):
        rows = lr.read_place_cells(loc_db, device_id="dev2")
        if lr.is_v2(loc_db):
            assert {r["place_id"] for r in rows} == {"pid_home2"}
        else:
            assert rows == []


class TestPlaceGridMap:
    def test_v2_member_grid_expanded(self, loc_db):
        m = lr.place_grid_map(loc_db, device_id="dev1")
        if lr.is_v2(loc_db):
            # 成员网格（非代表网格）也必须能找到 place
            assert m[HOME_MEMBER_GK]["place_id"] == "pid_home"
            assert m[HOME_GK]["place_id"] == "pid_home"
            assert m[WORK_GK]["place_id"] == "pid_work"
        else:
            assert set(m) == {HOME_GK, WORK_GK}
            assert m[HOME_GK]["label"] == "家"

    def test_device_isolation(self, loc_db):
        m = lr.place_grid_map(loc_db, device_id="dev2")
        # dev2 与 dev1 同网格 HOME_GK，映射互不串扰
        if lr.is_v2(loc_db):
            assert set(m) == {HOME_GK}
            assert m[HOME_GK]["place_id"] == "pid_home2"
        else:
            assert set(m) == {HOME_GK}

    def test_same_grid_takes_highest_visit_count(self):
        """同设备同网格多 place：取 visit_count 最高者（先占位者胜的降序保证）。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                place_id TEXT,
                grid_key TEXT,
                lat REAL, lon REAL, label TEXT,
                first_seen INTEGER, last_seen INTEGER,
                point_count INTEGER DEFAULT 0,
                visit_count INTEGER DEFAULT 0,
                stay_ms INTEGER DEFAULT 0,
                is_primary INTEGER DEFAULT 0,
                poi TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, "
            "first_seen, last_seen, point_count, visit_count, stay_ms, is_primary, poi) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("dev1", "pid_small", HOME_GK, 31.992, 118.783, "未知", DAY_START, DAY_END, 10, 2, 20, 0, "小地点"),
                ("dev1", "pid_big", HOME_GK, 31.992, 118.783, "未知", DAY_START, DAY_END, 50, 9, 100, 1, "大地点"),
            ],
        )
        # 插入顺序故意让小 place 在前，验证排序仍由 visit_count 决定
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        m = lr.place_grid_map(conn, device_id="dev1")
        conn.close()
        assert m[HOME_GK]["place_id"] == "pid_big"


class TestDayFromFilter:
    """persona 实际使用的 day_from 过滤（v1/v2 行为一致）。"""

    def test_stays_day_from(self, loc_db):
        assert len(lr.read_stays(loc_db, day_from=DAY)) == 3
        assert len(lr.read_stays(loc_db, device_id="dev1", day_from=DAY)) == 2
        assert lr.read_stays(loc_db, day_from="2099-01-01") == []

    def test_trips_day_from(self, loc_db):
        assert len(lr.read_trips(loc_db, day_from=DAY)) == 1
        assert lr.read_trips(loc_db, day_from="2099-01-01") == []


class TestReadDailyQuality:
    """Task 6 §3.2 坐标质量日行读取（FactCard full 透传数据源）。"""

    def test_missing_table_returns_none(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        assert lr.read_daily_quality(conn, device_id="dev1", day=DAY) is None
        conn.close()

    def test_no_row_returns_none(self, loc_db):
        # etl._SCHEMA 已建表但无行（未跑 Task 6 ETL 的旧库形态）
        assert lr.read_daily_quality(loc_db, device_id="dev1", day=DAY) is None

    def test_row_passthrough_without_audit_columns(self, loc_db):
        loc_db.execute(
            "INSERT INTO daily_location_quality(day, device_id, points_total, points_valid, "
            "accuracy_known, accuracy_le_50, accuracy_51_150, accuracy_gt_150, "
            "observed_half_hour_bins, median_interval_sec, providers_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (DAY, "dev1", 120, 100, 100, 60, 30, 10, 16, 45.0, '{"gps":80}'),
        )
        loc_db.commit()
        q = lr.read_daily_quality(loc_db, device_id="dev1", day=DAY)
        assert q is not None
        assert q["day"] == DAY
        assert q["device_id"] == "dev1"
        assert q["points_total"] == 120
        assert q["points_valid"] == 100
        assert q["accuracy_le_50"] == 60
        assert q["median_interval_sec"] == 45.0
        assert q["providers_json"] == '{"gps":80}'
        # 审计列不透传（快照确定性）
        assert "created_at" not in q
        assert "updated_at" not in q

    def test_day_device_isolated(self, loc_db):
        loc_db.executemany(
            "INSERT INTO daily_location_quality(day, device_id, points_total, points_valid, "
            "accuracy_known, accuracy_le_50, accuracy_51_150, accuracy_gt_150, "
            "observed_half_hour_bins, median_interval_sec, providers_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (DAY, "dev1", 120, 100, 100, 60, 30, 10, 16, 45.0, "{}"),
                ("2026-08-18", "dev1", 10, 8, 8, 4, 2, 2, 5, 60.0, "{}"),
                (DAY, "dev2", 50, 40, 40, 20, 10, 10, 12, 30.0, "{}"),
            ],
        )
        loc_db.commit()
        q = lr.read_daily_quality(loc_db, device_id="dev1", day=DAY)
        assert q["points_total"] == 120
        assert lr.read_daily_quality(loc_db, device_id="dev2", day=DAY)["points_total"] == 50
        assert lr.read_daily_quality(loc_db, device_id="dev1", day="2026-08-18")["points_total"] == 10
        assert lr.read_daily_quality(loc_db, device_id="dev1", day="2020-01-01") is None


class TestReadTagConflictCount:
    """v2 place_tag_conflicts 人工 tag 冲突计数（FactCard full 透传数据源）。"""

    def test_missing_table_returns_zero(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        assert lr.read_tag_conflict_count(conn, device_id="dev1") == 0
        conn.close()

    def test_counts_per_device(self, loc_db):
        if lr.is_v2(loc_db):
            assert lr.read_tag_conflict_count(loc_db, device_id="dev1") == 2
            assert lr.read_tag_conflict_count(loc_db, device_id="dev2") == 1
        else:
            # v1 无该表，恒 0
            assert lr.read_tag_conflict_count(loc_db, device_id="dev1") == 0
            assert lr.read_tag_conflict_count(loc_db, device_id="dev2") == 0

    def test_empty_table_returns_zero(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE place_tag_conflicts (device_id TEXT, new_place_id TEXT, "
            "old_place_id TEXT, tag TEXT, reason TEXT)"
        )
        assert lr.read_tag_conflict_count(conn, device_id="dev1") == 0
        conn.close()
