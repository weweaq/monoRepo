"""LLM request-body logging for gacore: capture the full payload sent to the model.

Every real model call — the main agent graph (incl. scheduled jobs that reuse the same
graph), the QQ trivial-reply branch, and any future get_llm caller — is intercepted at the
model instance level (monkey-patched invoke / ainvoke / stream / astream / bind_tools) and
appended as one JSON line to ``logs/<YYYY-MM-DD>/llm_requests.jsonl``, side-by-side with
the existing ``app.jsonl`` so a failing turn can be replayed.

What is captured per request:
- ts / session / pid / provider / model
- run kind (invoke|ainvoke|stream|astream)
- the full message list (SYSTEM / HUMAN / AI / TOOL payloads, role + content + tool_calls)
- tool definitions (name / description / args schema) captured at bind_tools time
- common request parameters (temperature, max_tokens, top_p, model_kwargs, ...)

What is never captured: API keys and other secret-valued fields — values under keys
matching the jsonl_logger secret set are masked with "***" recursively, and the LLM
object's api_key / base_url are never read or serialized.

Guardrail: messages are serialized defensively; any singular string content longer than
``_MAX_MESSAGE_CHARS`` is truncated so one runaway payload (e.g. huge base64 image data)
cannot balloon the log. Logging is best-effort and must never raise into the model call.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Sequence
from typing import Any, Final

from langchain_core.messages import BaseMessage, ToolMessage

from gacore.config import Config
from gacore.jsonl_logger import _SECRET_KEYS

_LOG_FILENAME: Final = "llm_requests.jsonl"
_LOG_DIR_FORMAT: Final = "%Y-%m-%d"
_MAX_MESSAGE_CHARS: Final = 30000

_SESSION_ID: Final = uuid.uuid4().hex[:8]
_PID: Final = os.getpid()
_WRITE_LOCK: threading.RLock = threading.RLock()


def _mask(obj: Any) -> Any:
    """Recursively replace the value of any secret-named key with ***."""
    if isinstance(obj, dict):
        return {k: (_mask(v) if not (k.lower() in _SECRET_KEYS and isinstance(v, str)) else "***") for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_mask(v) for v in obj)
    return obj


def _truncate(text: str, limit: int = _MAX_MESSAGE_CHARS) -> str:
    """Cap a string at ``limit`` chars, appending a truncation marker when trimmed."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _serialize_content(content: Any) -> Any:
    """Flatten a message content (str or content block list) into JSON-safe form."""
    if isinstance(content, str):
        return _truncate(content)
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(_truncate(block))
                continue
            if isinstance(block, dict):
                # Image blocks may carry huge base64 data — keep type + a digest hint only.
                if str(block.get("type", "")).lower() in {"image", "image_url"}:
                    out.append({"type": block.get("type"), "image_data": "[omitted for log size]",
                                "detail": block.get("detail")})
                else:
                    out.append(_mask(block))
            else:
                out.append(str(block))
        return out
    return _truncate(str(content))


def _serialize_message(msg: BaseMessage) -> dict[str, Any]:
    """Serialize one message: role, content, name, tool_call_id and tool_calls (masked)."""
    payload: dict[str, Any] = {
        "role": getattr(msg, "type", None),
        "content": _serialize_content(msg.content),
    }
    if getattr(msg, "name", None):
        payload["name"] = msg.name
    if isinstance(msg, ToolMessage) and getattr(msg, "tool_call_id", None):
        payload["tool_call_id"] = msg.tool_call_id
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        payload["tool_calls"] = _mask(tool_calls)
    return payload


def _serialize_tools(tools: Sequence[Any] | None) -> list[dict[str, Any]] | None:
    """Serialize BaseTool instances into {name, description, args} (masked args)."""
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for t in tools:
        try:
            args = dict(getattr(t, "args", {}) or {})
        except Exception:  # noqa: BLE001 — never fail serialization on a bad tool
            args = {}
        out.append(
            {
                "name": getattr(t, "name", ""),
                "description": _truncate(str(getattr(t, "description", "") or ""), 2000),
                "args": _mask(args),
            }
        )
    return out


def _extract_messages(input_: Any) -> list[BaseMessage]:
    """Back out the message list from a model-call input (list | single message | None)."""
    if isinstance(input_, Sequence) and not isinstance(input_, (str, bytes, bytearray)):
        return [m for m in input_ if isinstance(m, BaseMessage)]
    if isinstance(input_, BaseMessage) and not isinstance(input_, ToolMessage):
        return [input_]
    return []


