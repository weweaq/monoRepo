"""End-to-end shape of a Companion V1 turn: prompt blocks, reply plan, batched send.

This wires the real system-prompt assembly and the real planner to a fake QQ sender,
so the three pieces that must agree (style block, self memory, 1-3 segments) are
verified together without network calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gacore.companion.reply_batch import plan_final_reply
from gacore.companion.store import CompanionStore, ReflectionDraft
from gacore.config import Config
from gacore.context import build_system_prompt
from gacore.frontends import qq
from gacore.frontends.qq import QQApp, SegmentReceipt, SendReceipt
from gacore.state import new_state


def _receipt(text: str) -> SendReceipt:
    return SendReceipt(True, (SegmentReceipt(True, text, None),))


def _owner_state(cfg: Config, actor_id: str = "linwan") -> dict:
    return new_state(
        "聊两句",
        cfg,
        active_card="hanli",
        companion_context={"actor_id": actor_id, "principal_id": "owner"},
    )


def _seed_reflection(cfg: Config, statement: str = "技术分享更容易得到回应") -> None:
    store = CompanionStore.from_config(cfg)
    events = [
        store.append_event(
            kind="user.message.received",
            actor_id="linwan",
            principal_id="owner",
            source_id=f"src-{i}",
            summary=f"event {i}",
            idempotency_key=f"e2e-{i}",
        ).event_id
        for i in range(2)
    ]
    store.commit_reflection_batch(
        actor_id="linwan",
        principal_id="owner",
        reflections=[ReflectionDraft(statement, 0.8, tuple(events))],
        prompt_version="companion-self-memory@v1",
        last_reflected_seq=2,
    )


def test_owner_turn_prompt_carries_style_and_self_memory(tmp_path: object) -> None:
    cfg = Config.for_tests(tmp_path)  # type: ignore[arg-type]
    _seed_reflection(cfg)

    prompt = build_system_prompt(_owner_state(cfg), cfg)

    assert "[即时聊天表达规则]" in prompt
    assert "=== Self memory" in prompt
    assert "技术分享更容易得到回应" in prompt
    # Background blocks stay behind the role card and ahead of the style rules.
    assert prompt.index("=== Self memory") < prompt.index("[即时聊天表达规则]")


def test_non_owner_turn_gets_neither_block(tmp_path: object) -> None:
    cfg = Config.for_tests(tmp_path)  # type: ignore[arg-type]

    prompt = build_system_prompt(new_state("聊两句", cfg, active_card="hanli"), cfg)

    assert "[即时聊天表达规则]" not in prompt
    assert "=== Self memory" not in prompt


@pytest.mark.asyncio
async def test_owner_chat_turn_plans_and_delivers_two_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qq, "_SEGMENT_INTERVAL_SECONDS", 0)
    app = QQApp(MagicMock())
    app.send_text = AsyncMock(side_effect=lambda chat_id, text, **kw: _receipt(text))

    batch = plan_final_reply("今天下雨了。\n\n记得带伞。", "companion")
    receipts = await app._deliver_reply_batch("owner-chat", batch, is_group=False)

    assert [r.segments[0].actual_text for r in receipts] == ["今天下雨了。", "记得带伞。"]


@pytest.mark.asyncio
async def test_technical_reply_is_one_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qq, "_SEGMENT_INTERVAL_SECONDS", 0)
    app = QQApp(MagicMock())
    app.send_text = AsyncMock(side_effect=lambda chat_id, text, **kw: _receipt(text))
    text = "修好了。\n\n```python\nprint('ok')\n```"

    batch = app._plan_final_reply({"companion_context": {"actor_id": "linwan"}}, text)
    receipts = await app._deliver_reply_batch("owner-chat", batch, is_group=False)

    assert len(receipts) == 1
    assert receipts[0].segments[0].actual_text == text
