"""Tests for gacore.context: system-prompt assembly, history folding, summary extraction, periodic hints.

The prompt module is the port of GA's turn_end_callback + _get_anchor_prompt + get_global_memory
(ga.py:558-613): the system prompt is rebuilt per turn and never stored in state.messages.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Final

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

import gacore.context as ctx
from gacore.config import Config
from gacore.context import (
    build_system_prompt,
    build_turn_prompt,
    extract_summaries,
    fold_history,
    periodic_hints,
    stamp_history_lines,
)

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]

CHECKPOINT_HINT: Final = "[Checkpoint time: update working checkpoint]"
FILE_HINT: Final = "[Write your current state to a file]"
ASK_USER_HINT: Final = "[Long-running: consider asking the user for confirmation]"
ANTI_LOOP_HINT: Final = "[Warning: long loop detected, wrap up soon]"
MEMORY_HINT: Final = "[Memory refresh: reload L1 insights and L2 facts into working memory]"

# A fake FactCard with content, so tests can simulate an available card without DB.
_FAKE_CARD_WITH_CONTENT = {
    "day": "2026-08-31",
    "now_ms": 0,
    "available": True,
    "device_id": "d1",
    "compact": "=== 生活事实（今日概览）\n· 08:40 在家，直到 09:10；09:25 在公司",
    "compact_sections": [
        {"key": "places", "title": "足迹", "lines": ["· 08:40 在家，直到 09:10；09:25 在公司"]}
    ],
    "debug_meta": {"card_fp": "data/langTrack.db", "degrade": ""},
}

_FAKE_CARD_EMPTY = {
    "day": "2026-08-31",
    "now_ms": 0,
    "available": False,
    "device_id": "d1",
    "compact": "",
    "compact_sections": [],
    "debug_meta": {"card_fp": "data/langTrack.db", "degrade": "no_data"},
}


@pytest.fixture(autouse=True)
def _no_real_langtrack_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁止任何 prompt 用例触碰真实 data/langTrack.db：默认卡片为空且不抛异常。"""

    def _fake_build(*args, **kwargs):
        return _FAKE_CARD_EMPTY

    def _fake_render(card, *args, **kwargs):
        return str(card.get("compact") or "")

    monkeypatch.setattr(ctx.fact_card, "build", _fake_build)
    monkeypatch.setattr(ctx.fact_card, "render_compact", _fake_render)


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    """A hermetic Config rooted at tmp_path with no asset file present."""
    return Config.for_tests(tmp_path)


@pytest.fixture()
def cfg_with_assets(tmp_path: Path) -> Config:
    """A Config rooted at tmp_path whose asset dir carries a copy of the real sys_prompt.txt."""
    cfg = Config.for_tests(tmp_path)
    cfg.asset_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_PROJECT_ROOT / "config" / "assets" / "sys_prompt.txt", cfg.asset_dir / "sys_prompt.txt")
    return cfg


def _state(messages: list[BaseMessage], *, turn: int = 0, key_info: str | None = None) -> dict:
    """Build the GAState-shaped dict used by the prompt functions."""
    working: dict = {"key_info": key_info} if key_info is not None else {}
    return {"messages": messages, "working": working, "current_turn": turn}


def test_build_system_prompt_includes_l0_rules_from_asset_file(cfg_with_assets: Config) -> None:
    """Given a cfg whose asset dir has the real sys_prompt.txt, the prompt must contain its L0 rules."""
    real = (_PROJECT_ROOT / "config" / "assets" / "sys_prompt.txt").read_text(encoding="utf-8")
    probe_rule = next(line for line in real.splitlines() if "探测优先" in line)

    prompt = build_system_prompt(_state([HumanMessage(content="hi")]), cfg_with_assets)

    assert probe_rule in prompt


def test_build_system_prompt_falls_back_to_default_when_asset_missing(cfg: Config) -> None:
    """Given a cfg with no sys_prompt.txt, the prompt must fall back to GA's probe-first default rules."""
    prompt = build_system_prompt(_state([HumanMessage(content="hi")]), cfg)

    assert "探测优先" in prompt


def test_build_system_prompt_contains_key_info_when_working_set(cfg_with_assets: Config) -> None:
    """Given working.key_info set, the prompt must carry the working checkpoint section with its value."""
    state = _state([HumanMessage(content="hi")], key_info="user wants migration to python")

    prompt = build_system_prompt(state, cfg_with_assets)

    assert "[Working checkpoint]" in prompt
    assert "user wants migration to python" in prompt


def test_build_system_prompt_omits_key_info_when_working_empty(cfg_with_assets: Config) -> None:
    """Given empty working, the prompt must not contain the working checkpoint section."""
    prompt = build_system_prompt(_state([HumanMessage(content="hi")]), cfg_with_assets)

    assert "[Working checkpoint]" not in prompt


def test_build_system_prompt_injects_periodic_hints_at_turn_boundary(cfg_with_assets: Config) -> None:
    """Given current_turn=13, the prompt must contain the checkpoint hint injected at turn boundaries."""
    state = _state([HumanMessage(content="hi")], turn=13)

    prompt = build_system_prompt(state, cfg_with_assets)

    assert CHECKPOINT_HINT in prompt


