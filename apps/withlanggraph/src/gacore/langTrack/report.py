"""langTrack 分析报告：基于事实表生成"今天发生了什么"的数字生活画像与决策建议。

用法：

    python -m gacore.langTrack.report            # 今日

    python -m gacore.langTrack.report --day 2026-08-17

    python -m gacore.langTrack.report --day 2026-08-17 --verbose   # 含原始明细

    python -m gacore.langTrack.report --device <device_id>        # 多设备库必选

设备语义（Task 5d）：多设备库当日有 >=2 台设备数据时，必须 --device 显式
选择一台，报告禁止跨设备合并画像；单设备自动采用唯一设备。

输出分四块（对应修订文档 R6-P2）：

1. 屏幕时间（总量/app 排行/最长会话）

2. 通知疲劳（谁轰炸/几点轰炸/点击率 → 关哪些通知）

3. 睡眠推断（深夜通知/音频/亮屏）

4. 场景分布（places 标签 + wifi）

"""

from __future__ import annotations

import argparse

import datetime

import json

import sqlite3

import sys

from collections import defaultdict

from pathlib import Path
from gacore.langTrack.location_facts import format_place, resolve_place_name, user_tag_of
from gacore.langTrack.location_reader import table_columns
from gacore.langTrack.persona import build as build_persona

_TZ_CST = datetime.timezone(datetime.timedelta(hours=8))

