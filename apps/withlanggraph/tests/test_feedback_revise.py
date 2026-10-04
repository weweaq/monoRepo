"""Tests for the C4 repair-ladder additions in feedback.py.

Covers: the corrections/preferences store (Q3=A per-day JSON), the C5 report-version
counter, and the LLM minimal-revision path (ladder ②) — zero-tool single-turn call, hard
diff gate, stricter retry, deterministic append-degrade — plus the ①→② upgrade hook
revise_from_pending. The LLM is monkeypatched at the gacore.feedback.get_llm seam; all
runs use Config.for_tests(tmp_path) so nothing touches real project dirs.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

from gacore.config import Config
from gacore.feedback import (
    apply_correction,
    current_report_version,
    list_active_corrections,
    load_delivered,
    next_report_version,
    record_correction,
    revise_report_llm,
    save_delivered,
)

# Pre-stamped delivered report: every bullet already carries its [节-序号] anchor and the
# text ends without a trailing newline, so save_delivered's stamp_report_bullets pass is a
# byte-for-byte no-op and the delivered archive equals this text exactly (verified by
# test_delivered_archive_equals_report). Blank lines between sections exercise the
# section-separation preservation in the fallback append.
REPORT = (
    "# 今日状态\n"
    "- [今日状态-1] 状态一句话\n\n"
    "# 工作日志\n"
    "- [工作日志-1] git 9 提交\n"
    "- [工作日志-2] 上午收 9-08 日报尾巴\n\n"
    "# 个人观察\n"
    "- [个人观察-1] LPL 老线再加深"
)

DATE = "2026-10-03"


def _deliver(cfg: Config, date: str = DATE, body: str = REPORT) -> None:
    save_delivered(cfg, date, body)


def _section_of(text: str, heading: str) -> str:
    """The body of one ``# heading`` section, cut before the next heading."""
    return text.split(f"# {heading}\n", 1)[1].split("\n# ", 1)[0]


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _ScriptedLLM:
    """Fake chat model: each .invoke consumes the next scripted string, recording calls."""

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return _FakeResp(self._contents.pop(0))


def _patch_llm(monkeypatch, contents: list[str]):
    """Point gacore.feedback.get_llm at a scripted fake; returns (model, seen) where seen
    records the zero-tool call arguments (tools/env/bind_tools)."""
    model = _ScriptedLLM(contents)
    seen: dict = {}

    def _factory(tool_list, env=None, **kwargs):
        seen["tools"] = list(tool_list)
        seen["env"] = env
        seen["kwargs"] = kwargs
        return model

    monkeypatch.setattr("gacore.feedback.get_llm", _factory)
    return model, seen


# --------------------------------------------------------------------------- corrections store


