"""Simple scheduled-job runner for gacore: cron-like triggers that run the agent headless.

Each job is a single-turn agent run: the scheduler builds a fresh graph, feeds the job's
prompt as the sole user message, runs to completion (no ask_user interaction — scheduled
jobs must be self-contained), and writes the agent's final reply to a per-run output file
plus the daily note. State (last_run timestamps) persists in a JSON file so the loop
survives restarts without re-firing missed jobs.

Design choices for simplicity (per the user's "简单做就可以"):
  - Single-process polling loop (time.sleep), no Redis / no APScheduler / no threads.
  - Schedule spec is either "HH:MM" (daily at that time) or "every Nd" / "every Nh" / "every Nm"
    (interval). Not a full cron parser — covers the "daily report" use case.
  - Each job runs in its own thread_id so MemorySaver state never collides with the REPL.
  - Output goes to logs/scheduled/{job}_{timestamp}.md and edit_daily("today", ...).
  - deliver_to routes the finished reply to a channel: "file" (default; write output +
    daily note) or "email" (also send via send_email). Unknown channels fall back to file.
  - Missed jobs are NOT backfilled: on restart, a job whose scheduled time already passed
    today simply waits for the next occurrence (last_run is updated to "today" to prevent
    a catch-up storm). This mirrors the "thundering herd" mitigation from the user's notes.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from gacore.config import Config, load_dotenv
from gacore.jsonl_logger import get_logger
from gacore.proactive import PROACTIVE_POOL, proactive_due, run_proactive_job

logger = get_logger("scheduler")

_SCHEDULE_FILE: Final = "schedule.json"
_STATE_FILE: Final = "schedule_state.json"
_OUTPUT_SUBDIR: Final = "scheduled"
_TIME_RE: Final = re.compile(r"^(\d{1,2}):(\d{2})$")
_INTERVAL_RE: Final = re.compile(r"^every\s+(\d+)\s*([dhm])$", re.IGNORECASE)
_POLL_INTERVAL_SECONDS: Final = 30


@dataclass(slots=True)
class Job:
    """One scheduled job definition loaded from schedule.json.

    schedule is either "HH:MM" (daily) or "every N<d|h|m>" (interval).
    prompt is the self-contained instruction fed to the agent as the sole user message.
    deliver_to routes the finished reply to a channel: "file" (default) writes the output
    file + daily note, "email" additionally sends it via send_email; unknown values fall
    back to "file". email_to is the optional explicit recipient for the email channel —
    when empty the SMTP_TO / SMTP_USER env vars are used in that order.

    type == "proactive" marks a proactive-outreach job: run_loop dispatches it to the
    single-worker PROACTIVE_POOL instead of the blocking run_job path. window
    ("HH:MM-HH:MM", optional) restricts firing to that time-of-day window; an empty
    window means no restriction. cooldown_minutes (optional, 0 = off) enforces a
    minimum gap between consecutive triggers.
    """

    name: str
    schedule: str
    prompt: str
    enabled: bool = True
    deliver_to: str = "file"
    email_to: str = ""
    max_turns: int = 20
    type: str = "job"
    window: str = ""
    cooldown_minutes: int = 0
    scene: str = ""  # proactive scene key (morning/night/idle/...); empty = derive from name


@dataclass(slots=True)
class JobState:
    """Persisted runtime state for one job: last_run ISO timestamp and run count."""

    last_run: str = ""
    run_count: int = 0


@dataclass
class ScheduleResult:
    """Outcome of one job execution: exit_reason, output path, duration, error if any."""

    job_name: str
    exit_reason: str | None
    output_path: str | None
    duration_seconds: float
    error: str | None = None
    reply: str = ""


def _to_int(value: object, default: int = 0) -> int:
    """Parse an int config value, falling back to default on invalid input."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _log_proactive_failure(fut: object) -> None:
    """Log a top-level exception from a proactive worker (L1: never swallow)."""
    exc = fut.exception() if hasattr(fut, "exception") else None  # type: ignore[attr-defined]
    if exc is not None:
        logger.error(
            "proactive job raised top-level exception",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
        )


