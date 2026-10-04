"""Tests for gacore.review_server (C6/S4 web review page).

FastAPI TestClient + Config.for_tests(tmp_path); everything isolated:
- the LLM is faked at the gacore.feedback.get_llm seam (same as test_feedback_revise);
- email delivery is faked at gacore.scheduler._deliver (redeliver_day imports it at call time);
- rerun's load_jobs/run_job are faked on the gacore.scheduler module attributes
  (review_server calls them via `scheduler.<name>` so the patches take effect);
- load_dotenv is stubbed so the developer's real .env can never leak a REVIEW_TOKEN in.
Health fixtures (info_pack_health.jsonl / pack_detail/*.md) are hand-written since the
writer side (S1) is a separate workstream — this module only reads.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gacore.config import Config
from gacore.feedback import load_delivered, save_delivered
from gacore.review_server import create_app

TOKEN = "test-token"
DATE = "2026-10-03"

REPORT = (
    "# 今日状态\n"
    "- [今日状态-1] 状态一句话\n\n"
    "# 工作日志\n"
    "- [工作日志-1] git 9 提交\n"
    "- [工作日志-2] 上午收 9-08 日报尾巴\n\n"
    "# 个人观察\n"
    "- [个人观察-1] LPL 老线再加深"
)

REVISED = REPORT.replace("- [工作日志-2] 上午收 9-08 日报尾巴", "- [工作日志-2] 上午实际去了朝阳大悦城")


def _auth(monkeypatch, token: str | None) -> None:
    """Deterministic auth env: no real .env, REVIEW_TOKEN exactly as given (None = unset)."""
    monkeypatch.setattr("gacore.review_server.load_dotenv", lambda: None)
    if token is None:
        monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    else:
        monkeypatch.setenv("REVIEW_TOKEN", token)


def _client(cfg: Config, monkeypatch, token: str | None = TOKEN) -> TestClient:
    _auth(monkeypatch, token)
    return TestClient(create_app(cfg))


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"X-Review-Token": token}


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- auth


class TestAuth:
    def test_post_401_when_token_unset(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch, token=None)
        r = c.post("/api/corrections", json={"date": DATE, "anchor": "[工作日志-1]", "kind": "fact", "text": "x"})
        assert r.status_code == 401
        assert r.json()["ok"] is False

    def test_post_401_when_token_wrong(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch)
        r = c.post("/api/corrections", json={"date": DATE, "anchor": "[工作日志-1]", "kind": "fact", "text": "x"},
                   headers={"X-Review-Token": "wrong"})
        assert r.status_code == 401
        assert r.json()["ok"] is False

    def test_post_401_when_header_missing(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch)
        r = c.post("/api/corrections", json={"date": DATE, "anchor": "[工作日志-1]", "kind": "fact", "text": "x"})
        assert r.status_code == 401

    def test_get_pages_public_without_token(self, tmp_path: Path, monkeypatch):
        """GET 公开：无 token 头也能访问页面与 GET API。"""
        c = _client(Config.for_tests(tmp_path), monkeypatch, token=None)
        assert c.get(f"/review/{DATE}").status_code == 200
        assert c.get("/health").status_code == 200
        assert c.get(f"/api/corrections/{DATE}").status_code == 200
        assert c.get(f"/api/rerun/{DATE}/status").status_code == 200

    def test_post_ok_with_correct_token(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        c = _client(cfg, monkeypatch)
        r = c.post("/api/corrections", json={"date": DATE, "anchor": "[工作日志-1]", "kind": "fact", "text": "x"},
                   headers=_headers())
        assert r.status_code == 200
        assert r.json()["ok"] is True


# --------------------------------------------------------------------------- POST /api/corrections


class TestApiCorrections:
    def test_fact_record_persisted(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        c = _client(cfg, monkeypatch)
        r = c.post("/api/corrections", json={"date": DATE, "anchor": "[工作日志-2]", "kind": "fact", "text": "去了大悦城"},
                   headers=_headers())
        assert r.status_code == 200
        rec = r.json()["record"]
        assert rec["id"] == "c-20261003-01" and rec["kind"] == "fact" and rec["status"] == "active"
        path = cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == [rec]

    def test_pref_record_persisted(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        c = _client(cfg, monkeypatch)
        r = c.post("/api/corrections", json={"date": DATE, "anchor": "[个人观察-1]", "kind": "pref", "text": "不要罗列数据"},
                   headers=_headers())
        assert r.status_code == 200
        rec = r.json()["record"]
        assert rec["kind"] == "pref"
        prefs = json.loads((cfg.root / "data" / "feedback" / "preferences.json").read_text(encoding="utf-8"))
        assert prefs == [rec]
        assert not (cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json").exists()

    def test_get_returns_active_and_all(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        c = _client(cfg, monkeypatch)
        h = _headers()
        c.post("/api/corrections", json={"date": DATE, "anchor": "[工作日志-2]", "kind": "fact", "text": "v1"}, headers=h)
        c.post("/api/corrections", json={"date": DATE, "anchor": "[工作日志-2]", "kind": "fact", "text": "v2"}, headers=h)
        j = c.get(f"/api/corrections/{DATE}").json()
        assert j["ok"] is True
        assert [r["id"] for r in j["active"]] == ["c-20261003-02"]
        assert [r["id"] for r in j["all"]] == ["c-20261003-01", "c-20261003-02"]


# --------------------------------------------------------------------------- POST /api/revise


class TestApiRevise:
    def test_revise_verbatim_mode_replaces_and_redelivers(self, tmp_path: Path, monkeypatch):
        """verbatim：锚点能定位 → 原样替换（不走 LLM），bullet 带（人工订正）标记，
        replaced_from 入审计，重投命中 mock 的 _deliver。"""
        cfg = Config.for_tests(tmp_path)
        save_delivered(cfg, DATE, REPORT)
        deliveries: list[dict] = []

        def _fake_deliver(job, cfg_, reply, error, for_day=None):
            deliveries.append({"name": job.name, "for_day": for_day, "reply": reply})

        monkeypatch.setattr("gacore.scheduler._deliver", _fake_deliver)
        monkeypatch.setattr(
            "gacore.scheduler.load_jobs",
            lambda cfg_: [SimpleNamespace(name="daily-report")],
        )
        # get_llm 不 mock——auto+锚点可定位时必须零 LLM 调用；误调用会打到真实 API，
        # 这里塞一个会炸的 fake 让问题立刻显形
        monkeypatch.setattr(
            "gacore.feedback.get_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM must not be called")),
        )

        c = _client(cfg, monkeypatch)
        r = c.post(
            "/api/revise",
            json={"date": DATE, "items": [{"anchor": "[工作日志-2]", "kind": "fact", "text": "上午实际去了朝阳大悦城", "mode": "verbatim"}]},
            headers=_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True
        assert j["results"][0]["mode_used"] == "verbatim"
        assert j["revise"]["diff_ok"] is True
        # 存档 = 订正词原文 + （人工订正）标记
        updated = (load_delivered(cfg, DATE) or "").rstrip()
        assert "- [工作日志-2] 上午实际去了朝阳大悦城（人工订正）" in updated
        # 审计含 mode 与被替换原句
        recs = json.loads((cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json").read_text(encoding="utf-8"))
        assert recs[0]["mode"] == "verbatim"
        assert "收 9-08 日报尾巴" in recs[0]["replaced_from"]
        # 重投的是修订后的存档
        assert j["redeliver"]["status"] == "ok"
        assert len(deliveries) == 1 and deliveries[0]["for_day"] == DATE
        assert "朝阳大悦城" in deliveries[0]["reply"]

    def test_revise_default_mode_is_llm(self, tmp_path: Path, monkeypatch):
        """缺省（不带 mode 字段）= LLM 改写：走零工具修订，文末追加 ✎ 人工订正脚注。"""
        cfg = Config.for_tests(tmp_path)
        save_delivered(cfg, DATE, REPORT)
        monkeypatch.setattr(
            "gacore.feedback.get_llm",
            lambda *a, **k: SimpleNamespace(invoke=lambda messages: SimpleNamespace(content=REVISED)),
        )
        monkeypatch.setattr("gacore.scheduler._deliver", lambda *a, **k: None)
        monkeypatch.setattr(
            "gacore.scheduler.load_jobs", lambda cfg_: [SimpleNamespace(name="daily-report")]
        )
        c = _client(cfg, monkeypatch)
        r = c.post(
            "/api/revise",
            json={"date": DATE, "items": [{"anchor": "[工作日志-2]", "kind": "fact", "text": "上午实际去了朝阳大悦城"}]},
            headers=_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True and j["results"][0]["mode_used"] == "llm"
        updated = (load_delivered(cfg, DATE) or "").rstrip()
        assert "✎ 人工订正：[工作日志-2]" in updated
        recs = json.loads((cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json").read_text(encoding="utf-8"))
        assert recs[0]["mode"] == "llm"

    def test_revise_verbatim_forced_miss_errors_without_llm(self, tmp_path: Path, monkeypatch):
        """verbatim 强制模式锚点定位不到 → 显式报错，不静默转 LLM。"""
        cfg = Config.for_tests(tmp_path)
        save_delivered(cfg, DATE, REPORT)
        monkeypatch.setattr(
            "gacore.feedback.get_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM must not be called")),
        )
        c = _client(cfg, monkeypatch)
        r = c.post(
            "/api/revise",
            json={"date": DATE, "items": [{"anchor": "[不存在-9]", "kind": "fact", "text": "x", "mode": "verbatim"}]},
            headers=_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is False
        assert "no bullet" in j["results"][0]["error"]

    def test_revise_pref_item_lands_in_preferences_without_llm_or_mail(self, tmp_path: Path, monkeypatch):
        """pref 只落偏好库：不改正文、不调 LLM、不触发重发。"""
        cfg = Config.for_tests(tmp_path)
        save_delivered(cfg, DATE, REPORT)
        monkeypatch.setattr(
            "gacore.feedback.get_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM must not be called")),
        )
        deliveries: list[dict] = []
        monkeypatch.setattr("gacore.scheduler._deliver", lambda *a, **k: deliveries.append(a))
        c = _client(cfg, monkeypatch)
        r = c.post(
            "/api/revise",
            json={"date": DATE, "items": [{"anchor": "[个人观察-1]", "kind": "pref", "text": "个人观察节不要罗列数据", "note": "排版偏好"}]},
            headers=_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True and j["results"][0]["mode_used"] == "recorded"
        prefs = json.loads((cfg.root / "data" / "feedback" / "preferences.json").read_text(encoding="utf-8"))
        assert len(prefs) == 1 and prefs[0]["kind"] == "pref" and prefs[0]["note"] == "排版偏好"
        assert not (cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json").exists()
        assert deliveries == []  # 无正文变更 → 不重发

    def test_revise_llm_failure_still_records_and_reports_error(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        save_delivered(cfg, DATE, REPORT)

        class _Boom:
            def invoke(self, messages):
                raise RuntimeError("network down")

        monkeypatch.setattr("gacore.feedback.get_llm", lambda *a, **k: _Boom())
        c = _client(cfg, monkeypatch)
        r = c.post(
            "/api/revise",
            json={"date": DATE, "items": [{"anchor": "[工作日志-1]", "kind": "fact", "text": "x", "mode": "llm"}]},
            headers=_headers(),
        )
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is False
        assert "RuntimeError" in j["revise"]["error"]
        assert j["redeliver"]["status"] == "skipped"
        # LLM 失败不落 corrections：该文件语义是「已生效订正的事实底座」（③ 注入只认
        # active 记录），失败的尝试只体现在响应错误里，存档保持原样。
        assert not (cfg.root / "data" / "feedback" / "corrections" / f"{DATE}.json").exists()
        assert (load_delivered(cfg, DATE) or "").rstrip() == REPORT.rstrip()


# --------------------------------------------------------------------------- POST /api/rerun


class TestApiRerun:
    @pytest.fixture(autouse=True)
    def _reset_rerun_state(self):
        """_RERUN_STATE 是模块级共享 dict：每个用例前后清空，防跨用例泄漏。"""
        from gacore.review_server import _RERUN_LOCK, _RERUN_STATE

        with _RERUN_LOCK:
            _RERUN_STATE.clear()
        yield
        with _RERUN_LOCK:
            _RERUN_STATE.clear()

    @staticmethod
    def _patch_scheduler(monkeypatch, release: threading.Event | None = None, *, fail: bool = False):
        from gacore.scheduler import Job

        monkeypatch.setattr(
            "gacore.scheduler.load_jobs", lambda cfg: [Job(name="daily-report", schedule="", prompt="")]
        )
        calls: list[dict] = []

        def _fake_run_job(job, cfg_, graph_runner=None, for_day=None, deliver=True):
            calls.append({"name": job.name, "for_day": for_day, "deliver": deliver})
            if release is not None:
                release.wait(timeout=10)
            if fail:
                raise RuntimeError("boom")
            return SimpleNamespace(
                job_name=job.name, exit_reason="done", output_path="out.md", duration_seconds=0.1, error=None, reply=""
            )

        monkeypatch.setattr("gacore.scheduler.run_job", _fake_run_job)
        return calls

    def test_rerun_start_running_status_done(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        release = threading.Event()
        calls = self._patch_scheduler(monkeypatch, release)
        c = _client(cfg, monkeypatch)

        r = c.post("/api/rerun", json={"date": DATE, "email": True}, headers=_headers())
        assert r.status_code == 202
        assert r.json()["ok"] is True

        assert _wait_until(lambda: len(calls) == 1)
        assert calls[0] == {"name": "daily-report", "for_day": DATE, "deliver": True}

        st = c.get(f"/api/rerun/{DATE}/status").json()
        assert st["ok"] is True and st["state"] == "running"

        # 同日任务还在 running → 409
        r2 = c.post("/api/rerun", json={"date": DATE, "email": False}, headers=_headers())
        assert r2.status_code == 409
        assert r2.json()["ok"] is False

        release.set()
        assert _wait_until(lambda: c.get(f"/api/rerun/{DATE}/status").json()["state"] == "done")
        st = c.get(f"/api/rerun/{DATE}/status").json()
        assert st["exit_reason"] == "done" and st["output_path"] == "out.md"
        assert st["started_at"] and st["finished_at"]

    def test_rerun_error_state_recorded(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        release = threading.Event()
        self._patch_scheduler(monkeypatch, release, fail=True)
        c = _client(cfg, monkeypatch)
        assert c.post("/api/rerun", json={"date": DATE, "email": False}, headers=_headers()).status_code == 202
        release.set()
        assert _wait_until(lambda: c.get(f"/api/rerun/{DATE}/status").json()["state"] == "error")
        st = c.get(f"/api/rerun/{DATE}/status").json()
        assert "RuntimeError" in st["error"]

    def test_rerun_404_when_job_missing(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        monkeypatch.setattr("gacore.scheduler.load_jobs", lambda cfg: [])
        c = _client(cfg, monkeypatch)
        r = c.post("/api/rerun", json={"date": DATE, "email": True}, headers=_headers())
        assert r.status_code == 404
        assert r.json()["error"] == "job_not_found"

    def test_rerun_requires_token(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        self._patch_scheduler(monkeypatch)
        c = _client(cfg, monkeypatch)
        assert c.post("/api/rerun", json={"date": DATE, "email": True}).status_code == 401

    def test_rerun_status_idle_when_never_started(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch)
        j = c.get(f"/api/rerun/{DATE}/status").json()
        assert j == {"ok": True, "date": DATE, "state": "idle"}


# --------------------------------------------------------------------------- /health pages


def _write_health_jsonl(cfg: Config, lines: list[dict]) -> None:
    path = cfg.root / "data" / "logs" / "info_pack_health.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(ln, ensure_ascii=False) + "\n" for ln in lines), encoding="utf-8")


def _sample_line(date: str, *, status: str = "ok", chars: int = 120, note: str = "") -> dict:
    return {
        "ts": f"{date}T23:50:00+08:00",
        "date": date,
        "job": "daily-report",
        "trigger": "schedule",
        "total_chars": 900,
        "budget": 6000,
        "correction_chars": 0,
        "sources": [
            {"key": "_CHAT", "status": status, "chars": chars, "full_chars": chars + 80,
             "detail_chars": chars + 500, "note": note},
            {"key": "_BILI", "status": "failed", "chars": 12, "full_chars": 12, "detail_chars": 12,
             "note": "该源失败：CLI 未登录"},
        ],
    }


class TestHealthPages:
    def test_matrix_renders_cells_dates_notes(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _write_health_jsonl(cfg, [_sample_line("2026-10-02"), _sample_line("2026-10-03", note="truncated=3")])
        c = _client(cfg, monkeypatch)
        html_page = c.get("/health").text
        assert "2026-10-03" in html_page and "2026-10-02" in html_page
        assert "_CHAT" in html_page and "_BILI" in html_page
        assert "st-ok" in html_page and "st-failed" in html_page  # 状态色块
        assert "truncated=3" in html_page  # note 悬浮提示
        assert 'href="/health/source/2026-10-03/_CHAT"' in html_page

    def test_matrix_empty_state_when_missing(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch)
        page = c.get("/health").text
        assert "暂无数据" in page

    def test_source_page_renders_three_sections_and_bars(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        _write_health_jsonl(cfg, [_sample_line(DATE, chars=120)])
        md = (
            f"# pack detail {DATE} _CHAT\n\n"
            "## 完整取数详情\n\n- 摘录一\n- 摘录二\n\n"
            "## 渲染文本\n\n- top1 摘录\n\n"
            "## 实际进包\n\n〔CHAT｜状态:全量〕\n- top1 摘录\n"
        )
        detail_dir = cfg.root / "data" / "logs" / "pack_detail" / DATE
        detail_dir.mkdir(parents=True, exist_ok=True)
        (detail_dir / "_CHAT.md").write_text(md, encoding="utf-8")
        (cfg.logs_dir / DATE).mkdir(parents=True, exist_ok=True)
        (cfg.logs_dir / DATE / "llm_requests.jsonl").write_text('{"x": 1}\n', encoding="utf-8")

        c = _client(cfg, monkeypatch)
        page = c.get(f"/health/source/{DATE}/_CHAT").text
        assert "完整取数详情" in page and "渲染文本" in page and "实际进包" in page
        assert "摘录二" in page and "〔CHAT｜状态:全量〕" in page
        # 三级字符对比条：detail_chars=620 > full_chars=200 > chars=120
        assert ">620</span>" in page and ">200</span>" in page and ">120</span>" in page
        # L0 外链：scheduled 存档恒有；llm_requests.jsonl 存在才显示
        assert f'href="/review/{DATE}"' in page
        assert f'href="/logs/{DATE}/llm_requests.jsonl"' in page

    def test_source_page_no_numbers_when_jsonl_missing(self, tmp_path: Path, monkeypatch):
        """jsonl 无同日行 → 不渲染字符数字，页面仍可打开。"""
        cfg = Config.for_tests(tmp_path)
        detail_dir = cfg.root / "data" / "logs" / "pack_detail" / DATE
        detail_dir.mkdir(parents=True, exist_ok=True)
        (detail_dir / "_CHAT.md").write_text("## 实际进包\n\n- x\n", encoding="utf-8")
        c = _client(cfg, monkeypatch)
        page = c.get(f"/health/source/{DATE}/_CHAT").text
        assert "实际进包" in page
        assert "字符量对比" not in page  # 无同日 jsonl 行 → 整个对比条区域不渲染

    def test_source_page_fact_card_title_and_empty_state(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch)
        page = c.get(f"/health/source/{DATE}/_FACT_CARD").text
        assert "生活事实卡支线" in page
        assert "暂无数据" in page

    def test_source_page_rejects_bad_params(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch)
        assert c.get(f"/health/source/{DATE}/bad..key").status_code == 400
        assert c.get("/health/source/notadate/_CHAT").status_code == 400


# --------------------------------------------------------------------------- /logs static whitelist


class TestLogsRoute:
    def test_serves_scheduled_md(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        sched = cfg.logs_dir / "scheduled"
        sched.mkdir(parents=True, exist_ok=True)
        (sched / "daily-report_20261003_235000.md").write_text("# Scheduled Job\n", encoding="utf-8")
        c = _client(cfg, monkeypatch)
        r = c.get("/logs/scheduled/daily-report_20261003_235000.md")
        assert r.status_code == 200
        assert "Scheduled Job" in r.text

    def test_serves_llm_requests_jsonl(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        day_dir = cfg.logs_dir / DATE
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "llm_requests.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
        c = _client(cfg, monkeypatch)
        assert c.get(f"/logs/{DATE}/llm_requests.jsonl").status_code == 200

    def test_rejects_traversal_and_outside_whitelist(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        (cfg.logs_dir / "scheduled").mkdir(parents=True, exist_ok=True)
        (cfg.logs_dir / "scheduled" / "a.md").write_text("x", encoding="utf-8")
        c = _client(cfg, monkeypatch)
        assert c.get("/logs/scheduled/../../../.env").status_code in (403, 404)
        assert c.get("/logs/scheduled/secret.txt").status_code == 403
        assert c.get(f"/logs/{DATE}/other.jsonl").status_code == 403
        assert c.get("/logs/scheduled/missing.md").status_code == 404


# --------------------------------------------------------------------------- GET /review/{date}


class TestReviewPage:
    def test_bare_review_redirects_to_latest_delivered(self, tmp_path: Path, monkeypatch):
        """/review 无日期 → 307 跳最近一篇 delivered（dev-console「打开」落地页）。"""
        cfg = Config.for_tests(tmp_path)
        save_delivered(cfg, "2026-10-02", REPORT)
        save_delivered(cfg, DATE, REPORT)
        c = _client(cfg, monkeypatch)
        res = c.get("/review", follow_redirects=False)
        assert res.status_code == 307
        assert res.headers["location"] == f"/review/{DATE}"  # 最新一篇，而非更早的

    def test_bare_review_redirect_falls_back_to_today(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        c = _client(cfg, monkeypatch)
        res = c.get("/review", follow_redirects=False)
        assert res.status_code == 307
        assert re.fullmatch(r"/review/\d{4}-\d{2}-\d{2}", res.headers["location"])

    def test_renders_delivered_report_with_anchor_badges(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        save_delivered(cfg, DATE, REPORT)
        c = _client(cfg, monkeypatch)
        page = c.get(f"/review/{DATE}").text
        assert "今日状态" in page and "上午收 9-08 日报尾巴" in page
        assert 'data-anchor="[工作日志-2]"' in page  # 可点角标
        assert 'data-anchor="[个人观察-1]"' in page
        assert "提交修订" in page and "整体重生成" in page  # 两个动作按钮

    def test_empty_state_without_archive(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch)
        page = c.get(f"/review/{DATE}").text
        assert "暂无数据" in page

    def test_fallback_to_latest_scheduled_archive_containing_date(self, tmp_path: Path, monkeypatch):
        cfg = Config.for_tests(tmp_path)
        sched = cfg.logs_dir / "scheduled"
        sched.mkdir(parents=True, exist_ok=True)
        # 不含该日期的更新存档必须被跳过
        (sched / "daily-report_20261004_235000.md").write_text(
            "# Scheduled Job: daily-report\n- time: 2026-10-04T23:50:00+08:00\n\n## Reply\n\n别的日子\n",
            encoding="utf-8",
        )
        (sched / "daily-report_20261003_235000.md").write_text(
            "# Scheduled Job: daily-report\n- time: 2026-10-03T23:50:00+08:00\n\n"
            "## User Prompt (assembled, incl. info pack)\n\npack\n\n## Reply\n\n" + REPORT + "\n",
            encoding="utf-8",
        )
        c = _client(cfg, monkeypatch)
        page = c.get(f"/review/{DATE}").text
        assert "上午收 9-08 日报尾巴" in page  # Reply 节内容
        assert "scheduled/daily-report_20261003_235000.md" in page  # 来源标注
        assert 'data-anchor="[工作日志-1]"' in page
        assert "User Prompt" not in page  # 只渲染 Reply 节，不渲染提示词
        assert "别的日子" not in page  # 不含该日期的存档被跳过

    def test_rejects_invalid_date(self, tmp_path: Path, monkeypatch):
        c = _client(Config.for_tests(tmp_path), monkeypatch)
        assert c.get("/review/not-a-date").status_code == 400
