"""opencode 抽取契约：禁止反问、校正重试与审计指纹。"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Final

import httpx

NO_QUESTION_RULE: Final = (
    "输出契约：直接给出最终结果。禁止反问、禁止请求确认、禁止给选项、禁止等待用户回复。"
    "信息不足时按已知内容完成，缺失项按 schema 省略或用空值，不要提问。"
)
MAX_ATTEMPTS_ENV: Final = "OPENCODE_MAX_ATTEMPTS"
DEFAULT_MAX_ATTEMPTS: Final = 3
MAX_ATTEMPTS_CAP: Final = 5
_RETRY_STATUS: Final = frozenset({409, 429})
_NON_RETRY_STATUS: Final = frozenset({400, 401, 403, 404})
_QUESTION_MARKERS: Final = (
    "请确认",
    "请告诉我",
    "需要我",
    "你要哪个",
    "你希望我",
    "是否要我",
    "等你回复",
    "回复我",
    "选一个",
    "哪个方案",
    "要不要我",
)
_CODE_FENCE: Final = re.compile(r"```.*?```", re.S)


def is_opencode(llm: Any) -> bool:
    return getattr(llm, "_llm_type", "") == "opencode"


def with_no_question_rule(prompt: str) -> str:
    if NO_QUESTION_RULE in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{NO_QUESTION_RULE}"


def max_attempts(llm: Any) -> int:
    if not is_opencode(llm):
        return 2
    raw = os.environ.get(MAX_ATTEMPTS_ENV)
    if raw is None:
        return DEFAULT_MAX_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS
    return max(1, min(MAX_ATTEMPTS_CAP, value))


def detect_question(text: str) -> str | None:
    stripped = _CODE_FENCE.sub("", text).strip()
    for marker in _QUESTION_MARKERS:
        if marker in stripped:
            return marker
    if stripped.endswith(("?", "？")):
        return "trailing_question_mark"
    return None


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def corrective_message(violation: str) -> str:
    return (
        f"输出不符合要求（{violation}）。不要反问、不要请求确认、不要给选项；"
        "只输出符合 schema 的 JSON，不要解释。信息不足时按已知内容完成，缺失项省略或用空值。"
    )


def retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in _RETRY_STATUS or 500 <= status < 600
    status = getattr(exc, "status_code", None)
    if status in _NON_RETRY_STATUS:
        return False
    if isinstance(status, int) and (status in _RETRY_STATUS or 500 <= status < 600):
        return True
    retryable = getattr(exc, "is_retryable", None)
    if retryable is not None:
        return bool(retryable)
    return isinstance(exc, httpx.NetworkError)