def now_cst() -> datetime.datetime:
    """当前东八区时间（显式 +8，不依赖服务器本地时区）。"""
    return datetime.datetime.now(tz=_TZ_CST)

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"
def fmt_dur(ms: int) -> str:

    h, rem = divmod(ms // 1000, 3600)

    m = rem // 60

    return f"{h}小时{m}分" if h else f"{m}分钟"

# P1-4 时段划分（本地时间）：名称 / 起时 / 止时

_SEGMENTS = [

    ("凌晨", 0, 5), ("上午", 5, 11), ("午后", 11, 14),

    ("下午", 14, 18), ("晚上", 18, 23), ("深夜", 23, 24),

]

def _seg_of(hour: int) -> str:

    for name, a, b in _SEGMENTS:

        if a <= hour < b:

            return name

    return "深夜"

class MultiDeviceError(RuntimeError):
    """多设备库未选择设备：report 禁止跨设备合并画像（Task 5d）。"""

def devices_of_day(conn: sqlite3.Connection, day: str) -> list[str]:
    """当日有数据的设备（daily_stats / sessions / stays / anomalies 并集，含最小 schema 容错）。"""
    devs: set[str] = set()
    for table in ("daily_stats", "sessions", "stays", "anomalies"):
        cols = table_columns(conn, table)
        if "device_id" not in cols or "day" not in cols:
            continue
        try:
            devs.update(r[0] for r in conn.execute(
                f"SELECT DISTINCT device_id FROM {table} WHERE day=?", (day,)))
        except sqlite3.OperationalError:
            continue
    return sorted(devs)

def resolve_report_device(
    conn: sqlite3.Connection, day: str, device_id: str | None
) -> str | None:
    """解析报告设备（Task 5d）：多设备必须显式选择，禁止合并。

    - 显式指定 → 原样使用（该设备当日无数据时下游按"当日无数据"处理）；
    - 未指定：恰一台 → 自动采用；多台 → MultiDeviceError；零台 → None。
    """
    if device_id is not None:
        return device_id
    devs = devices_of_day(conn, day)
    if len(devs) > 1:
        raise MultiDeviceError(
            f"检测到 {len(devs)} 台设备当日有数据（{', '.join(devs)}），"
            "报告禁止跨设备合并画像，请用 --device 指定其中一台"
        )
    return devs[0] if devs else None

def _dev_bind(device_id: str | None) -> tuple[str, list]:
    """(SQL 片段, 参数) 成对返回，杜绝拼接与参数列表不同步：

    显式设备 → (" AND device_id=?", [device_id])；None（单设备/全库空态）→ ("", [])。
    """
    if device_id is None:
        return "", []
    return " AND device_id=?", [device_id]

def _fusion(conn: sqlite3.Connection, day: str, device_id: str | None = None) -> None:

    """P1-4 位置语义 × 使用数据按时段对齐，支撑'宅家刷手机'类叙事描述。"""

    day_start_ms = int(datetime.datetime.fromisoformat(f"{day} 00:00").timestamp()) * 1000

    day_end_ms = day_start_ms + 86400000

    # 位置事实经 location_reader 双读层（v1: grid_key JOIN / v2: place_id JOIN）
    from gacore.langTrack import location_reader as lr

    dev_frag, dev_args = _dev_bind(device_id)
    stays = lr.read_stays(conn, overlap=(day_start_ms, day_end_ms), device_id=device_id)

    sessions = conn.execute(

        f"SELECT start_ms, duration_ms, app FROM sessions WHERE day=?{dev_frag} "
        "ORDER BY start_ms",
        [day, *dev_args],

    ).fetchall()

    if not stays and not sessions:

        print("  当日无位置/使用数据")

        return

    per_seg_screen: dict[str, int] = defaultdict(int)

    per_seg_app: dict[str, list] = defaultdict(list)

    per_seg_place: dict[str, list] = defaultdict(list)

    for s in sessions:

        h = datetime.datetime.fromtimestamp(s["start_ms"] / 1000).hour

        seg = _seg_of(h)

        per_seg_screen[seg] += s["duration_ms"]

        per_seg_app[seg].append((s["app"], s["duration_ms"]))

    for st in stays:

        s_sec = st["start_ts"] / 1000

        e_sec = st["end_ts"] / 1000

        # Task 10：统一 PlaceRef 安全文案（不输出坐标、grid_key）
        name = _place_safe_from_stay(st)

        lbl = st["place_label"] or "未知"

        for sname, a, b in _SEGMENTS:

            seg_start = day_start_ms / 1000 + a * 3600

            seg_end = day_start_ms / 1000 + b * 3600

            if s_sec < seg_end and e_sec > seg_start:  # 停留段与该时段有交集

                per_seg_place[sname].append(

                    (max(s_sec, seg_start), min(e_sec, seg_end), name, lbl)

                )

    for name, a, b in _SEGMENTS:

        places = per_seg_place.get(name)

        apps = sorted(per_seg_app.get(name, []), key=lambda x: -x[1])

        scr = per_seg_screen.get(name, 0)

        loc = ""

        if places:

            # 取该时段覆盖时长最长的停留段

            p = max(places, key=lambda x: x[1] - x[0])

            loc = f"[{p[3]}]{p[2]}"

        if scr:

            top_app = apps[0][0] if apps else "-"

            print(f"  {a:02d}:00-{b:02d}:00 {name}  {loc:<26} 屏幕 {fmt_dur(scr)}  App {top_app}")

        else:

            print(f"  {a:02d}:00-{b:02d}:00 {name}  {loc or '—'}")

    # 叙事句：比较白天(5-18)在家与在公司/外出的屏幕使用，支撑"宅家刷手机"类描述

    day_segs = ("上午", "午后", "下午")

    day_screen = sum(per_seg_screen.get(s, 0) for s in day_segs)

    def _seg_app_ms(lbl: str) -> int:

        return int(

            sum(

                max(e - b, 0) for s in day_segs for (b, e, _n, _l) in per_seg_place.get(s, []) if _l == lbl

            ) * 1000

        )

    home_ms = _seg_app_ms("家")

    work_ms = _seg_app_ms("公司")

    if day_screen > 30 * 60 * 1000 and home_ms >= work_ms and home_ms > 0:

        tops = sorted(

            [(a, m) for seg in day_segs for a, m in per_seg_app.get(seg, [])],

            key=lambda x: -x[1],

        )

        main_app = tops[0][0] if tops else "手机"

        print(f"  → 叙事：白天宅家（{fmt_dur(home_ms)}）为主，屏幕共 {fmt_dur(day_screen)}，屏幕上用时最长的是 {main_app}")

    elif day_screen > 0 and work_ms > home_ms:

        wtops = sorted(

            [(a, m) for seg in day_segs for a, m in per_seg_app.get(seg, [])],

            key=lambda x: -x[1],

        )

        print(f"  → 叙事：白天主要在公司（{fmt_dur(work_ms)}），屏幕共 {fmt_dur(day_screen)}，屏幕上用时最长的是 {wtops and wtops[0][0] or '手机'}")

    elif day_screen > 0:

        print(f"  → 叙事：白天屏幕 {fmt_dur(day_screen)}，主要活跃地点非家非公司（外出/通勤）")

def _outings(conn: sqlite3.Connection, day: str, device_id: str | None = None) -> list:

    """P2：识别正式停留(stays)之外、且非家/公司的'短暂外出'，让已落库地点语义出现在画像里。

    方法：按网格聚合当日 GPS 精度较好(acc<=150)的点；每个网格内按 >45min 空档切段，

    段内 >=2 点且时间跨度 >=2min 的网格即视为一个短暂停留候选（排除家/公司等已知网格）。

    相邻(<=20min)的外出合并为一趟；落在公司停留段时间窗内的簇提示'午休外出/中途离开公司'，

    其余输出'短暂停留'叙事。不做激进拆分，仅作叙事提示。

    """

    day_start_ms = int(datetime.datetime.fromisoformat(f"{day} 00:00").timestamp()) * 1000

    day_end_ms = day_start_ms + 86400000

    from gacore.langTrack import location_reader as lr

    dev_frag, dev_args = _dev_bind(device_id)

    # 位置事实经双读层：stays 内嵌 place（v2 按 place_id JOIN）；网格→place 用 place_grid_map
    stays = lr.read_stays(conn, overlap=(day_start_ms, day_end_ms), device_id=device_id)

    grid_places = lr.place_grid_map(conn, device_id=device_id)

    known_grids = {s["grid_key"] for s in stays}

    # 家/公司网格直接从 grid_places 派生，避免重读 places + place_cells
    known_grids |= {g for g, p in grid_places.items() if p["label"] in ("家", "公司")}

    work_windows = []

    for st in stays:

        if st["place_label"] == "公司":

            work_windows.append((st["start_ts"] / 1000, st["end_ts"] / 1000))

    grid_pts: dict[str, list] = defaultdict(list)

    for r in conn.execute(

        f"SELECT ts, payload FROM events WHERE type='location' AND ts>=? AND ts<?"
        f"{dev_frag} ORDER BY ts",
        [day_start_ms, day_end_ms, *dev_args],

    ):

        try:

            pl = json.loads(r["payload"])

        except (TypeError, ValueError):

            continue

        if pl.get("provider") != "gps" or pl.get("acc", 999) > 150:

            continue

        grid_pts[f"{pl['lat']:.3f},{pl['lon']:.3f}"].append((r["ts"] / 1000, pl["lat"], pl["lon"]))

    cands = []

    for g, plist in grid_pts.items():

        if g in known_grids:

            continue

        plist.sort()

        segs, cur = [], [plist[0]]

        for i in range(1, len(plist)):

            if plist[i][0] - plist[i - 1][0] > 2700:  # 网格内 >45min 空档视为不同到访

                segs.append(cur)

                cur = [plist[i]]

            else:

                cur.append(plist[i])

        segs.append(cur)

        for seg in segs:

            if len(seg) >= 2 and seg[-1][0] - seg[0][0] >= 120:  # 至少2点、停留>=2分钟

                cands.append({"s": seg[0][0], "e": seg[-1][0], "grid": g})

    if not cands:

        return []

    cands.sort(key=lambda x: x["s"])

    groups, cur = [], [cands[0]]

    for c in cands[1:]:

        if c["s"] - cur[-1]["e"] <= 1200:  # 相邻外出间隔 <=20min 视为同一趟

            cur.append(c)

        else:

            groups.append(cur)

            cur = [c]

    groups.append(cur)

    def _name(gk: str) -> str:

        p = grid_places.get(gk)

        if not p:

            return "未知地点"  # 不输出坐标网格

        return _place_safe_text(p)

    outings: list = []

    print("\n■ 短暂停留/外出")

    for grp in groups:

        gs, ge = grp[0]["s"], grp[-1]["e"]

        names, seen = [], set()

        for c in grp:

            nm = _name(c["grid"])

            if nm not in seen:

                seen.add(nm)

                names.append(nm)

        place_desc = "→".join(names)

        t0 = datetime.datetime.fromtimestamp(gs).strftime("%H:%M")

        t1 = datetime.datetime.fromtimestamp(ge).strftime("%H:%M")

        mins = round((ge - gs) / 60)

        in_work = any(w0 - 30 <= gs and ge <= w1 + 30 for w0, w1 in work_windows)

        if in_work:

            hour = datetime.datetime.fromtimestamp(gs).hour

            tag = "午休外出" if 11 <= hour < 14 else "中途离开公司"

            print(f"  → {tag}：{t0}-{t1} 短暂离开公司，至 {place_desc} 一带（约 {mins} 分钟）")

        else:

            tag = "短暂停留"

            print(f"  → {tag}：{t0}-{t1} {place_desc} 一带（约 {mins} 分钟）")

        outings.append({

            "tag": tag, "start": t0, "end": t1, "place": place_desc, "minutes": mins,

        })

    return outings

_ACTIVITY_MAP = {

    # Task 10 证据边界：只给“场景”，不输出“用餐/就医”等确定性活动动词。

    "餐饮服务": "餐饮", "购物服务": "购物", "医疗保健服务": "医疗",

    "体育休闲服务": "休闲", "科教文化服务": "文娱", "娱乐场所": "娱乐",

    "生活服务": "生活服务", "住宿服务": "住宿", "风景名胜": "风景",

}

_BEHAVIOR_SCENE = {

    "用餐": "餐饮", "购物": "购物", "就医": "医疗", "休闲": "休闲",

    "娱乐": "娱乐", "游玩": "风景", "住宿": "住宿",

}

def _activity_of(poi_type: str | None, behavior: str | None) -> str | None:

    """P2-1 活动语义增强：POI type 中文大类/行为 → 中性场景词（餐饮/医疗…）。

    Task 10 证据边界：只陈述“在 X 场景附近停留”，不断言用户“在用餐/就医”。"""

    if behavior and behavior in _BEHAVIOR_SCENE:

        return _BEHAVIOR_SCENE[behavior]

    if poi_type:

        l1 = poi_type.split(";")[0]

        if l1 in _ACTIVITY_MAP:

            return _ACTIVITY_MAP[l1]

    return None

def _place_safe_text(p: dict) -> str:

    """统一 PlaceRef 安全文案（§2.6）：不输出坐标、grid_key、payload。"""

    pn, _src, _gran = resolve_place_name(

        poi=p.get("poi") or "",

        poi_fallback=p.get("poi_fallback") or "",

        address=p.get("address") or "",

        district=p.get("district") or "",

        township=p.get("township") or "",

        business_area=p.get("business_area") or "",

        parent_poi=p.get("parent_poi") or "",

        name_confidence=p.get("name_confidence") or 0.0,

        label=p.get("label") or "",

    )

    return format_place(pn, user_tag_of(p.get("label") or ""))


def _place_safe_from_stay(st: dict) -> str:

    """read_stays 行（内嵌 place_* 前缀字段）→ PlaceRef 安全文案。"""

    pn, _s, _g = resolve_place_name(

        poi=st.get("place_poi") or "",

        poi_fallback=st.get("place_poi_fallback") or "",

        address=st.get("place_address") or "",

        district=st.get("place_district") or "",

        township="", business_area="", parent_poi="",

        name_confidence=st.get("place_name_confidence") or 0.0,

        label=st.get("place_label") or "",

    )

    return format_place(pn, user_tag_of(st.get("place_label") or ""))

def _consumption(conn: sqlite3.Connection, day: str, device_id: str | None = None) -> list:

    """P2-1 消费/商圈画像：常去的餐饮/购物/医疗/休闲等活动类地点 + 商圈归属。"""

    from gacore.langTrack import location_reader as lr

    _poi_type_kw = ("餐饮", "购物", "医疗", "体育", "休闲", "娱乐", "住宿", "风景名胜")

    _behaviors = ("用餐", "购物", "就医", "休闲", "娱乐", "游玩", "住宿")

    def _match(p: dict) -> bool:

        if p["business_area"]:

            return True

        pt = p["poi_type"] or ""

        if any(k in pt for k in _poi_type_kw):

            return True

        return (p["behavior"] or "") in _behaviors

    rows = [p for p in lr.read_places(conn, device_id=device_id) if _match(p)][:8]

    items = []

    for p in rows:

        # Task 10 证据边界：只陈述“在 X 场景附近停留”，不断言“在用餐/就医/上班”。

        act = _activity_of(p["poi_type"], p["behavior"]) or "活动"

        items.append({

            "activity": f"在{act}附近停留",

            "name": _place_safe_text(p),

            "visit_count": p["visit_count"],

            "business_area": p["business_area"] or "",

            "district": p["district"] or "",

        })

    print("\n■ 消费/商圈画像")

    if not items:

        print("  （当前无消费/商圈样本——places 暂无餐饮/购物等活动类地点，"

              "ETL 补充 around 商圈数据后自动呈现）")

    else:

        for it in items:

            ba = f" · 商圈:{it['business_area']}" if it["business_area"] else ""

            print(f"  [{it['activity']}] {it['name']} 常去 {it['visit_count']} 次{ba}")

    return items

def _avg_hhmm(dts: list) -> str:

    mins = sum(dt.hour * 60 + dt.minute for dt in dts) / max(1, len(dts))

    return f"{int(mins // 60):02d}:{int(mins % 60):02d}"

def _avg_hhmm_night(dts: list) -> str:

    """跨午夜睡眠时间平均：凌晨(hour<12)视为次日(+24h)回绕，避免 23:xx 与 00:xx 直接平均失真。"""

    mins = []

    for dt in dts:

        m = dt.hour * 60 + dt.minute

        if dt.hour < 12:

            m += 24 * 60

        mins.append(m)

    avg = sum(mins) // len(mins) % (24 * 60)

    return f"{avg // 60:02d}:{avg % 60:02d}"

def _rhythm_weather(conn: sqlite3.Connection, day: str, device_id: str | None = None) -> dict:

    """P2-2 节律建模 + 天气语境：多日数据聚合作息节律（睡眠/工作/周末差异），天气接口加环境语境。"""

    result: dict = {"rhythm": None, "weather": None}

    dev_frag, dev_args = _dev_bind(device_id)

    days = [r["day"] for r in conn.execute(

        f"SELECT DISTINCT day FROM daily_stats WHERE 1=1{dev_frag} "
        "ORDER BY day DESC LIMIT 7",
        list(dev_args),

    )]

    if not days:

        print("\n■ 节律与天气")

        print("  （近 7 日无 daily_stats 数据，无法建模节律）")

        return result

    start_day = min(days)

    stats = conn.execute(

        f"SELECT day, total_screen_ms FROM daily_stats WHERE day>=?{dev_frag}",
        [start_day, *dev_args],

    ).fetchall()

    wd_total, we_total, wd_n, we_n = 0, 0, 0, 0

    for s in stats:

        if datetime.date.fromisoformat(s["day"]).weekday() >= 5:

            we_total += s["total_screen_ms"]; we_n += 1

        else:

            wd_total += s["total_screen_ms"]; wd_n += 1

    from gacore.langTrack import location_reader as lr

    stays = lr.read_stays(conn, day_from=start_day, device_id=device_id)

    work_starts, work_ends, sleep_starts, sleep_ends = [], [], [], []

    for st in stays:

        lbl = st["place_label"]

        st_dt = datetime.datetime.fromtimestamp(st["start_ts"] / 1000)

        en_dt = datetime.datetime.fromtimestamp(st["end_ts"] / 1000)

        if lbl == "公司":

            work_starts.append(st_dt); work_ends.append(en_dt)

        elif lbl == "家" and (st_dt.hour >= 20 or en_dt.hour <= 10):

            sleep_starts.append(st_dt); sleep_ends.append(en_dt)

    rhythm: dict = {"days": len(days)}

    if work_starts:

        rhythm["work_start"] = _avg_hhmm(work_starts)

        rhythm["work_end"] = _avg_hhmm(work_ends)

    if sleep_starts:

        rhythm["sleep_start"] = _avg_hhmm_night(sleep_starts)

        rhythm["wake_up"] = _avg_hhmm(sleep_ends)

    rhythm["weekday_screen_avg_ms"] = wd_total // max(1, wd_n)

    rhythm["weekend_screen_avg_ms"] = we_total // max(1, we_n)

    result["rhythm"] = rhythm

    print("\n■ 节律与天气")

    print(f"  节律（近{len(days)}天）:")

    if "work_start" in rhythm:

        print(f"    工作: 平均 {rhythm['work_start']} 到公司, {rhythm['work_end']} 离开")

    else:

        print("    工作: 数据不足（近期无公司停留段）")

    if "sleep_start" in rhythm:

        print(f"    睡眠: 平均 {rhythm['sleep_start']} 入睡, 次日 {rhythm['wake_up']} 起床")

    else:

        print("    睡眠: 数据不足")

    print(f"    屏幕: 工作日平均 {fmt_dur(rhythm['weekday_screen_avg_ms'])}"

          f" / 周末平均 {fmt_dur(rhythm['weekend_screen_avg_ms'])}")

    try:

        from gacore.langTrack import weather

        w = weather.get_weather(day)

    except Exception as e:

        print(f"  [天气] 获取失败: {e}")

        w = {}

    if w and w.get("status") == "1":

        result["weather"] = w

        approx = "（近似）" if w.get("approx") else ""

        print(f"  天气 {w.get('date')}{approx}: {w['text_day']} {w['temp_max']}~{w['temp_min']}℃ "

              f"{w.get('wind', '')}风")

    else:

        print("  天气: 无数据")

    return result

def _write_snapshot(
    conn: sqlite3.Connection, day: str, profile: dict,
    device_id: str | None = None,
) -> None:

    """P2-0 L5 画像快照：结构化 JSON 落盘，LLM 渲染与数据解耦，支持历史对比。

    多设备当日有数据时文件名追加设备段（`_{device_id}`），避免两台设备的
    同日快照互相覆盖；单设备保持既有命名，历史对比路径不变。
    """

    out = DB_PATH.parent / "profiles"

    out.mkdir(parents=True, exist_ok=True)

    suffix = f"_{device_id}" if device_id else ""

    p = out / f"langTrack_profile_{day}{suffix}.json"

    p.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"  → L5 画像快照已落盘: {p}")

