"""人物画像（C1 · 甲·外挂式，纯读）：聚合 B 阶段事实表，输出行为模式画像。

只读 daily_stats / sessions / stays / trips / places，不改 ETL、不加表。

这是"完全懂主人"的落地层：应用分类聚合 / 屏幕健康度 / 使用时段分布 /
生活规律 / 综合画像卡片。所有计算纯读，device 维度按 device_id 过滤
（B2 后 daily_stats 主键为 (device_id, day)；旧库无 device_id 列时退化为全量读）。

"""

from __future__ import annotations



import datetime

import json

import sqlite3

from collections import defaultdict

from pathlib import Path

from typing import Any



_CATEGORIES_FILE = Path(__file__).resolve().parents[3] / "data" / "app_categories.json"
_TZ_CST = datetime.timezone(datetime.timedelta(hours=8))



# 内置默认分类映射（app 显示名 -> 大类）。data/app_categories.json 被 .gitignore 排除，

# 克隆仓库后该文件可能不存在；此处提供兜底，保证模块在无文件时仍产出有意义的画像。

# 若 data/app_categories.json 存在，其内容会覆盖以下默认值。

_DEFAULT_CATEGORIES: dict[str, str] = {

    "微信": "社交", "QQ": "社交", "小红书": "社交",

    "抖音": "视频", "哔哩哔哩": "视频",

    "淘宝": "购物",

    "飞书": "通讯", "WeLink": "通讯",

    "Edge": "工具", "夸克": "工具", "时钟": "工具", "天气": "工具",

    "便签": "工具", "华为乾崑": "工具",

    "网易云音乐": "其他", "Marvis": "其他",

}



# 时段划分（与 report.py _SEGMENTS 一致）：名称 / 起时 / 止时

_SEGMENTS = [

    ("凌晨", 0, 5), ("上午", 5, 11), ("午后", 11, 14),

    ("下午", 14, 18), ("晚上", 18, 23), ("深夜", 23, 24),

]

_NIGHT_SEGS = ("深夜", "凌晨")  # 23:00-05:00 归为"夜"



# 屏幕健康阈值（单日 >5h 视为重度；窗口内 >=60% 天数重度 → 重度屏幕使用者）

_DEFAULT_HEAVY_MS = 5 * 3600 * 1000

_DEFAULT_HEAVY_FRAC = 0.6



_TZ = datetime.timezone(datetime.timedelta(hours=8))





def _load_categories() -> dict:

    cats = dict(_DEFAULT_CATEGORIES)

    try:

        file_cats = json.loads(_CATEGORIES_FILE.read_text(encoding="utf-8"))

    except (FileNotFoundError, json.JSONDecodeError):

        return cats

    cats.update(file_cats)

    return cats





def _seg_of(hour: int) -> str:

    for name, a, b in _SEGMENTS:

        if a <= hour < b:

            return name

    return "深夜"





def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:

    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

    return col in cols





def _avg_hhmm(ts_list: list) -> str | None:

    if not ts_list:

        return None

    dts = [datetime.datetime.fromtimestamp(t / 1000, tz=_TZ) for (t,) in ts_list]

    mins = sum(d.hour * 60 + d.minute for d in dts) / len(dts)

    return f"{int(mins // 60):02d}:{int(mins % 60):02d}"





def build(conn: sqlite3.Connection | None = None, device_id: str | None = None,

          days: int = 7, db_path: str | Path | None = None) -> dict[str, Any]:

    """构建人物画像。conn 与 db_path 二选一；device_id 为空则全量（单设备兼容）。"""

    own = False

    if conn is None:

        if db_path is None:

            db_path = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"

        conn = sqlite3.connect(str(db_path))

        own = True

    try:

        return _build(conn, device_id, days)

    finally:

        if own:

            conn.close()





