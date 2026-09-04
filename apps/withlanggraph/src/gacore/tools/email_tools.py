"""Email tool for gacore: send emails over SMTP with optional inline HTML images.

Ported from py-wei's weitrack/interaction/channels/smtp/sender.py into the gacore
tool shape: @tool decorator, TypedDict result/error (never raises), and an injectable
smtp factory so tests never touch a real network (mirrors web_tools.client_factory).

Configuration is read from the environment at call time (SMTP_USER / SMTP_PASSWORD /
SMTP_TO / SMTP_HOST / SMTP_PORT / SMTP_SSL / SMTP_TIMEOUT). SMTP_HOST / SMTP_PORT /
SMTP_SSL are optional: when unset they are inferred from the sender domain via the
SMTP_SERVERS provider table (qq / gmail / outlook / 163), exactly like py-wei.

The _env arg is an injection seam excluded from the tool's args schema (underscore-
prefixed), so production falls back to os.environ and tests pass a plain dict.
"""

from __future__ import annotations

import os
import smtplib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Final, Literal, TypedDict

from langchain_core.tools import tool

from gacore.jsonl_logger import get_logger

logger = get_logger("tools.email_tools")

# SMTP server defaults keyed by the first label of the sender's email domain.
SMTP_SERVERS: Final[dict[str, dict[str, object]]] = {
    "qq": {"server": "smtp.qq.com", "port": 465, "ssl": True},
    "gmail": {"server": "smtp.gmail.com", "port": 465, "ssl": True},
    "outlook": {"server": "smtp-mail.outlook.com", "port": 587, "ssl": False},
    "163": {"server": "smtp.163.com", "port": 465, "ssl": True},
}

_DEFAULT_TIMEOUT: Final = 10
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})

# image subtype by file extension, falling back to jpeg for unknown suffixes.
_IMAGE_SUBTYPES: Final[dict[str, str]] = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".gif": "gif",
    ".webp": "webp",
}


class SendEmailResult(TypedDict):
    """Successful send: the recipient, subject and a confirmation of delivery."""

    status: Literal["sent"]
    to: str
    subject: str
    image_count: int


class SendEmailError(TypedDict):
    """Failed send: machine-readable error tag, human message and the intended recipient."""

    error: str
    message: str
    to: str


@dataclass(frozen=True, slots=True)
class _SmtpSettings:
    """Resolved SMTP connection settings: host, port, encryption and credentials."""

    host: str
    port: int
    ssl: bool
    user: str
    password: str
    default_to: str
    timeout: int


def _detect_provider(email_addr: str) -> str:
    """Return the first label of the sender domain, e.g. 'foo@qq.com' -> 'qq'."""
    return email_addr.split("@")[-1].lower().split(".")[0]


def _resolve_settings(env: Mapping[str, str]) -> _SmtpSettings:
    """Resolve SMTP settings from env; host/port/ssl infer from the provider table when unset."""
    user = env.get("SMTP_USER", "").strip()
    password = env.get("SMTP_PASSWORD", "").strip()
    default_to = env.get("SMTP_TO", "").strip()

    try:
        timeout = max(1, int(env.get("SMTP_TIMEOUT", str(_DEFAULT_TIMEOUT))))
    except ValueError:
        timeout = _DEFAULT_TIMEOUT

    provider_cfg = SMTP_SERVERS.get(_detect_provider(user)) if user else None

    host = env.get("SMTP_HOST", "").strip() or (
        str(provider_cfg["server"]) if provider_cfg else ""
    )

    raw_ssl = env.get("SMTP_SSL", "").strip().lower()
    ssl_flag: bool | None = raw_ssl in _TRUTHY if raw_ssl else None

    raw_port = env.get("SMTP_PORT", "").strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            port = int(provider_cfg["port"]) if provider_cfg else 587
        ssl = ssl_flag if ssl_flag is not None else port == 465
    elif provider_cfg:
        port = int(provider_cfg["port"])
        ssl = ssl_flag if ssl_flag is not None else bool(provider_cfg["ssl"])
    else:
        port = 465 if ssl_flag else 587
        ssl = ssl_flag if ssl_flag is not None else False

    return _SmtpSettings(
        host=host,
        port=port,
        ssl=ssl,
        user=user,
        password=password,
        default_to=default_to,
        timeout=timeout,
    )


def _image_subtype(path: str) -> str:
    """Infer the MIME image subtype from the file extension (default: jpeg)."""
    ext = os.path.splitext(path)[1].lower()
    return _IMAGE_SUBTYPES.get(ext, "jpeg")


def _subject_header(subject: str) -> str | Header:
    """Return the subject as plain text for ASCII, RFC-2047 encoded only when non-ASCII (Chinese-safe)."""
    return subject if subject.isascii() else Header(subject, "utf-8")


