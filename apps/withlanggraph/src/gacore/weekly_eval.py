"""周报评分与画像候选捕获（P4，spec §5.4，决策 17/22/27）。

- 评分：QQ 回 `像不像我 有用`（0-5 两数，可带第三数字=最不像条目与自由文本），
  落 `data/eval/human/{week}.json`（`rubric_version` 版本化，锚点改动才 +1）。
- 候选：周报渲染 `[候选-N]`，回 `N` 确认（多选 `1 3`）、`-N` 否掉、`0` 全否、
  未提及保持 pending（决策 27）。confirmed 追加 `global_mem_facts.txt`。
- pending 状态（`data/eval/human/pending.json`）3 天过期；捕获在 QA 之前、订正之前。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gacore.config import Config
from gacore.jsonl_logger import get_logger
from gacore.qa_store import set_candidate_status
from gacore.weekly_stats import pending_candidates

logger = get_logger("weekly_eval")

_TZ = timezone(timedelta(hours=8))
RUBRIC_VERSION: int = 1  # 评分锚点版本（spec：锚点一改 +1，跨版本只比趋势）
_PENDING_TTL_DAYS: int = 3
_RATING_RE = re.compile(r"^\s*([0-5])\s+([0-5])(?:\s+([1-9]\d?))?(?:\s+(.+))?\s*$")


def _eval_dir(cfg: Config) -> Path:
    return cfg.root / "data" / "eval" / "human"


def _pending_path(cfg: Config) -> Path:
    return _eval_dir(cfg) / "pending.json"


def _now_str() -> str:
    return datetime.now(tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")


def set_pending_weekly(cfg: Config, week: str) -> dict[str, Any]:
    """周报投递时登记待交互状态（评分 + 候选清单，供回执按编号配对）。"""
    cands = pending_candidates(cfg)
    pending = {
        "week": week,
        "candidates": [
            {"key": c.get("question_key", ""), "question": c.get("question", ""),
             "answer": c.get("answer", "")}
            for c in cands
        ],
        "rated": False,
        "created_at": _now_str(),
    }
    _pending_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    _pending_path(cfg).write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
    logger.info("weekly pending state set", week=week, candidates=len(cands))
    return pending


def load_pending(cfg: Config) -> dict[str, Any] | None:
    """读未过期的待交互状态；过期（>3 天）自动清除返回 None。"""
    path = _pending_path(cfg)
    if not path.is_file():
        return None
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    created = pending.get("created_at", "")
    try:
        created_dt = datetime.strptime(str(created), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ)
        if datetime.now(tz=_TZ) - created_dt > timedelta(days=_PENDING_TTL_DAYS):
            path.unlink(missing_ok=True)
            return None
    except ValueError:
        return None
    return pending if isinstance(pending, dict) else None


def _save_rating(cfg: Config, week: str, alive: int, useful: int, least: int | None, raw: str) -> None:
    path = _eval_dir(cfg) / f"{week}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "week": week,
        "alive": alive,
        "useful": useful,
        "rubric_version": RUBRIC_VERSION,
        "least_like_me": least,
        "raw": raw,
        "created_at": _now_str(),
        "updated_at": _now_str(),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("weekly rating saved", week=week, alive=alive, useful=useful)


def _confirm_to_facts(cfg: Config, question: str, answer: str) -> None:
    """confirmed 候选追加 global_mem_facts.txt（[date] 行格式）；向量同步靠记忆维护全量兜底。"""
    facts = cfg.memory_dir / "global_mem_facts.txt"
    facts.parent.mkdir(parents=True, exist_ok=True)
    day = datetime.now(tz=_TZ).date().isoformat()
    line = f"[{day}] fact: {question} → {answer}（周报候选确认）\n"
    with facts.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _apply_candidate_reply(cfg: Config, pending: dict[str, Any], text: str) -> str | None:
    """解析 `1 3` / `-2` / `0` 组合（决策 27）并落到 candidates.jsonl；非候选形态返回 None。"""
    tokens = text.split()
    confirm: list[int] = []
    reject: list[int] = []
    all_reject = False
    for tok in tokens:
        if tok == "0":
            all_reject = True
        elif tok.startswith("-") and tok[1:].isdigit():
            reject.append(int(tok[1:]))
        elif tok.isdigit():
            confirm.append(int(tok))
        else:
            return None
    if not (confirm or reject or all_reject):
        return None
    cands: list[dict[str, Any]] = pending.get("candidates") or []
    n = len(cands)
    confirmed_qa: list[tuple[str, str]] = []

    def _idx_ok(i: int) -> bool:
        return 1 <= i <= n

    for i in confirm:
        if not _idx_ok(i):
            return None
        row = cands[i - 1]
        if set_candidate_status(cfg, str(row.get("key", "")), "confirmed"):
            confirmed_qa.append((str(row.get("question", "")), str(row.get("answer", ""))))
    for i in reject:
        if not _idx_ok(i):
            return None
        set_candidate_status(cfg, str(cands[i - 1].get("key", "")), "rejected")
    if all_reject:
        for row in cands:
            set_candidate_status(cfg, str(row.get("key", "")), "rejected")
    for q, a in confirmed_qa:
        try:
            _confirm_to_facts(cfg, q, a)
        except OSError as exc:
            logger.error("weekly candidate facts append failed", error_type=type(exc).__name__)
    parts = []
    if confirm:
        parts.append("确认 " + "、".join(str(i) for i in confirm))
    if reject:
        parts.append("否掉 " + "、".join(str(i) for i in reject))
    if all_reject:
        parts.append("全部否掉")
    logger.info("weekly candidates updated", confirmed=len(confirm), rejected=len(reject), all_reject=all_reject)
    return "✅ 画像候选已更新（" + "；".join(parts) + "），确认项将进长期记忆。"


def try_weekly_interaction(cfg: Config, content: str) -> str | None:
    """QQ 捕获入口（优先级：评分 > 候选确认；均要求 pending 未过期）。未命中返回 None。"""
    pending = load_pending(cfg)
    if not pending:
        return None
    text = (content or "").strip()
    week = str(pending.get("week", ""))
    m = _RATING_RE.match(text)
    if m and not pending.get("rated"):
        least = int(m.group(3)) if m.group(3) else None
        _save_rating(cfg, week, int(m.group(1)), int(m.group(2)), least, text)
        pending["rated"] = True
        _pending_path(cfg).write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
        return f"✅ 已记录本周评分（像不像我 {m.group(1)} · 有用 {m.group(2)}）。"
    reply = _apply_candidate_reply(cfg, pending, text)
    if reply is not None:
        return reply
    return None


def push_weekly_notice(cfg: Config, week: str) -> int:
    """周报投递后的确定性 QQ 提醒（评分 + 候选操作说明）；失败只记日志。"""
    import asyncio

    from gacore.langTrack.qq_push import load_known_users, send_c2c

    users = load_known_users()
    targets = [uid for uid, info in users.items() if not info.get("is_sandbox")]
    if not targets:
        targets = list(users)
    message = (
        f"本周周报（{week}）已发邮件。\n"
        "· 打分：回「像不像我 有用」两个 0-5 数字，如「4 3」，可再跟一个最不像的条目号。\n"
        "· 画像候选：回「1」确认、「-1」否掉、「0」全部否掉。"
    )
    try:
        result = asyncio.run(send_c2c(message, targets, is_sandbox=False))
        sent = int(result.get("ok") or 0)
        logger.info("weekly notice pushed", week=week, sent=sent, failures=len(result.get("failures") or []))
        return sent
    except Exception as exc:  # noqa: BLE001 — 提醒失败绝不影响周报
        logger.error("weekly notice push failed", week=week, error_type=type(exc).__name__)
        return 0
