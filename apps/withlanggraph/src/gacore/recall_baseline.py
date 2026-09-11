"""Read-only RAG recall baseline for gacore's off-graph memory recall.

Pure stdlib: never opens the DB, never loads the embedding model, never touches
production graph logic. It replays production ``llm_requests.jsonl`` logs to
measure, over real user turns, how often the ``=== 向量召回记忆 ===`` background
block was actually injected into the system prompt and how many semantic /
episodic facts (with sim scores) each injection carried.

This provides the "current behavior" baseline before deciding whether a query-
rewrite step (``rewrite_for_recall``) belongs in front of ``recall_context``.

Read-only: only log files under ``logs_root`` and the two output paths (JSON
report + human-readable summary) are touched.

Run::

    python -m gacore.recall_baseline --days 7
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Final

_RAG_HEADER: Final = "=== 向量召回记忆"
_SEM_HEADER: Final = "[长期画像·语义]"
_EPI_HEADER: Final = "[某天发生·情景]"
_SIM_RE: Final = re.compile(r"\(sim\s+([0-9.]+)\)")
_IMAGE_MARKER_RE: Final = re.compile(r"\[IMAGE:.+?\]")

_MAX_QUERY_LEN: Final = 200  # mirrors context.py::_rag_recall_block gate


def strip_image_markers(text: str) -> str:
    """Remove [IMAGE:path] markers to get plain user text (mirrors graph.py)."""
    return _IMAGE_MARKER_RE.sub("", text or "").strip()


_USER_ROLES: Final = ("user", "human")  # OpenAI "user" vs LangChain-serialized "human"


def user_query(messages: list | None) -> str:
    """Return the text of the most recent user-role message, or ''."""
    if not messages:
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") not in _USER_ROLES:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return strip_image_markers(content)
        # Multimodal content is a list of {type, text|image_url,...} dicts.
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")]
            return strip_image_markers(" ".join(parts))
    return ""


def system_text(messages: list | None) -> str:
    """Return the concatenated system-role prompt, or ''."""
    if not messages:
        return ""
    return "\n".join(
        m.get("content", "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str)
    )


def _hits_after(text: str, marker: str, stop_marker: str | None) -> int:
    i = text.find(marker)
    if i < 0:
        return 0
    seg = text[i + len(marker):]
    if stop_marker:
        j = seg.find(stop_marker)
        if j >= 0:
            seg = seg[:j]
    return len(_SIM_RE.findall(seg))


@dataclass
class Turn:
    """Per-LLM-invoke observation pulled from one llm_requests.jsonl record."""
    ts: str
    day: str
    session: str
    model: str
    query: str
    has_rag: bool
    n_sem: int = 0
    n_epi: int = 0
    sims: list[float] = field(default_factory=list)
    recalled: list[str] = field(default_factory=list)

    @property
    def n_hits(self) -> int:
        return self.n_sem + self.n_epi


def parse_turn(record: dict) -> Turn:
    """Extract a Turn from one parsed llm_requests.jsonl record."""
    messages = record.get("messages")
    query = user_query(messages)
    sys_prompt = system_text(messages)
    has_rag = _RAG_HEADER in sys_prompt
    n_sem = n_epi = 0
    sims: list[float] = []
    recalled: list[str] = []
    if has_rag:
        idx = sys_prompt.find(_RAG_HEADER)
        block = sys_prompt[idx:]
        block = block.split("\n===")[0]
        n_sem = _hits_after(block, _SEM_HEADER, _EPI_HEADER)
        n_epi = _hits_after(block, _EPI_HEADER, None)
        sims = [float(m) for m in _SIM_RE.findall(block)]
        recalled = [line.strip() for line in block.splitlines() if line.strip().startswith("- ")]
    return Turn(
        ts=str(record.get("ts", "")),
        day=str(record.get("ts", ""))[:10],
        session=str(record.get("session", "")),
        model=str(record.get("model", "")),
        query=query,
        has_rag=has_rag,
        n_sem=n_sem,
        n_epi=n_epi,
        sims=sims,
        recalled=recalled,
    )


def load_records(logs_root: Path, days: int) -> list[Turn]:
    """Parse every llm_requests.jsonl in the last ``days`` day dirs into Turns."""
    today = date.today()
    turns: list[Turn] = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        f = logs_root / day / "llm_requests.jsonl"
        if not f.is_file():
            continue
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or not record.get("messages"):
                    continue
                turns.append(parse_turn(record))
    return turns


@dataclass
class Report:
    window: dict
    totals: dict
    injection: dict
    hits: dict
    per_day: list[dict]
    examples: list[dict]
    recalls: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def _rounded(value: float | None, nd: int = 4) -> float | None:
    return round(value, nd) if value is not None else None


def aggregate(turns: list[Turn]) -> Report:
    """Collapse per-invoke Turns into distinct-query aggregates and metrics."""
    gate_eligible = [t for t in turns if t.query and 1 <= len(t.query) <= _MAX_QUERY_LEN]
    # Distinct user queries (same text across sessions/invokes counts once).
    by_query: dict[str, list[Turn]] = {}
    for t in gate_eligible:
        by_query.setdefault(t.query, []).append(t)

    injected_q = {q for q, rows in by_query.items() if any(r.has_rag for r in rows)}
    empty_q = set(by_query) - injected_q

    all_sims = [s for t in turns if t.sims for s in t.sims]
    sem_hits = sum(t.n_sem for t in turns)
    epi_hits = sum(t.n_epi for t in turns)

    per_day: dict[str, dict] = {}
    for t in turns:
        d = per_day.setdefault(t.day, {"invokes": 0, "distinct": set(), "injected": set(), "sem": 0, "epi": 0})
        d["invokes"] += 1
        if t.query and 1 <= len(t.query) <= _MAX_QUERY_LEN:
            d["distinct"].add(t.query)
        if t.has_rag:
            d["injected"].add(t.query)
            d["sem"] += t.n_sem
            d["epi"] += t.n_epi

    day_rows = []
    for day in sorted(per_day):
        d = per_day[day]
        day_rows.append({
            "day": day,
            "invokes": d["invokes"],
            "distinct_queries": len(d["distinct"]),
            "injected_queries": len(d["injected"]),
            "semantic_hits": d["sem"],
            "episodic_hits": d["epi"],
        })

    # Example queries close the picture for human review (a few from each bucket).
    def sample(queries, limit, key):
        return [
            {"query": q, "n_invokes": len(by_query[q]), "key": key(q)}
            for q in sorted(queries, key=lambda q: len(q), reverse=True)[:limit]
        ]

    examples = sample(injected_q, 5, lambda q: "injected")
    examples += sample(empty_q, 8, lambda q: "empty")

    # 输入 vs 召回明细：每个去重 query 一条，召回内容来自其注入轮次的真实系统提示词。
    ordered_q = sorted(by_query, key=lambda q: min(t.ts for t in by_query[q]))
    recalls = []
    for q in ordered_q:
        rows = by_query[q]
        rep = next((t for t in rows if t.recalled), rows[0])
        recalls.append({
            "day": min(t.day for t in rows),
            "query": q,
            "has_rag": any(t.has_rag for t in rows),
            "sims": [round(s, 3) for s in rep.sims],
            "recalled": rep.recalled,
        })

    n_eligible = len(by_query)
    return Report(
        window={"day_dirs": len(set(t.day for t in turns)), "distinct_days": len(per_day), "log_records": len(turns)},
        totals={
            "llm_invokes": len(turns),
            "gate_eligible_queries": n_eligible,
            "distinct_queries": len(by_query),
            "sessions": len({t.session for t in turns}),
        },
        injection={
            "injected_queries": len(injected_q),
            "empty_queries": len(empty_q),
            "inject_rate": _rounded(len(injected_q) / n_eligible) if n_eligible else None,
            "empty_rate": _rounded(len(empty_q) / n_eligible) if n_eligible else None,
        },
        hits={
            "injected_rows_with_hits": sum(1 for t in turns if t.has_rag and not t.sims),
            "total_semantic_hits": sem_hits,
            "total_episodic_hits": epi_hits,
            "avg_hits_per_injected": _rounded(sem_hits / (sum(1 for t in turns if t.has_rag)) if any(t.has_rag for t in turns) else None),
            "sim_min": _rounded(min(all_sims)) if all_sims else None,
            "sim_avg": _rounded(statistics.mean(all_sims)) if all_sims else None,
            "sim_max": _rounded(max(all_sims)) if all_sims else None,
        },
        per_day=day_rows,
        examples=examples,
        recalls=recalls,
    )


def render_human(report: Report) -> str:
    """Render a compact human-readable summary (Chinese, UTF-8)."""
    inj = report.injection
    line = []
    line.append("RAG 召回基线（只读 · 生产 llm_requests 日志）")
    line.append("=" * 46)
    t = report.totals
    line.append(f"覆盖 {report.window['distinct_days']} 天  {report.window['log_records']} 条 LLM 调用")
    line.append(f"  去重后用户轮次(query) {t['distinct_queries']} 个（gate 通过 {t['gate_eligible_queries']}），来自 {t['sessions']} 个会话")
    line.append("")
    line.append("注入情况（处理过的用户 query 里，系统提示词真正带上了向量召回头）")
    line.append(f"  注入 {inj['injected_queries']} 个   空 {inj['empty_queries']} 个")
    line.append(f"  注入率 {round((inj['inject_rate'] or 0) * 100, 1)}%   空返回率 {round((inj['empty_rate'] or 0) * 100, 1)}%")
    line.append("")
    h = report.hits
    line.append("命中质量（注入的轮次里实际带回多少事实条目 / 相似度）")
    line.append(f"  语义画像命中 {h['total_semantic_hits']} 条   情景事件命中 {h['total_episodic_hits']} 条")
    line.append(f"  平均每注入 {h['avg_hits_per_injected']} 条   sim 范围 [{h['sim_min']}, {h['sim_max']}]"
                + (f"（均值 {h['sim_avg']}）" if h["sim_avg"] is not None else ""))
    line.append("")
    line.append("按天（distinct 注入 / 命中明细）")
    line.append("  " + " · ".join(
        f"{r['day']}: 注入{r['injected_queries']}/{r['distinct_queries']} (语义{r['semantic_hits']}/情景{r['episodic_hits']})"
        for r in report.per_day
    ))
    line.append("")
    line.append("输入 vs 召回明细（按首次出现排序）")
    for r in report.recalls:
        tag = "注入" if r["has_rag"] else "空  "
        line.append(f"  ─ [{tag}] {r['day']} 问题: {r['query'][:80]}")
        if r["recalled"]:
            for item in r["recalled"]:
                line.append(f"        ↳ {item[:100]}")
        else:
            line.append("        （未注入 / 无命中）")
    line.append("")
    line.append("示例 query（按长度截断示意，便于快速扫一眼）")
    for e in report.examples:
        tag = "注入" if e["key"] == "injected" else "空  "
        line.append(f"  [{tag}] {e['query'][:60]}")
    return "\n".join(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only RAG recall baseline from llm_requests logs.")
    parser.add_argument("--days", type=int, default=7, help="days of log history to replay (default 7)")
    parser.add_argument("--logs-root", type=Path, default=Path("logs"), help="gacore logs root (default ./logs)")
    parser.add_argument("--out", type=Path, default=Path("data/recall_baseline_report.json"), help="JSON report output")
    parser.add_argument("--human-out", type=Path, default=Path("data/recall_baseline_summary.txt"), help="human-readable summary output")
    args = parser.parse_args(argv)

    turns = load_records(args.logs_root, args.days)
    if not turns:
        print(f"no llm_requests records found under {args.logs_root} within {args.days} days")
        return 1

    report = aggregate(turns)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.to_json(), encoding="utf-8")
    human = render_human(report)
    args.human_out.write_text(human, encoding="utf-8")
    print(human)
    print(f"\nJSON 报告已写入: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())