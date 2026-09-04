"""LangGraph agent wiring for gacore: the official create_agent loop and its runner.

Replaces the hand-written 2-node StateGraph with ``langchain.agents.create_agent``: the
official prebuilt agent topology (model node + prebuilt ToolNode + tool routing) with
GA's turn logic carried by middleware (gacore.middleware) and the custom GAState schema.
ask_user interrupts pause the graph via the checkpointer and resume with a Command, as
before.

Image-aware pre-processing: classify_message / wait_for_text nodes wrap the core agent
subgraph to implement multi-image accumulation via LangGraph interrupt/resume. Images
are batched in state.pending_images until the user provides text; then all images +
text are routed to the core agent (process node) for VLM analysis.
"""

from __future__ import annotations

import re
import uuid
from typing import Final

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from gacore.config import Config
from gacore.llm import get_llm
from gacore.middleware import GAPromptMiddleware, GATurnLogicMiddleware, format_agent_error
from gacore.state import GAState, new_state
from gacore.tools import build_tool_list

DEFAULT_RECURSION_LIMIT: Final = 200

_IMAGE_MARKER_RE: Final = re.compile(r"\[IMAGE:(.+?)\]")


def suggested_recursion_limit(max_turns: int | None) -> int:
    """Return a recursion limit that leaves headroom for a full turn budget.

    Each tool round costs two graph steps (model, tools) and a plain answer one (model),
    so max_turns needs a 2x multiplier; the +50 margin absorbs empty-response retries and
    done_hooks loops. Falls back to the module default when max_turns is unknown.
    """
    if max_turns is None:
        return DEFAULT_RECURSION_LIMIT
    return max_turns * 2 + 50


# --------------------------------------------------------------------------- image-aware nodes


def _parse_image_markers(text: str) -> list[str]:
    """Extract image file paths from [IMAGE:path] markers in the message text."""
    return _IMAGE_MARKER_RE.findall(text)


def _text_without_markers(text: str) -> str:
    """Strip [IMAGE:path] markers and return the remaining user text."""
    return _IMAGE_MARKER_RE.sub("", text).strip()


def classify_message(state: GAState) -> dict:
    """Detect [IMAGE:path] markers in the latest HumanMessage and accumulate in pending_images.

    Only looks at the most recent HumanMessage — earlier messages have already been
    processed. Deduplicates by file path so the same image is never stored twice.
    """
    messages = state.get("messages") or []
    if not messages:
        return {}
    last_msg = messages[-1]
    if not isinstance(last_msg, HumanMessage):
        return {}
    content = last_msg.content
    if not isinstance(content, str):
        return {}

    paths = _parse_image_markers(content)
    if not paths:
        return {}

    existing: list[str] = state.get("pending_images") or []
    new_paths = [p for p in paths if p not in existing]
    if new_paths:
        return {"pending_images": existing + new_paths}
    return {}


def _has_text(state: GAState) -> bool:
    """Return True when the latest HumanMessage has real text beyond [IMAGE:] markers."""
    messages = state.get("messages") or []
    if not messages:
        return False
    last_msg = messages[-1]
    if not isinstance(last_msg, HumanMessage):
        return False
    content = last_msg.content
    if not isinstance(content, str):
        return False
    return bool(_text_without_markers(content))


def route_after_classify(state: GAState) -> str:
    """Conditional routing: wait_for_text if images present without text, else process."""
    pending = state.get("pending_images") or []
    if pending and not _has_text(state):
        return "wait"
    return "process"


def wait_for_text(state: GAState) -> dict:
    """Interrupt and wait until the user provides text alongside the accumulated images.

    When the frontend sends Command(update=...) with more images (no text), the node
    re-executes from the top, sees still no text, and interrupts again. When
    Command(resume=text) arrives, the resume value is added as a HumanMessage.
    """
    while True:
        if _has_text(state):
            return {}

        pending = state.get("pending_images") or []
        answer = interrupt({
            "waiting_for_text": True,
            "pending_count": len(pending),
        })

        # Resumed with text (Command(resume=text))
        if isinstance(answer, str) and answer.strip():
            return {"messages": [HumanMessage(content=answer)]}

        # Resumed with update only (Command(update=...) for more images):
        # state is already updated, loop back to check again.


def route_after_wait(state: GAState) -> str:
    """After wait_for_text returns, route to the core agent process node."""
    return "process"