def test_fold_history_caps_at_max_lines_keeping_most_recent() -> None:
    """Given 40 messages and max_lines=30, only the last 30 entries survive, most recent last."""
    messages = [HumanMessage(content=f"msg-{i}") for i in range(40)]

    folded = fold_history(messages, max_lines=30)

    assert len(folded) == 30
    assert folded[0] == "[USER] msg-10"
    assert folded[-1] == "[USER] msg-39"
    assert not any("msg-0" in line for line in folded)


def test_fold_history_truncates_long_content_with_marker() -> None:
    """Given content longer than 200 chars, the folded line keeps the first 200 chars plus a marker."""
    long = "a" * 250

    human_line = fold_history([HumanMessage(content=long)])[0]
    ai_line = fold_history([AIMessage(content=long)])[0]

    assert human_line == "[USER] " + "a" * 200 + "..."
    assert ai_line == "[Agent] " + "a" * 200 + "..."


def test_fold_history_short_content_kept_verbatim() -> None:
    """Given short content, the folded line carries the full content with no truncation marker."""
    assert fold_history([HumanMessage(content="short")]) == ["[USER] short"]
    assert fold_history([AIMessage(content="brief")]) == ["[Agent] brief"]


def test_fold_history_skips_tool_messages() -> None:
    """Given a ToolMessage among the history, it must be skipped rather than folded."""
    messages = [
        HumanMessage(content="q"),
        ToolMessage(content="tool result", tool_call_id="t1"),
        AIMessage(content="a"),
    ]

    folded = fold_history(messages)

    assert folded == ["[USER] q", "[Agent] a"]
    assert not any("tool result" in line for line in folded)


def test_fold_history_handles_empty_list() -> None:
    """Given no messages, fold_history returns an empty list."""
    assert fold_history([]) == []


def test_extract_summaries_finds_blocks_in_ai_messages() -> None:
    """Given AIMessages with <summary> blocks, their inner text is returned in order."""
    messages = [
        HumanMessage(content="<summary>ignored: user text</summary>"),
        AIMessage(content="did work <summary>checked file state</summary> done"),
        AIMessage(content="<summary>insight one</summary> then <summary>insight two</summary>"),
        AIMessage(content="no block here"),
        AIMessage(content="<summary>\n  padded  \n</summary>"),
    ]

    summaries = extract_summaries(messages)

    assert summaries == ["checked file state", "insight one", "insight two", "padded"]


def test_extract_summaries_skips_non_string_content() -> None:
    """Given an AIMessage with structured (non-str) content, it must be skipped without error."""
    messages = [AIMessage(content=[{"type": "text", "text": "<summary>structured</summary>"}])]

    assert extract_summaries(messages) == []


@pytest.mark.parametrize(
    ("turn", "expected"),
    [
        (5, []),
        (10, [MEMORY_HINT]),
        (13, [CHECKPOINT_HINT]),
        (31, [FILE_HINT]),
        (101, [ANTI_LOOP_HINT]),
        (175, [ASK_USER_HINT, ANTI_LOOP_HINT]),
    ],
)
def test_periodic_hints_at_turns(turn: int, expected: list[str], cfg: Config) -> None:
    """Given a turn number, periodic_hints returns exactly the hints scheduled for that turn."""
    assert periodic_hints(turn, cfg) == expected


def test_periodic_hints_turn_130_fires_checkpoint_and_anti_loop(cfg: Config) -> None:
    """Given turn 130, both the checkpoint hint (%13) and the anti-loop warning (>100) must fire."""
    hints = periodic_hints(130, cfg)

    assert CHECKPOINT_HINT in hints
    assert ANTI_LOOP_HINT in hints
    assert MEMORY_HINT in hints


def test_build_turn_prompt_returns_system_message_plus_existing_without_duplication(cfg_with_assets: Config) -> None:
    """Given a state with history, the prompt is [SystemMessage, *messages]: no dupes, no mutation."""
    messages = [HumanMessage(content="q1"), AIMessage(content="a1")]
    state = _state(messages, key_info="k1")

    result = build_turn_prompt(state, cfg_with_assets)

    assert len(result) == 1 + len(messages)
    assert isinstance(result[0], SystemMessage)
    assert result[1:] == messages
    assert result[1] is messages[0]
    assert sum(1 for m in result if isinstance(m, SystemMessage)) == 1
    assert state["messages"] == messages
    assert state["working"] == {"key_info": "k1"}


def test_build_turn_prompt_embeds_folded_context_in_system_message(cfg_with_assets: Config) -> None:
    """Given a history, the system message must carry the folded earlier context section."""
    messages = [HumanMessage(content="q1"), AIMessage(content="a1")]

    result = build_turn_prompt(_state(messages, key_info="k1"), cfg_with_assets)

    assert isinstance(result[0], SystemMessage)
    assert "=== Earlier context ===" in result[0].content
    assert "[USER] q1" in result[0].content
    assert "[Agent] a1" in result[0].content
    assert "k1" in result[0].content