class TestRecordCorrection:
    def test_fact_record_fields_complete(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        rec = record_correction(cfg, DATE, "[工作日志-2]", "fact", "其实下午去了朝阳大悦城")
        assert rec["id"] == "c-20261003-01"
        assert rec["anchor"] == "[工作日志-2]"
        assert rec["kind"] == "fact"
        assert rec["text"] == "其实下午去了朝阳大悦城"
        assert rec["status"] == "active"
        # East-8 wall-clock timestamps with second precision.
        for key in ("created_at", "updated_at"):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rec[key])
        path = cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == [rec]

    def test_same_anchor_supersedes_old(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        record_correction(cfg, DATE, "[工作日志-2]", "fact", "v1")
        r2 = record_correction(cfg, DATE, "[工作日志-2]", "fact", "v2")
        assert r2["id"] == "c-20261003-02"
        recs = json.loads(
            (cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json").read_text(encoding="utf-8")
        )
        assert [(r["id"], r["status"]) for r in recs] == [
            ("c-20261003-01", "superseded"),
            ("c-20261003-02", "active"),
        ]
        # A different anchor must NOT supersede anything.
        r3 = record_correction(cfg, DATE, "[工作日志-1]", "fact", "v3")
        assert r3["id"] == "c-20261003-03"
        recs = json.loads(
            (cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json").read_text(encoding="utf-8")
        )
        assert {r["id"]: r["status"] for r in recs} == {
            "c-20261003-01": "superseded",
            "c-20261003-02": "active",
            "c-20261003-03": "active",
        }

    def test_pref_routes_to_preferences_file(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        rec = record_correction(cfg, DATE, "[个人观察-1]", "pref", "个人观察节不要罗列数据")
        assert rec["kind"] == "pref"
        assert rec["status"] == "active"
        prefs = json.loads((cfg.root / "data" / "feedback" / "preferences.json").read_text(encoding="utf-8"))
        assert len(prefs) == 1
        assert prefs[0]["text"] == "个人观察节不要罗列数据"
        assert prefs[0]["id"].startswith("p-20261003-")
        # fact 与 pref 分存储：pref 不产生当日 corrections 文件。
        assert not (cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json").exists()

    def test_bad_json_tolerated_as_empty(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        path = cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        rec = record_correction(cfg, DATE, "[工作日志-1]", "fact", "x")
        assert rec["id"] == "c-20261003-01"  # bad store read as empty, sequence restarts at 01
        assert len(json.loads(path.read_text(encoding="utf-8"))) == 1


class TestListActiveCorrections:
    def test_filters_status_and_kind_and_sorts(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        record_correction(cfg, DATE, "[工作日志-2]", "fact", "v1")
        record_correction(cfg, DATE, "[工作日志-2]", "fact", "v2")
        record_correction(cfg, DATE, "[工作日志-1]", "fact", "v3")
        record_correction(cfg, DATE, "-", "pref", "偏好一条")
        active = list_active_corrections(cfg, DATE)
        assert [r["id"] for r in active] == ["c-20261003-02", "c-20261003-03"]
        assert all(r["kind"] == "fact" and r["status"] == "active" for r in active)
        # Other days are isolated (per-day file) and missing files read as empty.
        assert list_active_corrections(cfg, "2026-10-02") == []


# --------------------------------------------------------------------------- version counter (C5)


class TestVersionCounter:
    def test_first_delivery_is_one_and_not_persisted(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        assert current_report_version(cfg, DATE) == 1
        assert not (cfg.root / "data" / "feedback" / "versions.json").exists()

    def test_next_increments_and_persists(self, tmp_path: Path):
        cfg = Config.for_tests(tmp_path)
        assert next_report_version(cfg, DATE) == 2
        assert current_report_version(cfg, DATE) == 2
        assert next_report_version(cfg, DATE) == 3
        assert current_report_version(cfg, DATE) == 3
        data = json.loads((cfg.root / "data" / "feedback" / "versions.json").read_text(encoding="utf-8"))
        assert data == {DATE: 3}
        # Versions are per-date; another day still reads as first delivery.
        assert current_report_version(cfg, "2026-10-02") == 1


# --------------------------------------------------------------------------- LLM minimal revision (ladder ②)


class TestReviseReportLlm:
    def test_gate_pass_single_section(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        revised = REPORT.replace("- [工作日志-2] 上午收 9-08 日报尾巴", "- [工作日志-2] 上午实际去了朝阳大悦城")
        model, seen = _patch_llm(monkeypatch, [revised])
        res = revise_report_llm(cfg, DATE, [{"anchor": "[工作日志-2]", "text": "上午实际去了朝阳大悦城"}])
        assert res["ok"] is True
        assert res["error"] == ""
        assert res["diff_ok"] is True
        assert res["fallback"] is False
        assert res["sections_changed"] == ["工作日志"]
        assert res["text"].rstrip() == revised.rstrip()
        # Zero-tool guarantee: empty tool list, env from os.environ after load_dotenv,
        # and no bind_tools binding.
        assert seen["tools"] == []
        assert seen["env"] is os.environ
        assert seen["kwargs"].get("bind_tools") is False
        assert len(model.calls) == 1
        # Prompt shape: system rules + user carrying full text and the correction item.
        assert "逐字保留" in model.calls[0][0].content
        user = model.calls[0][1].content
        assert "已投递日报全文" in user and "[工作日志-2]" in user and "上午实际去了朝阳大悦城" in user

    def test_retry_after_gate_failure_then_ok(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        bad = REPORT.replace(
            "- [工作日志-2] 上午收 9-08 日报尾巴", "- [工作日志-2] 上午实际去了朝阳大悦城"
        ).replace("- [个人观察-1] LPL 老线再加深", "- [个人观察-1] 关注IG了")
        good = REPORT.replace("- [工作日志-2] 上午收 9-08 日报尾巴", "- [工作日志-2] 上午实际去了朝阳大悦城")
        model, _seen = _patch_llm(monkeypatch, [bad, good])
        res = revise_report_llm(cfg, DATE, [{"anchor": "[工作日志-2]", "text": "上午实际去了朝阳大悦城"}])
        assert len(model.calls) == 2  # stricter retry happened
        assert res["ok"] is True
        assert res["diff_ok"] is True
        assert res["fallback"] is False
        assert res["text"].rstrip() == good.rstrip()
        assert res["sections_changed"] == ["工作日志"]
        retry_user = model.calls[1][1].content
        assert "个人观察" in retry_user  # stricter retry names the violated section

    def test_two_gate_failures_degrade_to_fallback_append(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        bad = REPORT.replace(
            "- [工作日志-2] 上午收 9-08 日报尾巴", "- [工作日志-2] 上午实际去了朝阳大悦城"
        ).replace("- [个人观察-1] LPL 老线再加深", "- [个人观察-1] 关注IG了")
        model, _seen = _patch_llm(monkeypatch, [bad, bad])
        res = revise_report_llm(cfg, DATE, [{"anchor": "[工作日志-2]", "text": "上午实际去了朝阳大悦城"}])
        assert len(model.calls) == 2
        assert res["ok"] is True
        assert res["fallback"] is True
        assert res["diff_ok"] is False
        # Correction appended verbatim at the end of the target section.
        work = _section_of(res["text"], "工作日志")
        assert work.rstrip().endswith("> （人工订正）上午实际去了朝阳大悦城")
        assert res["sections_changed"] == ["工作日志"]
        # Non-target sections remain the delivered original verbatim.
        assert _section_of(res["text"], "个人观察") == _section_of(REPORT, "个人观察")
        assert _section_of(res["text"], "今日状态") == _section_of(REPORT, "今日状态")

    def test_no_delivered_fails_before_llm(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        model, _seen = _patch_llm(monkeypatch, ["不应被消费"])
        res = revise_report_llm(cfg, DATE, [{"anchor": "[工作日志-1]", "text": "x"}])
        assert res["ok"] is False
        assert res["error"] == "no_delivered"
        assert res["text"] == ""
        assert model.calls == []

    def test_anchor_not_found_fails_fast(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        model, _seen = _patch_llm(monkeypatch, ["不应被消费"])
        res = revise_report_llm(cfg, DATE, [{"anchor": "[不存在-9]", "text": "x"}])
        assert res["ok"] is False
        assert res["error"] == "anchor_not_found:[不存在-9]"
        assert model.calls == []

    def test_llm_exception_returns_error_not_raise(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)

        class _Boom:
            def invoke(self, messages):
                raise RuntimeError("network down")

        monkeypatch.setattr("gacore.feedback.get_llm", lambda *a, **k: _Boom())
        res = revise_report_llm(cfg, DATE, [{"anchor": "[工作日志-1]", "text": "x"}])
        assert res["ok"] is False
        assert "RuntimeError" in res["error"]
        assert res["text"] == ""

    def test_empty_items_fails(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        model, _seen = _patch_llm(monkeypatch, ["不应被消费"])
        res = revise_report_llm(cfg, DATE, [{"anchor": "", "text": ""}])
        assert res["ok"] is False
        assert res["error"] == "no_items"
        assert model.calls == []


class TestApplyCorrection:
    """统一订正入口（QQ 确认与评审页共用）：auto=原样替换优先、定位不到升级 ②。"""

    def test_verbatim_anchor_hit_replaces_verbatim_with_mark(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        monkeypatch.setattr(
            "gacore.feedback.get_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM must not be called")),
        )
        res = apply_correction(cfg, DATE, "[工作日志-2]", "fact", "上午实际去了朝阳大悦城", mode="verbatim")
        assert res["status"] == "ok" and res["mode_used"] == "verbatim"
        updated = (load_delivered(cfg, DATE) or "").rstrip()
        assert "- [工作日志-2] 上午实际去了朝阳大悦城（人工订正）" in updated
        assert "收 9-08 日报尾巴" in res["replaced_from"]
        assert res["correction"]["mode"] == "verbatim" and res["correction"]["replaced_from"]

    def test_auto_anchor_miss_falls_back_to_llm(self, tmp_path: Path, monkeypatch):
        """锚点存在但不是标准 bullet（原样替换定位不到）→ auto 自动升级 ②。"""
        cfg = Config.for_tests(tmp_path)
        # 把锚点行改成非 "- " 开头的引用行：_verbatim_patch 定位不到，但锚点仍在文中
        quoted = REPORT.replace(
            "- [工作日志-2] 上午收 9-08 日报尾巴", "> 引用：[工作日志-2] 上午收 9-08 日报尾巴"
        )
        save_delivered(cfg, DATE, quoted)
        monkeypatch.setattr(
            "gacore.feedback.get_llm",
            lambda *a, **k: SimpleNamespace(invoke=lambda m: SimpleNamespace(content=quoted)),
        )
        res = apply_correction(cfg, DATE, "[工作日志-2]", "fact", "x")
        assert res["status"] == "ok" and res["mode_used"] == "llm"
        assert "✎ 人工订正：[工作日志-2]" in (load_delivered(cfg, DATE) or "")

    def test_verbatim_forced_miss_errors_without_llm(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        monkeypatch.setattr(
            "gacore.feedback.get_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM must not be called")),
        )
        res = apply_correction(cfg, DATE, "[不存在-9]", "fact", "x", mode="verbatim")
        assert res["status"] == "error" and "no bullet" in res["error"]
        assert res["mode_used"] == ""

    def test_append_intent_keeps_sub_bullet_semantics(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        monkeypatch.setattr(
            "gacore.feedback.get_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM must not be called")),
        )
        res = apply_correction(cfg, DATE, "[工作日志-1]", "fact", "补一条：下午去了大悦城", mode="verbatim", intent="append")
        assert res["status"] == "ok" and res["mode_used"] == "verbatim"
        updated = (load_delivered(cfg, DATE) or "")
        assert "- [反馈] 补一条：下午去了大悦城（人工订正）" in updated

    def test_pref_records_only_without_touching_report(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _deliver(cfg)
        before = load_delivered(cfg, DATE)
        res = apply_correction(cfg, DATE, "-", "pref", "个人观察节不要罗列数据", note="排版偏好")
        assert res["status"] == "ok" and res["mode_used"] == "recorded"
        assert (load_delivered(cfg, DATE) or "") == before
        import json as _json
        prefs = _json.loads((cfg.root / "data" / "feedback" / "preferences.json").read_text(encoding="utf-8"))
        assert prefs[0]["note"] == "排版偏好"

    def test_no_delivered_report_errors(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        res = apply_correction(cfg, DATE, "[工作日志-1]", "fact", "x")
        assert res["status"] == "error" and "no delivered report" in res["error"]
