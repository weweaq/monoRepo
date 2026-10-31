"""Watches: "the owner said they finish work at six — ask them at six" (V3).

A watch is a `companion_intent` with `kind='watch'`: no tools, no action, just a trigger.
The model only classifies the sentence (is this a timed fact worth remembering?); the clock
is parsed by code, exactly like commitments parse their due dates.
"""

from __future__ import annotations

import json
import re
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from langchain_core.messages import HumanMessage, SystemMessage

from gacore.companion import intent as intent_api
from gacore.companion.store import CompanionStore, Intent
from gacore.jsonl_logger import get_logger
from gacore.redact import redact_text

logger = get_logger("companion.follow_up")

WATCH_PROMPT_VERSION: Final = "companion-watch@v1"
MIN_CONFIDENCE: Final = 0.6
MAX_AHEAD_MS: Final = 12 * 60 * 60 * 1000
WATCH_TTL_MS: Final = 3 * 60 * 60 * 1000
MAX_WATCH_TITLE: Final = 60
_TZ: Final = timezone(timedelta(hours=8))
_DAY_MS: Final = 24 * 60 * 60 * 1000

# Cheap gate: a watch needs a clock expression AND a life event the owner describes.
CLOCK_MARKERS: Final = (
    "点",
    "：",
    ":",
    "半",
)
LIFE_WORDS: Final = (
    "下班",
    "上班",
    "加班",
    "开会",
    "吃饭",
    "睡觉",
    "回家",
    "到家",
    "出门",
    "上课",
    "健身",
    "出差",
    "见客户",
    "面试",
    "出发",
)
EVENING_HINTS: Final = ("下班", "晚上", "加班", "到家", "吃饭", "回来", "傍晚")

_JSON_OBJECT: Final = re.compile(r"\{.*\}", re.S)
_CLOCK: Final = re.compile(
    r"(?P<hour>\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*[点时:：]\s*(?P<minute>\d{1,2}|半)?"
)
_CN_DIGITS: Final = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_SYSTEM_PROMPT: Final = """判断主人的这句话里，是否有一个"到了某个时间点值得问一句"的日程。

值得记（watch）：
- 主人说自己几点下班 / 到家 / 开会 / 吃饭 / 出门 / 上班；
- 时间明确，且是今天或明天的某个钟点。

不值得记：
- 角色自己的承诺（那是另一类）；
- 没有具体时间的"我最近很忙"；
- 已经发生的事（"我六点就下班了"）。

只输出 JSON，键名一字不差：
{"is_watch": true, "topic": "下班", "time_text": "六点", "confidence": 0.9}

规则：topic 是 2–10 字的动作；time_text 必须原样摘出原文里的时间词，不要自己换算；
confidence 是"这确实是一个到点值得问的日程"的把握，0 到 1。"""

Runner = Callable[[str], "WatchOutcome"]


@dataclass(frozen=True, slots=True)
class WatchOutcome:
    ok: bool
    topic: str = ""
    time_text: str = ""
    confidence: float = 0.0
    error: str | None = None


def looks_like_watch(text: str) -> bool:
    """Deterministic prefilter: a clock expression plus a life event."""
    body = text or ""
    if not any(marker in body for marker in CLOCK_MARKERS):
        return False
    return any(word in body for word in LIFE_WORDS)


def parse_clock_ms(
    text: str, now_ms: int, *, evening_hint: bool = False
) -> int | None:
    """Parse "六点 / 6点 / 18:00 / 下午三点 / 六点半" into the next matching instant."""
    match = _CLOCK.search(text or "")
    if not match:
        return None
    hour = _number(match.group("hour"))
    if hour is None or not 0 <= hour <= 24:
        return None
    raw_minute = match.group("minute")
    if raw_minute is None:
        minute = 0
    elif raw_minute == "半":
        minute = 30
    else:
        minute = int(raw_minute) if raw_minute.isdigit() else _number(raw_minute)
        if minute is None or not 0 <= minute <= 59:
            return None

    body = text or ""
    if any(word in body for word in ("下午", "傍晚", "晚上", "夜里")) and hour < 12:
        hour += 12
    elif any(word in body for word in ("上午", "早上", "凌晨")) and hour == 12:
        hour = 0
    elif hour <= 11 and evening_hint:
        hour += 12
    if hour == 24:
        hour = 0

    local = datetime.fromtimestamp(now_ms / 1000, tz=_TZ)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    return int(candidate.timestamp() * 1000)


