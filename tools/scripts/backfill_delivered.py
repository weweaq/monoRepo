"""One-shot backfill: build logs/delivered_report/{date}.md source-of-truth files
from the historical daily-report archives under logs/scheduled/.

Why:
    The daily-report delivery path only started calling save_delivered() (the
    feedback source-of-truth) on 2026-09-11. Every earlier report exists only as
    a full "run archive" in logs/scheduled/, so the feedback loop ("改日报 → 确认
    → 确认重发") has no truth file to read for those days. This script fills the
    gap so historical days are directly editable/redeliverable.

What it does per day (覆盖式，只留最新):
    1. Collect all daily-report_YYYYMMDD_*.md archives for that day.
    2. Drop "failed / empty" runs (error header != none, or Reply is "(empty reply)"
       or blank). These are real data-noise (e.g. config errors) and must never become
       the day's canonical report.
    3. Among the surviving valid runs, keep only the LATEST one (by mtime) — the
       report body is used verbatim, no fusion. Multiple runs of the same day are
       almost always 补跑/多次生成, and the latest is the most complete final version.
    4. save_delivered() anchor-stamps and overwrites logs/delivered_report/{date}.md.

Re-runnable: re-running regenerates and overwrites existing per-day files (历史正片是
可再生成的产物，重跑以最新归档对齐，符合"只存最新"的预期).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Final

_THIS = Path(__file__).resolve()
_APP_ROOT = _THIS.parents[2] / "apps" / "withlanggraph"
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT / "src"))

from gacore import feedback  # noqa: E402
from gacore.config import Config, load_dotenv  # noqa: E402

_SCHEDULED_SUBDIR: Final = "scheduled"
_REPLY_HEAD: Final = re.compile(r"^## Reply\s*$")
# The delivered reply runs from "## Reply" up to the raw-output section header
# "## Reply (raw model output, …)". Matching any "## " would wrongly cut at the
# report's own "## 1." body sections, so we stop only at that literal raw header.
_RAW_REPLY_HEAD: Final = re.compile(r"^## Reply \(raw")
_ERROR_HDR: Final = re.compile(r"^-\s*error:\s*(.*)$")


# --------------------------------------------------------------------------- #
# extraction helpers
# --------------------------------------------------------------------------- #
def extract_reply_text(path: Path) -> str | None:
    """Return the ## Reply section (until the next ##) of an archive, or None.

    A run whose reply section is "(empty reply)" or effectively blank is treated
    as a failure and returns None (caller drops it from the latest pick).
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    start = None
    for i, ln in enumerate(lines):
        if _REPLY_HEAD.match(ln.strip()):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if _RAW_REPLY_HEAD.match(lines[i].strip()):
            end = i
            break
    body = "\n".join(lines[start:end]).strip()
    if not body or body == "(empty reply)":
        return None
    return body


def is_failed_run(path: Path) -> bool:
    """True when the archive's header records a non-none error (real failure)."""
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _ERROR_HDR.match(ln)
            if m:
                return (m.group(1).strip() or "").lower() != "none"
    except OSError:
        return True
    return False


def _sort_key(path: Path):
    """Order by mtime; the latest (most complete) run sorts last."""
    return path.stat().st_mtime


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_delivered",
        description="Backfill logs/delivered_report/{date}.md from historical archives "
                    "(覆盖式：每天只留最新一份有效正文).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="scan and report what would be written without writing.")
    args = parser.parse_args()

    load_dotenv()
    cfg = Config.default()
    sched_dir = cfg.logs_dir / _SCHEDULED_SUBDIR
    if not sched_dir.is_dir():
        print(f"[backfill] scheduled dir not found: {sched_dir}", file=sys.stderr)
        return 2

    # Late import to reuse the same DSML-stripping used by the delivery path, so the
    # backfilled canonical body matches what was actually delivered (no <summary>).
    from gacore.scheduler import _sanitize_reply

    # Pass 1 — collect every archive with its filename-day tag and cleaned body.
    # Each run is assigned a "target day": the filename tag, overridden to a day the
    # body itself names via a 补跑/rerun marker (e.g. "信息包·2026-09-08") when present.
    # This prevents a cross-day rerun filed by filename from merging into the wrong day.
    _RERUN_DAY_RE: Final = re.compile(r"补跑[^\d]*(\d{4}-\d{2}-\d{2})|信息包[:：·\s]*(\d{4}-\d{2}-\d{2})")
    tagged: dict[str, list[tuple[str, Path]]] = {}
    for p in sorted(sched_dir.glob("daily-report_*.md"), key=_sort_key):
        m = re.match(r"^daily-report_(\d{8})_\d{6}\.md$|^daily-report_(\d{8})_resend_body\.md$",
                     p.name)
        if not m:
            continue
        tag = m.group(1) or m.group(2)
        body = extract_reply_text(p)
        # Cross-day signature inside the body (rerun of a different day).
        day = f"{tag[:4]}-{tag[4:6]}-{tag[6:8]}"
        if body:
            marker = _RERUN_DAY_RE.search(body)
            if marker:
                rerun_day = marker.group(1) or marker.group(2)
                if rerun_day != day:
                    # Re-file under the day this rerun actually covers.
                    tag = rerun_day.replace("-", "")
                    day = rerun_day
        tagged.setdefault(tag, []).append((day, p))

    written = skipped = failed = 0
    for tag in sorted(tagged):
        date = f"{tag[:4]}-{tag[4:6]}-{tag[6:8]}"
        report = feedback._report_path(cfg, date)

        candidates = [(d, p) for d, p in tagged[tag] if not is_failed_run(p)]
        latest_draft: str | None = None
        for _d, p in candidates:
            body = extract_reply_text(p)
            if body:
                cleaned = _sanitize_reply(body)
                # Drop runs that are only the "(empty reply)" placeholder (post-clean
                # text is just that marker, sometimes + an AI-generated footnote).
                placeholder = cleaned.replace("*（内容由AI生成，仅供参考）*", "").strip()
                if cleaned and placeholder.lower() not in ("", "(empty reply)"):
                    # Iteration is mtime-ascending, so keep overwriting to the latest.
                    latest_draft = cleaned

        if latest_draft is None:
            print(f"  {date}: No valid reply bodies ({len(tagged[tag])} runs) — SKIP")
            if report.is_file():
                skipped += 1
            else:
                failed += 1
            continue

        if args.dry_run:
            print(f"  {date}: [latest] -> {report.relative_to(cfg.logs_dir)}"
                  f" ({len(latest_draft)} chars)")
            written += 1
            continue

        feedback.save_delivered(cfg, date, latest_draft)
        print(f"  {date}: [latest] wrote {report.relative_to(cfg.logs_dir)}"
              f" ({len(latest_draft)} chars)")
        written += 1

    action = "would write" if args.dry_run else "wrote"
    print(f"\n[backfill] {action} {written} file(s), skipped {skipped} existing, "
          f"{failed} days empty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
