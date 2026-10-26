"""P2 主动询问候选池：四类来源 + 可解释打分 + 依据校验。

设计文档：``apps/withlanggraph/docs/proactive-inquiry-2.0.md`` §4/§5/§9。

来源与口径（全部只读本地既有数据，不新增表、不新增采集链路）：

- ``chat_loop``：主线程最后一条用户消息里的「回头再说 / 明天再弄 / 等忙完」类承诺句；
- ``kg_todo``：抽取层 ``kg_edges`` 中 ``src='todos'`` 且 ``valid_to IS NULL`` 的未完成待办；
- ``langtrack``：``daily_stats`` 的连续晚睡 / 屏幕时间异常（只取可解释的异常，不取单日噪声）；
- ``memory_conflict``：日报「画像候选」链路里仍 pending 的画像条目（长期画像缺口）。

打分权重与阈值与设计文档一字不差：:

    score = 0.30*gain + 0.25*freshness + 0.20*specificity
          + 0.15*answer_likelihood + 0.10*novelty - 0.30*intrusion

低于 ``_THRESHOLD`` 不入发送队列；同一 topic 7 天冷却与「已回答永不再问」由
``qa_store.seed_candidates`` 落库时兜底。所有采集器 best-effort：库/表缺失或数据不足
一律返回空，候选池永远不能拖垮主动询问主链路。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("proactive_candidates")

_TZ = timezone(timedelta(hours=8))

SOURCE_CHAT_LOOP = "chat_loop"
SOURCE_KG_TODO = "kg_todo"
SOURCE_LANGTRACK = "langtrack"
SOURCE_MEMORY_CONFLICT = "memory_conflict"

_THRESHOLD: float = 0.55
_MAX_PER_SOURCE: int = 3
_TOPIC_COOLDOWN_DAYS: int = 7
_HOT_CHAT_MINUTES: int = 30

_WEIGHT_GAIN = 0.30
_WEIGHT_FRESHNESS = 0.25
_WEIGHT_SPECIFICITY = 0.20
_WEIGHT_ANSWER = 0.15
_WEIGHT_NOVELTY = 0.10
_WEIGHT_INTRUSION = 0.30

_SILENT_START = (23, 30)
_SILENT_END = (7, 30)

_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[-/月]\d{1,2}日?")
_NUM_RE = re.compile(r"\d{1,2}:\d{2}|\d+(?:\.\d+)?%?")
_NOISE_RE = re.compile(r"[\d\s，。？！？、:：;；()（）\[\]【】,.\-—/\"'“”‘’]+")

_COMMITMENT_RE = re.compile(
    r"(回头再说|回头再聊|回头弄|回头再弄|明天再弄|明天再说|等忙完|忙完再|"
    r"晚点再说|稍后再说|待会再说|下次再说|之后再弄|改天再弄|等有空)"
)
_KG_TODO_SQL = """
SELECT e.edge_id, e.assertion_text, e.valid_from, e.relation,
       COALESCE(nf.label, e.from_node) AS from_label,
       COALESCE(nt.label, e.to_node) AS to_label
FROM kg_edges e
LEFT JOIN kg_nodes nf ON nf.node_id = e.from_node
LEFT JOIN kg_nodes nt ON nt.node_id = e.to_node
WHERE e.invalidated = 0 AND e.src = 'todos' AND e.valid_to IS NULL
  AND (e.valid_from IS NULL OR e.valid_from >= ?)
