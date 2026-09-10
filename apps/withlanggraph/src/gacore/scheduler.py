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
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from gacore.config import Config, load_dotenv
from gacore.feedback import save_delivered, stamp_report_bullets
from gacore.jsonl_logger import get_logger
from gacore.proactive import PROACTIVE_POOL, proactive_due, run_proactive_job

logger = get_logger("scheduler")

_SCHEDULE_FILE: Final = "schedule.json"
_STATE_FILE: Final = "schedule_state.json"
_OUTPUT_SUBDIR: Final = "scheduled"
_TIME_RE: Final = re.compile(r"^(\d{1,2}):(\d{2})$")
_INTERVAL_RE: Final = re.compile(r"^every\s+(\d+)\s*([dhm])$", re.IGNORECASE)
_POLL_INTERVAL_SECONDS: Final = 30

# ---- 调度回复清洗与完成性校验（修复 2026-09-03 日报正文被 <summary>/DSML 标签污染）----
# <summary>...</summary> 完整块（GA 协议思考块）：qq.py 流式路径已在发送前剥离，调度/邮件路径补齐。
_SUMMARY_BLOCK_RE: Final = re.compile(r"<summary>.*?</summary>", re.DOTALL)
# 工具调用 DSL 的闭合标签残尾行：形如 </｜｜DSML｜｜parameter> / </||DSML||invoke> / </｜｜tool_calls>
# （｜=U+FF5C 全角竖线；兼容 0~1 个 ASCII "DSML" 中间标记，以及全角/半角竖线变体）。
# 仅匹配"整行只有闭合 + 标签关键字"的形式，避免误删正文普通文本。
# 正常开标签会以结构化 tool_calls 走 tools 节点、不落入 content，故此处只处理残尾闭合标签。
_TOOL_DSL_LINE_RE: Final = re.compile(
    r"(?m)^[ \t]*</[｜|\s]*D?S?M?L?[｜|\s]*(?:parameter|invoke|tool_calls)[｜|\s]*>[ \t]*$"
)


def _sanitize_reply(reply: str) -> str:
    """清洗调度产出正文：剥 <summary> 完整块、未闭合 <summary> 残渣、工具调用 DSL 残尾行。

    幂等：已清洗文本二次调用结果不变；任一类缺失（无 summary / 无 DSL 残标）均安全跳过。
    """
    if not reply:
        return reply
    text = reply
    # 1) 完整 <summary>...</summary> 思考块（跨行）
    text = _SUMMARY_BLOCK_RE.sub("", text)
    # 2) 若仍残留未闭合 <summary>（无对应闭合标签，其后即为思考残渣，正文本体未产出）-> 清到结尾
    if "<summary>" in text and "</summary>" not in text:
        text = text.split("<summary>", 1)[0]
    # 3) 工具调用 DSL 闭合标签残尾行（</...parameter|invoke|tool_calls>）
    text = _TOOL_DSL_LINE_RE.sub("", text)
    return text.strip()


def _is_incomplete_reply(raw: str, cleaned: str) -> bool:
    """是否"形似正文、实为残渣"：原始内容非空，但清洗后为空，说明只是 summary/DSML 残渣。"""
    return bool((raw or "").strip() and not (cleaned or "").strip())


# ---- 日报失败自动重试一次（2026-09-05，9/4 事故修复）----
# 触发：模型末条消息仅为 <summary>/DSML 残渣（INCOMPLETE）或空（EMPTY）——这类"没产出正文"
# 往往是模型单轮偷懒/误判完成，信息包素材是好的，重跑一次大概率能成篇。第二次仍失败才判败。
_RETRYABLE_REASONS: Final = frozenset({"INCOMPLETE_REPLY", "EMPTY_REPLY"})
_MAX_JOB_ATTEMPTS: Final = 2
# 重试 prompt：在已装配好信息包的 prompt 末尾追加强硬输出约束，Info Pack 只预取一次不重复组装。
_RETRY_PROMPT_SUFFIX: Final = (
    "\n\n[二次生成·硬约束] 上一轮你只输出了内部思考块(<summary>)或空内容，没有产出正式的日报正文。"
    "本次必须正常作答：\n"
    "1. 严禁输出 <summary> 或任何 XML/类标签样式的思考过程，也不要再调用任何工具，直接干干净净写正文。\n"
    "2. 正文是结构化 Markdown，按既定分节输出：# 今日状态 / # 工作日志 / # 个人观察 / "
    "# 关键变化 / # 明日计划（没内容的节连同标题一起整节省略）。\n"
    "3. 不要开场白、不要解释你在做什么，直接输出可阅读的邮件正文。"
)


