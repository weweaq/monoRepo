"""GA turn logic as official ``create_agent`` middleware: prompt injection and turn control.

This module replaces the hand-written 2-node StateGraph (gacore.nodes.agent) with the
official ``langchain.agents.create_agent`` middleware system. Two middleware classes
carry what GA's engine called the agent-node + no_tool-final logic:

- ``GAPromptMiddleware`` rebuilds the per-turn system prompt inside ``wrap_model_call``
  (the official channel for customizing the model request; ``ModelRequest.override`` is
  the non-deprecated way to replace the system message).
- ``GATurnLogicMiddleware`` implements the control logic that used to live in the agent
  node and its router: the exit_reason short-circuit and the max_turns guard run in
  ``before_model`` (jumping to END via the ``jump_to`` state channel); empty-response
  retries, done_hooks continuation and task completion run in ``after_model`` (jumping
  back to the model node when another LLM turn is needed).

Middleware control flow uses the official ``hook_config``/``can_jump_to`` mechanism:
a hook decorated with ``@hook_config(can_jump_to=[...])`` gets a conditional graph edge
that reads the ``jump_to`` channel, so returning ``{"jump_to": "end"}`` / ``{"jump_to":
"model"}`` redirects execution without any custom graph wiring.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    Runtime,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from gacore.config import Config
from gacore.context import build_system_prompt
from gacore.jsonl_logger import get_logger
from gacore.state import GAState

logger = get_logger("middleware")

_EMPTY_PROMPT: Final = "[Empty response. Please respond or call a tool.]"
_MAX_EMPTY_RETRIES: Final = 3
_AGENT_ERROR_PREFIX: Final = "[Agent error:"

# --- LLM 调用全量留痕（2026-09-04） ------------------------------------------
# 每次 model call 往 logs/llm_calls/<YYYY-MM-DD>.jsonl 追加一行：
# 完整请求消息（system + history，单条 20k 字符封顶）+ 响应摘要/全文 + token 用量。
# 用途：排查"到底用什么生成了日报/回复"——请求原文可复现。日志 append 失败只 warning，
# 绝不影响主链路。
_LLM_LOG_DIRNAME: Final = "llm_calls"
_LLM_LOG_MSG_CAP: Final = 20_000  # 单条消息在日志里的字符上限（防巨型 tool 结果撑爆行）

# --- Output-side time guard ------------------------------------------------
# Asia/Shanghai (UTC+8): the project's canonical clock — the exact source the
# get_time tool and the [Current time] anchor are built from.
_TZ: Final = timezone(timedelta(hours=8))
_MAX_TIME_GUARD_RETRIES: Final = 2
_TIME_GUARD_PROMPT: Final = (
    "[时间守卫] 你刚才的回复里包含与当前真实时间明显不符的时间断言（{details}）。"
    "这是硬性错误：禁止凭记忆、对话上下文或任何注入文本推算当下。"
    "请立即调用 get_time 工具获取官方时钟，并以该返回值重新作答；"
    "若你本意并不需要报时间，忽略上述提醒、继续正常作答即可。"
)
_CN_DIGITS: Final = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_WEEK_CN2IDX: Final = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
# Negation words inside an assertion fragment mark it as non-assertion ("今天不是星期三",
# "现在没到3点") — such statements are skipped so the guard never trips on a denial.
_NEGATION_RE: Final = re.compile(r"不|没|非")


def _cn2num(s: str) -> int:
    """Convert a Chinese numeral (roughly up to 24) into an int; -1 when unrecognized."""
    if not s:
        return -1
    if s == "十":
        return 10
    if len(s) == 2 and s.startswith("十"):
        return 10 + _CN_DIGITS.get(s[1], -1)
    if len(s) == 2 and s.endswith("十"):
        return 10 * _CN_DIGITS.get(s[0], -1)
    if len(s) == 2:
        return 10 * _CN_DIGITS.get(s[0], 0) + _CN_DIGITS.get(s[1], -1)
    return _CN_DIGITS.get(s, -1)


def check_reply_time_assertions(
    content: str, real_dt: datetime | None = None
) -> list[str]:
    """Detect concrete "now" assertions in a reply that disagree with the real clock.

    Only statements clearly anchored to the present moment are checked: “现在/当前/
    此刻/这会儿” for clock readings and “今天” for weekday / calendar dates.
    Future and relative mentions (“明天早上9点”, “上周四”) are intentionally ignored
    to avoid false positives. The deviation threshold for clock readings is >= 1 hour
    (midnight-wrap aware), so a 12-point reply for an actual 9-point morning is caught
    while a rounding-vs-exact-minute difference is not.

    Returns a list of human-readable violations; an empty list means the reply's time
    assertions are correct (or absent).
    """
    if not content:
        return []
    if real_dt is None:
        real_dt = datetime.now(_TZ)
    problems: list[str] = []
    now_hour, now_min = real_dt.hour, real_dt.minute

    # 1. Clock reading inside a "now" context: 现在是N点 / 此刻N:MM / 这会儿N点半…
    # 12-hour-format aware: a bare 1-12 reading may mean the same hour twice a day,
    # so both candidates (r and r+12) are considered and the smallest minute-gap wins.
    # "现在是1点" at a real 13:30 matches (gap 30min < 1h, no trip) while "现在是9点"
    # at a real 12:30 still trips the guard (gap >= 1h).
    # Half-hour / quarter markers ("3点半", "3点一刻", "3点三刻") and explicit "X点Y分"
    # are folded into the reported minute, so "现在是3点半" against a real 16:00
    # (gap 30min) does NOT trip. Negated statements ("现在不是3点") are not assertions.
    _now_ctx = r"(?:现在|当前|此刻|这会儿)"
    _clock_minutes = now_hour * 60 + now_min
    _frac: Final = {"半": 30, "一刻": 15, "三刻": 45}

    def _clock_gap_minutes(reported: tuple[int, int], clock_minutes: int) -> int:
        hour, minute = reported
        candidates = {hour * 60 + minute}
        if 1 <= hour <= 12:
            candidates.add((hour + 12) * 60 + minute)
        gap = min(abs(c - clock_minutes) for c in candidates)
        return min(gap, 24 * 60 - gap)  # midnight wrap: 23:50 vs 0:00 -> gap 10min

    def _negated(seg: str) -> bool:
        """True when the assertion fragment carries a negation word (not a real assertion)."""
        return bool(_NEGATION_RE.search(seg))

    # a) hh:mm colon form: 现在15:30 / 此刻8点10分 not covered here, see (b)
    for m in re.finditer(
        _now_ctx + r"[^\n，。！？；;\n]{0,12}?(\d{1,2})\s*:\s*(\d{1,2})", content
    ):
        seg = m.group(0)
        if _negated(seg):
            continue
        hour, minute = int(m.group(1)), int(m.group(2))
        if (
            0 <= hour <= 23
            and 0 <= minute <= 59
            and _clock_gap_minutes((hour, minute), _clock_minutes) >= 60
        ):
            problems.append(
                f"写了“现在{m.group(0)[m.start(1):]}”，真实当前 {now_hour}:{now_min:02d}"
            )
    # b) 点/时 form with optional half/quarter/分钟 marker: 现在3点 / 现在3点半 / 现在3点10分
    for m in re.finditer(
        _now_ctx
        + r"[^\n，。！？；;\n]{0,12}?(\d{1,2})\s*[点时]\s*(半|一刻|三刻|(\d{1,2})分)?",
        content,
    ):
        seg = m.group(0)
        if _negated(seg):
            continue
        hour = int(m.group(1))
        if not (0 <= hour <= 23):
            continue
        minute = _frac.get(m.group(2) or "", 0)
        if m.group(3):
            minute = int(m.group(3))
        if _clock_gap_minutes((hour, minute), _clock_minutes) >= 60:
            problems.append(
                f"写了“现在{m.group(0)[m.start(1):]}”，真实当前 {now_hour}:{now_min:02d}"
            )
    # c) Chinese clock reading: 现在三点 / 现在三点半
    for m in re.finditer(
        _now_ctx
        + r"[^\n，。！？；;\n]{0,12}?([一二两三四五六七八九十]+)\s*[点时]\s*(半|一刻|三刻|(\d{1,2})分)?",
        content,
    ):
        seg = m.group(0)
        if _negated(seg):
            continue
        hour = _cn2num(m.group(1))
        minute = _frac.get(m.group(2) or "", 0)
        if m.group(3):
            minute = int(m.group(3))
        if 0 < hour <= 24 and _clock_gap_minutes((hour, minute), _clock_minutes) >= 60:
            problems.append(
                f"写了“现在{m.group(0)[m.start(1):]}”，真实当前 {now_hour}:{now_min:02d}"
            )

    # 2. Weekday anchored to "today": 今天是星期X（否定句不算断言）
    for m in re.finditer(r"今天.{0,4}?星期([一二三四五六日天])", content):
        if _negated(m.group(0)):
            continue
        if _WEEK_CN2IDX[m.group(1)] != real_dt.weekday():
            problems.append(
                f"写了“今天是星期{m.group(1)}”，真实为 星期{('一二三四五六日')[real_dt.weekday()]}"
            )

    # 3. Calendar date anchored to "today": 今天是X月X日 / 今天X月X号（否定句不算断言）
    for m in re.finditer(r"今天.{0,6}?(\d{1,2})\s*[月/]\s*(\d{1,2})\s*[日号]", content):
        if _negated(m.group(0)):
            continue
        if (int(m.group(1)), int(m.group(2))) != (real_dt.month, real_dt.day):
            problems.append(
                f"写了“今天是{m.group(1)}月{m.group(2)}日”，真实为 {real_dt.month}月{real_dt.day}日"
            )

    return problems


def _cap_llm_log_content(content: str) -> str:
    """Cap a single message's logged content; mark the truncation inline."""
    if len(content) <= _LLM_LOG_MSG_CAP:
        return content
    return content[:_LLM_LOG_MSG_CAP] + f"…[truncated {len(content) - _LLM_LOG_MSG_CAP} chars]"


