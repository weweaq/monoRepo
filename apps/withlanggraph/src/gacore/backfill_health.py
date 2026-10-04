"""信息包体检数据补录 CLI：对历史日期重放 build_info_pack_report 并落盘体检产物。

用途：体检 jsonl/pack_detail 是 2026-10-04（v3 S1）才开始记录的，此前跑过的日报
没有体检产物——本工具按日期区间重放取数（git/文件/QQ 摘录/NCM/B站/Edge 均支持
按天查询），把历史窗口补进 data/logs/info_pack_health.jsonl 与
data/logs/pack_detail/{date}/，使 /health 总览矩阵不缺列。

诚实边界（backfill 行的语义）：
- 补录的是「以当前数据回看该日」的取数结果——_LONG_TERM/_MEMORY 等画像类源是
  当前态而非当日态；B站/Edge 受各自历史窗口限制，查不到的日期如实记 missing_data。
- trigger="backfill" 用于与真实运行（scheduled/rerun）区分。
- 当天（今日）不补——例行日报尚未跑完，等 scheduled 落盘。

用法（cwd = apps/withlanggraph）：
    uv run python -m gacore.backfill_health --from 2026-08-05 --to 2026-10-02
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from gacore.config import Config, load_dotenv
from gacore.jsonl_logger import get_logger


def refresh_day(cfg: Config, date: str, job: str = "daily-report", trigger: str = "backfill") -> str:
    """单日体检重放并落盘（jsonl 一行 + pack_detail 三节 + 事实卡支线）。

    返回 "ok" / "skip:原因" / "fail:原因"。与 backfill_range 共用；
    允许当日（页面刷新按钮的语义即"以当前数据回看"），仅拒绝未来日期——CLI 的当日跳过
    策略留在 backfill_range。诚实边界同模块 docstring：画像类源是当前态，B站/Edge 受历史窗口限制。
    """
    from gacore.daily_info_pack import (
        build_info_pack_report,
        write_fact_card_detail,
        write_pack_detail,
        write_pack_health,
    )

    today = datetime.now().astimezone().date().isoformat()
    if date > today:
        return "skip:未来日期"
    pack, stats = build_info_pack_report(date, cfg)
    if not stats:
        return "fail:无 stats（全部源异常？）"
    write_pack_health(cfg, date, job, trigger, stats, len(pack), correction_chars=0)
    for st in stats:
        write_pack_detail(
            cfg, date, st.get("key", ""), st.get("title", ""), st.get("status", ""),
            st.get("detail_body", ""), st.get("pack_body", ""), st.get("packed_body", ""),
        )
    try:
        from gacore.langTrack import fact_card

        write_fact_card_detail(cfg, fact_card.build(day=date, detail="compact", outlet="debug"))
    except Exception as exc:  # noqa: BLE001 — 事实卡支线失败不影响主补录
        get_logger("backfill_health").warning("fact-card detail skipped", date=date, error=str(exc))
    return "ok"


def backfill_range(cfg: Config, start: str, end: str, job: str = "daily-report") -> dict:
    """重放 [start, end] 闭区间（含端点）的体检落盘。返回 {"ok": [...], "skip": [...], "fail": [...]}。"""
    out: dict[str, list] = {"ok": [], "skip": [], "fail": []}
    today = datetime.now().astimezone().date().isoformat()
    d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= end_d:
        date = d.isoformat()
        try:
            if date > today:
                out["skip"].append(f"{date}:未来日期")
            elif date == today:
                out["skip"].append(f"{date}:当日等 scheduled 落盘")
            else:
                res = refresh_day(cfg, date, job=job)
                if res == "ok":
                    out["ok"].append(date)
                    print(f"[backfill] {date} ok", flush=True)
                else:
                    kind, _, msg = res.partition(":")
                    out[kind].append(f"{date}:{msg}")
                    print(f"[backfill] {date} {kind.upper()} {msg}", flush=True)
        except Exception as exc:  # noqa: BLE001 — 单日失败不断链
            out["fail"].append(f"{date}:{type(exc).__name__}:{exc}"[:160])
            print(f"[backfill] {date} FAIL {type(exc).__name__}", flush=True)
        d += timedelta(days=1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gacore.backfill_health",
        description="按日期区间重放信息包取数，补录体检 jsonl 与 pack_detail（历史窗口诚实降级）。",
    )
    parser.add_argument("--from", dest="start", required=True, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="结束日期 YYYY-MM-DD（含端点）")
    parser.add_argument("--job", default="daily-report", help="job 名（落 jsonl 的 job 字段）")
    args = parser.parse_args()
    for v in (args.start, args.end):
        datetime.strptime(v, "%Y-%m-%d")  # 格式校验，失败直接抛
    load_dotenv()
    cfg = Config.default()
    result = backfill_range(cfg, args.start, args.end, job=args.job)
    print(
        f"[backfill] 完成：ok={len(result['ok'])} skip={len(result['skip'])} fail={len(result['fail'])}"
    )
    for f in result["fail"]:
        print(f"  FAIL {f}")
    return 0 if not result["fail"] else 1


if __name__ == "__main__":
    sys.exit(main())
