"""CLI: rerun a scheduled job for a historical day or week (e.g. regenerate + resend a report).

Scenario: a daily report failed / came out wrong / was never sent — rerun it for that day
with the current codebase. The info pack and trajectory map are rebuilt from stored data
for the target day; the email subject carries the target day with a 补跑 (rerun) marker.

Weekly mode (P4): rerun the weekly report for a past week — the numbers block, candidate
list, prev-week look-back, archive (`logs/delivered_weekly/{week}.md`), audit and the QQ
rating notice all anchor to the given week, so you can replay last week end-to-end
(答题、补充、打分、候选确认全部走正常回路).

Usage (cwd = apps/withlanggraph, mono .venv):
    python -m gacore.rerun --day 2026-09-08                  # 日报重跑 + 发邮件
    python -m gacore.rerun --day 2026-09-08 --no-email       # 重跑只存档
    python -m gacore.rerun --week 2026-W42                   # 周报按历史周重跑（默认 weekly-report）
    python -m gacore.rerun --week 2026-W42 --no-email
    python -m gacore.rerun --day 2026-09-08 --job weekly-report   # 自定义 job

Notes:
  - The output archive (logs/scheduled/{job}_{ts}.md) and daily-note status bullet land
    under TODAY's timestamp; the report CONTENT (info pack / trajectory / email subject)
    covers the target day/week.
  - The scheduler's run-loop must not be mid-fire of the same job when you rerun; for the
    23:50 daily-report that means rerun during the day, not right at 23:50.
  - Rerunning overwrites nothing: each run appends a new output archive, the LLM may edit
    the target day's daily note, and the onboard pack is re-exported (harmless). 日报正文
    会就地重写 delivered_report/{day}.md，审计按版本落 v2+（v1 基线不动）；周报同理写
    delivered_weekly/{week}.md。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime

from gacore.config import Config, load_dotenv
from gacore.scheduler import load_jobs, run_job

_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gacore.rerun",
        description="Rerun a scheduled job for a historical day (--day) or week (--week).",
    )
    parser.add_argument("--day", default=None, help="target day, YYYY-MM-DD, e.g. 2026-09-08")
    parser.add_argument("--week", default=None, help="target week, YYYY-Www, e.g. 2026-W42")
    parser.add_argument(
        "--job",
        default=None,
        help="job name in config/schedule.json (default: daily-report for --day, weekly-report for --week)",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="skip channel delivery; still write the output archive and daily note",
    )
    args = parser.parse_args(argv)

    if bool(args.day) == bool(args.week):
        print("[rerun] need exactly one of --day YYYY-MM-DD / --week YYYY-Www", file=sys.stderr)
        return 2
    if args.day:
        try:
            datetime.strptime(args.day, "%Y-%m-%d")
        except ValueError:
            print(f"[rerun] invalid --day {args.day!r}: expected YYYY-MM-DD", file=sys.stderr)
            return 2
    if args.week:
        if not _WEEK_RE.match(args.week):
            print(f"[rerun] invalid --week {args.week!r}: expected YYYY-Www, e.g. 2026-W42", file=sys.stderr)
            return 2
        try:
            from gacore.weekly_stats import week_window

            week_window(args.week)
        except ValueError:
            print(f"[rerun] invalid --week {args.week!r}: no such ISO week", file=sys.stderr)
            return 2

    job_name = args.job or ("weekly-report" if args.week else "daily-report")
    target = args.week or args.day

    load_dotenv()
    cfg = Config.default()
    jobs = load_jobs(cfg)
    job = next((j for j in jobs if j.name == job_name), None)
    if job is None:
        names = ", ".join(j.name for j in jobs)
        print(f"[rerun] job {job_name!r} not found in schedule.json (available: {names})", file=sys.stderr)
        return 2

    print(
        f"[rerun] {job.name!r} for {target} "
        f"(deliver={'no' if args.no_email else 'yes'}, max_turns={job.max_turns})"
    )
    result = run_job(job, cfg, for_day=args.day, for_week=args.week, deliver=not args.no_email)
    print(f"[rerun] exit_reason={result.exit_reason} duration={result.duration_seconds:.1f}s")
    print(f"[rerun] error={result.error or 'none'}")
    print(f"[rerun] output={result.output_path}")
    return 0 if result.error is None else 1


if __name__ == "__main__":
    sys.exit(main())