def test_build_turn_prompt_with_empty_messages_returns_single_system_message(cfg: Config) -> None:
    """Given an empty history, the prompt is a single system message with no earlier-context section."""
    result = build_turn_prompt(_state([]), cfg)

    assert len(result) == 1
    assert isinstance(result[0], SystemMessage)
    assert "=== Earlier context ===" not in result[0].content


def test_stamp_history_lines_marks_timestamp_lines() -> None:
    """History lines carrying an explicit time/date get a [历史@时间戳] prefix."""
    lines = ["[USER] 昨天3点开会", "[Agent] 好的，8月24日见", "[USER] 好的"]
    stamped = stamp_history_lines(lines)
    assert stamped[0].startswith("[历史@时间戳] ")
    assert stamped[1].startswith("[历史@时间戳] ")
    assert stamped[2] == "[USER] 好的"


def test_build_turn_prompt_marks_old_history_timestamps(cfg_with_assets: Config) -> None:
    """Past conversation timestamps are physically marked as historical inside the system prompt."""
    messages = [HumanMessage(content="8月1日 我们约过面基"), AIMessage(content="好的，到时见")]
    prompt = build_turn_prompt(_state(messages), cfg_with_assets)
    sys_msg = prompt[0]
    assert isinstance(sys_msg, SystemMessage)
    assert "[历史@时间戳] [USER] 8月1日 我们约过面基" in sys_msg.content


# ------------------------------------------------------------------ fact card injection


def _set_card(monkeypatch: pytest.MonkeyPatch, card: dict) -> None:
    def _fake_build(*args, **kwargs):
        return card

    def _fake_render(c, *args, **kwargs):
        return str(c.get("compact") or "")

    monkeypatch.setattr(ctx.fact_card, "build", _fake_build)
    monkeypatch.setattr(ctx.fact_card, "render_compact", _fake_render)


def test_fact_card_injected_between_rollover_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch, cfg_with_assets: Config
) -> None:
    """Given an available card, the compact text must be injected before the working checkpoint."""
    _set_card(monkeypatch, _FAKE_CARD_WITH_CONTENT)
    state = _state([HumanMessage(content="hi")], key_info="k1")

    prompt = build_system_prompt(state, cfg_with_assets)

    assert ctx.LANGTRACK_CARD_PREFIX in prompt
    assert "· 08:40 在家，直到 09:10；09:25 在公司" in prompt
    # 卡片在 working checkpoint 之前
    assert prompt.index(ctx.LANGTRACK_CARD_PREFIX) < prompt.index("[Working checkpoint]")
    assert prompt.index(ctx.LANGTRACK_CARD_PREFIX) < prompt.index("[Working checkpoint]") < prompt.index("k1")


def test_fact_card_empty_card_not_injected(monkeypatch: pytest.MonkeyPatch, cfg_with_assets: Config) -> None:
    """Given an empty card (no data), the compact text must NOT be injected."""
    _set_card(monkeypatch, _FAKE_CARD_EMPTY)

    prompt = build_system_prompt(_state([HumanMessage(content="hi")]), cfg_with_assets)

    assert ctx.LANGTRACK_CARD_PREFIX not in prompt


def test_fact_card_build_failure_degrades_without_breaking_prompt(
    monkeypatch: pytest.MonkeyPatch, cfg_with_assets: Config
) -> None:
    """Given a build that raises, the prompt must still be assembled (缺库不能弄死 QQ)."""

    def _boom(*args, **kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr(ctx.fact_card, "build", _boom)

    prompt = build_system_prompt(_state([HumanMessage(content="hi")]), cfg_with_assets)

    assert ctx.LANGTRACK_CARD_PREFIX not in prompt
    assert "探测优先" in prompt  # 正常兜底规则仍在


def test_memory_bg_rule_appended_only_once(
    monkeypatch: pytest.MonkeyPatch, cfg_with_assets: Config
) -> None:
    """The memory-background rule must appear exactly once even with daily + card both injected."""
    _set_card(monkeypatch, _FAKE_CARD_WITH_CONTENT)
    monkeypatch.setattr(ctx, "load_recent_daily_summaries", lambda cfg_: "8月30日：昨日开会，讨论了日程")

    prompt = build_system_prompt(_state([HumanMessage(content="hi")]), cfg_with_assets)

    assert prompt.count("[记忆背景铁律]") == 1


def test_fact_card_injected_logged(
    monkeypatch: pytest.MonkeyPatch, cfg_with_assets: Config
) -> None:
    """The injection must emit a structured 'fact card injected' log with outlet=prompt."""
    records: list[tuple[str, dict]] = []

    class _Rec:
        def info(self, message: str, **fields: object) -> None:
            records.append((message, fields))

    monkeypatch.setattr(ctx, "_context_logger", _Rec())
    _set_card(monkeypatch, _FAKE_CARD_WITH_CONTENT)

    build_system_prompt(_state([HumanMessage(content="hi")]), cfg_with_assets)

    hits = [r for m, r in records if m == "fact card injected"]
    assert hits
    assert hits[0]["outlet"] == "prompt"
    assert hits[0]["injected"] is True
