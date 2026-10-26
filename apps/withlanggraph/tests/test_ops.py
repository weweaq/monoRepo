"""Ops status aggregation (P1): `python -m gacore.ops status`."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gacore import ops

CST = timezone(timedelta(hours=8))


def _write_heartbeat(
    directory: Path,
    name: str,
    *,
    state: str = "running",
    age_sec: float = 0,
    stale_after_sec: int = 60,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(CST)
    payload = {
        "schema_version": 1,
        "component": name,
        "instance_id": "abc12345",
        "pid": 4242,
        "state": state,
        "seq": 3,
        "started_at": current.isoformat(timespec="seconds"),
        "updated_at": current.isoformat(timespec="seconds"),
        "updated_at_epoch": current.timestamp() - age_sec,
        "interval_sec": 15,
        "stale_after_sec": stale_after_sec,
        "detail": {"note": "ok"},
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_logs(logs_dir: Path, day: str, records: list[dict]) -> None:
    path = logs_dir / day / "app.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def test_read_heartbeats_marks_stale_and_missing(tmp_path: Path) -> None:
    now = datetime.now(CST)
    _write_heartbeat(tmp_path, "main", age_sec=120, now=now)
    _write_heartbeat(tmp_path, "scheduler", age_sec=1, now=now)
    beats = ops.read_heartbeats(tmp_path, now=now)

    assert beats["main"]["stale"] is True
    assert beats["scheduler"]["stale"] is False
    assert beats["qq"]["state"] == "missing"
    assert beats["qq"]["stale"] is False


def test_stopped_component_is_not_stale(tmp_path: Path) -> None:
    now = datetime.now(CST)
    _write_heartbeat(tmp_path, "main", state="stopped", age_sec=3600, now=now)
    assert ops.read_heartbeats(tmp_path, now=now)["main"]["stale"] is False


def test_build_status_aggregates_latest_etl_and_ingest(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    logs_dir = tmp_path / "logs"
    _write_heartbeat(run_dir, "main", age_sec=1)
    _write_logs(
        logs_dir,
        "2026-10-21",
        [
            {"ts": "2026-10-21T09:00:00+08:00", "message": "etl.run.started", "run_id": "old"},
            {"ts": "2026-10-21T09:01:00+08:00", "message": "etl.run.finished", "run_id": "old", "status": "ok"},
        ],
    )
    _write_logs(
        logs_dir,
        "2026-10-22",
        [
            {"ts": "2026-10-22T08:00:00+08:00", "message": "ingest completed", "batch_id": "b1"},
            {
                "ts": "2026-10-22T08:30:00+08:00",
                "message": "ingest failed",
                "batch_id": "b2",
                "error_type": "RuntimeError",
                "error_message": "boom key=abcdefgh12",
            },
        ],
    )

    status = ops.build_status(root=tmp_path, heartbeat_directory=run_dir, logs_directory=logs_dir)

    assert status["etl"]["run_id"] == "old"
    assert status["etl"]["status"] == "ok"
    assert status["ingest"]["last_failure"]["batch_id"] == "b2"
    assert "abcdefgh12" not in status["ingest"]["last_failure"]["error_message"]
    assert status["degraded"] is False


def test_build_status_degrades_on_stale_or_failed_etl(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    logs_dir = tmp_path / "logs"
    _write_heartbeat(run_dir, "scheduler", age_sec=3600)
    _write_logs(
        logs_dir,
        "2026-10-22",
        [{"ts": "2026-10-22T08:00:00+08:00", "message": "etl.run.failed", "run_id": "r1"}],
    )

    status = ops.build_status(root=tmp_path, heartbeat_directory=run_dir, logs_directory=logs_dir)

    assert status["degraded"] is True
    assert any("scheduler heartbeat stale" in r for r in status["reasons"])
    assert any("etl last run failed" in r for r in status["reasons"])


def test_build_status_flags_nothing_running(tmp_path: Path) -> None:
    status = ops.build_status(
        root=tmp_path,
        heartbeat_directory=tmp_path / "run",
        logs_directory=tmp_path / "logs",
    )
    assert status["degraded"] is True
    assert "no component heartbeat files" in status["reasons"]


def test_build_status_includes_proactive_ledger(tmp_path: Path) -> None:
    """P0：ops 能看到 7 日提问/回答/忽略与真实回复率（来自 data/qa 账本）。"""
    from gacore.config import Config
    from gacore.qa_store import mark_pushed, open_questions, record_answer, seed_questions

    run_dir = tmp_path / "run"
    logs_dir = tmp_path / "logs"
    _write_heartbeat(run_dir, "main", age_sec=1)

    cfg = Config.for_tests(tmp_path)
    seed_questions(
        cfg,
        "2026-10-24",
        ["- [想问你-1] 问题甲（依据：10-24）", "- [想问你-2] 问题乙（依据：10-24）"],
    )
    keys = [r["question_key"] for r in open_questions(cfg)]
    mark_pushed(cfg, keys, now=datetime(2026, 10, 24, 8, 30, tzinfo=CST))
    record_answer(cfg, keys[0], "答案甲")

    status = ops.build_status(
        root=tmp_path,
        heartbeat_directory=run_dir,
        logs_directory=logs_dir,
        now=datetime(2026, 10, 25, 9, 0, tzinfo=CST),
    )
    proactive = status["proactive"]
    assert proactive["reply"]["asked"] == 2
    assert proactive["reply"]["answered"] == 1
    assert proactive["reply"]["answer_rate"] == 0.5
    # P1：questions 场景的真实口径回填自 qa 账本（按问题条数，而非消息条数）
    assert proactive["by_scene"]["questions"] == {"asked": 2, "answered": 1, "ignored": 0}
    assert proactive["muted_sources"] == []
    rendered = ops._render(status)
    assert "PROACTIVE  7d asked=2 answered=1 ignored=0 rate=50% today=0" in rendered


def test_main_json_and_exit_codes(tmp_path: Path, capsys) -> None:
    run_dir = tmp_path / "run"
    logs_dir = tmp_path / "logs"
    _write_heartbeat(run_dir, "main", age_sec=1)

    code = ops.main(
        ["status", "--json", "--root", str(tmp_path), "--run-dir", str(run_dir), "--logs-dir", str(logs_dir)]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["components"]["main"]["state"] == "running"

    _write_heartbeat(run_dir, "main", age_sec=time.time() + 60)
    code = ops.main(
        ["status", "--json", "--root", str(tmp_path), "--run-dir", str(run_dir), "--logs-dir", str(logs_dir)]
    )
    assert code == 1
