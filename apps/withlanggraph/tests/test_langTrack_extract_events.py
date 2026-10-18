"""events 通道 + runner 测试（封闭：假 LLM + tmp 库，不触网、不触真实 langTrack.db）。

覆盖 §9 的四条落库不变量（无悬空边 / L2 必有 attributor / L3 置信度上限 / 幂等重跑）
与 §10 的失败隔离、回滚开关（默认停用不在此层，属 S4）。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_langTrack_extract_events.py -q
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.config import Config  # noqa: E402
from gacore.daily_info_pack import write_pack_detail  # noqa: E402
from gacore.langTrack.extract import runner  # noqa: E402
from gacore.langTrack.extract.runner import export_day, run_extract  # noqa: E402
from gacore.langTrack.extract.types import ChannelSpec  # noqa: E402

DAY = "2026-10-16"
PROMPT_NAME = "events@v1.md"

PAYLOAD: dict[str, Any] = {
    "nodes": [
        {"node_id": "person_zhangsan", "node_type": "Person", "label": "张三"},
        {"node_id": "event_20261016_001", "node_type": "Event", "label": "相机选型讨论"},
    ],
    "edges": [
        {
            "from": "person_zhangsan",
            "to": "event_20261016_001",
            "relation": "attended",
            "edge_level": "L1",
            "assertion_text": "讨论了相机选型",
            "event_time": "2026-10-16T12:30",
        },
        {
            "from": "person_zhangsan",
            "to": "theme_camera",
            "relation": "prefers",
            "edge_level": "L2",
            "attributor": "person_zhangsan",
            "assertion_text": "喜欢复古机身",
        },
        {
            "from": "person_zhangsan",
            "to": "theme_laptop",
            "relation": "may_want",
            "edge_level": "L3",
            "confidence": 0.95,
        },
        {
            "from": "person_zhangsan",
            "to": "theme_x",
            "relation": "claims",
            "edge_level": "L2",
            "assertion_text": "缺 attributor 应被丢弃",
        },
    ],
}


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeLLM:
    """按序返回预设回复的假模型；超出后重复最后一条。"""

    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls = 0

    async def ainvoke(self, messages: list[Any]) -> _Resp:
        idx = min(self.calls, len(self.contents) - 1)
        self.calls += 1
        return _Resp(self.contents[idx])


def _setup(tmp_path: Path) -> tuple[Config, Path]:
    cfg = Config.for_tests(tmp_path)
    write_pack_detail(
        cfg, DAY, "_CHAT", "〔CHAT〕", "ok",
        "- 12:30 与张三讨论相机选型\n- 21:00 调试设备", "pack 文本", "进包文本",
    )
    prompt_dir = cfg.root / "config" / "kg_prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    # 全部已注册通道的 prompt 桩都写齐，全通道运行才不会因缺 prompt 报错
    for name in ("events@v1.md", "relations@v1.md", "todos@v1.md"):
        (prompt_dir / name).write_text("抽取 prompt（测试桩）", encoding="utf-8")
    return cfg, tmp_path / "kg.db"


def _query(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_writes_nodes_and_edges_without_dangling_edges(tmp_path):
    cfg, db = _setup(tmp_path)
    llm = FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, llm=llm)

    assert result.status == "ok"
    assert result.rows > 0
    # 3 条边入库（缺 attributor 的 L2 被丢弃），占位节点补齐 theme_camera / theme_laptop
    assert len(_query(db, "SELECT edge_id FROM kg_edges")) == 3
    dangling = _query(
        db,
        "SELECT count(*) FROM kg_edges e WHERE NOT EXISTS ("
        "SELECT 1 FROM kg_nodes n WHERE n.node_id = e.from_node) "
        "OR NOT EXISTS (SELECT 1 FROM kg_nodes n WHERE n.node_id = e.to_node)",
    )
    assert dangling[0][0] == 0
    assert result.channels["events"]["nodes"] == 4


def test_l2_without_attributor_is_dropped_and_counted(tmp_path):
    cfg, db = _setup(tmp_path)
    llm = FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, llm=llm)

    missing = _query(db, "SELECT count(*) FROM kg_edges WHERE edge_level='L2' AND attributor IS NULL")
    assert missing[0][0] == 0
    assert result.channels["events"]["dropped_l2_no_attributor"] == 1


def test_l3_confidence_is_clamped_to_ceiling(tmp_path):
    cfg, db = _setup(tmp_path)
    llm = FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, llm=llm)

    over = _query(db, "SELECT count(*) FROM kg_edges WHERE edge_level='L3' AND confidence > 0.6")
    assert over[0][0] == 0
    assert _query(db, "SELECT confidence FROM kg_edges WHERE edge_level='L3'")[0][0] == 0.6
    assert result.channels["events"]["clamped_l3"] == 1


def test_edge_id_is_content_derived(tmp_path):
    cfg, db = _setup(tmp_path)
    run_extract(DAY, cfg, db_path=db, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]))
    first = _query(db, "SELECT edge_id FROM kg_edges ORDER BY edge_id")

    # 同素材重跑（--force）不应新增边：内容派生主键撞 PK 即忽略
    run_extract(DAY, cfg, db_path=db, force=True, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]))

    assert _query(db, "SELECT edge_id FROM kg_edges ORDER BY edge_id") == first


def test_rerun_same_material_is_skipped(tmp_path):
    cfg, db = _setup(tmp_path)
    first = run_extract(DAY, cfg, db_path=db, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]))
    edges_before = _query(db, "SELECT count(*) FROM kg_edges")[0][0]

    second = run_extract(DAY, cfg, db_path=db, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]))

    assert second.status == "skipped"
    assert second.rows == 0
    assert second.run_id == first.run_id
    assert _query(db, "SELECT count(*) FROM kg_runs")[0][0] == 1
    assert _query(db, "SELECT count(*) FROM kg_edges")[0][0] == edges_before


def test_invalid_json_is_retried_once(tmp_path):
    cfg, db = _setup(tmp_path)
    llm = FakeLLM(["这不是 JSON", json.dumps(PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, llm=llm, only=["events"])

    assert result.status == "ok"
    assert llm.calls == 2


def test_channel_fails_when_json_still_invalid(tmp_path):
    cfg, db = _setup(tmp_path)
    llm = FakeLLM(["仍然不是 JSON"])

    result = run_extract(DAY, cfg, db_path=db, llm=llm)

    assert result.status == "failed"
    assert result.rows == 0
    assert result.channels["events"]["status"] == "failed"
    assert "ValueError" in result.channels["events"]["error"]
    assert _query(db, "SELECT status FROM kg_runs")[0][0] == "failed"


def test_failed_run_is_retried_with_same_run_id(tmp_path):
    """非 ok 的历史运行复用同一 run_id 原地重跑，避免血缘断链。"""
    cfg, db = _setup(tmp_path)
    failed = run_extract(DAY, cfg, db_path=db, llm=FakeLLM(["坏 JSON"]))
    retried = run_extract(DAY, cfg, db_path=db, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]))

    assert failed.run_id == retried.run_id
    assert retried.status == "ok"
    assert _query(db, "SELECT count(*) FROM kg_runs")[0][0] == 1


def test_partial_status_when_one_channel_fails(tmp_path, monkeypatch):
    cfg, db = _setup(tmp_path)

    def _boom(conn, ctx, payload):
        raise RuntimeError("通道内部炸了")

    failing = ChannelSpec(key="zzz_probe", prompt=PROMPT_NAME, sources=("_CHAT",), handle=_boom)
    monkeypatch.setattr(runner, "CHANNELS", (*runner.CHANNELS, failing))
    llm = FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, llm=llm)

    assert result.status == "partial"
    assert result.channels["events"]["status"] == "ok"
    assert result.channels["zzz_probe"]["status"] == "failed"


def test_unknown_channel_key_is_rejected(tmp_path):
    cfg, db = _setup(tmp_path)
    try:
        run_extract(DAY, cfg, db_path=db, only=["nope"], llm=FakeLLM(["{}"]))
    except ValueError as exc:
        assert "未知通道" in str(exc)
        return
    raise AssertionError("未知通道名应被拒绝")


def test_dry_run_plans_without_llm_or_write(tmp_path):
    cfg, db = _setup(tmp_path)
    llm = FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, dry_run=True, llm=llm)

    assert result.status == "dry-run"
    assert llm.calls == 0
    assert not db.exists()
    assert result.material_chars > 0


def test_export_day_marks_source_table(tmp_path):
    cfg, db = _setup(tmp_path)
    run_extract(DAY, cfg, db_path=db, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]))

    tables = {rec["_table"] for rec in export_day(DAY, cfg, db)}

    assert tables == {"kg_runs", "kg_nodes", "kg_edges"}


def test_empty_material_skips_channel(tmp_path, monkeypatch):
    """素材为空（无 pack_detail 且现建也无 detail）→ 通道 skipped，不调 LLM。"""
    from gacore.langTrack.extract import material as mat

    cfg = Config.for_tests(tmp_path)
    prompt_dir = cfg.root / "config" / "kg_prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / PROMPT_NAME).write_text("prompt", encoding="utf-8")
    # 回退路径必须打桩：真实 build_info_pack_report 会触发 bili/git/浏览器取数（违反测试封闭性）
    monkeypatch.setattr(mat, "_BUILD_REPORT_FN", lambda _day, _cfg: ("pack", []))
    db = tmp_path / "kg.db"
    llm = FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, llm=llm, only=["events"])

    assert result.status == "skipped"
    assert result.channels["events"]["status"] == "skipped"
    assert llm.calls == 0
