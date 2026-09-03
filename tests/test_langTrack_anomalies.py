"""test_langTrack_anomalies.py —— Task 5c anomalies/候选推断设备隔离测试。

覆盖（计划 Task 5c 清单）：
- anomalies 唯一键加入 device_id：旧表迁移保留历史行，跨设备同日同类同网格不再互相吞行；
- detect_anomalies 家/公司按 (device_id, 地点键) 匹配：他设备的家不算自己的家，
  late_night_out / off_schedule 均按设备独立评估，poi 查名带 device_id；
- v2（user_version>=2）经 place_id 关联：stay 的 grid_key 只是 place 成员网格
  （≠代表网格）时不再误报深夜在外；new_place 写 place_id；
- infer_home_work_candidates 全 (device_id, grid_key) 键：两设备同网格统计互不
  混算，已确认标签不串扰他设备行；
- detect_route_changes 跨设备同指纹不再吞行；
- apply_labels v2 标签行带 device_id 时设备隔离更新。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from gacore.langTrack import etl, storage
from gacore.langTrack import location_migration as lm
from gacore.langTrack.label_places import apply_labels

_TZ = datetime.timezone(datetime.timedelta(hours=8))
HOME = (31.992, 118.783)
HOME_GK = "31.992,118.783"
WORK = (31.998, 118.790)
WORK_GK = "31.998,118.790"
NEUTRAL = (31.980, 118.800)
NEUTRAL_GK = "31.980,118.800"
BASE = int(datetime.datetime(2026, 8, 17, 8, 0, tzinfo=_TZ).timestamp() * 1000)


def _ts(day: str, hh: int, mm: int) -> int:
    d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=hh, minute=mm, tzinfo=_TZ)
    return int(d.timestamp() * 1000)


@pytest.fixture
def anomaly_env(monkeypatch):
    """固定 ETL 配置为 DEFAULTS，隔离仓库 data/ 下的用户配置。"""
    from gacore.langTrack import etl_config
    monkeypatch.setattr(
        etl_config, "load_etl_config",
        lambda: json.loads(json.dumps(etl_config.DEFAULTS)),
    )


def _ins_ev(cur, device_id: str, ts: int, lat: float, lon: float, idx: list):
    cur.execute(
        "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
        (idx[0], device_id, ts, "location",
         json.dumps({"lat": lat, "lon": lon, "acc": 20, "provider": "gps"}), ts),
    )
    idx[0] += 1


def _v1_db(path: Path) -> None:
    """v1 库：两设备 places/stays（dev1 家/公司已确认，dev2 同家网格未确认）。"""
    conn = sqlite3.connect(path)
    conn.executescript(storage._SCHEMA)
    conn.executescript(etl._SCHEMA)
    conn.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, last_seen, "
        "visit_count, is_primary) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", HOME_GK, *HOME, "家", BASE, BASE, 10, 1),
            ("dev1", WORK_GK, *WORK, "公司", BASE, BASE, 10, 1),
            ("dev2", HOME_GK, *HOME, "未知", BASE, BASE, 10, 1),
        ],
    )
    conn.commit()
    conn.close()


def _add_stay(path: Path, device_id: str, day: str, hh: int, mm: int,
              dur_min: int, lat: float, lon: float, grid: str):
    start = _ts(day, hh, mm)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
        "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, grid_key, day) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (device_id, start, start + dur_min * 60000, dur_min * 60000, lat, lon,
         lat, lon, lat, lon, 3, 10.0, grid, day),
    )
    conn.commit()
    conn.close()


def _kinds(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            tuple(r) for r in conn.execute(
                "SELECT day, kind, device_id FROM anomalies ORDER BY day, kind, device_id"
            )
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 唯一键迁移
# ---------------------------------------------------------------------------

class TestMigrateAnomaliesUnique:
    def _old_anomalies(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE anomalies")
        conn.execute(
            """
            CREATE TABLE anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                kind TEXT NOT NULL,
                device_id TEXT NOT NULL,
                grid_key TEXT,
                poi TEXT,
                detail TEXT,
                ts INTEGER,
                UNIQUE(day, kind, grid_key)
            )
            """
        )
        conn.execute(
            "INSERT INTO anomalies(id, day, kind, device_id, grid_key, poi, detail, ts) "
            "VALUES (1, '2026-08-17', 'late_night_out', 'dev1', '31.9,118.7', 'x', 'd', 1)"
        )
        # 复现真实生产 v1 库：旧表自带 day 索引（随 RENAME 占名，曾致迁移后索引丢失）
        conn.execute("CREATE INDEX idx_anomalies_day ON anomalies(day)")
        conn.commit()
        conn.close()

    def test_migrates_old_unique_and_keeps_rows(self, tmp_path, anomaly_env):
        """旧 UNIQUE(day,kind,grid_key) 表迁移后：历史行/ id 保留，新键生效。"""
        path = tmp_path / "lt.db"
        _v1_db(path)
        self._old_anomalies(path)

        conn = sqlite3.connect(path)
        etl._migrate_anomalies_unique(conn)
        conn.close()

        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT id, day, kind, device_id FROM anomalies").fetchall()
        # 跨设备同日同类同网格：新键下两行并存（旧键会吞掉第二行）
        conn.execute(
            "INSERT INTO anomalies(day, kind, device_id, grid_key, poi, detail, ts) "
            "VALUES ('2026-08-17', 'late_night_out', 'dev2', '31.9,118.7', 'x', 'd', 2)"
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM anomalies WHERE kind='late_night_out'"
        ).fetchone()[0]
        conn.close()
        assert rows == [(1, "2026-08-17", "late_night_out", "dev1")]
        assert n == 2

    def test_migration_keeps_day_index(self, tmp_path, anomaly_env):
        """旧表迁移后 idx_anomalies_day 必须落在新表上（曾因重命名索引占名被丢）。"""
        path = tmp_path / "lt.db"
        _v1_db(path)
        self._old_anomalies(path)

        conn = sqlite3.connect(path)
        etl._migrate_anomalies_unique(conn)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_anomalies_day' AND tbl_name='anomalies'"
        ).fetchall()
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM anomalies WHERE day='2026-08-17'"
        ).fetchall()
        conn.close()
        assert idx == [("idx_anomalies_day",)]
        assert any("idx_anomalies_day" in str(r) for r in plan)

    def test_idempotent_and_new_schema_skipped(self, tmp_path, anomaly_env):
        """新 schema（_SCHEMA 已含 device_id 键）与二次迁移均为 no-op。"""
        path = tmp_path / "lt.db"
        _v1_db(path)  # etl._SCHEMA 建表即新键
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO anomalies(id, day, kind, device_id, grid_key, poi, detail, ts) "
            "VALUES (5, '2026-08-17', 'off_schedule', 'dev1', '', '', 'd', 1)"
        )
        conn.commit()
        etl._migrate_anomalies_unique(conn)
        etl._migrate_anomalies_unique(conn)
        rows = conn.execute("SELECT id FROM anomalies").fetchall()
        conn.close()
        assert rows == [(5,)]

    def test_v2_frozen_schema_skipped(self, tmp_path, anomaly_env):
        """user_version>=2：schema 冻结（其唯一索引本含 device_id），迁移跳过。"""
        path = tmp_path / "lt.db"
        _v1_db(path)
        self._old_anomalies(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version=2")
        etl._migrate_anomalies_unique(conn)
        # 旧键未动：跨设备同键插入仍被吞（迁移未执行）
        conn.execute(
            "INSERT OR IGNORE INTO anomalies(day, kind, device_id, grid_key, poi, detail, ts) "
            "VALUES ('2026-08-17', 'late_night_out', 'dev2', '31.9,118.7', 'x', 'd', 2)"
        )
        n = conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        conn.close()
        assert n == 1


# ---------------------------------------------------------------------------
# v1：detect_anomalies 设备隔离
# ---------------------------------------------------------------------------

class TestDetectAnomaliesV1:
    def test_late_night_home_isolation(self, tmp_path, anomaly_env):
        """他设备的家不算自己的家；跨设备同日同类同网格异常并存。"""
        path = tmp_path / "lt.db"
        _v1_db(path)
        # 周一深夜：dev1 在自家（不报）；dev2 在 dev1 的家网格（报）；
        # 两人同在中立网格（同日同类同网格 → 旧唯一键会吞掉第二行）
        _add_stay(path, "dev1", "2026-08-17", 23, 30, 60, *HOME, HOME_GK)
        _add_stay(path, "dev2", "2026-08-17", 23, 30, 60, *HOME, HOME_GK)
        _add_stay(path, "dev1", "2026-08-17", 23, 50, 30, *NEUTRAL, NEUTRAL_GK)
        _add_stay(path, "dev2", "2026-08-17", 23, 50, 30, *NEUTRAL, NEUTRAL_GK)

        conn = sqlite3.connect(path)
        etl.detect_anomalies(conn)
        conn.close()

        rows = [r for r in _kinds(path) if r[1] == "late_night_out"]
        assert rows == [
            ("2026-08-17", "late_night_out", "dev1"),
            ("2026-08-17", "late_night_out", "dev2"),
            ("2026-08-17", "late_night_out", "dev2"),
        ]

    def test_off_schedule_per_device(self, tmp_path, anomaly_env):
        """dev1 正午在公司、dev2 正午不在：仅 dev2 报 off_schedule（同日并存）。"""
        path = tmp_path / "lt.db"
        _v1_db(path)
        # 2026-08-17 周一：dev1 09:00-18:00 在公司；dev2 09:00-18:00 在家网格
        _add_stay(path, "dev1", "2026-08-17", 9, 0, 540, *WORK, WORK_GK)
        _add_stay(path, "dev2", "2026-08-17", 9, 0, 540, *HOME, HOME_GK)

        conn = sqlite3.connect(path)
        etl.detect_anomalies(conn)
        conn.close()

        rows = [r for r in _kinds(path) if r[1] == "off_schedule"]
        assert rows == [("2026-08-17", "off_schedule", "dev2")]

    def test_place_name_device_scoped(self, tmp_path, anomaly_env):
        """poi 查名带 device_id：dev1 的 geocode 不串到 dev2 同网格地点。"""
        path = tmp_path / "lt.db"
        _v1_db(path)
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE places SET poi='甲小区' WHERE device_id='dev1' AND grid_key=?", (HOME_GK,)
        )
        conn.commit()
        conn.close()
        _add_stay(path, "dev2", "2026-08-17", 23, 30, 60, *HOME, HOME_GK)

        conn = sqlite3.connect(path)
        etl.detect_anomalies(conn)
        conn.close()

        conn = sqlite3.connect(path)
        poi = conn.execute(
            "SELECT poi FROM anomalies WHERE device_id='dev2' AND kind='late_night_out'"
        ).fetchone()[0]
        conn.close()
        # dev2 的地点无 geocode：落回 grid_key，不串用 dev1 的"甲小区"
        assert poi == HOME_GK


# ---------------------------------------------------------------------------
# v2：place_id 关联
# ---------------------------------------------------------------------------

def _make_v2_source(path: Path) -> None:
    """v2 迁移源库：dev1 夜间在家 + 白天在公司；dev2 夜间在 dev1 的家网格。"""
    conn = sqlite3.connect(path)
    conn.executescript(storage._SCHEMA)
    conn.executescript(etl._SCHEMA)
    cur = conn.cursor()
    idx = [1]
    # dev1 家停留（23:00-23:25，≥10min 成段）
    for k in range(6):
        _ins_ev(cur, "dev1", _ts("2026-08-17", 23, 0) + k * 300_000, *HOME, idx)
    # dev1 公司停留（12:30-13:15，覆盖正午 13:00）
    for k in range(10):
        _ins_ev(cur, "dev1", _ts("2026-08-17", 12, 30) + k * 300_000, *WORK, idx)
    # dev2 夜间停留在 dev1 的家网格（dev2 未标家 → 异常）
    for k in range(6):
        _ins_ev(cur, "dev2", _ts("2026-08-17", 23, 30) + k * 300_000, *HOME, idx)
    conn.commit()
    conn.close()


@pytest.fixture
def v2_db(tmp_path, anomaly_env, monkeypatch):
    """已激活位置事实 v2 的库（shadow → prepare → activate → finalize）。"""
    path = tmp_path / "lt.db"
    _make_v2_source(path)
    labels = tmp_path / "place_labels.json"
    labels.write_text(
        json.dumps({
            "version": 2,
            "labels": [
                {"device_id": "dev1", "grid_key": HOME_GK, "tag": "家"},
                {"device_id": "dev1", "grid_key": WORK_GK, "tag": "公司"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    from gacore.langTrack import label_places
    monkeypatch.setattr(label_places, "CONFIG_PATH", labels)

    lm.build_location_shadow(path)
    report = lm.prepare_location_migration(path, labels_path=labels, run_id="t1")
    conn = sqlite3.connect(path)
    lm.activate_location_v2(conn, "t1", pending_labels_path=report["pending_labels_path"])
    conn.close()
    lm.finalize_label_swap(path, labels_path=labels)
    # 迁移后首次 ETL 同款收尾：v3 标签文件 → v2 places.label（经 (device,place_id)）
    apply_labels(path)
    return path


class TestDetectAnomaliesV2:
    def test_home_matched_by_place_id_not_grid(self, v2_db, anomaly_env):
        """v2 家判定走 place_id：stay 的 grid_key（成员网格）≠家代表网格也不误报。

        v1 逻辑按 grid_key 匹配会把成员网格的夜间停留误报为深夜在外。
        """
        conn = sqlite3.connect(v2_db)
        home_pid = conn.execute(
            "SELECT place_id FROM places WHERE device_id='dev1' AND label='家'"
        ).fetchone()[0]
        # 模拟成员网格：stay 自身 grid_key 与家代表网格不同，但 place_id 指向家
        conn.execute(
            "UPDATE stays SET grid_key='31.950,118.750' "
            "WHERE device_id='dev1' AND place_id=?", (home_pid,)
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(v2_db)
        etl.detect_anomalies(conn)
        conn.close()

        rows = [r for r in _kinds(v2_db) if r[1] == "late_night_out"]
        # dev1 在家（place_id 匹配）→ 不报；dev2 在"别人的"家网格 → 报
        assert [r[2] for r in rows] == ["dev2"]

    def test_new_place_writes_place_id(self, v2_db, anomaly_env):
        """v2 new_place 行带 place_id（引用 canonical place，不再裸 grid 定位）。"""
        now_ms = int(time.time() * 1000)
        conn = sqlite3.connect(v2_db)
        conn.execute(
            "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, "
            "first_seen, visit_count) "
            "VALUES ('dev2','newpid1','31.970,118.770',31.970,118.770,'未知',?,2)",
            (now_ms - 86400000,),
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(v2_db)
        etl.detect_anomalies(conn)
        conn.close()

        conn = sqlite3.connect(v2_db)
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT a.place_id, p.place_id AS formal_pid FROM anomalies a "
            "LEFT JOIN places p ON p.device_id=a.device_id AND p.place_id=a.place_id "
            "WHERE a.kind='new_place'"
        ).fetchone()
        conn.close()
        assert r is not None
        assert r["place_id"] == "newpid1"
        assert r["formal_pid"] == "newpid1"  # 无孤儿引用

    def test_new_place_threshold_uses_point_count(self, v2_db, anomaly_env):
        """v2 阈值对齐 v1 语义（原始点数≤3）：visit_count=2 但 point_count 大不报。"""
        now_ms = int(time.time() * 1000)
        conn = sqlite3.connect(v2_db)
        conn.execute(
            "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, "
            "first_seen, point_count, visit_count) "
            "VALUES ('dev2','pid_few_pts','31.960,118.760',31.960,118.760,'未知',?,2,2)",
            (now_ms - 86400000,),
        )
        # 大地点：visit_count=2（≤3，若误用会报）但原始点数 50
        conn.execute(
            "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, "
            "first_seen, point_count, visit_count) "
            "VALUES ('dev2','pid_many_pts','31.965,118.765',31.965,118.765,'未知',?,50,2)",
            (now_ms - 86400000,),
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(v2_db)
        etl.detect_anomalies(conn)
        conn.close()

        conn = sqlite3.connect(v2_db)
        pids = [r[0] for r in conn.execute(
            "SELECT place_id FROM anomalies WHERE kind='new_place'"
        )]
        conn.close()
        assert "pid_few_pts" in pids
        assert "pid_many_pts" not in pids

    def test_off_schedule_uses_place_id(self, v2_db, anomaly_env):
        """v2 off_schedule：dev1 正午在公司 place → 不报；dev2 报。"""
        conn = sqlite3.connect(v2_db)
        etl.detect_anomalies(conn)
        conn.close()

        rows = [r for r in _kinds(v2_db) if r[1] == "off_schedule"]
        assert [r[2] for r in rows] == ["dev2"]

    def test_idempotent_double_run(self, v2_db, anomaly_env):
        """v2 连跑两次 detect_anomalies：行数与内容稳定（DELETE+重算）。"""
        conn = sqlite3.connect(v2_db)
        n1 = etl.detect_anomalies(conn)
        first = _kinds(v2_db)
        n2 = etl.detect_anomalies(conn)
        conn.close()
        assert n1 == n2
        assert _kinds(v2_db) == first


# ---------------------------------------------------------------------------
# infer_home_work_candidates 设备隔离
# ---------------------------------------------------------------------------

class TestInferHomeWork:
    def test_same_grid_stats_isolated(self, tmp_path, anomaly_env):
        """同网格两设备：凌晨天数/白天次数各自累计（dev2 不蹭 dev1 的凌晨数据）。"""
        path = tmp_path / "lt.db"
        conn = sqlite3.connect(path)
        conn.executescript(storage._SCHEMA)
        conn.executescript(etl._SCHEMA)
        cur = conn.cursor()
        idx = [1]
        # dev1：三个凌晨在家网格 → 家候选；dev2：同网格仅白天一次路过
        for day in ("2026-08-12", "2026-08-13", "2026-08-14"):
            for k in range(2):
                _ins_ev(cur, "dev1", _ts(day, 1, 0) + k * 300_000, *HOME, idx)
        _ins_ev(cur, "dev2", _ts("2026-08-17", 14, 0), *HOME, idx)
        cur.executemany(
            "INSERT INTO places(device_id, grid_key, lat, lon, label) VALUES (?,?,?,?,?)",
            [("dev1", HOME_GK, *HOME, "未知"),
             ("dev2", HOME_GK, *HOME, "未知")],
        )
        conn.commit()
        etl.infer_home_work_candidates(conn)
        rows = {
            r[0]: tuple(r[1:]) for r in conn.execute(
                "SELECT device_id, candidate_label, confidence_home, confidence_work "
                "FROM places"
            )
        }
        conn.close()
        # dev1：home_conf=3/3=1.0 → 候选家；dev2：自身无凌晨数据 → 无候选、置信 0
        assert rows["dev1"] == ("家", 1.0, 0.0)
        assert rows["dev2"] == (None, 0.0, 0.0)

    def test_confirmed_label_device_scoped(self, tmp_path, anomaly_env):
        """dev1 已确认家（无自身事件）：候选回填只落 dev1 行，dev2 不被串标。"""
        path = tmp_path / "lt.db"
        conn = sqlite3.connect(path)
        conn.executescript(storage._SCHEMA)
        conn.executescript(etl._SCHEMA)
        conn.executemany(
            "INSERT INTO places(device_id, grid_key, lat, lon, label) VALUES (?,?,?,?,?)",
            [("dev1", HOME_GK, *HOME, "家"),
             ("dev2", HOME_GK, *HOME, "未知")],
        )
        conn.commit()
        etl.infer_home_work_candidates(conn)
        rows = dict(conn.execute("SELECT device_id, candidate_label FROM places"))
        conn.close()
        assert rows == {"dev1": "家", "dev2": None}


# ---------------------------------------------------------------------------
# detect_route_changes 跨设备
# ---------------------------------------------------------------------------

class TestDetectRouteChanges:
    def test_cross_device_not_swallowed(self, tmp_path, anomaly_env):
        """两设备同日同路线指纹：两行并存（旧唯一键吞掉第二个设备）。"""
        path = tmp_path / "lt.db"
        _v1_db(path)
        # 第二段路线两人同指纹 rkSAMEKE；终点各去不同处避免"往返对"豁免
        conn = sqlite3.connect(path)
        conn.executemany(
            "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "
            "end_lat, end_lon, dist_m, n_points, day, route_key, route_mode) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("dev1", _ts("2026-08-17", 8, 0), _ts("2026-08-17", 8, 30), 1_800_000,
                 31.99, 118.78, 31.998, 118.79, 1000.0, 5, "2026-08-17", "rkAAAAAA", "driving"),
                ("dev1", _ts("2026-08-17", 18, 0), _ts("2026-08-17", 18, 30), 1_800_000,
                 31.998, 118.79, 31.99, 118.80, 1000.0, 5, "2026-08-17", "rkSAMEKEY", "driving"),
                ("dev2", _ts("2026-08-17", 8, 0), _ts("2026-08-17", 8, 30), 1_800_000,
                 31.99, 118.78, 31.998, 118.79, 1000.0, 5, "2026-08-17", "rkBBBBBB", "driving"),
                ("dev2", _ts("2026-08-17", 18, 0), _ts("2026-08-17", 18, 30), 1_800_000,
                 31.998, 118.79, 31.99, 118.80, 1000.0, 5, "2026-08-17", "rkSAMEKEY", "driving"),
            ],
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(path)
        etl._migrate_anomalies_unique(conn)
        etl.detect_route_changes(conn)
        conn.close()

        conn = sqlite3.connect(path)
        devs = sorted(r[0] for r in conn.execute(
            "SELECT device_id FROM anomalies WHERE kind='route_change'"
        ))
        conn.close()
        assert devs == ["dev1", "dev2"]


# ---------------------------------------------------------------------------
# apply_labels v2 标签行设备隔离
# ---------------------------------------------------------------------------

class TestApplyLabelsV2Rows:
    def test_device_scoped_update(self, tmp_path, anomaly_env, monkeypatch):
        """v2 标签行（device_id+grid_key）：只更新指定设备的同网格地点。"""
        path = tmp_path / "lt.db"
        _v1_db(path)
        # dev1 在家网格已标"公司"（与 dev2 的目标标签不同，可观察隔离）
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE places SET label='公司' WHERE device_id='dev1' AND grid_key=?", (HOME_GK,)
        )
        conn.commit()
        conn.close()
        labels = tmp_path / "place_labels.json"
        labels.write_text(
            json.dumps({
                "version": 2,
                "labels": [{"device_id": "dev2", "grid_key": HOME_GK, "tag": "家"}],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        from gacore.langTrack import label_places
        monkeypatch.setattr(label_places, "CONFIG_PATH", labels)

        n = apply_labels(path)

        conn = sqlite3.connect(path)
        rows = dict(conn.execute(
            "SELECT device_id, label FROM places WHERE grid_key=?", (HOME_GK,)
        ))
        conn.close()
        assert n == 1
        assert rows == {"dev1": "公司", "dev2": "家"}


# ---------------------------------------------------------------------------
# Task 12：off_schedule 跨天 stay 口径（覆盖日分组）
# ---------------------------------------------------------------------------

class TestOffScheduleCrossDay:
    def test_v1_cross_day_work_stay_covers_next_noon(self, tmp_path, anomaly_env):
        """跨天公司 stay（起始日 08-17，覆盖 08-18 正午）不得在 08-18 误报缺席。

        旧口径按 stays.day（起始日）分组：08-18 组只有当晚家中停留，
        正午查不到公司 → 误报 off_schedule，与 fact_card 时间线
        （窗口裁剪后显示"公司 00:00-16:25"）自相矛盾。
        """
        path = tmp_path / "lt.db"
        _v1_db(path)
        # 08-17（周一）20:00 起的公司停留，跨午夜至 08-18 16:00（覆盖 08-18 正午）
        _add_stay(path, "dev1", "2026-08-17", 20, 0, 1200, *WORK, WORK_GK)
        # 08-18 当天确有其他停留（家中 20:00-21:00），保证"当天有停驻"前提成立
        _add_stay(path, "dev1", "2026-08-18", 20, 0, 60, *HOME, HOME_GK)

        conn = sqlite3.connect(path)
        etl.detect_anomalies(conn)
        conn.close()

        rows = [r for r in _kinds(path) if r[1] == "off_schedule" and r[2] == "dev1"]
        assert rows == [("2026-08-17", "off_schedule", "dev1")]

    def test_v2_cross_day_work_stay_covers_next_noon(self, v2_db, anomaly_env):
        """v2 同口径：place_id 指向公司的跨天 stay 覆盖次日正午 → 不误报。"""
        conn = sqlite3.connect(v2_db)
        work_pid = conn.execute(
            "SELECT place_id FROM places WHERE device_id='dev1' AND label='公司'"
        ).fetchone()[0]
        home_pid = conn.execute(
            "SELECT place_id FROM places WHERE device_id='dev1' AND label='家'"
        ).fetchone()[0]
        start = _ts("2026-08-17", 20, 0)
        end = _ts("2026-08-18", 16, 0)
        conn.execute(
            "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
            "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, "
            "grid_key, place_id, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dev1", start, end, end - start, *WORK, *WORK, *WORK, 3, 10.0, WORK_GK,
             work_pid, "2026-08-17"),
        )
        h0 = _ts("2026-08-18", 20, 0)
        conn.execute(
            "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
            "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, "
            "grid_key, place_id, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dev1", h0, h0 + 3600000, 3600000, *HOME, *HOME, *HOME, 3, 10.0, HOME_GK,
             home_pid, "2026-08-18"),
        )
        conn.commit()
        etl.detect_anomalies(conn)
        conn.close()

        rows = [r for r in _kinds(v2_db) if r[1] == "off_schedule" and r[2] == "dev1"]
        assert ("2026-08-18", "off_schedule", "dev1") not in rows
