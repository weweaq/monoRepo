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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gacore.audit import _load_topic_synonyms, _topic_from_table
from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("qa_store")

_TZ = timezone(timedelta(hours=8))
_MAX_ASKS: int = 2  # 未答最多重问 2 期（spec §5.3）
_IGNORED_AFTER_HOURS: int = 24  # 发出后超过该时长仍无回答，记一次 ignored_count
_SIMILAR_JACCARD: float = 0.6  # 同期 token(2-gram) 重合阈值
_BARE_ANSWER_MAX_CHARS: int = 30
_ANCHOR_RE = re.compile(r"^\s*-\s*\[想问你-(\d+)\]\s*(.*)$")
# 显式回答：必须带方括号（裸 `Q3 财报什么时候发？` 这类正常消息不得被吞成答案）
_BRACKET_RE = re.compile(r"^\s*\[\s*(想问你-|[Qq])\s*(\d+)\s*\]\s*(.*)$")
_BARE_ANCHOR_RE = re.compile(r"^\s*想问你-\s*(\d+)\s*(.*)$")
_ANSWER_WORD_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
# 像问句的内容不是答案：裸答只在"唯一待答"时兜底，不能把「Q3 财报什么时候发？」记成回答
_LOOKS_LIKE_QUESTION_RE = re.compile(r"[?？]\s*$|吗\s*$|什么|怎么|为什么|为啥|哪|谁|多少|是不是|有没有")
# 周报交互前缀：识别到就交回周报解析器，绝不进裸答（否则「评分 5 5」会被记成问题答案）
_WEEKLY_CMD_RE = re.compile(r"^\s*(?:评分|打分|候选)")
_WEEK_ID_RE = re.compile(r"^\d{4}-W\d{2}$")
_NOISE_RE = re.compile(r"[\d\s，。？！？、:：;；()（）\[\]【】,.\-—/\"'“”‘’]+")


def _now_dt() -> datetime:
    return datetime.now(tz=_TZ)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


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


def _day_as_date(day: str) -> str:
    """把行的日锚归一成 ISO 日期：`YYYY-Www`（周报提问）折算到该周周日，其余非日期返回空串。"""
    if _WEEK_ID_RE.match(day):
        year, num = day.split("-W")
        try:
            monday = date.fromisocalendar(int(year), int(num), 1)
        except ValueError:
            return ""
        return (monday + timedelta(days=6)).isoformat()
    return day if len(day) == 10 and day[4] == "-" else ""


def _load_open_state(cfg: Config) -> dict[str, Any]:
    path = _qa_dir(cfg) / "open_state.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def set_prompt_order(cfg: Config, keys: list[str]) -> None:
    """记录本次推送的编号顺序：[Q1]..[Qn] 与用户看到的消息顺序一一对应（解析入口用它）。"""
    state = _load_open_state(cfg)
    state["day"] = _today()
    state["keys"] = list(keys)
    state.setdefault("question_key", "")
    path = _qa_dir(cfg) / "open_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _refresh_open_state(cfg: Config) -> None:
    open_ = [r for r in _read_rows(cfg) if r.get("status") == "open"]
    single = open_[0]["question_key"] if len(open_) == 1 else ""
    prev = _load_open_state(cfg)
    # keys 是「用户看到的那条消息」的不可变快照：只由 set_prompt_order 整体替换。
    # 这里若按 open 集合过滤，答掉 Q1 后编号会整体前移，后续 [Q2] 就会指到第 3 题。
    path = _qa_dir(cfg) / "open_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "day": prev.get("day") or _today(),
                "question_key": single,
                "keys": list(prev.get("keys") or []),
            },
            ensure_ascii=False,
        ),
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
            # P0 提问账本（proactive-inquiry-2.0 §8）：来源 + 首次/最近发送 + 连续未回次数。
            # 旧行缺这些字段时一律按空/0 读取，不影响既有解析。
            "source": "qa_open",
            "asked_at": "",
            "delivered_at": "",
            "ignored_count": 0,
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


