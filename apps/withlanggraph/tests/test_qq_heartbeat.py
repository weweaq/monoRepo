"""QQ frontend heartbeat tests (design docs/observability-design.md §6.5)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure `src` is importable and fake botpy BEFORE importing the module under test
# (same approach as test_qq.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_fake_botpy = types.ModuleType("botpy")


class _FakeIntents:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def none(cls):  # noqa: N805 - mirrors botpy.Intents.none()
        return cls()


class _FakeClient:
    def __init__(self, **kwargs):
        pass


_fake_botpy.Intents = _FakeIntents
_fake_botpy.Client = _FakeClient
_fake_message = types.ModuleType("botpy.message")
_fake_message.C2CMessage = type("C2CMessage", (), {})
_fake_message.GroupMessage = type("GroupMessage", (), {})
sys.modules.setdefault("botpy", _fake_botpy)
sys.modules.setdefault("botpy.message", _fake_message)

from gacore.frontends import qq  # noqa: E402
from gacore.frontends.qq import QQApp, _heartbeat_loop, _make_bot_class  # noqa: E402
from gacore.heartbeat import Heartbeat  # noqa: E402


@pytest.fixture(autouse=True)
def _complete_botpy_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the botpy stub used by the module under test to a complete one.

    ``test_qq.py`` installs its own ``botpy`` stub (no ``Client``) into
    ``sys.modules``; whichever stub is active when ``gacore.frontends.qq`` gets
    imported becomes the module-global ``qq.botpy``. Building a bot class
    (``_make_bot_class``) needs ``botpy.Client``, so patch it explicitly
    instead of depending on collection order.
    """
    monkeypatch.setattr(qq, "botpy", _fake_botpy)


def _read(root: Path) -> dict:
    return json.loads((root / "qq.json").read_text(encoding="utf-8"))


async def test_heartbeat_loop_keeps_last_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Periodic beats keep the state and re-carry the caller's detail (design §5.2)."""
    monkeypatch.setattr(qq, "DEFAULT_INTERVAL_SECONDS", 0.02)
    root = tmp_path / "run"
    hb = Heartbeat("qq", root=root)
    detail: dict[str, object] = {"retry_in_sec": 5}
    hb.start()
    hb.beat(state="reconnecting", **detail)

    task = asyncio.create_task(_heartbeat_loop(hb, detail))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    data = _read(root)
    assert data["state"] == "reconnecting"
    assert data["seq"] >= 3  # start + reconnecting + at least one periodic beat
    # 周期心跳必须带上调用方的 detail：connected_at 这类线索不能被下一拍冲掉。
    assert data["detail"] == {"retry_in_sec": 5}


async def test_connected_at_survives_periodic_beats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """on_ready 写入的 connected_at 不能被后续周期心跳清空。"""
    monkeypatch.setattr(qq, "DEFAULT_INTERVAL_SECONDS", 0.02)
    root = tmp_path / "run"
    app = QQApp(MagicMock())
    hb = Heartbeat("qq", root=root)
    hb.start()
    app._heartbeat = hb

    bot = _make_bot_class(app)()
    await bot.on_ready()

    task = asyncio.create_task(_heartbeat_loop(hb, app._hb_detail))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    data = _read(root)
    assert data["state"] == "running"
    assert "connected_at" in data["detail"]


async def test_on_ready_marks_heartbeat_running(tmp_path: Path) -> None:
    """`on_ready` is the only place that may mark the QQ heartbeat `running`."""
    root = tmp_path / "run"
    app = QQApp(MagicMock())
    hb = Heartbeat("qq", root=root)
    hb.start()
    hb.beat(state="connecting")
    app._heartbeat = hb

    bot = _make_bot_class(app)()
    await bot.on_ready()

    data = _read(root)
    assert data["state"] == "running"
    assert "connected_at" in data["detail"]


async def test_start_records_connecting_reconnect_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start(): connecting → reconnecting (client run failed) → stopped (cancelled)."""
    root = tmp_path / "run"
    monkeypatch.setenv("GACORE_RUN_DIR", str(root))
    monkeypatch.setattr(qq, "_RECONNECT_INITIAL", 0.01)
    monkeypatch.setattr(qq, "_RECONNECT_MAX", 0.01)
    # Keep the periodic beat out of the way so the reconnect detail is observable.
    monkeypatch.setattr(qq, "DEFAULT_INTERVAL_SECONDS", 5)

    fake_bot = MagicMock()

    async def failing_start(appid: str, secret: str) -> None:
        raise RuntimeError("boom")

    fake_bot.start = failing_start
    monkeypatch.setattr(qq, "_make_bot_class", MagicMock(return_value=lambda: fake_bot))

    app = QQApp(MagicMock())
    task = asyncio.create_task(app.start())
    await asyncio.sleep(0.15)
    data = _read(root)
    assert data["state"] == "reconnecting"
    assert data["detail"]["retry_in_sec"] == 0.01
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _read(root)["state"] == "stopped"
