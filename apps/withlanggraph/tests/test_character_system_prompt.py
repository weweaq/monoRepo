"""Tests for role-card injection into build_system_prompt.

The core design guarantee: tool availability is decided by the runtime assembly
(graph/model binding), NOT by the persona. So when a card is active the prompt must
contain BOTH the base L0 rules (probe-first action rules that run tools) AND the
persona text plus the explicit tool-bridge line — the character can call tools while
talking in character.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Final

import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from gacore.character import card_dir
from gacore.config import Config
from gacore.context import ROLE_HEADER, build_system_prompt

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
_ROLE_TOOL_BRIDGE: Final = "保留系统智能体的全部能力"


@pytest.fixture()
def cfg_with_cards(tmp_path: Path) -> Config:
    """Hermetic cfg with the real sys_prompt.txt plus one character card installed."""
    cfg = Config.for_tests(tmp_path)
    cfg.asset_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_PROJECT_ROOT / "config" / "assets" / "sys_prompt.txt", cfg.asset_dir / "sys_prompt.txt")
    d = card_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "nami.md").write_text(
        "# 娜美\n我要画出世界地图。我是航海士，不会说自己是 AI。\n",
        encoding="utf-8",
    )
    return cfg


def _state(messages: list[BaseMessage], *, active_card: str | None = None) -> dict:
    return {"messages": messages, "working": {}, "current_turn": 0, "active_card": active_card}


def test_no_card_keeps_base_prompt_only(cfg_with_cards: Config) -> None:
    prompt = build_system_prompt(_state([HumanMessage(content="hi")]), cfg_with_cards)
    assert "探测优先" in prompt          # base L0 rules still there
    assert ROLE_HEADER not in prompt    # no persona layer
    assert "航海士" not in prompt


def test_active_card_stacks_persona_on_top_of_base_rules(cfg_with_cards: Config) -> None:
    prompt = build_system_prompt(_state([HumanMessage(content="hi")], active_card="nami"), cfg_with_cards)

    # Base rules stay present: the character keeps full tool capability.
    assert "探测优先" in prompt
    # Persona layer present, with header + card body.
    assert ROLE_HEADER in prompt
    assert "我要画出世界地图" in prompt
    # The explicit tool-bridge line is injected so the model knows tools remain usable.
    assert _ROLE_TOOL_BRIDGE in prompt


def test_missing_card_is_silently_skipped(cfg_with_cards: Config) -> None:
    prompt = build_system_prompt(_state([HumanMessage(content="hi")], active_card="no-such"), cfg_with_cards)
    assert "探测优先" in prompt
    assert ROLE_HEADER not in prompt
    assert "no-such" not in prompt


def test_state_without_active_card_key_is_handled(cfg_with_cards: Config) -> None:
    prompt = build_system_prompt({"messages": [HumanMessage(content="hi")]}, cfg_with_cards)
    assert ROLE_HEADER not in prompt


def test_active_card_survives_graph_channels_and_reaches_llm(cfg_with_cards: Config) -> None:
    """Regression for the 'card set but not injected' bug.

    The card only works end-to-end when GAState declares ``active_card`` so the
    graph channels keep it alive: set at /role time, it must reach the model call
    where GAPromptMiddleware calls build_system_prompt. If the field is dropped
    from GAState.__annotations__, the channel silently vanishes and the persona
    never lands in the system prompt — this test pins that wiring.
    """
    import langchain_core  # noqa: F401  (ensure pydantic models behave)

    from gacore.character import card_dir
    from gacore.state import GAState

    # Guard: the field must be part of the state schema or the channel is dropped.
    assert "active_card" in GAState.__annotations__, (
        "GAState must declare active_card; without it the graph channel silently drops the value"
    )

    # Install the nami card into the hermetic cfg's card dir.
    d = card_dir(cfg_with_cards)
    d.mkdir(parents=True, exist_ok=True)
    (d / "nami.md").write_text("# 娜美\n我要画出世界地图。我是航海士，不会说自己是 AI。\n", encoding="utf-8")

    _run_e2e_assert(cfg_with_cards)


def _run_e2e_assert(cfg: Config) -> None:
    """Drive the real graph with a capture LLM and assert the persona lands.

    Kept as a plain helper so the sync test body stays readable; it creates and
    closes its own event loop (no pytest-asyncio coupling for this file).
    """
    import asyncio

    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver

    from conftest import BindableGenericFakeChatModel
    from gacore.graph import build_graph
    from gacore.state import new_state

    seen: list[str] = []

    class CaptureLLM(BindableGenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            for m in messages:
                if getattr(m, "type", "") == "system":
                    seen.append(str(m.content))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def run() -> None:
        llm = CaptureLLM(messages=iter([AIMessage(content="我是航海士，说吧。")]))
        graph = build_graph(llm=llm, cfg=cfg, checkpointer=MemorySaver())
        state = new_state("给我点钱", cfg, active_card="nami")
        await graph.ainvoke(state, {"configurable": {"thread_id": "verify-e2e"}, "recursion_limit": 200})

    asyncio.run(run())

    assert seen, "model was never called"
    prompt = seen[-1]
    # The first '# 娜美' heading is the display name and is stripped by card_prompt;
    # the body text is what must land in the system prompt alongside the bridge.
    assert "我要画出世界地图" in prompt
    assert "我是航海士" in prompt
    assert _ROLE_TOOL_BRIDGE in prompt
    assert "探测优先" in prompt