def _llm_log_path(cfg: Config) -> Path:
    return cfg.logs_dir / _LLM_LOG_DIRNAME / f"{datetime.now(_TZ).strftime('%Y-%m-%d')}.jsonl"


def _serialize_llm_messages(request: ModelRequest[None]) -> list[dict[str, Any]]:
    """Serialize the full request message list (system + history) for the call log."""
    out: list[dict[str, Any]] = []
    sys_msg = getattr(request, "system_message", None)
    if sys_msg is not None:
        content = sys_msg.content if isinstance(sys_msg.content, str) else str(sys_msg.content or "")
        out.append({"role": "system", "chars": len(content), "content": _cap_llm_log_content(content)})
    for m in request.messages or []:
        content = m.content if isinstance(m.content, str) else str(m.content or "")
        entry: dict[str, Any] = {"role": getattr(m, "type", "unknown"), "chars": len(content),
                                 "content": _cap_llm_log_content(content)}
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = [
                {"name": tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", ""),
                 "args": _cap_llm_log_content(json.dumps(tc.get("args", {}), ensure_ascii=False, default=str))
                         if isinstance(tc, dict) else ""}
                for tc in tool_calls
            ]
        out.append(entry)
    return out


def _serialize_llm_response(response: ModelResponse[Any] | AIMessage) -> dict[str, Any]:
    """Serialize the model response (content / tool_calls / usage) for the call log.

    ModelResponse.result is a *list* of BaseMessage (usually one AIMessage) — take the
    last message as the model's answer; a bare AIMessage is used as-is.
    """
    msg = getattr(response, "result", None) or response
    if isinstance(msg, list):
        msg = msg[-1] if msg else None
    content = getattr(msg, "content", "") or ""
    if not isinstance(content, str):
        content = str(content or "")
    out: dict[str, Any] = {"chars": len(content), "content": _cap_llm_log_content(content)}
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = [
            tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "") for tc in tool_calls
        ]
    usage = getattr(msg, "usage_metadata", None) or getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        out["usage"] = {k: usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens") if k in usage}
    return out


def _append_llm_call_log(cfg: Config, entry: dict[str, Any]) -> None:
    """Append one call-log line; OSError degrades to a warning, never raises."""
    try:
        path = _llm_log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning("llm call log append failed", error_type=type(exc).__name__, stack_trace=str(exc))


def _build_llm_log_entry(request: ModelRequest[None]) -> dict[str, Any]:
    """Build the request-side half of the call-log entry (response filled by caller)."""
    state = getattr(request, "state", None) or {}
    turn = state.get("current_turn") if isinstance(state, dict) else None
    model_obj = getattr(request, "model", None)
    model_name = getattr(model_obj, "model_name", None) or getattr(model_obj, "model", None) or ""
    if not isinstance(model_name, str):
        model_name = str(model_name)
    user_head = ""
    for m in reversed(list(request.messages or [])):
        if getattr(m, "type", "") == "human":
            c = m.content if isinstance(m.content, str) else str(m.content or "")
            user_head = c[:80]
            break
    return {
        "ts": datetime.now(_TZ).isoformat(timespec="seconds"),
        "model": model_name,
        "turn": turn,
        "user_head": user_head,
        "messages": _serialize_llm_messages(request),
    }


class GAPromptMiddleware(AgentMiddleware[GAState, None, Any]):
    """Rebuild the per-turn system prompt (rules + working checkpoint + hints).

    GA never stores the system prompt in state.messages: it is rebuilt fresh on every
    model call. This middleware applies that behavior at the model-request level, which
    keeps the ``messages`` channel clean (no duplicated leading SystemMessage).

    Note: the full ``state.messages`` is passed to the LLM by create_agent; we do NOT
    fold history into the system prompt — that would cause every message to appear
    twice, triggering duplicate replies from the model.
    """

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg

    def wrap_model_call(
        self, request: ModelRequest[None], handler: Any
    ) -> ModelResponse[Any] | AIMessage:
        """Replace the request's system message with the GA per-turn prompt.

        Also appends one full request/response line to logs/llm_calls/<date>.jsonl —
        the "what did we actually send the LLM" audit trail (2026-09-04). Logging
        failures degrade to warnings and never break the call.
        """
        req = self._inject_prompt(request)
        entry = _build_llm_log_entry(req)
        try:
            response = handler(req)
        except Exception as exc:  # log the failure, then re-raise untouched
            entry["response"] = {"error": f"{type(exc).__name__}: {exc}"}
            _append_llm_call_log(self.cfg, entry)
            raise
        entry["response"] = _serialize_llm_response(response)
        _append_llm_call_log(self.cfg, entry)
        return response

    async def awrap_model_call(
        self, request: ModelRequest[None], handler: Any
    ) -> ModelResponse[Any] | AIMessage:
        """Async twin of wrap_model_call — required when the graph runs via astream()."""
        req = self._inject_prompt(request)
        entry = _build_llm_log_entry(req)
        try:
            response = await handler(req)
        except Exception as exc:  # log the failure, then re-raise untouched
            entry["response"] = {"error": f"{type(exc).__name__}: {exc}"}
            _append_llm_call_log(self.cfg, entry)
            raise
        entry["response"] = _serialize_llm_response(response)
        _append_llm_call_log(self.cfg, entry)
        return response

    def _inject_prompt(self, request: ModelRequest[None]) -> ModelRequest[None]:
        """Build the per-turn system message and return an overridden request.

        Note: fold_history is intentionally not called here. create_agent already
        passes the full state.messages to the LLM; folding them into the system
        prompt as well would cause every message to appear twice, triggering
        duplicate replies from the model.
        """
        state = request.state
        prompt = build_system_prompt(state, self.cfg)
        return request.override(system_message=SystemMessage(content=prompt))


class GATurnLogicMiddleware(AgentMiddleware[GAState, None, Any]):
    """Turn-loop control: short-circuits, max_turns guard, empty retry, done_hooks, completion.

    before_model (can jump to END):
      - state already carries exit_reason -> short-circuit without calling the model
        (resuming an interrupted turn never re-invokes the LLM).
      - current_turn exceeded max_turns -> exit with MAX_TURNS_EXCEEDED.

    after_model (can jump back to MODEL or END):
      - tool calls present -> None (the default route sends them to the tools node).
      - empty content -> retry with a corrective HumanMessage (max 3), else EXITED.
      - pending done_hooks -> fire the next one as a HumanMessage and loop back to the
        model for another turn.
      - an injected agent-error message -> exit with AGENT_ERROR.
      - a real answer -> complete the task with CURRENT_TASK_DONE.
    """

    @hook_config(can_jump_to=["end"])
    def before_model(
        self, state: GAState, runtime: Runtime[None]
    ) -> dict[str, Any] | None:
        """Short-circuit on exit_reason / max_turns before the model is called."""
        if state.get("exit_reason"):
            return {"jump_to": "end"}
        turn = state.get("current_turn", 0) + 1
        if turn > state.get("max_turns", 40):
            return {"jump_to": "end", "exit_reason": "MAX_TURNS_EXCEEDED"}
        return {"current_turn": turn}

    @hook_config(can_jump_to=["model", "end"])
    def after_model(
        self, state: GAState, runtime: Runtime[None]
    ) -> dict[str, Any] | None:
        """Apply the final (no_tool) logic after one model call: retry / hooks / done."""
        messages = state.get("messages") or []
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        response = messages[-1]
        if response.tool_calls:
            return None  # default route: tools node
        done_hooks = state.get("done_hooks") or []
        if response.content.startswith(_AGENT_ERROR_PREFIX):
            return {"exit_reason": "AGENT_ERROR", "retry_count": 0}
        if not response.content:
            if state.get("retry_count", 0) < _MAX_EMPTY_RETRIES:
                return {
                    "jump_to": "model",
                    "messages": [response, HumanMessage(content=_EMPTY_PROMPT)],
                    "retry_count": state.get("retry_count", 0) + 1,
                }
            return {"exit_reason": "EXITED", "retry_count": 0}
        # Output-side time guard: a reply that asserts a concrete "now" wildly at odds
        # with the real clock triggers a corrective retry that forces a get_time call
        # (unless the guard retry budget is exhausted -> exit with TIME_GUARD_EXCEEDED).
        # Compatible with the existing guardrails: it sits alongside the empty-retry
        # logic, uses its own retry counter, and reuses the same jump_to->model channel.
        reply_text = response.content if isinstance(response.content, str) else ""
        guard_hits = check_reply_time_assertions(reply_text)
        if guard_hits:
            if state.get("time_guard_retries", 0) < _MAX_TIME_GUARD_RETRIES:
                return {
                    "jump_to": "model",
                    "messages": [
                        response,
                        HumanMessage(
                            content=_TIME_GUARD_PROMPT.format(details="；".join(guard_hits))
                        ),
                    ],
                    "time_guard_retries": state.get("time_guard_retries", 0) + 1,
                }
            return {"exit_reason": "TIME_GUARD_EXCEEDED", "retry_count": 0}
        if done_hooks:
            return {
                "jump_to": "model",
                "messages": [response, HumanMessage(content=done_hooks[0])],
                "done_hooks": done_hooks[1:],
                "retry_count": 0,
            }
        return {"exit_reason": "CURRENT_TASK_DONE", "retry_count": 0}


def format_agent_error(exc: Exception) -> str:
    """Format a provider failure as the GA agent-error message (for ModelRetryMiddleware)."""
    return f"[Agent error: {exc}]"