def load_jobs(cfg: Config) -> list[Job]:
    """Load enabled jobs from config/schedule.json; return empty list when missing or invalid."""
    path = cfg.asset_dir.parent / _SCHEDULE_FILE
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Failed to load schedule.json", error_type=type(e).__name__, stack_trace=str(e))
        return []
    jobs_raw = raw.get("jobs", []) if isinstance(raw, dict) else []
    jobs: list[Job] = []
    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        try:
            kind = item.get("type", "job")
            if kind == "proactive":
                # M2: proactive jobs are scene-driven, so a missing prompt is allowed and
                # falls back to the scene-derived default inside build_proactive_prompt.
                prompt = item.get("prompt", "")
            else:
                # Non-proactive jobs keep the required-prompt contract: a malformed entry
                # (missing prompt) is still skipped below via KeyError.
                prompt = item["prompt"]
            jobs.append(
                Job(
                    name=item["name"],
                    schedule=item["schedule"],
                    prompt=prompt,
                    enabled=item.get("enabled", True),
                    deliver_to=item.get("deliver_to", "file"),
                    email_to=item.get("email_to", ""),
                    max_turns=item.get("max_turns", 20),
                    type=kind,
                    window=item.get("window", ""),
                    cooldown_minutes=_to_int(item.get("cooldown_minutes", 0)),
                    scene=item.get("scene", ""),
                )
            )
        except (KeyError, TypeError) as e:
            logger.warning("Skipping malformed job", job=item, error=str(e))
    return [j for j in jobs if j.enabled]


def load_state(cfg: Config) -> dict[str, JobState]:
    """Load persisted job states from memory/schedule_state.json; empty dict when missing."""
    path = cfg.memory_dir / _STATE_FILE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    states: dict[str, JobState] = {}
    for name, data in raw.items():
        if isinstance(data, dict):
            states[name] = JobState(
                last_run=data.get("last_run", ""),
                run_count=data.get("run_count", 0),
            )
    return states


