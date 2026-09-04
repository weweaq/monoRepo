"""LangGraph state schema and initializer for gacore.

The state schema extends the official ``langchain.agents.middleware.AgentState`` (which
provides the ``messages`` channel with the add_messages reducer plus the ``jump_to``
temporary control channel used by create_agent middleware) with gacore's overwrite-only
working channels. Every custom channel has no reducer, so LangGraph's default overwrite
semantics apply: the latest update replaces the previous value.
"""

from __future__ import annotations

from typing import Any, Final

from langchain.agents.middleware import AgentState
from langchain_core.messages import HumanMessage

from gacore.config import Config

EXIT_REASONS: Final = (
    "CURRENT_TASK_DONE",
    "EXITED",
    "MAX_TURNS_EXCEEDED",
    "AGENT_ERROR",
    "TIME_GUARD_EXCEEDED",
)
DEFAULT_MAX_TURNS: Final = 40


class GAState(AgentState[Any], total=False):
    """Agent loop state: official AgentState channels plus gacore's working channels.

    Inherited from ``AgentState``: ``messages`` (add_messages reducer) and ``jump_to``
    (ephemeral control channel consumed by create_agent's routing edges). The remaining
    channels are gacore-specific and use plain overwrite semantics.
    """

    working: dict
    current_turn: int
    max_turns: int
    done_hooks: list[str]
    retry_count: int
    time_guard_retries: int  # output-side time-guard retry budget (see middleware.py)
    exit_reason: str | None
    pending_images: list[str]  # accumulated image paths for multi-image processing
    active_card: str | None  # active character-card id for this conversation; declared so the graph channel does not drop it
    rollover_context: str | None  # one-shot cross-day memory injection (from onboard_pack.json); cleared after first turn
    output_mode: str | None  # per-turn output formatting mode ("proposal" = multi-option reply); cleared by cleanup_images after the turn


def new_state(user_input: str, cfg: Config, active_card: str | None = None) -> GAState:
    """Seed a fresh GAState for a new conversation with the user's first message."""
    return GAState(
        messages=[HumanMessage(content=user_input)],
        working={},
        current_turn=0,
        max_turns=cfg.max_turns,
        done_hooks=[],
        retry_count=0,
        time_guard_retries=0,
        exit_reason=None,
        active_card=active_card,
        pending_images=[],
    )
