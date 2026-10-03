"""build_info_pack_report / 双文件落盘 单测（C1 v0.7）。

覆盖：
- monkeypatch SOURCES 后的四态分类（ok/empty/failed/missing_data）与 header 状态元信息；
- chars / full_chars / detail_chars 三级计数（渲染文本→行级截断→完整取数详情）；
- info_pack_health.jsonl 行结构与字段投影；
- pack_detail 三节详情文件内容（完整取数详情/渲染文本/实际进包）；
- pack_detail 90 天保留期清理（非日期目录跳过）；
- 预算熔断整块丢弃：chars=0 且 note="未进包:预算熔断"；首块超预算才字符裁剪兜底。

运行：uv run --package withlanggraph pytest apps/withlanggraph/tests/test_info_pack_report.py -q
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

import gacore.daily_info_pack as dip
from gacore.config import Config
from gacore.daily_info_pack import _CN_TZ  # 受测模块的东八区时区（ts / 保留期口径）

_DATE = "2026-09-02"


def _cfg(tmp_path: Path) -> Config:
    return Config.for_tests(tmp_path)


def _spec(key: str, pack_body: str, detail_body: str = "", cap: int = 400, priority: int = 10) -> dip.SourceSpec:
    """构造假源：builder 直接返回给定三元组，title 由 key 派生（与真实 builder 风格一致）。"""
    return dip.SourceSpec(
        key=key,
        cap=cap,
        priority=priority,
        builder=lambda d, c, _k=key, _b=pack_body, _d=detail_body: (f"〔{_k.strip('_')}〕", _b, _d),
    )


# --------------------------------------------------------------------------- #
# 四态分类 + header 状态元信息（C3）                                           #
# --------------------------------------------------------------------------- #
def test_report_classifies_four_statuses_and_stamps_headers(tmp_path, monkeypatch):
    fake = (
        _spec("_OKSRC", "- 正常内容行"),
        _spec("_EMPTYSRC", "", detail_body=""),
        _spec("_FAILSRC", "- 该源失败：boom"),
        _spec("_MISSSRC", "- 今日无 B 站观看记录"),
    )
    monkeypatch.setattr(dip, "SOURCES", fake)
    pack, stats = dip.build_info_pack_report(_DATE, _cfg(tmp_path))

    assert [s["status"] for s in stats] == ["ok", "empty", "failed", "missing_data"]
    # header 状态元信息：全量 / 失败:原因 / 无数据
    assert "〔OKSRC｜状态:全量〕" in pack
    assert "〔FAILSRC｜状态:失败:boom〕" in pack
    assert "〔MISSSRC｜状态:无数据〕" in pack
    # empty 源整块跳过（不留 header、不占预算）
    assert "EMPTYSRC" not in pack


def test_report_three_level_char_counts(tmp_path, monkeypatch):
    lines = ["- 第一行内容", "- 第二行内容", "- 第三行内容"]
    body = "\n".join(lines)
    fake = (_spec("_COUNTED", body, detail_body="D" * 123, cap=10),)
    monkeypatch.setattr(dip, "SOURCES", fake)
    pack, stats = dip.build_info_pack_report(_DATE, _cfg(tmp_path))

    st = stats[0]
    expected_capped = "- 第一行内容\n（已截断 2 行，完整数据可经 langTrack_stats / search_daily 补查）"
    assert st["full_chars"] == len(body)             # 渲染文本（截断前）
    assert st["chars"] == len(expected_capped)        # 实际进包 = 行级截断后（行末截停，不断章）
    assert expected_capped in pack
    assert st["detail_chars"] == 123                  # 完整取数详情（未挑选未压缩）
    assert st["note"] == "truncated=2"


# --------------------------------------------------------------------------- #
# info_pack_health.jsonl                                                       #
# --------------------------------------------------------------------------- #
def test_write_pack_health_appends_jsonl_line(tmp_path):
    cfg = _cfg(tmp_path)
    stats = [
        {"key": "_BILI", "status": "failed", "chars": 10, "full_chars": 20,
         "detail_chars": 30, "note": "登录过期", "title": "〔多余字段应被投影掉〕"},
        {"key": "_GIT", "status": "ok", "chars": 5, "full_chars": 5,
         "detail_chars": 9, "note": ""},
    ]
    dip.write_pack_health(cfg, _DATE, "daily-report", "scheduled", stats, total_chars=777, correction_chars=0)

    path = cfg.root / "data" / "logs" / "info_pack_health.jsonl"
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["date"] == _DATE
    assert record["job"] == "daily-report"
    assert record["trigger"] == "scheduled"
    assert record["total_chars"] == 777
    assert record["budget"] == dip.PACK_BUDGET
    assert record["correction_chars"] == 0
    # ts 为东八区格式
    _dt.datetime.strptime(record["ts"], "%Y-%m-%d %H:%M:%S")
    # sources 只投影六字段
    assert record["sources"] == [
        {"key": "_BILI", "status": "failed", "chars": 10, "full_chars": 20,
         "detail_chars": 30, "note": "登录过期"},
        {"key": "_GIT", "status": "ok", "chars": 5, "full_chars": 5,
         "detail_chars": 9, "note": ""},
    ]
    # 再写一行 → 追加不覆盖
    dip.write_pack_health(cfg, _DATE, "daily-report", "rerun", stats, 1)
    assert len((path.read_text(encoding="utf-8")).splitlines()) == 2


def test_write_pack_health_swallows_errors(tmp_path):
    """落盘失败（目标路径被目录占用 → open 抛 OSError）只告警，不抛给调用方。"""
    cfg = _cfg(tmp_path)
    blocker = cfg.root / "data" / "logs" / "info_pack_health.jsonl"
    blocker.parent.mkdir(parents=True)
    blocker.mkdir()
    dip.write_pack_health(cfg, _DATE, "daily-report", "scheduled", [], 0)  # 不抛即通过


# --------------------------------------------------------------------------- #
# pack_detail 三节详情                                                         #
# --------------------------------------------------------------------------- #
def test_write_pack_detail_three_sections(tmp_path):
    cfg = _cfg(tmp_path)
    dip.write_pack_detail(
        cfg, _DATE, "_BILI", "〔浏览·B站观看 top〕", "failed",
        detail_body="- 全部 10 条观看", pack_body="- 前 5 条观看", packed_body="",
    )
    path = cfg.root / "data" / "logs" / "pack_detail" / _DATE / "_BILI.md"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == (
        "# _BILI · 2026-09-02 · failed\n"
        "\n"
        "## 完整取数详情\n- 全部 10 条观看\n"
        "\n"
        "## 渲染文本\n- 前 5 条观看\n"
        "\n"
        "## 实际进包\n未进包（预算熔断）\n"
    )


def test_write_pack_detail_empty_detail_renders_placeholder(tmp_path):
    cfg = _cfg(tmp_path)
    dip.write_pack_detail(cfg, _DATE, "_CHAT", "〔对话〕", "empty", "", "", "")
    text = (cfg.root / "data" / "logs" / "pack_detail" / _DATE / "_CHAT.md").read_text(encoding="utf-8")
    assert "## 完整取数详情\n（无）" in text


def test_write_pack_detail_cleans_dirs_over_90_days(tmp_path):
    cfg = _cfg(tmp_path)
    root = cfg.root / "data" / "logs" / "pack_detail"
    today = _dt.datetime.now(_CN_TZ).date()
    old = root / (today - _dt.timedelta(days=91)).isoformat()
    edge = root / (today - _dt.timedelta(days=90)).isoformat()  # 恰好 90 天：保留
    junk = root / "not-a-date"                                   # 非日期目录：跳过
    for d in (old, edge, junk):
        d.mkdir(parents=True)
        (d / "keep.txt").write_text("x", encoding="utf-8")

    dip.write_pack_detail(cfg, _DATE, "_GIT", "t", "ok", "d", "p", "pk")

    assert not old.exists()   # 超期目录被清理
    assert edge.exists()      # 90 天整不清理
    assert junk.exists()      # 非日期目录跳过
    assert (root / _DATE / "_GIT.md").is_file()


# --------------------------------------------------------------------------- #
# 预算熔断：整块丢弃 + 首块字符裁剪兜底（C3）                                   #
# --------------------------------------------------------------------------- #
def test_budget_circuit_breaker_drops_whole_blocks(tmp_path, monkeypatch):
    # cap=1000：让 600 字的假源体不被行级截断，只考验整包预算熔断
    heavy_a = _spec("_HEAVYA", "A" * 600, priority=10, cap=1000)
    heavy_b = _spec("_HEAVYB", "B" * 600, priority=20, cap=1000)
    monkeypatch.setattr(dip, "SOURCES", (heavy_a, heavy_b))
    monkeypatch.setattr(dip, "PACK_BUDGET", 1200)
    pack, stats = dip.build_info_pack_report(_DATE, _cfg(tmp_path))

    assert 0 < len(pack) <= 1200
    assert "〔HEAVYA｜状态:全量〕" in pack
    assert "HEAVYB" not in pack                    # 装不下 → 整块丢弃（其后全弃）
    assert not pack.rstrip().endswith("…")          # 丢弃不产生半行
    assert stats[0]["chars"] == 600 and stats[0]["note"] == ""
    assert stats[1]["chars"] == 0                   # 熔断丢弃 → 进包为 0
    assert stats[1]["note"] == "未进包:预算熔断"
    assert stats[1]["full_chars"] == 600            # 但渲染文本计数不受影响


def test_first_block_over_budget_falls_back_to_char_clip(tmp_path, monkeypatch):
    big = _spec("_BIG", "X" * 500, priority=10)
    monkeypatch.setattr(dip, "SOURCES", (big,))
    monkeypatch.setattr(dip, "PACK_BUDGET", 100)
    pack, stats = dip.build_info_pack_report(_DATE, _cfg(tmp_path))

    assert len(pack) <= 100
    assert pack.endswith("…")                       # 首块（消费指令头）超预算 → 字符裁剪兜底
    assert stats[0]["chars"] == 0
    assert stats[0]["note"] == "未进包:预算熔断"


# --------------------------------------------------------------------------- #
# 薄包装与 stats 暂存（scheduler seam）                                        #
# --------------------------------------------------------------------------- #
def test_build_info_pack_thin_wrapper_and_last_pack_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(dip, "SOURCES", (_spec("_OKSRC", "- 内容"),))
    cfg = _cfg(tmp_path)
    pack = dip.build_info_pack(_DATE, cfg)

    assert "〔OKSRC｜状态:全量〕" in pack
    assert "〔当日信息包·2026-09-02〕" in pack
    stats = dip.last_pack_stats()
    assert [s["key"] for s in stats] == ["_OKSRC"]
    assert set(stats[0]) >= {"key", "title", "status", "chars", "full_chars",
                             "detail_chars", "note", "detail_body", "pack_body", "packed_body"}


def test_report_never_raises_when_builder_explodes(tmp_path, monkeypatch):
    def _boom(date, cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(dip, "SOURCES", (dip.SourceSpec("_BOOM", 400, 10, _boom),))
    pack, stats = dip.build_info_pack_report(_DATE, _cfg(tmp_path))
    assert isinstance(pack, str) and len(pack) <= dip.PACK_BUDGET
    assert stats[0]["status"] == "failed"
    assert "〔BOOM｜状态:失败:boom〕" in pack


@pytest.mark.parametrize("missing", ["detail_body", "pack_body", "packed_body"])
def test_stats_missing_body_defaults(tmp_path, monkeypatch, missing):
    """手拼 stats 缺三节正文键时，write_pack_detail 仍可安全落盘（scheduler 容错）。"""
    cfg = _cfg(tmp_path)
    st = {"key": "_X", "title": "〔X〕", "status": "ok", "detail_body": "d",
          "pack_body": "p", "packed_body": "pk"}
    st.pop(missing)
    dip.write_pack_detail(cfg, _DATE, st.get("key", ""), st.get("title", ""), st.get("status", ""),
                          st.get("detail_body", ""), st.get("pack_body", ""), st.get("packed_body", ""))
    assert (cfg.root / "data" / "logs" / "pack_detail" / _DATE / "_X.md").is_file()