def _params_from(llm: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Collect the request-level params without ever touching secret fields."""
    params: dict[str, Any] = {}
    for key in ("temperature", "max_tokens", "max_output_tokens", "top_p", "top_k", "stop"):
        value = kwargs.get(key, getattr(llm, key, None))
        if value is not None:
            params[key] = value if not isinstance(value, list) else [str(v) for v in value]
    model_kwargs = kwargs.get("model_kwargs") or getattr(llm, "model_kwargs", None)
    if model_kwargs:
        params["model_kwargs"] = _mask(dict(model_kwargs))
    return params


def log_llm_request(
    *,
    provider: str,
    model: str | None,
    run_kind: str,
    messages: list[BaseMessage],
    tools: Sequence[Any] | None,
    params: dict[str, Any],
) -> None:
    """Append one request-body record to today's llm_requests.jsonl (best-effort)."""
    try:
        cfg = Config.default()
        log_dir = cfg.logs_dir / time.strftime(_LOG_DIR_FORMAT)
        log_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session": _SESSION_ID,
            "pid": _PID,
            "provider": provider,
            "model": model,
            "run_kind": run_kind,
            "messages": [_serialize_message(m) for m in messages],
            "tools": _serialize_tools(tools),
            "params": params,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _WRITE_LOCK:
            with open(log_dir / _LOG_FILENAME, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — logging must never break the model call
        return


def _extract_tool_list(bound_result: Any) -> list[Any] | None:
    """Best-effort: pull the bound tool list off a RunnableBinding if it exposes one."""
    try:
        tools = bound_result.tools if hasattr(bound_result, "tools") else None
        return list(tools) if tools else None
    except Exception:  # noqa: BLE001
        return None


def _patch_instance(obj: Any, name: str, fn: Any) -> None:
    """Attach a function onto a model instance, bypassing pydantic v2 field validation.

    Modern ChatOpenAI / FakeMessagesListChatModel are pydantic v2 ``BaseModel``
    subclasses whose ``__setattr__`` rejects any key that is not a declared field
    (raises ``ValueError: "... object has no field ..."``). Attribute lookup for the
    patched name then resolves through the instance ``__dict__`` normally (no data
    descriptor on the class), so the original call sites keep working unchanged.
    """
    object.__setattr__(obj, name, fn)


def install_llm_logging(llm: Any, provider: str) -> Any:
    """Monkey-patch a chat-model instance so every call logs its full request body.

    Patches invoke/ainvoke/stream/astream (capturing messages + params) and bind_tools
    (capturing the tool definitions). Returns the same instance unchanged so callers can
    chain .bind()/.bind_tools() as usual. Patching is idempotent per instance.
    """
    if getattr(llm, "_gacore_llm_log_installed", False):
        return llm

    original_invoke = llm.invoke
    original_ainvoke = llm.ainvoke
    original_stream = getattr(llm, "stream", None)
    original_astream = getattr(llm, "astream", None)
    original_bind_tools = llm.bind_tools

    def _capture(run_kind: str, messages: list[BaseMessage], kwargs: dict[str, Any]) -> None:
        log_llm_request(
            provider=provider,
            model=getattr(llm, "model_name", None) or getattr(llm, "model", None),
            run_kind=run_kind,
            messages=messages,
            tools=getattr(llm, "_gacore_bound_tools", None),
            params=_params_from(llm, kwargs),
        )

    def _invoke(input_: Any, *args: Any, **kwargs: Any) -> Any:
        _capture("invoke", _extract_messages(input_), kwargs)
        return original_invoke(input_, *args, **kwargs)

    def _ainvoke(input_: Any, *args: Any, **kwargs: Any) -> Any:
        _capture("ainvoke", _extract_messages(input_), kwargs)
        return original_ainvoke(input_, *args, **kwargs)

    async def _astream(*args: Any, **kwargs: Any) -> Any:
        messages = _extract_messages(kwargs.get("input", kwargs.get("messages", args[0] if args else None)))
        _capture("astream", messages, kwargs)
        async for chunk in original_astream(*args, **kwargs):
            yield chunk

    def _stream(*args: Any, **kwargs: Any) -> Any:
        messages = _extract_messages(kwargs.get("input", kwargs.get("messages", args[0] if args else None)))
        _capture("stream", messages, kwargs)
        yield from original_stream(*args, **kwargs)

    def _bind_tools(tools: Any, *args: Any, **kwargs: Any) -> Any:
        bound = original_bind_tools(tools, *args, **kwargs)
        captured = getattr(llm, "_gacore_bound_tools", None) or []
        bound_tools = _extract_tool_list(bound) or (list(tools) if tools else [])
        # Prefer the binding's own exposure; fall back to the raw list passed in.
        _patch_instance(llm, "_gacore_bound_tools", list(bound_tools) or captured)
        return bound

    _patch_instance(llm, "_gacore_llm_log_installed", True)
    _patch_instance(llm, "invoke", _invoke)
    _patch_instance(llm, "ainvoke", _ainvoke)
    if original_astream is not None and llm.astream is not None:
        _patch_instance(llm, "astream", _astream)
    if original_stream is not None and llm.stream is not None:
        _patch_instance(llm, "stream", _stream)
    _patch_instance(llm, "bind_tools", _bind_tools)
    return llm


__all__ = ("install_llm_logging", "log_llm_request")
