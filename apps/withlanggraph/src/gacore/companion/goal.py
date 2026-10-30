"""One small read-only goal per actor (V2-Step 5, migration v6).

Design: ``docs/companion-self-memory-v2-plan.md`` §4.5 / §5.3 / V2-Step 5.

A goal is the only place where the character may *act* on its own, so the rules are tight:

- at most one ``in_progress`` goal per actor (partial unique index in the store);
- execution may only use the read-only whitelist below — the model cannot widen it;
- every transition that matters writes a real event (``goal.started`` /
  ``read_action.*`` / ``goal.succeeded|failed|cancelled``), so a goal's outcome is
  evidence, not a mutable column;
- a finished goal can never be re-started (terminal states are final in the store), which
  is what makes "restart the process, don't re-run yesterday's goal" true.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Final

from langchain_core.messages import HumanMessage, SystemMessage

from gacore.companion.commitment import parse_due_ms
from gacore.companion.store import (
    CompanionStore,
    CompanionStoreError,
    Goal,
)
from gacore.jsonl_logger import get_logger
from gacore.redact import redact_text

logger = get_logger("companion.goal")

GOAL_PROMPT_VERSION: Final = "companion-goal@v1"
MAX_GOAL_TEXT_CHARS: Final = 120
MAX_DEADLINE_DAYS: Final = 7
MAX_TOOLS: Final = 4

# Only these tools may execute a goal. Everything writable or outward-facing is absent on
# purpose (plan: "禁止 Shell、文件写入、邮件、公开评论、账号操作").
ALLOWED_TOOLS: Final = frozenset(
    {
        "get_time",
        "read_daily",
        "search_daily",
        "last_seen",
        "bili_history",
        "browser_history",
        "web_scan",
        "web_execute_js",
        "ncm_me",
    }
)

_TZ: Final = timezone(timedelta(hours=8))
_DAY_MS: Final = 24 * 60 * 60 * 1000
_JSON_OBJECT: Final = re.compile(r"\{.*\}", re.S)

_SYSTEM_PROMPT: Final = """根据输入（一条承诺或一条有证据的反思）提出最多一个短小、只读、可取消、可验证的目标。

硬规则：
1. 只能使用只读工具：get_time / read_daily / search_daily / last_seen /
   bili_history / browser_history / web_scan / web_execute_js / ncm_me。
2. 不得扩大 allowed_tools；不得创建外发、写文件、付费、账号或不可逆动作。
3. 截止时间必须能在 7 天内。
4. 没有合适目标时返回 {"goal": null}。