def _build_message(
    subject: str,
    body: str,
    from_addr: str,
    to_addr: str,
    image_paths: list[str] | None = None,
) -> MIMEMultipart | MIMEText:
    """Build an HTML email; existing image files are inlined as cid:photoN attachments."""
    headers = {"Subject": _subject_header(subject), "From": from_addr, "To": to_addr}
    if not image_paths:
        msg: MIMEMultipart | MIMEText = MIMEText(body, "html", "utf-8")
        for key, value in headers.items():
            msg[key] = value
        return msg

    valid_images = [path for path in image_paths if os.path.exists(path)]
    missing = len(image_paths) - len(valid_images)
    if missing:
        logger.warning("send_email skipping missing image files", missing=missing)

    if not valid_images:
        msg = MIMEText(body, "html", "utf-8")
        for key, value in headers.items():
            msg[key] = value
        return msg

    img_tags = "".join(
        f'<img src="cid:photo{idx}" style="max-width:100%;height:auto;margin-top:16px;border-radius:8px;">'
        for idx in range(len(valid_images))
    )
    if "</body>" in body:
        body_with_images = body.replace("</body>", img_tags + "</body>")
    else:
        body_with_images = body + img_tags

    msg = MIMEMultipart("related")
    for key, value in headers.items():
        msg[key] = value
    msg.attach(MIMEText(body_with_images, "html", "utf-8"))

    for idx, image_path in enumerate(valid_images):
        try:
            with open(image_path, "rb") as fh:
                img = MIMEImage(fh.read(), _subtype=_image_subtype(image_path))
            img.add_header("Content-ID", f"<photo{idx}>")
            img.add_header("Content-Disposition", "inline", filename=os.path.basename(image_path))
            msg.attach(img)
        except OSError as exc:
            logger.warning("send_email failed to attach image", path=image_path, error=str(exc))

    return msg


def _send_sync(
    subject: str,
    to_addr: str,
    body: str,
    settings: _SmtpSettings,
    image_paths: list[str] | None = None,
    smtp_factory: Callable[..., object] | None = None,
) -> SendEmailResult | SendEmailError:
    """Connect, authenticate and send; returns an error dict on any failure (never raises)."""
    factory = smtp_factory or (smtplib.SMTP_SSL if settings.ssl else smtplib.SMTP)
    try:
        server = factory(settings.host, settings.port, timeout=settings.timeout)
        if not settings.ssl:
            server.starttls()
        server.login(settings.user, settings.password)
        msg = _build_message(subject, body, settings.user, to_addr, image_paths)
        server.sendmail(settings.user, [to_addr], msg.as_string())
        server.quit()
    except (smtplib.SMTPException, OSError) as exc:
        logger.error(
            "send_email failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
            context={"to": to_addr, "subject": subject, "host": settings.host},
        )
        return SendEmailError(error="smtp_failed", message=str(exc), to=to_addr)

    image_count = len(image_paths) if image_paths else 0
    logger.info("send_email sent", to=to_addr, subject=subject, image_count=image_count)
    return SendEmailResult(status="sent", to=to_addr, subject=subject, image_count=image_count)


@tool
def send_email(
    to: str | None = None,
    subject: str = "",
    body: str = "",
    image_paths: list[str] | None = None,
    _env: Mapping[str, str] | None = None,
) -> SendEmailResult | SendEmailError:
    """Send an email over SMTP; body is HTML and image_paths are inlined as images.

    Credentials and SMTP server come from the environment (SMTP_USER / SMTP_PASSWORD /
    SMTP_TO / SMTP_HOST / SMTP_PORT / SMTP_SSL / SMTP_TIMEOUT); SMTP_HOST / SMTP_PORT /
    SMTP_SSL are optional and auto-detected from the sender domain when unset. When `to`
    is empty the SMTP_TO default recipient is used. Returns an error dict, never raises.
    """
    env = os.environ if _env is None else _env
    settings = _resolve_settings(env)
    recipient = (to or "").strip() or settings.default_to

    if not settings.user or not settings.password:
        logger.warning("send_email skipped: SMTP credentials not configured")
        return SendEmailError(error="smtp_not_configured", message="SMTP_USER / SMTP_PASSWORD not set", to=recipient)

    if not settings.host:
        logger.warning("send_email skipped: no SMTP host and provider unknown")
        return SendEmailError(error="smtp_not_configured", message="cannot resolve SMTP_HOST from SMTP_USER", to=recipient)

    if not recipient:
        logger.warning("send_email skipped: no recipient")
        return SendEmailError(error="recipient_required", message="no `to` argument and no SMTP_TO default", to="")

    return _send_sync(subject, recipient, body, settings, image_paths)
