"""LangChain tools for memory: short-term working checkpoint and long-term persistence.

Mirrors GA's do_update_working_checkpoint / do_start_long_term_update: global_mem.txt holds
L2 global facts, global_mem_insight.txt holds the L1 insight index. The tools are pure —
update_working_checkpoint returns a Command that writes state.working; start_long_term_update
returns a dict the standard ToolNode wraps into a ToolMessage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Final

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from gacore.config import Config

_FACTS_FILE: Final = "global_mem.txt"
_INSIGHTS_FILE: Final = "global_mem_insight.txt"


@tool
def update_working_checkpoint(
    key_info: str,
    related_sop: str | None = None,
    tool_call_id: Annotated[str | None, InjectedToolCallId] = None,
) -> Command:
    """Update the short-term working checkpoint with current task state.

    Returns a Command whose update folds key_info (and related_sop) into state.working and
    pairs a ToolMessage to the originating tool_call_id; no goto is used.
    """
    return Command(
        update={
            "working": {"key_info": key_info, "related_sop": related_sop or ""},
            "messages": [
                ToolMessage(
                    content=json.dumps(
                        {
                            "key_info": key_info,
                            "related_sop": related_sop or "",
                            "result": "working key_info updated",
                        },
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def start_long_term_update(topic: str, _cfg: Config | None = None) -> dict:
    """Distill a topic into long-term memory: append to the L2 fact store and the L1 insight index.

    _cfg is an injection seam excluded from the tool's args schema; production calls fall
    back to Config.default() and tests inject Config.for_tests(tmp_path).
    """
    cfg = _cfg or Config.default()
    facts_path = cfg.memory_dir / _FACTS_FILE
    insights_path = cfg.memory_dir / _INSIGHTS_FILE
    now = datetime.now(UTC).astimezone()
    timestamp = now.isoformat(timespec="seconds")
    day = now.date().isoformat()
    try:
        cfg.memory_dir.mkdir(parents=True, exist_ok=True)
        with facts_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {topic}\n")
        with insights_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{day}] insight: {topic}\n")
    except OSError as e:
        return {"error": str(e)}
    return {"updated": "global_mem+insight", "topic": topic, "paths": [str(facts_path), str(insights_path)]}
