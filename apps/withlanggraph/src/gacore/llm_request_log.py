"""LLM request+response body logging for gacore: capture the full call unit.

Every real model call — the main agent graph (incl. scheduled jobs that reuse the same
graph), the QQ trivial-reply branch, and any future get_llm caller — is intercepted at
the model instance level (monkey-patched invoke / ainvoke / stream / astream / bind_tools).
One JSON line per call = the complete unit for ops triage: the full request (messages,
tool definitions, params) PLUS the response (content, tool_calls, token usage), the
duration, and — when the call fails — the error. Lines are written after the call
completes so response and duration land in the same record.

What is captured per request:
- ts / session / pid / provider / model
  (session is the SAME per-process id as app.jsonl's — see jsonl_logger.session_id —
  so a request line joins the system-side lines of its process exactly)
- run kind (invoke|ainvoke|stream|astream)
- the full message list (SYSTEM / HUMAN / AI / TOOL payloads, role + content + tool_calls);
  non-message inputs (plain string prompts from judge/structured calls) are wrapped as a
  single user message instead of being dropped
- tool definitions (name / description / args schema) captured at bind_tools time
- common request parameters (temperature, max_tokens, top_p, model_kwargs, ...)

What is captured per response:
- content (truncated like request messages) and content_chars
- tool_calls issued by the model (masked)
- usage_metadata (input/output/total tokens) and finish_reason when the provider exposes them
- on failure: {"error": "Type: message"} and the original exception is re-raised unchanged
- on streaming: aggregated text + tool_call_chunks + usage, with "interrupted": true when
  the consumer closed the generator early

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
from collections.abc import Sequence
from typing import Any, Final

from langchain_core.messages import BaseMessage, ToolMessage

from gacore.config import Config
from gacore.jsonl_logger import _SECRET_KEYS, session_id

_LOG_FILENAME: Final = "llm_requests.jsonl"
_LOG_DIR_FORMAT: Final = "%Y-%m-%d"
_MAX_MESSAGE_CHARS: Final = 30000

# Same per-process session id as app.jsonl (jsonl_logger is the single source), so
# request lines join system-side lines of the same process exactly — no pid guessing.
_SESSION_ID: Final = session_id()
_PID: Final = os.getpid()

_WRITE_LOCK = threading.RLock()


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


def _content_text(content: Any) -> str:
    """Flatten message content (str or block list) to one text blob (for lengths/joins)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("type") or ""))
        return "".join(parts)
    return str(content or "")


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


