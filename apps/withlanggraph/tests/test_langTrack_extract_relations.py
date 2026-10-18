"""relations 通道测试（封闭：假 LLM + tmp 库，不触网、不触真实 langTrack.db）。

覆盖 §9 落库不变量在 relations 通道上的同档要求（无悬空边 / L2 必有 attributor / L3 上限 / 幂等）。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_langTrack_extract_relations.py -q
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
from gacore.langTrack.extract.channels import CHANNELS  # noqa: E402
from gacore.langTrack.extract.runner import run_extract  # noqa: E402

DAY = "2026-10-16"
PROMPT_NAME = "relations@v1.md"

PAYLOAD: dict[str, Any] = {
    "nodes": [
        {"node_id": "person_zhangsan", "node_type": "Person", "label": "张三"},
        {"node_id": "person_lisi", "node_type": "Person", "label": "李四"},
    ],
    "relations": [
        {
            "from": "person_zhangsan",
            "to": "person_lisi",
            "relation": "colleague_of",
            "edge_level": "L1",
            "assertion_text": "同事",
            "evidence": "一起做项目",
        },
        {
            "from": "person_zhangsan",
            "to": "project_kg",
            "relation": "works_with",
            "edge_level": "L2",
            "attributor": "person_zhangsan",
            "assertion_text": "合作中",
        },
        {
            "from": "person_zhangsan",
            "to": "theme_x",
            "relation": "friend_of",
            "edge_level": "L2",
            "assertion_text": "缺 attributor 应被丢弃",
        },
        {
            "from": "person_zhangsan",
            "to": "person_lisi",
            "relation": "may_know",
            "edge_level": "L3",
            "strength": 0.95,
        },
    ],
}


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeLLM:
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
        "- 与李四在同一项目组", "pack 文本", "进包文本",
    )
    prompt_dir = cfg.root / "config" / "kg_prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / PROMPT_NAME).write_text("抽取 prompt（测试桩）", encoding="utf-8")
    return cfg, tmp_path / "kg.db"


def _query(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_relations_channel_is_registered():
    assert "relations" in {c.key for c in CHANNELS}


def test_writes_relations_without_dangling(tmp_path):
    cfg, db = _setup(tmp_path)
    llm = FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, llm=llm, only=["relations"])

    assert result.status == "ok"
    # 3 条关系入库（缺 attributor 的 L2 被丢弃），占位节点补齐 project_kg
    assert len(_query(db, "SELECT relation_id FROM kg_relations")) == 3
    dangling = _query(
        db,
        "SELECT count(*) FROM kg_relations r WHERE NOT EXISTS ("
        "SELECT 1 FROM kg_nodes n WHERE n.node_id = r.from_node) "
        "OR NOT EXISTS (SELECT 1 FROM kg_nodes n WHERE n.node_id = r.to_node)",
    )
    assert dangling[0][0] == 0
    assert result.channels["relations"]["relations"] == 3
    assert result.channels["relations"]["dropped_l2_no_attributor"] == 1


def test_l3_strength_is_clamped(tmp_path):
    cfg, db = _setup(tmp_path)
    run_extract(DAY, cfg, db_path=db, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]), only=["relations"])

    over = _query(db, "SELECT count(*) FROM kg_relations WHERE edge_level='L3' AND strength > 0.6")
    assert over[0][0] == 0
    assert _query(db, "SELECT strength FROM kg_relations WHERE edge_level='L3'")[0][0] == 0.6


def test_relation_id_is_content_derived_and_rerun_is_idempotent(tmp_path):
    cfg, db = _setup(tmp_path)
    run_extract(DAY, cfg, db_path=db, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]), only=["relations"])
    first = _query(db, "SELECT relation_id FROM kg_relations ORDER BY relation_id")

    run_extract(
        DAY, cfg, db_path=db, force=True,
        llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]), only=["relations"],
    )

    assert _query(db, "SELECT relation_id FROM kg_relations ORDER BY relation_id") == first


def test_export_day_includes_relations_table(tmp_path):
    from gacore.langTrack.extract.runner import export_day

    cfg, db = _setup(tmp_path)
    run_extract(DAY, cfg, db_path=db, llm=FakeLLM([json.dumps(PAYLOAD, ensure_ascii=False)]), only=["relations"])

    tables = {rec["_table"] for rec in export_day(DAY, cfg, db)}

    assert "kg_relations" in tables