def _make_retry_prompt(prompt: str) -> str:
    """Info Pack 已内嵌在 prompt 里，重试只需追加硬约束后缀，避免重复组装信息包。"""
    return prompt + _RETRY_PROMPT_SUFFIX


# 日报 job 的命名标记：凡 job.name 含该子串即视为"日报类"，触发信息包注入与跨日 onboard 导出。
_DAILY_JOB_MARKER: Final = "daily"


def _is_daily_job(job: Job) -> bool:
    """日报类 job 判定（命名嗅探，命中 _DAILY_JOB_MARKER）。"""
    return _DAILY_JOB_MARKER in job.name.lower()


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


def _build_job_prompt(job: Job, cfg: Config, for_day: str | None = None) -> str:
    """Compose the user prompt for one job.

    For the daily-report job, prepend the precomputed "当日信息包" (daily info pack) as
    a leading section of the user message — the main channel chosen in
    daily-report-redesign-v2: zero change to GAState / context.py. The pack carries the
    deterministic daily facts (B站/Edge/git/文件活动/画像素材/ncm 基线/前日日报) so the
    LLM can focus on dynamic retrieval (search_daily) and writing. Any failure while
    building the pack falls back to the plain prompt so the job still runs.

    for_day: ISO date — when set (historical re-run), the info pack is built for that
    day instead of today. Defaults to today so scheduled runs are unchanged.
    """
    prompt = job.prompt
    if "daily" in job.name.lower():
        try:
            from gacore.daily_info_pack import build_info_pack

            today = for_day or datetime.now(UTC).astimezone().date().isoformat()
            info_pack = build_info_pack(today, cfg)
            if info_pack:
                prompt = f"{info_pack}\n\n{job.prompt}"
                logger.info(
                    "daily info pack injected into user prompt",
                    job=job.name,
                    date=today,
                    info_pack_chars=len(info_pack),
                )
        except Exception as e:  # noqa: BLE001 — never let the pack break the job
            logger.warning(
                "daily info pack build failed; falling back to plain prompt",
                job=job.name,
                error_type=type(e).__name__,
                stack_trace=str(e),
            )
    return prompt


