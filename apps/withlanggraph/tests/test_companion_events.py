"""Tests for Companion V1 owner C2C ingress events (Step 4).

The write happens inside ``QQApp.on_message`` before any business routing, so the
tests drive the real handler with a fake graph and a temp-rooted Config, then read
the event rows straight from the SQLite store.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


from gacore.companion.config import CompanionConfig
from gacore.companion.identity import DEFAULT_ACTOR_ID, actor_id_for
from gacore.companion.store import CompanionStore
from gacore.companion.summary import minimize_user_message
from gacore.config import Config
from gacore.frontends import qq

OWNER = "owner-openid"


class _StubConfig:
    """Replace ``qq.Config`` so every ``Config.default()`` in the handler stays in tmp_path."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def default(self) -> Config:
        return self._cfg


def _msg(msg_id, content, *, user: str = OWNER) -> MagicMock:
    msg = MagicMock()
    msg.id = msg_id
    msg.content = content
    msg.author = MagicMock()
    msg.author.user_openid = user
    msg.author.member_openid = user
    msg.author.id = user
    msg.attachments = []
    return msg


def _app() -> qq.QQApp:
    app = qq.QQApp(MagicMock())
    app.client = MagicMock()
    app.send_text = AsyncMock()
    return app


def _rows(cfg: Config) -> list[dict]:
    store = CompanionStore.from_config(cfg)
    with store._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM companion_event ORDER BY seq")]


@pytest.fixture
def owner_env(monkeypatch, tmp_path: Path) -> Config:
    cfg = Config.for_tests(tmp_path)
    monkeypatch.setenv("COMPANION_MVP_ENABLED", "1")
    monkeypatch.setenv("COMPANION_OWNER_QQ_OPENID", OWNER)
    monkeypatch.setattr(qq, "Config", _StubConfig(cfg))
    # Keep the handler off the real data/ dir; only the Companion store is under test.
    monkeypatch.setattr(qq, "_record_known_user", lambda user_id: None)
    monkeypatch.setattr(qq, "record_last_active", lambda user_id, *a, **kw: None)
    monkeypatch.setattr(qq, "record_user_emotion", lambda user_id, text, *a, **kw: None)
    qq._processed_ids.clear()
    qq._user_card.clear()
    qq._companion_stores.clear()
    return cfg


class TestSummary:
    def test_collapses_whitespace(self) -> None:
        assert minimize_user_message("  第一行\n\n  第二行\t ") == "第一行 第二行"

    def test_caps_at_two_hundred_chars(self) -> None:
        out = minimize_user_message("x" * 300)
        assert len(out) == 200
        assert out.endswith("…")
        assert minimize_user_message("y" * 200) == "y" * 200

    def test_redacts_credentials(self) -> None:
        token = "sk-" + "a" * 24
        out = minimize_user_message(f"我的 key 是 {token}")
        assert token not in out
        assert "***" in out

    def test_empty_text_stays_empty(self) -> None:
        assert minimize_user_message("") == ""

    def test_rejects_non_positive_limit(self) -> None:
        with pytest.raises(ValueError):
            minimize_user_message("x", max_chars=0)


class TestActor:
    def test_owner_without_card_uses_default_actor(self) -> None:
        active = CompanionConfig(enabled=True, owner_openid=OWNER)
        assert actor_id_for(OWNER, active, None) == DEFAULT_ACTOR_ID
        assert actor_id_for(OWNER, active, "   ") == DEFAULT_ACTOR_ID

    def test_owner_with_card_uses_card(self) -> None:
        active = CompanionConfig(enabled=True, owner_openid=OWNER)
        assert actor_id_for(OWNER, active, "linwan") == "linwan"

    def test_non_owner_and_disabled_are_skipped(self) -> None:
        active = CompanionConfig(enabled=True, owner_openid=OWNER)
        assert actor_id_for("stranger", active, "linwan") is None
        assert actor_id_for(OWNER, CompanionConfig(enabled=False, owner_openid=OWNER), None) is None


