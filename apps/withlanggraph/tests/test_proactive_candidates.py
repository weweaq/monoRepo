"""P2 候选池封闭测试：四类来源采集 / 打分 / 阈值 / 去重 / 接线。

不触真实库、不触网络：langTrack.db 与 qa 文件都落在 pytest tmp_path。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from gacore import proactive, proactive_candidates as pc
from gacore.config import Config
from gacore.langTrack.extract.schema import EXTRACT_SCHEMA
from gacore.qa_store import all_rows, open_questions, qa_brief, seed_candidates
from gacore.scheduler import Job

_TZ = timezone(timedelta(hours=8))


def _dt(y: int, mo: int, d: int, h: int = 8, mi: int = 30) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_TZ)


def _kg_db(cfg: Config) -> sqlite3.Connection:
    db = cfg.root / "data" / "langTrack.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(EXTRACT_SCHEMA)
    return conn


def _daily_db(cfg: Config) -> sqlite3.Connection:
    db = cfg.root / "data" / "langTrack.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE daily_stats (day TEXT, total_screen_ms INTEGER, sleep_start_hhmm TEXT)"
    )
    return conn


# --------------------------------------------------------------------------- scoring


def _ctx(now: datetime, **kwargs) -> pc.ScoreContext:
    defaults = {
        "now": now,
        "reply_by_source": {},
        "hot_chat": False,
        "silent": False,
        "recent_topics": set(),
        "recent_questions": (),
    }
    defaults.update(kwargs)
    return pc.ScoreContext(**defaults)


def test_score_full_marks_and_boundaries():
    now = _dt(2026, 10, 26)
    cand = pc.Candidate(
        source=pc.SOURCE_KG_TODO,
        ref="kg_edges:e1",
        topic="待办",
        anchor="2026-10-25 未完成待办：下周把相机买了",
        question="「下周把相机买了」这件事后来做了吗？",
        freshness=now,
        gain=0.85,
    )
    parts = pc.score_candidate(cand, _ctx(now))
    assert parts["freshness"] == 1.0
    assert parts["specificity"] == 1.0
    assert parts["answer_likelihood"] == 0.5  # 冷启动
    assert parts["novelty"] == 1.0
    assert parts["intrusion"] == 0.0
    assert parts["score"] == 0.88  # 0.30*.85 + 0.25 + 0.20 + 0.15*.5 + 0.10

    stale = pc.Candidate(
        source=pc.SOURCE_KG_TODO,
        ref="kg_edges:e2",
        topic="待办",
        anchor="最近怎么样",
        question="最近怎么样？",
        freshness=now - timedelta(days=8),
        gain=0.0,
    )
    parts = pc.score_candidate(stale, _ctx(now))
    assert parts["freshness"] == 0.0
    assert parts["specificity"] == 0.0
    assert parts["score"] == 0.175  # 只有 answer_likelihood + novelty


def test_score_intrusion_and_novelty():
    now = _dt(2026, 10, 26)
    cand = pc.Candidate(
        source=pc.SOURCE_LANGTRACK,
        ref="daily_stats:sleep",
        topic="作息",
        anchor="2026-10-23~2026-10-25 连续三天入睡晚于 02:00",
        question="最近三天都两点后才睡，是事情多还是作息乱了？",
        freshness=now,
        gain=0.75,
    )
    normal = pc.score_candidate(cand, _ctx(now))
    silent = pc.score_candidate(cand, _ctx(now, silent=True))
    repeated = pc.score_candidate(cand, _ctx(now, recent_topics={"作息"}))
    assert silent["score"] < normal["score"]
    assert repeated["novelty"] == 0.0 and repeated["score"] < normal["score"]


def test_select_drops_below_threshold(tmp_path, monkeypatch):
    now = _dt(2026, 10, 26)
    low = pc.Candidate(
        source=pc.SOURCE_CHAT_LOOP,
        ref="t1:msg1",
        topic="闲聊",
        anchor="最近怎么样",
        question="最近怎么样？",
        freshness=now - timedelta(days=8),
        gain=0.0,
    )
    monkeypatch.setattr(pc, "collect_all", lambda *a, **k: [low])
    assert pc.select(Config.for_tests(tmp_path), now) == []


def test_select_prefers_one_source_group(tmp_path, monkeypatch):
    now = _dt(2026, 10, 26)
    high = pc.Candidate(
        source=pc.SOURCE_KG_TODO,
        ref="kg_edges:e1",
        topic="待办",
        anchor="2026-10-25 未完成待办：买相机",
        question="「买相机」这件事后来做了吗？",
        freshness=now,
        gain=0.85,
    )
    other = pc.Candidate(
        source=pc.SOURCE_MEMORY_CONFLICT,
        ref="qa_candidate:k1",
        topic="画像",
        anchor="2026-10-25 画像候选：在准备考研",
        question="上次你说「在准备考研」，现在还成立吗？",
        freshness=now,
        gain=0.60,
    )
    monkeypatch.setattr(pc, "collect_all", lambda *a, **k: [high, other])
    picked = pc.select(Config.for_tests(tmp_path), now)
    assert [c.source for c in picked] == [pc.SOURCE_KG_TODO]


# --------------------------------------------------------------------------- collectors


def test_collect_kg_todo_open_edges_only(tmp_path):
    cfg = Config.for_tests(tmp_path)
    conn = _kg_db(cfg)
    with conn:
        conn.execute(
            "INSERT INTO kg_nodes (node_id, node_type, label, first_day, last_day, src, run_id)"
            " VALUES ('person_zhangsan','Person','张三','2026-10-25','2026-10-25','todos','r1')"
        )
        conn.execute(
            "INSERT INTO kg_edges (edge_id, from_node, to_node, relation, edge_level,"
            " assertion_text, valid_from, valid_to, invalidated, day, src, run_id)"
            " VALUES ('e1','person_zhangsan','event_1','has_todo','L1','下周把相机买了',"
            " '2026-10-25',NULL,0,'2026-10-25','todos','r1')"
        )
        # 已闭合的待办不应入池
        conn.execute(
            "INSERT INTO kg_edges (edge_id, from_node, to_node, relation, edge_level,"
            " assertion_text, valid_from, valid_to, invalidated, day, src, run_id)"
            " VALUES ('e2','person_zhangsan','event_2','promised','L1','答应帮看代码',"
            " '2026-10-20','2026-10-21',0,'2026-10-20','todos','r1')"
        )
    conn.close()

    cands = pc.collect_kg_todo(cfg, _dt(2026, 10, 26))
    assert len(cands) == 1
    cand = cands[0]
    assert cand.source == pc.SOURCE_KG_TODO
    assert cand.ref == "kg_edges:e1"
    assert "相机" in cand.question
    assert "2026-10-25" in cand.anchor
    assert "2026-10-25" in cand.evidence


def test_collect_langtrack_late_sleep(tmp_path):
    cfg = Config.for_tests(tmp_path)
    conn = _daily_db(cfg)
    with conn:
        for day, sleep in (
            ("2026-10-23", "02:20"),
            ("2026-10-24", "03:10"),
            ("2026-10-25", "02:05"),
        ):
            conn.execute(
                "INSERT INTO daily_stats VALUES (?, ?, ?)", (day, 3_600_000, sleep)
            )
    conn.close()

    cands = pc.collect_langtrack(cfg, _dt(2026, 10, 26))
    assert len(cands) == 1
    assert cands[0].topic == "作息"
    assert "02:20" in cands[0].anchor
    assert "2026-10-25" in cands[0].question


def test_collect_langtrack_screen_surge(tmp_path):
    cfg = Config.for_tests(tmp_path)
    conn = _daily_db(cfg)
    with conn:
        # 近 3 天 6h/天，此前 7 天 2h/天 → +200%
        for i, ms in enumerate([21_600_000, 21_600_000, 21_600_000] + [7_200_000] * 7):
            day = (datetime(2026, 10, 25) - timedelta(days=i)).date().isoformat()
            conn.execute("INSERT INTO daily_stats VALUES (?, ?, NULL)", (day, ms))
    conn.close()

    cands = pc.collect_langtrack(cfg, _dt(2026, 10, 26))
    assert len(cands) == 1
    assert cands[0].topic == "屏幕"
    assert "多了 200%" in cands[0].question


def test_collect_langtrack_ignores_single_day_noise(tmp_path):
    cfg = Config.for_tests(tmp_path)
    conn = _daily_db(cfg)
    with conn:
        conn.execute("INSERT INTO daily_stats VALUES ('2026-10-25', 3600000, '02:30')")
    conn.close()
    assert pc.collect_langtrack(cfg, _dt(2026, 10, 26)) == []


def test_collect_memory_conflict_pending_portrait(tmp_path):
    cfg = Config.for_tests(tmp_path)
    path = cfg.root / "data" / "qa" / "candidates.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "question_key": "k1",
                "question": "你是在读还是上班？",
                "answer": "在准备考研",
                "status": "pending",
                "answered_at": "2026-10-25 09:00:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    cands = pc.collect_memory_conflict(cfg, _dt(2026, 10, 26))
    assert len(cands) == 1
    assert cands[0].source == pc.SOURCE_MEMORY_CONFLICT
    assert "考研" in cands[0].question
    assert "2026-10-25" in cands[0].anchor


def test_collect_chat_loop_commitment_phrase(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    messages = [
        {"type": "human", "content": "帮我看看这个方案"},
        {"type": "ai", "content": "好，我先看一版"},
        {"type": "human", "content": "这个先等忙完再弄吧"},
    ]
    monkeypatch.setattr(proactive, "_thread_id_of", lambda cfg_, uid: "t1")
    monkeypatch.setattr(proactive, "_read_thread_messages", lambda cfg_, tid: messages)

    cands = pc.collect_chat_loop(cfg, _dt(2026, 10, 26), "u1")
    assert len(cands) == 1
    assert cands[0].source == pc.SOURCE_CHAT_LOOP
    assert cands[0].ref == "t1:msg2"
    assert "等忙完再弄" in cands[0].question

    # 最后一条不是承诺句 → 不入池
    monkeypatch.setattr(
        proactive, "_read_thread_messages", lambda cfg_, tid: [{"type": "human", "content": "今天好累"}]
    )
    assert pc.collect_chat_loop(cfg, _dt(2026, 10, 26), "u1") == []


# --------------------------------------------------------------------------- seeding / dedupe


def _row(**kwargs) -> dict:
    base = {
        "source": pc.SOURCE_KG_TODO,
        "ref": "kg_edges:e1",
        "topic": "待办",
        "anchor": "2026-10-25 未完成待办：买相机",
        "question": "「买相机」这件事后来做了吗？",
        "evidence": ["2026-10-25"],
        "freshness": "2026-10-25T08:00:00+08:00",
        "gain": 0.85,
    }
    base.update(kwargs)
    return base


def test_seed_candidates_marks_kind_and_source(tmp_path):
    cfg = Config.for_tests(tmp_path)
    out = seed_candidates(cfg, [_row()], day="2026-10-26")
    assert out["seeded"] == 1
    row = open_questions(cfg)[0]
    assert row["kind"] == "candidate"
    assert row["source"] == pc.SOURCE_KG_TODO
    assert row["topic"] == "待办"
    assert row["anchor"].startswith("候选-2026-10-26-")
    assert row["asks"] == 0


def test_seed_candidates_topic_cooldown_and_answered_dedupe(tmp_path):
    cfg = Config.for_tests(tmp_path)
    seed_candidates(cfg, [_row()], day="2026-10-26")
    # 同 topic、不同措辞：7 天冷却命中
    out = seed_candidates(
        cfg,
        [_row(question="「买相机」这事后来办了吗？", ref="kg_edges:e2")],
        day="2026-10-26",
    )
    assert out["seeded"] == 0 and out["dup_topic"] == 1

    # 已答 key 永不再入池
    key = open_questions(cfg)[0]["question_key"]
    from gacore.qa_store import record_answer

    record_answer(cfg, key, "买好了")
    out = seed_candidates(cfg, [_row()], day="2026-10-27")
    assert out["seeded"] == 0 and out["dup_answered"] == 1


def test_qa_brief_filters_candidate_rows(tmp_path):
    cfg = Config.for_tests(tmp_path)
    seed_candidates(cfg, [_row()], day="2026-10-26")
    answered, open_ = qa_brief(cfg, before_date="2026-10-27")
    assert answered == [] and open_ == []


def test_verify_evidence_flags_fabricated_number():
    rows = [{"anchor": "2026-10-23~2026-10-25 连续三天入睡晚于 02:00", "question": "最近三天都两点后才睡，是事情多还是作息乱了？", "evidence": ["2026-10-25"]}]
    assert pc.verify_evidence("最近三天都两点后才睡（2026-10-25），怎么了？", rows) == []
    assert pc.verify_evidence("你昨晚 03:47 才睡吧？", rows) == ["03:47"]


# --------------------------------------------------------------------------- integration


def test_questions_scene_falls_back_to_candidate_pool(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    monkeypatch.setattr("gacore.proactive._target_users", lambda job: ["u1"])
    cand = pc.Candidate(
        source=pc.SOURCE_KG_TODO,
        ref="kg_edges:e1",
        topic="待办",
        anchor="2026-10-25 未完成待办：买相机",
        question="「买相机」这件事后来做了吗？",
        freshness=_dt(2026, 10, 26),
        gain=0.85,
    )
    monkeypatch.setattr("gacore.proactive_candidates.select", lambda *a, **k: [cand])
    captured: dict = {}

    def fake_headless(job, cfg_, prompt, max_turns, active_card=None):
        captured["prompt"] = prompt
        return "END", "转述完成", [json.dumps({"status": "sent", "to": ["u1"]})]

    monkeypatch.setattr("gacore.proactive._headless_run", fake_headless)
    monkeypatch.setattr("gacore.proactive._write_back_proactive", lambda *a, **k: False)
    job = Job(name="daily-questions", schedule="08:30", prompt="", scene="questions")
    result = proactive.run_proactive_job(job, cfg, clock=lambda: _dt(2026, 10, 26))
    assert result["sent"] == 1
    assert "[Q1] 「买相机」这件事后来做了吗？" in captured["prompt"]
    rows = all_rows(cfg)
    assert len(rows) == 1 and rows[0]["kind"] == "candidate" and rows[0]["asks"] == 1


def test_candidate_pool_can_be_disabled_by_config(tmp_path, monkeypatch):
    cfg = Config.for_tests(tmp_path)
    path = cfg.root / "config" / "proactive.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"candidates": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setattr("gacore.proactive._target_users", lambda job: ["u1"])

    def _boom(*args, **kwargs):
        raise AssertionError("candidate pool must not run when disabled")

    monkeypatch.setattr("gacore.proactive_candidates.select", _boom)
    job = Job(name="daily-questions", schedule="08:30", prompt="", scene="questions")
    result = proactive.run_proactive_job(job, cfg, clock=lambda: _dt(2026, 10, 26))
    assert result["skipped"] == [{"user": "", "reason": "no_open_questions"}]
