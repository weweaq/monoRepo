"""Interactive REPL for gacore: a thin human-in-the-loop frontend over the compiled graph.

Port of GA's frontends/tui_v3.py. run_repl is the testable core: it drives one
conversation turn at a time on a single thread (so MemorySaver keeps conversation
history across turns), streaming every node update to stdout so the user watches the
agent call tools and then give the final answer. ask_user interrupts surface either as
a streamed ``__interrupt__`` chunk or a raised GraphInterrupt; the REPL prompts the
human once, resumes with a ``Command(resume=...)``, and reports the final
``exit_reason``. Input is injected via ``input_func`` so tests never touch real stdin
or the network.

Run with ``python -m gacore`` (with PYTHONPATH=src or after installing the package).
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable, Iterator
from typing import Final

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langgraph.errors import GraphInterrupt
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from prompt_toolkit import PromptSession

from gacore.config import Config, load_dotenv
from gacore.graph import DEFAULT_RECURSION_LIMIT, build_graph
from gacore.jsonl_logger import get_logger
from gacore.state import new_state

__version__: Final = "0.1.0"

logger = get_logger("cli")

_COMMANDS: Final[dict[str, str]] = {
    "/help": "show this help",
    "/working": "show the current working checkpoint",
    "/memory": "show long-term memory files",
    "/reset": "start a fresh conversation (clears history)",
    "/quit": "exit gacore",
}

# Created lazily so importing gacore.cli works in non-console environments (tests, CI).
# Falls back to builtin input() when no real console is available (e.g. PyCharm Run,
# piping, CI without a TTY) where prompt_toolkit would raise NoConsoleScreenBufferError.
_session: PromptSession | None = None
_use_fallback: bool = False


def _default_input(prompt: str) -> str:
    """Read one line: prompt_toolkit if a console is available, else builtin input()."""
    global _session, _use_fallback
    if _use_fallback:
        return input(prompt)
    if _session is None:
        try:
            _session = PromptSession()
        except Exception:  # noqa: BLE001 — no console / no TTY
            _use_fallback = True
            return input(prompt)
    try:
        return _session.prompt(prompt)
    except Exception:  # noqa: BLE001 — console disappeared mid-session
        _use_fallback = True
        return input(prompt)


def _interrupt_payload(chunk: dict) -> dict | None:
    """Extract the first interrupt's value dict from a streamed chunk, or None.

    Works on both langgraph surfaces: a chunk carrying ``__interrupt__`` and any
    dict-shaped payload. ask_user interrupts carry ``{"question": ..., "options": ...}``.
    """
    interrupts = chunk.get("__interrupt__")
    if not isinstance(interrupts, (list, tuple)) or not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    return value if isinstance(value, dict) else None


def _stream(graph: CompiledStateGraph, state: object, config: dict) -> Iterator[dict]:
    """Stream node updates, normalizing a raised GraphInterrupt into an interrupt chunk."""
    try:
        yield from graph.stream(state, config, stream_mode="updates")
    except GraphInterrupt as exc:
        raw = exc.args[0] if exc.args else ()
        interrupts = tuple(raw) if isinstance(raw, (tuple, list)) else (raw,)
        yield {"__interrupt__": interrupts}


def _format_args(args: dict) -> str:
    """Render tool-call args compactly for the streaming header line."""
    parts: list[str] = []
    for key, value in args.items():
        text = str(value)
        if len(text) > 40:
            text = text[:37] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def _render_tool_result(message: ToolMessage) -> None:
    """Log one tool result line (process info), keeping ask_user's answer compact."""
    if message.name == "ask_user":
        try:
            payload = json.loads(message.content)
            logger.info(f"[tools] <- ask_user -> answer: {payload.get('answer')!r}")
            return
        except (TypeError, ValueError):
            pass
    content = str(message.content)
    if len(content) > 200:
        content = content[:197] + "..."
    logger.info(f"[tools] <- {content}")


def _render_update(node_name: str, update: object, printed_ids: set[str]) -> bool:
    """Render one streamed node update; skip messages already rendered (by id).

    The wrapper graph nests the compiled create_agent subgraph as the ``process`` node,
    and ``cleanup_images`` returns the full message list, so ``stream_mode="updates"``
    emits the same messages in multiple chunks (and previous turns' messages inside
    later turns' full-state chunks). Tracking rendered message ids makes the renderer
    idempotent: each message is shown exactly once. Process lines (tool calls/results)
    go through the structured logger; only the final AI reply is printed to the user.
    Returns True when it printed the final AI reply.
    """
    if not isinstance(update, dict):
        return False  # e.g. {'agent': None} short-circuit
    messages = update.get("messages")
    if not isinstance(messages, list):
        return False
    printed_reply = False
    for message in messages:
        mid = getattr(message, "id", None)
        if mid is not None and mid in printed_ids:
            continue  # already rendered by an earlier chunk (or an earlier turn)
        if isinstance(message, AIMessage):
            if message.tool_calls:
                for call in message.tool_calls:
                    name = call.get("name", "?")
                    if name == "ask_user":
                        # The interactive [ask_user] prompt follows; no need to echo args.
                        logger.info("[agent] -> ask_user")
                    else:
                        logger.info(f"[agent] -> {name}({_format_args(call.get('args') or {})})")
            elif message.content:
                print(str(message.content))
                printed_reply = True
        elif isinstance(message, ToolMessage):
            _render_tool_result(message)
        if mid is not None:
            printed_ids.add(mid)
    return printed_reply


