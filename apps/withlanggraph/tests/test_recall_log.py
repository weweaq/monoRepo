"""Tests for gacore.recall_log (structured recall/rewrite log schema)."""

import json
from datetime import datetime

from gacore.recall_log import RecallLog, build_record, iter_records, record_line, summarize


def _rec(**kw):
    kw.setdefault("session", "s")
    kw.setdefault("ts", datetime(2026, 9, 10, 12, 0, 0))
    kw.setdefault("semantic", [{"content": "领导叫大帅", "dist": 0.34}])
    kw.setdefault("episodic", [{"content": "介绍过一位领导大帅", "day": "2026-09-08", "dist": 0.46}])
    return build_record("他叫大帅我的领导", **kw)


def test_build_record_fields_and_sim():
    rec = _rec()
    assert rec["day"] == "2026-09-10"
    assert rec["input_query"] == "他叫大帅我的领导"
    assert rec["query_used"] == rec["input_query"]  # raw by default
    assert rec["injected"] is True
    assert rec["semantic"][0]["sim"] == round(1 - 0.34, 3) == 0.66
    assert rec["episodic"][0]["day"] == "2026-09-08"
    assert rec["sim_max"] == 0.66


def test_build_record_empty_not_injected():
    rec = build_record("", variant=None)
    assert rec["injected"] is False
    assert rec["n_sem"] == 0 and rec["n_epi"] == 0
    assert rec["sim_min"] is None and rec["sim_max"] is None


def test_build_record_rewritten_variant():
    rec = _rec(query_used="我们单位领导大帅是谁", rewrite={"query": "我们单位领导大帅是谁", "skipped_reason": None}, variant="rewritten")
    assert rec["query_used"] == "我们单位领导大帅是谁"
    assert rec["variant"] == "rewritten"
    assert rec["rewrite"]["query"] == "我们单位领导大帅是谁"


def test_record_line_roundtrip():
    rec = _rec()
    loaded = json.loads(record_line(rec))
    assert loaded["input_query"] == rec["input_query"]
    assert loaded["semantic"][0]["sim"] == 0.66


def test_recall_log_append_and_iter(tmp_path):
    log = RecallLog(tmp_path)
    log.append(_rec())
    log.append(_rec(ts=datetime(2026, 9, 10, 13, 0, 0)))
    files = list(tmp_path.glob("2026-09-10/recall.jsonl"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").count("\n") == 2
    back = iter_records(tmp_path)
    assert len(back) == 2
    assert back[0]["input_query"] == "他叫大帅我的领导"


def test_summarize_empty_and_populated():
    assert summarize([])["total"] == 0
    stats = summarize([_rec(), _rec(variant="rewritten"), build_record("普通", variant=None)])
    assert stats["eligible_queries"] == 3
    assert stats["injected"] == 2
    assert stats["empty"] == 1
    assert stats["inject_rate"] == round(2 / 3, 3)
    assert stats["sim_max"] == 0.66