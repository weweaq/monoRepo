"""提问回路 qa 落库层（qa_store）的封闭测试：临时 data/qa，不碰真实数据。"""

from __future__ import annotations

from gacore.config import Config
from gacore.qa_store import (
    archive_stale,
    mark_pushed,
    open_questions,
    record_answer,
    seed_questions,
    stats,
    try_answer,
)


def _seed_day(cfg, day: str, bullets: list[str]) -> dict:
    return seed_questions(cfg, day, bullets)


def test_seed_and_open_questions_order(tmp_path):
    cfg = Config.for_tests(tmp_path)
    out = _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] 你今天 9 次登录学校系统，是在读还是上班？（依据：10-25 浏览）",
            "- [想问你-2] 工学椅一条说已放弃、一条说待办，哪个是真的？（依据：长期画像）",
        ],
    )
    assert out["seeded"] == 2
    open_ = open_questions(cfg)
    assert len(open_) == 2
    assert open_[0]["anchor"] == "想问你-1"
    assert open_[0]["day"] == "2026-10-25"
    assert open_[0]["status"] == "open"
    assert open_[0]["asks"] == 0


def test_answered_key_never_reopens(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25 浏览）"])
    key = open_questions(cfg)[0]["question_key"]
    assert record_answer(cfg, key, "在读") is not None
    out = _seed_day(cfg, "2026-10-26", ["- [想问你-1] 你是在读还是上班？（依据：10-25 浏览）"])
    assert out["seeded"] == 0
    assert out["dup_answered"] == 1
    assert open_questions(cfg) == []


def test_same_day_similar_questions_deduped(tmp_path):
    cfg = Config.for_tests(tmp_path)
    out = _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] 你今天九次登录学校系统，是在读还是上班？（依据：10-25）",
            "- [想问你-2] 今天 9 次登录学校系统，你是在读还是在上班？（依据：10-25）",
        ],
    )
    assert out["seeded"] == 1
    assert out["dup_similar"] == 1


def test_record_answer_updates_row_and_candidate(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 工学椅到底还买不买？（依据：长期画像）"])
    key = open_questions(cfg)[0]["question_key"]
    row = record_answer(cfg, key, "先不买了")
    assert row["status"] == "answered"
    assert row["answer"] == "先不买了"
    assert open_questions(cfg) == []
    cands = (cfg.root / "data" / "qa" / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(cands) == 1
    assert '"pending"' in cands[0]


def test_archive_stale_after_two_reasks(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    key = open_questions(cfg)[0]["question_key"]
    mark_pushed(cfg, [key])
    mark_pushed(cfg, [key])
    mark_pushed(cfg, [key])  # 第 3 次推送后仍未答
    assert archive_stale(cfg) == 1
    assert open_questions(cfg) == []


def test_try_answer_explicit_index(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] 你是在读还是上班？（依据：10-25）",
            "- [想问你-2] 工学椅还买吗？（依据：长期画像）",
        ],
    )
    reply = try_answer(cfg, "[Q2] 先不买了")
    assert reply is not None and "2" in reply
    assert len(open_questions(cfg)) == 1
    assert open_questions(cfg)[0]["anchor"] == "想问你-1"


def test_try_answer_bare_only_with_single_pushed_open(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    key = open_questions(cfg)[0]["question_key"]
    # 未推送过：裸回答不吞（用户还没看到问题）
    assert try_answer(cfg, "在读") is None
    mark_pushed(cfg, [key])
    reply = try_answer(cfg, "在读")
    assert reply is not None
    assert open_questions(cfg) == []


def test_try_answer_bare_rejected_with_multiple_open(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] 你是在读还是上班？（依据：10-25）",
            "- [想问你-2] 工学椅还买吗？（依据：长期画像）",
        ],
    )
    keys = [q["question_key"] for q in open_questions(cfg)]
    mark_pushed(cfg, keys)
    assert try_answer(cfg, "在读", allow_bare=True) is None


def test_try_answer_respects_allow_bare_flag(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    mark_pushed(cfg, [open_questions(cfg)[0]["question_key"]])
    assert try_answer(cfg, "在读", allow_bare=False) is None


def test_stats_reports_answer_rate(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] 你是在读还是上班？（依据：10-25）",
            "- [想问你-2] 工学椅还买吗？（依据：长期画像）",
        ],
    )
    key = open_questions(cfg)[0]["question_key"]
    record_answer(cfg, key, "在读")
    s = stats(cfg)
    assert s["total"] == 2
    assert s["answered"] == 1
    assert s["answer_rate"] == 0.5


# ----------------------------------------------------- P3 Task 5: _QA 信息源


def test_qa_source_empty_is_missing_data(tmp_path):
    import gacore.daily_info_pack as dip

    cfg = Config.for_tests(tmp_path)
    title, pack, detail = dip._build_qa("2026-10-26", cfg)
    assert "〔QA·想问你的回答〕" in title
    assert pack.startswith("- 当日无问答数据")
    assert detail == ""
    status, _ = dip.classify_body(pack)
    assert status == "missing_data"


def test_qa_source_renders_answered_and_open(tmp_path, monkeypatch):
    import gacore.daily_info_pack as dip

    cfg = Config.for_tests(tmp_path)
    _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] 你是在读还是上班？（依据：10-25）",
            "- [想问你-2] 工学椅还买吗？（依据：长期画像）",
        ],
    )
    record_answer(cfg, open_questions(cfg)[0]["question_key"], "在读")
    # 回答日期锚定到今天（record_answer 写 answer_day=今天），before_date 用今天保证入窗
    from datetime import datetime, timedelta, timezone

    today = datetime.now(tz=timezone(timedelta(hours=8))).date().isoformat()
    title, pack, detail = dip._build_qa(today, cfg)
    assert "已答" in pack and "在读" in pack
    assert "仍未回答（勿重复问）" in pack
    assert dip.classify_body(pack)[0] == "ok"
    assert "archived" not in detail  # 无归档行
    assert "→ 在读" in detail  # detail 全量含已答行


def test_qa_source_truncates_to_cap(tmp_path, monkeypatch):
    import gacore.daily_info_pack as dip

    monkeypatch.setattr(dip, "_QA_CAP", 120)
    cfg = Config.for_tests(tmp_path)
    _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] " + "很" * 200 + "累吗？（依据：10-25）",
            "- [想问你-2] 今天午饭吃了什么？（依据：10-25）",
        ],
    )
    # 两条语义不同都能种入（近似问句会被同期语义去重砍掉，另行覆盖在 seed 测试）
    assert len(open_questions(cfg)) == 2
    record_answer(cfg, open_questions(cfg)[0]["question_key"], "还好")
    title, pack, detail = dip._build_qa("2026-10-26", cfg)
    assert len(pack) <= 120
    assert dip.classify_body(pack)[0] == "ok"