def _update_exit_reason(update: object) -> str | None:
    """Read exit_reason from a node update dict, if present."""
    if isinstance(update, dict):
        reason = update.get("exit_reason")
        if isinstance(reason, str):
            return reason
    return None


def _run_turn(
    graph: CompiledStateGraph,
    cfg: Config,
    user_input: str,
    config: dict,
    input_func: Callable[[str], str],
    printed_ids: set[str],
) -> str | None:
    """Run one user turn to completion, streaming node updates and resolving interrupts."""
    state: object = new_state(user_input, cfg)
    exit_reason: str | None = None
    printed_reply = False
    while True:
        interrupted = False
        for chunk in _stream(graph, state, config):
            if "__interrupt__" in chunk:
                payload = _interrupt_payload(chunk)
                if payload is None:
                    break
                question = str(payload.get("question") or "?")
                options = payload.get("options")
                if isinstance(options, list) and options:
                    print(f"[ask_user] {question} (options: {', '.join(str(o) for o in options)})")
                else:
                    print(f"[ask_user] {question}")
                try:
                    answer = input_func("Your answer: ")
                except EOFError:
                    answer = "abort"
                state = Command(resume=answer)
                interrupted = True
                break
            for node_name, update in chunk.items():
                if _render_update(node_name, update, printed_ids):
                    printed_reply = True
                reason = _update_exit_reason(update)
                if reason is not None:
                    exit_reason = reason
        if not interrupted:
            break
    if not printed_reply and exit_reason:
        logger.info(f"[{exit_reason}]")
    return exit_reason


def _print_memory(cfg: Config) -> None:
    """Print the long-term memory files under cfg.memory_dir, newest first."""
    if not cfg.memory_dir.is_dir():
        print("[memory] (no memory files yet)")
        return
    files = sorted(cfg.memory_dir.glob("*.txt"), reverse=True)
    if not files:
        print("[memory] (no memory files yet)")
        return
    for path in files:
        print(f"== {path.name} ==")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"  (unreadable: {e})")
            continue
        print(text if text else "  (empty)")


def _handle_command(command: str, graph: CompiledStateGraph, cfg: Config, config: dict) -> bool:
    """Handle a slash command; return True when the REPL should exit."""
    if command == "/quit":
        return True
    if command == "/help":
        for name, desc in _COMMANDS.items():
            print(f"  {name:<9} {desc}")
        return False
    if command == "/working":
        snapshot = graph.get_state(config)
        values = snapshot.values or {}
        working = values.get("working")
        if working:
            print(f"[working] {working}")
        else:
            print("[working] (none)")
        return False
    if command == "/memory":
        _print_memory(cfg)
        return False
    if command == "/reset":
        config["configurable"]["thread_id"] = uuid.uuid4().hex
        print("Conversation reset.")
        return False
    print(f"Unknown command: {command} (try /help)")
    return False


def run_repl(
    cfg: Config | None = None,
    llm: Runnable | None = None,
    input_func: Callable[[str], str] | None = None,
) -> str | None:
    """Run the interactive REPL over the compiled graph and return the final exit_reason.

    Args:
        cfg: Runtime configuration; defaults to Config.default().
        llm: Chat model bound to the tool list. When None, build_graph resolves it from
            the environment (get_llm may raise when no provider/API key is configured —
            tests pass a fake).
        input_func: Line reader taking a prompt and returning the user's input; defaults
            to a prompt_toolkit PromptSession. Raise EOFError to simulate Ctrl-D.

    Returns:
        The final ``exit_reason`` ("CURRENT_TASK_DONE", "EXITED", ...) or None when the
        user quit with /quit before any turn completed.
    """
    resolved_cfg = cfg or Config.default()
    graph = build_graph(llm=llm, cfg=resolved_cfg)
    png = graph.get_graph().draw_png()
    with open("agent_graph.png", "wb") as f:
        f.write(png)
    print("图已保存到 agent_graph.png，可用图片查看器打开")
    input_source = input_func or _default_input
    # One thread per REPL session: MemorySaver keeps conversation history across turns.
    thread_id = uuid.uuid4().hex
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
    }
    # Rendered message ids for the whole session: full-state stream chunks repeat
    # messages (including previous turns'), so dedupe by id keeps each on screen once.
    printed_ids: set[str] = set()
    print(f"gacore v{__version__} - interactive agent")
    print("Type /help for commands, /quit to exit")
    last_reason: str | None = None
    try:
        while True:
            try:
                line = input_source("> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                print("\nInterrupted. Goodbye.")
                break
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("/"):
                if _handle_command(stripped.lower(), graph, resolved_cfg, config):
                    break
                continue
            last_reason = _run_turn(graph, resolved_cfg, stripped, config, input_source, printed_ids)
            if last_reason == "EXITED":
                break
    except KeyboardInterrupt:
        print("\nInterrupted. Goodbye.")
    return last_reason


def main() -> None:
    """CLI entry point: load .env, build config + graph, run the REPL, exit 0/1."""
    load_dotenv()
    try:
        run_repl(cfg=Config.default())
    except Exception as e:  # noqa: BLE001 - CLI boundary: report any failure and exit 1
        logger.error(
            "REPL failed to start",
            error_type=type(e).__name__,
            stack_trace=str(e),
            context={},
        )
        print(f"gacore: {type(e).__name__}: {e}")
        sys.exit(1)


__all__ = ("main", "run_repl")
