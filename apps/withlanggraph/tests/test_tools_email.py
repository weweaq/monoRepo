"""Tests for gacore.tools.email_tools — SMTP fully mocked via an injected smtp factory, no real network.

The _send_sync seam takes an injectable smtp_factory (mirrors web_tools.client_factory) so
connection/login/send are exercised against a FakeSmtpServer; tool-level tests monkeypatch
_send_sync to assert validation and recipient resolution without touching the transport.

Assertions on message content decode the MIME payload (utf-8 bodies are base64/QP-encoded by
smtplib's email library, so raw as_string() text assertions would be brittle).
"""

from __future__ import annotations

import email
import smtplib
from collections.abc import Callable
from email.header import decode_header
from pathlib import Path
from typing import Any

import pytest

from gacore.tools.email_tools import (
    SendEmailResult,
    _build_message,
    _resolve_settings,
    _send_sync,
    _SmtpSettings,
    send_email,
)


class FakeSmtpServer:
    """Minimal smtplib.SMTP/SMTP_SSL stand-in: records login/send/quit, can raise on demand."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.connect_args: tuple[object, ...] | None = None
        self.login_args: tuple[str, str] | None = None
        self.sent: tuple[str, list[str], str] | None = None
        self.quit_called = False
        self.starttls_called = False

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        if self._error is not None:
            raise self._error
        self.login_args = (user, password)

    def sendmail(self, from_addr: str, to_addrs: list[str], msg: str) -> None:
        self.sent = (from_addr, to_addrs, msg)

    def quit(self) -> None:
        self.quit_called = True


def make_smtp_factory(server: FakeSmtpServer) -> Callable[..., FakeSmtpServer]:
    """Return a factory that records connection kwargs and hands back the fake."""

    def factory(host: str, port: int, timeout: int) -> FakeSmtpServer:
        server.connect_args = (host, port, timeout)
        return server

    return factory


def _ssl_settings(**overrides: str) -> _SmtpSettings:
    """Settings for sender@qq.com (auto-detected smtp.qq.com:465 SSL) with optional env overrides."""
    env = {
        "SMTP_USER": "sender@qq.com",
        "SMTP_PASSWORD": "auth-code",
        "SMTP_TO": "default@example.com",
        **overrides,
    }
    return _resolve_settings(env)


def _decoded_text_part(msg: Any) -> str:
    """Decode the html text part of a (possibly multipart) message to a string."""
    payload = msg.get_payload()
    if isinstance(payload, list):
        text_part = next(p for p in payload if p.get_content_type() == "text/html")
        raw = text_part.get_payload(decode=True)
    else:
        raw = msg.get_payload(decode=True)
    return raw.decode("utf-8")


def _decoded_subject(msg: Any) -> str:
    """Decode a possibly RFC-2047-encoded Subject header to plain text."""
    raw = str(msg["Subject"])
    chunks = decode_header(raw)
    return "".join(
        text.decode(charset or "utf-8") if isinstance(text, bytes) else text
        for text, charset in chunks
    )


def test_send_sync_sends_via_ssl_and_reports_sent() -> None:
    settings = _ssl_settings()
    server = FakeSmtpServer()

    result = _send_sync("Hello", "to@example.com", "<p>hi</p>", settings, smtp_factory=make_smtp_factory(server))

    assert server.connect_args == ("smtp.qq.com", 465, 10)
    assert server.login_args == ("sender@qq.com", "auth-code")
    assert server.quit_called is True
    assert server.sent is not None
    assert server.sent[0] == "sender@qq.com"
    assert server.sent[1] == ["to@example.com"]
    parsed = email.message_from_string(server.sent[2])
    assert parsed["From"] == "sender@qq.com"
    assert parsed["To"] == "to@example.com"
    assert _decoded_subject(parsed) == "Hello"
    assert _decoded_text_part(parsed) == "<p>hi</p>"
    assert result == {"status": "sent", "to": "to@example.com", "subject": "Hello", "image_count": 0}


def test_send_sync_uses_starttls_when_ssl_disabled() -> None:
    settings = _ssl_settings(SMTP_HOST="smtp-mail.outlook.com", SMTP_PORT="587")
    server = FakeSmtpServer()

    _send_sync("s", "to@example.com", "b", settings, smtp_factory=make_smtp_factory(server))

    assert server.starttls_called is True
    assert server.connect_args == ("smtp-mail.outlook.com", 587, 10)


def test_send_sync_returns_error_dict_on_smtp_authentication_failure() -> None:
    server = FakeSmtpServer(error=smtplib.SMTPAuthenticationError(535, b"auth failed"))

    result = _send_sync("s", "to@example.com", "b", _ssl_settings(), smtp_factory=make_smtp_factory(server))

    assert result["error"] == "smtp_failed"
    assert result["to"] == "to@example.com"
    assert isinstance(result["message"], str)
    assert result["message"]


def test_send_sync_returns_error_dict_on_connection_oserror() -> None:
    server = FakeSmtpServer(error=OSError("connection refused"))

    result = _send_sync("s", "to@example.com", "b", _ssl_settings(), smtp_factory=make_smtp_factory(server))

    assert result["error"] == "smtp_failed"
    assert "connection refused" in result["message"]


def test_build_message_plain_html_sets_headers_when_no_images() -> None:
    msg = _build_message("主题", "<p>hi</p>", "a@qq.com", "b@qq.com")

    assert msg["From"] == "a@qq.com"
    assert msg["To"] == "b@qq.com"
    assert _decoded_subject(msg) == "主题"
    assert _decoded_text_part(msg) == "<p>hi</p>"


def test_build_message_inlines_images_with_cid(tmp_path: Path) -> None:
    img_a = tmp_path / "a.jpg"
    img_b = tmp_path / "b.jpg"
    img_a.write_bytes(b"\xff\xd8fakejpeg")
    img_b.write_bytes(b"\xff\xd8fakejpeg")

    msg = _build_message("s", "<p>body</p>", "a@qq.com", "b@qq.com", [str(img_a), str(img_b)])

    assert 'src="cid:photo0"' in _decoded_text_part(msg)
    assert 'src="cid:photo1"' in _decoded_text_part(msg)
    image_parts = [p for p in msg.get_payload() if p.get_content_type() == "image/jpeg"]
    assert [p["Content-ID"] for p in image_parts] == ["<photo0>", "<photo1>"]


def test_build_message_inlines_png_with_image_png_subtype(tmp_path: Path) -> None:
    img = tmp_path / "qr.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    msg = _build_message("s", "<p>扫码</p>", "a@qq.com", "b@qq.com", [str(img)])

    assert 'src="cid:photo0"' in _decoded_text_part(msg)
    image_parts = [p for p in msg.get_payload() if p.get_content_type() == "image/png"]
    assert [p["Content-ID"] for p in image_parts] == ["<photo0>"]


def test_build_message_skips_missing_images(tmp_path: Path) -> None:
    existing = tmp_path / "a.jpg"
    existing.write_bytes(b"\xff\xd8jpeg")

    msg = _build_message(
        "s",
        "<p>b</p>",
        "a@qq.com",
        "b@qq.com",
        [str(existing), str(tmp_path / "nope.jpg")],
    )

    text = _decoded_text_part(msg)
    assert 'src="cid:photo0"' in text
    assert "photo1" not in text
    image_parts = [p for p in msg.get_payload() if p.get_content_type() == "image/jpeg"]
    assert [p["Content-ID"] for p in image_parts] == ["<photo0>"]


def test_send_email_returns_config_error_when_credentials_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_send_sync(*args: Any, **kwargs: Any) -> SendEmailResult:
        nonlocal called
        called = True
        return SendEmailResult(status="sent", to="x", subject="s", image_count=0)

    monkeypatch.setattr("gacore.tools.email_tools._send_sync", fake_send_sync)

    result = send_email.func(to="a@b.com", subject="s", body="b", _env={})

    assert result["error"] == "smtp_not_configured"
    assert called is False


def test_send_email_returns_recipient_error_when_no_recipient_available() -> None:
    env = {"SMTP_USER": "u@qq.com", "SMTP_PASSWORD": "p"}

    result = send_email.func(subject="s", body="b", _env=env)

    assert result["error"] == "recipient_required"


def test_send_email_uses_smtp_to_default_when_to_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_send_sync(subject: str, to_addr: str, body: str, settings: _SmtpSettings, image_paths: list[str] | None = None) -> SendEmailResult:
        captured["to"] = to_addr
        return SendEmailResult(status="sent", to=to_addr, subject=subject, image_count=0)

    monkeypatch.setattr("gacore.tools.email_tools._send_sync", fake_send_sync)
    env = {"SMTP_USER": "u@qq.com", "SMTP_PASSWORD": "p", "SMTP_TO": "d@example.com"}

    result = send_email.func(subject="s", body="b", _env=env)

    assert captured["to"] == "d@example.com"
    assert result["status"] == "sent"


def test_send_email_explicit_to_overrides_default_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_send_sync(subject: str, to_addr: str, body: str, settings: _SmtpSettings, image_paths: list[str] | None = None) -> SendEmailResult:
        captured["to"] = to_addr
        return SendEmailResult(status="sent", to=to_addr, subject=subject, image_count=0)

    monkeypatch.setattr("gacore.tools.email_tools._send_sync", fake_send_sync)
    env = {"SMTP_USER": "u@qq.com", "SMTP_PASSWORD": "p", "SMTP_TO": "d@example.com"}

    result = send_email.func(to="x@example.com", subject="s", body="b", _env=env)

    assert captured["to"] == "x@example.com"
    assert result["status"] == "sent"


def test_send_email_schema_exposes_public_args_but_excludes_env_seam() -> None:
    props = send_email.args_schema.model_json_schema()["properties"]

    assert "to" in props
    assert "subject" in props
    assert "body" in props
    assert "image_paths" in props
    assert "_env" not in props
