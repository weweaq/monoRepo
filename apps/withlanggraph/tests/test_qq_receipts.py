"""Tests for structured QQ delivery receipts (Companion V1 Step 5).

``_send_markdown`` must distinguish three outcomes per 4500-char segment:
confirmed, definite failure (plain fallback allowed), and unknown (timeout /
connection loss — no fallback, because the markdown may already be delivered).

Provenance: these cases originally lived in ``test_companion_delivery.py`` and were
dropped when Step 9 rewrote that file for reply batching (2026-10-30). They were
restored here, in a purpose-named module, so a future rewrite of the batching tests
cannot silently delete the delivery-receipt contract again. Keep them here: this
file is the only automated coverage of "failed/unknown sends are never recorded".
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Fake botpy BEFORE importing the module under test (same shim as test_qq.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_fake_botpy = types.ModuleType("botpy")


class _FakeIntents:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


_fake_botpy.Intents = _FakeIntents
_fake_message = types.ModuleType("botpy.message")
_fake_message.C2CMessage = type("C2CMessage", (), {})
_fake_message.GroupMessage = type("GroupMessage", (), {})
sys.modules.setdefault("botpy", _fake_botpy)
sys.modules.setdefault("botpy.message", _fake_message)


from gacore.frontends import qq
from gacore.frontends.qq import QQApp, SendReceipt

# Explicit markers: the repo root sets asyncio_mode=auto, but running from inside
# apps/withlanggraph (rootdir without that ini) would otherwise treat these as sync.
pytestmark = pytest.mark.asyncio


class _ScriptedApi:
    """Async callable that replays a fixed list of results/exceptions."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _app(script: list[object]) -> tuple[QQApp, _ScriptedApi]:
    app = QQApp(MagicMock())
    api = _ScriptedApi(script)
    app.client = MagicMock()
    app.client.api.post_c2c_message = api
    return app, api


class TestSendMarkdown:
    async def test_markdown_success(self) -> None:
        app, api = _app([{"id": "mid-1"}])
        receipt = await app._send_markdown("u1", "hello", is_group=False, msg_id="m1")

        assert receipt.confirmed is True
        assert len(api.calls) == 1
        assert api.calls[0]["msg_type"] == 2
        segment = receipt.segments[0]
        assert segment.confirmed is True
        assert segment.actual_text == "hello"
        assert segment.provider_message_id == "mid-1"
        assert segment.outcome_unknown is False

    async def test_markdown_definite_failure_falls_back_to_plain(self) -> None:
        # botpy maps HTTP 4xx/5xx to RuntimeError subclasses — a definite rejection.
        app, api = _app([RuntimeError("rejected"), {"id": "plain-id"}])
        receipt = await app._send_markdown("u1", "hello", is_group=False, msg_id="m1")

        assert receipt.confirmed is True
        assert [call["msg_type"] for call in api.calls] == [2, 0]
        assert receipt.segments[0].provider_message_id == "plain-id"

    async def test_timeout_is_unknown_and_does_not_fall_back(self) -> None:
        app, api = _app([TimeoutError("timed out")])
        receipt = await app._send_markdown("u1", "hello", is_group=False, msg_id="m1")

        assert receipt.confirmed is False
        assert len(api.calls) == 1  # no plain retry: the markdown may already be sent
        segment = receipt.segments[0]
        assert segment.outcome_unknown is True
        assert segment.confirmed is False

    async def test_all_failures_are_not_confirmed(self) -> None:
        app, api = _app([RuntimeError("markdown rejected"), RuntimeError("plain rejected")])
        receipt = await app._send_markdown("u1", "hello", is_group=False, msg_id="m1")

        assert receipt.confirmed is False
        assert [call["msg_type"] for call in api.calls] == [2, 0]
        assert receipt.segments[0].outcome_unknown is False

    async def test_multi_segment_partial_success(self) -> None:
        first = "a" * qq._SPLIT_LIMIT
        content = first + "\n" + "b" * 100
        # Part 1 succeeds; part 2 markdown + plain both rejected.
        app, api = _app([{"id": "m-1"}, RuntimeError("rejected"), RuntimeError("rejected")])
        receipt = await app._send_markdown("u1", content, is_group=False, msg_id="m1")

        assert receipt.confirmed is False
        assert len(receipt.segments) == 2
        assert receipt.segments[0].confirmed is True
        assert receipt.segments[0].actual_text == first
        assert receipt.segments[1].confirmed is False
        assert receipt.segments[1].actual_text == "b" * 100

    async def test_empty_provider_id_is_still_confirmed(self) -> None:
        app, _ = _app([{"id": ""}])
        receipt = await app._send_markdown("u1", "hello", is_group=False, msg_id="m1")

        assert receipt.confirmed is True
        assert receipt.segments[0].provider_message_id is None

    async def test_no_client_is_not_confirmed(self) -> None:
        app = QQApp(MagicMock())
        app.client = None
        receipt = await app._send_markdown("u1", "hello", is_group=False, msg_id="m1")

        assert receipt.confirmed is False
        assert receipt.segments[0].actual_text == "hello"


class TestSendTextChatLog:
    async def test_chat_log_written_after_receipt_with_confirmed_text(self, monkeypatch) -> None:
        app, _ = _app([{"id": "m-1"}])
        logged: list[str] = []
        monkeypatch.setattr(
            qq,
            "_persist_chat_log",
            lambda memory_dir, chat_id, user_id, role, text: logged.append(text),
        )

        receipt = await app.send_text("u1", "hello")

        assert isinstance(receipt, SendReceipt)
        assert receipt.confirmed is True
        assert logged == ["hello"]

    async def test_chat_log_skipped_when_nothing_confirmed(self, monkeypatch) -> None:
        app, _ = _app([RuntimeError("rejected"), RuntimeError("rejected")])
        logged: list[str] = []
        monkeypatch.setattr(
            qq,
            "_persist_chat_log",
            lambda memory_dir, chat_id, user_id, role, text: logged.append(text),
        )

        receipt = await app.send_text("u1", "hello")

        assert receipt.confirmed is False
        assert logged == []
