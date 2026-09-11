"""Offline A/B for query rewriting in front of vector recall.

Replays a set of real user queries (from the RAG recall baseline) against the LIVE
pgvector store, once with the raw utterance and once with a deepseek-rewritten query,
then records BOTH outcomes as structured ``recall.jsonl`` logs (variant raw/rewritten)
and prints a before/after comparison.

Read path only against memory: it queries (never writes) the vector tables, rewrites
via the user's own deepseek key, and appends structured logs under ``--log-base``.

Run (after provisioning the ``vector`` extra / sentence-transformers)::

    python -m gacore.recall_ab --max 8 --subset all
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from gacore import vector_store as vs
from gacore.recall_baseline import aggregate, load_records
from gacore.recall_log import RecallLog, build_record

KEYS = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL")

REWRITE_SYS = (
    "你是向量检索的查询改写助手。用户给的是聊天的口语原话；请改写成 1 条适合向量检索的"
    "明确查询：消解指代（它/那个/他→具体对象）、补全业务背景、聚焦到事件或事实本身。"
    "只输出改写后的一句话，不要任何解释、引号或多余内容。"
)


def load_env(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE .env file (values only, no shell expansion)."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, _, v = raw.partition("=")
        out[k.strip()] = v.strip()
    return out


def rewrite_query(env: dict[str, str], query: str) -> tuple[str | None, str | None]:
    """Rewrite ``query`` via deepseek chat. Returns (rewritten, reason) on failure."""
    key = env.get("DEEPSEEK_API_KEY")
    url = (env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    model = env.get("DEEPSEEK_MODEL") or "deepseek-chat"
    if not key:
        return None, "missing_api_key"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": REWRITE_SYS},
            {"role": "user", "content": f"原话：{query}"},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = (body["choices"][0]["message"]["content"] or "").strip()
        return (text or None), (None if text else "empty_response")
    except Exception as exc:  # noqa: BLE001
        return None, f"llm_error:{type(exc).__name__}"


def recall_both(query: str, day_from: str, day_to: str, k: int, threshold: float) -> tuple[list, list]:
    return (
        vs.nearby(query, k=k, threshold=threshold),
        vs.nearby_episodic(query, k=k, threshold=threshold, day_from=day_from, day_to=day_to),
    )


def collect_queries(logs_root: Path, days: int, subset: str, max_q: int) -> list[str]:
    turns = load_records(logs_root, days)
    report = aggregate(turns)
    recalls = report.recalls  # distinct, first-seen order
    if subset == "empty":
        recalls = [r for r in recalls if not r["has_rag"]]
    elif subset == "injected":
        recalls = [r for r in recalls if r["has_rag"]]
    return [r["query"] for r in recalls][:max_q]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query-rewrite A/B against live pgvector recall.")
    parser.add_argument("--log-base", type=Path, default=Path("logs"), help="daily recall log root")
    parser.add_argument("--logs-root", type=Path, default=Path("logs"), help="baseline source logs")
    parser.add_argument("--baseline-days", type=int, default=7)
    parser.add_argument("--subset", choices=["all", "empty", "injected"], default="all")
    parser.add_argument("--max", type=int, default=8)
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--recall-window-days", type=int, default=90)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--no-rewrite", action="store_true", help="only recall the raw query")
    args = parser.parse_args(argv)

    env = load_env(args.env)
    queries = collect_queries(args.logs_root, args.baseline_days, args.subset, args.max)
    if not queries:
        print("no baseline queries collected")
        return 1

    today = date.today()
    day_to = today.isoformat()
    day_from = (today - timedelta(days=args.recall_window_days)).isoformat()
    log = RecallLog(args.log_base)

    print(f"A/B on {len(queries)} queries (k={args.k}, threshold={args.threshold}, window {day_from}..{day_to})")
    print("=" * 64)

    for i, q in enumerate(queries, 1):
        sem_r, epi_r = recall_both(q, day_from, day_to, args.k, args.threshold)
        log.append(build_record(q, semantic=sem_r, episodic=epi_r, variant="raw",
                                model=env.get("DEEPSEEK_MODEL", ""), k=args.k, threshold=args.threshold,
                                day_from=day_from, day_to=day_to))

        rew, reason = (None, None)
        sem_w, epi_w = sem_r, epi_r
        if not args.no_rewrite:
            rew, reason = rewrite_query(env, q)
            rewrite_meta: dict | None = None
            if rew:
                rewrite_meta = {"query": rew, "skipped_reason": None}
                sem_w, epi_w = recall_both(rew, day_from, day_to, args.k, args.threshold)
            else:
                rewrite_meta = {"query": None, "skipped_reason": reason}
                sem_w, epi_w = [], []
            log.append(build_record(q, query_used=rew or q, semantic=sem_w, episodic=epi_w,
                                    rewrite=rewrite_meta, variant="rewritten",
                                    model=env.get("DEEPSEEK_MODEL", ""), k=args.k,
                                    threshold=args.threshold, day_from=day_from, day_to=day_to))

        top_r = (sem_r + epi_r)[: args.k]
        top_w = (sem_w + epi_w)[: args.k]
        print(f"\n#{i} 输入: {q}")
        if rew:
            print(f"  改写 → {rew}")
        elif not args.no_rewrite:
            print(f"  改写失败({reason})")
        print(f"  原始召回 top:   {_fmt(top_r)}")
        print(f"  改写后召回 top: {_fmt(top_w)}")

    print(f"\n结构化日志已写入 {args.log_base.resolve()}/*/recall.jsonl")
    return 0


def _fmt(rows: list[dict]) -> str:
    if not rows:
        return "(空)"
    return " | ".join(f"<{1 - r['dist']:.3f}> {r['content'][:34]}" for r in rows)


if __name__ == "__main__":
    raise SystemExit(main())