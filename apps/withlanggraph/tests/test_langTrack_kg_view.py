"""KG 可视化（kg_view）的封闭测试：临时库 + 纯只读快照。

不触碰真实 data/langTrack.db、不启 ETL 子进程、不联网。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gacore.langTrack.server as server_mod
from gacore.langTrack import kg_view
from gacore.langTrack.extract.schema import EXTRACT_SCHEMA
from gacore.langTrack.server import create_app
from gacore.langTrack.storage import Storage


def _node(
    conn: sqlite3.Connection,
    node_id: str,
    label: str,
    node_type: str = "Person",
    first_day: str = "2026-10-01",
    last_day: str = "2026-10-02",
) -> None:
    conn.execute(
        "INSERT INTO kg_nodes (node_id,node_type,label,canonical,aliases,first_day,last_day,src,confidence,run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (node_id, node_type, label, "", "[]", first_day, last_day, "events", 0.9, "r1"),
    )


def _edge(
    conn: sqlite3.Connection,
    edge_id: str,
    from_node: str,
    to_node: str,
    relation: str,
    level: str,
    day: str,
) -> None:
    conn.execute(
        "INSERT INTO kg_edges (edge_id,from_node,to_node,relation,edge_level,day,src,run_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (edge_id, from_node, to_node, relation, level, day, "events", "r1"),
    )


def _relation(
    conn: sqlite3.Connection,
    relation_id: str,
    from_node: str,
    to_node: str,
    relation: str,
    level: str,
    day: str,
) -> None:
    conn.execute(
        "INSERT INTO kg_relations (relation_id,from_node,to_node,relation,edge_level,day,src,run_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (relation_id, from_node, to_node, relation, level, day, "relations", "r1"),
    )


def _alias(
    conn: sqlite3.Connection,
    alias: str,
    canonical: str,
    method: str,
    score: float,
    day: str,
    run_id: str,
    superseded_by: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO kg_aliases (alias,canonical,method,score,day,src,superseded_by,run_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (alias, canonical, method, score, day, "events", superseded_by, run_id),
    )


def _run(conn: sqlite3.Connection, run_id: str, day: str, status: str, channel_stats: dict) -> None:
    import json

    conn.execute(
        "INSERT INTO kg_runs (run_id,day,raw_hash,prompt_ver,status,channel_stats,started_at,finished_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, day, "hash", "v1", status, json.dumps(channel_stats), f"{day} 23:30:00", f"{day} 23:40:00"),
    )


def _seed(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(EXTRACT_SCHEMA)

    _node(conn, "p1", "甲")
    _node(conn, "p2", "乙")
    _node(conn, "p3", "乙", first_day="2026-10-02")
    _node(conn, "e1", "听歌", node_type="Event", first_day="2026-10-01", last_day="2026-10-01")
    _node(conn, "p4", "x</script><script>alert(1)</script>", first_day="2026-10-02")

    _edge(conn, "edge1", "p1", "e1", "likes", "L1", "2026-10-01")
    _edge(conn, "edge2", "p2", "e1", "knows", "L2", "2026-10-02")
    # Deliberately insert the newer relation first: the snapshot must compare
    # days explicitly, not rely on SQLite's unspecified row order.
    _relation(conn, "rel2", "p1", "p2", "friend_of", "L1", "2026-10-02")
    _relation(conn, "rel1", "p1", "p2", "friend_of", "L1", "2026-10-01")

    _alias(conn, "a1", "p1", "exact", 1.0, "2026-10-01", "r1")
    _alias(conn, "a2", "p2", "fuzzy", 0.90, "2026-10-01", "r1")
    _alias(conn, "a3", "p3", "fuzzy", 0.70, "2026-10-01", "r1")
    _alias(conn, "old", "p1", "exact", 1.0, "2026-10-01", "r1")
    _alias(conn, "new", "p1", "exact", 1.0, "2026-10-02", "r2", superseded_by="old|2026-10-01|r1")

    _run(
        conn,
        "r1",
        "2026-10-01",
        "ok",
        {"events": {"status": "ok", "rows": 2}, "_material": {"chars": 100, "sources": ["events"]}},
    )
    _run(
        conn,
        "r2",
        "2026-10-02",
        "partial",
        {"events": {"status": "failed", "rows": 0, "error": "boom"}},
    )
    conn.commit()
    return conn


@pytest.fixture
def kg_db(tmp_path):
    path = tmp_path / "kg.db"
    conn = _seed(path)
    yield conn
    conn.close()


def test_snapshot_counts_and_alias_rules(kg_db):
    snap = kg_view.load_kg_snapshot(kg_db)
    counts = snap["audit"]["counts"]

    assert counts["nodes"] == 5
    assert counts["edges"] == 2
    assert counts["relation_rows"] == 2
    assert counts["relation_unique"] == 1
    assert counts["isolated"] == 2  # p3（同名但无边）+ p4（恶意 label 的孤立点）
    assert counts["alias_effective"] == 3
    assert counts["alias_candidates"] == 1
    assert counts["duplicate_groups"] == 1

    assert snap["days"] == ["2026-10-01", "2026-10-02"]
    relation_edges = [e for e in snap["edges"] if e["kind"] == "relation"]
    assert len(relation_edges) == 1
    assert relation_edges[0]["day"] == "2026-10-02"
    assert snap["day_stats"][0]["new_edges"] == 1
    assert snap["day_stats"][0]["edges_total"] == 1
    assert snap["day_stats"][-1]["new_edges"] <= snap["day_stats"][-1]["edges_total"]
    assert snap["day_stats"][-1]["nodes_total"] == 5
    assert snap["runs"][-1]["status"] == "partial"
    assert snap["runs"][-1]["channels"][0]["status"] == "failed"

    candidates = snap["aliases"]["candidates"]
    assert [c["alias"] for c in candidates] == ["a3"]

    effective = {e["alias"]: e for e in snap["aliases"]["effective"]}
    assert "old" not in effective  # superseded_by 反向指针命中旧行
    assert effective["a2"]["canonical"] == "p2"  # fuzzy 0.90 >= 0.85 生效
    assert effective["new"]["final"] == "p1"

    groups = snap["aliases"]["duplicate_groups"]
    assert groups[0]["label"] == "乙"
    assert groups[0]["ids"] == ["p2", "p3"]

    isolated = snap["audit"]["isolated"]
    assert isolated["count"] == 2
    assert {n["id"] for n in isolated["sample"]} == {"p3", "p4"}


def test_render_views_are_self_contained_and_link_each_other(kg_db):
    overview = kg_view.render_kg_html(kg_db, "overview")
    assert '<title>KG 全景图' in overview
    assert 'id="kg-data"' in overview
    assert "/kg?view=timeline" in overview
    assert "/kg?view=audit" in overview
    assert "cytoscape" in overview
    assert "Cytoscape Consortium" in overview  # 库文件真的内联了，而不是只出现函数名
    assert "unpkg.com" not in overview
    assert "cdn.jsdelivr.net" not in overview

    timeline = kg_view.render_kg_html(kg_db, "timeline")
    assert '<title>KG 演化' in timeline
    assert "图谱演化" in timeline

    audit = kg_view.render_kg_html(kg_db, "audit")
    assert '<title>KG 体检' in audit
    assert "图谱体检" in audit
    assert "cytoscape" not in audit  # 体检页不需要图库，保持轻量

    # 非法 view 回落总览，避免 500
    assert '<title>KG 全景图' in kg_view.render_kg_html(kg_db, "bogus")


def test_render_static_links_and_escapes_json(kg_db):
    html = kg_view.render_kg_html(kg_db, "overview", static_links=True)
    assert "kg_timeline.html" in html
    assert "kg_audit.html" in html
    assert "/kg?view=" not in html
    assert "</script><script>alert(1)" not in html
    assert "\\u003c/script\\u003e" in html


def test_cli_exports_three_views(kg_db, tmp_path):
    out = tmp_path / "out"
    db_path = Path(kg_db.execute("PRAGMA database_list").fetchone()[2])
    assert kg_view.main(["--db", str(db_path), "--out-dir", str(out)]) == 0
    for name in ("kg_overview.html", "kg_timeline.html", "kg_audit.html"):
        path = out / name
        assert path.exists()
        assert path.stat().st_size > 1000
    assert "kg_timeline.html" in (out / "kg_overview.html").read_text(encoding="utf-8")

    single = tmp_path / "single"
    assert kg_view.main(["--db", str(db_path), "--out-dir", str(single), "--view", "audit"]) == 0
    assert (single / "kg_audit.html").exists()
    assert not (single / "kg_overview.html").exists()
    assert "kg_overview.html" not in (single / "kg_audit.html").read_text(encoding="utf-8")


def test_empty_kg_renders_without_crash(tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.db")
    conn.executescript(EXTRACT_SCHEMA)
    try:
        assert kg_view.render_kg_html(conn, "overview")
        timeline = kg_view.render_kg_html(conn, "timeline")
        assert "if(!DAYS.length)" in timeline
        assert "暂无抽取日" in timeline
        audit = kg_view.render_kg_html(conn, "audit")
        assert "C.nodes?Math.round(iso.count/C.nodes*100):0" in audit
    finally:
        conn.close()


def test_channel_stats_non_dict_is_tolerated(tmp_path):
    conn = sqlite3.connect(tmp_path / "kg.db")
    conn.executescript(EXTRACT_SCHEMA)
    try:
        _run(conn, "r1", "2026-10-01", "ok", [])  # type: ignore[arg-type]
        conn.commit()
        snap = kg_view.load_kg_snapshot(conn)
        assert snap["runs"][0]["channels"] == []
        assert snap["runs"][0]["material_chars"] == 0
    finally:
        conn.close()


def test_kg_route_serves_views(tmp_path, monkeypatch):
    db_path = tmp_path / "kg.db"
    _seed(db_path).close()
    monkeypatch.setattr(server_mod, "DB_PATH", db_path)
    monkeypatch.setattr(server_mod, "_ETL_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 3600)

    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage, heartbeat_dir=tmp_path / "run")
    try:
        with TestClient(app) as client:
            r = client.get("/kg?view=timeline")
            assert r.status_code == 200
            assert "text/html" in r.headers["content-type"]
            assert "图谱演化" in r.text

            r2 = client.get("/kg?view=bogus")
            assert r2.status_code == 200
            assert "KG 全景图" in r2.text
    finally:
        storage.close()


def test_kg_route_returns_503_when_db_missing(tmp_path, monkeypatch):
    db_path = tmp_path / "missing.db"
    monkeypatch.setattr(server_mod, "DB_PATH", db_path)
    monkeypatch.setattr(server_mod, "_ETL_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 3600)

    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage, heartbeat_dir=tmp_path / "run")
    try:
        with TestClient(app) as client:
            r = client.get("/kg")
            assert r.status_code == 503
            assert "KG 视图暂不可用" in r.text
        assert not db_path.exists()  # mode=ro 不得顺手建空库
    finally:
        storage.close()


def test_kg_route_returns_503_without_kg_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(server_mod, "DB_PATH", db_path)
    monkeypatch.setattr(server_mod, "_ETL_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(server_mod, "_ETL_TIMEOUT_SECONDS", 3600)

    storage = Storage(tmp_path / "langTrack.db")
    app = create_app(storage, heartbeat_dir=tmp_path / "run")
    try:
        with TestClient(app) as client:
            r = client.get("/kg")
            assert r.status_code == 503
            assert "no such table" in r.text
    finally:
        storage.close()
