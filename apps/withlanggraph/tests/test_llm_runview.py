"""Unit tests for gacore.llm_runview (run reconstruction over the three log sources).

Fixtures hand-write the same artifacts the real writers produce:
- logs/{date}/llm_requests.jsonl  (llm_request_log, one line = request+response+duration)
- logs/{date}/app.jsonl           (jsonl_logger: deliver_email sent / ERROR events)
- logs/scheduled/{job}_{ts}.md    (scheduler _write_output archive with Reply section)
All logic under test is the reconstruction heuristics documented in the module docstring;
regression anchor = the 2026-10-03 real-data walk-through in the llm-view demos."""

from __future__ import annotations

import json
from pathlib import Path

from gacore.config import Config
from gacore.llm_runview import (
    build_runs,
    classify_run,
    run_view,
)


def _req(ts: str, session: str, pid: int, *, msgs: list[dict] | None = None,
         tools: int = 27, resp: dict | None = None, duration: int = 700) -> dict:
    return {
        "ts": ts, "session": session, "pid": pid, "provider": "fake", "model": "m-1",
        "run_kind": "invoke", "messages": msgs if msgs is not None else [], "tools": [{"name": f"t{i}"} for i in range(tools)],
        "params": {"temperature": 0}, "duration_ms": duration, "response": resp,
    }


def _sys(ts: str, session: str, pid: int, msg: str, **extra: object) -> str:
    return json.dumps({"ts": ts, "level": "INFO", "module": "scheduler", "message": msg,
                       "session": session, "pid": pid, **extra}, ensure_ascii=False)


def _write_logs(tmp_path: Path, date: str, requests: list[dict], app_lines: list[str]) -> None:
    d = tmp_path / "logs" / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "llm_requests.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in requests), encoding="utf-8")
    (d / "app.jsonl").write_text("\n".join(app_lines), encoding="utf-8")


def test_build_runs_groups_by_session_and_gap() -> None:
    records = [
        _req("2026-10-03T23:50:46", "s1", 100, msgs=[
            {"role": "system", "content": "基本要求"},
            {"role": "human", "content": "〔当日信息包·2026-10-03〕 写日报"},
        ]),
        _req("2026-10-03T23:54:22", "s1", 100),
        _req("2026-10-03T23:56:00", "s1", 100),  # 98s after previous — same run
        _req("2026-10-03T23:57:00", "s2", 100),  # different session — new run
    ]
    runs = build_runs(records)
    assert [len(r["calls"]) for r in runs] == [3, 1]