def open_questions(cfg: Config, *, before_date: str | None = None) -> list[dict[str, Any]]:
    """未答问题（按日锚+锚点排序）；`before_date` 为 as-of 栅栏（周报取本周口径用）。"""
    rows = [r for r in _read_rows(cfg) if r.get("status") == "open"]
    if before_date:
        rows = [r for r in rows if (_day_as_date(str(r.get("day", ""))) or "") < before_date]
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


def mark_pushed(cfg: Config, keys: list[str], *, now: datetime | None = None) -> None:
    """推送成功后 asks+1，并落提问账本（首次 asked_at / 最近 delivered_at）。

    P0 只记账不改行为：``asks`` 仍是重问预算的唯一依据；``asked_at`` 只在首次发送时写入，
    ``delivered_at`` 每次覆盖，供 ignored_count 与 7 日回复率统计使用。
    """
    keyset = set(keys)
    ts = (now or _now_dt()).astimezone(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    for row in _read_rows(cfg):
        if row["question_key"] in keyset and row.get("status") == "open":
            row["asks"] = int(row.get("asks") or 0) + 1
            if not row.get("asked_at"):
                row["asked_at"] = ts
            row["delivered_at"] = ts
            row["updated_at"] = _now()
            _rewrite_row(cfg, row)


def _parse_ts(raw: Any) -> datetime | None:
    """Parse the ``YYYY-MM-DD HH:MM:SS`` timestamps written by this module."""
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ)
    except (TypeError, ValueError):
        return None


def mark_ignored(
    cfg: Config,
    *,
    now: datetime | None = None,
    after_hours: int = _IGNORED_AFTER_HOURS,
) -> int:
    """给"发出后超时仍未答"的问题记一次 ignored_count（P0 账本）。

    以 ``delivered_at`` 为锚：同一轮发送最多记一次（``ignored_marked_for`` 幂等标记），
    之后若再次推送，delivered_at 变化，下一轮超时才能再记一次。只追加计数，
    不影响 ``status``/``asks``/重问预算，因此对现有推送行为零改动。
    """
    cutoff = (now or _now_dt()).astimezone(_TZ) - timedelta(hours=after_hours)
    n = 0
    for row in _read_rows(cfg):
        if row.get("status") != "open":
            continue
        delivered = str(row.get("delivered_at") or "")
        sent_dt = _parse_ts(delivered)
        if sent_dt is None or sent_dt > cutoff:
            continue
        if row.get("ignored_marked_for") == delivered:
            continue
        row["ignored_count"] = int(row.get("ignored_count") or 0) + 1
        row["ignored_marked_for"] = delivered
        row["updated_at"] = _now()
        _rewrite_row(cfg, row)
        n += 1
    if n:
        logger.info("qa ignored questions counted", count=n, after_hours=after_hours)
    return n


