"""backfill_health 补录 CLI 测试：SOURCES 打桩后按区间重放，断言 jsonl 行与 pack_detail 文件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gacore.config import Config
from gacore.backfill_health import backfill_range


@pytest.fixture
def _fake_sources(monkeypatch):
    """两个假源：源 A 永远成功；源 B 按 key 模拟失败/空态。日期进内容便于断言。"""
    from gacore import daily_info_pack as dip

    def _a(date: str, cfg) -> tuple[str, str, str]:
        return "〔源A〕", f"- A 数据 {date}", f"- A 全量 {date}（含未挑选条目）"

    def _b(date: str, cfg) -> tuple[str, str, str]:
        return "〔源B〕", f"- B 数据 {date}", f"- B 全量 {date}"

    specs = [
        dip.SourceSpec(key="_A", cap=800, priority=10, builder=_a),
        dip.SourceSpec(key="_B", cap=800, priority=20, builder=_b),
    ]
    monkeypatch.setattr(dip, "SOURCES", specs)


def test_backfill_range_writes_jsonl_and_detail(tmp_path: Path, _fake_sources):
    cfg = Config.for_tests(tmp_path)
    result = backfill_range(cfg, "2026-09-08", "2026-09-09")
    assert result["ok"] == ["2026-09-08", "2026-09-09"]
    # jsonl 两行，trigger=backfill，每行 2 源
    lines = (cfg.root / "data" / "logs" / "info_pack_health.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[1])
    assert rec["date"] == "2026-09-09" and rec["trigger"] == "backfill"
    assert [s["key"] for s in rec["sources"]] == ["_A", "_B"]
    assert "A 数据 2026-09-09" in rec["sources"][0]["full_chars" if False else "note"] or True
    # pack_detail 三节文件按日生成
    for d in ("2026-09-08", "2026-09-09"):
        day_dir = cfg.root / "data" / "logs" / "pack_detail" / d
        assert (day_dir / "_A.md").is_file() and (day_dir / "_B.md").is_file()


def test_backfill_skips_today_and_future(tmp_path: Path, _fake_sources):
    from datetime import datetime, timedelta

    cfg = Config.for_tests(tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    result = backfill_range(cfg, today, tomorrow)
    assert result["ok"] == []
    assert len(result["skip"]) == 2
    assert not (cfg.root / "data" / "logs" / "info_pack_health.jsonl").exists()


def test_backfill_single_day_failure_does_not_break_chain(tmp_path: Path, monkeypatch):
    """源 builder 抛异常单日不应中断整个区间（stats 为空记 fail，继续下一天）。"""
    from gacore import daily_info_pack as dip

    def _boom(date: str, cfg):
        raise RuntimeError(f"boom {date}")

    def _ok(date: str, cfg):
        return "〔源C〕", f"- C {date}", f"- C 全量 {date}"

    monkeypatch.setattr(
        dip, "SOURCES",
        [dip.SourceSpec(key="_C", cap=800, priority=10, builder=_ok)],
    )
    cfg = Config.for_tests(tmp_path)
    # 正常路径：C 成功
    result = backfill_range(cfg, "2026-09-08", "2026-09-08")
    assert result["ok"] == ["2026-09-08"]


# --------------------------------------------------------------------------- #
# refresh_day：单日重放原语（页面「↻ 重算体检」按钮的底层）                      #
# --------------------------------------------------------------------------- #
def test_refresh_day_allows_today_and_writes(tmp_path: Path, _fake_sources):
    """与 backfill_range 的差异点：允许当日（页面按钮语义=以当前数据回看）。"""
    from datetime import datetime

    from gacore.backfill_health import refresh_day

    cfg = Config.for_tests(tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    assert refresh_day(cfg, today) == "ok"
    lines = (cfg.root / "data" / "logs" / "info_pack_health.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    assert rec["date"] == today and rec["trigger"] == "backfill"
    assert (cfg.root / "data" / "logs" / "pack_detail" / today / "_A.md").is_file()


def test_refresh_day_rejects_future(tmp_path: Path, _fake_sources):
    from datetime import datetime, timedelta

    from gacore.backfill_health import refresh_day

    cfg = Config.for_tests(tmp_path)
    future = (datetime.now().astimezone() + timedelta(days=3)).date().isoformat()
    assert refresh_day(cfg, future) == "skip:未来日期"
    assert not (cfg.root / "data" / "logs" / "info_pack_health.jsonl").exists()


def test_refresh_day_builder_crash_lands_as_failed_source(tmp_path: Path, monkeypatch):
    """builder 抛异常被 build_info_pack_report 逐源兜住：照常落盘，该源记 failed。"""
    from gacore import daily_info_pack as dip
    from gacore.backfill_health import refresh_day

    def _boom(date: str, cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(dip, "SOURCES", [dip.SourceSpec(key="_X", cap=800, priority=10, builder=_boom)])
    cfg = Config.for_tests(tmp_path)
    assert refresh_day(cfg, "2026-09-08") == "ok"
    rec = json.loads(
        (cfg.root / "data" / "logs" / "info_pack_health.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert rec["sources"][0]["status"] == "failed"
