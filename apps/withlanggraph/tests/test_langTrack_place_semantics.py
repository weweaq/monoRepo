"""place_semantics.py 单元测试：区县背景配置 + 编辑器数据层（v3.5，DB 即真源）。

v3.5 起本模块只管区县 note（地点标签/note 由编辑器直写 places.label/note 列）；
旧版 places/roles 键的文档兼容读取（忽略）。测试封闭：tmp 配置文件 + tmp sqlite 库。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.langTrack import place_semantics as ps
from gacore.langTrack.location_reader import ensure_note_column

_EMPTY = {"districts": []}


def _write(tmp_path, doc, name="sem.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_load_missing_file_returns_empty(tmp_path):
    assert ps.load(tmp_path / "absent.json") == _EMPTY


def test_load_bad_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert ps.load(p) == _EMPTY


def test_normalize_keeps_districts_ignores_legacy_keys():
    """旧版 places/roles 键忽略；districts 非法条目跳过。"""
    doc = ps.normalize_doc({
        "places": [{"poi": "张垛", "tag": "旧版条目应被忽略"}],
        "roles": {"home": "旧版角色应被忽略"},
        "districts": [
            {"note": "无名区县"},
            {"district": "当涂县", "note": "手写背景"},
            {"district": 123},
        ],
    })
    assert doc == {"districts": [{"district": "当涂县", "note": "手写背景"}]}


def test_normalize_non_dict_returns_empty():
    assert ps.normalize_doc([1, 2]) == _EMPTY
    assert ps.normalize_doc("x") == _EMPTY


def test_district_note(tmp_path):
    p = _write(tmp_path, {"districts": [{"district": "当涂县", "note": "手写背景"}]})
    cfg = ps.load(p)
    assert ps.district_note("当涂县", cfg=cfg) == "手写背景"
    assert ps.district_note("雨花台区", cfg=cfg) == ""
    assert ps.district_note("", cfg=cfg) == ""


def test_save_doc_roundtrip_and_cache_invalidation(tmp_path):
    """save_doc 原子写 + 规范化 + 缓存失效（写后立读即新值）。"""
    p = tmp_path / "sem.json"
    ps.save_doc({"districts": [{"district": "当涂县", "note": "v1"}]}, p)
    assert ps.district_note("当涂县", cfg=ps.load(p)) == "v1"
    ps.save_doc({"districts": [{"district": "当涂县", "note": "v2"}]}, p)
    assert ps.district_note("当涂县", cfg=ps.load(p)) == "v2"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk == {"districts": [{"district": "当涂县", "note": "v2"}]}


def test_save_district_notes_replaces_whole_table(tmp_path):
    p = tmp_path / "sem.json"
    n = ps.save_district_notes(
        [{"district": "当涂县", "note": "a"}, {"district": "雨花台区", "note": ""}], p
    )
    assert n == 2
    doc = ps.load(p)
    assert [d["district"] for d in doc["districts"]] == ["当涂县", "雨花台区"]


def _make_db(tmp_path: Path) -> Path:
    """最小 v2 places/stays 库（含 note 列迁移）。"""
    path = tmp_path / "lt.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE places (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
          grid_key TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
          label TEXT DEFAULT '未知', first_seen INTEGER, last_seen INTEGER,
          visit_count INTEGER NOT NULL DEFAULT 0, is_primary INTEGER NOT NULL DEFAULT 0,
          address TEXT, poi TEXT, district TEXT, township TEXT,
          place_id TEXT, point_count INTEGER, stay_ms INTEGER
        );
        CREATE TABLE stays (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
          place_id TEXT, grid_key TEXT, start_ts INTEGER, end_ts INTEGER, day TEXT
        );
        """
    )
    conn.execute("PRAGMA user_version = 2")
    conn.executemany(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, last_seen, "
        "visit_count, address, poi, district, township, place_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dev1", "g1", 31.99, 118.78, "家", 1000, 2000, 260,
             "江苏省南京市雨花台区X路1号", "家小区", "雨花台区", "Y街道", "p_home"),
            ("dev1", "g2", 31.33, 118.67, "张威的老家", 500, 900, 5,
             "安徽省马鞍山市当涂县乌溪镇张垛", "张垛", "当涂县", "乌溪镇", "p_zd"),
        ],
    )
    conn.executemany(
        "INSERT INTO stays(device_id, place_id, grid_key, start_ts, end_ts, day) VALUES (?,?,?,?,?,?)",
        [
            ("dev1", "p_home", "g1", 100, 200, "2026-10-05"),
            ("dev1", "p_zd", "g2", 300, 400, "2026-10-04"),
            ("dev1", "p_zd", "g2", 500, 600, "2026-10-05"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_editor_data_shapes_and_recent(tmp_path):
    db = _make_db(tmp_path)
    d = ps.editor_data(db_path=db, config_path=tmp_path / "sem.json")
    assert [p["poi"] for p in d["places"]] == ["家小区", "张垛"]  # 访问降序
    assert d["home_district"] == "雨花台区"
    zd = d["places"][1]
    assert zd["last_day"] == "2026-10-05"
    assert [x["day"] for x in zd["recent"]] == ["2026-10-04", "2026-10-05"]
    assert [x["district"] for x in d["districts"]] == ["雨花台区", "当涂县"]
    assert len(d["recent_days"]) == 14


def test_editor_data_db_missing(tmp_path):
    d = ps.editor_data(db_path=tmp_path / "none.db", config_path=tmp_path / "sem.json")
    assert d["places"] == [] and "不存在" in d["db_note"]


def test_save_place_labels_writes_label_and_note(tmp_path):
    """保存直写 label/note（角色值原样保留给计算逻辑），place_id 匹配。"""
    db = _make_db(tmp_path)
    n = ps.save_place_labels(db, [
        {"place_id": "p_zd", "label": "张威的老家", "note": "外婆家，逢年过节必回"},
        {"place_id": "p_home", "label": "家", "note": ""},
    ])
    assert n == 2
    conn = sqlite3.connect(db)
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT place_id, label, note FROM places")}
    conn.close()
    assert rows["p_zd"] == ("张威的老家", "外婆家，逢年过节必回")
    assert rows["p_home"][0] == "家"


def test_save_place_labels_poi_fallback_and_truncation(tmp_path):
    """无 place_id 时按 poi 匹配（v1 库路径）；超长截断。"""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE places SET place_id=NULL WHERE place_id='p_zd'")
    conn.commit()
    conn.close()
    n = ps.save_place_labels(db, [
        {"place_id": "", "poi": "张垛", "label": "老" * 30, "note": "n" * 600}
    ])
    assert n == 1
    conn = sqlite3.connect(db)
    label, note = conn.execute(
        "SELECT label, note FROM places WHERE poi='张垛'").fetchone()
    conn.close()
    assert label == "老" * 24
    assert note == "n" * 500


def test_ensure_note_column_idempotent_and_never_touches_user_version(tmp_path):
    """列迁移幂等；绝不递增 user_version（该版本号被 location v2 占用）。"""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    assert ensure_note_column(conn) is True
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert ensure_note_column(conn) is False
    conn.close()
