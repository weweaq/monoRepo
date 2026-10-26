"""提问回路 qa 落库层（qa_store）的封闭测试：临时 data/qa，不碰真实数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gacore.config import Config
from gacore.qa_store import (
    _day_as_date,
    all_rows,
    archive_stale,
    mark_ignored,
    mark_pushed,
    match_and_record,
    match_answer,
    open_questions,
    qa_brief,
    record_answer,
    reply_stats,
    seed_questions,
    set_prompt_order,
    stats,
    try_answer,
    unanswer,
)

_TZ = timezone(timedelta(hours=8))


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_TZ)


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
        "2026-10-24",
        [
            "- [想问你-1] 你是在读还是上班？（依据：10-24）",
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


# ------------------------------------------ P3 修复：编号解析、as-of 栅栏、周 id 日锚


def test_explicit_q_index_follows_latest_day_anchor(tmp_path):
    """[Q1] 有旧问题未答时仍指向最近一期正文的 Q1（决策 29）。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-19", ["- [想问你-1] 旧问题甲（依据：10-19）"])
    _seed_day(cfg, "2026-10-20", ["- [想问你-1] 今天的问题丙（依据：10-20）"])
    assert try_answer(cfg, "[Q1] 我的答案") is not None
    rows = {r["question"]: r for r in all_rows(cfg)}
    assert rows["今天的问题丙（依据：10-20）"]["status"] == "answered"
    assert rows["旧问题甲（依据：10-19）"]["status"] == "open"


def test_prompt_order_drives_q_numbering(tmp_path):
    """[Qn] 优先按推送时记录的展示顺序解析（决策 29）。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-19", ["- [想问你-1] 旧问题甲（依据：10-19）"])
    _seed_day(cfg, "2026-10-20", ["- [想问你-1] 今天的问题丙（依据：10-20）"])
    opens = open_questions(cfg)
    set_prompt_order(cfg, [opens[1]["question_key"], opens[0]["question_key"]])
    assert try_answer(cfg, "[Q1] 我的答案") is not None
    rows = {r["question"]: r for r in all_rows(cfg)}
    assert rows["今天的问题丙（依据：10-20）"]["status"] == "answered"


def test_anchor_only_reply_is_not_swallowed_as_bare_answer(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    mark_pushed(cfg, [open_questions(cfg)[0]["question_key"]])
    assert try_answer(cfg, "[Q1]") is None
    assert len(open_questions(cfg)) == 1


def test_qa_brief_fences_future_answers_and_questions(tmp_path):
    """as-of 栅栏（决策 30）：重跑 10-20 不得看到 10-22 之后的问答。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-19", ["- [想问你-1] 旧问题（依据：10-19）"])
    _seed_day(cfg, "2026-10-22", ["- [想问你-1] 未来的问题（依据：10-22）"])
    future = [q for q in open_questions(cfg) if q["day"] == "2026-10-22"][0]
    record_answer(cfg, future["question_key"], "未来才知道的答案")
    answered, open_ = qa_brief(cfg, before_date="2026-10-20")
    assert answered == []
    assert [r["day"] for r in open_] == ["2026-10-19"]


def test_prompt_order_survives_answer_and_archive(tmp_path):
    """展示顺序是不可变快照：答掉 Q1 后 [Q2] 仍必须指用户看到的第 2 条（决策 29）。

    回归用例：早先实现把 keys 按 open 集合过滤，答掉 Q1 后编号整体前移，[Q2] 会指到第 3 题。
    """
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", [
        "- [想问你-1] 问题甲（依据：10-25）",
        "- [想问你-2] 问题乙（依据：10-25）",
        "- [想问你-3] 问题丙（依据：10-25）",
    ])
    opens = open_questions(cfg)
    set_prompt_order(cfg, [q["question_key"] for q in opens])
    assert try_answer(cfg, "[Q1] 答案一") is not None
    assert try_answer(cfg, "[Q2] 答案二") is not None
    rows = {r["question"]: r for r in all_rows(cfg)}
    assert rows["问题甲（依据：10-25）"]["status"] == "answered"
    assert rows["问题乙（依据：10-25）"]["status"] == "answered"
    assert rows["问题丙（依据：10-25）"]["status"] == "open"


