"""KG 抽取运行器：素材 → 通道 LLM 抽取 → 落库 → kg_runs 血缘。

独立 CLI 入口（决策 D2）：不挂进 ``etl.py``，保住 ETL"纯本地、确定性、可重建"的属性，
也便于独立测试（不触网、不触真实库）。

- 幂等：唯一索引 ``ux_kg_runs_day_hash_ver(day, raw_hash, prompt_ver)`` 命中且
  ``status='ok'`` → 整次跳过；非 ok 的历史运行复用同一 ``run_id`` 原地重跑。
- 失败隔离：通道并发执行，任一通道异常只记 ``failed``，不影响其他通道（整次 status=partial）。
- 超时兜底：单通道超过 ``KG_EXTRACT_CHANNEL_TIMEOUT``（默认 900s）未返回即记 failed，
  避免"LLM 客户端挂死在空闲连接"的通道把 ``asyncio.gather`` 无限拖住。
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from langchain_core.messages import HumanMessage, SystemMessage

from gacore.config import Config
from gacore.langTrack.extract.channels import CHANNELS
from gacore.langTrack.extract.intervals import reclose_day
from gacore.langTrack.extract.material import load_material, material_stats
from gacore.langTrack.extract.resolve import resolve_entities
from gacore.langTrack.extract.schema import migrate_extract_tables
from gacore.langTrack.extract.types import ChannelOutcome, ChannelSpec, ExtractContext

PROMPT_VER: Final = "kg@v1"
_PROMPT_RELPATH: Final = Path("config") / "kg_prompts"
_CN_TZ: Final = _dt.timezone(_dt.timedelta(hours=8))
_FENCE_RE: Final = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

_OK_STATUS: Final = "ok"

# 单通道超时兜底（秒）。正常一整天素材的单通道 LLM 调用约 4 分钟，此处留 ~4x 余量；
# 它兜的是"LLM 客户端挂死在空闲连接上、永不返回"这条路径——没有它，一个通道挂死会
# 让 asyncio.gather 无限等待，整轮抽取卡住（LLM_TIMEOUT 只能兜单次请求，兜不住重试叠加）。
_DEFAULT_CHANNEL_TIMEOUT: Final = 900.0


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


async def _extract_channel(
    spec: ChannelSpec,
    llm: Any,
    prompt: str,
    material_text: str,
    conn: sqlite3.Connection,
    ctx: ExtractContext,
) -> ChannelOutcome:
    """单通道：素材 → LLM（JSON 解析失败重试 1 次）→ 单事务落库。异常只记 failed。"""
    outcome = ChannelOutcome(key=spec.key)
    if not material_text.strip():
        outcome.status = "skipped"
        outcome.error = "该通道素材为空"
        return outcome
    try:
        messages = [SystemMessage(content=prompt), HumanMessage(content=material_text)]
        raw = _content_of(await llm.ainvoke(messages))
        try:
            payload = parse_json_payload(raw)
        except (ValueError, json.JSONDecodeError):
            raw = _content_of(await llm.ainvoke(messages))
            payload = parse_json_payload(raw)
        written, extra = spec.handle(conn, ctx, payload)
        outcome.rows = written
        outcome.extra = extra
    except Exception as exc:  # noqa: BLE001 - 失败隔离：单通道异常不拖垮整次运行
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
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
    material_text: str,
    conn: sqlite3.Connection,
    ctx: ExtractContext,
) -> list[ChannelOutcome]:
    timeout = _channel_timeout()
    tasks = [
        _extract_channel_guarded(spec, llm, prompts[spec.key], material_text, conn, ctx, timeout)
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
            _run_channels(specs, _resolve_llm(llm), prompts, material.text, conn, ctx)
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


def _resolve_llm(llm: Any) -> Any:
    """注入点：默认按 .env 的 provider 建无工具模型（与 feedback/memory_maintenance 同法）。"""
    if llm is not None:
        return llm
    from gacore.llm import get_llm

    return get_llm([], os.environ, bind_tools=False)


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
