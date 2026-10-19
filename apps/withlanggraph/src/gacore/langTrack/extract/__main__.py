"""KG 抽取 CLI：``python -m gacore.langTrack.extract --day 2026-10-16``。

输出保持纯 ASCII（R10：Windows 脚本/终端输出避免中文），异常原文经 stdout 的
utf-8 replace 兜底输出，不因编码崩掉。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gacore.config import load_dotenv
from gacore.langTrack.extract.runner import export_day, run_extract


def _parse_only(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _channel_line(key: str, stat: dict) -> str:
    """One ASCII line per channel carrying the ops telemetry (duration / tokens / finish)."""
    parts = [f"channel={key}", f"status={stat.get('status')}", f"rows={stat.get('rows', 0)}"]
    for field_name in ("prompt_chars", "response_chars", "elapsed_ms", "attempts"):
        if stat.get(field_name) is not None:
            parts.append(f"{field_name}={stat[field_name]}")
    usage = stat.get("usage") or {}
    if usage.get("output_tokens") is not None:
        parts.append(f"out_tokens={usage['output_tokens']}")
    if stat.get("finish_reason"):
        parts.append(f"finish={stat['finish_reason']}")
    if stat.get("error"):
        parts.append(f"error={stat['error']}")
    return "  " + " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    load_dotenv()  # R9：密钥读取顺序 环境变量 → .env；CLI 边界处补齐（run_extract 作为库函数不产生副作用）

    parser = argparse.ArgumentParser(description="langTrack KG extract")
    parser.add_argument("--day", required=True, help="target day, YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="re-run even if an ok run exists")
    parser.add_argument("--only", default=None, help="comma separated channel keys")
    parser.add_argument("--dry-run", action="store_true", help="plan only: no LLM call, no write")
    parser.add_argument("--dump", action="store_true", help="export this day's kg_* rows as JSONL")
    parser.add_argument("--db", default=None, help="sqlite path (default: data/langTrack.db)")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else None

    if args.dump:
        for record in export_day(args.day, db_path=db_path):
            print(json.dumps(record, ensure_ascii=False, default=str))
        return 0

    result = run_extract(
        args.day,
        force=args.force,
        only=_parse_only(args.only),
        dry_run=args.dry_run,
        db_path=db_path,
    )
    print(f"day={result.day} status={result.status} run_id={result.run_id or '-'}")
    print(f"material_chars={result.material_chars} raw_hash={result.raw_hash[:12]} rows={result.rows}")
    material = result.channels.get("_material") or {}
    if material:
        print(f"  material sources={material.get('sources')} by_source_chars={material.get('by_source_chars')}")
    for key, stat in result.channels.items():
        if key.startswith("_"):
            continue
        print(_channel_line(key, stat))
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
