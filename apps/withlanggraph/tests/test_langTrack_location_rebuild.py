"""test_langTrack_location_rebuild.py —— Task 5b 激活后全量重建 + etl.run v2 分支守卫。

覆盖：
- rebuild_location_v2 未激活（user_version<2）时显式拒绝，不执行任何构建；
- 重建保留正式表人工 tag 与 geocode 缓存（label/geocode 列不在 UPSERT 统计列内）；
- 中心偏移 > regeo_shift_m 的地点 geocode 缓存失效（§2.5 清空待重编）；
- shadow 中不存在的地点被删除（无 stay 支持 → 事实消失）；
- trips 已编码路线缓存经 shadow 带回，不烧高德配额；
- stays.place_id 无孤儿引用；连续两次重建四张表内容一致（幂等，含 id）；
- etl.run 在 v2 激活库上走 rebuild 分支：v1 位置管线零触碰
  （visit_count 不累加、无 etl_version 列、place_id 语义保持），
  连跑两次位置事实完全一致（P0 累加事故不复发）。
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
from gacore.langTrack import location_migration as lm

_TZ = datetime.timezone(datetime.timedelta(hours=8))
HOME = (31.992, 118.783)
WORK = (31.998, 118.790)
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


def _make_v1_db(path: Path) -> None:
    """v1 库：两设备通勤 + 跨午夜；v1 places/trips 含待迁移 tag 与路线缓存。"""
    conn = sqlite3.connect(path)
    conn.executescript(storage._SCHEMA)
    conn.executescript(etl._SCHEMA)
    cur = conn.cursor()
    idx = [1]
    for k in range(7):
        _ins_loc(cur, "dev1", BASE + k * 600_000, *HOME, acc=20, idx=idx)
    _ins_loc(cur, "dev1", BASE + 3_900_000, 31.9935, 118.785, acc=None, provider="network", idx=idx)
    _ins_loc(cur, "dev1", BASE + 4_350_000, 31.995, 118.7865, acc=None, provider="network", idx=idx)
    _ins_loc(cur, "dev1", BASE + 4_800_000, 31.9965, 118.788, acc=None, provider="network", idx=idx)
    for k in range(13):
        _ins_loc(cur, "dev1", BASE + 5_100_000 + k * 600_000, *WORK, acc=45, idx=idx)
    for k in range(5):
        _ins_loc(cur, "dev1", _ts("2026-08-18", 23, 50) + k * 600_000, *HOME, acc=20, idx=idx)
    for k in range(7):
        _ins_loc(cur, "dev2", BASE + k * 600_000, *HOME, acc=None, provider="network", idx=idx)

    cur.execute(
        "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "
        "end_lat, end_lon, dist_m, n_points, day, polyline, route_key, route_mode, route_encoded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", BASE + 3_900_000, BASE + 5_100_000, 1_200_000, 31.9935, 118.785,
         31.998, 118.790, 690.0, 4, "2026-08-17", "enc:abc", "rk_hw", "driving", 999),
    )
    cur.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, last_seen, "
        "visit_count, is_primary) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", "31.992,118.783", *HOME, "家", BASE, BASE + 3_600_000, 30, 1),
            ("dev1", "31.998,118.790", *WORK, "公司", BASE + 5_100_000, BASE + 12_600_000, 25, 1),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def v2_db(tmp_path, shadow_env, monkeypatch):
    """已激活位置事实 v2 的库：shadow → prepare → activate → finalize 全流程。

    标签文件用 v2 格式（(device_id, grid_key) 定位），避免 v1 平铺在两设备
    shadow 上触发 multi_device_ambiguity 阻断。
    """
    path = tmp_path / "lt.db"
    _make_v1_db(path)
    labels = tmp_path / "place_labels.json"
    labels.write_text(
        json.dumps({
            "version": 2,
            "labels": [{"device_id": "dev1", "grid_key": "31.992,118.783", "tag": "家"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    # etl.run → apply_labels 读取模块级 CONFIG_PATH，指向测试标签文件
    from gacore.langTrack import label_places
    monkeypatch.setattr(label_places, "CONFIG_PATH", labels)

    lm.build_location_shadow(path)
    report = lm.prepare_location_migration(path, labels_path=labels, run_id="t1")
    conn = sqlite3.connect(path)
    lm.activate_location_v2(conn, "t1", pending_labels_path=report["pending_labels_path"])
    conn.close()
    lm.finalize_label_swap(path, labels_path=labels)
    return path


def _snap(conn, table, exclude=("created_at", "updated_at")):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if not cols:
        return []  # 表不存在（如 v1 库无 place_cells）：快照为空，v1/v2 可比
    keep = [c for c in cols if c not in exclude]
    sql = f"SELECT {','.join(keep)} FROM {table} ORDER BY {','.join(keep)}"
    return [tuple(r) for r in conn.execute(sql)]


def _location_snapshot(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return {t: _snap(conn, t) for t in ("places", "place_cells", "stays", "trips")}
    finally:
        conn.close()


def _set_geocode(db_path: Path, device_id: str, place_id: str, poi: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE places SET poi=?, address='某某路1号', district='某区', matched_level='poi', "
        "name_evidence='legacy_cache' WHERE device_id=? AND place_id=?",
        (poi, device_id, place_id),
    )
    conn.commit()
    conn.close()


class TestRebuildGuard:
    def test_rebuild_requires_v2(self, tmp_path, shadow_env):
        """未激活 v2：显式拒绝且不执行 shadow 构建（正式表零变化）。"""
        path = tmp_path / "lt.db"
        _make_v1_db(path)
        before = _location_snapshot(path)
        with pytest.raises(lm.LocationMigrationError, match="user_version"):
            lm.rebuild_location_v2(path)
        conn = sqlite3.connect(path)
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        assert "shadow_places_v2" not in tables
        assert _location_snapshot(path) == before


class TestRebuildV2:
    def test_preserves_labels_and_geocode(self, v2_db, shadow_env):
        """人工 tag 与达标 geocode 缓存在重建后保留（不进 UPSERT 统计列）。"""
        conn = sqlite3.connect(v2_db)
        home_id = conn.execute(
            "SELECT place_id FROM places WHERE device_id='dev1' AND grid_key='31.992,118.783'"
        ).fetchone()[0]
        conn.close()
        _set_geocode(v2_db, "dev1", home_id, "甲小区南门")

        lm.rebuild_location_v2(v2_db)

        conn = sqlite3.connect(v2_db)
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT label, poi, address, district, name_evidence, visit_count "
            "FROM places WHERE device_id='dev1' AND place_id=?", (home_id,)
        ).fetchone()
        conn.close()
        assert r["label"] == "家"
        assert r["poi"] == "甲小区南门"
        assert r["address"] == "某某路1号"
        assert r["name_evidence"] == "legacy_cache"
        # visit_count 语义 = stay 段数（2），v1 累加事故不复现
        assert r["visit_count"] == 2

    def test_invalidates_shifted_geocode(self, v2_db, shadow_env):
        """中心偏移 > 50m：geocode 缓存清空待重编（§2.5），label 仍保留。"""
        conn = sqlite3.connect(v2_db)
        work_id = conn.execute(
            "SELECT place_id FROM places WHERE device_id='dev1' AND grid_key='31.998,118.790'"
        ).fetchone()[0]
        # 正式表中心偏移至 ~800m 外（shadow 重建回到真实中心 → 偏移超阈值）
        conn.execute(
            "UPDATE places SET lat=31.9925, lon=118.7835 WHERE device_id='dev1' AND place_id=?",
            (work_id,),
        )
        conn.commit()
        conn.close()
        _set_geocode(v2_db, "dev1", work_id, "乙大厦")

        lm.rebuild_location_v2(v2_db)

        conn = sqlite3.connect(v2_db)
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT label, poi, address, name_evidence, lat, lon "
            "FROM places WHERE device_id='dev1' AND place_id=?", (work_id,)
        ).fetchone()
        conn.close()
        assert r["poi"] is None
        assert r["address"] is None
        assert r["name_evidence"] == ""
        assert (r["lat"], r["lon"]) == WORK  # 统计列以 shadow 为准
        assert r["label"] == "公司"  # tag 不受缓存失效影响

    def test_removes_vanished_place(self, v2_db, shadow_env):
        """shadow 中不存在的地点（无 stay 支持）被删除。"""
        conn = sqlite3.connect(v2_db)
        conn.execute(
            "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, point_count, "
            "visit_count, stay_ms) VALUES ('dev9','ghostpid','31.0,117.0',31.0,117.0,'家',1,1,1)"
        )
        conn.commit()
        conn.close()

        lm.rebuild_location_v2(v2_db)

        conn = sqlite3.connect(v2_db)
        n = conn.execute(
            "SELECT COUNT(*) FROM places WHERE place_id='ghostpid'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        conn.close()
        assert n == 0
        assert total == 3  # dev1 home/work + dev2 home

    def test_preserves_route_cache_and_no_orphan(self, v2_db, shadow_env):
        """trips 路线缓存经 shadow 带回；stays.place_id 无孤儿引用。"""
        lm.rebuild_location_v2(v2_db)
        conn = sqlite3.connect(v2_db)
        conn.row_factory = sqlite3.Row
        t = conn.execute(
            "SELECT polyline, route_key, route_mode, route_encoded_at FROM trips"
        ).fetchone()
        orphans = conn.execute(
            "SELECT COUNT(*) FROM stays s WHERE s.place_id IS NULL OR s.place_id NOT IN "
            "(SELECT place_id FROM places WHERE device_id=s.device_id)"
        ).fetchone()[0]
        n_stays = conn.execute("SELECT COUNT(*) FROM stays").fetchone()[0]
        conn.close()
        assert t["polyline"] == "enc:abc"
        assert t["route_key"] == "rk_hw"
        assert t["route_mode"] == "driving"
        assert t["route_encoded_at"] == 999
        assert orphans == 0
        assert n_stays == 4

    def test_idempotent_double_run(self, v2_db, shadow_env):
        """连续两次重建：四张表内容（含 id）完全一致。"""
        lm.rebuild_location_v2(v2_db)
        first = _location_snapshot(v2_db)
        lm.rebuild_location_v2(v2_db)
        assert _location_snapshot(v2_db) == first


class TestEtlRunV2Guard:
    def _run(self, path):
        etl.run(path, run_geocode=False, run_route=False, run_poi=False)

    def test_run_v2_branch(self, v2_db, shadow_env):
        """v2 激活库：etl.run 走 rebuild 分支，v1 位置管线零触碰。"""
        self._run(v2_db)
        conn = sqlite3.connect(v2_db)
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        # v2 表结构保持：无 v1 迁移追加的 etl_version 列
        place_cols = {r[1] for r in conn.execute("PRAGMA table_info(places)")}
        assert "etl_version" not in place_cols
        assert {"place_id", "point_count", "stay_ms", "is_primary"} <= place_cols
        # 三个计数语义：visit_count = stay 段数（非累加的 v1 visit_count=30）
        home = conn.execute(
            "SELECT visit_count, stay_ms, point_count FROM places "
            "WHERE device_id='dev1' AND grid_key='31.992,118.783'"
        ).fetchone()
        assert home["visit_count"] == 2
        assert home["stay_ms"] == 3_600_000 + 2_400_000
        # stays 全部带 place_id（v2 语义未被 v1 INSERT 洗掉）
        n_stays, n_with_place = conn.execute(
            "SELECT COUNT(*), COUNT(place_id) FROM stays"
        ).fetchone()
        # trips 路线缓存保留
        t = conn.execute("SELECT polyline FROM trips").fetchone()
        conn.close()
        assert n_stays == 4
        assert n_with_place == 4
        assert t["polyline"] == "enc:abc"

    def test_run_v2_twice_idempotent(self, v2_db, shadow_env):
        """连跑两次 etl.run：位置事实完全一致（P0 visit_count 累加不复发）。"""
        self._run(v2_db)
        first = _location_snapshot(v2_db)
        self._run(v2_db)
        assert _location_snapshot(v2_db) == first

    def test_run_v1_branch_unchanged(self, tmp_path, shadow_env, monkeypatch):
        """v1 库：etl.run 仍走 v1 管线（行为不变），不产生 v2 表/版本。"""
        path = tmp_path / "lt.db"
        _make_v1_db(path)
        from gacore.langTrack import label_places
        monkeypatch.setattr(label_places, "CONFIG_PATH", tmp_path / "none.json")

        etl.run(path, run_geocode=False, run_route=False, run_poi=False)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # v1 语义：places 无 place_id 列；visit_count 保留 upsert 累加行为
        place_cols = {r[1] for r in conn.execute("PRAGMA table_info(places)")}
        n_places = conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        n_stays = conn.execute("SELECT COUNT(*) FROM stays").fetchone()[0]
        conn.close()
        assert "place_id" not in place_cols
        assert "place_cells" not in tables
        assert "shadow_places_v2" not in tables
        # v1 语义：build_places 对每个出现过的网格建 place（含 3 个途经点网格），
        # dev1 home/work + dev1 三个途经网格 + dev2 home = 6；这正是 v2 要修的弱点
        assert n_places == 6
        assert n_stays == 4
