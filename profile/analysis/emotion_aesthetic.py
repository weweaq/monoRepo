"""情绪审美画像（内容消费画像维度之一）。

数据来源：网易云听歌历史（raw_data.source='netease'）。
判断逻辑（设计文档 §4.5 降级版）：
   - 重复播放模式：playCount 识别单曲循环 / 高复听
   - 时长分布：song.dt（间接代理，非真实收听行为）
   - 歌手偏好：artists 频率
降级维度：音乐风格分布、播放时段（无字段）。
情绪倾向为间接推断（基于复听强度 + 时长），非歌词情感分析。
"""

from collections import Counter

from profile.db.store import query_raw_data
from profile.log import get_logger
from profile.models import ChatRecord

logger = get_logger("analysis.emotion_aesthetic")

_DT_BUCKETS = [
    ("短 (<3min)", 0, 180_000),
    ("中 (3-5min)", 180_000, 300_000),
    ("长 (>5min)", 300_000, float("inf")),
]


def from_db() -> dict:
    rows = query_raw_data(source="netease")
    logger.info("情绪审美分析（netease）", extra={"extra": {"raw_count": len(rows)}})
    if not rows:
        return {"status": "无 netease 数据", "count": 0}

    songs = []
    for r in rows:
        rj = r.get("raw_json") or {}
        song = rj.get("song") or {}
        pc = rj.get("playCount")
        dt = song.get("dt")
        if pc is None or dt is None:
            continue
        songs.append({
            "name": song.get("name", ""),
            "artists": song.get("artists") or [],
            "album": song.get("album"),
            "playCount": pc,
            "dt": dt,
        })

    if not songs:
        return {"status": "无有效听歌记录", "count": 0}

    total = len(songs)
    playcounts = [s["playCount"] for s in songs]
    dts = [s["dt"] for s in songs]
    artist_counter = Counter(a for s in songs for a in s["artists"])

    avg_pc = sum(playcounts) / total
    replay_heavy = sum(1 for pc in playcounts if pc >= 10)
    top_replay = sorted(songs, key=lambda s: s["playCount"], reverse=True)[:10]
    top_replay_list = [
        {"name": s["name"], "artists": s["artists"], "playCount": s["playCount"]}
        for s in top_replay
    ]

    dt_buckets = Counter()
    for dt in dts:
        for label, lo, hi in _DT_BUCKETS:
            if lo <= dt < hi:
                dt_buckets[label] += 1
                break
    dt_dist = {label: round(dt_buckets[label] / total * 100, 1) for label, _, _ in _DT_BUCKETS}
    avg_dt_min = round(sum(dts) / total / 1000 / 60, 1)

    tendency = _infer_tendency(avg_pc, replay_heavy / total, avg_dt_min)

    return {
        "status": "ok",
        "样本量": total,
        "复听强度": {
            "平均播放次数": round(avg_pc, 1),
            "高复听占比(>=10次)": round(replay_heavy / total * 100, 1),
        },
        "高复听Top10": top_replay_list,
        "时长分布": dt_dist,
        "平均时长(min)": avg_dt_min,
        "偏好歌手Top10": artist_counter.most_common(10),
        "情绪倾向(间接)": tendency,
    }


def _infer_tendency(avg_pc: float, heavy_ratio: float, avg_dt_min: float) -> dict:
    notes = []
    if avg_pc >= 5 or heavy_ratio >= 0.2:
        notes.append("高复听：对特定曲目有强情感依恋/怀旧倾向，偏好反复沉浸而非广撒网")
    else:
        notes.append("复听分散：听歌面广，探索型消费，单曲情感锚定较弱")
    if avg_dt_min >= 4:
        notes.append("偏好中长曲：更能接受完整叙事/器乐铺陈，审美偏沉浸")
    else:
        notes.append("偏好短曲：碎片化收听，审美偏轻量快消费")
    return {
        "结论": notes,
        "说明": "基于 playCount + dt 的间接代理，未做歌词情感分析（风格/时段维度降级）",
    }


def analyze(records: list[ChatRecord]) -> dict:
    logger.info("情绪审美分析（兼容版）", extra={"extra": {"records_count": len(records)}})
    if not records:
        return {"status": "样本不足", "count": 0}
    names = Counter(r.content for r in records if r.content)
    return {
        "status": "ok",
        "说明": "基于 read() 的弱信号，缺 playCount/dt，建议用 from_db()",
        "曲名出现Top10": names.most_common(10),
    }