def run_job(
    job: Job,
    cfg: Config,
    graph_runner: Callable[[str, Config, int], str | None] | None = None,
    for_day: str | None = None,
    deliver: bool = True,
) -> ScheduleResult:
    """Execute one job: run the agent headless, capture reply, write output + daily note.

    graph_runner is the injection seam for tests: production passes None (uses the real
    build_graph + run_once), tests pass a fake that returns a canned reply without LLM.

    for_day: ISO date — historical re-run: info pack + trajectory map are produced for
    that day instead of today; email/note/output still land under today's timestamp but
    carry the historical day's data. Defaults to None (today).

    deliver: when False, skip channel delivery (email etc.) — output archive and daily
    note are still written. Used by `python -m gacore.rerun --no-email`.

    The reply is extracted from the final state's last AIMessage content.
    """
    start = time.monotonic()
    name = job.name
    logger.info("Job started", job=name, schedule=job.schedule)
    error: str | None = None
    reply = ""
    raw_reply = ""
    exit_reason: str | None = None
    prompt = job.prompt
    try:
        prompt = _build_job_prompt(job, cfg, for_day)
        # 失败自动重试一次：首次仅得残渣/空正文（INCOMPLETE/EMPTY）→ 换硬约束 prompt 重跑。
        # 信息包在第一次 prompt 里已装配，重试只追加约束后缀，不重复组装信息包。
        active_prompt = prompt
        for attempt in range(_MAX_JOB_ATTEMPTS):
            if graph_runner is None:
                exit_reason, reply, raw_reply = _default_graph_runner(
                    active_prompt, cfg, job.max_turns
                )
            else:
                exit_reason = graph_runner(active_prompt, cfg, job.max_turns)
                reply = f"[test reply for {name}]"
                raw_reply = reply
            if (
                attempt == 0
                and exit_reason in _RETRYABLE_REASONS
                and _is_daily_job(job)
            ):
                logger.warning(
                    "Job reply incomplete/empty; retrying once",
                    job=name,
                    exit_reason=exit_reason,
                )
                active_prompt = _make_retry_prompt(prompt)
                continue
            break
    except Exception as e:  # noqa: BLE001 — scheduler must not crash on one job failure
        error = f"{type(e).__name__}: {e}"
        logger.error("Job failed", job=name, error_type=type(e).__name__, stack_trace=str(e))
        exit_reason = "AGENT_ERROR"

    # 完成性校验：模型末条消息仅为 summary/工具DSML 残渣（INCOMPLETE_REPLY）或为空
    # （EMPTY_REPLY，如 09-04 00:21 重跑）时按失败处理——不投递"成功"邮件、不写 OK
    # 附注、不触发跨日 onboard 导出，避免污染下游（09-03/09-04 事故修复点）。
    if error is None and exit_reason == "INCOMPLETE_REPLY":
        error = (
            "INCOMPLETE_REPLY: 模型末条消息仅为 <summary>/工具调用DSML 残渣，"
            "未产出可交付的日报正文"
        )
        logger.warning("Job reply incomplete; marked as failed", job=name, exit_reason=exit_reason)
    elif error is None and exit_reason == "EMPTY_REPLY":
        error = "EMPTY_REPLY: 模型未产出任何 AIMessage 正文（空回复）"
        logger.warning("Job reply empty; marked as failed", job=name, exit_reason=exit_reason)
    duration = time.monotonic() - start
    output_path = _write_output(cfg, job, reply, error, prompt=prompt, raw_reply=raw_reply)
    _write_daily_note(cfg, job, reply, error)
    # Episodic memory: after a successful daily-report run, embed the day's daily
    # note into the separate day-tagged vector table so future conversation can recall
    # "那天发生了什么" via vector search (阶段三). Best-effort only — a vector failure
    # must never fail the report delivery.
    if error is None and _is_daily_job(job):
        try:
            _sync_episodic(cfg, for_day)
        except Exception as e:  # noqa: BLE001 — vector sync must never break the report
            logger.error("episodic sync failed", job=name, error_type=type(e).__name__, stack_trace=str(e))
    if deliver:
        deliver_reply = reply
        if error is None and _is_daily_job(job) and (reply or "").strip():
            # 投递正文统一打 [节-序号] 锚点并落真相源：用户按锚点回指订正，
            # 反馈读写与邮件/QQ 看到的是同一份正文。钉锚失败绝不影响投递。
            try:
                deliver_reply = stamp_report_bullets(reply)
                save_delivered(
                    cfg, for_day or datetime.now(UTC).astimezone().date().isoformat(), deliver_reply
                )
            except Exception as e:  # noqa: BLE001 — stamping must never break delivery
                logger.error(
                    "anchor stamping failed; delivering raw",
                    job=name,
                    error_type=type(e).__name__,
                    stack_trace=str(e),
                )
                deliver_reply = reply
        _deliver(job, cfg, deliver_reply, error, for_day=for_day)
    else:
        logger.info("delivery skipped (deliver=False)", job=name)

    # Cross-day rollover: after a successful daily-report run, export an onboard
    # memory pack (recent daily summaries + long-term persona) for the QQ frontend
    # to consume on the first message of the new day. Best-effort only — a failure
    # here must never block the report itself.
    if error is None and _is_daily_job(job):
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