def save_state(cfg: Config, states: dict[str, JobState]) -> None:
    """Persist job states to memory/schedule_state.json (atomic best-effort)."""
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.memory_dir / _STATE_FILE
    payload = {name: asdict(s) for name, s in states.items()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def next_run_time(schedule: str, last_run: str | None, now: datetime) -> datetime | None:
    """Compute the next due time for a schedule spec.

    Supports:
      - "HH:MM" → daily at that time. If today's slot already passed (and last_run was today),
        returns tomorrow at HH:MM.
      - "every N<m|h|d>" → interval from last_run (or now if never run).

    Returns None for unparseable schedules.
    """
    # Interval form: "every 30m", "every 6h", "every 2d"
    m = _INTERVAL_RE.match(schedule)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        base = _parse_iso(last_run) if last_run else now
        candidate = base + delta
        # If the candidate is already in the past, walk forward in delta steps until future.
        while candidate <= now:
            candidate += delta
        return candidate

    # Daily time form: "HH:MM"
    m = _TIME_RE.match(schedule)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        today_slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        last = _parse_iso(last_run) if last_run else None
        # If never ran or last run was before today's slot, and slot hasn't passed → run today.
        if last is None or last < today_slot:
            if now >= today_slot:
                return today_slot
            return today_slot
        # Already ran today's slot → next is tomorrow.
        return today_slot + timedelta(days=1)

    return None


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp string; return None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def is_due(job: Job, state: JobState, now: datetime) -> bool:
    """Return True when the job should fire right now (now >= next_run_time)."""
    nxt = next_run_time(job.schedule, state.last_run, now)
    if nxt is None:
        return False
    return now >= nxt


def run_job(
    job: Job,
    cfg: Config,
    graph_runner: Callable[[str, Config, int], str | None] | None = None,
) -> ScheduleResult:
    """Execute one job: run the agent headless, capture reply, write output + daily note.

    graph_runner is the injection seam for tests: production passes None (uses the real
    build_graph + run_once), tests pass a fake that returns a canned reply without LLM.

    The reply is extracted from the final state's last AIMessage content.
    """
    start = time.monotonic()
    name = job.name
    logger.info("Job started", job=name, schedule=job.schedule)
    error: str | None = None
    reply = ""
    exit_reason: str | None = None
    try:
        if graph_runner is None:
            exit_reason, reply = _default_graph_runner(job.prompt, cfg, job.max_turns)
        else:
            exit_reason = graph_runner(job.prompt, cfg, job.max_turns)
            reply = f"[test reply for {name}]"
    except Exception as e:  # noqa: BLE001 — scheduler must not crash on one job failure
        error = f"{type(e).__name__}: {e}"
        logger.error("Job failed", job=name, error_type=type(e).__name__, stack_trace=str(e))
        exit_reason = "AGENT_ERROR"

    duration = time.monotonic() - start
    output_path = _write_output(cfg, job, reply, error)
    _write_daily_note(cfg, job, reply, error)
    _deliver(job, cfg, reply, error)

    # Cross-day rollover: after a successful daily-report run, export an onboard
    # memory pack (recent daily summaries + long-term persona) for the QQ frontend
    # to consume on the first message of the new day. Best-effort only — a failure
    # here must never block the report itself.
    if error is None and "daily" in job.name.lower():
        try:
            _export_onboard_pack(cfg)
        except Exception as e:  # noqa: BLE001 — pack export must never break the job
            logger.error("onboard pack export failed", job=name, error_type=type(e).__name__, stack_trace=str(e))

    logger.info(
        "Job finished",
        job=name,
        exit_reason=exit_reason,
        duration_seconds=round(duration, 2),
        output_path=output_path,
    )
    return ScheduleResult(
        job_name=name,
        exit_reason=exit_reason,
        output_path=output_path,
        duration_seconds=duration,
        error=error,
        reply=reply,
    )


def _default_graph_runner(prompt: str, cfg: Config, max_turns: int) -> tuple[str | None, str]:
    """Build a fresh graph and run the prompt as a single-turn headless agent run.

    Returns (exit_reason, reply_text) — the reply is extracted from the last AIMessage
    in the final state. Scheduled jobs are single-turn, so the last AI message is the
    agent's final answer.
    """
    from langchain_core.messages import AIMessage

    from gacore.graph import build_graph, run_once

    graph = build_graph(cfg=cfg)
    thread_id = f"sched-{uuid.uuid4().hex[:8]}"
    state = run_once(graph, prompt, thread_id=thread_id, max_turns=max_turns)
    exit_reason = state.get("exit_reason")
    reply = ""
    messages = state.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and isinstance(msg.content, str) and msg.content:
            reply = msg.content
            break
    return exit_reason, reply


_REPLY_CACHE: Final = "_last_scheduled_reply"  # legacy; kept for backward-compat of state files


def _write_output(cfg: Config, job: Job, reply: str, error: str | None) -> str | None:
    """Write the job's reply to logs/scheduled/{job}_{timestamp}.md; return the path string."""
    out_dir = cfg.logs_dir / _OUTPUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{job.name}_{ts}.md"
    lines = [
        f"# Scheduled Job: {job.name}",
        f"- time: {datetime.now(UTC).astimezone().isoformat(timespec='seconds')}",
        f"- schedule: {job.schedule}",
        f"- error: {error or 'none'}",
        "",
        "## Prompt",
        "",
        job.prompt,
        "",
        "## Reply",
        "",
        reply or "(empty reply)",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    return str(path)


def _write_daily_note(cfg: Config, job: Job, reply: str, error: str | None) -> None:
    """Append a bullet to today's daily note recording this scheduled run."""
    # Imported here to avoid a circular import at module load (daily_notes imports config).
    from gacore.tools.daily_notes import edit_daily

    today = datetime.now(UTC).astimezone().date().isoformat()
    status = f"FAILED ({error})" if error else "OK"
    snippet = (reply or "")[:200].replace("\n", " ")
    bullet = f"- [scheduled:{job.name}] {status} — {snippet}"
    # Try to append; if the note exists, append via last-line replacement.
    # If it doesn't exist, create it with the bullet.
    existing = edit_daily.func(date=today, old_str="", new_str=bullet, _cfg=cfg)
    if isinstance(existing, dict) and existing.get("error") == "empty_old_str":
        # Note already exists — append by replacing the header's trailing content.
        # Simplest: read, append, write (daily_notes doesn't have a pure append mode,
        # so we use the header line as the old_str anchor).
        from gacore.tools.daily_notes import read_daily

        content = read_daily.func(date=today, _cfg=cfg)
        if isinstance(content, str) and content:
            # Anchor on the last non-empty line.
            lines = [ln for ln in content.splitlines() if ln.strip()]
            if lines:
                anchor = lines[-1]
                edit_daily.func(date=today, old_str=anchor, new_str=anchor + "\n" + bullet, _cfg=cfg)


def _onboard_pack_path(cfg: Config) -> Path:
    """Return the onboard memory pack path (data/onboard_pack.json)."""
    return cfg.root / "data" / "onboard_pack.json"


def _load_active_qq_thread(cfg: Config) -> str:
    """Return the first active QQ thread id from data/qq_user_threads.json, if any."""
    path = cfg.root / "data" / "qq_user_threads.json"
    if not path.is_file():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    for thread in raw.values():
        if isinstance(thread, str) and thread:
            return thread
    return ""


def _long_term_insight(cfg: Config) -> str:
    """Return the long-term persona text from memory/global_mem_insight.txt.

    Falls back to any memory/global_mem*.txt file when the insight file is absent.
    """
    path = cfg.memory_dir / "global_mem_insight.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    candidates = sorted(cfg.memory_dir.glob("global_mem*.txt"))
    for p in candidates:
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    return ""


def _summarize_long_term(text: str, limit_lines: int = 40) -> str:
    """Compress the long-term persona into a compact summary when inject_full is off."""
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= limit_lines:
        return text
    return "\n".join(lines[:limit_lines]) + "\n...(画像较长,已按摘要截断,完整内容见 memory/global_mem_insight.txt)"


def _export_onboard_pack(cfg: Config) -> None:
    """Assemble and write data/onboard_pack.json after a successful daily-report run.

    The pack carries the recent N days of daily-note summaries plus the long-term
    persona, so the QQ frontend can inject "yesterday's memory" into the first
    message of the new day (see src/gacore/frontends/qq.py::_maybe_rollover).

    Same-name overwrite makes the export naturally idempotent. Raises on failure —
    callers wrap in try/except so a bad pack never blocks the report itself.
    """
    from gacore.tools.daily_notes import load_recent_daily_summaries

    days = cfg.rollover.recent_days
    daily_summary = load_recent_daily_summaries(cfg, days=days)
    insight_full = _long_term_insight(cfg)
    inject_full = cfg.rollover.inject_long_term_full
    long_term_md = insight_full if inject_full else _summarize_long_term(insight_full)
    now = datetime.now(UTC).astimezone()
    date = now.date().isoformat()
    pack = {
        "date": date,
        "created_at": now.isoformat(timespec="seconds"),
        "source_job": "daily-report",
        "prev_thread_id": _load_active_qq_thread(cfg),
        "payload": {
            "daily_summary_md": daily_summary,
            "long_term_md": long_term_md,
            "inject_full": inject_full,
            "recent_days": days,
        },
    }
    path = _onboard_pack_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pack, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="",
    )
    logger.info(
        "onboard pack exported for cross-day rollover",
        path=str(path),
        date=date,
        daily_days=days,
        inject_full=inject_full,
    )


def _resolve_email_recipient(job: Job, env: Mapping[str, str]) -> str:
    """Pick the email recipient: job.email_to, then SMTP_TO, then SMTP_USER (send to self)."""
    if job.email_to.strip():
        return job.email_to.strip()
    for key in ("SMTP_TO", "SMTP_USER"):
        value = env.get(key, "").strip()
        if value:
            return value
    return ""


def _email_body_html(reply: str, error: str | None) -> str:
    """Render the job reply as a minimal HTML email (escaped, pre-wrapped, error banner on top)."""
    status = f"<p style='color:#b00'><b>FAILED:</b> {html.escape(error)}</p>" if error else ""
    content = html.escape(reply or "(empty reply)")
    return (
        "<!DOCTYPE html><html><body>"
        f"{status}<pre style='font-family:ui-monospace,Consolas,monospace;white-space:pre-wrap;'>{content}</pre>"
        "</body></html>"
    )


def _deliver_email(job: Job, cfg: Config, reply: str, error: str | None, env: Mapping[str, str] | None = None) -> None:
    """Deliver the job's reply via send_email; never raises, logs the outcome.

    Recipient resolution and SMTP configuration follow send_email's rules (SMTP_* env
    vars); the only difference is the recipient defaults to SMTP_USER (send to self)
    when neither job.email_to nor SMTP_TO is set. A missing SMTP_USER / SMTP_PASSWORD
    is silently skipped with a warning — email is a best-effort channel, never fatal.
    """
    from gacore.tools.email_tools import send_email

    resolved_env = dict(os.environ) if env is None else dict(env)
    recipient = _resolve_email_recipient(job, resolved_env)
    if not recipient:
        logger.warning(
            "deliver_email skipped: no recipient (job.email_to / SMTP_TO / SMTP_USER all empty)",
            job=job.name,
        )
        return

    today = datetime.now(UTC).astimezone().date().isoformat()
    prefix = "[gacore][FAILED]" if error else "[gacore]"
    subject = f"{prefix} {job.name} · {today}"
    result = send_email.func(
        to=recipient,
        subject=subject,
        body=_email_body_html(reply, error),
        _env=resolved_env,
    )
    if isinstance(result, dict) and result.get("status") == "sent":
        logger.info("deliver_email sent", job=job.name, to=recipient, subject=subject)
    else:
        logger.warning("deliver_email failed", job=job.name, to=recipient, result=result)


def _deliver(job: Job, cfg: Config, reply: str, error: str | None) -> None:
    """Route the finished job's reply to its configured channel (deliver_to)."""
    if job.deliver_to == "email":
        _deliver_email(job, cfg, reply, error)
    elif job.deliver_to != "file":
        logger.warning(
            "deliver_to unsupported, falling back to file",
            job=job.name,
            deliver_to=job.deliver_to,
        )


def run_loop(
    cfg: Config | None = None,
    poll_interval: int = _POLL_INTERVAL_SECONDS,
    graph_runner: Callable[[str, Config, int], str | None] | None = None,
    max_iterations: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Main scheduler loop: poll for due jobs, run them, persist state.

    Args:
        cfg: Runtime config; defaults to Config.default().
        poll_interval: Seconds between polls (default 30).
        graph_runner: Injection seam for tests; None uses the real graph.
        max_iterations: Stop after N polls (tests); None = run forever.
        clock: Time source override (tests); None = datetime.now().

    Returns:
        Number of jobs executed across all iterations.
    """
    resolved_cfg = cfg or Config.default()
    now_fn = clock or (lambda: datetime.now(UTC).astimezone())
    states = load_state(resolved_cfg)
    jobs_run = 0
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        now = now_fn()
        jobs = load_jobs(resolved_cfg)
        for job in jobs:
            state = states.get(job.name, JobState())
            if not is_due(job, state, now):
                continue
            if job.type == "proactive":
                # Proactive jobs: extra window/cooldown gate, then dispatch to the
                # single-worker PROACTIVE_POOL so the poll loop is never blocked by
                # LLM generation or QQ network I/O.
                if not proactive_due(job, state, now):
                    # Debug level on purpose: is_due stays true while the job waits for
                    # its window/cooldown, so this branch runs on every poll tick and an
                    # info log would flood the log with "not yet due" noise.
                    logger.debug(
                        "proactive job not due: window miss or cooldown",
                        job=job.name,
                        schedule=job.schedule,
                        now=now.isoformat(timespec="seconds"),
                    )
                    continue
                future = PROACTIVE_POOL.submit(run_proactive_job, job, resolved_cfg)
                if future is None:
                    logger.warning(
                        "proactive pool queue full, skipping this tick",
                        job=job.name,
                        schedule=job.schedule,
                    )
                    continue
                # L1: surface worker exceptions instead of letting the future die silently.
                future.add_done_callback(_log_proactive_failure)
                logger.info(
                    "Proactive job dispatched",
                    job=job.name,
                    schedule=job.schedule,
                    now=now.isoformat(timespec="seconds"),
                )
                states[job.name] = JobState(
                    last_run=now.isoformat(timespec="seconds"),
                    run_count=state.run_count + 1,
                )
                save_state(resolved_cfg, states)
                jobs_run += 1
                continue
            logger.info("Job due, firing", job=job.name, schedule=job.schedule, now=now.isoformat(timespec="seconds"))
            run_job(job, resolved_cfg, graph_runner=graph_runner)
            states[job.name] = JobState(
                last_run=now.isoformat(timespec="seconds"),
                run_count=state.run_count + 1,
            )
            save_state(resolved_cfg, states)
            jobs_run += 1
        if max_iterations is None:
            time.sleep(poll_interval)

    return jobs_run


def main() -> None:
    """CLI entry point: load .env, run the scheduler loop forever (Ctrl-C to stop)."""
    load_dotenv()
    cfg = Config.default()
    logger.info("Scheduler starting", poll_interval=_POLL_INTERVAL_SECONDS)
    try:
        run_loop(cfg=cfg)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")


__all__ = (
    "Job",
    "JobState",
    "ScheduleResult",
    "is_due",
    "load_jobs",
    "load_state",
    "main",
    "next_run_time",
    "run_job",
    "run_loop",
    "save_state",
)