def extract_watch(
    text: str, *, model: object | None = None, runner: Runner | None = None
) -> WatchOutcome:
    """Ask the classifier whether this sentence holds a timed watch (no clock math)."""
    if not looks_like_watch(text):
        return WatchOutcome(ok=False, error="no watch marker")
    if runner is not None:
        return runner(text)
    try:
        resolved = model if model is not None else _default_model()
        response = resolved.invoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=text)]
        )
        raw = getattr(response, "content", response)
        payload = raw if isinstance(raw, str) else str(raw)
    except Exception as exc:  # noqa: BLE001 — a watch scan never breaks the caller
        logger.warning(
            "companion watch model call failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return WatchOutcome(ok=False, error=f"model failed: {type(exc).__name__}")
    match = _JSON_OBJECT.search(payload or "")
    if not match:
        return WatchOutcome(ok=False, error="response is not JSON")
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return WatchOutcome(ok=False, error="response JSON is malformed")
    return _validate(data)


def record_watch(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    event_id: str,
    summary: str,
    occurred_at_ms: int,
    model: object | None = None,
    runner: Runner | None = None,
    now_ms: int | None = None,
) -> Intent | None:
    """Create a watch card from one owner message; never raises into the chat path."""
    if not looks_like_watch(summary):
        return None
    try:
        outcome = extract_watch(summary, model=model, runner=runner)
    except Exception:  # noqa: BLE001
        logger.warning("companion watch extraction crashed", stack_trace=traceback.format_exc())
        return None
    if not outcome.ok:
        return None
    if outcome.confidence < MIN_CONFIDENCE:
        return None
    trigger = parse_clock_ms(
        outcome.time_text or summary,
        occurred_at_ms,
        evening_hint=any(word in summary for word in EVENING_HINTS),
    )
    if trigger is None:
        return None
    if not 0 < trigger - occurred_at_ms <= MAX_AHEAD_MS:
        return None
    title = f"{outcome.topic or '日程'}"
    try:
        card = intent_api.create_watch(
            store, actor_id, principal_id, title=title[:MAX_WATCH_TITLE], trigger_at_ms=trigger,
            expires_at_ms=trigger + WATCH_TTL_MS, source_event_id=event_id,
            question_hint=redact_text(summary)[:120],
        )
    except ValueError as exc:
        logger.warning("companion watch rejected", error_type=type(exc).__name__)
        return None
    logger.info(
        "companion watch recorded",
        actor_id=actor_id,
        topic=title,
        trigger_at_ms=trigger,
    )
    return card


# ------------------------------------------------------------------ background scan


_EXECUTOR: ThreadPoolExecutor | None = None
_LOCK = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="companion-watch")
        return _EXECUTOR


def schedule_watch_extraction(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    *,
    event_id: str,
    summary: str,
    occurred_at_ms: int,
) -> None:
    """Best-effort background scan; the chat path never waits for the model."""
    if not looks_like_watch(summary):
        return
    try:
        _executor().submit(
            _scan,
            store,
            actor_id,
            principal_id,
            event_id,
            summary,
            occurred_at_ms,
        )
    except Exception:  # noqa: BLE001
        logger.warning("companion watch scheduling failed", stack_trace=traceback.format_exc())


def _scan(
    store: CompanionStore,
    actor_id: str,
    principal_id: str,
    event_id: str,
    summary: str,
    occurred_at_ms: int,
) -> None:
    try:
        record_watch(
            store,
            actor_id,
            principal_id,
            event_id=event_id,
            summary=summary,
            occurred_at_ms=occurred_at_ms,
        )
    except Exception:  # noqa: BLE001
        logger.warning("companion watch scan failed", stack_trace=traceback.format_exc())


# ------------------------------------------------------------------------- helpers


def _validate(data: object) -> WatchOutcome:
    if not isinstance(data, dict):
        return WatchOutcome(ok=False, error="payload is not an object")
    if data.get("is_watch") is not True:
        return WatchOutcome(ok=False, error="not a watch")
    topic = data.get("topic")
    time_text = data.get("time_text")
    confidence = data.get("confidence")
    if not isinstance(topic, str) or not topic.strip():
        return WatchOutcome(ok=False, error="topic missing")
    if not isinstance(time_text, str) or not time_text.strip():
        return WatchOutcome(ok=False, error="time_text missing")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return WatchOutcome(ok=False, error="confidence is not a number")
    if not 0 <= float(confidence) <= 1:
        return WatchOutcome(ok=False, error="confidence out of range")
    return WatchOutcome(
        ok=True,
        topic=redact_text(topic.strip())[:MAX_WATCH_TITLE],
        time_text=redact_text(time_text.strip()),
        confidence=float(confidence),
    )


def _number(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if raw.startswith("十"):
        digit = _CN_DIGITS.get(raw[1:])
        return None if digit is None else 10 + digit
    if raw.endswith("十"):
        digit = _CN_DIGITS.get(raw[:-1])
        return None if digit is None else digit * 10
    if "十" in raw:
        head, _, tail = raw.partition("十")
        tens = _CN_DIGITS.get(head)
        ones = _CN_DIGITS.get(tail) if tail else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    return _CN_DIGITS.get(raw)


def _default_model() -> object:
    from gacore.llm import get_llm

    return get_llm([], bind_tools=False)
