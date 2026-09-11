"""Unit tests for gacore.recall_baseline (read-only log-replay parser)."""

from gacore.recall_baseline import (
    aggregate,
    parse_turn,
    strip_image_markers,
    user_query,
)

_SEM_HIT = "- [2026-09-09T20:28:32+08:00] [婚姻·登记日期] 改为 2026-09-12 (sim 0.661)"
_EPI_HIT = "- 2026-09-08 · 结婚领证 (sim 0.512)"
_HEADER = "=== 向量召回记忆（语义画像 + 某天事件，仅供了解过往，绝不代表当前） ==="


def _record(query: str, system_rag: str | None, user_image: bool = False) -> dict:
    if system_rag:
        system = "基础规则\n" + system_rag
    else:
        system = "基础规则\n[Current time] 2026-09-10"
    content = f"[IMAGE:x.png] {query}" if user_image else query
    return {
        "ts": "2026-09-10T10:00:00",
        "session": "s1",
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }


def test_strip_image_markers():
    assert strip_image_markers("[IMAGE:a.png] 吃了没") == "吃了没"
    assert strip_image_markers("   ") == ""


def test_user_query_takes_last_user_and_strips_images():
    rec = _record("那段经历后来呢", f"{_HEADER}\n{_SEM_HIT}", user_image=True)
    assert user_query(rec["messages"]) == "那段经历后来呢"


def test_user_query_accepts_langchain_human_role():
    # LangChain serializes user messages as role="human", not "user".
    messages = [
        {"role": "system", "content": "规则"},
        {"role": "ai", "content": "好的"},
        {"role": "tool", "content": "{\"ok\": true}"},
        {"role": "human", "content": "[IMAGE:x.png] 那天发生了什么"},
    ]
    assert user_query(messages) == "那天发生了什么"


def test_parse_turn_detects_no_rag():
    turn = parse_turn(_record("普通问题", None))
    assert turn.has_rag is False
    assert turn.n_sem == 0 and turn.n_epi == 0
    assert turn.sims == []


def test_parse_turn_counts_semantic_and_episodic_hits():
    block = f"{_HEADER}\n[长期画像·语义]\n{_SEM_HIT}\n[某天发生·情景]\n{_EPI_HIT}"
    turn = parse_turn(_record("婚期定在哪天", block))
    assert turn.has_rag is True
    assert turn.n_sem == 1
    assert turn.n_epi == 1
    assert turn.sims == [0.661, 0.512]
    assert turn.recalled == [_SEM_HIT, _EPI_HIT]


def test_aggregate_emits_input_to_recall_detail():
    rag = f"{_HEADER}\n[长期画像·语义]\n{_SEM_HIT}"
    turns = [
        parse_turn(_record("婚期", rag)),
        parse_turn(_record("今天去哪吃", None)),
    ]
    report = aggregate(turns)
    by_query = {r["query"]: r for r in report.recalls}
    assert by_query["婚期"]["has_rag"] is True
    assert by_query["婚期"]["recalled"] == [_SEM_HIT]
    assert by_query["婚期"]["sims"] == [0.661]
    assert by_query["今天去哪吃"]["has_rag"] is False
    assert by_query["今天去哪吃"]["recalled"] == []


def test_aggregate_injection_rate_and_gate():
    rag = f"{_HEADER}\n[长期画像·语义]\n{_SEM_HIT}"
    turns = [
        parse_turn(_record("婚期", rag)),            # injected
        parse_turn(_record("婚期", rag)),            # dup query, still injected
        parse_turn(_record("今天去哪吃", None)),      # empty
        parse_turn(_record("", None)),              # empty query -> not gate-eligible
        parse_turn(_record("x" * 250, None)),       # too long -> not gate-eligible
    ]
    report = aggregate(turns)
    assert report.totals["gate_eligible_queries"] == 2        # 婚期 + 今天去哪吃
    assert report.totals["distinct_queries"] == 2
    assert report.injection["injected_queries"] == 1          # 婚期
    assert report.injection["empty_queries"] == 1             # 今天去哪吃
    assert report.injection["inject_rate"] == 0.5
    assert report.hits["total_semantic_hits"] == 2            # 婚期出现2次invoke
    assert report.hits["sim_max"] == 0.661