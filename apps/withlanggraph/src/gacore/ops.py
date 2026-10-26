"""Operational status aggregation (P1 observability): one command answers "how now".

Reads the two signal layers built in P0 without touching any service:

- L1 liveness: ``data/run/<component>.json`` heartbeat files (see ``gacore.heartbeat``)
- L0 events: the newest ``etl.run.*`` / ``ingest failed`` records in ``logs/<day>/app.jsonl``

Usage::

    python -m gacore.ops status
    python -m gacore.ops status --json
    python -m gacore.ops status --root <app-root> --run-dir <dir> --logs-dir <dir>

Exit code is 0 when nothing is degraded, 1 otherwise (cron-friendly). A missing
heartbeat file is reported as ``missing`` but is not by itself degradation: the
langTrack server may legitimately run without the unified launcher.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from gacore.heartbeat import run_dir
from gacore.redact import redact_obj

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
_CST: Final = timezone(timedelta(hours=8))
COMPONENTS: Final = ("main", "scheduler", "qq", "langtrack_server")
_ETL_MESSAGES: Final = (
    "etl.run.finished",
    "etl.run.failed",
    "etl.run.interrupted",
    "etl.run.started",
)
_ETL_STATUS: Final = {
    "etl.run.finished": "ok",
    "etl.run.failed": "failed",
    "etl.run.interrupted": "interrupted",
    "etl.run.started": "running",
}
_TAIL_BYTES: Final = 256 * 1024
_LOOKBACK_DAYS: Final = 7
_BAD_ETL_STATUSES: Final = frozenset({"failed", "interrupted"})


def _now() -> datetime:
    return datetime.now(_CST)


def _iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def read_heartbeats(
    directory: Path | str,
    *,
    components: Sequence[str] = COMPONENTS,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Read every component heartbeat; missing/unreadable files never raise."""
    current = now or _now()
    base = Path(directory)
    out: dict[str, dict[str, Any]] = {}
    for name in components:
        path = base / f"{name}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            out[name] = {"state": "missing", "stale": False, "age_sec": None}
            continue
        except (OSError, json.JSONDecodeError) as exc:
            out[name] = {
                "state": "unreadable",
                "stale": False,
                "age_sec": None,
                "error_type": type(exc).__name__,
            }
            continue
        if not isinstance(payload, dict):
            out[name] = {"state": "unreadable", "stale": False, "age_sec": None}
            continue
        updated = payload.get("updated_at_epoch")
        age: float | None = None
        if isinstance(updated, (int, float)):
            age = round(current.timestamp() - float(updated), 1)
        stale_after = payload.get("stale_after_sec")
        state = str(payload.get("state") or "unknown")
        stale = bool(
            age is not None
            and isinstance(stale_after, (int, float))
            and age > stale_after
            and state != "stopped"
        )
        out[name] = {
            "state": state,
            "stale": stale,
            "age_sec": age,
            "stale_after_sec": stale_after,
            "pid": payload.get("pid"),
            "instance_id": payload.get("instance_id"),
            "updated_at": payload.get("updated_at"),
            "detail": redact_obj(payload.get("detail") or {}),
        }
    return out


