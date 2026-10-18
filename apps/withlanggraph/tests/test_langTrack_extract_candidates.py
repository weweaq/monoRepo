"""候选类通道测试（封闭：假 LLM + tmp 库，不触网、不触真实 langTrack.db）。

覆盖 S3 的 emotions / patterns / worldview：
- 值域钳制（valence/arousal/intensity 与 confidence；patterns ≤0.7、worldview ≤0.6）；
- subject 缺失丢弃、subject 不在 nodes 时补占位（无悬空引用）；
- 只产候选（status 恒 candidate）；
- 内容派生主键 → 重跑幂等；
- export_day 覆盖三张候选表。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_langTrack_extract_candidates.py -q
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
from gacore.langTrack.extract.channels import emotions as emotions_ch  # noqa: E402
from gacore.langTrack.extract.channels import patterns as patterns_ch  # noqa: E402
from gacore.langTrack.extract.channels import worldview as worldview_ch  # noqa: E402
from gacore.langTrack.extract.runner import export_day, run_extract  # noqa: E402
from gacore.langTrack.extract.schema import migrate_extract_tables  # noqa: E402
from gacore.langTrack.extract.types import ExtractContext  # noqa: E402

DAY = "2026-10-16"


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


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "kg.db")
    migrate_extract_tables(conn)
    return conn


def _ctx(run_id: str = "run1") -> ExtractContext:
    return ExtractContext(day=DAY, run_id=run_id, prompt_ver="kg@v1")


def _q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    return conn.execute(sql, params).fetchall()


def _setup(tmp_path: Path) -> Config:
    cfg = Config.for_tests(tmp_path)
    write_pack_detail(
        cfg, DAY, "_CHAT", "〔CHAT〕", "ok",
        "- 22:00 还在改代码，有点累但挺兴奋", "pack 文本", "进包文本",
    )
    prompt_dir = cfg.root / "config" / "kg_prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for spec in CHANNELS:
        (prompt_dir / spec.prompt).write_text("抽取 prompt（测试桩）", encoding="utf-8")
    return cfg


# --------------------------------------------------------------------------- #
# emotions                                                                    #
# --------------------------------------------------------------------------- #
EMOTIONS_PAYLOAD: dict[str, Any] = {
    "nodes": [{"node_id": "person_user", "node_type": "Person", "label": "本人"}],
    "emotions": [
        {
            "subject": "person_user", "emotion": "焦虑",
            "valence": -1.8, "arousal": 1.4, "intensity": 2.0,
            "trigger_text": "deadline 逼近", "evidence": "有点烦", "confidence": 1.3,
        },
        {"subject": "person_boss", "emotion": "压力", "confidence": 0.5},
        {"emotion": "缺 subject 应被丢弃"},
    ],
}


def test_emotions_clamps_ranges_and_drops_malformed(tmp_path):
    conn = _conn(tmp_path)
    written, stats = emotions_ch.handle(conn, _ctx(), EMOTIONS_PAYLOAD)

    rows = _q(conn, "SELECT subject, valence, arousal, intensity, confidence FROM kg_emotions ORDER BY subject")
    assert len(rows) == 2
    # person_boss 在前（字典序）；person_user 的值全部钳制到值域
    assert rows[0][0] == "person_boss"
    assert rows[1] == ("person_user", -1.0, 1.0, 1.0, 1.0)
    assert stats["dropped_malformed"] == 1
    assert stats["emotions"] == 2


def test_emotions_placeholder_subject_avoids_dangling(tmp_path):
    conn = _conn(tmp_path)
    emotions_ch.handle(conn, _ctx(), EMOTIONS_PAYLOAD)

    assert _q(conn, "SELECT count(*) FROM kg_nodes WHERE node_id='person_boss'")[0][0] == 1
    dangling = _q(
        conn,
        "SELECT count(*) FROM kg_emotions e WHERE NOT EXISTS("
        "SELECT 1 FROM kg_nodes n WHERE n.node_id=e.subject)",
    )[0][0]
    assert dangling == 0


def test_emotions_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    emotions_ch.handle(conn, _ctx(), EMOTIONS_PAYLOAD)
    first = _q(conn, "SELECT count(*) FROM kg_emotions")[0][0]

    emotions_ch.handle(conn, _ctx(), EMOTIONS_PAYLOAD)

    assert _q(conn, "SELECT count(*) FROM kg_emotions")[0][0] == first


# --------------------------------------------------------------------------- #
# patterns                                                                    #
# --------------------------------------------------------------------------- #
PATTERNS_PAYLOAD: dict[str, Any] = {
    "nodes": [{"node_id": "person_user", "node_type": "Person", "label": "本人"}],
    "patterns": [
        {"subject": "person_user", "pattern": "夜间活跃", "confidence": 0.95, "support_count": 3},
        {"subject": "person_user", "pattern": "偏好极简设计", "evidence": "多次提到"},
        {"pattern": "缺 subject 应被丢弃", "confidence": 0.5},
    ],
}


def test_patterns_clamps_confidence_to_candidate_ceiling(tmp_path):
    conn = _conn(tmp_path)
    written, stats = patterns_ch.handle(conn, _ctx(), PATTERNS_PAYLOAD)

    rows = _q(conn, "SELECT pattern, confidence, status FROM kg_patterns ORDER BY pattern")
    assert len(rows) == 2
    by_pattern = {r[0]: (r[1], r[2]) for r in rows}
    assert by_pattern["夜间活跃"] == (0.7, "candidate")
    assert by_pattern["偏好极简设计"][0] == 0.0  # 缺 confidence → 0.0
    assert by_pattern["偏好极简设计"][1] == "candidate"
    assert stats["clamped_confidence"] == 1
    assert stats["dropped_malformed"] == 1


def test_patterns_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    patterns_ch.handle(conn, _ctx(), PATTERNS_PAYLOAD)
    first = _q(conn, "SELECT count(*) FROM kg_patterns")[0][0]

    patterns_ch.handle(conn, _ctx(), PATTERNS_PAYLOAD)

    assert _q(conn, "SELECT count(*) FROM kg_patterns")[0][0] == first


# --------------------------------------------------------------------------- #
# worldview                                                                   #
# --------------------------------------------------------------------------- #
WORLDVIEW_PAYLOAD: dict[str, Any] = {
    "nodes": [{"node_id": "person_user", "node_type": "Person", "label": "本人"}],
    "worldview": [
        {"subject": "person_user", "statement": "本地优先胜过云依赖", "domain": "技术", "confidence": 0.9},
        {"statement": "缺 subject 应被丢弃", "confidence": 0.4},
    ],
}


def test_worldview_clamps_confidence_to_candidate_ceiling(tmp_path):
    conn = _conn(tmp_path)
    written, stats = worldview_ch.handle(conn, _ctx(), WORLDVIEW_PAYLOAD)

    rows = _q(conn, "SELECT statement, domain, confidence, status FROM kg_worldview")
    assert rows == [("本地优先胜过云依赖", "技术", 0.6, "candidate")]
    assert stats["clamped_confidence"] == 1
    assert stats["dropped_malformed"] == 1


def test_worldview_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    worldview_ch.handle(conn, _ctx(), WORLDVIEW_PAYLOAD)
    first = _q(conn, "SELECT count(*) FROM kg_worldview")[0][0]

    worldview_ch.handle(conn, _ctx(), WORLDVIEW_PAYLOAD)

    assert _q(conn, "SELECT count(*) FROM kg_worldview")[0][0] == first


# --------------------------------------------------------------------------- #
# 集成：run_extract + export_day                                              #
# --------------------------------------------------------------------------- #
def test_run_extract_emotions_and_export_includes_table(tmp_path):
    cfg = _setup(tmp_path)
    db = tmp_path / "kg.db"
    llm = FakeLLM([json.dumps(EMOTIONS_PAYLOAD, ensure_ascii=False)])

    result = run_extract(DAY, cfg, db_path=db, llm=llm, only=["emotions"])

    assert result.status == "ok"
    assert result.channels["emotions"]["rows"] > 0
    tables = {rec["_table"] for rec in export_day(DAY, cfg, db_path=db)}
    assert "kg_emotions" in tables
    assert "kg_patterns" not in tables  # 未运行通道 → 空表不导出记录


def test_run_extract_all_candidate_channels(tmp_path):
    cfg = _setup(tmp_path)
    db = tmp_path / "kg.db"
    llm = FakeLLM([
        json.dumps(EMOTIONS_PAYLOAD, ensure_ascii=False),
        json.dumps(PATTERNS_PAYLOAD, ensure_ascii=False),
        json.dumps(WORLDVIEW_PAYLOAD, ensure_ascii=False),
    ])

    result = run_extract(
        DAY, cfg, db_path=db, llm=llm, only=["emotions", "patterns", "worldview"]
    )

    assert result.status == "ok"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM kg_emotions").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM kg_patterns").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM kg_worldview").fetchone()[0] == 1
    finally:
        conn.close()
    tables = {rec["_table"] for rec in export_day(DAY, cfg, db_path=db)}
    assert {"kg_emotions", "kg_patterns", "kg_worldview"} <= tables
