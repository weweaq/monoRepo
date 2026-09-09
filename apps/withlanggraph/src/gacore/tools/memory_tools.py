"""LangChain tools for memory: short-term working checkpoint and long-term persistence.

Mirrors GA's do_update_working_checkpoint / do_start_long_term_update: global_mem.txt holds
L2 global facts, global_mem_insight.txt holds the L1 insight index. The tools are pure —
update_working_checkpoint returns a Command that writes state.working; start_long_term_update
returns a dict the standard ToolNode wraps into a ToolMessage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from gacore.config import Config


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
    now = datetime.now(UTC).astimezone()
    timestamp = now.isoformat(timespec="seconds")
    day = now.date().isoformat()
    # Delegate to the single persist helper so the active write path fan-outs to the facts
    # portrait + pgvector exactly like the passive memory_maintain node (阶段二 方案2:
    # 写记忆的同步必须收口到写函数, 任何写入口都不会漏同步向量库).
    from gacore.memory_maintenance import persist_entry

    written = persist_entry(
        cfg,
        fact_line=f"[{timestamp}] {topic}",
        insight_line=f"[{day}] insight: {topic}",
        facts_statement=topic,
    )
    if not written.get("updated"):
        return {"error": written.get("error") or "persist failed"}
    return {"updated": "global_mem+insight+facts", "topic": topic, "paths": written["paths"]}