def test_out_of_range_q_number_is_not_guessed(tmp_path):
    """点名的编号越界/已答：返回 None，绝不 fall-through 到别的题。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", [
        "- [想问你-1] 问题甲（依据：10-25）",
        "- [想问你-2] 问题乙（依据：10-25）",
    ])
    opens = open_questions(cfg)
    set_prompt_order(cfg, [q["question_key"] for q in opens])
    assert try_answer(cfg, "[Q3] 我的答案") is None
    assert try_answer(cfg, "[Q1] 答案一") is not None
    # Q1 已答：再发 [Q1] 不猜，也不落到 Q2
    assert try_answer(cfg, "[Q1] 补一句") is None
    assert [r["status"] for r in all_rows(cfg) if r["question"] == "问题乙（依据：10-25）"] == ["open"]


def test_bare_q_without_brackets_is_not_swallowed(tmp_path):
    """`Q3 财报什么时候发？` 是正常对话，不是对第 3 题的答案（只有 [Qn] 才算）。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    mark_pushed(cfg, [open_questions(cfg)[0]["question_key"]])
    assert try_answer(cfg, "Q3 财报什么时候发？") is None
    assert open_questions(cfg)[0]["status"] == "open"


def test_weekly_prefixed_command_is_not_swallowed_as_answer(tmp_path):
    """`评分 5 5` / `候选 1` 归周报解析器：即便本次不适用也不能被裸答记成问题答案。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    mark_pushed(cfg, [open_questions(cfg)[0]["question_key"]])
    assert try_answer(cfg, "评分 5 5") is None
    assert try_answer(cfg, "候选 1") is None
    assert open_questions(cfg)[0]["status"] == "open"


def test_week_id_rows_map_to_sunday(tmp_path):
    """周报提问的日锚 YYYY-Www 折算到周日，才能进 as-of 窗口（决策 39）。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-W43", ["- [想问你-1] 周报问题（依据：本周）"])
    assert _day_as_date("2026-W43") == "2026-10-25"
    assert [r["day"] for r in qa_brief(cfg, before_date="2026-10-26")[1]] == ["2026-W43"]
    assert qa_brief(cfg, before_date="2026-10-25")[1] == []


# --------------------------------------------------------------------------- P0 提问账本


def test_mark_pushed_records_ask_ledger(tmp_path):
    """首次发送写 asked_at，重复发送只更新 delivered_at（不改重问语义）。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    key = open_questions(cfg)[0]["question_key"]
    mark_pushed(cfg, [key], now=_dt(2026, 10, 25, 8, 30))
    row = all_rows(cfg)[0]
    assert row["source"] == "qa_open"
    assert row["asked_at"] == "2026-10-25 08:30:00"
    assert row["delivered_at"] == "2026-10-25 08:30:00"

    mark_pushed(cfg, [key], now=_dt(2026, 10, 26, 8, 30))
    row = all_rows(cfg)[0]
    assert row["asks"] == 2
    assert row["asked_at"] == "2026-10-25 08:30:00"
    assert row["delivered_at"] == "2026-10-26 08:30:00"


def test_mark_ignored_counts_once_per_delivery(tmp_path):
    """发出满 24h 未答记一次 ignored_count；同一轮发送幂等，重推后才可再记。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    key = open_questions(cfg)[0]["question_key"]
    mark_pushed(cfg, [key], now=_dt(2026, 10, 25, 8, 30))

    assert mark_ignored(cfg, now=_dt(2026, 10, 25, 20)) == 0  # 未满 24h
    assert mark_ignored(cfg, now=_dt(2026, 10, 26, 9)) == 1
    assert all_rows(cfg)[0]["ignored_count"] == 1
    assert mark_ignored(cfg, now=_dt(2026, 10, 27, 9)) == 0  # 同一轮发送不重复记
    # 记账不改状态：问题仍 open，asks 不变
    assert all_rows(cfg)[0]["status"] == "open"
    assert all_rows(cfg)[0]["asks"] == 1

    mark_pushed(cfg, [key], now=_dt(2026, 10, 28, 8, 30))
    assert mark_ignored(cfg, now=_dt(2026, 10, 29, 9)) == 1
    assert all_rows(cfg)[0]["ignored_count"] == 2