def _build(conn: sqlite3.Connection, device_id: str | None, days: int) -> dict[str, Any]:

    cur = conn.cursor()

    cats = _load_categories()



    dev_filter = ""

    dparams: list = []

    if device_id and _has_col(conn, "daily_stats", "device_id"):

        dev_filter = "WHERE device_id=?"

        dparams = [device_id]



    rows = cur.execute(

        f"SELECT day, total_screen_ms, app_ranking_json FROM daily_stats {dev_filter} "

        f"ORDER BY day DESC LIMIT ?",

        dparams + [days],

    ).fetchall()



    result: dict[str, Any] = {

        "device_id": device_id,

        "days": days,

        "available": False,

        "category_usage": [],

        "uncategorized": [],

        "screen_health": {},

        "rhythm": {},

        "routine": {},

        "traits": [],

        "card": "",

    }

    if not rows:

        return result

    result["available"] = True

    min_day = rows[-1][0]



    # ---- 1. 应用分类聚合 ----

    cat_ms = defaultdict(int)

    uncat: set[str] = set()

    total_app_ms = 0

    for _day, _total, ranking_json in rows:

        for a in (json.loads(ranking_json or "[]") or []):

            name = a.get("app")

            ms = a.get("ms", 0)

            if not name:

                continue

            total_app_ms += ms

            cat = cats.get(name)

            if cat is None:

                uncat.add(name)

                cat = "其他"

            cat_ms[cat] += ms

    cat_list = sorted(

        (

            {

                "category": c,

                "ms": m,

                "hours": round(m / 3600000, 2),

                "pct": round(m / total_app_ms * 100, 1) if total_app_ms else 0,

            }

            for c, m in cat_ms.items()

        ),

        key=lambda x: -x["ms"],

    )

    result["category_usage"] = cat_list

    result["uncategorized"] = sorted(uncat)



    # ---- 2. 屏幕健康度 ----

    totals = [r[1] for r in rows]

    avg = sum(totals) // len(totals)

    if len(totals) >= 2:

        last = totals[0]

        rest_avg = sum(totals[1:]) // len(totals[1:])

        if rest_avg and abs(last - rest_avg) / rest_avg > 0.1:

            trend = "up" if last > rest_avg else "down"

        else:

            trend = "flat"

    else:

        trend = "flat"

    heavy_days = sum(1 for t in totals if t > _DEFAULT_HEAVY_MS)

    heavy_user = (heavy_days / len(totals)) >= _DEFAULT_HEAVY_FRAC

    result["screen_health"] = {

        "avg_total_ms": avg,

        "avg_hours": round(avg / 3600000, 2),

        "trend": trend,

        "heavy_user": heavy_user,

        "heavy_days": heavy_days,

        "note": "重度屏幕使用者" if heavy_user else "屏幕使用在正常区间",

    }



    # ---- 3. 使用时段分布（rhythm）----

    sparams: list = [min_day]

    sfilter = ""

    if device_id:

        sfilter = "AND device_id=?"

        sparams = [min_day, device_id]

    seg_ms = defaultdict(int)
    try:
        _sess_rows = cur.execute(
            f"SELECT start_ms, duration_ms FROM sessions WHERE day>=? {sfilter}",
            sparams,
        ).fetchall()
    except sqlite3.OperationalError:
        # 极简库/测试夹具可能无 sessions 表 -> 节奏维度退化为空
        _sess_rows = []
    for start_ms, dur in _sess_rows:
        if not start_ms:
            continue
        dt = datetime.datetime.fromtimestamp(start_ms / 1000, tz=_TZ)
        seg_ms[_seg_of(dt.hour)] += dur

    total_seg = sum(seg_ms.values()) or 1

    night_ms = sum(seg_ms[s] for s in _NIGHT_SEGS)

    night_pct = round(night_ms / total_seg * 100, 1)

    night_owl = night_pct >= 25

    peak_seg = max(seg_ms, key=seg_ms.get) if seg_ms else None

    result["rhythm"] = {

        "segments": dict(seg_ms),

        "night_pct": night_pct,

        "night_owl": night_owl,

        "peak_segment": peak_seg,

    }

    # ---- 3.5 P0 拟人字段接入（v2 设计 3.3.1，只增不减；旧库缺列自动降级）----

    p0_cols = ["sleep_start_hhmm", "sleep_end_hhmm", "sleep_duration_min", "time_app_json"]

    if all(_has_col(conn, "daily_stats", c) for c in p0_cols):

        _p0 = cur.execute(

            f"SELECT sleep_start_hhmm, sleep_end_hhmm, sleep_duration_min, time_app_json "

            f"FROM daily_stats {dev_filter} ORDER BY day DESC LIMIT 1",

            dparams,

        ).fetchone()

        if _p0:

            result["rhythm"]["sleep_start"] = _p0[0]

            result["rhythm"]["sleep_end"] = _p0[1]

            result["rhythm"]["sleep_duration_min"] = _p0[2]

            result["rhythm"]["time_app_matrix"] = json.loads(_p0[3] or "[]")

            try:

                _rtf_days = cur.execute(

                    f"SELECT COUNT(*) FROM daily_stats {dev_filter}", dparams

                ).fetchone()[0]

            except sqlite3.Error:

                _rtf_days = 0

            result["rhythm"]["sleep_confidence"] = "low" if _rtf_days < 14 else "ok"

    # 行为稳定度：跨天聚合表 behavior_stability（P0 字段 3，8 天短窗 confidence=low）

    stability: dict[str, Any] = {}

    try:

        _bs_cols = [r[1] for r in conn.execute("PRAGMA table_info(behavior_stability)")]

    except sqlite3.OperationalError:

        _bs_cols = []

    if _bs_cols:

        bs_q = "SELECT * FROM behavior_stability"

        bs_params: list = []

        if dev_filter:

            bs_q += " WHERE device_id=?"

            bs_params = dparams

        bs_q += " ORDER BY day DESC LIMIT 1"

        try:

            bs_row = cur.execute(bs_q, bs_params).fetchone()

            if bs_row:

                _bs = dict(zip([c[0] for c in cur.description], bs_row))

                try:

                    _bs_days = cur.execute(

                        f"SELECT COUNT(*) FROM daily_stats {dev_filter}", dparams

                    ).fetchone()[0]

                except sqlite3.Error:

                    _bs_days = 0

                stability = {

                    "score": _bs.get("stability_score"),

                    "leave_home_std_ms": _bs.get("leave_home_std_ms"),

                    "return_home_std_ms": _bs.get("return_home_std_ms"),

                    "route_key_reuse_rate": _bs.get("route_key_reuse_rate"),

                    "sleep_regularity": _bs.get("sleep_regularity"),

                    "window_days": _bs.get("window_days"),

                    "confidence": "low" if _bs_days < 14 else "ok",

                    "periodicity": json.loads(_bs.get("periodicity_json") or "{}"),

                }

        except sqlite3.OperationalError:

            stability = {}

    result["stability"] = stability

    if stability.get("sleep_regularity") is not None:

        result["rhythm"]["sleep_regularity"] = stability["sleep_regularity"]



    # ---- 4. 生活规律（routine）---- 位置事实经 location_reader 双读层（v1/v2 兼容）

    # ---- deprecated 计数口径（仅缺 SpatialProfile 骨架时降级）----
    try:
        from gacore.langTrack import location_reader as lr
        all_stays = lr.read_stays(conn, device_id=device_id, with_place=True)
        home = [(s["start_ts"],) for s in all_stays if s["place_label"] == "家"]
        work = [(s["start_ts"],) for s in all_stays if s["place_label"] == "公司"]
    except Exception:  # noqa: BLE001 缺表降级：规律维度退化为空
        all_stays, home, work = [], [], []

    try:
        from gacore.langTrack import location_reader as lr
        trips_legacy = [(t["start_ts"],) for t in lr.read_trips(conn, device_id=device_id, day_from=min_day)]
    except Exception:  # noqa: BLE001 缺表降级：通勤稳定性退化为 False
        trips_legacy = []

    # deprecated：旧口径以「家/公司停留条数」「trip 数」直接推断「作息规律/通勤稳定」，
    # 证据强度不足（trip 数量≠通勤稳定，停留条数≠工作日节奏）。Task 10 起改读
    # SpatialProfile 的 median/IQR/Evidence，以下字段仅在无 spatial 骨架时回退借用。
    home_n_legacy, work_n_legacy = len(home), len(work)
    # P2-2：home_days/work_days 分别按"家/公司出现的不同本地日期数"独立统计，
    # 不再把合并口径 anchor_days 同时复制给两个字段（两个字段同为一个合并值，语义失真）。
    # 独立无样本时回退 legacy（历史停留条数，仅作兼容兜底）。
    def _local_day(ts_ms: int) -> str:
        return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=_TZ_CST).strftime("%Y-%m-%d")

    home_n = len({_local_day(s["start_ts"]) for s in all_stays
                  if s["place_label"] == "家" and s.get("start_ts")}) or home_n_legacy
    work_n = len({_local_day(s["start_ts"]) for s in all_stays
                  if s["place_label"] == "公司" and s.get("start_ts")}) or work_n_legacy
    commute_stable_legacy = len(trips_legacy) >= 3
    work_start_legacy = _avg_hhmm(work)

    # ---- Task 10：通勤/节奏改读 SpatialProfile 客观聚合（median/IQR/Evidence）----
    sp = {}
    try:
        from gacore.langTrack import spatial_profile as sprofile
        sp = sprofile.build_spatial_profile(
            conn, device_id=device_id, as_of_day=rows[0][0],
        ) or {}
    except Exception:  # noqa: BLE001 缺表/降级：spatial 骨架为空，不回退旧口径之外
        sp = {}
    _cp = sp.get("commute_profile") or {}
    _hr = sp.get("home_work_rhythm") or {}
    _cp_ev = _cp.get("evidence") or {}
    _hr_ev = _hr.get("evidence") or {}

    # 证据边界：有客观样本才算「规律/稳定」。anchor_days = 有家或公司锚点的有效天数；
    # valid_days = 有效家↔公司往返样本天数。不凭 trip 条数直接断言“通勤稳定”。
    regular = bool(_hr) and int(_hr.get("anchor_days") or 0) >= 3
    commute_stable = bool(_cp) and int(_cp.get("valid_days") or 0) >= 3
    work_start = (_cp.get("depart_hhmm_median") or work_start_legacy)

    note = []

    if regular:

        note.append("作息规律")

    if work_start:

        note.append(f"约 {work_start} 出门上班" if regular else f"约 {work_start} 开始活动")

    result["routine"] = {

        # deprecated：以下常规键为兼容旧消费者保留；证据来源已切换到 SpatialProfile。
        "regular": regular,

        "work_start": work_start,

        "commute_stable": commute_stable,

        "home_days": home_n,

        "work_days": work_n,

        "note": "；".join(note) if note else "作息数据不足",

        # Task 10：通勤/节奏证据边界——附 SpatialProfile 客观聚合（median/IQR/Evidence）。
        # 通勤稳定性/作息规律只由下列证据判定，不直接由 trip 数量或停留条数断言。
        "commute_evidence": {
            "depart_median": _cp.get("depart_hhmm_median"),
            "depart_iqr": _cp.get("depart_hhmm_iqr"),
            "duration_human": _cp.get("duration_human"),
            "valid_days": _cp.get("valid_days"),
            "weekday_valid_days": _cp.get("weekday_valid_days"),
            "endpoint_dist_m": _cp.get("endpoint_dist_m"),
            "evidence": _cp_ev,
        },

        "home_work_rhythm": _hr,  # 含 weekday/weekend median/IQR/anchor_days/evidence

    }



    # ---- 5. 特征 + 画像卡片 ----

    traits: list[str] = []

    video = next((c for c in cat_list if c["category"] == "视频"), None)

    if video and (video["pct"] >= 25 or video is cat_list[0]):

        traits.append(f"重度视频消费者（日均约 {video['hours']}h）")

    social = next((c for c in cat_list if c["category"] == "社交"), None)

    if social and social is cat_list[0]:

        traits.append("社交重度用户")

    if heavy_user:

        traits.append("重度屏幕使用者")

    if night_owl:

        traits.append(f"夜猫子（深夜活跃占比 {night_pct}%）")

    if regular:

        traits.append("作息规律")



    parts = []

    if regular:

        parts.append("作息规律的通勤上班族")

    elif work_start:

        parts.append(f"约 {work_start} 开始一天活动")

    else:

        parts.append("生活节奏数据尚少")

    if video:

        parts.append(

            f"视频消费者（日均 {video['hours']}h{'，居首' if video is cat_list[0] else ''}）"

        )

    if social:

        parts.append(f"社交{'重度' if social is cat_list[0] else '轻度'}")

    if night_owl:

        parts.append("典型夜猫子")

    card = "；".join(parts) + "。"



    result["traits"] = traits

    result["card"] = card

    return result
