from collections import Counter
from datetime import datetime

from profile.models import ChatRecord


MIN_SAMPLE_SIZE = 2


def analyze(records: list[ChatRecord]) -> dict:
    if len(records) < MIN_SAMPLE_SIZE:
        return {"status": "样本不足", "count": len(records)}

    hours = Counter(r.time.hour for r in records)

    distribution = {h: 0 for h in range(24)}
    distribution.update(dict(hours))

    peak_start, peak_count = _find_best_window(hours)
    trough_start, trough_count = _find_worst_window(hours)

    days = _count_days(records)
    daily_avg = len(records) / max(days, 1)

    sources = Counter(r.source for r in records)

    return {
        "status": "ok",
        "24小时分布": distribution,
        "峰值时段": f"{peak_start:02d}:00-{(peak_start+2)%24:02d}:00",
        "低谷时段": f"{trough_start:02d}:00-{(trough_start+2)%24:02d}:00",
        "日均活跃次数": round(daily_avg, 1),
        "总记录数": len(records),
        "天数": days,
        "source": dict(sources),
    }


def _find_best_window(hours: Counter) -> tuple[int, int]:
    best_start = 0
    best_count = 0
    for h in range(24):
        total = sum(hours.get((h + i) % 24, 0) for i in range(3))
        if total >= best_count:
            best_count = total
            best_start = h
    return best_start, best_count


def _find_worst_window(hours: Counter) -> tuple[int, int]:
    worst_start = 0
    worst_count = float("inf")
    for h in range(24):
        total = sum(hours.get((h + i) % 24, 0) for i in range(3))
        if total < worst_count:
            worst_count = total
            worst_start = h
    return worst_start, worst_count


def _count_days(records: list[ChatRecord]) -> int:
    dates = {r.time.date() for r in records}
    return len(dates)
