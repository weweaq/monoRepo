"""Event-driven long-term memory maintenance for gacore (阶段一：规则触发的画像修订).

The QQ bot's long-term memory (``memory/global_mem.txt`` L2 facts +
``memory/global_mem_insight.txt`` L1 insights) is today written only when the LLM
*chooses* to call ``start_long_term_update`` — append-only, no trigger, no revision:
a key fact that drifts (e.g. a wedding date moved 08 → 12) never corrects the longer
portrait. This module adds the missing "active merge" step.

Design follows the mainstream memory pipelines (Mem0 / LangMem / Letta): trigger → judge
→ write, but scoped to a single-user assistant and the batch-sediment philosophy we
already own (daily report = whole-day sediment; this is the spot-level complement that
only fires on persona keywords).

Three phases, each a pure function with an injectable judge:

  1. ``should_evoke(last_user_text)``  — cheap keyword trigger on persona anchors.
     No LLM, gates everything downstream so routine chat never costs a judge call.
  2. ``judge_relation(portrait, fact, judge)`` — LLM (or a fake in tests) decides one of
     ``MERGE`` / ``NEW`` / ``NOOP``, optionally returning a revised fact whose meaning is
     precision-updated against an existing portrait line.
  3. ``apply(verdict, cfg)`` — write to long-term memory: MERGE appends a dated revision
     line (history is preserved, never overwritten), NEW appends like the existing tool,
     NOOP is a no-op. Naming, dates, delimiters all follow project conventions.

Pure by design: no module-level mutation, cfg resolved once per call so tests always
inject ``Config.for_tests`` and never touch real memory. Compatible with the existing
append-only ``start_long_term_update``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

from pydantic import BaseModel

from gacore.config import Config
from gacore.jsonl_logger import get_logger

_FACTS_FILE: Final = "global_mem.txt"
_INSIGHTS_FILE: Final = "global_mem_insight.txt"
# Retrieval-ready fact portrait (see vector_store._portrait_lines): short statements the
# vector store feeds on. MERGE/NEW keep a compact mirror here so semantic recall stays
# fresh without depending on the verbose timestamped event log.
_RETRIEVAL_FACTS: Final = "global_mem_facts.txt"

_logger = get_logger("memory_maintenance")


class _StructuredVerdict(BaseModel):
    """Structured-output schema for the judge prompt (provider-agnostic)."""

    action: str
    revised: str = ""

# --------------------------------------------------------------------------- trigger (pluggable)

# Built-in default anchors (fallback when config/memory_trigger.json is absent).
# Production loads config/memory_trigger.json via KeywordTrigger — this constant only
# guarantees a deterministic behaviour (and test compatibility) without a config file.
# Drawn from the real portrait (婚/家/工作/健康/住址/作息/偏好). A blank or
# all-punctuation message never triggers; routine chit-chat that hits no anchor stays
# silent.
DEFAULT_ANCHORS: Final = frozenset({
    "婚", "婚礼", "领证", "登记", "喜酒", "彩礼", "聘礼", "婚纱", "婚期", "办酒", "订婚",
    "家", "搬家", "住", "房租", "买房", "看房", "地址", "小区", "通勤", "家里",
    "工作", "上班", "辞职", "离职", "换工作", "上班时间", "工位", "出差", "加班", "老板", "同事", "项目", "升职", "涨薪",
    "体检", "复查", "血压", "心率", "睡眠", "失眠", "吃药", "过敏", "生病", "医生", "医院", "手术",
    "妈妈", "爸爸", "爸妈", "对象", "女朋友", "男朋友", "老婆", "老公", "女儿", "儿子",
    "作息", "几点睡", "几点起", "饭点", "胃口", "习惯", "偏好", "喜欢", "讨厌", "目标", "计划", "打算", "婆婆", "丈母娘",
})

_TRIGGER_CONFIG: Final = "memory_trigger.json"


@dataclass
class TriggerResult:
    """Outcome of a trigger probe — enough detail to audit *why* it fired."""

    triggered: bool
    reason: str = "no_anchor"   # machine-readable tag for the audit trail
    matched: tuple[str, ...] = ()  # the category(ies) / anchors that matched
    context: str = ""           # optional opaque signal for the judge (e.g. top-k recall from vectors)


class MemoryTrigger(Protocol):
    """Pluggable trigger source for memory maintenance.

    ``probe`` decides whether a turn's latest user text warrants a memory-maintenance
    judgment. KeywordTrigger (present) is a cheap local gate; EmbeddingTrigger (future)
    will recall related portrait lines via vector similarity. Swapping the source must
    not touch the judge/apply pipeline — that is the whole point of the abstraction.
    """

    kind: str

    def probe(self, last_user_text: str) -> TriggerResult:
        ...


class KeywordTrigger:
    """Keyword-anchor trigger, loadable from config/memory_trigger.json.

    The anchor list lives in config (not code) so adding a new persona category — a
    pet, an investment, a side project — edits JSON and never needs a code change.
    Missing/corrupt config falls back to ``DEFAULT_ANCHORS`` (deterministic, like the
    proactive guard's behaviour). A blank message never triggers.
    """

    kind: Final = "keyword"

    def __init__(self, anchors: frozenset[str] = DEFAULT_ANCHORS) -> None:
        self.anchors = anchors

    @classmethod
    def from_config(cls, cfg: Config) -> "KeywordTrigger":
        path = cfg.root / "config" / _TRIGGER_CONFIG
        anchors: set[str] = set(DEFAULT_ANCHORS)
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict) and isinstance(raw.get("anchors"), dict):
                loaded: set[str] = set()
                for group in raw["anchors"].values():
                    if isinstance(group, list):
                        loaded.update(a for a in group if isinstance(a, str))
                if loaded:
                    # Config explicitly supplied anchors: use them (replacing, not merging,
                    # so a stale built-in never collides with a curated list).
                    anchors = loaded
        return cls(frozenset(anchors))

    def probe(self, last_user_text: str) -> TriggerResult:
        text = (last_user_text or "").strip()
        if not text:
            return TriggerResult(triggered=False)
        for anchor in self.anchors:
            if anchor in text:
                return TriggerResult(triggered=True, reason="anchor_hit", matched=(anchor,))
        return TriggerResult(triggered=False)


def should_evoke(last_user_text: str) -> bool:
    """Compatibility convenience: keyword-gate using the built-in default anchors.

    Prefer ``KeywordTrigger().probe`` in new call sites so the source stays swappable
    and the audit gets a matched-anchor tag; this function exists to keep existing
    callers/tests stable until they migrate to the trigger object.
    """
    return KeywordTrigger(DEFAULT_ANCHORS).probe(last_user_text).triggered


class VectorTrigger:
    """Semantic trigger: recall portrait lines by embedding similarity (阶段二).

    Probes the local pgvector store with the message's embedding; a close match fires the
    trigger and carries the recalled lines in ``context`` so the judge can compare the
    new fact against them directly. This is how "搬家到朝阳区" recalls the portrait line
    "家住朝阳" even without a shared literal keyword.

    Degrades gracefully: if the vector backend / embedding model is unavailable it
    reports ``triggered=False`` (reason ``vector_unavailable``) rather than raising, so a
    transient or un-configured vector store never breaks the memory pipeline.
    """

    kind: Final = "vector"

    def __init__(self, k: int = 3, threshold: float = 0.45) -> None:
        self.k = k
        self.threshold = threshold

    def probe(self, last_user_text: str) -> TriggerResult:
        text = (last_user_text or "").strip()
        if not text:
            return TriggerResult(triggered=False)
        try:
            from gacore import vector_store
            hits = vector_store.nearby(text, k=self.k, threshold=self.threshold)
        except Exception as exc:  # noqa: BLE001 — degrade to no-match on any backend issue
            _logger.warning("vector trigger unavailable, degrading to no-match",
                            error_type=type(exc).__name__, error=str(exc))
            return TriggerResult(triggered=False, reason="vector_unavailable")
        if not hits:
            return TriggerResult(triggered=False)
        context = "\n".join(f"- {h['content']} (sim {1 - h['dist']:.3f})" for h in hits)
        return TriggerResult(
            triggered=True,
            reason="vector_hit",
            matched=tuple(h["content"] for h in hits),
            context=context,
        )


class CombinedTrigger:
    """Fallback chain: keyword OR vector. Any fire triggers.

    Keyword is the cheap always-on gate (works offline, no model); vector adds semantic
    recall where the keyboard misses. If vector is unavailable it simply contributes
    nothing and the keyword decision stands.
    """

    kind: Final = "combined"

    def __init__(self, keyword: MemoryTrigger | None = None, vector: MemoryTrigger | None = None) -> None:
        self.keyword = keyword or KeywordTrigger()
        self.vector = vector or VectorTrigger()

    def probe(self, last_user_text: str) -> TriggerResult:
        kw = self.keyword.probe(last_user_text)
        if kw.triggered:
            return TriggerResult(triggered=True, reason="keyword_hit", matched=kw.matched)
        vec = self.vector.probe(last_user_text)
        if vec.triggered:
            return TriggerResult(
                triggered=True,
                reason="vector_hit",
                matched=vec.matched,
                context=vec.context,
            )
        return TriggerResult(triggered=False, reason="no_anchor", matched=kw.matched)


# --------------------------------------------------------------------------- judge

# Verdict vocabulary — matches the mainstream 'extract → merge' decision set (Mem0's
# ADD/UPDATE/DELETE/NONE, trimmed to the stable core).
VERDICT_MERGE: Final = "MERGE"
VERDICT_NEW: Final = "NEW"
VERDICT_NOOP: Final = "NOOP"
_VALID_VERDICTS: Final = frozenset({VERDICT_MERGE, VERDICT_NEW, VERDICT_NOOP})

# A merged fact line must name a struct + field and a negation of the drift, e.g.
#   [婚姻·登记日期] 改为 2026-09-12（原 2026-09-08）
# The header is how apply() groups revisions and how a later reader tells the latest value.
_ENSURE_FACT_RE: Final = re.compile(
    r"^\s*(?:用户近况|主题|persona|topic)\s*[：:]\s*",
    flags=re.IGNORECASE,
)


@dataclass
class Verdict:
    """Outcome of a memory-maintenance judgment."""

    action: str                     # MERGE / NEW / NOOP
    fact: str = ""                  # the fact/phrase to write (MERGE: revised value; NEW: new line)
    field_hint: str = ""            # optional [struct·field] hint for MERGE header grouping
    reason: str = ""                # machine-readable human summary, surfaced to logs
    meta: dict = field(default_factory=dict)  # free-form payload from the judge (parsed from LLM JSON)


def judge_relation(
    portrait: str,
    fact: str,
    judge: Callable[[str, str], Verdict],
) -> Verdict:
    """Decide how ``fact`` relates to the existing ``portrait``.

    ``judge`` is the interpretation seam: production wires an LLM (see ``build_llm_judge``);
    tests inject a deterministic fake. The portrait + fact are presented verbatim so the
    judge can see both existing lines and the new signal before labelling.
    """
    return judge(portrait, fact)


def build_llm_judge(
    cfg: Config,
    llm: object | None = None,
) -> Callable[[str, str], Verdict]:
    """Build an LLM-backed judge binding gacore's chat provider.

    ``llm`` is an injection seam (defaults to ``get_llm([])``); tests pass a stub whose
    ``invoke`` returns a crafted response. Strict JSON parse, verified action, and a
    prompt that forbids fabricating facts the user never stated.
    """
    from gacore.llm import get_llm

    model = llm if llm is not None else get_llm([])
    if hasattr(model, "with_structured_output"):
        # Prefer structured output when the provider supports it (Guaranteed json).
        structured = model.with_structured_output(_StructuredVerdict)
    else:
        structured = None

    def _judge(portrait: str, fact: str) -> Verdict:
        prompt = (
            "你是长期记忆维护器。下面是主人现有的长期画像，以及一条新对话信号。\n\n"
            f"【现有长期画像】\n{portrait or '（无）'}\n\n"
            f"【新信号】\n{fact}\n\n"
            "请判断这条信号与画像的关系，只输出 JSON：\n"
            '{"action":"MERGE|NEW|NOOP","revised":""}。\n'
            "规则：\n"
            "- MERGE：信号与画像中某条事实描述同一个事（如婚期/住址/工作），且包含了便于更新到画像的修订值，"
            "此时 revised 填能替换原画像该条的精确表述（带新值，可含『原 xxx』注明改动）；\n"
            "- NEW：信号是一条全新的、画像里没有的长期事实，revised 填该事实；\n"
            "- NOOP：信号只是闲聊、情绪、无长期价值或画像已涵盖，revised 留空。\n"
            "不得编造主人没说过的事实；拿不准就 NOOP。"
        )
        try:
            if structured is not None:
                raw = structured.invoke(prompt)
                action = str(raw.action or "").upper()
                revised = str(getattr(raw, "revised", "") or "")
            else:
                content = model.invoke(prompt).content
                action, revised = _extract_from_content(str(content))
        except Exception as exc:  # noqa: BLE001 — a judge failure must degrade to NOOP, never crash the turn
            _logger.warning("memory_maintenance judge failed, degrading to NOOP",
                            error_type=type(exc).__name__, error=str(exc))
            return Verdict(action=VERDICT_NOOP, reason=f"judge_error:{type(exc).__name__}", meta={"error": str(exc)})
        if action not in _VALID_VERDICTS:
            action = VERDICT_NOOP
        return Verdict(
            action=action,
            fact=revised or fact,
            reason=f"llm_verdict:{action}",
            meta={"raw_action": action, "revised": revised},
        )

    return _judge


def _extract_from_content(content: str) -> tuple[str, str]:
    """Best-effort JSON action/revised extraction from a plain-text model response."""
    m = re.search(r"\"action\"\s*:\s*[\"']?([A-Z]+)[\"']?", content)
    if not m:
        return "", ""
    action = m.group(1).upper()
    r = re.search(r"\"revised\"\s*:\s*[\"'](.*?)[\"']", content, flags=re.S)
    return action, (r.group(1).strip() if r else "")


# --------------------------------------------------------------------------- write

def _now_east8_ts() -> str:
    """East-8 ISO timestamp (seconds) — project canonical clock for user-facing times."""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def apply(verdict: Verdict, cfg: Config) -> dict:
    """Persist a judged verdict to long-term memory.

    - MERGE: append a dated revision line to ``global_mem.txt`` (grouped under an
      optional ``[struct·field]`` header), and a mirror line to the insight file.
      History is preserved — the OLD value stays in place, the new line carries the
      updated value, so nothing is ever destroyed.
    - NEW: append exactly like ``start_long_term_update``.
    - NOOP: nothing.

    Returns a short result dict reporting action and the files touched (mirrors
    ``memory_tools.start_long_term_update``'s return shape so callers stay uniform).
    """
    if verdict.action == VERDICT_NOOP:
        return {"action": VERDICT_NOOP, "updated": False, "paths": []}
    if not verdict.fact.strip():
        return {"aux": verdict.action, "action": VERDICT_NOOP, "updated": False, "paths": []}
    try:
        cfg.memory_dir.mkdir(parents=True, exist_ok=True)
        ts = _now_east8_ts()
        day = datetime.now(UTC).astimezone().date().isoformat()
        if verdict.action == VERDICT_MERGE:
            header, body = _split_header(verdict.fact)
            header = f"[{verdict.field_hint}]" if verdict.field_hint else header
            fact_line = f"[{ts}] {header} {body}"
            insight_line = f"[{day}] insight·merge: {header} {body}"
        else:  # NEW
            cleaned_fact = _strip_std_prefix(verdict.fact)
            fact_line = f"[{ts}] {cleaned_fact}"
            insight_line = f"[{day}] insight: {cleaned_fact}"
        _append(cfg.memory_dir / _FACTS_FILE, fact_line)
        _append(cfg.memory_dir / _INSIGHTS_FILE, insight_line)
        # Mirror a compact, retrieval-friendly statement into the facts portrait so the
        # vector store (which feeds on global_mem_facts.txt) tracks this update in
        # near-real-time. Best-effort: a write failure must not fail the main turn.
        try:
            _append(cfg.memory_dir / _RETRIEVAL_FACTS, _as_fact_statement(verdict))
        except OSError as exc:
            _logger.warning("facts portrait mirror failed", action=verdict.action, error=str(exc))
        return {
            "action": verdict.action,
            "updated": True,
            "fact": verdict.fact,
            "paths": [str(cfg.memory_dir / _FACTS_FILE), str(cfg.memory_dir / _INSIGHTS_FILE)],
        }
    except OSError as exc:
        _logger.warning("memory_maintenance apply failed", action=verdict.action, error=str(exc))
        return {"action": "ERROR", "updated": False, "paths": [], "error": str(exc)}


def _as_fact_statement(verdict: Verdict) -> str:
    """Turn a verdict into one compact, retrieval-friendly statement for the facts portrait.

    Uses the [struct·field] hint as a category tag when present (e.g. ``[婚姻]``), else a
    bare revised fact. Kept short so the embedding model sees a clean semantic unit, not a
    long event-log line.
    """
    category = f"[{verdict.field_hint}]" if verdict.field_hint else ""
    return f"{category} {verdict.fact}".strip()


def _strip_std_prefix(fact: str) -> str:
    """Drop a leading '用户近况：' / '主题：' prefix if the judge left one.

    Deliberately keeps any trailing [tag] — a fact the model wrote with its own header
    is preserved as-is (a bare bracketed date looks like that header's value).
    """
    text = fact.strip()
    return _ENSURE_FACT_RE.sub("", text).strip()


def _split_header(fact: str) -> tuple[str, str]:
    """Split a '[struct·field] value' fact into (header, body).

    Returns ('[MERGED]', fact) when no leading [tag] is present; strips a leading
    '用户近况：' prefix before splitting so a structured revised value normalises.
    """
    text = _ENSURE_FACT_RE.sub("", fact.strip()).strip()
    if text.startswith("[") and "]" in text:
        close = text.index("]") + 1
        return text[:close].strip(), text[close:].strip()
    return "[MERGED]", text


def _append(path, line: str) -> None:
    """Append a single line (trailing newline) to ``path``, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def load_portrait(cfg: Config) -> str:
    """Return the current long-term portrait text (insight file preferred, any fallback)."""
    insight = cfg.memory_dir / _INSIGHTS_FILE
    if insight.is_file():
        return insight.read_text(encoding="utf-8", errors="replace").strip()
    candidates = sorted(cfg.memory_dir.glob("global_mem*.txt"))
    for p in candidates:
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    return ""


def maintain_once(
    cfg: Config,
    last_user_text: str,
    judge: Callable[[str, str], Verdict],
    trigger: MemoryTrigger | None = None,
) -> dict:
    """End-to-end single maintenance pass: trigger → judge → apply.

    ``trigger`` defaults to a built-in keyword gate (deterministic, no config); pass an
    external source (``KeywordTrigger.from_config(cfg)``, or a future embedding source)
    to opt into the config-loaded anchors. Returns a result dict, or an early
    ``{"action": "SKIP", ...}`` when the trigger stays silent (no LLM cost).
    """
    trig = trigger or KeywordTrigger(DEFAULT_ANCHORS)
    tr = trig.probe(last_user_text)
    if not tr.triggered:
        return {
            "action": "SKIP", "triggered": False, "updated": False, "reason": "no_anchor",
            "trigger_kind": trig.kind, "matched": list(tr.matched),
        }
    portrait = load_portrait(cfg)
    # A vector trigger's context carries semantically-recalled portrait lines we want the
    # judge to compare the new fact against; surface it above the raw portrait so MERGE
    # judgments are made against the most relevant existing facts, not just the whole file.
    if tr.context:
        portrait = f"[语义召回的画像行]\n{tr.context}\n\n[现有长期画像]\n{portrait}"
    verdict = judge_relation(portrait, last_user_text, judge)
    result = apply(verdict, cfg)
    result["triggered"] = True
    result["trigger_kind"] = trig.kind
    result["matched"] = list(tr.matched) if tr.matched else []
    return result


__all__ = (
    "VERDICT_MERGE",
    "VERDICT_NEW",
    "VERDICT_NOOP",
    "DEFAULT_ANCHORS",
    "CombinedTrigger",
    "KeywordTrigger",
    "MemoryTrigger",
    "TriggerResult",
    "VectorTrigger",
    "Verdict",
    "apply",
    "build_llm_judge",
    "judge_relation",
    "load_portrait",
    "maintain_once",
    "should_evoke",
)