def _default_graph_runner(prompt: str, cfg: Config, max_turns: int) -> tuple[str | None, str, str]:
    """Build a fresh graph and run the prompt as a single-turn headless agent run.

    Returns (exit_reason, reply_text, raw_reply_text) — the reply is extracted from the
    last AIMessage in the final state; raw_reply is the pre-sanitize original (for the
    run archive). Scheduled jobs are single-turn, so the last AI message is the agent's
    final answer.
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
    raw_reply = reply
    reply = _sanitize_reply(reply)
    if _is_incomplete_reply(raw_reply, reply):
        # 末条消息实际是 <summary>/工具DSML 残渣而非正文（如 09-03 事故）→ 标记未完成，
        # 交由 run_job 视为失败处理，禁止把垃圾当成功交付。
        return "INCOMPLETE_REPLY", reply, raw_reply
    if not reply.strip():
        # 末条消息为空（如 09-04 00:21 重跑的 empty reply）→ 同样按失败处理，
        # 不投递"成功"邮件。
        return "EMPTY_REPLY", reply, raw_reply
    return exit_reason, reply, raw_reply


_REPLY_CACHE: Final = "_last_scheduled_reply"  # legacy; kept for backward-compat of state files


def _reconstruct_system_prompt(prompt: str, cfg: Config) -> str:
    """Best-effort reproduce the system prompt the run actually used.

    GA rebuilds the system prompt per model call (GAPromptMiddleware); for scheduled
    runs the state is a fresh new_state(prompt) with no rollover/output_mode overrides,
    so rebuilding here is faithful up to the second-level time anchor. Returns "" on
    any failure — archiving must never break the job.
    """
    try:
        from gacore.context import build_system_prompt
        from gacore.state import new_state

        return build_system_prompt(new_state(prompt, cfg), cfg)
    except Exception as e:  # noqa: BLE001 — archive helper degrades to empty
        logger.warning("system prompt reconstruction failed", error_type=type(e).__name__, stack_trace=str(e))
        return ""


def _write_output(
    cfg: Config,
    job: Job,
    reply: str,
    error: str | None,
    *,
    prompt: str | None = None,
    raw_reply: str = "",
) -> str | None:
    """Write the run's full input/output archive to logs/scheduled/{job}_{timestamp}.md.

    Sections: metadata → System Prompt (reconstructed) → User Prompt (assembled, i.e.
    info pack + job prompt — NOT the bare job prompt) → Reply (sanitized, what got
    delivered) → Reply (raw, pre-sanitize). This is the "到底用什么生成了日报"
    troubleshooting artifact (2026-09-04); the per-model-call request log lives in
    logs/llm_calls/<date>.jsonl (middleware).
    """
    out_dir = cfg.logs_dir / _OUTPUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{job.name}_{ts}.md"
    assembled_prompt = prompt if prompt is not None else job.prompt
    system_prompt = _reconstruct_system_prompt(assembled_prompt, cfg) if _is_daily_job(job) else ""
    today = datetime.now(UTC).astimezone().date().isoformat()
    lines = [
        f"# Scheduled Job: {job.name}",
        f"- time: {datetime.now(UTC).astimezone().isoformat(timespec='seconds')}",
        f"- schedule: {job.schedule}",
        f"- error: {error or 'none'}",
        f"- llm_call_log: logs/llm_calls/{today}.jsonl",
        "",
    ]
    if system_prompt:
        lines += ["## System Prompt (reconstructed)", "", system_prompt, ""]
    lines += [
        "## User Prompt (assembled, incl. info pack)",
        "",
        assembled_prompt,
        "",
        "## Reply",
        "",
        reply or "(empty reply)",
        "",
        "## Reply (raw model output, pre-sanitize)",
        "",
        raw_reply or "(empty)",
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
    # 写入前同样清洗，杜绝 <summary>/DSML 残渣进 daily note（与调度回复清洗同源）
    snippet = _sanitize_reply(reply or "")[:200].replace("\n", " ")
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


def _sync_episodic(cfg: Config, for_day: str | None) -> None:
    """Embed the day's daily note into the episodic vector table (best-effort).

    ``for_day`` may be None for one-off runs; such runs naturally target today. The
    daily note md stored under ``memory/daily/`` is the source of truth; this only adds
    vector rows in the separate episodic table — memory text is never mutated.
    """
    from gacore import vector_store

    day = (for_day or datetime.now(UTC).astimezone().date().isoformat())
    result = vector_store.sync_episodic_daily(cfg, day)
    logger.info("episodic embedded for day", day=day, lines=result.get("lines"), stored=result.get("stored"))


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


# ---- 邮件正文 Markdown→HTML（2026-09-05，修复邮件显示裸 Markdown 源码）----
# 日报 reply 是结构化 Markdown（# 标题 / - bullet / **加粗**，prompt 已禁表格图片引用块）。
# 旧实现 html.escape 后塞 <pre> 等宽标签——手机邮箱里 #、**、- 全是裸字符，无任何排版。
# 这里做确定性子集转换：h1-h6 / 无序列表 / **加粗** / `行内码` / 段落；先转义再变换（XSS
# 安全），未识别行降级为段落，绝不抛异常。样式全部内联（邮箱客户端普遍剥离 <style> 块）。
_MD_HEADER_RE: Final = re.compile(r"^(#{1,6})\s+(.+)$")
_MD_BULLET_RE: Final = re.compile(r"^[-*+]\s+(.+)$")
_MD_BOLD_RE: Final = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE_RE: Final = re.compile(r"`([^`\n]+?)`")

_EMAIL_CONTAINER_STYLE: Final = (
    "max-width:680px;margin:0 auto;padding:20px 16px;"
    "font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;"
    "color:#1a1a1a;line-height:1.75;font-size:15px;"
)
_EMAIL_HEADER_STYLES: Final = {
    1: "font-size:19px;margin:24px 0 10px;padding-bottom:6px;border-bottom:2px solid #0aa2c0;color:#111;",
    2: "font-size:17px;margin:20px 0 8px;padding-bottom:5px;border-bottom:1px solid #0aa2c0;color:#222;",
    3: "font-size:15.5px;margin:16px 0 6px;color:#333;",
}
_EMAIL_HEADER_FALLBACK_STYLE: Final = "font-size:15px;margin:14px 0 6px;color:#333;"
_EMAIL_UL_STYLE: Final = "margin:6px 0 12px;padding-left:22px;"
_EMAIL_LI_STYLE: Final = "margin:5px 0;"
_EMAIL_P_STYLE: Final = "margin:8px 0;"
_EMAIL_CODE_STYLE: Final = (
    "background:#f2f3f5;padding:1px 5px;border-radius:4px;"
    "font-family:ui-monospace,Consolas,monospace;font-size:13px;"
)


def _md_inline(text: str) -> str:
    """行内变换：先 html.escape 再套 **加粗** / `行内码`——转义在前保证 XSS 安全。"""
    t = html.escape(text)
    t = _MD_BOLD_RE.sub(r"<b>\1</b>", t)
    t = _MD_CODE_RE.sub(rf"<code style='{_EMAIL_CODE_STYLE}'>\1</code>", t)
    return t


def _md_to_email_html(md: str) -> str:
    """把约束子集 Markdown（标题/bullet/加粗/行内码/段落）转成内联样式 HTML 块序列。"""
    blocks: list[str] = []
    bullets: list[str] = []

    def flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li style='{_EMAIL_LI_STYLE}'>{b}</li>" for b in bullets)
            blocks.append(f"<ul style='{_EMAIL_UL_STYLE}'>{items}</ul>")
            bullets.clear()

    for raw_line in (md or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush_bullets()
            continue
        m = _MD_HEADER_RE.match(line)
        if m:
            flush_bullets()
            level = len(m.group(1))
            style = _EMAIL_HEADER_STYLES.get(level, _EMAIL_HEADER_FALLBACK_STYLE)
            blocks.append(f"<h{level} style='{style}'>{_md_inline(m.group(2))}</h{level}>")
            continue
        m = _MD_BULLET_RE.match(line)
        if m:
            bullets.append(_md_inline(m.group(1)))
            continue
        flush_bullets()
        blocks.append(f"<p style='{_EMAIL_P_STYLE}'>{_md_inline(line)}</p>")
    flush_bullets()
    return "".join(blocks)


def _email_body_html(reply: str, error: str | None) -> str:
    """渲染日报邮件正文：Markdown 子集 → 内联样式 HTML（旧版是转义后 <pre> 裸文本）。"""
    status = f"<p style='color:#b00'><b>FAILED:</b> {html.escape(error)}</p>" if error else ""
    content = _md_to_email_html(reply) if (reply or "").strip() else "<p>(empty reply)</p>"
    return (
        "<!DOCTYPE html><html><body>"
        f"<div style='{_EMAIL_CONTAINER_STYLE}'>{status}{content}</div>"
        "</body></html>"
    )


def _deliver_email(job: Job, cfg: Config, reply: str, error: str | None, env: Mapping[str, str] | None = None, for_day: str | None = None) -> None:
    """Deliver the job's reply via send_email; never raises, logs the outcome.

    Recipient resolution and SMTP configuration follow send_email's rules (SMTP_* env
    vars); the only difference is the recipient defaults to SMTP_USER (send to self)
    when neither job.email_to nor SMTP_TO is set. A missing SMTP_USER / SMTP_PASSWORD
    is silently skipped with a warning — email is a best-effort channel, never fatal.

    For daily jobs, a trip-trajectory map (高德静态图, GCJ02) is rendered and inlined
    as an image (cid:photo0) when trips exist for the day; the reply body also gains a
    one-line 行程文字摘要 via trip_summary_text. A missing trajectory never blocks the
    email — it is an optional visual layer (C2).
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

    # 轨迹图与行程文字：仅每日报告，数据天 = for_day or 今天
    traj_png: Path | None = None
    if _is_daily_job(job) and error is None:
        from gacore.langTrack import trajectory_map

        day = for_day or datetime.now(UTC).astimezone().date().isoformat()
        db = cfg.root / "data" / "langTrack.db"
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                traj_png = trajectory_map.render_day_trajectory(
                    conn, day, cfg.logs_dir / "trajectory" / f"{day}.png"
                )
                trip_text = trajectory_map.trip_summary_text(conn, day)
            finally:
                conn.close()
            if trip_text and reply:
                reply = reply.rstrip() + "\n\n## 当日行程\n" + trip_text
        except Exception as e:  # noqa: BLE001 — trajectory is optional, never break email
            logger.warning(
                "trajectory_map skipped in email deliver",
                job=job.name,
                error_type=type(e).__name__,
                stack_trace=str(e),
            )

    today = datetime.now(UTC).astimezone().date().isoformat()
    prefix = "[gacore][FAILED]" if error else "[gacore]"
    # 历史补跑时主题挂数据日（for_day）而非发送时刻，收件人才能一眼看出这是哪天的日报
    day_label = f"{for_day}（补跑）" if for_day and for_day != today else today
    subject = f"{prefix} {job.name} · {day_label}"
    body = _email_body_html(reply, error)
    image_paths = [str(traj_png)] if traj_png is not None else None
    result = send_email.func(
        to=recipient,
        subject=subject,
        body=body,
        image_paths=image_paths,
        _env=resolved_env,
    )
    if isinstance(result, dict) and result.get("status") == "sent":
        logger.info("deliver_email sent", job=job.name, to=recipient, subject=subject)
    else:
        logger.warning("deliver_email failed", job=job.name, to=recipient, result=result)


def _deliver(job: Job, cfg: Config, reply: str, error: str | None, for_day: str | None = None) -> None:
    """Route the finished job's reply to its configured channel (deliver_to)."""
    if job.deliver_to == "email":
        _deliver_email(job, cfg, reply, error, for_day=for_day)
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