ORDER BY (e.valid_from IS NULL), e.valid_from DESC, e.edge_id
LIMIT ?
"""
_LANGTRACK_SQL = """
SELECT day, SUM(total_screen_ms) AS ms, MAX(sleep_start_hhmm) AS sleep
FROM daily_stats
GROUP BY day
ORDER BY day DESC
LIMIT ?
"""


@dataclass
class Candidate:
    """统一候选结构（设计文档 §4）：来源不同但字段一致。"""

    source: str
    ref: str
    topic: str
    anchor: str
    question: str
    freshness: datetime
    gain: float
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def to_row(self) -> dict[str, Any]:
        """转成 ``qa_store.seed_candidates`` 接受的行。"""
        return {
            "source": self.source,
            "ref": self.ref,
            "topic": self.topic,
            "anchor": self.anchor,
            "question": self.question,
            "evidence": list(self.evidence),
            "freshness": self.freshness.isoformat(timespec="seconds"),
            "gain": round(float(self.gain), 3),
        }


@dataclass
class ScoreContext:
    """打分上下文：同一轮所有候选共享，避免逐条重复读库。"""

    now: datetime
    reply_by_source: dict[str, dict[str, int]]
    hot_chat: bool
    silent: bool
    recent_topics: set[str]
    recent_questions: tuple[str, ...]


def _east8(dt: datetime) -> datetime:
    return dt.astimezone(_TZ) if dt.tzinfo else dt.replace(tzinfo=_TZ)


def _as_datetime(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return _east8(value)
    raw = str(value or "").strip()
    if not raw:
        return fallback
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[: len(fmt) + 2], fmt).replace(tzinfo=_TZ)
        except ValueError:
            continue
    return fallback


def _bigrams(text: str) -> set[str]:
    clean = _NOISE_RE.sub("", text or "")
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _similar(a: str, b: str) -> bool:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return False
    return len(ba & bb) / len(ba | bb) > 0.6


def _topic_of(cfg: Config, text: str) -> str:
    from gacore.qa_store import topic_of

    return topic_of(cfg, text)


def _snippet(text: str, limit: int = 40) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _in_silent_window(now: datetime) -> bool:
    h, m = now.hour, now.minute
    start = _SILENT_START[0] * 60 + _SILENT_START[1]
    end = _SILENT_END[0] * 60 + _SILENT_END[1]
    cur = h * 60 + m
    return cur >= start or cur < end


# --------------------------------------------------------------------------- collectors


def collect_chat_loop(cfg: Config, now: datetime, user_id: str = "") -> list[Candidate]:
    """主线程最后一条用户消息里的承诺句（``recall_topic`` 的承诺句扩展）。"""
    if not user_id:
        return []
    from gacore import proactive

    thread_id = proactive._thread_id_of(cfg, user_id)
    if not thread_id:
        return []
    messages = proactive._read_thread_messages(cfg, thread_id)
    if not messages:
        return []
    last_idx = -1
    for i, msg in enumerate(messages):
        if proactive._msg_type(msg) == "human":
            last_idx = i
    if last_idx < 0:
        return []
    text = proactive._msg_text(messages[last_idx]).strip()
    if not text or len(text) > 200 or not _COMMITMENT_RE.search(text):
        return []
    snippet = _snippet(text, 60)
    return [
        Candidate(
            source=SOURCE_CHAT_LOOP,
            ref=f"{thread_id}:msg{last_idx}",
            topic=_topic_of(cfg, text),
            anchor=f"主线程最后一条消息：{snippet}",
            question=f"你之前说「{snippet}」，后来弄了吗？",
            freshness=now,
            gain=0.55,
            evidence=tuple(_NUM_RE.findall(text)),
        )
    ]


def collect_kg_todo(cfg: Config, now: datetime, limit: int = _MAX_PER_SOURCE) -> list[Candidate]:
    """抽取层未闭合待办（``valid_to IS NULL``），只取近 14 天的。"""
    db = cfg.root / "data" / "langTrack.db"
    if not db.is_file():
        return []
    cutoff = (now - timedelta(days=14)).date().isoformat()
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(_KG_TODO_SQL, (cutoff, limit)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(
            "candidate: kg_todo query failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
        )
        return []
    out: list[Candidate] = []
    for edge_id, assertion, valid_from, relation, from_label, to_label in rows:
        assertion = str(assertion or "").strip()
        subject = assertion or f"{from_label} / {to_label}"
        if not subject:
            continue
        if relation == "promised":
            question = f"你答应过的「{_snippet(subject, 50)}」，后来兑现了吗？"
        elif relation == "plans":
            question = f"你之前计划「{_snippet(subject, 50)}」，还在推进吗？"
        elif relation == "blocked_by":
            question = f"「{_snippet(subject, 50)}」现在还被卡着吗？"
        else:
            question = f"「{_snippet(subject, 50)}」这件事后来做了吗？"
        day = str(valid_from or "")[:10]
        fresh = _as_datetime(valid_from, now)
        gain = 0.85 if (now - fresh) <= timedelta(days=7) else 0.70
        out.append(
            Candidate(
                source=SOURCE_KG_TODO,
                ref=f"kg_edges:{edge_id}",
                topic=_topic_of(cfg, subject),
                anchor=f"{day or '近期'} 未完成待办：{subject}",
                question=question,
                freshness=fresh,
                gain=gain,
                evidence=tuple(_NUM_RE.findall(subject)) + ((day,) if day else ()),
            )
        )
    return out


def _parse_hhmm(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if len(text) < 4 or ":" not in text:
        return None
    try:
        h, m = text.split(":", 1)
        hh, mm = int(h), int(m[:2])
    except ValueError:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh * 60 + mm


def _consecutive_days(days: list[str]) -> bool:
    if len(days) < 2:
        return True
    parsed = []
    for d in days:
        try:
            parsed.append(date.fromisoformat(d))
        except ValueError:
            return False
    return all((parsed[i] - parsed[i + 1]).days == 1 for i in range(len(parsed) - 1))


def collect_langtrack(cfg: Config, now: datetime) -> list[Candidate]:
    """langTrack 可解释异常：连续 3 天晚睡 / 近 3 天屏幕时间显著上浮。"""
    db = cfg.root / "data" / "langTrack.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(_LANGTRACK_SQL, (35,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(
            "candidate: langtrack query failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
        )
        return []

    out: list[Candidate] = []
    recent3 = rows[:3]
    if len(recent3) == 3:
        days = [str(r[0]) for r in recent3]
        sleeps = [_parse_hhmm(r[2]) for r in recent3]
        if (
            _consecutive_days(days)
            and all(s is not None and 120 <= s < 720 for s in sleeps)
        ):
            times = "/".join(str(r[2])[:5] for r in recent3)
            out.append(
                Candidate(
                    source=SOURCE_LANGTRACK,
                    ref=f"daily_stats:sleep:{days[-1]}~{days[0]}",
                    topic="作息",
                    anchor=f"{days[-1]}~{days[0]} 连续三天入睡晚于 02:00（{times}）",
                    question=f"最近三天都两点后才睡（{days[-1]}–{days[0]}），是事情多还是作息乱了？",
                    freshness=_as_datetime(days[0], now),
                    gain=0.75,
                    evidence=(days[-1], days[0], *[str(r[2])[:5] for r in recent3]),
                )
            )

    baseline = [float(r[1] or 0) for r in rows[3:]]
    if len(recent3) == 3 and len(baseline) >= 7:
        base_avg = sum(baseline) / len(baseline)
        recent_avg = sum(float(r[1] or 0) for r in recent3) / 3
        if base_avg > 0 and recent_avg >= base_avg * 1.5:
            days = [str(r[0]) for r in recent3]
            pct = int(round((recent_avg / base_avg - 1) * 100))
            out.append(
                Candidate(
                    source=SOURCE_LANGTRACK,
                    ref=f"daily_stats:screen:{days[-1]}~{days[0]}",
                    topic="屏幕",
                    anchor=(
                        f"{days[-1]}~{days[0]} 日均屏幕 {recent_avg / 3600000:.1f}h，"
                        f"此前 {len(baseline)} 天日均 {base_avg / 3600000:.1f}h（+{pct}%）"
                    ),
                    question=f"最近三天屏幕时间比平时多了 {pct}%，在忙什么？",
                    freshness=_as_datetime(days[0], now),
                    gain=0.60,
                    evidence=(str(pct), f"{recent_avg / 3600000:.1f}", f"{base_avg / 3600000:.1f}"),
                )
            )
    return out


def collect_memory_conflict(cfg: Config, now: datetime, limit: int = _MAX_PER_SOURCE) -> list[Candidate]:
    """日报「画像候选」链路里仍 pending 的画像条目（不新增 LLM 扫描）。"""
    from gacore.weekly_stats import pending_candidates

    out: list[Candidate] = []
    for row in pending_candidates(cfg, limit=limit):
        answer = str(row.get("answer") or "").strip()
        if len(answer) < 2:
            continue
        snippet = _snippet(answer, 40)
        ts = str(row.get("answered_at") or row.get("created_at") or "")
        day = ts[:10]
        out.append(
            Candidate(
                source=SOURCE_MEMORY_CONFLICT,
                ref=f"qa_candidate:{row.get('question_key', '')}",
                topic=_topic_of(cfg, answer),
                anchor=f"{day or '近期'} 画像候选：{snippet}",
                question=f"上次你说「{snippet}」，我准备记进长期画像——现在还成立吗？",
                freshness=_as_datetime(ts, now),
                gain=0.80,
                evidence=tuple(_NUM_RE.findall(answer)) + ((day,) if day else ()),
            )
        )
    return out


def collect_all(cfg: Config, now: datetime, user_id: str = "") -> list[Candidate]:
    """采集四类来源；单个来源失败只记日志，不中断其余来源。"""
    now = _east8(now)
    out: list[Candidate] = []
    collectors = (
        ("chat_loop", lambda: collect_chat_loop(cfg, now, user_id)),
        ("kg_todo", lambda: collect_kg_todo(cfg, now)),
        ("langtrack", lambda: collect_langtrack(cfg, now)),
        ("memory_conflict", lambda: collect_memory_conflict(cfg, now)),
    )
    for name, fn in collectors:
        try:
            out.extend(fn())
        except Exception as exc:  # noqa: BLE001 — 候选池 best-effort
            logger.error(
                "candidate: collector failed",
                source=name,
                error_type=type(exc).__name__,
                stack_trace=str(exc),
            )
    return out


# --------------------------------------------------------------------------- scoring


def _freshness_score(cand: Candidate, now: datetime) -> float:
    age_days = (now - _east8(cand.freshness)).total_seconds() / 86400
    if age_days <= 1:
        return 1.0
    if age_days >= 7:
        return 0.0
    return round(1 - (age_days - 1) / 6, 4)


def _specificity(anchor: str) -> float:
    text = anchor or ""
    if _DATE_RE.search(text):
        return 1.0
    if re.search(r"\d", text):
        return 0.8
    return 0.3 if len(text) >= 8 else 0.0


def _answer_likelihood(source: str, ctx: ScoreContext) -> float:
    stats = ctx.reply_by_source.get(source) or {}
    asked = int(stats.get("asked") or 0)
    if asked < 1:
        return 0.5
    return round(int(stats.get("answered") or 0) / asked, 4)


def _novelty(cand: Candidate, ctx: ScoreContext) -> float:
    if cand.topic and cand.topic in ctx.recent_topics:
        return 0.0
    if any(_similar(cand.question, q) for q in ctx.recent_questions):
        return 0.0
    return 1.0


def _intrusion(cand: Candidate, ctx: ScoreContext) -> float:
    score = 0.0
    if ctx.silent or ctx.hot_chat:
        score = 1.0
    stats = ctx.reply_by_source.get(cand.source) or {}
    asked = int(stats.get("asked") or 0)
    ignored = int(stats.get("ignored") or 0)
    if asked >= 2 and ignored / asked >= 0.8:
        score = 1.0
    elif ignored:
        score = max(score, 0.5)
    return score


def score_candidate(cand: Candidate, ctx: ScoreContext) -> dict[str, float]:
    """可解释打分（设计文档 §5），返回各维度与总分。"""
    parts = {
        "gain": round(float(cand.gain), 4),
        "freshness": _freshness_score(cand, ctx.now),
        "specificity": _specificity(cand.anchor),
        "answer_likelihood": _answer_likelihood(cand.source, ctx),
        "novelty": _novelty(cand, ctx),
        "intrusion": _intrusion(cand, ctx),
    }
    score = (
        _WEIGHT_GAIN * parts["gain"]
        + _WEIGHT_FRESHNESS * parts["freshness"]
        + _WEIGHT_SPECIFICITY * parts["specificity"]
        + _WEIGHT_ANSWER * parts["answer_likelihood"]
        + _WEIGHT_NOVELTY * parts["novelty"]
        - _WEIGHT_INTRUSION * parts["intrusion"]
    )
    parts["score"] = round(score, 4)
    return parts


def build_context(cfg: Config, now: datetime, user_id: str = "") -> ScoreContext:
    from gacore import proactive, qa_store

    now = _east8(now)
    state = proactive.load_state(cfg)
    entry = state.get(f"u_{user_id}") or {} if user_id else {}
    last_active = proactive._parse_iso(str(entry.get("last_active") or ""))
    hot_chat = (
        last_active is not None
        and (now - _east8(last_active)) < timedelta(minutes=_HOT_CHAT_MINUTES)
    )
    stats = qa_store.reply_stats(cfg, days=30, now=now)
    cutoff = (now - timedelta(days=_TOPIC_COOLDOWN_DAYS)).date().isoformat()
    recent = [
        row
        for row in qa_store.all_rows(cfg)
        if str(row.get("day") or "") >= cutoff
    ]
    recent_topics = {
        str(row.get("topic") or "") or _topic_of(cfg, str(row.get("question") or ""))
        for row in recent
    }
    return ScoreContext(
        now=now,
        reply_by_source=dict(stats.get("by_source") or {}),
        hot_chat=hot_chat,
        silent=_in_silent_window(now),
        recent_topics={t for t in recent_topics if t},
        recent_questions=tuple(str(row.get("question") or "") for row in recent),
    )


def select(
    cfg: Config,
    now: datetime,
    *,
    user_id: str = "",
    limit: int = _MAX_PER_SOURCE,
) -> list[Candidate]:
    """采集 → 打分 → 阈值过滤 → 同源分组取最高分来源 → 去重，返回入选候选。

    一条主动消息里的问题必须同源或同主题（设计文档 §5 批量规则），因此这里只取
    得分最高的一类来源，其余候选留在池里等下一个时段。
    """
    from gacore.qa_store import question_key

    now = _east8(now)
    cands = collect_all(cfg, now, user_id)
    if not cands:
        return []
    ctx = build_context(cfg, now, user_id)
    groups: dict[str, list[tuple[float, Candidate]]] = {}
    for cand in cands:
        parts = score_candidate(cand, ctx)
        passed = parts["score"] >= _THRESHOLD
        logger.info(
            "proactive.candidate",
            source=cand.source,
            ref=cand.ref,
            score=parts["score"],
            selected=passed,
            gain=parts["gain"],
            freshness=parts["freshness"],
            specificity=parts["specificity"],
            answer_likelihood=parts["answer_likelihood"],
            novelty=parts["novelty"],
            intrusion=parts["intrusion"],
        )
        if not passed:
            continue
        groups.setdefault(cand.source, []).append((parts["score"], cand))
    if not groups:
        return []
    best_source = max(groups, key=lambda src: max(sc for sc, _ in groups[src]))
    picked = sorted(groups[best_source], key=lambda item: (-item[0], item[1].ref))[:limit]
    seen: set[str] = set()
    out: list[Candidate] = []
    for _score, cand in picked:
        key = question_key(cfg, cand.question)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


# --------------------------------------------------------------------------- evidence


def verify_evidence(text: str, rows: list[dict[str, Any]]) -> list[str]:
    """依据校验：正文里的日期/数字必须能在候选依据里找到。

    返回无法回溯的 token（空列表 = 通过）。用于发送后的 ``proactive.evidence`` 审计：
    候选问题本身由模板从真实数据生成（日期/数字均来自来源行），因此入池即已可追溯；
    这里再兜一层，防止模型转述时凭空添数字。
    """
    allowed: set[str] = set()
    for row in rows or []:
        for value in row.get("evidence") or []:
            allowed.add(str(value))
        for value in (row.get("anchor"), row.get("question")):
            allowed.update(_NUM_RE.findall(str(value or "")))
    violations: list[str] = []
    for token in _NUM_RE.findall(text or ""):
        if token not in allowed and token not in violations:
            violations.append(token)
    return violations


__all__ = (
    "Candidate",
    "ScoreContext",
    "SOURCE_CHAT_LOOP",
    "SOURCE_KG_TODO",
    "SOURCE_LANGTRACK",
    "SOURCE_MEMORY_CONFLICT",
    "build_context",
    "collect_all",
    "collect_chat_loop",
    "collect_kg_todo",
    "collect_langtrack",
    "collect_memory_conflict",
    "score_candidate",
    "select",
    "verify_evidence",
)
