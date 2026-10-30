"""Step 2 of Companion Self-Memory V1: the owner/master-switch config.

Companion only ever runs for one configured owner; a missing owner must leave the
feature off instead of guessing (design §4.1).
"""

from __future__ import annotations

from gacore.companion.config import CompanionConfig, load_companion_config


def test_disabled_by_default() -> None:
    """No environment keys means Companion is off and nobody is treated as owner."""
    cfg = load_companion_config({})

    assert cfg == CompanionConfig(enabled=False, owner_openid=None)
    assert cfg.active is False


def test_active_needs_both_switch_and_owner() -> None:
    """``active`` is the single gate: switch alone or owner alone is not enough."""
    assert CompanionConfig(enabled=True, owner_openid=None).active is False
    assert CompanionConfig(enabled=False, owner_openid="u1").active is False
    assert CompanionConfig(enabled=True, owner_openid="u1").active is True


def test_parses_truthy_switch_and_trims_owner() -> None:
    cfg = load_companion_config(
        {"COMPANION_MVP_ENABLED": "1", "COMPANION_OWNER_QQ_OPENID": "  u1  "}
    )

    assert cfg.enabled is True
    assert cfg.owner_openid == "u1"
    assert cfg.active is True


def test_zero_switch_is_off() -> None:
    assert load_companion_config({"COMPANION_MVP_ENABLED": "0"}).enabled is False


def test_blank_owner_is_none() -> None:
    """Whitespace-only owner is treated as unset, so the feature stays off."""
    cfg = load_companion_config(
        {"COMPANION_MVP_ENABLED": "true", "COMPANION_OWNER_QQ_OPENID": "   "}
    )

    assert cfg.owner_openid is None
    assert cfg.active is False
