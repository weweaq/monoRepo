"""Secret redaction (P1 observability): logs and ops surfaces must not leak keys."""

from __future__ import annotations

import pytest

from gacore.redact import MASK, redact_obj, redact_text


def test_redacts_url_query_secret() -> None:
    url = "https://restapi.amap.com/v3/geocode/regeo?key=0123456789abcdef&output=json"
    out = redact_text(url)
    assert "0123456789abcdef" not in out
    assert f"key={MASK}" in out
    assert "output=json" in out


def test_redacts_bearer_header() -> None:
    out = redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert f"Bearer {MASK}" in out


def test_redacts_api_key_shapes() -> None:
    out = redact_text("using sk-abc123def456gh789 and password=hunter2hunter2")
    assert "sk-abc123def456gh789" not in out
    assert "hunter2hunter2" not in out


def test_redacts_known_env_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMAP_KEY", "amapsecret0123456789")
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu.secret.value.123")
    out = redact_text("failed: amapsecret0123456789 and zhipu.secret.value.123")
    assert "amapsecret0123456789" not in out
    assert "zhipu.secret.value.123" not in out
    assert out.count(MASK) == 2


def test_short_env_values_are_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_TOKEN", "abc")
    assert redact_text("abc") == "abc"


def test_redaction_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "supersecretpass")
    once = redact_text("password=supersecretpass?token=abcdefgh")
    assert redact_text(once) == once


def test_plain_text_untouched() -> None:
    text = "periodic ETL failed rc=1 after 3210ms"
    assert redact_text(text) == text


def test_redact_obj_recurses_into_context() -> None:
    payload = {
        "stack_trace": "RuntimeError: boom key=abcdefgh12",
        "context": {"url": "https://x/y?token=tokentoken", "n": 3},
        "items": ["sk-abcdefgh12345"],
    }
    out = redact_obj(payload)
    assert "abcdefgh12" not in out["stack_trace"]
    assert "tokentoken" not in out["context"]["url"]
    assert out["context"]["n"] == 3
    assert out["items"] == [MASK]
