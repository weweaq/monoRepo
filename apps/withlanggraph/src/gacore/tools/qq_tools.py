"""QQ proactive push tool for gacore: let the agent push a C2C message to known users.

Wraps ``gacore.langTrack.qq_push.send_c2c`` (the same botpy BotHttp/BotAPI REST path
the CLI script uses — never ``Client``, whose websocket loop would hang a one-shot
call) into a sync ``@tool`` the agent graph can invoke. The async botpy call is
bridged through a dedicated worker thread so it is safe inside a running event loop
(e.g. the asyncio QQ frontend).

Recipients default to every openid recorded in ``data/qq_known_users.json`` (the
people who have privately messaged the bot — for this self-hosted setup that is the
owner). Tests replace the module-level ``_default_send`` with a fake via monkeypatch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Final, Literal, NotRequired, TypedDict

from langchain_core.tools import tool

from gacore.jsonl_logger import get_logger
from gacore.langTrack.qq_push import load_known_users, send_c2c

logger = get_logger("tools.qq_tools")

_PUSH_TIMEOUT_S: Final = 90

# P2 output-side guard (design doc §5.5 "工具侧再校验一次"): QQ private-message
# reading is best under ~200 chars; anything longer is truncated before delivery.
_PUSH_MAX_CHARS: Final = 200

_EXECUTOR: ThreadPoolExecutor | None = None


class QqPushResult(TypedDict):
    """Successful push: how many recipients got it, who, and the platform message ids.

    ``truncated`` (audit L-1) is only set when the over-length message was cut to
    ``_PUSH_MAX_CHARS`` before delivery — a signal the caller can surface to the user
    (e.g. "消息被截断，全文见报告") instead of silently losing the tail.
    """

    status: Literal["sent"]
    ok: int
    to: list[str]
    failures: list[str]
    ids: dict[str, str]
    truncated: NotRequired[bool]


class QqPushError(TypedDict):
    """Failed push: machine-readable tag, human message and the intended recipients."""

    error: str
    message: str
    to: list[str]


def _executor() -> ThreadPoolExecutor:
    """Module-level single worker thread; safe to reuse across tool calls."""
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qq-push")
    return _EXECUTOR


def _run_async(coro_factory: Callable[[], Awaitable[dict]]) -> dict:
    """Run an async botpy call in a worker thread; returns a dict, never raises."""
    def worker() -> dict:
        return asyncio.run(coro_factory())

    future = _executor().submit(worker)
    try:
        return future.result(timeout=_PUSH_TIMEOUT_S)
    except FutureTimeoutError:
        logger.warning("qq_push timed out", timeout=_PUSH_TIMEOUT_S)
        return {"error": "timeout", "message": f"推送超时（>{_PUSH_TIMEOUT_S}s）"}
    except Exception as exc:  # noqa: BLE001 — tools never raise
        logger.error(
            "qq_push bridge failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
        )
        return {"error": "bridge_failed", "message": str(exc)}


def _default_send(content: str, targets: list[str], sandbox: bool) -> dict:
    """Real sender: botpy REST over a worker-thread event loop."""
    return _run_async(lambda: send_c2c(content, targets, sandbox))


@tool
def qq_push(
    message: str,
    to: str | None = None,
) -> QqPushResult | QqPushError:
    """主动推送一条私聊消息给主人（已知 QQ 用户）。

    用于把 langTrack 画像报告、日程/事项提醒等内容主动发给主人。机器人收到过私聊的
    用户 openid 会自动记录在 data/qq_known_users.json（自用场景通常只有主人一个人）。
    - message: 要推送的消息正文。
    - to: 可选，逗号分隔的 openid 列表；通常留空 = 推给全部已知用户（主人）。
    返回结构化结果（status=ok 时含各用户 message id），失败返回 error dict，不抛异常。
    QQ_APP_ID / QQ_APP_SECRET 未配置或 botpy 未安装时返回 error。注意 QQ 平台对主动
    推送有频控/策略限制，不要频繁调用。
    """
    targets = [x.strip() for x in (to or "").split(",") if x.strip()]
    # P2 output-side guard: truncate over-length messages before delivery so the
    # proactive greeting stays ≤200 chars even if the LLM ignores the prompt rule.
    # Audit L-1: the truncation is reported back via ``truncated=True`` on success so
    # callers can tell the user the message was cut instead of failing silently.
    message = (message or "").strip()
    truncated = False
    if len(message) > _PUSH_MAX_CHARS:
        logger.warning(
            "qq_push message truncated",
            before=len(message),
            max_chars=_PUSH_MAX_CHARS,
        )
        message = message[:_PUSH_MAX_CHARS]
        truncated = True
    if not targets:
        users = load_known_users()
        targets = list(users.keys())
    if not targets:
        return QqPushError(
            error="no_recipients",
            message="没有已知 QQ 用户——主人先给机器人发一条私聊即可记录 openid",
            to=[],
        )

    result = _default_send(message, targets, False)
    if not isinstance(result, dict):
        result = {"error": "bad_result", "message": f"发送函数返回异常: {result!r}"}
    if result.get("error"):
        return QqPushError(
            error=str(result["error"]),
            message=str(result.get("message", "")),
            to=targets,
        )

    ok = int(result.get("ok", 0))
    failures = [str(x) for x in (result.get("failures") or [])]
    ids = {str(k): str(v) for k, v in (result.get("ids") or {}).items()}
    if ok == 0:
        errs = result.get("errors") or {}
        detail = "；".join(f"{o}: {errs[o]}" for o in failures) or "unknown"
        return QqPushError(
            error="all_failed",
            message=f"全部 {len(targets)} 个目标推送失败：{detail}",
            to=targets,
        )

    delivered = [o for o in targets if o not in failures]
    logger.info("qq_push sent", ok=ok, failures=failures, truncated=truncated)
    if truncated:
        return QqPushResult(
            status="sent",
            ok=ok,
            to=delivered,
            failures=failures,
            ids=ids,
            truncated=True,
        )
    return QqPushResult(status="sent", ok=ok, to=delivered, failures=failures, ids=ids)
