"""test_langTrack_label_places.py —— Task 4 标签文件 v3 与两阶段切换测试。

覆盖（计划 Task 4 清单）：
- parse_label_doc：v1 平铺 / v2 (device,grid) / v3 (device,place_id) 解析与非法拒绝；
- 两阶段文件切换：pending 写入备份正式文件、正式文件不动、swap 原子替换；
- DB 投影：v2 正式 places.label → v3 行；v1 表（无 place_id 列）拒绝投影；
- apply_labels_v3：以 (device_id,place_id) 更新、设备隔离、anchor_grid_key 不参与更新。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from gacore.langTrack import label_places as lp
from gacore.langTrack import location_migration as lm

# ---------------------------------------------------------------------------
# parse_label_doc
# ---------------------------------------------------------------------------

class TestParseLabelDoc:
    def test_v1_flat_doc(self):
        version, rows = lp.parse_label_doc({"31.992,118.783": "家", "31.998,118.790": "公司"})
        assert version == 1
        assert rows == [
            {"grid_key": "31.992,118.783", "tag": "家"},
            {"grid_key": "31.998,118.790", "tag": "公司"},
        ]

    def test_v1_explicit_version_key(self):
        version, rows = lp.parse_label_doc({"version": 1, "31.992,118.783": "家"})
        assert version == 1
        assert len(rows) == 1

    def test_v2_doc(self):
        version, rows = lp.parse_label_doc(
            {"version": 2, "labels": [{"device_id": "dev1", "grid_key": "31.992,118.783", "tag": "家"}]}
        )
        assert version == 2
        assert rows == [{"device_id": "dev1", "grid_key": "31.992,118.783", "tag": "家"}]

    def test_v3_doc(self):
        version, rows = lp.parse_label_doc(
            {
                "version": 3,
                "labels": [
                    {
                        "device_id": "dev1",
                        "place_id": "a13f0cde7129ab40",
                        "anchor_grid_key": "31.992,118.783",
                        "tag": "家",
                        "updated_at": "2026-09-01T11:00:00+08:00",
                    }
                ],
            }
        )
        assert version == 3
        assert rows[0]["place_id"] == "a13f0cde7129ab40"
        assert rows[0]["anchor_grid_key"] == "31.992,118.783"

    def test_rejects_non_object(self):
        with pytest.raises(lp.LabelFileError):
            lp.parse_label_doc(["nope"])

    def test_rejects_unknown_version(self):
        with pytest.raises(lp.LabelFileError):
            lp.parse_label_doc({"version": 9, "labels": []})

    def test_rejects_v1_bad_entry(self):
        with pytest.raises(lp.LabelFileError):
            lp.parse_label_doc({"31.992,118.783": 5})
        with pytest.raises(lp.LabelFileError):
            lp.parse_label_doc({"": "家"})

    def test_rejects_missing_required_fields(self):
        with pytest.raises(lp.LabelFileError):
            lp.parse_label_doc({"version": 2, "labels": [{"device_id": "dev1", "tag": "家"}]})
        with pytest.raises(lp.LabelFileError):
            lp.parse_label_doc({"version": 3, "labels": [{"device_id": "dev1", "tag": "家"}]})
        with pytest.raises(lp.LabelFileError):
            lp.parse_label_doc({"version": 3, "labels": [{"place_id": "p1", "tag": ""}]})
        with pytest.raises(lp.LabelFileError):
            lp.parse_label_doc({"version": 3, "labels": "not-a-list"})


# ---------------------------------------------------------------------------
# 文件 IO：load / atomic write / pending / swap
# ---------------------------------------------------------------------------

class TestLabelFileIO:
    def test_load_missing_file_returns_empty(self, tmp_path):
        version, rows = lp.load_label_doc(tmp_path / "absent.json")
        assert (version, rows) == (0, [])

    def test_load_bad_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(lp.LabelFileError):
            lp.load_label_doc(p)

    def test_atomic_write_roundtrip(self, tmp_path):
        p = tmp_path / "labels.json"
        labels = [{"device_id": "dev1", "place_id": "p1", "anchor_grid_key": "g", "tag": "家", "updated_at": None}]
        lp.write_labels_v3_atomic(p, labels)
        version, rows = lp.load_label_doc(p)
        assert version == 3
        assert rows == labels
        # 临时文件不残留
        assert [f.name for f in tmp_path.iterdir()] == ["labels.json"]

    def test_pending_backs_up_and_keeps_formal(self, tmp_path):
        formal = tmp_path / "place_labels.json"
        formal.write_text(json.dumps({"31.992,118.783": "家"}, ensure_ascii=False), encoding="utf-8")
        labels = [{"device_id": "dev1", "place_id": "p1", "anchor_grid_key": "31.992,118.783",
                   "tag": "家", "updated_at": "2026-09-01T00:00:00+08:00"}]
        pending = lp.write_labels_v3_pending(formal, labels)
        assert pending.name == "place_labels.json.v3.pending"
        # 正式文件不动；backup 保留旧内容
        assert json.loads(formal.read_text(encoding="utf-8")) == {"31.992,118.783": "家"}
        backup = tmp_path / "place_labels.json.v2_backup"
        assert json.loads(backup.read_text(encoding="utf-8")) == {"31.992,118.783": "家"}
        # pending 是 v3 文档
        version, rows = lp.load_label_doc(pending)
        assert (version, rows) == (3, labels)

    def test_swap_pending_replaces_formal(self, tmp_path):
        formal = tmp_path / "place_labels.json"
        formal.write_text("{}", encoding="utf-8")
        labels = [{"device_id": "dev1", "place_id": "p1", "anchor_grid_key": None, "tag": "家", "updated_at": None}]
        pending = lp.write_labels_v3_pending(formal, labels)
        lp.swap_pending_labels(pending, formal)
        version, rows = lp.load_label_doc(formal)
        assert version == 3
        assert rows == labels
        assert not pending.exists()  # pending 已消费

    def test_swap_missing_pending_raises(self, tmp_path):
        formal = tmp_path / "place_labels.json"
        formal.write_text("{}", encoding="utf-8")
        with pytest.raises(lp.LabelFileError):
            lp.swap_pending_labels(tmp_path / "absent.v3.pending", formal)
        assert formal.read_text(encoding="utf-8") == "{}"  # 正式文件不受影响


# ---------------------------------------------------------------------------
# DB 投影与 apply_labels_v3
# ---------------------------------------------------------------------------

def _v2_places_db(tmp_path: Path) -> Path:
    """v2 结构 places 表（含 place_id 列）+ 两设备样本。"""
    p = tmp_path / "v2.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE places (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          device_id TEXT NOT NULL,
          place_id TEXT NOT NULL,
          grid_key TEXT NOT NULL,
          label TEXT,
          updated_at TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO places(device_id, place_id, grid_key, label) VALUES (?,?,?,?)",
        [
            ("dev1", "p_home", "31.992,118.783", "家"),
            ("dev1", "p_work", "31.998,118.790", "未知"),
            ("dev2", "p_home2", "31.992,118.783", "家"),
        ],
    )
    conn.commit()
    conn.close()
    return p


class TestProjectAndApply:
    def test_project_from_v2_places(self, tmp_path):
        db = _v2_places_db(tmp_path)
        conn = sqlite3.connect(db)
        rows = lp.project_labels_v3_from_db(conn)
        conn.close()
        # label='未知' 不投影；两设备同 grid 隔离
        assert {(r["device_id"], r["place_id"], r["tag"]) for r in rows} == {
            ("dev1", "p_home", "家"),
            ("dev2", "p_home2", "家"),
        }
        assert all(r["anchor_grid_key"] for r in rows)

    def test_project_rejects_v1_places(self, tmp_path):
        p = tmp_path / "v1.db"
        conn = sqlite3.connect(p)
        conn.executescript(
            "CREATE TABLE places(device_id TEXT, grid_key TEXT, label TEXT);"
        )
        with pytest.raises(lp.LabelFileError):
            lp.project_labels_v3_from_db(conn)
        conn.close()

    def test_apply_labels_v3_device_scoped(self, tmp_path):
        db = _v2_places_db(tmp_path)
        n = lp.apply_labels_v3(
            db,
            [
                {"device_id": "dev1", "place_id": "p_work", "anchor_grid_key": "whatever", "tag": "公司"},
                {"device_id": "dev1", "place_id": "p_home2", "tag": "家"},  # dev2 的点不受影响
                {"device_id": "dev1", "place_id": "p_gone", "tag": "家"},   # 无匹配行
            ],
        )
        assert n == 1  # 只有 dev1/p_work 命中
        conn = sqlite3.connect(db)
        rows = dict(conn.execute("SELECT place_id, label FROM places WHERE device_id='dev1'"))
        conn.close()
        assert rows == {"p_home": "家", "p_work": "公司"}
        # dev2 行保持原样（anchor_grid_key 不参与更新，dev1 的 p_home2 请求没改 dev2 的行）
        conn = sqlite3.connect(db)
        assert conn.execute(
            "SELECT label FROM places WHERE device_id='dev2'"
        ).fetchone()[0] == "家"
        conn.close()


# ---------------------------------------------------------------------------
# prepare → activate → finalize 全链路（两阶段切换 + 崩溃恢复）
# ---------------------------------------------------------------------------

def _shadow_places_db(tmp_path: Path, *, two_devices: bool = False) -> Path:
    """v1 库 + 手工 shadow：dev1 家/公司两点 + dev2 同 grid（可选）。

    旧 places：dev1 (31.992,118.783,家,poi=甲小区南门) / dev1 (31.998,118.790,公司,poi=乙大厦)；
    shadow：同位置的 canonical cluster（place_id=sha1(device|grid)[:16]）。
    """
    from gacore.langTrack import etl

    p = tmp_path / "mig.db"
    conn = sqlite3.connect(p)
    conn.executescript(etl._SCHEMA)  # 完整 v1 六张业务表（activate 前置）
    conn.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, visit_count, poi, address, matched_level) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", "31.992,118.783", 31.992, 118.783, "家", 30, "甲小区南门", "某某路1号", "poi"),
            ("dev1", "31.998,118.790", 31.998, 118.790, "公司", 25, "乙大厦", "某某路2号", "poi"),
        ],
    )
    if two_devices:
        conn.execute(
            "INSERT INTO places(device_id, grid_key, lat, lon, label, visit_count) "
            "VALUES ('dev2', '31.992,118.783', 31.992, 118.783, '未知', 5)"
        )
    conn.commit()
    conn.close()

    # shadow：build_location_shadow 的产物形态（create v2 + shadow 表 + 插入 cluster 行）
    import hashlib

    conn = sqlite3.connect(p)
    lm.create_location_v2_tables(conn)
    conn.executescript(lm._shadow_ddl())

    def _cluster(dev: str, gk: str, lat: float, lon: float, visit_count: int) -> tuple:
        pid = hashlib.sha1(f"{dev}|{gk}".encode()).hexdigest()[:16]
        conn.execute(
            "INSERT INTO shadow_places_v2(device_id, place_id, grid_key, lat, lon, label, "
            "first_seen, last_seen, point_count, visit_count, stay_ms, is_primary, "
            "source_coord_system, center_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dev, pid, gk, lat, lon, "未知", 1000, 2000, 10, visit_count, 3_600_000, 1, "unknown", "stay_duration_weighted"),
        )
        conn.execute(
            "INSERT INTO shadow_place_cells_v2(device_id, place_id, grid_key) VALUES (?,?,?)",
            (dev, pid, gk),
        )
        conn.execute(
            "INSERT INTO shadow_stays_v2(device_id, start_ts, end_ts, duration_ms, center_lat, "
            "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, grid_key, "
            "place_id, source_coord_system, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dev, 1000, 4_600_000, 3_600_000, lat, lon, lat, lon, lat, lon, 10, 30.0, gk, pid, "unknown", "2026-08-17"),
        )
        return pid

    home_pid = _cluster("dev1", "31.992,118.783", 31.992, 118.783, 2)
    _cluster("dev1", "31.998,118.790", 31.998, 118.790, 1)
    if two_devices:
        _cluster("dev2", "31.992,118.783", 31.992, 118.783, 1)
    # 一条 dev1 trip，from/to 引用 home cluster（验证 prepare 的 place_id 改写传播）
    conn.execute(
        "INSERT INTO shadow_trips_v2(device_id, start_ts, end_ts, duration_ms, start_lat, "
        "start_lon, end_lat, end_lon, dist_m, n_points, day, from_place_id, to_place_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", 4_600_000, 5_700_000, 1_100_000, 31.992, 118.783, 31.998, 118.790, 900.0, 3, "2026-08-17", home_pid, home_pid),
    )
    conn.commit()
    conn.close()
    return p


class TestTwoPhaseSwap:
    def test_full_flow_prepare_activate_finalize(self, tmp_path):
        db = _shadow_places_db(tmp_path)
        labels = tmp_path / "place_labels.json"
        labels.write_text(json.dumps({"31.992,118.783": "家"}, ensure_ascii=False), encoding="utf-8")

        report = lm.prepare_location_migration(db, labels_path=labels, run_id="run-1")
        assert report["tag_migrated"] == 1
        assert report["tag_issues"] == 0
        assert report["blocked"] == 0
        assert report["geocode_reused"] == 2  # 家（甲小区南门）+ 公司（乙大厦）都偏移≈0
        assert labels.read_text(encoding="utf-8")  # 正式文件仍是 v1
        assert json.loads(labels.read_text(encoding="utf-8")) == {"31.992,118.783": "家"}

        # activate（带 pending 路径）→ finalize
        conn = sqlite3.connect(db)
        lm.activate_location_v2(conn, "run-1", pending_labels_path=str(labels.with_name(labels.name + ".v3.pending")))
        conn.close()
        target = lm.finalize_label_swap(db, labels_path=labels)
        assert target == labels
        version, rows = lp.load_label_doc(labels)
        assert version == 3
        assert len(rows) == 1 and rows[0]["tag"] == "家"
        # 状态 complete；pending 已消费
        conn = sqlite3.connect(db)
        assert lm.read_migration_state(conn)["status"] == "complete"
        conn.close()

    def test_recover_with_pending_file(self, tmp_path):
        """DB 已 COMMIT、标签文件替换前崩溃 → 启动恢复用 pending 完成替换。"""
        db = _shadow_places_db(tmp_path)
        labels = tmp_path / "place_labels.json"
        labels.write_text("{}", encoding="utf-8")
        lm.prepare_location_migration(db, labels_path=labels, run_id="run-2")
        conn = sqlite3.connect(db)
        lm.activate_location_v2(conn, "run-2", pending_labels_path=str(labels.with_name(labels.name + ".v3.pending")))
        conn.close()
        # 崩溃点：finalize 未执行
        action = lm.recover_pending_swap(db, labels_path=labels)
        assert action == "swapped"
        version, _ = lp.load_label_doc(labels)
        assert version == 3
        conn = sqlite3.connect(db)
        assert lm.read_migration_state(conn)["status"] == "complete"
        conn.close()

    def test_recover_pending_missing_projects_from_db(self, tmp_path):
        """pending 丢失 → 从正式 places（v2）label 投影重建。"""
        import hashlib

        db = _shadow_places_db(tmp_path)
        labels = tmp_path / "place_labels.json"
        labels.write_text("{}", encoding="utf-8")
        lm.prepare_location_migration(db, labels_path=labels, run_id="run-3")
        conn = sqlite3.connect(db)
        lm.activate_location_v2(conn, "run-3", pending_labels_path=str(labels.with_name(labels.name + ".v3.pending")))
        conn.close()
        labels.with_name(labels.name + ".v3.pending").unlink()  # pending 丢失

        action = lm.recover_pending_swap(db, labels_path=labels)
        assert action == "projected"
        version, rows = lp.load_label_doc(labels)
        assert version == 3
        # DB 投影来自激活后正式 places.label（prepare 已把旧 DB tag 家/公司写进 shadow，
        # 且 place_id 落定为 survivor legacy ID）
        home_pid = hashlib.sha1(b"dev1|legacy|31.992,118.783").hexdigest()[:16]
        work_pid = hashlib.sha1(b"dev1|legacy|31.998,118.790").hexdigest()[:16]
        assert {(r["place_id"], r["tag"]) for r in rows} == {(home_pid, "家"), (work_pid, "公司")}
        conn = sqlite3.connect(db)
        assert lm.read_migration_state(conn)["status"] == "complete"
        conn.close()

    def test_finalize_without_pending_state_raises(self, tmp_path):
        db = _shadow_places_db(tmp_path)
        labels = tmp_path / "place_labels.json"
        labels.write_text("{}", encoding="utf-8")
        with pytest.raises(lm.LocationMigrationError):
            lm.finalize_label_swap(db, labels_path=labels)


class TestCliOrchestration:
    """etl.py CLI 迁移编排冒烟：prepare → activate（含标签切换）→ recover no-op。"""

    def test_cli_prepare_activate_recover(self, tmp_path, monkeypatch, capsys):
        from gacore.langTrack import etl

        db = _shadow_places_db(tmp_path)
        labels = tmp_path / "place_labels.json"
        labels.write_text(json.dumps({"31.992,118.783": "家"}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(lm, "DEFAULT_LABELS_PATH", labels)

        monkeypatch.setattr("sys.argv", ["etl", "--db", str(db), "--location-prepare"])
        etl.main()
        conn = sqlite3.connect(db)
        assert lm.read_migration_state(conn)["status"] == "prepared"
        conn.close()
        assert labels.with_name(labels.name + ".v3.pending").exists()

        monkeypatch.setattr("sys.argv", ["etl", "--db", str(db), "--location-activate"])
        etl.main()
        conn = sqlite3.connect(db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert lm.read_migration_state(conn)["status"] == "complete"
        conn.close()
        version, rows = lp.load_label_doc(labels)
        assert version == 3 and rows and rows[0]["tag"] == "家"
        assert not labels.with_name(labels.name + ".v3.pending").exists()

        # 已 complete 后 recover 是 no-op
        monkeypatch.setattr("sys.argv", ["etl", "--db", str(db), "--location-recover"])
        etl.main()
        assert "none" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# confirm 交互（多设备候选隔离展示 + v3 标签分别落库）
# ---------------------------------------------------------------------------

def _confirm_v2_db(tmp_path: Path) -> Path:
    """v2 库：两设备各一个待确认候选（label=未知 且 candidate_label 非空）。"""
    p = tmp_path / "confirm.db"
    conn = sqlite3.connect(p)
    conn.execute(
        """
        CREATE TABLE places (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          device_id TEXT NOT NULL,
          place_id TEXT NOT NULL,
          grid_key TEXT NOT NULL,
          lat REAL, lon REAL,
          label TEXT, first_seen INTEGER, last_seen INTEGER,
          point_count INTEGER DEFAULT 0, visit_count INTEGER DEFAULT 0,
          stay_ms INTEGER DEFAULT 0, is_primary INTEGER DEFAULT 0,
          poi TEXT, address TEXT,
          candidate_label TEXT, confidence_home REAL, confidence_work REAL,
          updated_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO places(device_id, place_id, grid_key, lat, lon, label, "
        "point_count, visit_count, stay_ms, candidate_label, confidence_home, confidence_work) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", "p1", "31.992,118.783", 31.992, 118.783, "未知", 40, 8, 1000, "家", 0.9, 0.1),
            ("dev2", "p2", "31.992,118.783", 31.992, 118.783, "未知", 30, 6, 800, "家", 0.8, 0.2),
        ],
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    return p


