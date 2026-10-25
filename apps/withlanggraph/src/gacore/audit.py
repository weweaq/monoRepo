"""日报断言基线闸（P1）：离线审计已投递日报里的时间量级断言。

设计（docs/superpowers/specs/2026-10-24-daily-report-eval-design.md）：

- 只读：输入是已投递日报与记忆库文件，本模块不写库、不联网、不调 LLM。
- 断言抽取：遮蔽引文/标题/时刻后，量级（N 天/周/月/年）与断言词同子句且距离 ≤12 才算断言。
- 基线查询：显式主题词表扫记忆库三源（daily/*.md + global_mem_insight + global_mem_facts）。
- 结果版本化落盘：v1 = 首次投递快照（含 report_text 冻结，决策 23），历史基线只认 v1。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