class TestIngress:
    async def test_owner_command_writes_one_event(self, owner_env: Config) -> None:
        app = _app()
        with patch.object(qq, "_ALLOWED", frozenset({OWNER})):
            await app.on_message(_msg("m-help", "/help"), is_group=False)

        rows = _rows(owner_env)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "user.message.received"
        assert row["actor_id"] == DEFAULT_ACTOR_ID
        assert row["principal_id"] == "owner"
        assert row["source_id"] == "m-help"
        assert row["summary"] == "/help"

    async def test_owner_card_becomes_actor(self, owner_env: Config) -> None:
        qq._user_card[OWNER] = "linwan"
        app = _app()
        with patch.object(qq, "_ALLOWED", frozenset({OWNER})):
            await app.on_message(_msg("m-card", "/help"), is_group=False)

        assert [r["actor_id"] for r in _rows(owner_env)] == ["linwan"]

    @pytest.mark.parametrize(
        "content",
        ["/new", "别问了，不想回答", "4 3", "在吗", "今天想聊聊最近的计划"],
    )
    async def test_every_business_branch_records_once(self, owner_env: Config, monkeypatch, content: str) -> None:
        app = _app()
        # Neutralize the downstream branches so the test only observes the ingress write.
        monkeypatch.setattr(qq, "is_mute_request", lambda text: text.startswith("别问了"))
        monkeypatch.setattr(qq, "trivial_detect", lambda text: text == "在吗")
        monkeypatch.setattr(qq, "feedback_route", lambda text: None)
        monkeypatch.setattr(qq, "try_weekly_interaction", lambda cfg, text: None)
        monkeypatch.setattr(qq, "try_answer", lambda cfg, text, allow_bare=True: None)
        monkeypatch.setattr(app, "_mute_and_reply", AsyncMock())
        monkeypatch.setattr(app, "_trivial_reply", AsyncMock())
        monkeypatch.setattr(app, "_enqueue_agent_turn", MagicMock())

        with patch.object(qq, "_ALLOWED", frozenset({OWNER})):
            await app.on_message(_msg(f"m-{content}", content), is_group=False)

        rows = _rows(owner_env)
        assert len(rows) == 1
        assert rows[0]["summary"] == content

    async def test_group_message_is_not_recorded(self, owner_env: Config) -> None:
        app = _app()
        with patch.object(qq, "_ALLOWED", frozenset({OWNER})):
            await app.on_message(_msg("m-group", "/help"), is_group=True)

        assert _rows(owner_env) == []

    async def test_non_owner_is_not_recorded(self, owner_env: Config) -> None:
        app = _app()
        with patch.object(qq, "_ALLOWED", frozenset({OWNER, "stranger"})):
            await app.on_message(_msg("m-stranger", "/help", user="stranger"), is_group=False)

        assert _rows(owner_env) == []

    async def test_image_message_is_not_recorded(self, owner_env: Config, monkeypatch) -> None:
        app = _app()
        monkeypatch.setattr(qq, "_extract_image_urls", lambda data: ["https://img.example/a.png"])
        monkeypatch.setattr(app, "_handle_images_v2", AsyncMock())

        with patch.object(qq, "_ALLOWED", frozenset({OWNER})):
            await app.on_message(_msg("m-img", "看图"), is_group=False)

        assert _rows(owner_env) == []

    async def test_empty_text_is_not_recorded(self, owner_env: Config) -> None:
        app = _app()
        with patch.object(qq, "_ALLOWED", frozenset({OWNER})):
            await app.on_message(_msg("m-empty", "   "), is_group=False)

        assert _rows(owner_env) == []

    async def test_duplicate_message_id_writes_once(self, owner_env: Config) -> None:
        app = _app()
        with patch.object(qq, "_ALLOWED", frozenset({OWNER})):
            await app.on_message(_msg("m-dup", "/help"), is_group=False)
            qq._processed_ids.clear()  # force the DB idempotency key, not the process dedupe
            await app.on_message(_msg("m-dup", "/help"), is_group=False)

        assert len(_rows(owner_env)) == 1

    async def test_store_failure_does_not_block_chat(self, owner_env: Config, monkeypatch) -> None:
        app = _app()

        def _boom(cfg):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(qq, "_companion_store", _boom)

        with patch.object(qq, "_ALLOWED", frozenset({OWNER})):
            await app.on_message(_msg("m-fail", "/help"), is_group=False)

        app.send_text.assert_awaited()
        assert _rows(owner_env) == []
