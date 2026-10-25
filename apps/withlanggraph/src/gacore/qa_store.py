"""提问回路 qa 落库层（P3，spec §5.3）。

文件优先不建表（spec §6）：``data/qa/{date}.jsonl`` 每行一个问题，
``candidates.jsonl`` 画像候选（pending/confirmed/rejected），
``open_state.json`` 裸回答指针（有且仅有 1 条待答问题时生效）。

约定：已回答的 question_key 永不再问（seed 跳过）；未答最多重问 2 期
（push 计数，超限归档为"用户不关心"）；回答只影响下一期，不自动写长期画像。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gacore.audit import _load_topic_synonyms, _topic_from_table
from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("qa_store")

_TZ = timezone(timedelta(hours=8))
_MAX_ASKS: int = 2  # 未答最多重问 2 期（spec §5.3）
_SIMILAR_JACCARD: float = 0.6  # 同期 token(2-gram) 重合阈值
_BARE_ANSWER_MAX_CHARS: int = 30
_ANCHOR_RE = re.compile(r"^\s*-\s*\[想问你-(\d+)\]\s*(.*)$")
_EXPLICIT_RE = re.compile(r"^\s*(?:\[\s*)?(?:想问你-|[Qq])\s*(\d+)\s*\]?\s*(.+)$")
_NOISE_RE = re.compile(r"[\d\s，。？！？、:：;；()（）\[\]【】,.\-—/\"'“”‘’]+")


def _now() -> str:
    return datetime.now(tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now(tz=_TZ).date().isoformat()


def _qa_dir(cfg: Config) -> Path:
    return cfg.root / "data" / "qa"


def _day_files(cfg: Config) -> list[Path]:
    d = _qa_dir(cfg)
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def _read_rows(cfg: Config) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in _day_files(cfg):
        if f.name == "candidates.jsonl":
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "question_key" in row:
                rows.append(row)
    return rows


def _write_day(cfg: Config, day: str, rows: list[dict[str, Any]]) -> None:
    path = _qa_dir(cfg) / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def _rewrite_row(cfg: Config, row: dict[str, Any]) -> None:
    day = row["day"]
    path = _qa_dir(cfg) / f"{day}.jsonl"
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        data = json.loads(line)
        lines.append(json.dumps(row if data.get("question_key") == row["question_key"] else data,
                                ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def question_key(cfg: Config, question: str) -> str:
    """sha1(topic + 意图)：主题取词表命中（无则空），意图 = 问题文本去数字/日期/噪声后归一。"""
    topic = _topic_from_table(_load_topic_synonyms(cfg), question) or ""
    intent = _NOISE_RE.sub("", question).lower()
    return hashlib.sha1(f"{topic}|{intent}".encode("utf-8")).hexdigest()[:16]


def _bigrams(text: str) -> set[str]:
    clean = _NOISE_RE.sub("", text)
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _similar(a: str, b: str) -> bool:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return False
    return len(ba & bb) / len(ba | bb) > _SIMILAR_JACCARD


def _refresh_open_state(cfg: Config) -> None:
    open_ = [r for r in _read_rows(cfg) if r.get("status") == "open"]
    single = open_[0]["question_key"] if len(open_) == 1 else ""
    path = _qa_dir(cfg) / "open_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"day": _today(), "question_key": single}, ensure_ascii=False),
        encoding="utf-8",
    )


def seed_questions(cfg: Config, day: str, bullets: list[str]) -> dict[str, int]:
    """把投递正文「想问你」bullets 种入 qa 库；已答 key 与同期语义重复不再开放。"""
    existing = _read_rows(cfg)
    answered = {r["question_key"] for r in existing if r.get("status") == "answered"}
    day_rows = [r for r in existing if r.get("day") == day and r.get("kind") != "candidate"]
    day_file_rows = [r for r in day_rows]
    counts = {"seeded": 0, "dup_answered": 0, "dup_similar": 0}
    kept_texts = [r["question"] for r in day_file_rows]
    for bullet in bullets:
        m = _ANCHOR_RE.match(bullet.strip())
        if not m:
            continue
        num, text = m.group(1), m.group(2).strip()
        key = question_key(cfg, text)
        if key in answered:
            counts["dup_answered"] += 1
            continue
        if any(r["question_key"] == key for r in day_file_rows):
            continue
        if any(_similar(text, prev) for prev in kept_texts):
            counts["dup_similar"] += 1
            continue
        row = {
            "question_key": key,
            "question": text,
            "anchor": f"想问你-{num}",
            "day": day,
            "status": "open",
            "answer": "",
            "answer_day": "",
            "asks": 0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        day_file_rows.append(row)
        kept_texts.append(text)
        counts["seeded"] += 1
    _write_day(cfg, day, day_file_rows)
    _refresh_open_state(cfg)
    logger.info("qa questions seeded", day=day, **counts)
    return counts


def open_questions(cfg: Config) -> list[dict[str, Any]]:
    rows = [r for r in _read_rows(cfg) if r.get("status") == "open"]
    return sorted(rows, key=lambda r: (r.get("day", ""), int(re.sub(r"\D", "", r.get("anchor", "")) or 0)))


def record_answer(cfg: Config, key: str, answer: str) -> dict[str, Any] | None:
    """落回答：行转 answered + 画像候选 pending；返回更新后的行（未找到返回 None）。"""
    for row in _read_rows(cfg):
        if row["question_key"] != key or row.get("status") != "open":
            continue
        row["status"] = "answered"
        row["answer"] = answer
        row["answer_day"] = _today()
        row["updated_at"] = _now()
        _rewrite_row(cfg, row)
        cand_path = _qa_dir(cfg) / "candidates.jsonl"
        cand_path.parent.mkdir(parents=True, exist_ok=True)
        with cand_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {
                    "question_key": key,
                    "question": row["question"],
                    "answer": answer,
                    "status": "pending",
                    "created_at": _now(),
                    "updated_at": _now(),
                },
                ensure_ascii=False,
            ) + "\n")
        _refresh_open_state(cfg)
        logger.info("qa answer recorded", question_key=key, answer_len=len(answer))
        return row
    return None


def mark_pushed(cfg: Config, keys: list[str]) -> None:
    """推送成功后 asks+1（重问计数）；随后归档超限未答。"""
    keyset = set(keys)
    for row in _read_rows(cfg):
        if row["question_key"] in keyset and row.get("status") == "open":
            row["asks"] = int(row.get("asks") or 0) + 1
            row["updated_at"] = _now()
            _rewrite_row(cfg, row)


def archive_stale(cfg: Config, max_asks: int = _MAX_ASKS) -> int:
    """未答且 asks > max_asks 的问题归档为「用户不关心」，不再注入/推送。"""
    n = 0
    for row in _read_rows(cfg):
        if row.get("status") == "open" and int(row.get("asks") or 0) > max_asks:
            row["status"] = "archived"
            row["updated_at"] = _now()
            _rewrite_row(cfg, row)
            n += 1
    if n:
        _refresh_open_state(cfg)
        logger.info("qa stale questions archived", count=n)
    return n


def try_answer(cfg: Config, content: str, *, allow_bare: bool = True) -> str | None:
    """QQ 捕获入口（决策 8：不走订正阶梯）。命中返回确认文本，未命中返回 None。"""
    content = (content or "").strip()
    open_ = open_questions(cfg)
    m = _EXPLICIT_RE.match(content)
    if m:
        idx = int(m.group(1))
        text = m.group(2).strip()
        if 1 <= idx <= len(open_) and text:
            row = record_answer(cfg, open_[idx - 1]["question_key"], text)
            if row:
                return f"✅ 已记下你对 [Q{idx}] 的回答，会进下一期日报。"
        return None
    if (
        allow_bare
        and open_
        and len(open_) == 1
        and int(open_[0].get("asks") or 0) > 0
        and 0 < len(content) <= _BARE_ANSWER_MAX_CHARS
    ):
        row = record_answer(cfg, open_[0]["question_key"], content)
        if row:
            return "✅ 已记下你的回答，会进下一期日报。"
    return None


def all_rows(cfg: Config) -> list[dict[str, Any]]:
    """qa 库全量行（含 archived），供信息包 detail 与运维查看；内部读口的外公开。"""
    return _read_rows(cfg)


def set_candidate_status(cfg: Config, question_key: str, status: str) -> bool:
    """更新画像候选状态（pending/confirmed/rejected）；未找到该候选返回 False。"""
    path = _qa_dir(cfg) / "candidates.jsonl"
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    hit = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if isinstance(row, dict) and row.get("question_key") == question_key:
            row["status"] = status
            row["updated_at"] = _now()
            hit = True
            out.append(json.dumps(row, ensure_ascii=False))
        else:
            out.append(line)
    if hit:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return hit


def stats(cfg: Config) -> dict[str, Any]:
    """度量（spec §5.3）：每期提问数、回答率、去重命中。"""
    rows = _read_rows(cfg)
    answered = sum(1 for r in rows if r.get("status") == "answered")
    archived = sum(1 for r in rows if r.get("status") == "archived")
    per_day: dict[str, int] = {}
    for r in rows:
        per_day[r.get("day", "")] = per_day.get(r.get("day", ""), 0) + 1
    return {
        "total": len(rows),
        "answered": answered,
        "archived": archived,
        "answer_rate": round(answered / len(rows), 2) if rows else 0.0,
        "per_day": per_day,
    }


def qa_brief(
    cfg: Config, *, before_date: str | None = None, recent_days: int = 7
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`_QA` 信息源取数（决策 24）：近 N 天已回答问答（新→旧）+ 全部未答问题。"""
    rows = _read_rows(cfg)
    anchor = before_date or _today()
    horizon = (datetime.fromisoformat(anchor) - timedelta(days=recent_days)).date().isoformat()
    answered = sorted(
        (r for r in rows if r.get("status") == "answered" and str(r.get("answer_day", "")) > horizon),
        key=lambda r: str(r.get("answer_day", "")),
        reverse=True,
    )
    open_ = [r for r in rows if r.get("status") == "open"]
    return answered, open_
