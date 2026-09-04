"""Tests for gacore.tools.qq_tools.qq_push — fully mocked, no real botpy / network."""

from __future__ import annotations

import sys
import types

import gacore.tools.qq_tools as qq_tools_mod

# Ensure we have the real module (not a tool shadowed in __init__.py).
if not isinstance(qq_tools_mod, types.ModuleType):
    qq_tools_mod = sys.modules["gacore.tools.qq_tools"]

from gacore.tools.qq_tools import qq_push  # noqa: E402

_USERS = {
    "openid_A": {"first_seen": "2026-08-22T23:18:39+08:00", "last_seen": "2026-08-22T23:18:39+08:00"},
    "openid_B": {"first_seen": "2026-08-22T23:19:00+08:00", "last_seen": "2026-08-22T23:19:00+08:00"},
}


def test_registered_in_tool_names():
    """工具必须在 registry 中注册（外挂式：import + TOOL_NAMES + _TOOLS 三处）。"""
    from gacore.tools import TOOL_NAMES, build_tool_list

    assert "qq_push" in TOOL_NAMES
    names = [t.name for t in build_tool_list(None)]
    assert "qq_push" in names


def test_args_schema_exposes_only_message_and_to():
    """LLM 可见参数只有 message/to；注入 seam（_send_factory）必须被排除。"""
    schema = qq_push.get_input_schema().model_json_schema()
    props = set(schema.get("properties", {}).keys())
    assert props == {"message", "to"}
    assert "message" in schema.get("required", [])


def test_push_success_default_recipients(monkeypatch):
    """缺省 to：推给全部已知用户；结果含各用户 message id。"""
    monkeypatch.setattr(qq_tools_mod, "load_known_users", lambda: dict(_USERS))

    def fake_send(content, targets, sandbox):
        assert sandbox is False
        return {
            "ok": 2,
            "failures": [],
            "errors": {},
            "ids": {"openid_A": "id-1", "openid_B": "id-2"},
        }

    monkeypatch.setattr(qq_tools_mod, "_default_send", fake_send)
    result = qq_push.invoke({"message": "昨日画像报告"})
    assert result["status"] == "sent"
    assert result["ok"] == 2
    assert result["to"] == ["openid_A", "openid_B"]
    assert result["failures"] == []
    assert result["ids"] == {"openid_A": "id-1", "openid_B": "id-2"}


def test_push_success_explicit_to(monkeypatch):
    """指定 to：只推给给定 openid，且内容原样透传。"""
    monkeypatch.setattr(qq_tools_mod, "load_known_users", lambda: dict(_USERS))
    captured: dict = {}

    def fake_send(content, targets, sandbox):
        captured["content"] = content
        captured["targets"] = targets
        return {"ok": 1, "failures": [], "errors": {}, "ids": {"openid_B": "id-B"}}

    monkeypatch.setattr(qq_tools_mod, "_default_send", fake_send)
    result = qq_push.invoke({"message": "今晚记得收衣服", "to": " openid_B "})
    assert result["status"] == "sent"
    assert captured["targets"] == ["openid_B"]
    assert captured["content"] == "今晚记得收衣服"


def test_push_no_recipients(monkeypatch):
    """无已知用户：返回 no_recipients，不调用发送器。"""
    monkeypatch.setattr(qq_tools_mod, "load_known_users", lambda: {})
    called = False

    def fake_send(content, targets, sandbox):
        nonlocal called
        called = True
        return {"ok": 0, "failures": [], "errors": {}, "ids": {}}

    monkeypatch.setattr(qq_tools_mod, "_default_send", fake_send)
    result = qq_push.invoke({"message": "hi"})
    assert result["error"] == "no_recipients"
    assert called is False


def test_push_sender_error_passthrough(monkeypatch):
    """发送器返回 error（如未配置/超时）：透传 error dict。"""
    monkeypatch.setattr(qq_tools_mod, "load_known_users", lambda: dict(_USERS))

    def fake_send(content, targets, sandbox):
        return {"error": "qq_not_configured", "message": "缺少 QQ_APP_ID / QQ_APP_SECRET"}

    monkeypatch.setattr(qq_tools_mod, "_default_send", fake_send)
    result = qq_push.invoke({"message": "hi"})
    assert result["error"] == "qq_not_configured"
    assert result["to"] == ["openid_A", "openid_B"]


def test_push_all_failed(monkeypatch):
    """全部失败：返回 all_failed，附每用户错误明细。"""
    monkeypatch.setattr(qq_tools_mod, "load_known_users", lambda: dict(_USERS))

    def fake_send(content, targets, sandbox):
        return {
            "ok": 0,
            "failures": ["openid_A", "openid_B"],
            "errors": {"openid_A": "11264 频控", "openid_B": "timeout"},
            "ids": {},
        }

    monkeypatch.setattr(qq_tools_mod, "_default_send", fake_send)
    result = qq_push.invoke({"message": "hi"})
    assert result["error"] == "all_failed"
    assert "11264" in result["message"]


def test_push_partial_failure(monkeypatch):
    """部分失败：status=sent，failures 列出失败者。"""
    monkeypatch.setattr(qq_tools_mod, "load_known_users", lambda: dict(_USERS))

    def fake_send(content, targets, sandbox):
        return {
            "ok": 1,
            "failures": ["openid_B"],
            "errors": {"openid_B": "timeout"},
            "ids": {"openid_A": "id-A"},
        }

    monkeypatch.setattr(qq_tools_mod, "_default_send", fake_send)
    result = qq_push.invoke({"message": "hi"})
    assert result["status"] == "sent"
    assert result["ok"] == 1
    assert result["to"] == ["openid_A"]
    assert result["failures"] == ["openid_B"]


def test_push_truncates_overlong_message_and_flags(monkeypatch):
    """L-1: over-length message is cut to _PUSH_MAX_CHARS and flagged truncated=True."""
    monkeypatch.setattr(qq_tools_mod, "load_known_users", lambda: dict(_USERS))
    captured: dict = {}

    def fake_send(content, targets, sandbox):
        captured["content"] = content
        return {"ok": 2, "failures": [], "errors": {}, "ids": {"openid_A": "id-1", "openid_B": "id-2"}}

    monkeypatch.setattr(qq_tools_mod, "_default_send", fake_send)
    long_msg = "长" * (qq_tools_mod._PUSH_MAX_CHARS + 50)
    result = qq_push.invoke({"message": long_msg})
    assert result["status"] == "sent"
    assert result["truncated"] is True
    assert len(captured["content"]) == qq_tools_mod._PUSH_MAX_CHARS


def test_push_short_message_not_truncated(monkeypatch):
    """L-1: a within-limit message is delivered verbatim, no truncated flag set."""
    monkeypatch.setattr(qq_tools_mod, "load_known_users", lambda: dict(_USERS))

    def fake_send(content, targets, sandbox):
        return {"ok": 1, "failures": [], "errors": {}, "ids": {"openid_A": "id-A"}}

    monkeypatch.setattr(qq_tools_mod, "_default_send", fake_send)
    result = qq_push.invoke({"message": "短消息"})
    assert result["status"] == "sent"
    assert "truncated" not in result