def _messages_to_log(input_: Any) -> list[BaseMessage | dict[str, Any]]:
    """Messages for the record; wrap non-message inputs (judge/structured string prompts).

    The memory judge and structured-output calls invoke the model with a plain string,
    not a BaseMessage list — previously those lines recorded zero messages, which read
    as "empty call" in ops views. Wrap them verbatim as a single user message.
    """
    msgs = _extract_messages(input_)
    if msgs or input_ is None:
        return list(msgs)
    if isinstance(input_, str):
        return [{"role": "user", "content": _truncate(input_)}]
    try:
        wrapped = json.dumps(input_, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 — best-effort capture of exotic inputs
        wrapped = str(input_)
    return [{"role": "user", "content": _truncate(wrapped)}]


def _params_from(llm: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Collect the request-level params without ever touching secret fields."""
    params: dict[str, Any] = {}
    for key in ("temperature", "max_tokens", "max_output_tokens", "top_p", "top_k", "stop"):
        if key in kwargs and kwargs[key] is not None:
            params[key] = kwargs[key]
    for key in ("temperature", "max_tokens", "top_p", "top_k"):
        val = getattr(llm, key, None)
        if val is not None and key not in params:
            params[key] = val
    return params


def _serialize_response(result: Any) -> dict[str, Any]:
    """Best-effort response capture: content / tool_calls / usage / finish_reason."""
    try:
        if isinstance(result, BaseMessage):
            resp: dict[str, Any] = {
                "content": _serialize_content(result.content),
                "content_chars": len(_content_text(result.content)),
            }
            tool_calls = getattr(result, "tool_calls", None) or []
            if tool_calls:
                resp["tool_calls"] = _mask(tool_calls)
            usage = getattr(result, "usage_metadata", None)
            if usage:
                resp["usage"] = _mask(dict(usage))
            meta = getattr(result, "response_metadata", None)
            finish = meta.get("finish_reason") if isinstance(meta, dict) else None
            if finish:
                resp["finish_reason"] = finish
            return resp
        return {"kind": type(result).__name__, "text": _truncate(str(result))}
    except Exception:  # noqa: BLE001 — never fail logging on an exotic response
        return {"kind": type(result).__name__}


def _error_response(e: BaseException) -> dict[str, Any]:
    return {"error": _truncate(f"{type(e).__name__}: {e}", 500)}


class _StreamAgg:
    """Aggregate streamed chunks into one response record (text / tool_calls / usage)."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._tool_chunks: dict[int, dict[str, str]] = {}
        self._usage: dict[str, Any] | None = None
        self.chunks = 0

    def add(self, chunk: Any) -> None:
        self.chunks += 1
        self._parts.append(_content_text(getattr(chunk, "content", "")))
        usage = getattr(chunk, "usage_metadata", None)
        if usage:
            self._usage = dict(usage)
        for tcc in getattr(chunk, "tool_call_chunks", None) or []:
            if isinstance(tcc, dict):
                idx = int(tcc.get("index") or 0)
                slot = self._tool_chunks.setdefault(idx, {"name": "", "args": "", "id": ""})
                slot["name"] = slot["name"] or str(tcc.get("name") or "")
                slot["id"] = slot["id"] or str(tcc.get("id") or "")
                slot["args"] += str(tcc.get("args") or "")

    def response(self, *, error: str | None = None, interrupted: bool = False) -> dict[str, Any]:
        resp: dict[str, Any] = {
            "aggregated": True,
            "chunks": self.chunks,
            "content": _truncate("".join(self._parts)),
            "content_chars": len("".join(self._parts)),
        }
        if self._tool_chunks:
            resp["tool_calls"] = _mask([v for _, v in sorted(self._tool_chunks.items())])
        if self._usage:
            resp["usage"] = _mask(self._usage)
        if error:
            resp["error"] = error
        if interrupted:
            resp["interrupted"] = True
        return resp


def log_llm_request(
    *,
    provider: str,
    model: str | None,
    run_kind: str,
    messages: Sequence[Any],
    tools: Sequence[Any] | None,
    params: dict[str, Any],
    response: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append one complete call record (request + response + duration) to today's
    llm_requests.jsonl. Written after the call completes so the response and duration
    land in the same line; best-effort — logging must never break the model call."""
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
            "messages": [
                m if isinstance(m, dict) else _serialize_message(m) for m in messages
            ],
            "tools": _serialize_tools(tools),
            "params": params,
        }
        if duration_ms is not None:
            record["duration_ms"] = duration_ms
        if response is not None:
            record["response"] = response
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
    """Monkey-patch a chat-model instance so every call logs request + response + duration.

    Patches invoke/ainvoke/stream/astream (the record is written after the call completes
    so the response and duration join the same line; a failed call records
    ``response.error`` and re-raises unchanged) and bind_tools (capturing the tool
    definitions). Returns the same instance unchanged so callers can chain
    .bind()/.bind_tools() as usual. Patching is idempotent per instance.
    """
    if getattr(llm, "_gacore_llm_log_installed", False):
        return llm

    original_invoke = llm.invoke
    original_ainvoke = llm.ainvoke
    original_stream = getattr(llm, "stream", None)
    original_astream = getattr(llm, "astream", None)
    original_bind_tools = llm.bind_tools

    def _capture(
        run_kind: str,
        messages: Sequence[Any],
        kwargs: dict[str, Any],
        response: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        log_llm_request(
            provider=provider,
            model=getattr(llm, "model_name", None) or getattr(llm, "model", None),
            run_kind=run_kind,
            messages=messages,
            tools=getattr(llm, "_gacore_bound_tools", None),
            params=_params_from(llm, kwargs),
            response=response,
            duration_ms=duration_ms,
        )

    def _invoke(input_: Any, *args: Any, **kwargs: Any) -> Any:
        msgs = _messages_to_log(input_)
        t0 = time.monotonic()
        try:
            result = original_invoke(input_, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 — log then re-raise unchanged
            _capture("invoke", msgs, kwargs, response=_error_response(e), duration_ms=_ms(t0))
            raise
        _capture("invoke", msgs, kwargs, response=_serialize_response(result), duration_ms=_ms(t0))
        return result

    async def _ainvoke(input_: Any, *args: Any, **kwargs: Any) -> Any:
        msgs = _messages_to_log(input_)
        t0 = time.monotonic()
        try:
            result = await original_ainvoke(input_, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 — log then re-raise unchanged
            _capture("ainvoke", msgs, kwargs, response=_error_response(e), duration_ms=_ms(t0))
            raise
        _capture("ainvoke", msgs, kwargs, response=_serialize_response(result), duration_ms=_ms(t0))
        return result

    async def _astream(*args: Any, **kwargs: Any) -> Any:
        msgs = _messages_to_log(kwargs.get("input", kwargs.get("messages", args[0] if args else None)))
        t0 = time.monotonic()
        agg = _StreamAgg()
        error: BaseException | None = None
        completed = False
        try:
            async for chunk in original_astream(*args, **kwargs):
                agg.add(chunk)
                yield chunk
            completed = True
        except Exception as e:  # noqa: BLE001 — log then re-raise unchanged
            error = e
            raise
        finally:
            _capture(
                "astream", msgs, kwargs,
                response=agg.response(error=_error_response(error)["error"] if error else None,
                                      interrupted=not completed),
                duration_ms=_ms(t0),
            )

    def _stream(*args: Any, **kwargs: Any) -> Any:
        msgs = _messages_to_log(kwargs.get("input", kwargs.get("messages", args[0] if args else None)))
        t0 = time.monotonic()
        agg = _StreamAgg()
        error: BaseException | None = None
        completed = False
        try:
            for chunk in original_stream(*args, **kwargs):
                agg.add(chunk)
                yield chunk
            completed = True
        except Exception as e:  # noqa: BLE001 — log then re-raise unchanged
            error = e
            raise
        finally:
            _capture(
                "stream", msgs, kwargs,
                response=agg.response(error=_error_response(error)["error"] if error else None,
                                      interrupted=not completed),
                duration_ms=_ms(t0),
            )

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


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


__all__ = ("install_llm_logging", "log_llm_request")
