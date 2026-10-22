"""Secret redaction for log payloads and ops surfaces (P1 observability).

Two layers, applied in this order:

1. **Value layer** — any ``os.environ`` value whose *name* looks like a credential
   (``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` / ``*_PASSWORD`` ...) and is at least 8
   chars long is replaced verbatim. This catches leaked third-party keys regardless
   of their format (AMAP_KEY, ZHIPU_API_KEY, SMTP_PASSWORD, ...).
2. **Pattern layer** — URL query secrets (``?key=...``), ``Bearer`` headers,
   ``sk-...``-style API keys and ``api_key=...`` assignments.

All functions are idempotent and never raise: redaction must not break logging.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Final

MASK: Final = "***"
_MIN_SECRET_LEN: Final = 8
_PLACEHOLDERS: Final = frozenset({"none", "null", "true", "false", "changeme", "***"})

_SECRET_ENV_NAME: Final = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH)(?:_|$)",
    re.IGNORECASE,
)

_QUERY_SECRET: Final = re.compile(
    r"([?&](?:api_?key|key|token|access_token|refresh_token|secret|password|passwd"
    r"|sig|signature|auth)=)([^&\s\"'<>]+)",
    re.IGNORECASE,
)
_BEARER: Final = re.compile(
    r"(\bBearer\s+)([A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)
_API_KEY_TOKEN: Final = re.compile(
    r"\b(?:sk|pk|ak|rk|ghp|gho|ghs|github_pat)[-_][A-Za-z0-9_\-]{8,}\b"
)
_ASSIGN_SECRET: Final = re.compile(
    r"(\b(?:api_?key|access_token|refresh_token|secret|password|passwd|token"
    r"|key|sig|signature|auth)\b"
    r"\s*[:=]\s*[\"']?)([^\"'\s,;&}]{8,})",
    re.IGNORECASE,
)


def _secret_env_values() -> tuple[str, ...]:
    """Environment values that look like credentials, longest first (avoid partials)."""
    values: set[str] = set()
    for name, value in os.environ.items():
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        if value.strip().lower() in _PLACEHOLDERS:
            continue
        if _SECRET_ENV_NAME.search(name):
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def redact_text(text: str, *, _secrets: tuple[str, ...] | None = None) -> str:
    """Return ``text`` with known credentials masked; idempotent and total."""
    if not text:
        return text
    secrets = _secret_env_values() if _secrets is None else _secrets
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    text = _QUERY_SECRET.sub(lambda m: m.group(1) + MASK, text)
    text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
    text = _API_KEY_TOKEN.sub(MASK, text)
    text = _ASSIGN_SECRET.sub(lambda m: m.group(1) + MASK, text)
    return text


def redact_obj(value: Any, *, _secrets: tuple[str, ...] | None = None) -> Any:
    """Recursively redact strings inside dicts/lists; other types pass through."""
    if isinstance(value, str):
        return redact_text(value, _secrets=_secrets)
    if isinstance(value, Mapping):
        secrets = _secret_env_values() if _secrets is None else _secrets
        return {k: redact_obj(v, _secrets=secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        secrets = _secret_env_values() if _secrets is None else _secrets
        return [redact_obj(v, _secrets=secrets) for v in value]
    return value
