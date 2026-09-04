"""活跃时段分析（规则版，无需 LLM）。"""

from collections import Counter
from datetime import datetime

from profile.models import ChatRecord


def analyze(records: list[ChatRecord]) -> dict:
    if len(records) < 2:
        return {"status": "样本不足", "count": len(records)}

    hours = Counter(r.time.hour for r in records)
    distribution = {h: 0 for h in range(24)}
    distribution.update(dict(hours))

    peak_start, _ = _find_best_window(hours)
    trough_start, _ = _find_worst_window(hours)

    days = len({r.time.date() for r in records})
    daily_avg = len(records) / max(days, 1)

    return {
        "status": "ok",
        "24小时分布": distribution,
        "峰值时段": f"{peak_start:02d}:00-{(peak_start + 2) % 24:02d}:00",
        "低谷时段": f"{trough_start:02d}:00-{(trough_start + 2) % 24:02d}:00",
        "日均活跃次数": round(daily_avg, 1),
        "总记录数": len(records),
        "天数": days,
    }


def _find_best_window(hours: Counter) -> tuple[int, int]:
    best_start, best_count = 0, 0
    for h in range(24):
        total = sum(hours.get((h + i) % 24, 0) for i in range(3))
        if total >= best_count:
            best_start, best_count = h, total
    return best_start, best_count


def _find_worst_window(hours: Counter) -> tuple[int, int]:
    worst_start, worst_count = 0, float("inf")
    for h in range(24):
        total = sum(hours.get((h + i) % 24, 0) for i in range(3))
        if total < worst_count:
            worst_start, worst_count = h, total
    return worst_start, worst_count
