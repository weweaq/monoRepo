"""Heartbeat contract tests: file schema, state semantics, isolation (design §5)."""

from __future__ import annotations

import json
from pathlib import Path

from gacore.heartbeat import DEFAULT_INTERVAL_SECONDS, Heartbeat, run_dir


def _read(root: Path, component: str = "scheduler") -> dict:
    return json.loads((root / f"{component}.json").read_text(encoding="utf-8"))


def test_start_beat_stop_writes_contract(tmp_path: Path) -> None:
    root = tmp_path / "run"
    hb = Heartbeat("scheduler", root=root, interval_sec=30)
    hb.start(poll_interval=30)
    first = _read(root)
    assert first["component"] == "scheduler"
    assert first["state"] == "starting"
    assert first["schema_version"] == 1
    assert first["interval_sec"] == 30
    assert first["stale_after_sec"] == 90
    assert first["detail"] == {"poll_interval": 30}
    assert first["seq"] == 1
    assert first["started_at"]
    assert first["updated_at_epoch"] > 0

    hb.beat(state="busy", stale_after_sec=1800, job="daily-report")
    second = _read(root)
    assert second["state"] == "busy"
    assert second["stale_after_sec"] == 1800
    assert second["seq"] > first["seq"]
    assert second["detail"] == {"job": "daily-report"}

    # No state arg: keep "busy"; no stale arg: back to the default window; detail replaces.
    hb.beat()
    third = _read(root)
    assert third["state"] == "busy"
    assert third["stale_after_sec"] == 90
    assert third["detail"] == {}

    hb.stop()
    fourth = _read(root)
    assert fourth["state"] == "stopped"

    # Atomic write: the temp file must never be left behind.
    assert list(root.glob(".*.tmp")) == []


def test_default_stale_after_has_floor_of_60(tmp_path: Path) -> None:
    hb = Heartbeat("main", root=tmp_path / "run", interval_sec=10)
    hb.start()
    assert _read(tmp_path / "run", "main")["stale_after_sec"] == 60
    assert DEFAULT_INTERVAL_SECONDS == 15


def test_stop_can_record_error_state(tmp_path: Path) -> None:
    root = tmp_path / "run"
    hb = Heartbeat("scheduler", root=root)
    hb.start()
    hb.stop(state="error", error_type="RuntimeError", error_message="boom")
    data = _read(root)
    assert data["state"] == "error"
    assert data["detail"] == {"error_type": "RuntimeError", "error_message": "boom"}


def test_write_failure_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "run"
    blocker.write_text("not a directory", encoding="utf-8")
    hb = Heartbeat("scheduler", root=blocker)
    hb.start()
    hb.beat(state="running")
    hb.stop()  # none of these may raise


def test_write_failure_logs_one_throttled_warning(tmp_path: Path, monkeypatch) -> None:
    """写失败要落一条 WARNING（含 error_type/error_message），且 60s 内只记一次。"""
    from gacore import heartbeat as heartbeat_mod

    emitted: list[dict] = []

    class _Capture:
        def warning(self, message: str, **fields: object) -> None:
            emitted.append({"message": message, **fields})

    monkeypatch.setattr(heartbeat_mod, "logger", _Capture())

    blocker = tmp_path / "run"
    blocker.write_text("not a directory", encoding="utf-8")
    hb = Heartbeat("scheduler", root=blocker)
    hb.start()
    hb.beat(state="running")
    hb.stop()  # 三次失败，但节流后只应有一条 WARNING

    assert len(emitted) == 1
    assert emitted[0]["message"] == "heartbeat write failed"
    assert emitted[0]["component"] == "scheduler"
    assert emitted[0]["error_type"]
    assert emitted[0]["error_message"]


def test_run_dir_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GACORE_RUN_DIR", str(tmp_path / "custom"))
    assert run_dir(tmp_path) == tmp_path / "custom"


def test_run_dir_relative_override_resolves_against_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GACORE_RUN_DIR", "run-custom")
    assert run_dir(tmp_path) == tmp_path / "run-custom"


def test_run_dir_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GACORE_RUN_DIR", raising=False)
    assert run_dir(tmp_path) == tmp_path / "data" / "run"


def test_instances_have_distinct_instance_ids(tmp_path: Path) -> None:
    root = tmp_path / "run"
    a = Heartbeat("scheduler", root=root)
    b = Heartbeat("scheduler", root=root)
    a.start()
    first_id = _read(root)["instance_id"]
    b.start()
    second = _read(root)
    assert second["instance_id"] != first_id
    assert second["seq"] == 1  # each instance counts its own writes
