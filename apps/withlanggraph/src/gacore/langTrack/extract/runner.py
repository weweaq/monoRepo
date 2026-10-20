"""KG 抽取运行器：素材 → 通道 LLM 抽取 → 落库 → kg_runs 血缘。

独立 CLI 入口（决策 D2）：不挂进 ``etl.py``，保住 ETL"纯本地、确定性、可重建"的属性，
也便于独立测试（不触网、不触真实库）。

- 幂等：唯一索引 ``ux_kg_runs_day_hash_ver(day, raw_hash, prompt_ver)`` 命中且
  ``status='ok'`` → 整次跳过；非 ok 的历史运行复用同一 ``run_id`` 原地重跑。
- 素材路由：每个通道只吃自己 ``spec.sources`` 声明的源（``Material.select``），
  不再六通道同吃当日全量素材——素材越大，推理模型的合法生成越久。
- 失败隔离：通道并发执行，任一通道异常只记 ``failed``，不影响其他通道（整次 status=partial）。
- 超时兜底：单通道超过 ``KG_EXTRACT_CHANNEL_TIMEOUT``（默认 900s）未返回即记 failed，
  避免"LLM 客户端挂死在空闲连接"的通道把 ``asyncio.gather`` 无限拖住。
- 运维日志：每通道一行进 ``logs/<day>/app.jsonl``，并把同一份体检指标（耗时 / 素材与回复
  字符数 / token 用量 / finish_reason / 尝试次数）落进 ``kg_runs.channel_stats``——据此可判定
  慢在素材侧（``prompt_chars``）还是 provider 侧（``output_tokens`` + ``finish_reason``）。
- 落库幂等：``edge_id`` 由内容派生，重跑撞 PK 即静默忽略。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from langchain_core.messages import HumanMessage, SystemMessage

from gacore.config import Config
from gacore.jsonl_logger import get_logger
from gacore.langTrack.extract.channels import CHANNELS
from gacore.langTrack.extract.contract import (
    corrective_message,
    detect_question,
    fingerprint,
    is_opencode,
    max_attempts,
    retryable_error,
    with_no_question_rule,
)
from gacore.langTrack.extract.intervals import reclose_day
from gacore.langTrack.extract.material import Material, load_material, material_stats
from gacore.langTrack.extract.resolve import resolve_entities
from gacore.langTrack.extract.schema import migrate_extract_tables
from gacore.langTrack.extract.types import ChannelOutcome, ChannelSpec, ExtractContext

PROMPT_VER: Final = "kg@v1"
_PROMPT_RELPATH: Final = Path("config") / "kg_prompts"
_CN_TZ: Final = _dt.timezone(_dt.timedelta(hours=8))
_FENCE_RE: Final = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

_OK_STATUS: Final = "ok"

_LOG = get_logger("langTrack.extract")

# 单通道超时兜底（秒）。正常一整天素材的单通道 LLM 调用约 4 分钟，此处留 ~4x 余量；
# 它兜的是"LLM 客户端挂死在空闲连接上、永不返回"这条路径——没有它，一个通道挂死会
# 让 asyncio.gather 无限等待，整轮抽取卡住（LLM_TIMEOUT 只能兜单次请求，兜不住重试叠加）。
_DEFAULT_CHANNEL_TIMEOUT: Final = 900.0

# 抽取路径的单次 LLM 请求超时（秒）。比全局 LLM_TIMEOUT(300s) 大一倍：抽取跑的是推理
# 模型，一整天素材的合法生成（隐藏推理 token 为主）实测可达 400s+，用交互路径的预算会把
# 正常生成误杀成超时（2026-10-18 todos 通道即此症状）。
_DEFAULT_EXTRACT_LLM_TIMEOUT: Final = 600.0

# 抽取路径走 opencode 时的默认推理档位。免费档 glm-5.3-flash-free 的模型默认档在整日
# events 素材上会把 limit.output=32000 全烧在隐藏推理（finish_reason=length、447s、
# 截断成非法 JSON），low 档同素材实测 160s、finish=stop（2026-10-20 A/B）。
_DEFAULT_EXTRACT_OPENCODE_VARIANT: Final = "low"


@dataclass(frozen=True, slots=True)
class RunResult:
    """一次抽取运行的对外结果（CLI 打印 / 测试断言用）。"""

    run_id: str
    day: str
    status: str  # ok | partial | failed | skipped | dry-run
    raw_hash: str
    material_chars: int
    rows: int = 0  # 本次新写入的 kg_nodes + 各通道记录表行数（skipped / dry-run 恒为 0）
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)


def prompt_path(cfg: Config, name: str) -> Path:
    return cfg.root / _PROMPT_RELPATH / name


def load_prompt(cfg: Config, name: str) -> str:
    """读外置 prompt；缺失即报错（prompt 是通道的必需资产，不做静默降级）。"""
    path = prompt_path(cfg, name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"kg prompt 缺失：{path}") from exc
    if not text.strip():
        raise ValueError(f"kg prompt 为空：{path}")
    return text


def parse_json_payload(text: str) -> dict[str, Any]:
    """从 LLM 回复里抠出 JSON 对象：剥 ``` 围栏 → 取首尾花括号区间 → 解析。

    这是外部输入边界，解析失败抛 ValueError（由调用方决定重试或记 failed）。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回复中找不到 JSON 对象")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("顶层 JSON 不是对象")
    return payload


def _now_stamp() -> str:
    return _dt.datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _new_run_id() -> str:
    return f"{_dt.datetime.now(_CN_TZ).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"


def _resolve_db_path(cfg: Config, db_path: Path | None) -> Path:
    return db_path if db_path is not None else cfg.root / "data" / "langTrack.db"


def _select_channels(only: Sequence[str] | None) -> tuple[ChannelSpec, ...]:
    if not only:
        return CHANNELS
    wanted = {k.strip() for k in only if k.strip()}
    unknown = sorted(wanted - {c.key for c in CHANNELS})
    if unknown:
        raise ValueError(f"未知通道：{','.join(unknown)}（可选：{','.join(c.key for c in CHANNELS)}）")
    return tuple(c for c in CHANNELS if c.key in wanted)


def _lookup_run(conn: sqlite3.Connection, day: str, raw_hash: str, prompt_ver: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT run_id, status FROM kg_runs WHERE day = ? AND raw_hash = ? AND prompt_ver = ?",
        (day, raw_hash, prompt_ver),
    ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def _content_of(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


def _usage_of(response: Any) -> dict[str, Any]:
    """取响应里的 token 用量（provider 不给就空 dict）；运维日志用来分辨耗时构成。"""
    usage = getattr(response, "usage_metadata", None)
    return dict(usage) if isinstance(usage, dict) else {}


def _finish_reason_of(response: Any) -> str:
    """取 finish_reason（如 ``length`` = 被截断）；缺失返回空串。"""
    meta = getattr(response, "response_metadata", None)
    finish = meta.get("finish_reason") if isinstance(meta, dict) else None
    return str(finish) if finish else ""


def _probe(response: Any, outcome: ChannelOutcome, started: float, attempts: int) -> None:
    """把一次通道调用的体检指标写进 outcome.extra（含失败路径）。"""
    outcome.extra["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    outcome.extra["attempts"] = attempts
    if response is None:
        return
    outcome.extra["response_chars"] = len(_content_of(response))
    usage = _usage_of(response)
    if usage:
        outcome.extra["usage"] = usage
    finish = _finish_reason_of(response)
    if finish:
        outcome.extra["finish_reason"] = finish


def _log_channel(outcome: ChannelOutcome) -> None:
    """通道级运维日志：一次运行每通道一行，回答"哪边慢、为什么慢"。"""
    fields: dict[str, Any] = {"status": outcome.status, "rows": outcome.rows, **outcome.extra}
    if outcome.error:
        fields["error"] = outcome.error
    if outcome.status == "ok":
        _LOG.info("kg_extract_channel", channel=outcome.key, **fields)
    else:
        _LOG.warning("kg_extract_channel", channel=outcome.key, **fields)


def _log_attempt(
    channel: str,
    attempt: int,
    result: str,
    raw: str,
    elapsed_ms: int,
    *,
    base: dict[str, Any] | None = None,
    response: Any = None,
    violation: str = "",
    error: str = "",
    action: str = "",
) -> None:
    """单次 LLM 调用的审计行：只记指纹/长度/用量，不记原文。"""
    fields: dict[str, Any] = {
        **(base or {}),
        "channel": channel,
        "attempt": attempt,
        "result": result,
        "elapsed_ms": elapsed_ms,
    }
    if action:
        fields["action"] = action
    if raw:
        fields["response_chars"] = len(raw)
        fields["response_hash"] = fingerprint(raw)
    if response is not None:
        usage = _usage_of(response)
        if usage:
            fields["usage"] = usage
        finish = _finish_reason_of(response)
        if finish:
            fields["finish_reason"] = finish
    if violation:
        fields["violation"] = violation
    if error:
        fields["error"] = error
    if result == "ok":
        _LOG.info("kg_extract_attempt", **fields)
    else:
        _LOG.warning("kg_extract_attempt", **fields)


def _exc_summary(exc: BaseException) -> str:
    """异常摘要：只留类型/状态码，避免把上游响应体写进日志和 channel_stats。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__
    return f"{name} status={status}" if status else name


async def _extract_channel(
    spec: ChannelSpec,
    llm: Any,
    prompt: str,
    material_text: str,
    conn: sqlite3.Connection,
    ctx: ExtractContext,
) -> ChannelOutcome:
    """单通道：素材 → LLM（opencode 带契约重试）→ 单事务落库。异常只记 failed。

    无论成败都把本次调用的体检指标记进 ``outcome.extra``：素材侧看 ``prompt_chars``，
    provider 侧看 ``usage`` / ``finish_reason``，中间看 ``elapsed_ms`` / ``attempts``。
    """
    outcome = ChannelOutcome(key=spec.key)
    outcome.extra = {"prompt_chars": len(material_text), "routed_sources": list(spec.sources)}
    if not material_text.strip():
        outcome.status = "skipped"
        outcome.error = "该通道素材为空"
        return outcome
    started = time.perf_counter()
    attempts = 0
    response: Any = None
    payload: dict[str, Any] | None = None
    reasons: list[str] = []
    last_error = ""
    oc = is_opencode(llm)
    limit = max_attempts(llm)
    system_prompt = with_no_question_rule(prompt) if oc else prompt
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=material_text)]
    audit_base = {
        "day": ctx.day,
        "run_id": ctx.run_id,
        "provider": str(getattr(llm, "_llm_type", "") or ""),
        "model": str(getattr(llm, "model_name", "") or getattr(llm, "model", "") or ""),
    }
    audit_base = {key: value for key, value in audit_base.items() if value}
    try:
        while attempts < limit:
            attempts += 1
            response = None
            attempt_started = time.perf_counter()
            try:
                response = await llm.ainvoke(messages)
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - attempt_started) * 1000)
                reasons.append(f"transport:{type(exc).__name__}")
                # Provider-agnostic: transient transport failures (409/429/5xx/network) get
                # one retry on every provider; timeouts and auth 4xx do not
                # (contract.retryable_error). Gating this on ``oc`` made zhipu 5xx flakiness
                # fail a channel outright.
                will_retry = attempts < limit and retryable_error(exc)
                _log_attempt(
                    spec.key,
                    attempts,
                    "error",
                    "",
                    elapsed_ms,
                    base=audit_base,
                    error=type(exc).__name__,
                    action="retry" if will_retry else "abort",
                )
                if will_retry:
                    await asyncio.sleep(min(2.0, 0.5 * 2 ** (attempts - 1)))
                    continue
                raise
            raw = _content_of(response)
            elapsed_ms = int((time.perf_counter() - attempt_started) * 1000)
            try:
                payload = parse_json_payload(raw)
            except ValueError as exc:
                marker = detect_question(raw) if oc else None
                reason = f"question:{marker}" if marker else f"json_invalid:{type(exc).__name__}"
                reasons.append(reason)
                last_error = f"{type(exc).__name__}: {exc}"
                will_retry = attempts < limit
                _log_attempt(
                    spec.key,
                    attempts,
                    "rejected",
                    raw,
                    elapsed_ms,
                    base=audit_base,
                    response=response,
                    violation=reason,
                    action="retry" if will_retry else "abort",
                )
                if not will_retry:
                    break
                if oc:
                    messages = [*messages, HumanMessage(content=corrective_message(reason))]
                continue
            violation = ""
            if oc:
                marker = detect_question(raw)
                if marker:
                    violation = f"question_ignored:{marker}"
                    _LOG.warning(
                        "kg_extract_question_ignored",
                        **audit_base,
                        channel=spec.key,
                        attempt=attempts,
                        marker=marker,
                        response_hash=fingerprint(raw),
                        response_chars=len(raw),
                        action="accept",
                    )
            _log_attempt(
                spec.key,
                attempts,
                "ok",
                raw,
                elapsed_ms,
                base=audit_base,
                response=response,
                violation=violation,
                action="accept",
            )
            break
        if payload is None:
            detail = f"attempts={attempts}"
            if reasons:
                detail += f", reasons={','.join(reasons)}"
            if last_error:
                detail += f", last={last_error}"
            outcome.status = "failed"
            outcome.error = f"LLM 输出不可用（{detail}）"
        else:
            written, extra = spec.handle(conn, ctx, payload)
            outcome.rows = written
            outcome.extra.update(extra)
    except Exception as exc:  # noqa: BLE001 - 失败隔离：单通道异常不拖垮整次运行
        outcome.status = "failed"
        detail = _exc_summary(exc)
        if attempts:
            detail += f" | attempts={attempts}"
        if reasons:
            detail += f" | reasons={','.join(reasons)}"
        outcome.error = detail
    if reasons:
        outcome.extra["failure_reasons"] = reasons
    _probe(response, outcome, started, attempts)
    _log_channel(outcome)
    return outcome


def _channel_timeout() -> float:
    """单通道超时（秒）：``KG_EXTRACT_CHANNEL_TIMEOUT`` 覆盖，缺失/非法回落到默认值。"""
    raw = os.environ.get("KG_EXTRACT_CHANNEL_TIMEOUT")
    if raw is None:
        return _DEFAULT_CHANNEL_TIMEOUT
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CHANNEL_TIMEOUT
    return seconds if seconds > 0 else _DEFAULT_CHANNEL_TIMEOUT


async def _extract_channel_guarded(
    spec: ChannelSpec,
    llm: Any,
    prompt: str,
    material_text: str,
    conn: sqlite3.Connection,
    ctx: ExtractContext,
    timeout: float,
) -> ChannelOutcome:
    """给单通道套超时兜底：挂死的通道记 failed，不让 gather 无限等待。

    ``wait_for`` 超时以取消内层协程的方式生效；``_extract_channel`` 只吞 ``Exception``，
    取消（``CancelledError`` 属 ``BaseException``）会正常穿透，故不会被误记成 ok。
    """
    try:
        return await asyncio.wait_for(
            _extract_channel(spec, llm, prompt, material_text, conn, ctx),
            timeout=timeout,
        )
    except TimeoutError:
        return ChannelOutcome(key=spec.key, status="failed", error=f"通道超时：{timeout:g}s 内未返回")


async def _run_channels(
    specs: Sequence[ChannelSpec],
    llm: Any,
    prompts: dict[str, str],
    material: Material,
    conn: sqlite3.Connection,
    ctx: ExtractContext,
) -> list[ChannelOutcome]:
    timeout = _channel_timeout()
    tasks = [
        _extract_channel_guarded(
            spec, llm, prompts[spec.key], material.select(spec.sources), conn, ctx, timeout
        )
        for spec in specs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    outcomes: list[ChannelOutcome] = []
    for spec, result in zip(specs, results, strict=True):
        if isinstance(result, ChannelOutcome):
            outcomes.append(result)
        else:  # gather 层异常（不该发生，兜底记账）
            outcomes.append(ChannelOutcome(key=spec.key, status="failed", error=str(result)))
    return outcomes


def _overall_status(outcomes: Sequence[ChannelOutcome]) -> str:
    statuses = {o.status for o in outcomes}
    if not statuses:
        return "skipped"
    if statuses == {"skipped"}:
        return "skipped"
    if "failed" in statuses:
        return "failed" if statuses <= {"failed", "skipped"} else "partial"
    return _OK_STATUS


def _postprocess(conn: sqlite3.Connection, day: str, run_id: str) -> dict[str, Any]:
    """通道落库后的确定性收尾：实体消解 + 区间闭合。

    两者都只读通道产物、只写映射表/派生列，不依赖 LLM；失败不拖垮整次运行（记账在 channel_stats）。
    """
    try:
        resolve_entities(conn, day, run_id)
        reclosed = reclose_day(conn, day)
        return {"status": "ok", "reclosed": reclosed}
    except Exception as exc:  # noqa: BLE001 - 收尾失败只记账，不改变已落库的通道产物
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def run_extract(
    day: str,
    cfg: Config | None = None,
    *,
    force: bool = False,
    only: Sequence[str] | None = None,
    dry_run: bool = False,
    db_path: Path | None = None,
    llm: Any = None,
) -> RunResult:
    """跑一次 KG 抽取；返回 RunResult（不抛异常，通道级失败进 channels 明细）。"""
    resolved = cfg or Config.default()
    specs = _select_channels(only)
    material = load_material(day, resolved)
    material_chars = len(material.text)

    if dry_run:
        return RunResult(
            run_id="",
            day=day,
            status="dry-run",
            raw_hash=material.raw_hash,
            material_chars=material_chars,
            channels={spec.key: {"status": "planned", "rows": 0} for spec in specs},
        )

    db = _resolve_db_path(resolved, db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        migrate_extract_tables(conn)
        existing = _lookup_run(conn, day, material.raw_hash, PROMPT_VER)
        if existing and existing[1] == _OK_STATUS and not force:
            return RunResult(
                run_id=existing[0],
                day=day,
                status="skipped",
                raw_hash=material.raw_hash,
                material_chars=material_chars,
                channels=json.loads(
                    conn.execute(
                        "SELECT channel_stats FROM kg_runs WHERE run_id = ?", (existing[0],)
                    ).fetchone()[0]
                ),
            )
        run_id = existing[0] if existing else _new_run_id()
        with conn:
            if existing:
                conn.execute(
                    "UPDATE kg_runs SET status='running', started_at=?, finished_at=NULL, "
                    "channel_stats='{}', updated_at=datetime('now','+8 hours') WHERE run_id=?",
                    (_now_stamp(), run_id),
                )
            else:
                conn.execute(
                    "INSERT INTO kg_runs (run_id, day, raw_hash, prompt_ver, status, started_at) "
                    "VALUES (?, ?, ?, ?, 'running', ?)",
                    (run_id, day, material.raw_hash, PROMPT_VER, _now_stamp()),
                )
        ctx = ExtractContext(day=day, run_id=run_id, prompt_ver=PROMPT_VER)
        prompts = {spec.key: load_prompt(resolved, spec.prompt) for spec in specs}
        outcomes = asyncio.run(
            _run_channels(specs, _resolve_llm(llm), prompts, material, conn, ctx)
        )
        status = _overall_status(outcomes)
        channel_stats: dict[str, Any] = {"_material": material_stats(material)}
        channel_stats.update({o.key: o.as_dict() for o in outcomes})
        if sum(o.rows for o in outcomes) > 0:
            post = _postprocess(conn, day, run_id)
            channel_stats["_postprocess"] = post
            if post["status"] == "failed" and status == _OK_STATUS:
                status = "partial"
        with conn:
            conn.execute(
                "UPDATE kg_runs SET status=?, channel_stats=?, finished_at=?, "
                "updated_at=datetime('now','+8 hours') WHERE run_id=?",
                (status, json.dumps(channel_stats, ensure_ascii=False), _now_stamp(), run_id),
            )
        return RunResult(
            run_id=run_id,
            day=day,
            status=status,
            raw_hash=material.raw_hash,
            material_chars=material_chars,
            rows=sum(o.rows for o in outcomes),
            channels=channel_stats,
        )
    finally:
        conn.close()


def _extract_llm_timeout() -> float:
    """抽取路径的单次 LLM 请求超时（秒）：``KG_EXTRACT_LLM_TIMEOUT`` 覆盖，非法回落默认。"""
    raw = os.environ.get("KG_EXTRACT_LLM_TIMEOUT")
    if raw is None:
        return _DEFAULT_EXTRACT_LLM_TIMEOUT
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_EXTRACT_LLM_TIMEOUT
    return seconds if seconds > 0 else _DEFAULT_EXTRACT_LLM_TIMEOUT


def _resolve_llm(llm: Any) -> Any:
    """注入点：默认按 .env 的 provider 建无工具模型（与 feedback/memory_maintenance 同法）。

    抽取路径与交互路径的预算不同：超时放宽到 ``KG_EXTRACT_LLM_TIMEOUT``（默认 600s，推理
    模型合法长生成需要）。传输层不重试（``max_retries=0``）：超时的请求重试基本还会超时；
    opencode 的“输出不合规”重试在 ``_extract_channel`` 里做（默认 3 次，见
    ``OPENCODE_MAX_ATTEMPTS``），非 opencode 仍只做 JSON 解析失败的 1 次重试。

    ``log_requests=False``：整日 KG 素材含私聊/通知原文，不写 ``llm_requests.jsonl``；
    只保留每通道的 hash/长度/usage 审计行。

    可选 ``KG_EXTRACT_LLM_PROVIDER`` 只覆盖抽取路径的 provider（如 ``opencode``），
    交互 agent 仍按 ``LLM_PROVIDER``——opencode 会话 API 不支持 gacore 的工具绑定。

    走 opencode 且 ``OPENCODE_VARIANT`` 未显式设置时补 ``low``：免费档 ``glm-5.3-flash-free``
    的默认推理档会把整日 events 素材的 32000 输出上限烧在隐藏推理上（``finish_reason=length``、
    截断成非法 JSON），``low`` 同素材实测 447s→160s 且 ``finish=stop``。显式设置（含 ``high``/
    ``max``）以环境变量为准，交互路径不受影响。
    """
    if llm is not None:
        return llm
    from gacore.llm import get_llm

    source: Mapping[str, str] = os.environ
    override = (os.environ.get("KG_EXTRACT_LLM_PROVIDER") or "").strip()
    if override:
        source = {**os.environ, "LLM_PROVIDER": override}
    provider = (source.get("LLM_PROVIDER") or "").strip().lower()
    if provider == "opencode" and not (source.get("OPENCODE_VARIANT") or "").strip():
        source = {**source, "OPENCODE_VARIANT": _DEFAULT_EXTRACT_OPENCODE_VARIANT}

    return get_llm(
        [],
        source,
        bind_tools=False,
        timeout=_extract_llm_timeout(),
        max_retries=0,
        log_requests=False,
    )


# --------------------------------------------------------------------------- #
# --dump：把某日 kg_* 记录导出为 JSONL 友好 dict（导出物，不是第二真源）        #
# --------------------------------------------------------------------------- #
_DAY_SCOPED_SQL: Final[dict[str, str]] = {
    "kg_runs": "SELECT * FROM kg_runs WHERE day = ?",
    "kg_nodes": "SELECT * FROM kg_nodes WHERE run_id IN (SELECT run_id FROM kg_runs WHERE day = ?)",
    "kg_edges": "SELECT * FROM kg_edges WHERE day = ?",
    "kg_relations": "SELECT * FROM kg_relations WHERE day = ?",
    "kg_aliases": "SELECT * FROM kg_aliases WHERE day = ?",
    "kg_emotions": "SELECT * FROM kg_emotions WHERE day = ?",
    "kg_patterns": "SELECT * FROM kg_patterns WHERE day = ?",
    "kg_worldview": "SELECT * FROM kg_worldview WHERE day = ?",
}


def export_day(day: str, cfg: Config | None = None, db_path: Path | None = None) -> list[dict[str, Any]]:
    """导出某日的 kg_* 记录；每条带 ``_table`` 字段标明来源表。"""
    resolved = cfg or Config.default()
    db = _resolve_db_path(resolved, db_path)
    conn = sqlite3.connect(db)
    try:
        migrate_extract_tables(conn)
        records: list[dict[str, Any]] = []
        for table, sql in _DAY_SCOPED_SQL.items():
            cur = conn.execute(sql, (day,))
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                records.append({"_table": table, **dict(zip(cols, row, strict=True))})
        return records
    finally:
        conn.close()
