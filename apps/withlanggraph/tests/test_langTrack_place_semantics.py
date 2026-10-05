"""place_semantics.py 单元测试：手工语义配置（tag/note/区县背景）加载与解析。

封闭性：全部用例写 tmp 配置文件或直接传 cfg，绝不读仓库真实 data/place_semantics.json。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.langTrack import place_semantics as ps

_EMPTY = {"places": [], "districts": []}


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


def test_normalize_skips_invalid_entries():
    doc = ps.normalize_doc({
        "places": [
            {"tag": "孤儿条目"},  # 无匹配键 → 跳过
            {"place_id": "p1", "tag": 123, "note": "ok"},  # 非 str tag → 归一为空串
            {"poi": "张垛", "tag": "老家", "note": "背景"},
        ],
        "districts": [
            {"note": "无名区县"},  # 缺 district 名 → 跳过
            {"district": "当涂县", "note": "手写背景"},
        ],
        "unexpected": 1,  # 未知键忽略
    })
    assert doc["places"] == [
        {"place_id": "p1", "poi": "", "tag": "", "note": "ok"},
        {"place_id": "", "poi": "张垛", "tag": "老家", "note": "背景"},
    ]
    assert doc["districts"] == [{"district": "当涂县", "note": "手写背景"}]


def test_normalize_non_dict_returns_empty():
    assert ps.normalize_doc([1, 2]) == _EMPTY
    assert ps.normalize_doc("x") == _EMPTY


def test_resolve_tag_precedence(tmp_path):
    p = _write(tmp_path, {"places": [{"poi": "张垛", "tag": "张威的老家"}]})
    cfg = ps.load(p)
    # poi 命中：tag 覆盖 DB label
    assert ps.resolve_tag(None, "张垛", "未知", cfg=cfg) == "张威的老家"
    assert ps.resolve_tag(None, "张垛", "公司", cfg=cfg) == "张威的老家"
    # 未命中：回落 DB label（"未知"不计）
    assert ps.resolve_tag("other", "其他点", "公司", cfg=cfg) == "公司"
    assert ps.resolve_tag("other", "其他点", "未知", cfg=cfg) == ""


def test_resolve_tag_place_id_priority(tmp_path):
    """place_id 命中优先于 poi 名（条目内两键都给时按 place_id 定位）。"""
    p = _write(tmp_path, {"places": [{"place_id": "p1", "poi": "张垛", "tag": "by-id"}]})
    cfg = ps.load(p)
    assert ps.resolve_tag("p1", "别的名", "", cfg=cfg) == "by-id"
    assert ps.resolve_tag("p2", "张垛", "", cfg=cfg) == "by-id"  # poi 命中同条目


def test_note_and_district_note(tmp_path):
    p = _write(tmp_path, {
        "places": [{"place_id": "p1", "note": "乌溪镇"}],
        "districts": [{"district": "当涂县", "note": "手写背景"}],
    })
    cfg = ps.load(p)
    assert ps.note_for("p1", "", cfg=cfg) == "乌溪镇"
    assert ps.note_for("p2", "张垛", cfg=cfg) == ""
    assert ps.district_note("当涂县", cfg=cfg) == "手写背景"
    assert ps.district_note("雨花台区", cfg=cfg) == ""
    assert ps.district_note("", cfg=cfg) == ""


def test_load_reloads_on_mtime_change(tmp_path):
    """mtime 缓存：文件内容变化（mtime 推进）后重读，手工编辑即时生效。"""
    p = _write(tmp_path, {"places": [{"poi": "A", "tag": "t1"}]})
    assert ps.resolve_tag(None, "A", "", cfg=ps.load(p)) == "t1"
    p.write_text(json.dumps({"places": [{"poi": "A", "tag": "t2"}]}, ensure_ascii=False), encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert ps.resolve_tag(None, "A", "", cfg=ps.load(p)) == "t2"
