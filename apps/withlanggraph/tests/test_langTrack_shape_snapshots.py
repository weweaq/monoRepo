"""test_langTrack_shape_snapshots.py —— Task 5e v1/v2 消费者 JSON shape 快照测试。

同一组语义数据（同一现实：家/公司两个地点、两段停留、一趟行程、一条异常）
分别构建 v1 正式库与 v2 正式形态库（SCHEMA_V2 转正 + PRAGMA user_version=2，
不执行 activate），跑同一组消费者断言：

- 兼容契约（只增字段、不删字段、不改旧字段含义）：
  FactCard 顶层键集 v1/v2 完全一致；StayBrief/TripBrief/PlaceBrief/
  AnomalyBrief/CurrentKnown 字段集与字面契约一致；
  旧字段 StayBrief.label / TripBrief.from_label/to_label / PlaceBrief.visits
  在两个版本下语义不变（label 仍来自关联 place）；
- v2 关联键正确性：stay.grid_key 是成员网格（≠ place 代表网格）时，
  label/poi 仍取自 place_id JOIN，不静默漂移；
- persona / report profile：v1/v2 输出的 JSON 键结构递归一致。
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

from gacore.langTrack import etl, persona, storage
from gacore.langTrack import fact_card as fc
from gacore.langTrack import location_migration as lm
from gacore.langTrack import report as rpt

_TZ = datetime.timezone(datetime.timedelta(hours=8))
DAY = "2026-08-17"
HOME_GK = "31.992,118.783"
WORK_GK = "31.998,118.790"
# v2 下 home place 的第二个成员网格（home stay 发生在这里，代表网格仍是 HOME_GK）
HOME_MEMBER_GK = "31.993,118.784"
NOW_MS = int(datetime.datetime(2026, 9, 1, 12, 0, tzinfo=_TZ).timestamp() * 1000)


def _ts(hh: int, mm: int) -> int:
    d = datetime.datetime(2026, 8, 17, hh, mm, tzinfo=_TZ)
    return int(d.timestamp() * 1000)


# 字面契约快照（旧 JSON shape；只增不删——删字段/改名在此处即失败）
# Task 7：StayBrief/TripBrief/PlaceBrief/CurrentKnown 增 PlaceRef 载荷，
# 旧字段 label/from_label/to_label/visits 保留兼容（label=format_place）
STAY_BRIEF_KEYS = {
    "label", "poi", "start_hhmm", "end_hhmm", "mins",
    "place_id", "place_name", "user_tag", "name_source", "poi_fallback",
    "point_count", "avg_accuracy_m", "behavior", "district",
}
TRIP_BRIEF_KEYS = {
    "start_hhmm", "end_hhmm", "dist_m", "from_label", "to_label",
    "from_place", "to_place", "route_dist_m",
}
PLACE_BRIEF_KEYS = {
    "label", "visits", "poi", "behavior", "address",
    "place_id", "place_name", "user_tag", "name_source", "poi_fallback",
    "district", "point_count", "stay_ms", "visit_episodes",
}
ANOMALY_BRIEF_KEYS = {"kind", "poi", "detail"}
CURRENT_KNOWN_KEYS = {
    "label", "since_hhmm", "observed_until_hhmm", "poi", "behavior", "district",
    "place_id", "place_name", "user_tag", "name_source", "poi_fallback",
}
REPORT_PROFILE_TOP_KEYS = {
    "date", "device_id", "generated_at", "screen", "notifications", "sleep",
    "scenes", "outings", "anomalies", "consumption", "rhythm", "persona", "weather",
}


def _seed_common(conn: sqlite3.Connection) -> None:
    """schema 无关的公共事实：daily_stats / etl_state。"""
    conn.execute(
        "INSERT INTO daily_stats(device_id, day, total_screen_ms, app_ranking_json, "
        "notification_count, notification_clicked, top_notification_apps_json, "
        "screen_on_count, screen_off_count, unlock_count, switch_count, "
        "location_count, audio_clip_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", DAY, 7_200_000, json.dumps([{"app": "微信", "ms": 3_600_000}]),
         5, 1, json.dumps([{"app": "微博", "n": 3}]), 40, 38, 30, 45, 50, 2),
    )
    conn.execute(
        "INSERT INTO etl_state(device_id, last_event_ts) VALUES (?,?)",
        ("dev1", _ts(18, 0)),
    )


def _seed_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(storage._SCHEMA)
    conn.executescript(etl._SCHEMA)
    _seed_common(conn)
    conn.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, "
        "last_seen, visit_count, is_primary, poi, address) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", HOME_GK, 31.992, 118.783, "家", _ts(0, 0), _ts(18, 0), 30, 1,
             "甲小区南门", "某某路1号"),
            ("dev1", WORK_GK, 31.998, 118.790, "公司", _ts(9, 0), _ts(18, 0), 25, 1,
             "乙大厦", "某某路2号"),
        ],
    )
    conn.executemany(
        "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
        "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, "
        "grid_key, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", _ts(0, 0), _ts(8, 0), 28_800_000, 31.992, 118.783,
             31.991, 118.782, 31.993, 118.784, 20, 50.0, HOME_GK, DAY),
            ("dev1", _ts(9, 0), _ts(18, 0), 32_400_000, 31.998, 118.790,
             31.997, 118.789, 31.999, 118.791, 30, 60.0, WORK_GK, DAY),
        ],
    )
    conn.execute(
        "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, "
        "start_lon, end_lat, end_lon, dist_m, n_points, day) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", _ts(8, 0), _ts(9, 0), 3_600_000, 31.992, 118.783, 31.998,
         118.790, 900, 12, DAY),
    )
    conn.execute(
        "INSERT INTO anomalies(day, kind, device_id, grid_key, poi, detail, ts) "
        "VALUES (?,?,?,?,?,?,?)",
        (DAY, "new_place", "dev1", WORK_GK, "乙大厦", "首次到访新地点：乙大厦（访问 1 次）",
         _ts(9, 0)),
    )
    conn.commit()


def _seed_v2(conn: sqlite3.Connection) -> None:
    """SCHEMA_V2 建表 → 转正 → user_version=2（模拟激活后形态，不执行 activate）。"""
    conn.executescript(storage._SCHEMA)
    conn.executescript(etl._SCHEMA)
    _seed_common(conn)
    conn.executescript(lm.SCHEMA_V2)
    for t in lm.V2_FACT_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute(f"ALTER TABLE {t}_v2 RENAME TO {t}")
    conn.executemany(
        "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, first_seen, "
        "last_seen, point_count, visit_count, stay_ms, is_primary, poi, address) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", "pid_home", HOME_GK, 31.992, 118.783, "家", _ts(0, 0), _ts(18, 0),
             100, 2, 61_200_000, 1, "甲小区南门", "某某路1号"),
            ("dev1", "pid_work", WORK_GK, 31.998, 118.790, "公司", _ts(9, 0), _ts(18, 0),
             80, 1, 32_400_000, 1, "乙大厦", "某某路2号"),
        ],
    )
    conn.executemany(
        "INSERT INTO place_cells(device_id, place_id, grid_key) VALUES (?,?,?)",
        [
            ("dev1", "pid_home", HOME_GK),
            ("dev1", "pid_home", HOME_MEMBER_GK),
            ("dev1", "pid_work", WORK_GK),
        ],
    )
    # v2 关键差异：home stay 在成员网格上（≠ place 代表网格），靠 place_id 关联
    conn.executemany(
        "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
        "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, "
        "grid_key, place_id, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", _ts(0, 0), _ts(8, 0), 28_800_000, 31.993, 118.784,
             31.992, 118.783, 31.994, 118.785, 20, 50.0, HOME_MEMBER_GK, "pid_home", DAY),
            ("dev1", _ts(9, 0), _ts(18, 0), 32_400_000, 31.998, 118.790,
             31.997, 118.789, 31.999, 118.791, 30, 60.0, WORK_GK, "pid_work", DAY),
        ],
    )
    conn.execute(
        "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, "
        "start_lon, end_lat, end_lon, from_place_id, to_place_id, dist_m, n_points, day) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", _ts(8, 0), _ts(9, 0), 3_600_000, 31.992, 118.783, 31.998,
         118.790, "pid_home", "pid_work", 900, 12, DAY),
    )
    conn.execute(
        "INSERT INTO anomalies(day, kind, device_id, place_id, grid_key, poi, detail, ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (DAY, "new_place", "dev1", "pid_work", WORK_GK, "乙大厦",
         "首次到访新地点：乙大厦（访问 1 次）", _ts(9, 0)),
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()


@pytest.fixture(params=["v1", "v2"])
def sema_pair(request):
    """语义等价的 v1 / v2 正式形态库：(conn, version)。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    (_seed_v1 if request.param == "v1" else _seed_v2)(conn)
    yield conn, request.param
    conn.close()


