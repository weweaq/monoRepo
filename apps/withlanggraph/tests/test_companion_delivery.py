"""Tests for Step 9 reply batching: planning 1-3 segments and batched delivery.

Only batch planning/delivery lives here. The Step 5 send-receipt contract
(``_send_markdown`` fallback/unknown/partial rules and the chat-log-after-receipt
rule) is covered by ``test_qq_receipts.py`` — do not move those cases back into this
file, or a rewrite of the batching tests will delete them again (happened once).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gacore.companion.reply_batch import (
    MAX_REPLY_SEGMENTS,
    REPLY_BATCH_VERSION,
    ReplyBatch,
    plan_final_reply,
)
from gacore.frontends import qq
from gacore.frontends.qq import QQApp, SegmentReceipt, SendReceipt


def _receipt(text: str) -> SendReceipt:
    return SendReceipt(True, (SegmentReceipt(True, text, None),))


def test_version_and_limit_match_the_contract() -> None:
    assert REPLY_BATCH_VERSION == "companion-reply-batch@v1"
    assert MAX_REPLY_SEGMENTS == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("嗯，我在。", ("嗯，我在。",)),
        ("今天下雨了。\n\n记得带伞。", ("今天下雨了。", "记得带伞。")),
        ("第一段。\n\n第二段。\n\n第三段。", ("第一段。", "第二段。", "第三段。")),
        ("今天下雨了。记得带伞。", ("今天下雨了。", "记得带伞。")),
    ],
)
def test_chat_replies_split_on_semantic_boundaries(
    text: str, expected: tuple[str, ...]
) -> None:
    assert plan_final_reply(text).segments == expected


@pytest.mark.parametrize(
    "text",
    [
        "第一段。\n\n第二段。\n\n第三段。\n\n第四段。",
        "看看这个：\n\n```python\nprint('hi')\n```",
        "| 项目 | 值 |\n| --- | --- |\n| a | 1 |",
        'Traceback (most recent call last):\n  File "x.py", line 3, in <module>',
        "日志在 C:\\Users\\me\\app.log 里。",
        "$ uv run pytest",
        "步骤：\n- 第一步\n- 第二步\n- 第三步",
        "# 报告\n\n正文。",
        "【方案一】先做 A。\n\n【方案二】先做 B。",
        "这是一段很长的话。" * 60,
        "今天下雨了。记得带伞",
    ],
)
def test_technical_or_ambiguous_replies_stay_atomic(text: str) -> None:
    assert plan_final_reply(text).segments == (text.strip(),)


def test_atomic_mode_never_splits() -> None:
    text = "今天下雨了。\n\n记得带伞。"

    assert plan_final_reply(text, "atomic").segments == (text,)
    assert plan_final_reply(text, "operator").segments == (text,)


def test_empty_text_yields_no_segments() -> None:
    assert plan_final_reply("   \n  ").segments == ()


def test_batch_id_is_unique_per_plan() -> None:
    first = plan_final_reply("嗯，我在。")
    second = plan_final_reply("嗯，我在。")

    assert first.logical_turn_id != second.logical_turn_id


@pytest.fixture()
def app() -> QQApp:
    instance = QQApp(MagicMock())
    instance.send_text = AsyncMock(side_effect=lambda chat_id, text, **kw: _receipt(text))
    return instance


@pytest.mark.asyncio
async def test_batch_sends_every_segment_in_order(app: QQApp, monkeypatch) -> None:
    monkeypatch.setattr(qq, "_SEGMENT_INTERVAL_SECONDS", 0)
    batch = plan_final_reply("今天下雨了。\n\n记得带伞。")

    receipts = await app._deliver_reply_batch("chat-1", batch, is_group=False)

    assert [r.segments[0].actual_text for r in receipts] == ["今天下雨了。", "记得带伞。"]


@pytest.mark.asyncio
async def test_cancelled_batch_drops_unsent_segments(app: QQApp, monkeypatch) -> None:
    monkeypatch.setattr(qq, "_SEGMENT_INTERVAL_SECONDS", 0)
    sent: list[str] = []

    async def _send(chat_id: str, text: str, **kwargs: object) -> SendReceipt:
        sent.append(text)
        if len(sent) == 1:
            app._cancel_pending_delivery(chat_id)
        return _receipt(text)

    app.send_text = AsyncMock(side_effect=_send)
    batch = plan_final_reply("第一段。\n\n第二段。\n\n第三段。")

    receipts = await app._deliver_reply_batch("chat-1", batch, is_group=False)

    assert sent == ["第一段。"]
    assert len(receipts) == 1


@pytest.mark.asyncio
async def test_two_batches_never_interleave(app: QQApp, monkeypatch) -> None:
    monkeypatch.setattr(qq, "_SEGMENT_INTERVAL_SECONDS", 0)
    order: list[str] = []
    gate = asyncio.Event()

    async def _send(chat_id: str, text: str, **kwargs: object) -> SendReceipt:
        order.append(text)
        if text == "A1":
            await gate.wait()
        return _receipt(text)

    app.send_text = AsyncMock(side_effect=_send)
    batch_a = ReplyBatch("turn-a", ("A1", "A2"))
    batch_b = ReplyBatch("turn-b", ("B1",))

    task_a = asyncio.create_task(app._deliver_reply_batch("chat-1", batch_a, is_group=False))
    await asyncio.sleep(0)
    task_b = asyncio.create_task(app._deliver_reply_batch("chat-1", batch_b, is_group=False))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(task_a, task_b)

    assert order == ["A1", "A2", "B1"]


def test_owner_c2c_plans_segments_while_group_stays_atomic() -> None:
    app = QQApp(MagicMock())
    text = "今天下雨了。\n\n记得带伞。"

    owner = app._plan_final_reply({"companion_context": {"actor_id": "linwan"}}, text)
    group = app._plan_final_reply({"companion_context": None}, text)

    assert owner.segments == ("今天下雨了。", "记得带伞。")
    assert group.segments == (text,)


def test_proposal_fan_out_stays_atomic_per_proposal() -> None:
    app = QQApp(MagicMock())
    text = "【方案一】先做 A。\n\n【方案二】先做 B。"

    batch = app._plan_final_reply({"companion_context": {"actor_id": "linwan"}}, text)

    assert batch.segments == ("【方案一】先做 A。", "【方案二】先做 B。")
