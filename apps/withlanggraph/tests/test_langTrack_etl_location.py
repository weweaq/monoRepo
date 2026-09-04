"""test_langTrack_etl_location.py —— Task 3 shadow 全量位置事实测试。

覆盖（计划 Task 3 清单）：
- --location-shadow 只写 shadow 表，不修改正式 places/stays/anomalies/trips 和标签文件；
- 全量从 events 重建 shadow_stays_v2；跨午夜 stay 不按 day 截断；
- canonical places 三项统计（point_count/visit_count/stay_ms）语义明确，禁止累加旧 visit_count；
- stays.place_id 回填所属 canonical place，无孤儿引用；
- trips 重建写 from/to_place_id 与 endpoint_coord_system；
- 旧 trips 缓存按 (device_id,start_ts,end_ts) 精确匹配迁移，无匹配不迁移；
- 连续运行两次除时间审计列外内容一致（幂等）；
- 两设备相同 grid 隔离、路过点不成为 place、每设备 top2 is_primary；
- incremental 参数明确记录 "location v2 full rebuild"；
- 坐标制从配置解析（设备区间），unknown 不猜测。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from gacore.langTrack import etl, storage
from gacore.langTrack import location_migration as lm

_TZ = datetime.timezone(datetime.timedelta(hours=8))
HOME = (31.992, 118.783)
WORK = (31.998, 118.790)
PASS_THROUGH = (31.980, 118.800)
BASE = int(datetime.datetime(2026, 8, 17, 8, 0, tzinfo=_TZ).timestamp() * 1000)


def _ts(day: str, hh: int, mm: int) -> int:
    d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=hh, minute=mm, tzinfo=_TZ)
    return int(d.timestamp() * 1000)


def _ins_loc(cur, device_id: str, ts: int, lat: float, lon: float,
             acc=None, provider: str = "gps", idx: list | None = None):
    idx = idx if idx is not None else [0]
    cur.execute(
        "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
        (idx[0], device_id, ts, "location",
         json.dumps({"lat": lat, "lon": lon, "acc": acc, "provider": provider}), ts),
    )
    idx[0] += 1


def _place_id_of(device_id: str, grid_key: str) -> str:
    """§2.3 规则 9：sha1(device_id|grid_keys)[:16]（单网格 place）。"""
    return hashlib.sha1(f"{device_id}|{grid_key}".encode()).hexdigest()[:16]


@pytest.fixture
def shadow_env(monkeypatch):
    """固定 ETL/坐标制配置为 DEFAULTS，隔离仓库 data/ 下未来可能出现的用户配置。"""
    from gacore.langTrack import etl_config
    monkeypatch.setattr(
        etl_config, "load_etl_config",
        lambda: json.loads(json.dumps(etl_config.DEFAULTS)),
    )
    monkeypatch.setattr(
        etl_config, "load_coord_systems",
        lambda: {"default": "unknown", "periods": []},
    )


@pytest.fixture
def v1_db(tmp_path):
    """v1 库：两设备 + 通勤 + 跨午夜 + 路过点事件，含旧 trips 缓存与样本正式表数据。"""
    path = tmp_path / "lt.db"
    conn = sqlite3.connect(path)
    conn.executescript(storage._SCHEMA)  # events 等接收层表
    conn.executescript(etl._SCHEMA)     # ETL 事实表
    cur = conn.cursor()
    idx = [1]

    # dev1 home1：08:00-09:00（7 点 @10min）
    for k in range(7):
        _ins_loc(cur, "dev1", BASE + k * 600_000, *HOME, acc=20, idx=idx)
    # dev1 通勤采样（gap 内 3 点 + 工作首点作 trip 终点）
    _ins_loc(cur, "dev1", BASE + 3_900_000, 31.9935, 118.785, acc=None, provider="network", idx=idx)
    _ins_loc(cur, "dev1", BASE + 4_350_000, 31.995, 118.7865, acc=None, provider="network", idx=idx)
    _ins_loc(cur, "dev1", BASE + 4_800_000, 31.9965, 118.788, acc=None, provider="network", idx=idx)
    # dev1 work：含 w0（trip 终点，非 stay 起点）
    for k in range(13):
        _ins_loc(cur, "dev1", BASE + 5_100_000 + k * 600_000, *WORK, acc=45, idx=idx)
    # dev1 路过点（5min 间隔 → 不构成 stay）
    _ins_loc(cur, "dev1", BASE + 20_000_000, *PASS_THROUGH, idx=idx)
    _ins_loc(cur, "dev1", BASE + 20_300_000, *PASS_THROUGH, idx=idx)
    # dev1 home2：跨午夜 23:50-00:30（5 点 @10min）
    for k in range(5):
        _ins_loc(cur, "dev1", _ts("2026-08-18", 23, 50) + k * 600_000, *HOME, acc=20, idx=idx)
    # dev2 home：与 dev1 同 grid
    for k in range(7):
        _ins_loc(cur, "dev2", BASE + k * 600_000, *HOME, acc=None, provider="network", idx=idx)

    # 旧 trips 缓存：一条可精确匹配（dev1, BASE+3900000, BASE+5100000），一条不可匹配
    cur.executemany(
        "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "
        "end_lat, end_lon, dist_m, n_points, day, polyline, route_key, route_mode, route_encoded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", BASE + 3_900_000, BASE + 5_100_000, 1_200_000, 31.9935, 118.785,
             31.998, 118.790, 690.0, 4, "2026-08-17", "enc:abc", "rk_hw", "driving", 999),
            ("dev1", BASE + 100, BASE + 200, 100, 31.99, 118.78, 31.99, 118.78, 0, 0,
             "2026-08-17", "enc:stale", "rk_stale", "driving", 100),
        ],
    )
    # 样本正式表数据（验证 shadow 构建零改动）
    cur.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, last_seen, "
        "visit_count, is_primary) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", "31.992,118.783", *HOME, "家", BASE, BASE + 3_600_000, 30, 1),
            ("dev1", "31.998,118.790", *WORK, "公司", BASE + 5_100_000, BASE + 12_600_000, 25, 1),
        ],
    )
    cur.execute(
        "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, center_lon, "
        "min_lat, min_lon, max_lat, max_lon, n_points, radius_m, grid_key, day) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", BASE, BASE + 3_600_000, 3_600_000, *HOME, 31.991, 118.782, 31.993, 118.784,
         7, 50.0, "31.992,118.783", "2026-08-17"),
    )
    cur.execute(
        "INSERT INTO anomalies(day, kind, device_id, grid_key, poi, detail, ts) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-17", "new_place", "dev1", "31.980,118.800", "某新点", "访问 1 次", BASE + 20_000_000),
    )
    conn.commit()
    conn.close()
    return path


def _rows(conn, table, exclude=("created_at", "updated_at")):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c not in exclude]
    sql = f"SELECT {','.join(keep)} FROM {table} ORDER BY {','.join(keep)}"
    return [tuple(r) for r in conn.execute(sql)]


def _snapshot_formal(conn) -> dict:
    out = {"__user_version__": conn.execute("PRAGMA user_version").fetchone()[0]}
    for t in ("places", "stays", "trips", "anomalies", "route_grids", "grid_pois", "events"):
        out[t] = _rows(conn, t)
    return out


def _label_file_bytes() -> bytes | None:
    p = ROOT / "data" / "place_labels.json"
    return p.read_bytes() if p.exists() else None


class TestShadowBuild:
    def test_shadow_only_writes_shadow_tables(self, v1_db, shadow_env):
        """正式事实表、user_version、标签文件零变化。"""
        conn = sqlite3.connect(v1_db)
        before = _snapshot_formal(conn)
        conn.close()
        labels_before = _label_file_bytes()

        lm.build_location_shadow(v1_db)

        conn = sqlite3.connect(v1_db)
        after = _snapshot_formal(conn)
        conn.close()
        assert after == before
        assert _label_file_bytes() == labels_before

    def test_full_rebuild_and_cross_midnight(self, v1_db, shadow_env):
        n = lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        conn.row_factory = sqlite3.Row
        # dev1 3 段（home1/work/home2）+ dev2 1 段
        assert n == 4
        rows = {
            (r["device_id"], r["start_ts"]): r
            for r in conn.execute("SELECT * FROM shadow_stays_v2")
        }
        # 跨午夜 stay 不截断：23:50 起、次日 00:30 止，day 取起始日
        home2 = rows[("dev1", _ts("2026-08-18", 23, 50))]
        assert home2["end_ts"] == _ts("2026-08-19", 0, 30)
        assert home2["duration_ms"] == 2_400_000
        assert home2["day"] == "2026-08-18"
        conn.close()

    def test_canonical_places_three_counts(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        conn.row_factory = sqlite3.Row
        places = {
            (r["device_id"], r["grid_key"]): r
            for r in conn.execute("SELECT * FROM shadow_places_v2")
        }
        assert len(places) == 3

        home = places[("dev1", "31.992,118.783")]
        work = places[("dev1", "31.998,118.790")]
        home_dev2 = places[("dev2", "31.992,118.783")]

        # visit_count = stay 段数（不是原始点数，不是累加旧 visit_count=30）
        assert home["visit_count"] == 2
        assert work["visit_count"] == 1
        assert home_dev2["visit_count"] == 1
        # stay_ms = 关联 stay 总时长（work stay 从 w0 后首点起算：12 点 × 10min = 6_600_000）
        assert home["stay_ms"] == 3_600_000 + 2_400_000
        assert work["stay_ms"] == 6_600_000
        # point_count = 成员网格内原始点数（含未参与 stay 的 w0 → work 13 点）
        assert home["point_count"] == 12
        assert work["point_count"] == 13
        assert home_dev2["point_count"] == 7
        # place_id = 稳定 cluster key；两设备同 grid 隔离
        assert home["place_id"] == _place_id_of("dev1", "31.992,118.783")
        assert home_dev2["place_id"] == _place_id_of("dev2", "31.992,118.783")
        assert home["place_id"] != home_dev2["place_id"]
        conn.close()

    def test_stay_place_id_backfill_no_orphan(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        orphans = conn.execute(
            "SELECT COUNT(*) FROM shadow_stays_v2 s WHERE s.place_id IS NOT NULL "
            "AND s.place_id NOT IN (SELECT place_id FROM shadow_places_v2 WHERE device_id=s.device_id)"
        ).fetchone()[0]
        nulls = conn.execute(
            "SELECT COUNT(*) FROM shadow_stays_v2 WHERE place_id IS NULL"
        ).fetchone()[0]
        assert orphans == 0
        assert nulls == 0  # 本场景所有 stay 网格都有归属 place
        # place_cells 覆盖全部成员网格
        cells = conn.execute("SELECT COUNT(*) FROM shadow_place_cells_v2").fetchone()[0]
        assert cells == 3
        conn.close()

    def test_trips_with_place_refs_and_cache(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        conn.row_factory = sqlite3.Row
        trips = list(conn.execute("SELECT * FROM shadow_trips_v2"))
        assert len(trips) == 1
        t = trips[0]
        assert t["device_id"] == "dev1"
        assert t["start_ts"] == BASE + 3_900_000
        assert t["end_ts"] == BASE + 5_100_000
        home_id = _place_id_of("dev1", "31.992,118.783")
        work_id = _place_id_of("dev1", "31.998,118.790")
        assert t["from_place_id"] == home_id
        assert t["to_place_id"] == work_id
        assert t["endpoint_coord_system"] == "unknown"
        assert t["n_points"] == 4
        assert t["dist_m"] >= 600  # 端点直距（Haversine）
        # 旧缓存精确匹配迁移；polyline 为高德 GCJ02
        assert t["polyline"] == "enc:abc"
        assert t["polyline_coord_system"] == "gcj02"
        assert t["route_key"] == "rk_hw"
        assert t["route_mode"] == "driving"
        assert t["route_encoded_at"] == 999
        conn.close()

    def test_cache_without_exact_match_dropped(self, v1_db, shadow_env):
        """旧 trips 无精确匹配（start/end 不一致）→ 缓存不迁移。"""
        lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        stale = conn.execute(
            "SELECT COUNT(*) FROM shadow_trips_v2 WHERE polyline='enc:stale'"
        ).fetchone()[0]
        assert stale == 0
        conn.close()

    def test_idempotent_double_run(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        first = {
            t: _rows(conn, t)
            for t in ("shadow_places_v2", "shadow_place_cells_v2",
                      "shadow_stays_v2", "shadow_trips_v2")
        }
        conn.close()

        n2 = lm.build_location_shadow(v1_db)

        conn = sqlite3.connect(v1_db)
        second = {
            t: _rows(conn, t)
            for t in ("shadow_places_v2", "shadow_place_cells_v2",
                      "shadow_stays_v2", "shadow_trips_v2")
        }
        conn.close()
        assert second == first
        assert n2 == 4

    def test_pass_through_no_place(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        # 路过点网格无 stay → 不进入 places/cells（路过不是待过）
        n = conn.execute(
            "SELECT COUNT(*) FROM shadow_places_v2 WHERE grid_key='31.980,118.800'"
        ).fetchone()[0]
        n_cells = conn.execute(
            "SELECT COUNT(*) FROM shadow_place_cells_v2 WHERE grid_key='31.980,118.800'"
        ).fetchone()[0]
        assert n == 0
        assert n_cells == 0
        conn.close()

    def test_top2_is_primary_per_device(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        # dev1：home+work 两处 → 都 primary；dev2：单地点 → 1 个 primary
        dev1_primary = conn.execute(
            "SELECT COUNT(*) FROM shadow_places_v2 WHERE device_id='dev1' AND is_primary=1"
        ).fetchone()[0]
        dev2_primary = conn.execute(
            "SELECT COUNT(*) FROM shadow_places_v2 WHERE device_id='dev2' AND is_primary=1"
        ).fetchone()[0]
        assert dev1_primary == 2
        assert dev2_primary == 1
        conn.close()

    def test_incremental_flag_logs_full_rebuild(self, v1_db, shadow_env, capsys):
        lm.build_location_shadow(v1_db, incremental=True)
        out = capsys.readouterr().out
        assert "location v2 full rebuild" in out
        conn = sqlite3.connect(v1_db)
        last = conn.execute(
            "SELECT mode, status FROM etl_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert last == ("location_v2_full", "done")
        conn.close()

    def test_coord_system_from_config(self, v1_db, shadow_env):
        cfg = {
            "default": "unknown",
            "periods": [
                {"device_id": "dev1", "start_ts": 0, "end_ts": None, "source": "wgs84"},
            ],
        }
        lm.build_location_shadow(v1_db, coord_config=cfg)
        conn = sqlite3.connect(v1_db)
        # dev1 全部事实标记 wgs84；dev2 无 period → unknown
        dev1_stay_sys = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT source_coord_system FROM shadow_stays_v2 WHERE device_id='dev1'"
            )
        }
        dev1_place_sys = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT source_coord_system FROM shadow_places_v2 WHERE device_id='dev1'"
            )
        }
        trip_sys = {
            r[0] for r in conn.execute("SELECT DISTINCT endpoint_coord_system FROM shadow_trips_v2")
        }
        dev2_sys = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT source_coord_system FROM shadow_stays_v2 WHERE device_id='dev2'"
            )
        }
        assert dev1_stay_sys == {"wgs84"}
        assert dev1_place_sys == {"wgs84"}
        assert trip_sys == {"wgs84"}
        assert dev2_sys == {"unknown"}
        # 原始坐标不做转换：home 中心保持源坐标
        home_lat = conn.execute(
            "SELECT lat FROM shadow_places_v2 WHERE device_id='dev1' AND grid_key='31.992,118.783'"
        ).fetchone()[0]
        assert home_lat == pytest.approx(HOME[0])
        conn.close()

    def test_accuracy_stats_on_stays(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        conn = sqlite3.connect(v1_db)
        conn.row_factory = sqlite3.Row
        home1 = conn.execute(
            "SELECT * FROM shadow_stays_v2 WHERE device_id='dev1' AND start_ts=?", (BASE,)
        ).fetchone()
        work = conn.execute(
            "SELECT * FROM shadow_stays_v2 WHERE device_id='dev1' AND start_ts=?",
            (BASE + 5_700_000,),
        ).fetchone()
        dev2 = conn.execute(
            "SELECT * FROM shadow_stays_v2 WHERE device_id='dev2' AND start_ts=?", (BASE,)
        ).fetchone()
        assert home1["accuracy_known_points"] == 7
        assert home1["avg_accuracy_m"] == 20.0
        assert work["accuracy_known_points"] == 12
        assert work["avg_accuracy_m"] == 45.0
        assert dev2["accuracy_known_points"] == 0
        assert dev2["avg_accuracy_m"] is None
        conn.close()


class TestSchemaDdlInvariants:
    """_DAILY_QUALITY_DDL / _SCHEMA 拼接契约（review 回归：DDL 拼进 _SCHEMA 丢分号）。

    - _DAILY_QUALITY_DDL 是单条语句：conn.execute 可直接执行（_write_daily_quality 补建路径）；
    - _SCHEMA 内嵌该 DDL 后仍可 executescript 整体建库，且表齐全（v1 建库路径）。
    """

    def test_quality_ddl_single_statement_execute(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(etl._DAILY_QUALITY_DDL)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "daily_location_quality" in names

    def test_schema_executescript_embeds_quality_table(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(etl._SCHEMA)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"daily_location_quality", "etl_runs", "trips", "places"} <= names


class TestDailyQuality:
    """§3.2 坐标质量日表：shadow 与 v1 管线共表写入（只读聚合，幂等重建）。"""

    def _quality(self, db_path) -> dict:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = {
            (r["day"], r["device_id"]): dict(r)
            for r in conn.execute("SELECT * FROM daily_location_quality")
        }
        conn.close()
        for r in rows.values():
            r.pop("created_at")
            r.pop("updated_at")
        return rows

    def test_shadow_writes_daily_quality(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        q = self._quality(v1_db)
        assert set(q) == {
            ("2026-08-17", "dev1"), ("2026-08-18", "dev1"),
            ("2026-08-19", "dev1"), ("2026-08-17", "dev2"),
        }
        # dev1 08-17：7 home(acc20,gps) + 3 commute(acc缺,network) + 13 work(acc45,gps)
        # + 2 路过(acc缺,gps) = 25 点全部有效解析
        d17 = q[("2026-08-17", "dev1")]
        assert d17["points_total"] == 25
        assert d17["points_valid"] == 25
        assert d17["accuracy_known"] == 20
        assert d17["accuracy_le_50"] == 20
        assert d17["accuracy_51_150"] == 0
        assert d17["accuracy_gt_150"] == 0
        assert json.loads(d17["providers_json"]) == {"gps": 22, "network": 3}
        assert d17["observed_half_hour_bins"] >= 8  # 8:00-19:00 跨多个半小时格
        assert d17["median_interval_sec"] is not None
        # dev1 08-18/08-19：跨午夜 home2 5 点按自然日拆分（23:50 1 点 + 00:00 后 4 点）
        d18 = q[("2026-08-18", "dev1")]
        assert d18["points_total"] == 1
        assert d18["accuracy_known"] == 1
        assert d18["accuracy_le_50"] == 1
        assert json.loads(d18["providers_json"]) == {"gps": 1}
        d19 = q[("2026-08-19", "dev1")]
        assert d19["points_total"] == 4
        assert d19["accuracy_known"] == 4
        assert json.loads(d19["providers_json"]) == {"gps": 4}
        # dev2 08-17：7 点 network 全部缺 accuracy → known=0 只观测不计档
        dev2 = q[("2026-08-17", "dev2")]
        assert dev2["points_total"] == 7
        assert dev2["accuracy_known"] == 0
        assert json.loads(dev2["providers_json"]) == {"network": 7}

    def test_shadow_quality_idempotent(self, v1_db, shadow_env):
        lm.build_location_shadow(v1_db)
        first = self._quality(v1_db)
        lm.build_location_shadow(v1_db)
        assert self._quality(v1_db) == first

    def test_shadow_creates_quality_table_on_old_db(self, v1_db, shadow_env):
        """P1 回归：Task 6 前的旧库（无 daily_location_quality 表）跑 shadow 幂等补建不崩溃。"""
        conn = sqlite3.connect(v1_db)
        conn.execute("DROP TABLE daily_location_quality")
        conn.commit()
        conn.close()

        lm.build_location_shadow(v1_db)

        q = self._quality(v1_db)
        assert q[("2026-08-17", "dev1")]["points_total"] == 25

    def test_incremental_quality_merges_new_events(self, v1_db, shadow_env, monkeypatch):
        """增量模式：load_events 为全量，受影响日质量行合并新旧点（无 stale/漏计）。"""
        from gacore.langTrack import label_places
        monkeypatch.setattr(
            label_places, "CONFIG_PATH", Path(v1_db).parent / "none.json"
        )
        etl.run(v1_db, run_geocode=False, run_route=False, run_poi=False)
        before = self._quality(v1_db)
        assert before[("2026-08-17", "dev1")]["points_total"] == 25

        conn = sqlite3.connect(v1_db)
        _ins_loc(conn.cursor(), "dev1", BASE + 30_000_000, *HOME, acc=20, idx=[1000])
        conn.commit()
        conn.close()

        etl.run(v1_db, incremental=True, run_geocode=False, run_route=False, run_poi=False)

        after = self._quality(v1_db)
        # 08-17 行含新增点（全量重算，非仅增量事件）
        assert after[("2026-08-17", "dev1")]["points_total"] == 26
        # 其余日不残留错误行：全量行集 upsert，与首跑一致；无新增键
        assert set(after) == set(before)
        assert after[("2026-08-19", "dev1")] == before[("2026-08-19", "dev1")]

    def test_v1_run_writes_quality_and_trips_coord_columns(
        self, v1_db, shadow_env, monkeypatch
    ):
        """v1 管线：日质量表落库；trips 落 endpoint/polyline 坐标制两列。"""
        from gacore.langTrack import label_places
        monkeypatch.setattr(
            label_places, "CONFIG_PATH", Path(v1_db).parent / "none.json"
        )
        etl.run(v1_db, run_geocode=False, run_route=False, run_poi=False)

        q = self._quality(v1_db)
        assert q[("2026-08-17", "dev1")]["points_total"] == 25
        assert q[("2026-08-17", "dev2")]["points_total"] == 7

        conn = sqlite3.connect(v1_db)
        conn.row_factory = sqlite3.Row
        trips = list(conn.execute("SELECT * FROM trips"))
        conn.close()
        assert trips
        # 起终点为 source 坐标制（默认 unknown）；带旧缓存的行 polyline 标 GCJ02
        assert {t["endpoint_coord_system"] for t in trips} == {"unknown"}
        cached = [t for t in trips if t["polyline"]]
        assert cached and all(t["polyline_coord_system"] == "gcj02" for t in cached)

        # 连跑两次幂等（审计列外一致）
        etl.run(v1_db, run_geocode=False, run_route=False, run_poi=False)
        assert self._quality(v1_db) == q


class TestShadowAccuracyFilter:
    """§3.1：shadow 几何构建走精度过滤（与 v1 geom_points 同口径），
    point_count / stay accuracy 统计保持原始点（§2.3：point_count 为
    成员网格内原始 location 点数，不等于参与 stay 判定的点数）。"""

    def test_geometry_filtered_counts_raw(self, tmp_path, monkeypatch):
        from gacore.langTrack import etl_config

        cfg = json.loads(json.dumps(etl_config.DEFAULTS))
        cfg["location"]["apply_accuracy_filter"] = True
        cfg["location"]["max_accuracy_m"] = 30.0
        monkeypatch.setattr(etl_config, "load_etl_config", lambda: cfg)
        monkeypatch.setattr(
            etl_config, "load_coord_systems",
            lambda: {"default": "unknown", "periods": []},
        )

        path = tmp_path / "acc.db"
        conn = sqlite3.connect(path)
        conn.executescript(storage._SCHEMA)
        conn.executescript(etl._SCHEMA)
        cur = conn.cursor()
        idx = [1]
        # home 停驻 8 点 @10min，其中第 5 点 acc=500 超阈值（过滤只影响几何）
        for k in range(8):
            acc = 500 if k == 4 else 20
            _ins_loc(cur, "dev1", BASE + k * 600_000, *HOME, acc=acc, idx=idx)
        conn.commit()
        conn.close()

        lm.build_location_shadow(path)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        stays = list(conn.execute("SELECT * FROM shadow_stays_v2"))
        places = list(conn.execute("SELECT * FROM shadow_places_v2"))
        conn.close()

        assert len(stays) == 1
        assert stays[0]["n_points"] == 7                # 几何输入：过滤后 7 点
        assert stays[0]["accuracy_known_points"] == 8   # 统计口径：窗口内原始 8 点
        assert stays[0]["avg_accuracy_m"] == 80.0       # (20*7+500)/8
        assert len(places) == 1
        assert places[0]["point_count"] == 8            # §2.3 原始点数
        assert places[0]["visit_count"] == 1
