"""日报断言基线闸（P1）：离线审计已投递日报里的时间量级断言。

设计（docs/superpowers/specs/2026-10-24-daily-report-eval-design.md）：

- 只读：输入是已投递日报与记忆库文件，本模块不写库、不联网、不调 LLM。
- 断言抽取：遮蔽引文/标题/时刻后，量级（N 天/周/月/年）与断言词同子句且距离 ≤12 才算断言。
- 基线查询：显式主题词表扫记忆库三源（daily/*.md + global_mem_insight + global_mem_facts）。
- 结果版本化落盘：v1 = 首次投递快照（含 report_text 冻结，决策 23），历史基线只认 v1。
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Final

from gacore.config import Config, load_dotenv
from gacore.jsonl_logger import get_logger

# ---------------------------------------------------------------- Task 1: 断言抽取

_NUM: dict[str, float] = {
    "半": 0.5, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}
_UNIT_DAYS: dict[str, int] = {"天": 1, "日": 1, "周": 7, "星期": 7, "个月": 30, "月": 30, "年": 365}

_DUR_RE = re.compile(r"(半|一|两|二|三|四|五|六|七|八|九|十|十一|十二|几|\d{1,3})\s*(个月|星期|年|月|周|天|日)")

_MASKS: tuple[re.Pattern[str], ...] = (
    re.compile(r"《[^》]*》"),
    re.compile(r"「[^」]*」"),
    re.compile(r"“[^”]*”"),
    re.compile(r"`[^`]*`"),
    re.compile(r"\*\*.*?\*\*"),
    re.compile(r"\b\d{1,2}:\d{2}\b"),
    re.compile(r"第\s*\d+\s*[期集话章]"),
)

_CLAIM_WORDS: tuple[str, ...] = (
    "沉寂", "没再", "没有再", "未再", "久未", "许久", "时隔", "隔了",
    "首次", "第一次", "重新", "再次", "连续", "持续", "半年", "数月", "多年", "久违",
)

_ANCHOR_RE = re.compile(r"^-\s*\[([^\]]+?)-(\d+)\]\s*")

_INLINE_ANCHOR_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}|上次|自\s*\d|"
    r"第\s*[一二三四五六七八九十\d]+\s*天|至今|此前|前一日|前夜|昨日|去年|上周)"
)

# 量级与断言词之间隔着句读 = 分属两个子句，不算同一断言（“40 年，话题首次”这类引文量级靠它排除）
_CLAUSE_BREAK = "，。；！？、：" + "…"


@dataclass(frozen=True, slots=True)
class Claim:
    section: str
    anchor: str | None
    text: str
    claim_word: str
    duration_text: str
    claimed_days: int | None
    anchored: bool


def _mask(line: str) -> str:
    for pat in _MASKS:
        line = pat.sub(" ", line)
    return line


def parse_duration_days(text: str) -> int | None:
    """把「半年/两个月/三周/4 日」这类时长短语换算成天数；换算不了返回 None。"""
    m = _DUR_RE.search(text)
    if not m:
        return None
    num_tok, unit = m.group(1), m.group(2)
    if num_tok == "几":
        return None
    if num_tok == "半" and unit == "年":
        return 180
    num = float(num_tok) if num_tok.isdigit() else _NUM.get(num_tok)
    if num is None:
        return None
    return int(num * _UNIT_DAYS[unit])


def _nearest_claim_word(masked: str, dur: re.Match[str]) -> str | None:
    """找与量级匹配同子句、距离 ≤12 的最近断言词；与量级区间重叠的词（如「半年」本身）不算。"""
    span_s, span_e = dur.span()
    best: str | None = None
    best_dist: int | None = None
    for word in _CLAIM_WORDS:
        start = 0
        while (idx := masked.find(word, start)) >= 0:
            start = idx + 1
            word_e = idx + len(word)
            if idx < span_e and word_e > span_s:
                continue
            if word_e <= span_s:
                dist, between = span_s - word_e, masked[word_e:span_s]
            else:
                dist, between = idx - span_e, masked[span_e:idx]
            if dist > 12 or any(ch in _CLAUSE_BREAK for ch in between):
                continue
            if best_dist is None or dist < best_dist:
                best, best_dist = word, dist
    return best


def extract_claims(report_text: str) -> list[Claim]:
    """按行扫日报：维护节标题、取 bullet 锚点，遮蔽后在同子句内配对量级与断言词。"""
    claims: list[Claim] = []
    section = ""
    for raw in report_text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip()
            continue
        if not stripped.startswith("-"):
            continue
        anchor_m = _ANCHOR_RE.match(stripped)
        anchor = f"{anchor_m[1]}-{anchor_m[2]}" if anchor_m else None
        masked = _mask(stripped)
        anchored = bool(_INLINE_ANCHOR_RE.search(masked))
        for dur in _DUR_RE.finditer(masked):
            word = _nearest_claim_word(masked, dur)
            if word is None:
                continue
            claims.append(
                Claim(
                    section=section,
                    anchor=anchor,
                    text=stripped,
                    claim_word=word,
                    duration_text=dur.group(0),
                    claimed_days=parse_duration_days(dur.group(0)),
                    anchored=anchored,
                )
            )
    return claims


# ------------------------------------------------------- Task 2: 主题识别与 last_seen 基线查询

# 初值来自 P0 探针的两条硬冲突主题；漏配 fail-open（查不到不判冲突），新主题持续补充
TOPIC_SYNONYMS: dict[str, tuple[str, ...]] = {
    "硬件": ("硬件", "ESP32", "STM32", "单片机", "嵌入式", "开发板"),
    "工学椅": ("工学椅", "人体工学椅", "西昊", "永艺", "黑白调"),
    "显示器": ("显示器", "MateView", "屏"),
    "桌面升级": ("桌面升级", "扩展坞", "升降桌"),
}

_GLOBAL_FILES: tuple[str, ...] = ("global_mem_insight.txt", "global_mem_facts.txt")
_DATE_PREFIX_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})")
_SNIPPET_RADIUS = 40


@dataclass(frozen=True, slots=True)
class LastSeen:
    found: bool
    date: str | None
    gap_days: int | None
    source: str
    snippet: str
    alt: tuple[str, ...] = ()


def find_topic(text: str) -> str | None:
    """返回文本命中的第一个主题名（按词表声明顺序）；无命中返回 None。"""
    return _topic_from_table(TOPIC_SYNONYMS, text)


def _topic_from_table(table: dict[str, tuple[str, ...]], text: str) -> str | None:
    for topic, synonyms in table.items():
        if any(syn in text for syn in synonyms):
            return topic
    return None


def _load_topic_synonyms(cfg: Config) -> dict[str, tuple[str, ...]]:
    """决策 28：主题词表外置 config/audit_topics.json（存在时整体覆盖内置初值）；解析失败回退内置。"""
    path = cfg.root / "config" / "audit_topics.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TOPIC_SYNONYMS
    table: dict[str, tuple[str, ...]] = {}
    if isinstance(raw, dict):
        for topic, words in raw.items():
            if isinstance(topic, str) and isinstance(words, list):
                synonyms = tuple(w for w in words if isinstance(w, str) and w)
                if synonyms:
                    table[topic] = synonyms
    return table or TOPIC_SYNONYMS


def _snippet_around(line: str, synonyms: tuple[str, ...]) -> str:
    idx = -1
    for syn in synonyms:
        idx = line.find(syn)
        if idx >= 0:
            break
    if idx < 0:
        return line.strip()
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(line), idx + _SNIPPET_RADIUS)
    return line[start:end].strip()


def _scan_daily(cfg: Config, synonyms: tuple[str, ...], before_date: str) -> list[tuple[str, str, str]]:
    """扫 memory/daily/*.md：日期取文件名前 10 位，返回 (date, source, snippet)。"""
    hits: list[tuple[str, str, str]] = []
    daily_dir = cfg.memory_dir / "daily"
    if not daily_dir.is_dir():
        return hits
    for f in sorted(daily_dir.glob("*.md")):
        day = f.name[:10]
        if len(day) != 10 or day[4] != "-" or day >= before_date:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if any(syn in line for syn in synonyms):
                hits.append((day, f"memory/daily/{f.name}", _snippet_around(line, synonyms)))
                break
    return hits


def _scan_global(cfg: Config, synonyms: tuple[str, ...], before_date: str) -> list[tuple[str, str, str]]:
    """扫 global_mem_*.txt：日期取行首 [YYYY-MM-DD]，返回 (date, source 带行号, snippet)。"""
    hits: list[tuple[str, str, str]] = []
    for name in _GLOBAL_FILES:
        path = cfg.memory_dir / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            m = _DATE_PREFIX_RE.match(line)
            if not m or m.group(1) >= before_date:
                continue
            if any(syn in line for syn in synonyms):
                hits.append((m.group(1), f"memory/{name}:{lineno}", _snippet_around(line, synonyms)))
    return hits


def _scan_reports(cfg: Config, synonyms: tuple[str, ...], before_date: str) -> list[tuple[str, str, str]]:
    """扫历史日报的 v1 冻结快照（决策 23）：只认 {date}.v1.json 的 report_text 字段。

    被订正就地重写的 delivered_report 当前文件、无 report_text 的快照一律不作为证据。
    """
    hits: list[tuple[str, str, str]] = []
    audit_dir = cfg.logs_dir / "report_audit"
    if not audit_dir.is_dir():
        return hits
    for f in sorted(audit_dir.glob("*.v1.json")):
        day = f.name[:10]
        if len(day) != 10 or day[4] != "-" or day >= before_date:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        text = data.get("report_text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text:
            continue
        for line in text.splitlines():
            if any(syn in line for syn in synonyms):
                hits.append((day, f"logs/report_audit/{f.name}", _snippet_around(line, synonyms)))
                break
    return hits


def last_seen(cfg: Config, topic: str, before_date: str) -> LastSeen:
    """在记忆三源 + 历史 v1 日报快照里查主题最近一次出现（严格早于 before_date）。

    `alt` 带上次近的 1-2 个命中日期（决策 37）：全局记忆里的回溯提及会把"最近提及"
    拉得很近，单看一个日期会误导（如 10-06 顺带提到工学椅 vs 真实上次事件 9-15）。
    """
    synonyms = _load_topic_synonyms(cfg).get(topic, (topic,))
    hits = (
        _scan_daily(cfg, synonyms, before_date)
        + _scan_global(cfg, synonyms, before_date)
        + _scan_reports(cfg, synonyms, before_date)
    )
    if not hits:
        return LastSeen(found=False, date=None, gap_days=None, source="", snippet="")
    ordered = sorted(hits, key=lambda h: h[0], reverse=True)
    day_str, source, snippet = ordered[0]
    base = date.fromisoformat(before_date)
    days: list[str] = []
    for d, _, _ in ordered:
        if d not in days:
            days.append(d)
    alt = tuple(f"{d}（{(base - date.fromisoformat(d)).days} 天）" for d in days[1:3])
    gap = (base - date.fromisoformat(day_str)).days
    return LastSeen(
        found=True, date=day_str, gap_days=gap, source=source, snippet=snippet, alt=alt
    )


# ------------------------------------------------------- Task 3: 冲突判定与单篇审计

# R2 容差带（决策 14）：claimed ≥ 2×actual 且 claimed − actual ≥ 14 天才判硬冲突，约数不冤枉
_FIRST_WORDS: tuple[str, ...] = ("首次", "第一次", "久违")
_RUNNING_WORDS: tuple[str, ...] = ("连续", "持续")
_WEEK_ID_RE = re.compile(r"^\d{4}-W\d{2}$")


def _report_path(cfg: Config, report_id: str) -> Path:
    """日报在 delivered_report/，周报（YYYY-Www）在 delivered_weekly/。"""
    if _WEEK_ID_RE.match(report_id):
        return cfg.logs_dir / "delivered_weekly" / f"{report_id}.md"
    return cfg.logs_dir / "delivered_report" / f"{report_id}.md"


def _anchor_day(report_id: str) -> str:
    """断言基线的日期锚点：日报 = 自身；周报 = 数据窗末日（周日）。"""
    if _WEEK_ID_RE.match(report_id):
        year, num = report_id.split("-W")
        return date.fromisocalendar(int(year), int(num), 7).isoformat()
    return report_id


def is_conflict(claimed_days: int, actual_gap_days: int) -> bool:
    return claimed_days >= 2 * actual_gap_days and claimed_days - actual_gap_days >= 14


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    severity: str
    section: str
    anchor: str | None
    claim: str
    claimed_days: int | None
    actual_last_seen: str | None
    actual_gap_days: int | None
    evidence: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    date: str
    version: int
    score: int
    missing: bool
    findings: tuple[Finding, ...]
    report_text: str = ""
    claims: tuple[Claim, ...] = ()


def _yesterday_claims(cfg: Config, before_date: str) -> list[Claim]:
    """读前一日 v1 快照的 report_text 抽断言（R5 跨篇对照语料）；无快照/非 ISO 日期返回空。"""
    try:
        prev = (date.fromisoformat(before_date) - timedelta(days=1)).isoformat()
    except ValueError:
        return []
    path = cfg.logs_dir / "report_audit" / f"{prev}.v1.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    text = data.get("report_text") if isinstance(data, dict) else None
    return extract_claims(text) if isinstance(text, str) and text else []


def _audit_claims(cfg: Config, claims: list[Claim], before_date: str) -> tuple[Finding, ...]:
    """规则闸：每条断言至多出一条 Finding，优先级 R2（硬冲突）> R4（首现无先例）> R5（计数断档）> R1（量级无锚）。

    P4 周报审计复用本函数，不复制规则。
    """
    findings: list[Finding] = []
    table = _load_topic_synonyms(cfg)
    prev_running: dict[str, int] = {}
    for c in _yesterday_claims(cfg, before_date):
        if c.claim_word in _RUNNING_WORDS and c.claimed_days is not None:
            topic = _topic_from_table(table, c.text)
            if topic:
                prev_running[topic] = max(prev_running.get(topic, 0), c.claimed_days)
    for claim in claims:
        topic = _topic_from_table(table, claim.text)
        if topic and claim.claimed_days is not None:
            seen = last_seen(cfg, topic, before_date)
            if seen.found and seen.gap_days is not None and is_conflict(claim.claimed_days, seen.gap_days):
                findings.append(
                    Finding(
                        rule="R2", severity="error", section=claim.section, anchor=claim.anchor,
                        claim=claim.text, claimed_days=claim.claimed_days,
                        actual_last_seen=seen.date, actual_gap_days=seen.gap_days,
                        evidence=seen.source + (f"；次近 {'、'.join(seen.alt)}" if seen.alt else ""),
                    )
                )
                continue
            if claim.claim_word in _FIRST_WORDS and not seen.found:
                findings.append(
                    Finding(
                        rule="R4", severity="warn", section=claim.section, anchor=claim.anchor,
                        claim=claim.text, claimed_days=claim.claimed_days,
                        actual_last_seen=None, actual_gap_days=None,
                        evidence=f"主题「{topic}」在记忆库无先例",
                    )
                )
                continue
        if claim.claim_word in _RUNNING_WORDS and claim.claimed_days is not None:
            prev = prev_running.get(topic) if topic else None
            if prev is not None and claim.claimed_days not in (prev + 1, 1):
                findings.append(
                    Finding(
                        rule="R5", severity="warn", section=claim.section, anchor=claim.anchor,
                        claim=claim.text, claimed_days=claim.claimed_days,
                        actual_last_seen=None, actual_gap_days=None,
                        evidence=f"昨日连续 {prev} 天 vs 今日 {claim.claimed_days} 天（主题「{topic}」计数断档）",
                    )
                )
                continue
        if not claim.anchored:
            findings.append(
                Finding(
                    rule="R1", severity="warn", section=claim.section, anchor=claim.anchor,
                    claim=claim.text, claimed_days=claim.claimed_days,
                    actual_last_seen=None, actual_gap_days=None, evidence="",
                )
            )
    return tuple(findings)


def audit_report(cfg: Config, date: str) -> AuditResult:
    """审计一篇已投递日报（YYYY-MM-DD）或周报（YYYY-Www）；文件缺失返回 missing 占位。

    周报：正文在 `logs/delivered_weekly/{week}.md`；last_seen 的基线锚点 = 数据窗末日（周日）。
    """
    path = _report_path(cfg, date)
    if not path.is_file():
        return AuditResult(date=date, version=0, score=100, missing=True, findings=())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return AuditResult(date=date, version=0, score=100, missing=True, findings=())
    claims = extract_claims(text)
    anchor = _anchor_day(date)
    findings = _audit_claims(cfg, claims, before_date=anchor)
    errors = sum(1 for f in findings if f.severity == "error")
    warns = len(findings) - errors
    score = max(0, 100 - 25 * errors - 5 * warns)
    return AuditResult(
        date=date, version=0, score=score, missing=False,
        findings=findings, report_text=text, claims=tuple(claims),
    )


# ------------------------------------------------- Task 4: 版本化落盘与 backfill CLI

logger = get_logger("audit")

# 想问你硬闸（spec §5.3）：P2 只做条数上限与依据锚检查；key 去重随 P3 qa 落库一起
QUESTION_LIMIT: int = 5


def _is_question_heading(line: str) -> bool:
    """`# 想问你` / `## 想问你` 都认（模型常把标题降级，不认就会漏闸）。"""
    stripped = line.strip()
    return stripped.startswith("#") and stripped.lstrip("#").strip() == "想问你"


def cap_questions(reply: str, *, limit: int = QUESTION_LIMIT) -> tuple[str, bool]:
    """对 `# 想问你` 节做确定性硬闸：条数超限截断、缺「依据」锚的条删除、清零则整节省略。

    只动该节，其余正文逐字不变（_diff_gate 纪律）。返回 (处理后的正文, 是否发生改动)。
    日报 limit=5（QUESTION_LIMIT）；周报传 limit=3（spec §5.4）。
    """
    lines = reply.splitlines()
    out: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if not _is_question_heading(line):
            out.append(line)
            i += 1
            continue
        j = i + 1
        bullets: list[str] = []
        while j < len(lines) and not lines[j].lstrip().startswith("#"):
            if lines[j].strip().startswith("- "):
                bullets.append(lines[j].strip())
            j += 1
        kept = [b for b in bullets if "依据" in b][:limit]
        if len(kept) != len(bullets):
            changed = True
        if kept:
            out.append(line)
            out.extend(kept)
        else:
            changed = True
            # 整节省略时顺带吃掉节后紧邻空行，避免遗留双空行
            if j < len(lines) and lines[j].strip() == "":
                j += 1
        i = j
    result = "\n".join(out)
    if reply.endswith("\n"):
        result += "\n"
    return result, changed


WEEKLY_LIMIT: int = 1200  # 周报正文硬上限（spec §5.4：丰富在结构不在字数）
_SECTION_RE: Final = re.compile(r"^#{1,3}\s")
# 核心节：删掉它们等于把「这周发生了什么 / 评分 / 提问 / 画像候选」四条主线打断，长度再紧也要保
_WEEKLY_KEEP_HINTS: Final = ("三条变化", "给这周打分", "想问你", "画像候选")


def _split_sections(reply: str) -> list[tuple[str, list[str]]]:
    """按 `#`/`##`/`###` 标题切节；首个标题前的引言单独成节（head=""）。"""
    sections: list[tuple[str, list[str]]] = []
    for line in reply.splitlines():
        if _SECTION_RE.match(line):
            sections.append((line, [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            sections.append(("", [line]))
    return sections


def _render_sections(sections: list[tuple[str, list[str]]], keep: list[bool]) -> str:
    out: list[str] = []
    for flag, (_, body) in zip(keep, sections):
        if flag:
            out.extend(body)
    return "\n".join(out).strip() + "\n"


def cap_weekly(reply: str, *, limit: int = WEEKLY_LIMIT) -> tuple[str, int]:
    """周报长度硬闸（决策 34）：超 `limit` 时从尾部整节删减，再按最长节压缩正文，最后硬截断。

    保护首节与核心节（`三条变化` / `给这周打分` / `想问你` / `画像候选`）——删节从尾部往前，
    先牺牲纯信息节（数字、一条你可能没注意到的、上周回看、本周未解）。仍超时**只压缩最长节
    的正文并保留其标题**（不整节删核心内容），极端情况才硬截断。返回 (正文, 删掉的节数)；
    保证 `len(正文) <= limit`。
    """
    if len(reply) <= limit:
        return reply, 0
    sections = _split_sections(reply)
    protected = {0} | {
        i
        for i, (head, _) in enumerate(sections)
        if any(hint in head for hint in _WEEKLY_KEEP_HINTS)
    }
    keep = [True] * len(sections)
    dropped = 0
    for i in range(len(sections) - 1, -1, -1):
        if len(_render_sections(sections, keep)) <= limit:
            break
        if i in protected:
            continue
        keep[i] = False
        dropped += 1
    body = _render_sections(sections, keep)
    if len(body) <= limit:
        return body, dropped
    # 仍超：压缩"最长的那一节正文"，每次至少砍 40 字，保留标题（结构不丢）
    for _ in range(200):
        if len(body) <= limit:
            return body, dropped
        candidates = [i for i, (_, lines) in enumerate(sections) if keep[i] and len(lines) > 1]
        if not candidates:
            break
        idx = max(candidates, key=lambda i: len("\n".join(sections[i][1])))
        lines = sections[idx][1]
        text = "\n".join(lines[1:])
        cut = max(len(body) - limit, 40)
        keep_len = max(0, len(text) - cut)
        lines[1:] = ([text[:keep_len].rstrip() + "…"] if keep_len > 0 else [])
        body = _render_sections(sections, keep)
    if len(body) <= limit:
        return body, dropped
    return body[: limit - 2].rstrip() + "…\n", dropped


_VERSION_RE_CACHE: dict[str, re.Pattern[str]] = {}


def write_audit(cfg: Config, result: AuditResult) -> Path:
    """按版本落盘审计结果：同日期已有 vN 则写 v(N+1)，v1 永不覆盖（审计冻结点）。"""
    out_dir = cfg.logs_dir / "report_audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = _VERSION_RE_CACHE.setdefault(
        result.date, re.compile(rf"{re.escape(result.date)}\.v(\d+)\.json$")
    )
    next_version = 1
    for existing in out_dir.iterdir():
        m = pattern.match(existing.name)
        if m:
            next_version = max(next_version, int(m.group(1)) + 1)
    frozen = replace(result, version=next_version)
    path = out_dir / f"{result.date}.v{next_version}.json"
    path.write_text(json.dumps(asdict(frozen), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def backfill(cfg: Config) -> dict[str, int]:
    """遍历全部已投递日报：无 v1 快照的审计并落 v1；已有 v1 的跳过（历史基线只认 v1）。

    汇总口径 = 全部 v1 快照（决策 38）：第二次跑也要给出与 baseline 一致的数字，
    否则 `claims=0/conflicts=0` 会让人误以为基线空了。
    """
    summary = {"reports": 0, "dailies": 0, "claims": 0, "bare": 0, "conflicts": 0}
    reports_dir = cfg.logs_dir / "delivered_report"
    audit_dir = cfg.logs_dir / "report_audit"
    if reports_dir.is_dir():
        for f in sorted(reports_dir.glob("*.md")):
            summary["dailies"] += 1
            if (audit_dir / f"{f.stem}.v1.json").is_file():
                continue
            result = audit_report(cfg, f.stem)
            write_audit(cfg, result)
    if audit_dir.is_dir():
        for f in sorted(audit_dir.glob("*.v1.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            summary["reports"] += 1
            claims = [c for c in data.get("claims", ()) if isinstance(c, dict)]
            findings = [x for x in data.get("findings", ()) if isinstance(x, dict)]
            summary["claims"] += len(claims)
            summary["bare"] += sum(1 for c in claims if not c.get("anchored"))
            summary["conflicts"] += sum(1 for x in findings if x.get("rule") == "R2")
    logger.info("audit backfill finished", **summary)
    return summary


def render_baseline(cfg: Config) -> str:
    """读全部 {date}.v1.json 汇总历史基线：总览表 + 硬冲突清单 + 无锚清单（≤50 条）。"""
    rows: list[dict] = []
    out_dir = cfg.logs_dir / "report_audit"
    if out_dir.is_dir():
        for f in sorted(out_dir.glob("*.v1.json")):
            try:
                rows.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    total_claims = sum(len(r.get("claims", ())) for r in rows)
    bare: list[tuple[str, dict]] = [
        (r["date"], c) for r in rows for c in r.get("claims", ()) if not c.get("anchored")
    ]
    conflicts = sorted(
        ((r["date"], r.get("score", 100), x) for r in rows for x in r.get("findings", ()) if x.get("rule") == "R2"),
        key=lambda t: t[1],
    )
    scores = [r.get("score", 100) for r in rows if not r.get("missing")]
    avg = round(sum(scores) / len(scores)) if scores else 0

    lines = [
        "# 日报断言基线（v1 冻结口径）",
        "",
        f"- 篇数：{len(rows)}",
        f"- 时间量级断言：{total_claims}",
        f"- 无锚断言：{len(bare)}",
        f"- 硬冲突（R2）：{len(conflicts)}",
        f"- v1 均分：{avg}",
        "",
        "## 硬冲突清单（按分数升序）",
        "",
    ]
    for day, score, x in conflicts:
        lines.append(
            f"- **{day}**（score {score}）R2：{x.get('claim', '')}"
            f" —— 声称 {x.get('claimed_days')} 天，最近提及 {x.get('actual_gap_days')} 天"
            f"（{x.get('actual_last_seen')}，{x.get('evidence')}）"
        )
    lines += ["", "## 无锚断言清单（最多 50 条）", ""]
    for day, c in bare[:50]:
        anchor = f"[{c['anchor']}] " if c.get("anchor") else ""
        text = str(c.get("text", ""))
        prefix = f"- [{c['anchor']}] " if c.get("anchor") else ""
        if prefix and text.startswith(prefix):
            text = text[len(prefix):]
        lines.append(f"- {day} {anchor}{text}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="日报断言基线闸：离线审计已投递日报")
    parser.add_argument("--backfill", action="store_true", help="审计全部已投递日报并落 v1 快照")
    args = parser.parse_args(argv)
    if not args.backfill:
        parser.print_help()
        return 2
    load_dotenv()
    cfg = Config.default()
    summary = backfill(cfg)
    baseline_path = cfg.logs_dir / "report_audit" / "baseline.md"
    baseline_path.write_text(render_baseline(cfg), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
