"""Structured JSONL log for vector-store recall (and future rewrite) events.

Aim: a durable, machine-readable record of "what query hit the vector store and what
came back", so we can later stats/maintain/test retrieval offline — the same shape the
RAG recall baseline report uses. One JSONL line per recall attempt:

- ``variant``: ``"raw"`` / ``"rewritten"`` mark A/B experiments; ``None`` in production.
- ``input_query``: the user's original utterance.
- ``query_used``: the actual string that was embedded (== input_query unless rewritten).
- ``rewrite``: null / ``{"query": ..., "skipped_reason": ...}`` describing what happened.
- ``semantic`` / ``episodic``: rows that cleared the threshold, mirroring production
  ``recall_context`` semantics (``sim = 1 - dist``, ``threshold`` on cosine distance).

Pure stdlib + only the caller decides where lines land. Everything is testable without a
DB or embedding model.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

FILENAME: Final = "recall.jsonl"


def _now_iso(now: datetime | None) -> str:
    """ISO-8601 with +08:00 offset (matches the repo's east-8 data convention)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _cell(rows: list[dict]) -> list[dict]:
    """Snap a recall result into a serializable cell (keep only what we need)."""
    return [
        {
            "content": r.get("content"),
            "sim": round(1.0 - r["dist"], 3) if r.get("dist") is not None else None,
            "day": r.get("day"),
            "chunk_key": r.get("chunk_key"),
        }
        for r in rows
    ]


def build_record(
    input_query: str,
    *,
    query_used: str | None = None,
    semantic: list[dict] | None = None,
    episodic: list[dict] | None = None,
    rewrite: dict | None = None,
    variant: str | None = None,
    session: str = "",
    model: str = "",
    k: int = 3,
    threshold: float = 0.5,
    day_from: str | None = None,
    day_to: str | None = None,
    ts: datetime | None = None,
) -> dict:
    """Build one canonical recall record (pure — no I/O)."""
    sem = _cell(semantic or [])
    epi = _cell(episodic or [])
    timestamp = _now_iso(ts)
    return {
        "ts": timestamp,
        "day": timestamp[:10],
        "session": session,
        "model": model,
        "variant": variant,
        "input_query": input_query,
        "query_used": query_used or input_query,
        "rewrite": rewrite,
        "params": {
            "k": k,
            "threshold": threshold,
            "day_from": day_from,
            "day_to": day_to,
        },
        "semantic": sem,
        "episodic": epi,
        "injected": bool(sem or epi),
        "n_sem": len(sem),
        "n_epi": len(epi),
        "sim_min": round(min(c["sim"] for c in sem + epi), 3) if (sem or epi) else None,
        "sim_max": round(max(c["sim"] for c in sem + epi), 3) if (sem or epi) else None,
    }


def build_sync_failure(
    *,
    error_type: str,
    error: str,
    step: str,
    extra_line: str = "",
    ts: datetime | None = None,
) -> dict:
    """Build one canonical vector-sync failure record (pure — mirrors recall events).

    Distinct from ``build_record``: this is a *write-path* diagnostic (a ``sync_portrait`` /
    ``sync_line`` step threw), not a recall event, so the record is tagged ``event:
    "sync_failure"`` and holds no query/sim shape. Swallowing the original exception is the
    existing production contract (txt is the source of truth); the only gap this closes is
    making the swallow *observable* — threaded into the same daily JSONL the recall logs
    land in, easy to grep / count for "vector sync silently off".
    """
    timestamp = _now_iso(ts)
    return {
        "ts": timestamp,
        "day": timestamp[:10],
        "event": "sync_failure",
        "step": step,
        "error_type": error_type,
        "error": error,
        "extra_line": extra_line,
    }


def record_line(record: dict) -> str:
    """Serialize one record to a single JSONL line (no trailing newline)."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def iter_records(base_dir: Path, days: int | None = None) -> list[dict]:
    """Read all recall.jsonl lines under ``base_dir`` (optionally limited to recent days)."""
    base_dir = Path(base_dir)
    records: list[dict] = []
    if not base_dir.is_dir():
        return records
    paths = sorted(base_dir.glob("*/" + FILENAME))
    if days is not None and paths:
        cutoff = date.today().toordinal() - days
        paths = [p for p in paths if _path_day(p) >= cutoff]
    for p in paths:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _path_day(p: Path) -> int:
    try:
        return date(int(p.parent.name[:4]), int(p.parent.name[5:7]), int(p.parent.name[8:10])).toordinal()
    except Exception:  # noqa: BLE001 — malformed dir names are not our records
        return 0


class RecallLog:
    """Append JSONL records to daily files under ``base_dir`` (e.g. logs/)."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def _file_for(self, record: dict) -> Path:
        day = record.get("day") or record.get("ts", "")[:10] or date.today().isoformat()
        return self.base_dir / day / FILENAME

    def append(self, record: dict) -> Path:
        target = self._file_for(record)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(record_line(record) + "\n")
        return target

    def append_many(self, records: list[dict]) -> int:
        for rec in records:
            self.append(rec)
        return len(records)


def summarize(records: list[dict]) -> dict[str, Any]:
    """Aggregate a batch of records into a compact stats dict (for maintenance/testing)."""
    if not records:
        return {"total": 0}
    eligible = [r for r in records if r.get("input_query")]
    injected = [r for r in eligible if r.get("injected")]
    all_sims = [s for r in injected for s in [r.get("sim_min"), r.get("sim_max")] if s is not None]
    n = len(eligible)
    return {
        "total": len(records),
        "eligible_queries": n,
        "injected": len(injected),
        "empty": n - len(injected),
        "inject_rate": round(len(injected) / n, 3) if n else None,
        "avg_max_sim": round(sum(r["sim_max"] for r in injected if r.get("sim_max")) / len(injected), 3) if injected and any(r.get("sim_max") for r in injected) else None,
        "sim_min": round(min(all_sims), 3) if all_sims else None,
        "sim_max": round(max(all_sims), 3) if all_sims else None,
    }


__all__ = (
    "FILENAME",
    "RecallLog",
    "build_record",
    "build_sync_failure",
    "iter_records",
    "record_line",
    "summarize",
)