只输出 JSON，键名一字不差：
{"goal": null} 或
{"goal": {"text": "一句话目标（≤120 字）", "allowed_tools": ["web_scan"],
          "deadline_text": "原文里的时间词", "success_criteria": "怎样算完成"}}"""

Runner = object


@dataclass(frozen=True, slots=True)
class GoalProposal:
    """A validated goal proposal, ready to be stored as ``proposed``."""

    text: str
    allowed_tools: tuple[str, ...]
    deadline_ms: int
    success_criteria: str


def propose_goal(
    source_text: str,
    *,
    model: object | None = None,
    runner: object | None = None,
    now_ms: int | None = None,
) -> GoalProposal | None:
    """Ask for at most one goal; ``None`` means "nothing worth doing" (or invalid)."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if runner is not None:
        data = runner(source_text)  # type: ignore[operator]
        return validate_proposal(data, now_ms=now)
    try:
        resolved = model if model is not None else _default_model()
        response = resolved.invoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=source_text)]
        )
        raw = getattr(response, "content", response)
        payload = raw if isinstance(raw, str) else str(raw)
    except Exception as exc:  # noqa: BLE001 — proposing a goal must never crash a turn
        logger.warning(
            "companion goal model call failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return None
    match = _JSON_OBJECT.search(payload or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return validate_proposal(data, now_ms=now)


def validate_proposal(
    data: object, *, now_ms: int, max_deadline_days: int = MAX_DEADLINE_DAYS
) -> GoalProposal | None:
    """Validate a proposal payload; anything outside the policy returns ``None``."""
    if not isinstance(data, dict):
        return None
    raw = data.get("goal")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    criteria = raw.get("success_criteria")
    tools = raw.get("allowed_tools")
    deadline_text = raw.get("deadline_text")
    if not isinstance(text, str) or not text.strip():
        return None
    text = redact_text(text.strip())
    if len(text) > MAX_GOAL_TEXT_CHARS:
        return None
    if not isinstance(criteria, str) or not criteria.strip():
        return None
    if not isinstance(deadline_text, str) or not deadline_text.strip():
        return None
    if not isinstance(tools, list) or not tools:
        return None
    if len(tools) > MAX_TOOLS or not all(isinstance(tool, str) for tool in tools):
        return None
    if any(tool not in ALLOWED_TOOLS for tool in tools):
        logger.warning("companion goal rejected a non-whitelisted tool", tools=tools)
        return None
    deadline = parse_due_ms(deadline_text, now_ms)
    if deadline is None:
        return None
    if deadline > now_ms + max_deadline_days * _DAY_MS:
        return None
    return GoalProposal(
        text=text,
        allowed_tools=tuple(dict.fromkeys(tools)),
        deadline_ms=deadline,
        success_criteria=redact_text(criteria.strip()),
    )


# --------------------------------------------------------------------- lifecycle


def create_goal(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    proposal: GoalProposal,
    *,
    commitment_id: str | None = None,
) -> Goal:
    """Store a validated proposal as ``proposed`` (nothing runs until it is approved)."""
    return store.add_goal(
        actor_id=actor_id,
        principal_id=principal_id,
        text=proposal.text,
        allowed_tools=proposal.allowed_tools,
        deadline_ms=proposal.deadline_ms,
        status="proposed",
        commitment_id=commitment_id,
    )


def approve_goal(store: CompanionStore, goal_id: str) -> Goal:
    """Human/owner consent step: ``proposed`` → ``approved``."""
    goal = _require_goal(store, goal_id)
    if goal.status != "proposed":
        raise CompanionStoreError(f"goal {goal_id!r} is {goal.status!r}, not proposed")
    return store.set_goal_status(goal_id, status="approved")


def start_goal(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    goal_id: str,
    *,
    occurred_at_ms: int | None = None,
) -> Goal:
    """``approved`` → ``in_progress``; a second active goal is refused by the store."""
    goal = _require_goal(store, goal_id)
    if goal.status != "approved":
        raise CompanionStoreError(f"goal {goal_id!r} is {goal.status!r}, not approved")
    started = store.set_goal_status(goal_id, status="in_progress")
    _emit(
        store,
        "goal.started",
        actor_id,
        principal_id,
        summary=f"开始目标：{goal.text}",
        idempotency_key=f"goal-started:{goal_id}",
        occurred_at_ms=occurred_at_ms,
    )
    return started


def finish_goal(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    goal_id: str,
    *,
    succeeded: bool,
    detail: str,
    occurred_at_ms: int | None = None,
) -> Goal:
    """Close a running goal and write the real result event that proves the outcome."""
    goal = _require_goal(store, goal_id)
    if goal.status != "in_progress":
        raise CompanionStoreError(f"goal {goal_id!r} is {goal.status!r}, not in progress")
    kind = "read_action.succeeded" if succeeded else "read_action.failed"
    result_event_id = _emit(
        store,
        kind,
        actor_id,
        principal_id,
        summary=detail,
        idempotency_key=f"read-action:{goal_id}:{'ok' if succeeded else 'failed'}",
        occurred_at_ms=occurred_at_ms,
    )
    finished = store.set_goal_status(
        goal_id,
        status="succeeded" if succeeded else "failed",
        result_event_id=result_event_id,
    )
    _emit(
        store,
        "goal.succeeded" if succeeded else "goal.failed",
        actor_id,
        principal_id,
        summary=f"目标{'完成' if succeeded else '失败'}：{goal.text}",
        idempotency_key=f"goal:{'succeeded' if succeeded else 'failed'}:{goal_id}",
        occurred_at_ms=occurred_at_ms,
    )
    return finished


def cancel_goal(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    goal_id: str,
    *,
    reason: str = "cancelled",
    occurred_at_ms: int | None = None,
) -> Goal:
    """Cancel an unfinished goal and record why."""
    goal = _require_goal(store, goal_id)
    cancelled = store.set_goal_status(goal_id, status="cancelled")
    _emit(
        store,
        "goal.cancelled",
        actor_id,
        principal_id,
        summary=f"取消目标：{goal.text}（{reason}）",
        idempotency_key=f"goal-cancelled:{goal_id}",
        occurred_at_ms=occurred_at_ms,
    )
    return cancelled


def goal_view(store: CompanionStore, actor_id: str, principal_id: str) -> list[dict[str, object]]:
    """Human-readable view of one actor's goals (debug/export entry point)."""
    return [
        {
            "goal_id": goal.goal_id,
            "status": goal.status,
            "text": goal.text,
            "allowed_tools": list(goal.allowed_tools),
            "deadline_ms": goal.deadline_ms,
            "result_event_id": goal.result_event_id,
        }
        for goal in store.goals(actor_id, principal_id)
    ]


# ------------------------------------------------------------------------- helpers


def _require_goal(store: CompanionStore, goal_id: str) -> Goal:
    goal = store.get_goal(goal_id)
    if goal is None:
        raise CompanionStoreError(f"unknown goal: {goal_id!r}")
    return goal


def _emit(
    store: CompanionStore,
    kind: str,
    actor_id: str,
    principal_id: str,
    *,
    summary: str,
    idempotency_key: str,
    occurred_at_ms: int | None = None,
) -> str:
    """Append one lifecycle event; the idempotency key makes a retry a no-op."""
    result = store.append_event(
        kind=kind,
        actor_id=actor_id,
        principal_id=principal_id,
        source_id=idempotency_key,
        summary=redact_text(summary),
        idempotency_key=idempotency_key,
        occurred_at_ms=occurred_at_ms,
    )
    return result.event_id


def _default_model() -> object:
    from gacore.llm import get_llm

    return get_llm([], bind_tools=False)