def test_tool_round_pairing_by_tool_call_id() -> None:
    records = [
        _req("2026-10-03T10:00:00", "s1", 1, msgs=[
            {"role": "system", "content": "s"},
            {"role": "human", "content": "hi"},
        ]),
        _req("2026-10-03T10:00:10", "s1", 1, msgs=[
            {"role": "system", "content": "s"},
            {"role": "human", "content": "hi"},
            {"role": "ai", "content": "", "tool_calls": [{"id": "c1", "name": "get_time", "args": {}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "10:00"},
        ], resp={"content": "done", "content_chars": 4}, duration=1200),
    ]
    runs = build_runs(records)
    c2 = runs[0]["calls"][1]
    assert c2["turnToolCalls"] == [
        {"id": "c1", "name": "get_time", "args": {}, "result": "10:00", "resultChars": 5}
    ]
    assert c2["resp"]["chars"] == 4 and c2["durationMs"] == 1200


def test_classify_daily_proactive_chat_internal() -> None:
    daily = [{"messages": [
        {"role": "system", "content": "基本要求"},
        {"role": "human", "content": "〔当日信息包·2026-10-03〕以下内容由系统预取"},
    ]}]
    assert classify_run(daily)[0] == "daily"
    assert "2026-10-03" in classify_run(daily)[1]

    proactive = [{"messages": [
        {"role": "system", "content": "行动原则"},
        {"role": "human", "content": "目标：主动给主人发一条 QQ 私聊消息（qq_push）"},
    ]}]
    assert classify_run(proactive)[0] == "proactive"

    chat = [{"messages": [
        {"role": "system", "content": "你是性格鲜明的聊天搭子，口语化即兴回应"},
        {"role": "human", "content": "在吗"},
    ]}]
    assert classify_run(chat) == ("chat", "QQ 快答", "在吗")

    internal = [{"messages": []}]
    assert classify_run(internal)[0] == "internal"


def test_delivery_join_session_exact_and_pid_fallback(tmp_path: Path) -> None:
    cfg = Config.for_tests(tmp_path)
    # session-exact: post-unification logs share one id across both sinks
    records = [_req("2026-10-03T23:54:22", "same", 7)]
    app_lines = [_sys("2026-10-03T23:57:00", "same", 7, "deliver_email sent",
                      subject="[gacore] daily-report · 2026-10-03")]
    _write_logs(tmp_path, "2026-10-03", records, app_lines)
    view = run_view(cfg, "2026-10-03")
    assert view["runs"][0]["systemEvents"][0]["subject"].endswith("2026-10-03")

    # pid fallback: legacy logs where the two sinks generated different ids
    records = [_req("2026-10-03T23:54:22", "aaa", 9)]
    app_lines = [_sys("2026-10-03T23:57:00", "bbb", 9, "deliver_email sent",
                      subject="[gacore] daily-report · 2026-10-03")]
    _write_logs(tmp_path, "2026-10-03", records, app_lines)
    view = run_view(cfg, "2026-10-03")
    assert len(view["runs"][0]["systemEvents"]) == 1


def test_redeliver_batch_detected_not_own_delivery(tmp_path: Path) -> None:
    cfg = Config.for_tests(tmp_path)
    records = [_req("2026-10-03T15:38:25", "x", 5)]
    # three distinct subjects within 1s = QQ-feedback redeliver batch, not this run's mail
    app_lines = [
        _sys("2026-10-03T15:40:00", "x", 5, "deliver_email sent", subject="[gacore] daily-report · 2026-10-03"),
        _sys("2026-10-03T15:40:00", "x", 5, "deliver_email sent", subject="[gacore][FAILED] daily-report · 2026-10-03"),
        _sys("2026-10-03T15:40:00", "x", 5, "deliver_email sent", subject="[gacore] daily-report · 2026-09-08（补跑）"),
    ]
    _write_logs(tmp_path, "2026-10-03", records, app_lines)
    view = run_view(cfg, "2026-10-03")
    run = view["runs"][0]
    assert run["systemEvents"] == []
    assert run["systemBatch"]["count"] == 3


def test_errors_attached_within_window(tmp_path: Path) -> None:
    cfg = Config.for_tests(tmp_path)
    records = [
        _req("2026-10-03T00:01:11", "s", 2),
        _req("2026-10-03T00:04:20", "s", 2),
    ]
    err = json.dumps({"ts": "2026-10-03T00:05:24.262+08:00", "level": "ERROR", "module": "memory_maintain",
                      "message": "memory_maintain gate failed", "session": "s", "pid": 2,
                      "error": "TypeError: unsupported operand"}, ensure_ascii=False)
    _write_logs(tmp_path, "2026-10-03", records, [err])
    view = run_view(cfg, "2026-10-03")
    errs = view["runs"][0]["errors"]
    assert len(errs) == 1 and errs[0]["module"] == "memory_maintain"


def test_output_archive_attached_by_finish_ts(tmp_path: Path) -> None:
    cfg = Config.for_tests(tmp_path)
    records = [_req("2026-10-03T00:01:11", "s", 2), _req("2026-10-03T00:04:20", "s", 2)]
    _write_logs(tmp_path, "2026-10-03", records, [])
    archive = tmp_path / "logs" / "scheduled"
    archive.mkdir(parents=True)
    (archive / "daily-report_20261003_000524.md").write_text(
        "# Scheduled Job: daily-report\n## Reply\n\n# 今日状态\n- ok\n", encoding="utf-8")
    view = run_view(cfg, "2026-10-03")
    out = view["runs"][0].get("output")
    assert out and out["replyChars"] > 0 and "今日状态" in out["reply"]


def test_missing_files_degrade_to_empty(tmp_path: Path) -> None:
    view = run_view(Config.for_tests(tmp_path), "2026-10-03")
    assert view == {"date": "2026-10-03", "runs": [],
                    "counts": {"runs": 0, "calls": 0, "toolRounds": 0}}