def cleanup_images(state: GAState) -> dict:
    """Strip [IMAGE:path] markers from all HumanMessages and clear pending_images.

    Called after the core agent completes a turn. Removing markers before the state
    is checkpointed prevents old [IMAGE:path] tags from leaking into future turns
    and causing the model to "accumulate" references to past images.
    """
    messages = state.get("messages") or []
    cleaned: list = []
    for msg in messages:
        if isinstance(msg, HumanMessage) and isinstance(msg.content, str):
            new_content = _text_without_markers(msg.content)
            if new_content:
                cleaned.append(HumanMessage(content=new_content, id=msg.id))
        else:
            cleaned.append(msg)
    # rollover_context is a one-shot injection: clear it here so it only appears in
    # the first turn's system prompt (see context.build_system_prompt). output_mode
    # is the same one-shot: proposal mode must never leak into later turns.
    return {"messages": cleaned, "pending_images": [], "rollover_context": None, "output_mode": None}


# --------------------------------------------------------------------------- graph assembly


def _build_core_agent(
    llm: BaseChatModel | None,
    cfg: Config,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """Build the core agent subgraph (model + tools loop) via create_agent."""
    tool_list = build_tool_list(cfg)
    resolved_llm = llm or get_llm(tool_list, bind_tools=False)
    return create_agent(
        resolved_llm,
        tools=tool_list,
        state_schema=GAState,
        middleware=[
            GAPromptMiddleware(cfg),
            GATurnLogicMiddleware(),
            ModelRetryMiddleware(
                max_retries=0,
                retry_on=(Exception,),
                on_failure=format_agent_error,
            ),
        ],
        checkpointer=checkpointer,
        name="gacore_core",
    )


def build_graph(
    llm: BaseChatModel | None = None,
    cfg: Config | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Assemble the full image-aware agent graph.

    Topology::

        START → classify_message ─┬─ (images + text →) ─→ process (core agent)
                                  │                          │
                                  └─ (images, no text →) ─→ wait_for_text
                                                               │
                                          Command(update) ────┘  (more images)
                                          Command(resume) ─────→ process → cleanup_images → END

    The ``process`` node wraps the core create_agent subgraph with the same middleware
    chain (GAPromptMiddleware → GATurnLogicMiddleware → ModelRetryMiddleware).

    Args:
        llm: The chat model (unbound; create_agent binds the tool list itself). When
            None, get_llm() resolves it from the environment.
        cfg: Runtime configuration; defaults to Config.default().
        checkpointer: Persistence backend; defaults to a fresh MemorySaver. Shared
            between the wrapper and the core subgraph.
    """
    resolved_cfg = cfg or Config.default()
    resolved_checkpointer = checkpointer or MemorySaver()

    core_agent = _build_core_agent(llm, resolved_cfg, resolved_checkpointer)

    workflow = StateGraph(GAState)
    workflow.add_node("classify_message", classify_message)
    workflow.add_node("wait_for_text", wait_for_text)
    workflow.add_node("process", core_agent)
    workflow.add_node("cleanup_images", cleanup_images)

    workflow.add_edge(START, "classify_message")
    workflow.add_conditional_edges(
        "classify_message",
        route_after_classify,
        {"wait": "wait_for_text", "process": "process"},
    )
    workflow.add_conditional_edges(
        "wait_for_text",
        route_after_wait,
        {"process": "process"},
    )
    workflow.add_edge("process", "cleanup_images")
    workflow.add_edge("cleanup_images", END)

    return workflow.compile(checkpointer=resolved_checkpointer)


def run_once(
    graph: CompiledStateGraph,
    user_input: str,
    thread_id: str | None = None,
    recursion_limit: int | None = None,
    **extra_state: object,
) -> dict:
    """Run one user turn to completion on a fresh thread and return the final state.

    Builds the initial GAState from the user input plus any extra_state overrides
    (e.g. max_turns for tests), then invokes the compiled graph. Each call uses its own
    thread id so MemorySaver checkpoints never collide between turns.
    """
    state: dict[str, object] = {**new_state(user_input, Config.default()), **extra_state}
    config = {
        "configurable": {"thread_id": thread_id or uuid.uuid4().hex},
        "recursion_limit": recursion_limit or DEFAULT_RECURSION_LIMIT,
    }
    return graph.invoke(state, config)