def reply_stats(cfg: Config, *, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    """近 N 天提问/回答/忽略统计（P0 验收：ops 能看到真实 7 日回复率）。

    分母是"窗口内实际发出过的问题"（``asked_at`` 落在窗口内）；只统计发过的问题，
    避免把尚未推送的种子问题算成 0 回复。旧行没有 ``asked_at`` 时退回 ``delivered_at``。
    """
    current = (now or _now_dt()).astimezone(_TZ)
    cutoff = current - timedelta(days=days)
    today = current.date().isoformat()
    asked_rows: list[dict[str, Any]] = []
    asked_today = 0
    for row in _read_rows(cfg):
        asked_dt = _parse_ts(row.get("asked_at") or row.get("delivered_at") or "")
        if asked_dt is None or asked_dt < cutoff:
            continue
        asked_rows.append(row)
        if asked_dt.date().isoformat() == today:
            asked_today += 1
    answered = [r for r in asked_rows if r.get("status") == "answered"]
    ignored = [r for r in asked_rows if int(r.get("ignored_count") or 0) > 0]
    by_source: dict[str, dict[str, int]] = {}
    for row in asked_rows:
        source = str(row.get("source") or "qa_open")
        bucket = by_source.setdefault(source, {"asked": 0, "answered": 0, "ignored": 0})
        bucket["asked"] += 1
        if row.get("status") == "answered":
            bucket["answered"] += 1
        if int(row.get("ignored_count") or 0) > 0:
            bucket["ignored"] += 1
    asked = len(asked_rows)
    return {
        "window_days": days,
        "asked": asked,
        "asked_today": asked_today,
        "answered": len(answered),
        "ignored": len(ignored),
        "answer_rate": round(len(answered) / asked, 2) if asked else 0.0,
        "by_source": by_source,
    }


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


def _resolve_explicit(
    cfg: Config, kind: str, idx: int, open_: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """把 `[Qn]` / `[想问你-N]` 解析到具体问题（决策 29）。

    - `[想问你-N]`：锚点属于某一份正文，取**最近一期**含该锚点的问题（按归一日期比较，
      周报的 `YYYY-Www` 折算到周日）。
    - `[Qn]`：按推送时落盘的展示顺序（`set_prompt_order`，不可变快照）解析；没有快照时
      退化为"最近一期正文的第 n 条"。
    - 点名的编号已答/越界时**返回 None**，绝不 fall-through 到别的题（静默答错比不捕获更糟）。
    """
    if kind == "Q":
        keys = list(_load_open_state(cfg).get("keys") or [])
        if keys:
            if not 1 <= idx <= len(keys):
                return None
            key = keys[idx - 1]
            return next((r for r in open_ if r["question_key"] == key), None)
    same = [
        r for r in open_
        if int(re.sub(r"\D", "", r.get("anchor", "")) or 0) == idx
    ]
    if not same:
        return None
    return max(same, key=lambda r: _day_as_date(str(r.get("day", ""))) or "")


def try_answer(cfg: Config, content: str, *, allow_bare: bool = True) -> str | None:
    """QQ 捕获入口（决策 8：不走订正阶梯）。命中返回确认文本，未命中返回 None。"""
    content = (content or "").strip()
    if _WEEKLY_CMD_RE.match(content):
        # 评分/候选是周报交互的领地：即使本次不适用（已评分/无 pending）也不能被裸答吞掉
        return None
    open_ = open_questions(cfg)
    kind: str | None = None
    idx = 0
    text = ""
    m = _BRACKET_RE.match(content)
    if m:
        kind = "想问你" if m.group(1).startswith("想问你") else "Q"
        idx = int(m.group(2))
        text = (m.group(3) or "").strip()
    else:
        m2 = _BARE_ANCHOR_RE.match(content)
        if m2:
            kind = "想问你"
            idx = int(m2.group(1))
            text = (m2.group(2) or "").strip()
    if kind is not None:
        if not _ANSWER_WORD_RE.search(text):
            # 只发了锚点/标点：不当作裸回答吞掉（交回普通对话，由助手追问）
            return None
        row = _resolve_explicit(cfg, kind, idx, open_)
        if row and record_answer(cfg, row["question_key"], text):
            return f"✅ 已记下你对 [Q{idx}] 的回答，会进下一期日报。"
        return None
    if (
        allow_bare
        and open_
        and len(open_) == 1
        and int(open_[0].get("asks") or 0) > 0
        and 0 < len(content) <= _BARE_ANSWER_MAX_CHARS
        and not _LOOKS_LIKE_QUESTION_RE.search(content)
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
    """`_QA` 信息源取数（决策 24）：近 N 天已回答问答（新→旧）+ 未答问题。

    as-of 栅栏（决策 30）：只取 `answer_day <= before_date` 的回答、`day < before_date`
    的未答问题；否则历史重跑会把之后才发生的问答当成已知事实注入。
    """
    rows = _read_rows(cfg)
    anchor = before_date or _today()
    horizon = (datetime.fromisoformat(anchor) - timedelta(days=recent_days)).date().isoformat()
    answered = sorted(
        (
            r
            for r in rows
            if r.get("status") == "answered"
            and horizon < str(r.get("answer_day", "")) <= anchor
        ),
        key=lambda r: str(r.get("answer_day", "")),
        reverse=True,
    )
    open_ = [
        r
        for r in rows
        if r.get("status") == "open" and (_day_as_date(str(r.get("day", ""))) or "") < anchor
    ]
    return answered, open_
