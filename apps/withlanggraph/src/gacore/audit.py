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
from datetime import date, datetime
from pathlib import Path

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


def find_topic(text: str) -> str | None:
    """返回文本命中的第一个主题名（按词表声明顺序）；无命中返回 None。"""
    for topic, synonyms in TOPIC_SYNONYMS.items():
        if any(syn in text for syn in synonyms):
            return topic
    return None


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


def last_seen(cfg: Config, topic: str, before_date: str) -> LastSeen:
    """在记忆库三源里查主题最近一次出现日期（严格早于 before_date）；查不到 found=False。"""
    synonyms = TOPIC_SYNONYMS.get(topic, (topic,))
    hits = _scan_daily(cfg, synonyms, before_date) + _scan_global(cfg, synonyms, before_date)
    if not hits:
        return LastSeen(found=False, date=None, gap_days=None, source="", snippet="")
    day_str, source, snippet = max(hits, key=lambda h: h[0])
    gap = (datetime.strptime(before_date, "%Y-%m-%d").date() - date.fromisoformat(day_str)).days
    return LastSeen(found=True, date=day_str, gap_days=gap, source=source, snippet=snippet)


# ------------------------------------------------------- Task 3: 冲突判定与单篇审计

# R2 容差带（决策 14）：claimed ≥ 2×actual 且 claimed − actual ≥ 14 天才判硬冲突，约数不冤枉
_FIRST_WORDS: tuple[str, ...] = ("首次", "第一次", "久违")


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


def _audit_claims(cfg: Config, claims: list[Claim], before_date: str) -> tuple[Finding, ...]:
    """规则闸：每条断言至多出一条 Finding，优先级 R2（硬冲突）> R4（首现无先例）> R1（量级无锚）。

    P4 周报审计复用本函数，不复制规则。
    """
    findings: list[Finding] = []
    for claim in claims:
        topic = find_topic(claim.text)
        if topic and claim.claimed_days is not None:
            seen = last_seen(cfg, topic, before_date)
            if seen.found and seen.gap_days is not None and is_conflict(claim.claimed_days, seen.gap_days):
                findings.append(
                    Finding(
                        rule="R2", severity="error", section=claim.section, anchor=claim.anchor,
                        claim=claim.text, claimed_days=claim.claimed_days,
                        actual_last_seen=seen.date, actual_gap_days=seen.gap_days, evidence=seen.source,
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
    """审计一篇已投递日报（date = YYYY-MM-DD）；文件缺失返回 missing 占位，不抛异常。"""
    path = cfg.logs_dir / "delivered_report" / f"{date}.md"
    if not path.is_file():
        return AuditResult(date=date, version=0, score=100, missing=True, findings=())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return AuditResult(date=date, version=0, score=100, missing=True, findings=())
    claims = extract_claims(text)
    findings = _audit_claims(cfg, claims, before_date=date)
    errors = sum(1 for f in findings if f.severity == "error")
    warns = len(findings) - errors
    score = max(0, 100 - 25 * errors - 5 * warns)
    return AuditResult(
        date=date, version=0, score=score, missing=False,
        findings=findings, report_text=text, claims=tuple(claims),
    )


# ------------------------------------------------- Task 4: 版本化落盘与 backfill CLI

logger = get_logger("audit")

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
    """遍历全部已投递日报：无 v1 快照的审计并落 v1；已有 v1 的跳过（历史基线只认 v1）。"""
    summary = {"reports": 0, "claims": 0, "bare": 0, "conflicts": 0}
    reports_dir = cfg.logs_dir / "delivered_report"
    if not reports_dir.is_dir():
        return summary
    for f in sorted(reports_dir.glob("*.md")):
        summary["reports"] += 1
        if (cfg.logs_dir / "report_audit" / f"{f.stem}.v1.json").is_file():
            continue
        result = audit_report(cfg, f.stem)
        write_audit(cfg, result)
        summary["claims"] += len(result.claims)
        summary["bare"] += sum(1 for c in result.claims if not c.anchored)
        summary["conflicts"] += sum(1 for x in result.findings if x.rule == "R2")
    logger.info("audit backfill finished", **summary)
    return summary


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
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