def _tail_lines(path: Path, *, max_bytes: int = _TAIL_BYTES) -> list[str]:
    """Last ``max_bytes`` of a file as lines; a leading partial line is dropped."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def _day_dirs(logs_dir: Path) -> list[Path]:
    try:
        return sorted(
            (
                child
                for child in logs_dir.iterdir()
                if child.is_dir() and len(child.name) == 10 and child.name[4] == "-"
            ),
            key=lambda p: p.name,
            reverse=True,
        )[:_LOOKBACK_DAYS]
    except OSError:
        return []


def find_latest_record(logs_dir: Path | str, messages: Iterable[str]) -> dict[str, Any] | None:
    """Newest record whose ``message`` is in ``messages`` (scans newest day files)."""
    wanted = set(messages)
    for day_dir in _day_dirs(Path(logs_dir)):
        path = day_dir / "app.jsonl"
        if not path.exists():
            continue
        for line in reversed(_tail_lines(path)):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("message") in wanted:
                return redact_obj(record)
    return None


def _etl_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    message = str(record.get("message") or "")
    return {
        "status": _ETL_STATUS.get(message, "unknown"),
        "ts": record.get("ts"),
        "run_id": record.get("run_id"),
        "mode": record.get("mode"),
        "duration_ms": record.get("duration_ms"),
        "error_type": record.get("error_type"),
        "error_message": record.get("error_message"),
        "table_counts": record.get("table_counts"),
        "max_event_ts": record.get("max_event_ts"),
    }


def _ingest_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    return {
        "ts": record.get("ts"),
        "batch_id": record.get("batch_id"),
        "device_id": record.get("device_id"),
        "client_device_id": record.get("client_device_id"),
        "error_type": record.get("error_type"),
        "error_message": record.get("error_message"),
    }


def _proactive_summary(base: Path, now: datetime) -> dict[str, Any]:
    """P0 提问账本摘要：7 日回复率、按 scene 发出数、静音来源。

    数据来自两处只读文件：``data/qa/*.jsonl``（问题行的 asked_at/status/ignored_count）
    与 ``data/proactive_state.json``（reply_stats）。任何读取异常都降级为 error 字段，
    监控命令本身绝不能被账本文件拖垮。
    """
    try:
        from gacore import proactive, qa_store
        from gacore.config import Config

        cfg = Config(
            root=base,
            asset_dir=base / "config" / "assets",
            memory_dir=base / "memory",
            logs_dir=base / "logs",
            temp_dir=base / "temp",
        )
        reply = qa_store.reply_stats(cfg, now=now)
        state = proactive.load_state(cfg)
        muted = qa_store.muted_sources(cfg, now=now)
    except Exception as exc:  # noqa: BLE001 — status must always render
        return {"error_type": type(exc).__name__, "error_message": str(exc)}

    by_scene: dict[str, dict[str, int]] = {}
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        for scene, bucket in (entry.get("reply_stats") or {}).items():
            if not isinstance(bucket, dict):
                continue
            target = by_scene.setdefault(scene, {"asked": 0, "answered": 0, "ignored": 0})
            for field in ("asked", "answered", "ignored"):
                target[field] += int(bucket.get(field) or 0)
    # P1 回填 questions 场景：真实口径在 qa 账本（按问题条数），proactive_state 的
    # asked 只按消息条数记，两种单位不能混算，有账本就以账本为准。
    if reply.get("asked"):
        by_scene["questions"] = {
            "asked": int(reply.get("asked") or 0),
            "answered": int(reply.get("answered") or 0),
            "ignored": int(reply.get("ignored") or 0),
        }
    # P3: 来源静音来自 qa 账本的连续忽略链；全局静音（"别问了"）来自 state 的
    # muted_until。两者都只读，不改状态。
    global_muted = 0
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        until = proactive.muted_until_of(entry)
        if until is not None and until > now:
            global_muted += 1
    return {
        "reply": reply,
        "by_scene": by_scene,
        "muted_sources": sorted(muted),
        "muted_until": dict(muted),
        "global_muted_users": global_muted,
    }


def build_status(
    *,
    root: Path | str = _PROJECT_ROOT,
    heartbeat_directory: Path | str | None = None,
    logs_directory: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate heartbeats + latest ETL/ingest records into one verdict."""
    current = now or _now()
    base = Path(root)
    hb_dir = Path(heartbeat_directory) if heartbeat_directory else run_dir(base)
    logs_dir = (
        Path(logs_directory)
        if logs_directory
        else Path(os.environ.get("GACORE_LOGS_DIR") or base / "logs")
    )

    components = read_heartbeats(hb_dir, now=current)
    etl = _etl_summary(find_latest_record(logs_dir, _ETL_MESSAGES))
    ingest = {"last_failure": _ingest_summary(find_latest_record(logs_dir, ("ingest failed",)))}

    reasons: list[str] = []
    for name, beat in components.items():
        if beat["stale"]:
            reasons.append(f"{name} heartbeat stale (age={beat['age_sec']}s)")
        if beat["state"] == "error":
            reasons.append(f"{name} state=error")
    if etl and etl["status"] in _BAD_ETL_STATUSES:
        reasons.append(f"etl last run {etl['status']}")
    if all(beat["state"] == "missing" for beat in components.values()):
        reasons.append("no component heartbeat files")

    return {
        "checked_at": _iso(current),
        "root": str(base),
        "run_dir": str(hb_dir),
        "logs_dir": str(logs_dir),
        "components": components,
        "etl": etl,
        "ingest": ingest,
        "proactive": _proactive_summary(base, current),
        "degraded": bool(reasons),
        "reasons": reasons,
    }


def _render(status: dict[str, Any]) -> str:
    lines = [f"langTrack ops status  {status['checked_at']}"]
    lines.append(f"run dir: {status['run_dir']}")
    lines.append("")
    lines.append(f"{'COMPONENT':<18}{'STATE':<14}{'AGE':>9}  PID")
    for name, beat in status["components"].items():
        age = "-" if beat["age_sec"] is None else f"{beat['age_sec']:.1f}s"
        marker = " !STALE" if beat["stale"] else ""
        lines.append(
            f"{name:<18}{beat['state'] + marker:<14}{age:>9}  {beat.get('pid') or '-'}"
        )
    etl = status.get("etl")
    if etl:
        lines.append("")
        lines.append(
            f"ETL  last={etl['status']}  run_id={etl['run_id']}  mode={etl['mode']}  "
            f"duration_ms={etl['duration_ms']}  at={etl['ts']}"
        )
        if etl.get("error_type") or etl.get("error_message"):
            lines.append(f"     error: {etl.get('error_type')}: {etl.get('error_message')}")
    else:
        lines.append("")
        lines.append("ETL  no etl.run.* record in the last 7 days")
    failure = status["ingest"].get("last_failure")
    lines.append(
        "INGEST  last failure: none"
        if not failure
        else f"INGEST  last failure: {failure['ts']} {failure['error_type']}: {failure['error_message']}"
    )
    proactive = status.get("proactive") or {}
    if proactive.get("error_type"):
        lines.append(
            f"PROACTIVE  unavailable: {proactive['error_type']}: {proactive.get('error_message')}"
        )
    else:
        reply = proactive.get("reply") or {}
        lines.append(
            f"PROACTIVE  7d asked={reply.get('asked', 0)} answered={reply.get('answered', 0)} "
            f"ignored={reply.get('ignored', 0)} rate={float(reply.get('answer_rate') or 0.0):.0%} "
            f"today={reply.get('asked_today', 0)}"
        )
        for scene, bucket in sorted((proactive.get("by_scene") or {}).items()):
            lines.append(
                f"  scene {scene}: asked={bucket['asked']} answered={bucket['answered']} "
                f"ignored={bucket['ignored']}"
            )
        muted = proactive.get("muted_sources") or []
        if muted:
            lines.append(f"  muted sources: {', '.join(muted)}")
        if proactive.get("global_muted_users"):
            lines.append(f"  globally muted users: {proactive['global_muted_users']}")
    lines.append("")
    lines.append("OVERALL  " + ("DEGRADED" if status["degraded"] else "ok"))
    for reason in status["reasons"]:
        lines.append(f"  - {reason}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gacore.ops")
    sub = parser.add_subparsers(dest="command")
    status_parser = sub.add_parser("status", help="aggregate heartbeats + latest ETL/ingest")
    status_parser.add_argument("--json", action="store_true", help="machine-readable output")
    status_parser.add_argument("--root", type=Path, default=_PROJECT_ROOT)
    status_parser.add_argument("--run-dir", type=Path, default=None)
    status_parser.add_argument("--logs-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.command in (None, "status"):
        status = build_status(
            root=args.root,
            heartbeat_directory=getattr(args, "run_dir", None),
            logs_directory=getattr(args, "logs_dir", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
        else:
            print(_render(status))
        return 1 if status["degraded"] else 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
