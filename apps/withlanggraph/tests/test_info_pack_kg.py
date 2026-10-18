"""_KG 信息源（S4）封闭单测：满 / 空 / 零产出 / 停用 / 截断。

合成 langTrack.db 落盘 tmp（表结构取自 extract.schema.EXTRACT_SCHEMA，与抽取层同一份 DDL），
走真实读层 `_kg_day_graph`（`_KG_FN` 默认指向它），不触真实库、不触网络、不触外部 CLI。

覆盖：
- 空态三形态：库不存在 / 库在但 kg_* 表未建 / 表在但当日无 kg_runs 行 → 全部 missing_data；
- 零产出：kg_runs 在而各表 0 行 → 同判 missing_data（不冒充 ok）；
- 满：人物 Top / 未完成待办（valid_to 未闭合）/ 关系变化 / 情绪基调 进 pack，detail=全量表；
- 停用：config `_KG.enabled=false` → status=disabled 且不进包；
- 截断：pack_body 超 cap 时由框架行截断（note=truncated=N）。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_info_pack_kg.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import gacore.daily_info_pack as dip
from gacore.config import Config
from gacore.langTrack.extract.schema import EXTRACT_SCHEMA

DAY = "2026-10-16"


def _cfg(tmp_path: Path) -> Config:
    return Config.for_tests(tmp_path)


def _db_path(cfg: Config) -> Path:
    return cfg.root / "data" / "langTrack.db"


def _conn(cfg: Config):
    import sqlite3

    db = _db_path(cfg)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(EXTRACT_SCHEMA)
    return conn


def _insert_run(conn, day: str, status: str = "ok") -> None:
    conn.execute(
        "INSERT INTO kg_runs (run_id, day, raw_hash, prompt_ver, status, started_at)"
        " VALUES ('r1', ?, 'h1', 'kg@v1', ?, '2026-10-16 23:30:00')",
        (day, status),
    )


def _insert_node(conn, node_id: str, node_type: str, label: str) -> None:
    conn.execute(
        "INSERT INTO kg_nodes (node_id, node_type, label, first_day, last_day, src, run_id)"
        " VALUES (?, ?, ?, ?, ?, 'events', 'r1')",
        (node_id, node_type, label, DAY, DAY),
    )


def _insert_edge(conn, edge_id: str, frm: str, to: str, relation: str, level: str,
                 assertion: str, *, src: str = "events", valid_to: str | None = None) -> None:
    conn.execute(
        "INSERT INTO kg_edges (edge_id, from_node, to_node, relation, edge_level,"
        " assertion_text, valid_from, valid_to, day, src, run_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'r1')",
        (edge_id, frm, to, relation, level, assertion, DAY, valid_to, DAY, src),
    )


def _seed_full(cfg: Config, *, status: str = "ok") -> None:
    """一条当日运行 + 4 节点 / 3 边（其中 1 条为未闭合待办）/ 1 情绪 / 1 稳定关系。"""
    conn = _conn(cfg)
    with conn:
        _insert_run(conn, DAY, status)
        _insert_node(conn, "person_zhangsan", "Person", "张三")
        _insert_node(conn, "person_lisi", "Person", "李四")
        _insert_node(conn, "project_mono", "Project", "mono 重构")
        _insert_node(conn, "event_20261016_001", "Event", "买相机")
        _insert_edge(conn, "e1", "person_zhangsan", "project_mono", "works_on", "L1",
                     "张三在推进 mono 重构")
        _insert_edge(conn, "e2", "person_zhangsan", "event_20261016_001", "has_todo", "L1",
                     "下周把相机买了", src="todos")
        # 已闭合（valid_to 有值）→ 不算未完成待办
        _insert_edge(conn, "e3", "person_lisi", "event_20261016_001", "promised", "L1",
                     "答应帮张三看代码", src="todos", valid_to="2026-10-20")
        conn.execute(
            "INSERT INTO kg_emotions (emotion_id, subject, emotion, intensity, trigger_text,"
            " day, src, run_id) VALUES ('m1','person_zhangsan','焦虑',0.8,'重构进度紧',?,'emotions','r1')",
            (DAY,),
        )
        conn.execute(
            "INSERT INTO kg_relations (relation_id, from_node, to_node, relation, edge_level,"
            " strength, day, src, run_id) VALUES ('rel1','person_zhangsan','person_lisi',"
            "'colleague_of','L1',0.9,?,'relations','r1')",
            (DAY,),
        )
    conn.close()


def _stub_external(monkeypatch) -> None:
    """封闭外部源：CLI/网络/子进程一律 mock 成空（参照 test_daily_info_pack._flood_all_sources）。"""
    monkeypatch.setattr(dip, "_BILLI_FN", lambda **k: {"entries": [], "total": 0})
    monkeypatch.setattr(dip, "_BROWSER_FN", lambda **k: {"entries": []})
    monkeypatch.setattr(dip, "_NCM_ME_FN", lambda: {"error": "mock"})
    monkeypatch.setattr(dip, "_NCM_PLAYLIST_FN", lambda *a, **k: {"error": "mock"})
    monkeypatch.setattr(dip, "_run_git", lambda root, date: (0, ""))


def _write_source_config(cfg: Config, data: dict) -> None:
    p = cfg.root / "config" / "info_pack.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 空态：库缺失 / 表未建 / 当日无运行 / 运行但零产出                            #
# --------------------------------------------------------------------------- #
def test_kg_no_db(tmp_path):
    title, pack, detail = dip._build_kg(DAY, _cfg(tmp_path))
    assert title == "〔KG·当日图摘要〕"
    assert pack == dip._KG_MISSING
    assert dip.classify_body(pack)[0] == "missing_data"
    assert detail == ""


def test_kg_tables_absent(tmp_path):
    """库存在但 kg_* 表未建（抽取层从未在本库跑过）→ 空态，不判失败。"""
    import sqlite3

    cfg = _cfg(tmp_path)
    db = _db_path(cfg)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER)")
    conn.close()
    title, pack, detail = dip._build_kg(DAY, cfg)
    assert pack == dip._KG_MISSING
    assert dip.classify_body(pack)[0] == "missing_data"


def test_kg_no_run_for_day(tmp_path):
    """表在、但当日无 kg_runs 行（抽取没跑过这天）→ 空态。"""
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    with conn:
        _insert_run(conn, "2026-10-15")
    conn.close()
    title, pack, detail = dip._build_kg(DAY, cfg)
    assert pack == dip._KG_MISSING
    assert detail == ""


def test_kg_run_without_rows(tmp_path):
    """运行在但零产出 → 同判空态（不把"抽了个空"冒充 ok）。"""
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    with conn:
        _insert_run(conn, DAY)
    conn.close()
    title, pack, detail = dip._build_kg(DAY, cfg)
    assert pack == dip._KG_MISSING
    assert dip.classify_body(pack)[0] == "missing_data"


# --------------------------------------------------------------------------- #
# 满：各节进 pack；detail=当日 kg_* 全量                                       #
# --------------------------------------------------------------------------- #
def test_kg_full(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_full(cfg)
    title, pack, detail = dip._build_kg(DAY, cfg)
    assert title == "〔KG·当日图摘要〕"
    assert dip.classify_body(pack)[0] == "ok"
    # 运行头（各表当日计数）
    assert "- 抽取运行 ok：节点 4 · 当日边 3（失效 0）· 稳定关系 1 · 情绪 1" in pack
    # 人物 Top（按当日关联边数 degree 排序：张三 2 边、买相机 2 边、其余各 1）
    assert "- 人物 Top：" in pack and "张三(2)" in pack and "买相机(2)" in pack
    # 未完成待办：只含 valid_to 未闭合的那条（e3 已闭合被排除）
    assert "- 未完成待办（1 条）：" in pack
    assert "张三 has_todo 买相机：下周把相机买了" in pack
    todo_block = pack.split("- 未完成待办（1 条）：")[1].split("- 关系变化")[0]
    assert "has_todo" in todo_block and "promised" not in todo_block
    # 关系变化
    assert "- 关系变化（3 条）：" in pack
    assert "张三 —works_on→ mono 重构［L1］：张三在推进 mono 重构" in pack
    # 情绪基调
    assert "- 情绪基调：焦虑 0.8（重构进度紧）" in pack
    # detail=全量表逐条（不挑选不压缩）
    for table in ("kg_runs", "kg_nodes", "kg_edges", "kg_relations", "kg_aliases",
                  "kg_emotions", "kg_patterns", "kg_worldview"):
        assert f"- {table} 全量（" in detail
    assert "- kg_edges 全量（3 条）：" in detail
    assert "- kg_relations 全量（1 条）：" in detail
    assert "person_zhangsan" in detail and "colleague_of" in detail


def test_kg_persons_todo_top_limits(tmp_path):
    """各节 TOP 收敛：超过 _KG_TODO_TOP 的未闭合待办只进 pack 前 N 条，detail 全量保留。"""
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    with conn:
        _insert_run(conn, DAY)
        _insert_node(conn, "person_zhangsan", "Person", "张三")
        for i in range(9):
            node_id = f"event_20261016_{i:03d}"
            _insert_node(conn, node_id, "Event", f"待办{i}")
            _insert_edge(conn, f"t{i}", "person_zhangsan", node_id, "has_todo", "L1",
                         f"第{i}件事", src="todos")
    conn.close()
    title, pack, detail = dip._build_kg(DAY, cfg)
    assert f"- 未完成待办（{dip._KG_TODO_TOP} 条）：" in pack
    assert "第0件事" in pack and f"第{dip._KG_TODO_TOP}件事" not in pack
    assert "- kg_edges 全量（9 条）：" in detail  # detail 不做 TOP 挑选


# --------------------------------------------------------------------------- #
# 停用与截断（走真实装配路径 build_info_pack_report）                           #
# --------------------------------------------------------------------------- #
def test_kg_disabled_by_config(tmp_path, monkeypatch):
    _stub_external(monkeypatch)
    cfg = _cfg(tmp_path)
    _seed_full(cfg)
    _write_source_config(cfg, {"sources": {"_KG": {"enabled": False}}})
    pack, stats = dip.build_info_pack_report(DAY, cfg)
    by = {s["key"]: s for s in stats}
    assert by["_KG"]["status"] == "disabled"
    assert by["_KG"]["note"] == "已停用(config)"
    assert "〔KG·当日图摘要" not in pack


def test_kg_enabled_enters_pack(tmp_path, monkeypatch):
    _stub_external(monkeypatch)
    cfg = _cfg(tmp_path)
    _seed_full(cfg)
    _write_source_config(cfg, {"sources": {"_KG": {"enabled": True, "cap": 600, "priority": 105}}})
    pack, stats = dip.build_info_pack_report(DAY, cfg)
    by = {s["key"]: s for s in stats}
    assert by["_KG"]["status"] == "ok"
    assert by["_KG"]["chars"] > 0
    assert "〔KG·当日图摘要｜状态:全量〕" in pack
    assert by["_KG"]["detail_chars"] > by["_KG"]["full_chars"]  # detail=全量表，比 pack 渲染长


def test_kg_cap_truncation(tmp_path, monkeypatch):
    """cap 收紧到 300 → 框架行截断并记 truncated=N（单一截断点，不断章）。

    默认 cap（1200）对实测日摘要（1071 字符）不触发截断，故此处显式下调 cap 以验证机制。
    """
    _stub_external(monkeypatch)
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    with conn:
        _insert_run(conn, DAY)
        _insert_node(conn, "person_zhangsan", "Person", "张三")
        for i in range(6):
            node_id = f"event_20261016_{i:03d}"
            _insert_node(conn, node_id, "Event", f"事项{i}")
            _insert_edge(conn, f"t{i}", "person_zhangsan", node_id, "has_todo", "L1",
                         "很长的待办描述" * 4, src="todos")
            _insert_edge(conn, f"x{i}", "person_zhangsan", node_id, "discussed", "L2",
                         "很长的断言描述" * 4)
    conn.close()
    cap = 300
    _write_source_config(cfg, {"sources": {"_KG": {"enabled": True, "cap": cap}}})
    pack, stats = dip.build_info_pack_report(DAY, cfg)
    by = {s["key"]: s for s in stats}
    assert by["_KG"]["full_chars"] > cap
    assert by["_KG"]["note"].startswith("truncated=")
    assert "（已截断" in by["_KG"]["packed_body"]
    assert by["_KG"]["chars"] <= cap + len(dip._TRUNCATION_TAIL.format(n=999)) + 1