class TestConfirmMultiDevice:
    def test_candidates_shown_per_device_and_tagged_separately(
        self, tmp_path, monkeypatch, capsys
    ):
        db = _confirm_v2_db(tmp_path)
        labels = tmp_path / "place_labels.json"
        monkeypatch.setattr(lp, "DB_PATH", db)
        monkeypatch.setattr(lp, "CONFIG_PATH", labels)

        from gacore.langTrack import geocode
        monkeypatch.setattr(geocode, "_amap_key", lambda: "test-key")
        monkeypatch.setattr(geocode, "reverse_geocode", lambda lat, lon, key, **kw: None)

        answers = iter(["家", "公司"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        lp.confirm()
        out = capsys.readouterr().out

        # 多设备：候选行必须带设备段，否则用户无法区分同网格两台设备的候选
        assert "·设备dev1" in out
        assert "·设备dev2" in out

        version, rows = lp.load_label_doc(labels)
        assert version == 3
        by_key = {(r["device_id"], r["place_id"]): r["tag"] for r in rows}
        assert by_key == {("dev1", "p1"): "家", ("dev2", "p2"): "公司"}

        conn = sqlite3.connect(db)
        labels_in_db = dict(conn.execute(
            "SELECT device_id || '/' || place_id, label FROM places"
        ))
        conn.close()
        assert labels_in_db == {"dev1/p1": "家", "dev2/p2": "公司"}

    def test_single_device_no_device_suffix(self, tmp_path, monkeypatch, capsys):
        db = _confirm_v2_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM places WHERE device_id='dev2'")
        conn.commit()
        conn.close()
        labels = tmp_path / "place_labels.json"
        monkeypatch.setattr(lp, "DB_PATH", db)
        monkeypatch.setattr(lp, "CONFIG_PATH", labels)

        from gacore.langTrack import geocode
        monkeypatch.setattr(geocode, "_amap_key", lambda: "test-key")
        monkeypatch.setattr(geocode, "reverse_geocode", lambda lat, lon, key, **kw: None)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")

        lp.confirm()
        out = capsys.readouterr().out
        assert "·设备" not in out
