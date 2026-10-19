"""events 通道 + runner 测试（封闭：假 LLM + tmp 库，不触网、不触真实 langTrack.db）。

覆盖 §9 的四条落库不变量（无悬空边 / L2 必有 attributor / L3 置信度上限 / 幂等重跑）
与 §10 的失败隔离、回滚开关（默认停用不在此层，属 S4）。

运行：uv run --package gacore pytest apps/withlanggraph/tests/test_langTrack_extract_events.py -q
"""

from __future__ import annotations

import asyncio
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
from gacore.langTrack.extract.channels import CHANNELS  # noqa: E402
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
    def __init__(
        self,
        content: str,
        *,
        usage: dict[str, Any] | None = None,
        finish: str | None = None,
    ) -> None:
        self.content = content
        self.usage_metadata = usage
        self.response_metadata = {"finish_reason": finish} if finish else {}


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
    # 按注册表写齐全部通道的 prompt 桩，全通道运行才不会因缺 prompt 报错
    for spec in CHANNELS:
        (prompt_dir / spec.prompt).write_text("抽取 prompt（测试桩）", encoding="utf-8")
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


def test_hanging_channel_times_out_without_blocking_others(tmp_path, monkeypatch):
    """单通道 LLM 永不返回 → 该通道记 failed 超时，其余通道照常落库（整次 partial）。

    这是挂死事故的回归护栏：没有超时兜底时 asyncio.gather 会无限等待，整轮抽取卡死。
    """
    cfg, db = _setup(tmp_path)
    monkeypatch.setenv("KG_EXTRACT_CHANNEL_TIMEOUT", "0.05")
    prompt_dir = cfg.root / "config" / "kg_prompts"
    (prompt_dir / "zzz@v1.md").write_text("HANG", encoding="utf-8")

    class _SelectiveLLM:
        async def ainvoke(self, messages: list[Any]) -> _Resp:
            if "HANG" in messages[0].content:
                await asyncio.sleep(3600)
            return _Resp(json.dumps(PAYLOAD, ensure_ascii=False))

    probe = ChannelSpec(key="zzz_probe", prompt="zzz@v1.md", sources=("_CHAT",), handle=lambda c, x, p: (0, {}))
    monkeypatch.setattr(runner, "CHANNELS", (*runner.CHANNELS, probe))

    result = run_extract(DAY, cfg, db_path=db, llm=_SelectiveLLM(), only=["events", "zzz_probe"])

    assert result.channels["zzz_probe"]["status"] == "failed"
    assert "超时" in result.channels["zzz_probe"]["error"]
    assert result.channels["events"]["status"] == "ok"
    assert result.status == "partial"


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


def test_channel_receives_only_its_routed_sources(tmp_path, monkeypatch):
    """素材路由接线：通道只吃 spec.sources 声明的源，不再六通道同吃当日全量素材。"""
    cfg = Config.for_tests(tmp_path)
    write_pack_detail(cfg, DAY, "_CHAT", "〔CHAT〕", "ok", "- chat 详情", "pack", "in-pack")
    write_pack_detail(cfg, DAY, "_LONG_TERM", "〔LONG_TERM〕", "ok", "- long 详情", "pack", "in-pack")
    prompt_dir = cfg.root / "config" / "kg_prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for spec in CHANNELS:
        (prompt_dir / spec.prompt).write_text(f"prompt::{spec.key}", encoding="utf-8")
    (prompt_dir / "zzz@v1.md").write_text("prompt::zzz_probe", encoding="utf-8")

    seen: dict[str, str] = {}

    class _RecordingLLM:
        async def ainvoke(self, messages: list[Any]) -> _Resp:
            seen[str(messages[0].content)] = str(messages[1].content)
            return _Resp("{}")

    probe = ChannelSpec(
        key="zzz_probe", prompt="zzz@v1.md", sources=("_LONG_TERM",), handle=lambda c, x, p: (0, {})
    )
    monkeypatch.setattr(runner, "CHANNELS", (*runner.CHANNELS, probe))

    result = run_extract(DAY, cfg, db_path=tmp_path / "kg.db", llm=_RecordingLLM(),
                         only=["events", "zzz_probe"])

    # events 声明了 _CHAT 与 _LONG_TERM（含），zzz_probe 只声明 _LONG_TERM（_CHAT 被裁掉）
    assert "_CHAT" in seen["prompt::events"] and "_LONG_TERM" in seen["prompt::events"]
    assert "_LONG_TERM" in seen["prompt::zzz_probe"]
    assert "_CHAT" not in seen["prompt::zzz_probe"]
    assert result.channels["zzz_probe"]["prompt_chars"] < result.channels["events"]["prompt_chars"]


def test_channel_stats_carry_ops_telemetry(tmp_path):
    """每通道落库体检指标（素材字符数 / 耗时 / token 用量 / finish_reason / 尝试次数）。

    这是"哪边慢、为什么慢"的运维日志：素材侧看 prompt_chars，provider 侧看 usage 与
    finish_reason，重跑/重试痕迹看 attempts。
    """
    cfg, db = _setup(tmp_path)
    resp = _Resp(
        json.dumps(PAYLOAD, ensure_ascii=False),
        usage={"input_tokens": 5000, "output_tokens": 12000, "total_tokens": 17000},
        finish="stop",
    )

    class _TelemetryLLM:
        async def ainvoke(self, messages: list[Any]) -> _Resp:
            return resp

    result = run_extract(DAY, cfg, db_path=db, llm=_TelemetryLLM(), only=["events"])

    stat = result.channels["events"]
    assert stat["status"] == "ok"
    assert stat["prompt_chars"] > 0
    assert "_CHAT" in stat["routed_sources"]
    assert stat["attempts"] == 1
    assert stat["elapsed_ms"] >= 0
    assert stat["usage"]["output_tokens"] == 12000
    assert stat["finish_reason"] == "stop"

    # 同一份指标必须落进 kg_runs.channel_stats（重启后仍可回溯），素材侧尺寸也在其中
    stored = json.loads(_query(db, "SELECT channel_stats FROM kg_runs")[0][0])
    assert stored["events"]["usage"]["output_tokens"] == 12000
    assert stored["events"]["prompt_chars"] > 0
    assert stored["_material"]["by_source_chars"]["_CHAT"] > 0


def test_extract_llm_budget_is_wider_and_never_retries(monkeypatch):
    """抽取路径的客户端：超时放宽到 KG_EXTRACT_LLM_TIMEOUT（默认 600s），且不重试。

    推理模型的合法长生成可达 400s+，用交互路径的 300s 预算会把正常生成误杀成超时；
    而超时的请求重试基本还会超时，重试只会再空等一个完整预算。
    """
    monkeypatch.setenv("LLM_PROVIDER", "zhipu")
    monkeypatch.setenv("ZHIPU_API_KEY", "test.zhipu-secret")
    monkeypatch.delenv("KG_EXTRACT_LLM_TIMEOUT", raising=False)

    llm = runner._resolve_llm(None)
    assert llm.request_timeout == 600.0
    assert llm.max_retries == 0

    monkeypatch.setenv("KG_EXTRACT_LLM_TIMEOUT", "120")
    assert runner._resolve_llm(None).request_timeout == 120.0

    monkeypatch.setenv("KG_EXTRACT_LLM_TIMEOUT", "abc")
    assert runner._resolve_llm(None).request_timeout == 600.0