@pytest.fixture
def sema_db(sema_pair):
    return sema_pair[0]


# ---------------------------------------------------------------------------
# FactCard：shape 快照 + 旧字段语义
# ---------------------------------------------------------------------------

class TestFactCardShape:
    def test_card_not_degraded(self, sema_db):
        c = fc.build(conn=sema_db, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
        assert c["available"] is True
        assert c["has_facts"] is True

    def test_top_level_keys_identical_and_legacy_present(self):
        """两版本顶层键集一致且含全部旧契约键（只增不删的字面快照）。"""
        keys = {}
        for v in ("v1", "v2"):
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            (_seed_v1 if v == "v1" else _seed_v2)(conn)
            c = fc.build(conn=conn, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
            keys[v] = set(c)
            conn.close()
        assert keys["v1"] == keys["v2"]
        legacy = {
            "device_id", "day", "available", "generated_at",
            "current_known", "screen_ms", "stays", "trips", "stay_minutes",
            "anomalies", "places", "notification_count", "unlock_count",
        }
        assert legacy <= keys["v1"]

    def test_brief_field_sets_match_contract(self, sema_db):
        c = fc.build(conn=sema_db, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
        assert len(c["stays"]) == 2
        for s in c["stays"]:
            assert set(s) == STAY_BRIEF_KEYS
        assert len(c["trips"]) == 1
        for t in c["trips"]:
            assert set(t) == TRIP_BRIEF_KEYS
        assert len(c["places"]) == 2
        for p in c["places"]:
            assert set(p) == PLACE_BRIEF_KEYS
        assert len(c["anomalies"]) == 1
        for a in c["anomalies"]:
            assert set(a) == ANOMALY_BRIEF_KEYS
        assert set(c["current_known"]) == CURRENT_KNOWN_KEYS


class TestFactCardLegacySemantics:
    """旧字段含义在 v1/v2 不变：label 仍来自关联 place，v2 用 place_id 关联。"""

    def test_stay_labels_and_poi(self, sema_pair):
        conn, ver = sema_pair
        c = fc.build(conn=conn, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
        home, work = c["stays"]
        assert home["label"] == "某某路1号〔家〕" and home["poi"] == "甲小区南门"
        assert work["label"] == "某某路2号〔公司〕" and work["poi"] == "乙大厦"
        assert home["start_hhmm"] == "00:00" and home["end_hhmm"] == "08:00"
        assert home["mins"] == 480
        assert home["place_name"] == "某某路1号" and home["name_source"] == "address"
        assert home["user_tag"] == "家"
        if ver == "v2":
            assert home["place_id"] == "pid_home"
        else:
            assert home["place_id"] in ("", None)  # v1 无 place_id

    def test_trip_from_to_labels(self, sema_pair):
        conn, ver = sema_pair
        c = fc.build(conn=conn, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
        t = c["trips"][0]
        assert t["from_label"] == "某某路1号〔家〕" and t["to_label"] == "某某路2号〔公司〕"
        assert t["dist_m"] == 900
        assert t["route_dist_m"] is None  # 未落路线距离恒 None
        if ver == "v2":
            assert t["from_place"]["place_id"] == "pid_home"
            assert t["to_place"]["place_id"] == "pid_work"
        else:
            assert t["from_place"] is None and t["to_place"] is None  # v1 无 PlaceRef

    def test_current_known_from_place_id_join(self, sema_pair):
        """current_known 的 label/poi 语义跨版本一致（v2 成员网格不漂移）。"""
        conn, ver = sema_pair
        c = fc.build(conn=conn, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
        ck = c["current_known"]
        assert ck["label"] == "某某路2号〔公司〕" and ck["poi"] == "乙大厦"
        assert ck["user_tag"] == "公司" and ck["place_name"] == "某某路2号"
        if ver == "v2":
            assert ck["place_id"] == "pid_work"
        else:
            assert ck["place_id"] in ("", None)

    def test_stay_minutes_buckets(self, sema_db):
        c = fc.build(conn=sema_db, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
        assert c["stay_minutes"]["家"] == 480
        assert c["stay_minutes"]["公司"] == 540

    def test_place_brief_visits_present(self, sema_pair):
        """旧字段 PlaceBrief.visits 在两版本都存在且为 int（计数语义见 Task 7 扩展）。"""
        conn, ver = sema_pair
        c = fc.build(conn=conn, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
        labels = [p["label"] for p in c["places"]]
        assert labels == ["某某路1号〔家〕", "某某路2号〔公司〕"]
        for p in c["places"]:
            assert isinstance(p["visits"], int)
        p0 = c["places"][0]
        assert p0["point_count"] == (100 if ver == "v2" else 30)
        if ver == "v2":
            assert p0["place_id"] == "pid_home"
            assert p0["visit_episodes"] == 2 and p0["stay_ms"] == 61_200_000

    def test_compact_rendered_both_versions(self, sema_db):
        c = fc.build(conn=sema_db, day=DAY, device_id="dev1", now_ms=NOW_MS, detail="full")
        assert c["compact"]
        assert "家" in c["compact"] or "公司" in c["compact"]


# ---------------------------------------------------------------------------
# persona：v1/v2 输出键结构一致
# ---------------------------------------------------------------------------

class TestPersonaShape:
    def test_key_set_identical(self, sema_db):
        p = persona.build(conn=sema_db, device_id="dev1", days=7)
        assert set(p)


# ---------------------------------------------------------------------------
# report：profile JSON 键结构 v1/v2 递归一致
# ---------------------------------------------------------------------------

def _paths(obj, prefix=""):
    """递归提取 JSON 键路径（列表取首元素），用于结构（而非值）对比。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _paths(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list) and obj:
        yield from _paths(obj[0], f"{prefix}[]")
    else:
        yield prefix


class TestReportProfileShape:
    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch, tmp_path):
        from gacore.langTrack import weather
        monkeypatch.setattr(weather, "get_weather", lambda day: {})
        monkeypatch.setattr(rpt, "DB_PATH", tmp_path / "langTrack.db")
        monkeypatch.setattr(
            rpt, "build_persona",
            lambda conn=None, device_id=None, days=7, db_path=None: {"available": False},
        )
        self.tmp = tmp_path

    def _profile(self, conn) -> dict:
        rpt.report(conn, DAY, device_id="dev1")
        p = self.tmp / "profiles" / f"langTrack_profile_{DAY}.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_profile_paths_identical(self, sema_pair):
        conn, ver = sema_pair
        profile = self._profile(conn)
        assert REPORT_PROFILE_TOP_KEYS <= set(profile)
        assert profile["device_id"] == "dev1"

        other = sqlite3.connect(":memory:")
        other.row_factory = sqlite3.Row
        (_seed_v1 if ver == "v2" else _seed_v2)(other)
        try:
            profile_other = self._profile(other)
        finally:
            other.close()
        assert sorted(_paths(profile)) == sorted(_paths(profile_other))

    def test_scenes_labels_consistent(self, sema_db):
        profile = self._profile(sema_db)
        assert [s["label"] for s in profile["scenes"]] == ["家", "公司"]
        assert profile["anomalies"] == [
            {"kind": "new_place", "poi": "乙大厦",
             "detail": "首次到访新地点：乙大厦（访问 1 次）"},
        ]