def report(
    conn: sqlite3.Connection, day: str, verbose: bool = False,
    device_id: str | None = None,
    now_override: datetime.datetime | None = None,
) -> None:
    """生成某日画像报告。

    Task 5d 设备语义：device_id 未指定时自动解析——多设备当日有数据则抛
    MultiDeviceError（禁止合并画像），单设备自动采用，无数据按全库空态处理。
    指定后所有事实查询（daily_stats/sessions/events/places/stays/anomalies/
    persona）都按该设备过滤。

    now_override（P2-3 注入点）：数据水位比较用的“当前时刻”，仅影响
    “最后定位早于当前”的措辞分支；默认 now_cst()。测试可传固定时刻，
    避免断言随时间漂移。
    """

    conn.row_factory = sqlite3.Row

    device_id = resolve_report_device(conn, day, device_id)

    dev_frag, dev_args = _dev_bind(device_id)

    print("=" * 52)

    dev_part = f" · 设备 {device_id}" if device_id else ""

    print(f"  langTrack 数字生活画像 · {day}{dev_part}")

    print("=" * 52)

    # ---------- 数据水位（Task 10 证据边界：不冒充“此刻”） ----------

    # 最后定位早于当前时间时只输出“最后记录到”，避免把历史画像写成“当前正在”。

    import datetime  # noqa: F401（函数体内已有 local import 约定）

    try:

        _last_row = conn.execute(

            f"SELECT MAX(ts) m FROM events WHERE type='location'{dev_frag}",

            list(dev_args),

        ).fetchone()

    except sqlite3.OperationalError:  # 缺 events 表（极简库）

        _last_row = None

    if _last_row and _last_row["m"]:

        last_dt = datetime.datetime.fromtimestamp(_last_row["m"] / 1000, tz=_TZ_CST)

        now_ref = now_override or now_cst()

        if last_dt < now_ref:

            print(f"  最后定位记录到 {last_dt:%Y-%m-%d %H:%M}"

                  "（早于当前，以下画像均截至该时刻）")

        else:

            print(f"  最后定位记录到 {last_dt:%Y-%m-%d %H:%M}（时间近似当前）")

    # ---------- 1. 屏幕时间 ----------

    print("\n■ 屏幕时间")

    row = conn.execute(

        f"SELECT * FROM daily_stats WHERE day=?{dev_frag}",
        [day, *dev_args],

    ).fetchone()

    if not row:

        print("  当日无数据")

        return

    print(f"  总屏幕时间: {fmt_dur(row['total_screen_ms'])}"
          f"  · 解锁 {row['unlock_count']} 次 · 切换 {row['switch_count']} 次")

    ranking = json.loads(row["app_ranking_json"] or "[]")

    for i, app in enumerate(ranking[:8], 1):

        print(f"  {i}. {app['app']}: {fmt_dur(app['ms'])}")

    # 最长连续使用段（沉浸时段）

    top = conn.execute(

        f"SELECT app, duration_ms, start_ms FROM sessions WHERE day=?{dev_frag} "
        "ORDER BY duration_ms DESC LIMIT 3",
        [day, *dev_args],

    ).fetchall()

    if top:

        print("  最长连续使用:")

        for s in top:

            import datetime

            t = datetime.datetime.fromtimestamp(s["start_ms"] / 1000).strftime("%H:%M")

            print(f"    {t} {s['app']} {fmt_dur(s['duration_ms'])}")

    # ---------- 2. 通知疲劳 ----------

    print("\n■ 通知疲劳")

    print(f"  通知 {row['notification_count']} 条, 点击 {row['notification_clicked']} 条"

          f" (点击率 {row['notification_clicked'] / max(1, row['notification_count']) * 100:.0f}%)")

    notif_apps = json.loads(row["top_notification_apps_json"] or "[]")

    if notif_apps:

        print("  通知来源:")

        for a in notif_apps:

            print(f"    {a['app']}: {a['n']} 条")

    # 每小时分布（找轰炸时段）

    hours = conn.execute(

        "SELECT strftime('%H', ts/1000,'unixepoch','+8 hours') h, COUNT(*) n "
        "FROM events WHERE type='notification' AND date(ts/1000,'unixepoch','+8 hours')=?"
        f"{dev_frag} GROUP BY h ORDER BY h",
        [day, *dev_args],

    ).fetchall()

    if hours:

        peak = max(hours, key=lambda r: r["n"])

        print(f"  通知高峰: {peak['h']}:00 ({peak['n']} 条)")

        for r in hours:

            bar = "#" * min(r["n"], 30)

            print(f"    {r['h']}:00 {r['n']:3d} {bar}")

    # 高量低点击 → 建议关闭

    if notif_apps:

        print("  降噪建议:")

        for a in notif_apps:

            if a["n"] >= 3:

                print(f"    「{a['app']}」{a['n']} 条 — 若多为无关推送可考虑关通知")

    # ---------- 3. 睡眠推断 ----------

    print("\n■ 睡眠推断（粗略）")

    late_notif = conn.execute(

        "SELECT COUNT(*) n FROM events WHERE type='notification' "

        "AND date(ts/1000,'unixepoch','+8 hours')=? "

        "AND strftime('%H', ts/1000,'unixepoch','+8 hours') BETWEEN '22' AND '23'"
        f"{dev_frag}",
        [day, *dev_args],

    ).fetchone()["n"]

    midnight_audio = conn.execute(

        "SELECT COUNT(*) n FROM events WHERE type='audio_env' "

        "AND date(ts/1000,'unixepoch','+8 hours')=? "

        "AND strftime('%H', ts/1000,'unixepoch','+8 hours') BETWEEN '00' AND '05'"
        f"{dev_frag}",
        [day, *dev_args],

    ).fetchone()["n"]

    screen_on = row["screen_on_count"]

    if midnight_audio > 5:

        print(f"  凌晨 00-05 点仍有 {midnight_audio} 条环境音频样本 → 疑似熬夜")

    if late_notif:

        print(f"  22-23 点收到 {late_notif} 条通知 → 睡前仍被手机打扰")

    print(f"  亮屏 {screen_on} 次, 解锁 {row['unlock_count']} 次")

    # ---------- 4. 场景分布 ----------

    print("\n■ 场景分布")

    from gacore.langTrack import location_reader as lr

    places = lr.read_places(conn, limit=5, device_id=device_id)

    scenes = []

    for p in places:

        # Task 10 证据边界：统一 PlaceRef 安全文案，不输出坐标、grid_key、payload。

        if p["label"] == "未知":

            if p["candidate_label"]:

                disp = f"疑似{p['candidate_label']}(待确认)"

            else:

                disp = _place_safe_text(p)

        else:

            disp = _place_safe_text(p)

        print(f"  [{disp}] 访问 {p['visit_count']} 次")

        scenes.append({

            "display": disp,

            "visit_count": p["visit_count"], "label": p["label"],

        })

    # ---------- 4.5 短暂停留/外出（P2：让'去了哪'如实出现在画像里） ----------

    outings = _outings(conn, day, device_id)

    # ---------- 5. 家/公司确认（P1-1 画像确认闭环入口） ----------

    print("\n■ 家/公司确认")

    pending = lr.read_places(conn, candidate_only=True, limit=6, device_id=device_id)

    if pending:

        for p in pending:

            conf = f"家 {p['confidence_home']:.2f} / 公司 {p['confidence_work']:.2f}"

            print(f"  [疑似{p['candidate_label']}] 访问 {p['visit_count']} 次"

                  f" · {conf} · {_place_safe_text(p)}")

        print("  → 运行 python -m gacore.langTrack.label_places 确认定名，"

              "确认后写入 data/place_labels.json 并在下次 ETL 正式生效")

    else:

        print("  暂无待确认候选点")

    # ---------- 6. 新地点/异常事件（P1-3 打破规律的点，作画像叙事节点） ----------

    print("\n■ 新地点/异常事件")

    anomaly_list = []

    has_anomalies = conn.execute(

        "SELECT name FROM sqlite_master WHERE type='table' AND name='anomalies'"

    ).fetchone()

    if not has_anomalies:

        print("  （anomalies 表未建立，请先重跑 ETL）")

    else:

        anoms = lr.read_anomalies(conn, day=day, device_id=device_id)

        if not anoms:

            print("  今日无异常事件")

        for a in anoms:

            kind_label = {

                "new_place": "新地点",

                "late_night_out": "深夜外出",

                "off_schedule": "缺席办公",

                "route_change": "路线变化",

            }.get(a["kind"], a["kind"])

            anomaly_list.append({"kind": a["kind"], "poi": a["poi"], "detail": a["detail"]})

            print(f"  [{kind_label}] {a['detail']}")

    # ---------- 7. 时段·位置·App 融合（P1-4 位置语义与使用数据对齐） ----------
    print("\n■ 时段·位置·App 融合")
    _fusion(conn, day, device_id)

    # ---------- 7.5 采集覆盖（A① 契约覆盖校验） ----------
    print("\n■ 采集覆盖")
    has_cov = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='contract_coverage'"
    ).fetchone()
    if not has_cov:
        print("  （contract_coverage 表未建立，请先重跑 ETL）")
    else:
        cov_rows = conn.execute(
            "SELECT type, desc, status, last_seen_ts, consumed FROM contract_coverage "
            "WHERE status IN ('missing','stale','unexpected') ORDER BY status, type"
        ).fetchall()
        if not cov_rows:
            print("  全部期望事件类型均已正常到达 ✓")
        else:
            for r in cov_rows:
                label = {
                    "missing": "从未到达", "stale": "停滞", "unexpected": "未登记新类型",
                }.get(r["status"], r["status"])
                last = ""
                if r["last_seen_ts"]:
                    last = " 最后:" + datetime.datetime.fromtimestamp(
                        r["last_seen_ts"] / 1000).strftime("%Y-%m-%d %H:%M")
                print(f"  [{label}] {r['type']}（{r['desc'] or '—'}） 消耗:{r['consumed']}{last}")

    # ---------- 8. 消费/商圈画像（P2-1） ----------

    consumption = _consumption(conn, day, device_id)

    # ---------- 9. 节律建模 + 天气语境（P2-2） ----------

    rhythm_weather = _rhythm_weather(conn, day, device_id)

    # ---------- 9.5 人物画像（C1 north-star 输出） ----------
    print("\n■ 人物画像")
    try:
        persona = build_persona(conn=conn, device_id=device_id, days=7)
    except Exception as e:
        print(f"  [人物画像] 生成失败: {e}")
        persona = None
    if persona and persona.get("available"):
        print(f"  {persona['card']}")
        if persona.get("traits"):
            print("  特征: " + " · ".join(persona["traits"]))
        sh = persona.get("screen_health", {})
        print(f"  屏幕: 日均 {sh.get('avg_hours', 0)}h（{sh.get('note', '')}）")
        rh = persona.get("rhythm", {})
        print(f"  节奏: 深夜活跃占比 {rh.get('night_pct', 0)}%"
              f"（{'夜猫子' if rh.get('night_owl') else '正常'}），高峰时段 {rh.get('peak_segment', '—')}")
        rt = persona.get("routine", {})
        if rt.get("note"):
            print(f"  规律: {rt['note']}")
        cats = persona.get("category_usage", [])
        if cats:
            top = " / ".join(f"{c['category']} {c['hours']}h" for c in cats[:3])
            print(f"  分类 Top: {top}")
        uncat = persona.get("uncategorized", [])
        if uncat:
            print(f"  待分类 app: {', '.join(uncat)}")
    elif persona:
        print("  近 7 日无足够数据生成画像")

    # ---------- P2-0 L5 每日结构化画像快照（与 LLM 渲染解耦，可历史对比） ----------

    profile: dict = {

        "date": day,

        "device_id": device_id,

        "generated_at": now_cst().isoformat(timespec="seconds"),

        "screen": {

            "total_screen_ms": row["total_screen_ms"],

            "unlock_count": row["unlock_count"],

            "switch_count": row["switch_count"],

            "app_ranking": ranking,

            "top_sessions": [

                {"app": s["app"], "duration_ms": s["duration_ms"], "start_ms": s["start_ms"]}

                for s in conn.execute(

                    f"SELECT app, duration_ms, start_ms FROM sessions WHERE day=?{dev_frag} "
                    "ORDER BY duration_ms DESC LIMIT 3",
                    [day, *dev_args])

            ],

        },

        "notifications": {

            "count": row["notification_count"], "clicked": row["notification_clicked"],

            "top_apps": notif_apps,

            "peak_hour": hours[0]["h"] if hours else None,

        },

        "sleep": {

            "midnight_audio_samples": midnight_audio,

            "late_notifications_22_23": late_notif,

            "screen_on_count": screen_on,

        },

        "scenes": scenes,

        "outings": outings,

        "anomalies": anomaly_list,

        "consumption": consumption,

        "rhythm": rhythm_weather.get("rhythm"),
        "persona": persona if (persona and persona.get("available")) else None,

        "weather": rhythm_weather.get("weather"),

    }

    # 多设备当日有数据时快照文件名带设备段，避免互相覆盖
    multi_day_device = device_id if len(devices_of_day(conn, day)) > 1 else None

    _write_snapshot(conn, day, profile, device_id=multi_day_device)

    if verbose:

        print("\n■ 原始明细（usage 今日）")

        for s in conn.execute(

            f"SELECT app, duration_ms, start_ms FROM sessions WHERE day=?{dev_frag} "
            "ORDER BY start_ms LIMIT 20",
            [day, *dev_args],

        ):

            import datetime

            t = datetime.datetime.fromtimestamp(s["start_ms"] / 1000).strftime("%H:%M")

            print(f"    {t} {s['app']} {fmt_dur(s['duration_ms'])}")

