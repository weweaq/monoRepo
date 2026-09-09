"""CLI: rerun a scheduled job for a historical day (e.g. regenerate + resend a daily report).

Scenario: yesterday's daily report failed / came out wrong / was never sent because the
service was down — rerun it for that day with the current codebase. The info pack and
trajectory map are rebuilt from stored data for the target day; the email subject carries
the target day with a 补跑 (rerun) marker so the recipient can tell which day it covers.

Usage (cwd = apps/withlanggraph, mono .venv):
    python -m gacore.rerun --day 2026-09-08                  # rerun + send email
    python -m gacore.rerun --day 2026-09-08 --no-email       # rerun, archive only
    python -m gacore.rerun --day 2026-09-08 --job weekly-summary

Notes:
  - The output archive (logs/scheduled/{job}_{ts}.md) and daily-note status bullet land
    under TODAY's timestamp; the report CONTENT (info pack / trajectory / email subject)
    covers the target day.
  - The scheduler's run-loop must not be mid-fire of the same job when you rerun; for the
    23:50 daily-report that means rerun during the day, not right at 23:50.
  - Rerunning overwrites nothing: each run appends a new output archive, the LLM may edit
    the target day's daily note, and the onboard pack is re-exported (harmless).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from gacore.config import Config, load_dotenv
from gacore.scheduler import load_jobs, run_job


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gacore.rerun",
        description="Rerun a scheduled job for a historical day (info pack + trajectory + delivery).",
    )
    parser.add_argument("--day", required=True, help="target day, YYYY-MM-DD, e.g. 2026-09-08")
    parser.add_argument("--job", default="daily-report", help="job name in config/schedule.json (default: daily-report)")
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="skip channel delivery; still write the output archive and daily note",
    )
    args = parser.parse_args()

    try:
        datetime.strptime(args.day, "%Y-%m-%d")
    except ValueError:
        print(f"[rerun] invalid --day {args.day!r}: expected YYYY-MM-DD", file=sys.stderr)
        return 2

    load_dotenv()
    cfg = Config.default()
    jobs = load_jobs(cfg)
    job = next((j for j in jobs if j.name == args.job), None)
    if job is None:
        names = ", ".join(j.name for j in jobs)
        print(f"[rerun] job {args.job!r} not found in schedule.json (available: {names})", file=sys.stderr)
        return 2

    print(
        f"[rerun] {job.name!r} for {args.day} "
        f"(deliver={'no' if args.no_email else 'yes'}, max_turns={job.max_turns})"
    )
    result = run_job(job, cfg, for_day=args.day, deliver=not args.no_email)
    print(f"[rerun] exit_reason={result.exit_reason} duration={result.duration_seconds:.1f}s")
    print(f"[rerun] error={result.error or 'none'}")
    print(f"[rerun] output={result.output_path}")
    return 0 if result.error is None else 1


if __name__ == "__main__":
    sys.exit(main())