def test_mark_ignored_skips_answered_rows(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    key = open_questions(cfg)[0]["question_key"]
    mark_pushed(cfg, [key], now=_dt(2026, 10, 25, 8, 30))
    record_answer(cfg, key, "在读")
    assert mark_ignored(cfg, now=_dt(2026, 10, 26, 9)) == 0
    assert all_rows(cfg)[0]["ignored_count"] == 0


def test_reply_stats_seven_day_rate(tmp_path):
    """7 日回复率只算窗口内实际发出过的问题，按来源分组。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(
        cfg,
        "2026-10-20",
        [
            "- [想问你-1] 问题甲（依据：10-20）",
            "- [想问你-2] 问题乙（依据：10-20）",
        ],
    )
    keys = [r["question_key"] for r in open_questions(cfg)]
    mark_pushed(cfg, keys, now=_dt(2026, 10, 20, 8, 30))
    record_answer(cfg, keys[0], "答案甲")

    # 窗口外（9 天前发出）的问题不计入
    _seed_day(cfg, "2026-10-17", ["- [想问你-1] 问题丙（依据：10-17）"])
    stale_key = next(r["question_key"] for r in open_questions(cfg) if r["question"] == "问题丙（依据：10-17）")
    mark_pushed(cfg, [stale_key], now=_dt(2026, 10, 17, 8, 30))

    stats_ = reply_stats(cfg, now=_dt(2026, 10, 26, 9))
    assert stats_["window_days"] == 7
    assert stats_["asked"] == 2
    assert stats_["answered"] == 1
    assert stats_["ignored"] == 0
    assert stats_["asked_today"] == 0
    assert stats_["answer_rate"] == 0.5
    assert stats_["by_source"]["qa_open"] == {"asked": 2, "answered": 1, "ignored": 0}


def test_reply_stats_empty_ledger_is_zero(tmp_path):
    cfg = Config.for_tests(tmp_path)
    stats_ = reply_stats(cfg, now=_dt(2026, 10, 26, 9))
    assert stats_["asked"] == 0
    assert stats_["answer_rate"] == 0.0
    assert stats_["by_source"] == {}


# --------------------------------------------------------------------------- P1 多问题匹配


class _StubLLM:
    """最小 chat-model 替身：``content`` 可为字符串（正常返回）或异常实例（模拟失败）。"""

    def __init__(self, content):
        self.content = content
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        if isinstance(self.content, Exception):
            raise self.content
        return type("R", (), {"content": self.content})()


def _seed_two_open(cfg) -> list[str]:
    _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] 你是在读还是上班？（依据：10-25）",
            "- [想问你-2] 工学椅到底还买不买？（依据：长期画像）",
        ],
    )
    keys = [q["question_key"] for q in open_questions(cfg)]
    mark_pushed(cfg, keys)
    return keys


def test_record_answer_stores_match_fields(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    key = open_questions(cfg)[0]["question_key"]
    row = record_answer(cfg, key, "在读", method="rule", confidence=0.82, now=_dt(2026, 10, 26, 9))
    assert row["match_method"] == "rule"
    assert row["match_confidence"] == 0.82
    assert row["answered_at"] == "2026-10-26 09:00:00"
    assert row["answer_day"] == "2026-10-26"
    assert row["answer"] == "在读"


def test_match_rule_attributes_natural_answer_among_multiple(tmp_path):
    """3 个开放问题场景的核心验收：自然语言短答只归因到匹配的那一题。"""
    cfg = Config.for_tests(tmp_path)
    keys = _seed_two_open(cfg)
    hit = match_and_record(cfg, "在读", now=_dt(2026, 10, 26, 9))
    assert hit is not None
    assert hit["question_key"] == keys[0]
    assert hit["method"] == "rule"
    assert hit["confidence"] >= 0.7

    rows = {r["question_key"]: r for r in all_rows(cfg)}
    assert rows[keys[0]]["status"] == "answered"
    assert rows[keys[0]]["match_method"] == "rule"
    assert rows[keys[0]]["answered_at"] == "2026-10-26 09:00:00"
    assert rows[keys[1]]["status"] == "open"
    assert [r["question_key"] for r in open_questions(cfg)] == [keys[1]]


def test_match_rule_does_not_guess_when_scores_are_close(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(
        cfg,
        "2026-10-25",
        [
            "- [想问你-1] 今天去学校吗？（依据：10-25）",
            "- [想问你-2] 今天去学校还是上班？（依据：10-25）",
        ],
    )
    mark_pushed(cfg, [q["question_key"] for q in open_questions(cfg)])
    assert match_answer(cfg, "今天去学校") is None


def test_match_skips_question_command_and_weekly_messages(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_two_open(cfg)
    assert match_answer(cfg, "在读吗") is None
    assert match_answer(cfg, "/在读") is None
    assert match_answer(cfg, "评分 5 5") is None
    assert match_answer(cfg, "在读" * 70) is None  # 超长消息不当答案


def test_match_answer_ignores_single_open_question(tmp_path):
    """唯一开放问题由 try_answer 的裸答路径负责，匹配层不重复接管。"""
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 你是在读还是上班？（依据：10-25）"])
    mark_pushed(cfg, [open_questions(cfg)[0]["question_key"]])
    assert match_answer(cfg, "在读") is None


def test_match_llm_fallback_records_high_confidence(tmp_path):
    cfg = Config.for_tests(tmp_path)
    keys = _seed_two_open(cfg)
    stub = _StubLLM('{"question_key": "%s", "confidence": 0.92}' % keys[0])
    hit = match_answer(cfg, "在读还是上班哈哈哈", llm=stub)
    assert stub.calls == 1
    assert hit is not None
    assert hit["question_key"] == keys[0]
    assert hit["method"] == "llm"
    assert hit["confidence"] == 0.92


def test_match_llm_low_confidence_is_dropped(tmp_path):
    cfg = Config.for_tests(tmp_path)
    keys = _seed_two_open(cfg)
    stub = _StubLLM('{"question_key": "%s", "confidence": 0.5}' % keys[0])
    assert match_answer(cfg, "在读还是上班哈哈哈", llm=stub) is None


def test_match_llm_failure_degrades_to_no_match(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_two_open(cfg)
    stub = _StubLLM(RuntimeError("timeout"))
    assert match_answer(cfg, "在读还是上班哈哈哈", llm=stub) is None
    assert stub.calls == 1


def test_match_llm_can_be_disabled_by_config(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_two_open(cfg)
    config_dir = cfg.root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "proactive.json").write_text(
        '{"match": {"llm_enabled": false}}', encoding="utf-8"
    )
    stub = _StubLLM('{"question_key": "x", "confidence": 0.99}')
    assert match_answer(cfg, "在读还是上班哈哈哈", llm=stub) is None
    assert stub.calls == 0


def test_unanswer_rolls_back_status_and_pending_candidate(tmp_path):
    cfg = Config.for_tests(tmp_path)
    _seed_day(cfg, "2026-10-25", ["- [想问你-1] 工学椅到底还买不买？（依据：长期画像）"])
    key = open_questions(cfg)[0]["question_key"]
    mark_pushed(cfg, [key])
    assert try_answer(cfg, "[Q1] 先不买了") is not None

    assert unanswer(cfg, key) is True
    row = all_rows(cfg)[0]
    assert row["status"] == "open"
    assert row["answer"] == ""
    assert row["answered_at"] == ""
    assert row["match_method"] == ""
    assert row["match_confidence"] == 0.0
    assert [r["question_key"] for r in open_questions(cfg)] == [key]
    cands = (cfg.root / "data" / "qa" / "candidates.jsonl").read_text(encoding="utf-8")
    assert cands.strip() == ""
    assert unanswer(cfg, key) is False