def main() -> None:

    parser = argparse.ArgumentParser(description="langTrack 分析报告")

    parser.add_argument("--db", type=Path, default=DB_PATH)

    parser.add_argument("--day", default=None, help="日期 YYYY-MM-DD，默认今天")

    parser.add_argument("--verbose", action="store_true")

    parser.add_argument("--device", default=None, help="设备ID；多设备库必须指定，报告不跨设备合并")

    parser.add_argument("--list-devices", action="store_true",
                        help="列出当日有数据的设备后退出")

    args = parser.parse_args()

    day = args.day or now_cst().strftime("%Y-%m-%d")
    conn = sqlite3.connect(args.db)

    if args.list_devices:
        devs = devices_of_day(conn, day)
        if devs:
            print(f"{day} 有数据的设备：")
            for d in devs:
                print(f"  - {d}")
        else:
            print(f"{day} 无设备数据")
        conn.close()
        return

    try:
        report(conn, day, args.verbose, device_id=args.device)
    except MultiDeviceError as e:
        print(f"错误：{e}", file=sys.stderr)
        print(f"提示：运行 python -m gacore.langTrack.report --list-devices "
              f"--day {day} 查看当日设备", file=sys.stderr)
        conn.close()
        raise SystemExit(2)

    conn.close()

if __name__ == "__main__":

    main()

