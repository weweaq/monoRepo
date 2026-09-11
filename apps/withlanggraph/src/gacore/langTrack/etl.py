"""langTrack ETL：把原始事件流清洗并加工成可分析的事实表。



产出三张表：

- sessions    前台会话（usage + session 事件拼接，含 app/起止/时长/activity）

- daily_stats 按天汇总（总时长/app 排行/通知统计/亮屏与锁屏次数）

- places      常驻点（位置网格聚类 + wifi/bt 佐证）



清洗规则（针对实测脏数据）：

- ts < 1e12 的事件丢弃（Android AccessibilityEvent 偶发 timeStamp=0）

- 系统 App 丢弃（launcher/systemui/输入法/自家 App 等，避免噪音污染排行）

- usage foreground_ms <= 0 丢弃

"""

from __future__ import annotations



import argparse

import datetime

import json

import os

import sqlite3

import time

from collections import defaultdict

from pathlib import Path


from gacore.langTrack.etl_config import load_etl_config
DB_PATH = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"



# 同设备重装前后 device_id 别名映射（data/device_aliases.json）：别名 → 主设备。

# app 端已改为基于 ANDROID_ID 的稳定标识，但历史数据里重装前的旧 device_id 仍存在，

# ETL 每次运行先把别名数据归并到主设备，避免同一台手机被劈成两半。

DEVICE_ALIASES_PATH = Path(__file__).resolve().parents[3] / "data" / "device_aliases.json"



# 与客户端 UsageRepository.SYSTEM_PACKAGES 对齐 + 实测新增噪音（42 个去重后）

SYSTEM_PACKAGES = {

    "android",

    "com.android.systemui",

    "com.android.launcher3",

    "com.android.launcher",  # OPPO/ColorOS 实际桌面包名（实测漏网）

    "com.oppo.launcher",

    "com.oplus.launcher",

    "com.oppo.safe",

    "com.coloros.safecenter",

    "com.google.android.inputmethod.latin",

    "com.sohu.inputmethod.sogou",

    "com.baidu.input",

    "com.iflytek.inputmethod",

    "com.android.settings",

    "com.oplus.settings",

    "com.coloros.phonemanager",

    "com.heytap.mcs",

    "com.oplus.battery",

    # 实测系统组件（OPPO/ColorOS/Android 平台），全部为噪音

    "com.android.permissioncontroller",

    "com.android.packageinstaller",

    "com.android.intentresolver",

    "com.android.photopicker",

    "com.android.wifi.dialog",

    "com.android.mms",

    "com.oplus.securitypermission",

    "com.oplus.wirelesssettings",

    "com.oplus.camera",

    "com.oplus.aiwriter",

    "com.oplus.appdetail",

    "com.oplus.melody",

    "com.oplus.screenrecorder",

    "com.oplus.notificationmanager",

    "com.oplus.viewtalk",

    "com.oplus.trafficmonitor",

    "com.oplus.safecenter",

    "com.oplus.linker",

    "com.coloros.digitalwellbeing",

    "com.coloros.gallery3d",

    "com.coloros.codebook",

    "com.coloros.familyguard",

    "com.heytap.browser",

    "com.heytap.market",

    "com.heytap.quicksearchbox",

}

# 自家 App 不进使用统计

OWN_PACKAGES = {"com.wei.checkapp"}


# ETL 事实表版本（B5 血缘）：B1–B8 任一逻辑变更时 bump。
ETL_VERSION = "1.0.0"


# Task 6 §3.2 坐标质量日表 DDL（只读聚合，v1/v2 共用；ETL 幂等重建）。
# 独立成常量：_write_daily_quality 写入前幂等补建——build_location_shadow 等
# 自建连接的库可能未执行过 etl._SCHEMA（P1 修复：旧库跑 --location-shadow 崩溃）。
# 常量只含单条 CREATE 语句（无 SQL 注释）：_write_daily_quality 用 conn.execute
# 补建——executescript 会隐式 COMMIT 挂起事务，破坏 v1 管线中途失败的整体回滚。
_DAILY_QUALITY_DDL = """CREATE TABLE IF NOT EXISTS daily_location_quality (
  day TEXT NOT NULL,
  device_id TEXT NOT NULL,
  points_total INTEGER NOT NULL,
  points_valid INTEGER NOT NULL,
  accuracy_known INTEGER NOT NULL,
  accuracy_le_50 INTEGER NOT NULL,
  accuracy_51_150 INTEGER NOT NULL,
  accuracy_gt_150 INTEGER NOT NULL,
  observed_half_hour_bins INTEGER NOT NULL,
  median_interval_sec REAL,
  providers_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
  PRIMARY KEY(day, device_id)
)"""


_SCHEMA = ("""

CREATE TABLE IF NOT EXISTS sessions (

  id INTEGER PRIMARY KEY AUTOINCREMENT,

  device_id TEXT NOT NULL,

  day TEXT NOT NULL,

  pkg TEXT NOT NULL,

  app TEXT NOT NULL,

  activity TEXT,

  start_ms INTEGER NOT NULL,

  end_ms INTEGER NOT NULL,

  duration_ms INTEGER NOT NULL

);

CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day);

CREATE INDEX IF NOT EXISTS idx_sessions_pkg ON sessions(pkg);



CREATE TABLE IF NOT EXISTS daily_stats (

  device_id TEXT NOT NULL DEFAULT 'unknown',

  day TEXT NOT NULL,

  total_screen_ms INTEGER NOT NULL DEFAULT 0,

  app_ranking_json TEXT,

  notification_count INTEGER NOT NULL DEFAULT 0,

  notification_clicked INTEGER NOT NULL DEFAULT 0,

  top_notification_apps_json TEXT,

  screen_on_count INTEGER NOT NULL DEFAULT 0,

  screen_off_count INTEGER NOT NULL DEFAULT 0,

  unlock_count INTEGER NOT NULL DEFAULT 0,

  switch_count INTEGER NOT NULL DEFAULT 0,

  location_count INTEGER NOT NULL DEFAULT 0,

  audio_clip_count INTEGER NOT NULL DEFAULT 0,

  sleep_start_hhmm TEXT,

  sleep_end_hhmm TEXT,

  sleep_duration_min INTEGER,

  time_app_json TEXT,

  updated_at TEXT DEFAULT (datetime('now', '+8 hours')),

  created_at TEXT,

  etl_version TEXT,

  PRIMARY KEY(device_id, day)

);



CREATE TABLE IF NOT EXISTS places (

  id INTEGER PRIMARY KEY AUTOINCREMENT,

  device_id TEXT NOT NULL,

  grid_key TEXT NOT NULL,

  lat REAL NOT NULL,

  lon REAL NOT NULL,

  label TEXT DEFAULT '未知',

  first_seen INTEGER,

  last_seen INTEGER,

  visit_count INTEGER NOT NULL DEFAULT 0,

  is_primary INTEGER NOT NULL DEFAULT 0,

  -- L2 语义落库字段（geocode 增量编码写入）

  address TEXT,

  poi TEXT,

  district TEXT,

  township TEXT,

  business_area TEXT,

  poi_type TEXT,

  -- P1-2 POI 三级语义（高德 type 拆"大类;中类;细类"）+ 名称硬信号 + 无POI兜底描述

  poi_l1 TEXT,

  poi_l2 TEXT,

  poi_l3 TEXT,

  poi_signal TEXT,

  poi_fallback TEXT,

  matched_level TEXT,

  behavior TEXT,

  geocoded_at INTEGER,

  -- L1 家/公司置信度候选（未确认前 label 保持中性表述，候选单独存）

  candidate_label TEXT,

  confidence_home REAL DEFAULT 0,

  confidence_work REAL DEFAULT 0,

  UNIQUE(device_id, grid_key)

);

CREATE INDEX IF NOT EXISTS idx_places_device ON places(device_id);



-- P1-3 新地点/异常事件（打破规律的点，作画像叙事节点）

CREATE TABLE IF NOT EXISTS anomalies (

  id INTEGER PRIMARY KEY AUTOINCREMENT,

  day TEXT NOT NULL,

  kind TEXT NOT NULL,

  device_id TEXT NOT NULL,

  grid_key TEXT,

  poi TEXT,

  detail TEXT,

  ts INTEGER,

  UNIQUE(day, kind, device_id, grid_key)

);

CREATE INDEX IF NOT EXISTS idx_anomalies_day ON anomalies(day);



CREATE TABLE IF NOT EXISTS stays (

  id INTEGER PRIMARY KEY AUTOINCREMENT,

  device_id TEXT NOT NULL,

  start_ts INTEGER NOT NULL,

  end_ts INTEGER NOT NULL,

  duration_ms INTEGER NOT NULL,

  center_lat REAL NOT NULL,

  center_lon REAL NOT NULL,

  min_lat REAL NOT NULL,

  min_lon REAL NOT NULL,

  max_lat REAL NOT NULL,

  max_lon REAL NOT NULL,

  n_points INTEGER NOT NULL DEFAULT 0,

  radius_m REAL NOT NULL DEFAULT 0,

  grid_key TEXT,

  day TEXT

);

CREATE INDEX IF NOT EXISTS idx_stays_device ON stays(device_id);

CREATE INDEX IF NOT EXISTS idx_stays_day ON stays(day);



-- L3 移动轨迹段：相邻停驻点之间的移动区间 + 高德路径规划补路

CREATE TABLE IF NOT EXISTS trips (

  id INTEGER PRIMARY KEY AUTOINCREMENT,

  device_id TEXT NOT NULL,

  start_ts INTEGER NOT NULL,

  end_ts INTEGER NOT NULL,

  duration_ms INTEGER NOT NULL,

  start_lat REAL NOT NULL,

  start_lon REAL NOT NULL,

  end_lat REAL NOT NULL,

  end_lon REAL NOT NULL,

  dist_m REAL NOT NULL,

  n_points INTEGER NOT NULL DEFAULT 0,

  day TEXT,

  polyline TEXT,

  route_key TEXT,

  route_mode TEXT,

  route_encoded_at INTEGER,

  -- Task 6 §3.3：trips 起终点保存 source 坐标，polyline 是高德 GCJ02，
  -- 两列显式标记坐标域，禁止混为同一坐标系解释
  endpoint_coord_system TEXT NOT NULL DEFAULT 'unknown',

  polyline_coord_system TEXT,

  UNIQUE(device_id, start_ts, end_ts)

);

CREATE INDEX IF NOT EXISTS idx_trips_device ON trips(device_id);

CREATE INDEX IF NOT EXISTS idx_trips_day ON trips(day);



-- P1 路过网格统计（通勤带）：trips.polyline 网格量化后的高频经过网格（纯本地，零配额）

CREATE TABLE IF NOT EXISTS route_grids (

  device_id TEXT NOT NULL,

  day TEXT NOT NULL,

  grid_lat REAL NOT NULL,

  grid_lon REAL NOT NULL,

  n_pass INTEGER NOT NULL DEFAULT 0,

  updated_at INTEGER,

  PRIMARY KEY(device_id, day, grid_lat, grid_lon)

);

CREATE INDEX IF NOT EXISTS idx_route_grids_grid ON route_grids(grid_lat, grid_lon);

-- A① 契约覆盖校验事实表：期望事件类型契约 vs 实际到达类型

CREATE TABLE IF NOT EXISTS contract_coverage (

  type          TEXT PRIMARY KEY,

  expected      INTEGER NOT NULL DEFAULT 1,

  consumed      TEXT    NOT NULL DEFAULT 'false',  -- true / partial / false

  desc          TEXT,

  arrived       INTEGER NOT NULL DEFAULT 0,        -- 是否在观察窗口内到达过

  event_count   INTEGER NOT NULL DEFAULT 0,

  last_seen_ts  INTEGER,

  status        TEXT    NOT NULL DEFAULT 'unknown', -- ok / stale / missing / unexpected

  created_at    TEXT DEFAULT (datetime('now','+8 hours')),

  updated_at    TEXT DEFAULT (datetime('now','+8 hours'))

);



-- P2 沿途 POI（网格级缓存）：每个网格最多一条周边 POI（around 100 次/日，低频克制）
CREATE TABLE IF NOT EXISTS grid_pois (
  grid_lat REAL NOT NULL,
  grid_lon REAL NOT NULL,
  name TEXT,
  type TEXT,
  distance TEXT,
  queried_at INTEGER,
  PRIMARY KEY(grid_lat, grid_lon)
);
""" + _DAILY_QUALITY_DDL + """
;
-- B5 ETL 运行血缘：每次 ETL 运行一条记录
CREATE TABLE IF NOT EXISTS etl_runs (
  run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  version      TEXT NOT NULL,
  started_at   TEXT,
  finished_at  TEXT,
  mode         TEXT NOT NULL DEFAULT 'full',
  device_id    TEXT,
  affected_days TEXT,
  status       TEXT NOT NULL DEFAULT 'running',
  git_rev      TEXT,
  rows_daily   INTEGER DEFAULT 0,
  rows_sessions INTEGER DEFAULT 0,
  rows_stays   INTEGER DEFAULT 0,
  created_at   TEXT DEFAULT (datetime('now','+8 hours')),
  updated_at   TEXT DEFAULT (datetime('now','+8 hours'))
);

-- B4 脏事件隔离：schema 校验未通过的事件落此处，不进 events 主流程、不崩 ETL
CREATE TABLE IF NOT EXISTS dirty_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  type        TEXT,
  raw         TEXT,
  reason      TEXT,
  arrived_at  TEXT DEFAULT (datetime('now','+8 hours')),
  created_at  TEXT DEFAULT (datetime('now','+8 hours')),
  updated_at  TEXT DEFAULT (datetime('now','+8 hours'))
);

-- B1 增量水位线（per-device）：上次已处理的最大事件 ts
CREATE TABLE IF NOT EXISTS etl_state (
  device_id    TEXT PRIMARY KEY,
  last_event_ts INTEGER,
  last_run_at  TEXT,
  updated_at   TEXT DEFAULT (datetime('now','+8 hours'))
);
""")


def clean_ts(ts: int) -> bool:

    """时间戳有效性：毫秒级且不为 0/负（过滤无障碍偶发脏数据）。"""

    return ts > 1_000_000_000_000





def load_events(conn: sqlite3.Connection) -> list[tuple[int, str, dict]]:

    """读取全部事件（device_id, ts, type, payload dict），跳过坏 ts。"""

    rows = conn.execute("SELECT device_id, ts, type, payload FROM events").fetchall()

    out = []

    for device_id, ts, type_, payload_raw in rows:

        if not clean_ts(ts):

            continue

        try:

            payload = json.loads(payload_raw)

        except json.JSONDecodeError:

            continue

        out.append((device_id, ts, type_, payload))

    return out





_TZ_CST = datetime.timezone(datetime.timedelta(hours=8))


def day_of(ts: int) -> str:
    """时间戳 → 日期字符串（显式东八区，不依赖服务器本地时区）。"""
    return datetime.datetime.fromtimestamp(
        ts / 1000, tz=_TZ_CST
    ).strftime("%Y-%m-%d")




def is_noise(pkg: str) -> bool:

    return pkg in SYSTEM_PACKAGES or pkg in OWN_PACKAGES





def build_sessions(events: list[tuple[int, str, int, dict]]) -> list[tuple]:

    """把 usage 事件拼成前台会话，用 app_switch/screen_off 做边界。



    简化模型：usage 事件本身自带 start(ts)/end(endMs)/duration，按 (device, app 连续)

    合并同 app 相邻片段，跨 app_switch 断开。不做复杂状态机，够用且稳。

    """

    # 按设备分组、按时间排序

    by_device: dict[str, list[tuple[int, str, dict]]] = defaultdict(list)

    for device_id, ts, type_, payload in events:

        by_device[device_id].append((ts, type_, payload))



    sessions = []

    for device_id, evs in by_device.items():

        evs.sort(key=lambda e: e[0])

        cur = None  # (pkg, app, activity, start_ms, end_ms, dur)

        for ts, type_, p in evs:

            if type_ == "usage":

                pkg = p.get("pkg", "")

                if is_noise(pkg):

                    continue

                app = p.get("app", pkg)

                fg_ms = p.get("foreground_ms", 0) or 0

                end_ms = p.get("endMs", ts)

                # 丢弃 ≤5 秒碎片会话（实测大量 0-5 秒的系统/切换噪音）

                if fg_ms < 5_000:

                    continue

                activity = p.get("activity", "")

                # 同 app 连续 → 合并（end 取 max，时长累加）

                if cur and cur[0] == pkg and abs(ts - cur[4]) < 2 * 60_000:

                    cur = (cur[0], cur[1], cur[2], cur[3], max(cur[4], end_ms), cur[5] + fg_ms)

                else:

                    if cur:

                        sessions.append((device_id, day_of(cur[3]), *cur))

                    cur = (pkg, app, activity, ts, end_ms, fg_ms)

            elif type_ == "session" and cur is not None:

                kind = p.get("kind")

                if kind == "app_switch" or kind == "screen_off":

                    sessions.append((device_id, day_of(cur[3]), *cur))

                    cur = None

        if cur:

            sessions.append((device_id, day_of(cur[3]), *cur))

    return sessions





def derive_screen_states(events) -> dict[tuple, dict]:
    """集中推导屏幕状态计数（on/off/unlock/switch），消除多处散算。

    统一口径：仅由 session 类事件的 kind 推导，按 (device_id, day) 聚合。
    不做跨天边界特殊处理（保持与历史 build_daily_stats 一致：每条事件按其 ts 的日历日计）。
    """
    counts: dict[tuple, dict] = defaultdict(lambda: {
        "screen_on": 0, "screen_off": 0, "unlock": 0, "switch": 0,
    })
    for device_id, ts, type_, p in events:
        if type_ != "session":
            continue
        kind = p.get("kind")
        d = day_of(ts)
        c = counts[(device_id, d)]
        if kind == "screen_on":
            c["screen_on"] += 1
        elif kind == "screen_off":
            c["screen_off"] += 1
        elif kind == "unlock":
            c["unlock"] += 1
        elif kind == "app_switch":
            c["switch"] += 1
    return counts


def build_daily_stats(events, sessions) -> list[tuple]:
    """按 (device_id, day) 汇总。"""
    stats: dict[tuple, dict] = defaultdict(lambda: {
        "total_screen_ms": 0, "app_usage": defaultdict(int), "notif_count": 0,
        "notif_clicked": 0, "notif_apps": defaultdict(int), "screen_on": 0,
        "screen_off": 0, "unlock": 0, "switch": 0, "location": 0, "audio_clip": 0,
    })
    for device_id, day, pkg, app, activity, start_ms, end_ms, dur in sessions:
        s = stats[(device_id, day)]
        s["total_screen_ms"] += dur
        s["app_usage"][app] += dur
    for device_id, ts, type_, p in events:
        d = day_of(ts)
        s = stats[(device_id, d)]
        if type_ == "notification":
            s["notif_count"] += 1
            if p.get("clicked"):
                s["notif_clicked"] += 1
            pkg = p.get("pkg", "unknown")
            if not is_noise(pkg):
                s["notif_apps"][p.get("app", pkg)] += 1
        elif type_ == "location":
            s["location"] += 1
        elif type_ == "audio_clip":
            s["audio_clip"] += 1
    # 屏幕状态统一由 derive_screen_states 推导，合并到每日汇总
    for (device_id, d), sc in derive_screen_states(events).items():
        s = stats[(device_id, d)]
        s["screen_on"] += sc["screen_on"]
        s["screen_off"] += sc["screen_off"]
        s["unlock"] += sc["unlock"]
        s["switch"] += sc["switch"]


    rows = []

    for (device_id, day), s in sorted(stats.items()):

        top_apps = sorted(s["app_usage"].items(), key=lambda kv: -kv[1])[:10]

        top_notif = sorted(s["notif_apps"].items(), key=lambda kv: -kv[1])[:5]

        rows.append((

            device_id, day, s["total_screen_ms"],

            json.dumps([{"app": a, "ms": m} for a, m in top_apps], ensure_ascii=False),

            s["notif_count"], s["notif_clicked"],

            json.dumps([{"app": a, "n": n} for a, n in top_notif], ensure_ascii=False),

            s["screen_on"], s["screen_off"], s["unlock"], s["switch"],

            s["location"], s["audio_clip"],

        ))

    return rows





# ---------------------------------------------------------------------------

# L1 停驻点检测（stays）

# ---------------------------------------------------------------------------



_EARTH_R = 6371000.0





def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:

    """两点球面距离（米）。"""

    import math

    p1 = math.radians(lat1)

    p2 = math.radians(lat2)

    dp = math.radians(lat2 - lat1)

    dl = math.radians(lon2 - lon1)

    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2

    return 2 * _EARTH_R * math.asin(math.sqrt(a))





def _median_center(points: list[tuple[float, float]]) -> tuple[float, float]:

    """中位数中心（对 GPS 抖动比均值更鲁棒）。points: [(lat, lon)]。"""

    n = len(points)

    lats = sorted(p[0] for p in points)

    lons = sorted(p[1] for p in points)



    def med(a: list) -> float:

        m = n // 2

        return a[m] if n % 2 else (a[m - 1] + a[m]) / 2.0



    return med(lats), med(lons)





# 停驻检测阈值（可配分档）：家/公司用大圈，密集商圈用小圈。

STAY_LARGE_RADIUS_M = 120.0   # 大圈：家/公司/大空间（方案 100m+，略放宽抗抖动）

STAY_SMALL_RADIUS_M = 60.0    # 小圈：密集商圈参考值（作为停留覆盖半径的分档标记）

STAY_MIN_DURATION_MS = 10 * 60 * 1000   # 停留最短时长：连续 10 分钟

STAY_MERGE_GAP_MS = 5 * 60 * 1000       # 边界合并：相邻停留间隔 < 5 分钟视为同一段

STAY_MERGE_RADIUS_M = 150.0             # 边界合并：相邻段中心距 < 150m 才合并（防通勤过渡段误并入）

STAY_MAX_JUMP_M = 500.0                 # GPS 漂移尖刺：相对上一点突跳 > 500m

STAY_MAX_SPEED_MPS = 40.0               # 漂移尖刺：瞬时速度 > 40m/s（144km/h）





def build_stays(

    points,

    large_radius_m: float = STAY_LARGE_RADIUS_M,

    small_radius_m: float = STAY_SMALL_RADIUS_M,

    min_stay_ms: int = STAY_MIN_DURATION_MS,

    merge_gap_ms: int = STAY_MERGE_GAP_MS,

    merge_radius_m: float = STAY_MERGE_RADIUS_M,

    max_jump_m: float = STAY_MAX_JUMP_M,

    max_speed_mps: float = STAY_MAX_SPEED_MPS,

) -> list[tuple]:

    """L1 停驻点检测：滑动窗口 + 中位数中心。

    输入 points 为 location_facts.location_points 产物（LocationPoint 列表，
    Task 6 起全链路共用解析：非法坐标已在解析层拒绝，本函数不再重复校验）。



    - 连续停留：新点与当前停留锚点（前 3 点中位数，固定不漂移）距离 <= 大圈半径 → 归入停留。

    - 漂移剔除：瞬时速度 > max_speed 且突跳 > max_jump 的 GPS 飞点直接丢弃；

      远离锚点但仅 1 点的"尖刺弹回"先挂起，下一点回到锚点则判为尖刺丢弃。

    - 边界合并：相邻两段停留 间隔 < merge_gap_ms 且 中心距 < merge_radius_m → 合并为同一段

      （双条件：移动中采样间隔通常 <5min，仅按时间合并且会把通勤过渡段误并入停留段）。

    - 阈值分档：检测统一用大圈（家/公司不劈碎）；停留的实际覆盖半径 <= 小圈

      则记为小圈（商圈/密集区），供画像区分空间粒度。阈值均可配置。

    - 输出元组：(device_id, start_ts, end_ts, duration_ms, center_lat, center_lon,

      min_lat, min_lon, max_lat, max_lon, n_points, radius_m, grid_key, day)

    """

    # 按设备分组、按时间排序

    by_device: dict[str, list[tuple[int, float, float]]] = defaultdict(list)

    for p in points:

        by_device[p.device_id].append((p.ts, p.lat, p.lon))

    for evs in by_device.values():

        evs.sort(key=lambda e: e[0])



    stays: list[tuple] = []

    for device_id, evs in by_device.items():

        if not evs:

            continue

        # ---- 粗停留段检测（固定锚点，防中位数中心随点加入漂移导致半径膨胀）----

        raw_segs: list[list[tuple[int, float, float]]] = []

        cur: list[tuple[int, float, float]] = [evs[0]]

        anchor = (evs[0][1], evs[0][2])  # 锚点：前 3 点中位数定锚后固定

        pending: tuple[int, float, float] | None = None

        prev = evs[0]

        for e in evs[1:]:

            ts, lat, lon = e

            dt = (ts - prev[0]) / 1000.0

            dist_prev = _haversine(prev[1], prev[2], lat, lon)

            speed = dist_prev / dt if dt > 0 else 0.0

            # 超速 + 突跳 → GPS 飞点，直接丢弃（不断开停留）

            if speed > max_speed_mps and dist_prev > max_jump_m:

                prev = e

                continue

            dist_anchor = _haversine(anchor[0], anchor[1], lat, lon)

            if dist_anchor <= large_radius_m:

                # 回到停留内；若有挂起点说明那是尖刺，丢弃

                pending = None

                cur.append(e)

                # 仅前 3 点用中位数重定锚（抗首点抖动），之后锚点固定不漂移

                if len(cur) <= 3:

                    anchor = _median_center([(x[1], x[2]) for x in cur])

            else:

                if pending is None:

                    # 首个远离点：挂起观察（可能是尖刺弹回，也可能是真离开）

                    pending = e

                else:

                    # 连续两个远离锚点 → 确认离开，切段；新段以挂起点为锚

                    raw_segs.append(cur)

                    cur = [pending, e]

                    anchor = (pending[1], pending[2])

                    pending = None

            prev = e

        if pending is not None:

            raw_segs.append(cur)

            cur = [pending]

        if cur:

            raw_segs.append(cur)



        # ---- 边界合并（间隔 < merge_gap 且 相邻段中心距离 < merge_radius → 同一段） ----

        # 双条件：仅按时间间隔合并且移动中采样点间隔通常 <5min，会把通勤过渡段

        # 误并入停留段导致半径膨胀（实测家段被拉到 587m）；空间相近才合并。

        segs: list[list[tuple[int, float, float]]] = []

        for seg in raw_segs:

            if segs and (seg[0][0] - segs[-1][-1][0]) < merge_gap_ms:

                c1 = _median_center([(x[1], x[2]) for x in segs[-1]])

                c2 = _median_center([(x[1], x[2]) for x in seg])

                if _haversine(c1[0], c1[1], c2[0], c2[1]) <= merge_radius_m:

                    segs[-1].extend(seg)

                    continue

            segs.append(list(seg))



        # ---- 生成停留记录（时长达标才保留） ----

        for seg in segs:

            start_ts = seg[0][0]

            end_ts = seg[-1][0]

            duration = end_ts - start_ts

            if duration < min_stay_ms:

                continue

            pts = [(x[1], x[2]) for x in seg]

            clat, clon = _median_center(pts)

            radius = max(_haversine(clat, clon, la, lo) for la, lo in pts)

            # 阈值分档：覆盖半径 <= 小圈 → 记小圈（商圈/密集区），否则大圈（家/公司）

            radius_m = min(radius, small_radius_m) if radius <= small_radius_m else max(radius, large_radius_m)

            lats = [la for la, _ in pts]

            lons = [lo for _, lo in pts]

            gk = f"{round(clat * 1000) / 1000:.3f},{round(clon * 1000) / 1000:.3f}"

            stays.append((

                device_id, start_ts, end_ts, duration,

                clat, clon, min(lats), min(lons), max(lats), max(lons),

                len(pts), round(radius_m, 1), gk, day_of(start_ts),

            ))

    stays.sort(key=lambda s: (s[0], s[1]))

    return stays





def build_places(points) -> list[tuple]:

    """位置网格聚类：0.001° (~110m) 网格，聚合停留次数与时间范围。

    输入 points 为 location_facts.location_points 产物（Task 6 共用解析）。

    修复：device_id 按事件实际值填充（不再写死空串），多设备按 (device_id, grid) 隔离。

    """

    grid: dict[tuple, dict] = {}

    for p in points:

        lat, lon = p.lat, p.lon

        gk = (round(lat * 1000) / 1000, round(lon * 1000) / 1000)

        cell = grid.setdefault((p.device_id, gk), {"lat": lat, "lon": lon, "first": p.ts, "last": p.ts, "n": 0})

        cell["first"] = min(cell["first"], p.ts)

        cell["last"] = max(cell["last"], p.ts)

        cell["n"] += 1

    rows = []

    for (device_id, (glat, glon)), c in grid.items():

        rows.append((device_id, f"{glat:.3f},{glon:.3f}", c["lat"], c["lon"],

                     "未知", c["first"], c["last"], c["n"]))

    return rows





def infer_home_work_candidates(conn: sqlite3.Connection) -> int:

    """L1 家/公司置信度推断（候选制 + 双榜并行 + 已确认点置信度回填）。



    - 家：凌晨 00:00-05:00 停留天数 >=3 → 高置信（home 榜）。

    - 公司：工作日白天 09:00-18:00 高频停留（>=15 次）→ 高置信（work 榜）。

    - 双榜并行：评估网格 = 凌晨停留榜 ∪ 工作日白天高频榜；纯白天高频、
      凌晨无停留的办公网格也能进入公司候选（修复验收盲区③）。

    - 已确认点（label 家/公司）不再跳过，同样计算并回填 confidence_home /
      confidence_work，candidate_label 记为其确认标签（修复验收盲区②）；
      未确认点 candidate_label 记推断候选，label 保持中性由用户确认。

    设备隔离（Task 5c）：全部统计与回填按 (device_id, grid_key) 显式键——
    两设备共处同一网格时各自独立累计/回填，他设备的已确认标签不串扰。

    """

    home_days: dict[tuple[str, str], set[str]] = defaultdict(set)

    work_count: dict[tuple[str, str], int] = defaultdict(int)

    rows = conn.execute(

        "SELECT device_id, ts, payload FROM events WHERE type='location'"

    ).fetchall()

    import datetime

    from gacore.langTrack import location_facts as lf

    for device_id, ts, raw in rows:

        try:

            p = json.loads(raw)

        except (json.JSONDecodeError, TypeError):

            continue

        # Task 6：共用唯一解析入口（非法坐标/ (0,0) 与位置事实同规则拒绝）
        pt = lf.parse_location_point(device_id, ts, p)
        if pt is None:
            continue
        gk = lf.grid_key_of(pt.lat, pt.lon)
        dt = datetime.datetime.fromtimestamp(ts / 1000, tz=_TZ_CST)
        hour = dt.hour
        if hour < 5:
            home_days[(device_id, gk)].add(dt.strftime("%Y-%m-%d"))
        if 9 <= hour < 18 and dt.weekday() < 5:

            work_count[(device_id, gk)] += 1



    # 已确认标签（label 家/公司）按 (device_id, grid_key) 索引，用于回填 candidate_label

    confirmed: dict[tuple[str, str], str] = {}

    for dev, gk, lab in conn.execute(

        "SELECT device_id, grid_key, label FROM places WHERE label IN ('家','公司')"

    ):

        confirmed[(dev, gk)] = lab



    WORK_THRESHOLD = 15  # 工作日白天高频阈值：>=15 次进入公司候选评估

    # 评估 (device, grid) = 凌晨停留榜 ∪ 工作日白天高频榜 ∪ 已确认 label。

    # 已确认点强制纳入，保证置信度无条件回填（即使不在任何高频榜）。

    grids = set(home_days) | {k for k, n in work_count.items() if n >= WORK_THRESHOLD} | set(confirmed)



    n_updated = 0

    for dev, gk in grids:

        home_conf = min(1.0, len(home_days.get((dev, gk), ())) / 3.0)

        work_conf = min(1.0, work_count.get((dev, gk), 0) / WORK_THRESHOLD)

        candidate = None

        if home_conf >= 0.67 and home_conf > work_conf:

            candidate = "家"

        elif work_conf >= 0.67 and work_conf > home_conf:

            candidate = "公司"

        # 已确认点回填确认标签；未确认点写入推断候选

        cand_final = confirmed.get((dev, gk), candidate)

        cur = conn.execute(

            "UPDATE places SET candidate_label=?, confidence_home=?, confidence_work=? "

            "WHERE device_id=? AND grid_key=?",

            (cand_final, round(home_conf, 2), round(work_conf, 2), dev, gk),

        )

        n_updated += cur.rowcount

    return n_updated





def detect_anomalies(
    conn: sqlite3.Connection, lookback_days: int = 7, now_ms: int | None = None
) -> int:
    """P1-3 新地点/异常事件探测：识别打破规律的点，写入 anomalies 表。

    三类异常（作画像叙事节点）：
    - new_place      首次到访新地点：近 lookback_days 天内 first_seen 且原始定位点数 <= 3
                     （v1 visit_count 即点数；v2 point_count，visit_count 是 stay 段数），
                     且非已确认家/公司（如新出现的医院、商场、陌生住宅区）。
    - late_night_out 深夜/凌晨在外：停驻点开始时间落在夜间窗口且不在本设备的家。
    - off_schedule   工作日白天缺席公司：当天有停驻但 13:00 无公司停留
                     （按覆盖日 × device_id 分组评估——跨天 stay 计入其覆盖的
                     每个自然日，Task 12；设备互不合并）。

    v1/v2 双读（Task 5c，显式 device_id/place_id）：家/公司集合按
    (device_id, 地点键) 匹配——v1 键为 grid_key，v2（user_version>=2）键为
    place_id（stay 的 grid_key 可能只是 place 的成员网格而非代表网格）；
    他设备的家/公司不算自己的，name 查询同样带 device_id。
    夜间边界与 new_place 回看窗口走 etl_config 外置配置。
    """
    cfg = load_etl_config()["anomaly"]
    night_start_h = cfg["night_start_h"]
    night_end_h = cfg["night_end_h"]
    lookback_days = cfg.get("new_place_lookback_days", lookback_days)

    v2 = conn.execute("PRAGMA user_version").fetchone()[0] >= 2
    key_col = "place_id" if v2 else "grid_key"

    conn.row_factory = sqlite3.Row

    conn.execute("DELETE FROM anomalies")

    now_ms = int(time.time() * 1000) if now_ms is None else now_ms

    if v2:
        home = {(r[0], r[1]) for r in conn.execute(
            "SELECT device_id, place_id FROM places WHERE label='家'")}
        work = {(r[0], r[1]) for r in conn.execute(
            "SELECT device_id, place_id FROM places WHERE label='公司'")}
    else:
        home = {(r[0], r[1]) for r in conn.execute(
            "SELECT device_id, grid_key FROM places WHERE label='家'")}
        work = {(r[0], r[1]) for r in conn.execute(
            "SELECT device_id, grid_key FROM places WHERE label='公司'")}

    def place_name(device_id: str, key: str | None) -> str:
        if not key:
            return ""
        r = conn.execute(
            f"SELECT poi, poi_fallback FROM places WHERE device_id=? AND {key_col}=? LIMIT 1",
            (device_id, key),
        ).fetchone()
        if r:
            return r["poi"] or r["poi_fallback"] or key
        return key

    rows: list[tuple] = []

    # 1) 新地点（v1/v2 places 行均含 first_seen/visit_count/poi 列；v2 附带 place_id）
    # 阈值对齐 v1 语义"原始定位点数≤3"：v1 visit_count 即点数；v2 visit_count 是
    # stay 段数（≤3 可含三次到访），须改用 point_count（成员网格原始点数）。
    for r in conn.execute("SELECT * FROM places"):
        if r["label"] in ("家", "公司"):
            continue
        n_points = r["point_count"] if v2 else r["visit_count"]
        if r["first_seen"] and r["first_seen"] >= now_ms - lookback_days * 86400000 and n_points <= 3:
            day = datetime.datetime.fromtimestamp(r["first_seen"] / 1000, tz=_TZ_CST).strftime("%Y-%m-%d")
            name = r["poi"] or r["poi_fallback"] or "未知地点"  # 不输出坐标网格
            # Task 10 证据边界：detail 不写动机式"首次到访"，且口径按版本分档：
            # v2 用 visit_count（即 stay 段数 / visit_episodes 口径）输出"停留 N 段"；
            # v1 的 visit_count 与 point_count 同源（原始定位点数），没有段数概念，
            # 只能如实写"记录到 N 点"，严禁把点数伪称成段数。
            if v2:
                detail = f"新地点停留：{name}（停留 {int(r['visit_count'])} 段）"
            else:
                detail = f"新地点停留：{name}（记录到 {int(r['visit_count'])} 点）"
            if v2:
                rows.append((day, "new_place", r["device_id"], r["place_id"], r["grid_key"], name, detail, r["first_seen"]))
            else:
                rows.append((day, "new_place", r["device_id"], r["grid_key"], name, detail, r["first_seen"]))

    # 2) 深夜/凌晨在外（夜间窗口停留且不在本设备的家）
    for r in conn.execute("SELECT * FROM stays"):
        h = datetime.datetime.fromtimestamp(r["start_ts"] / 1000, tz=_TZ_CST).hour
        if not (h >= night_start_h or h < night_end_h):
            continue
        key = r["place_id"] if v2 else r["grid_key"]
        if (r["device_id"], key) in home:
            continue
        day = r["day"]
        dur = (r["end_ts"] - r["start_ts"]) / 60000.0
        name = place_name(r["device_id"], key)
        detail = f"深夜在外停留 {dur:.0f} 分钟：{name}"
        if v2:
            rows.append((day, "late_night_out", r["device_id"], r["place_id"], r["grid_key"], name, detail, r["start_ts"]))
        else:
            rows.append((day, "late_night_out", r["device_id"], r["grid_key"], name, detail, r["start_ts"]))

    # 3) 工作日白天缺席公司（当天有停驻但 13:00 无公司停留；按设备分组）
    for day, device_id, day_stays in _group_stays_by_day(conn):
        if datetime.date.fromisoformat(day).weekday() >= 5:
            continue
        if not day_stays:
            continue
        # 正午 13:00 作为"白天在公司"的代表时刻：停留段覆盖 13:00 且落在公司
        # （不能用 start_ts 落在窗口内判断——公司停留段常开始于 08:40/09:47，会被误判缺席）
        noon = int(datetime.datetime.fromisoformat(f"{day} 13:00").replace(tzinfo=_TZ_CST).timestamp() * 1000)
        if noon > now_ms:
            # 进行中日且正午未到：无法判定"未到公司"，评估只会误报（Task 12b）
            continue
        key_idx = 3 if v2 else 2  # stay 条目 (start_ts, end_ts, grid_key, place_id)
        in_office = any(
            (device_id, s[key_idx]) in work and s[0] <= noon <= s[1]
            for s in day_stays
        )
        if not in_office:
            detail = "工作日白天未到公司（正午 13:00 无公司停留）"
            if v2:
                rows.append((day, "off_schedule", device_id, None, "", "", detail, day_stays[0][0]))
            else:
                rows.append((day, "off_schedule", device_id, "", "", detail, day_stays[0][0]))

    if v2:
        conn.executemany(
            "INSERT OR IGNORE INTO anomalies(day, kind, device_id, place_id, grid_key, poi, detail, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    else:
        conn.executemany(
            "INSERT OR IGNORE INTO anomalies(day, kind, device_id, grid_key, poi, detail, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )

    conn.commit()

    return len(rows)





def detect_route_changes(conn: sqlite3.Connection) -> int:

    """L3 路线变化事件：同日相邻出行的 route_key 不同 → route_change（复用 anomalies 叙事表）。



    前置：trips 已完成高德补路（route_key 非空）。anomalies 唯一键含 device_id

    （v1 迁移后 UNIQUE(day, kind, device_id, grid_key)；v2 为表达式唯一索引），

    故 grid_key 存 'rc:'+route_key[:8] 承载指纹保唯一。返回新增事件数。

    """

    conn.row_factory = sqlite3.Row

    rows = conn.execute(

        "SELECT device_id, day, start_ts, start_lat, start_lon, end_lat, end_lon, route_key "

        "FROM trips WHERE route_key IS NOT NULL AND route_key != '' "

        "ORDER BY device_id, start_ts"

    ).fetchall()



    def hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:

        import math

        r = 6371000.0

        p1, p2 = math.radians(lat1), math.radians(lat2)

        dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)

        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2

        return 2 * r * math.asin(math.sqrt(a))



    by_dev: dict[str, list] = defaultdict(list)

    for r in rows:

        by_dev[r["device_id"]].append(r)

    changes: list[tuple] = []

    PAIR_DIST = 400.0  # 往返对判距：A起点≈B终点 且 A终点≈B起点

    for _device_id, trips in by_dev.items():

        for i in range(1, len(trips)):

            prev, cur = trips[i - 1], trips[i]

            if prev["day"] != cur["day"] or prev["route_key"] == cur["route_key"]:

                continue

            # 往返对（去程+回程）：同一通勤路往返不报"路线变化"

            round_trip = (

                hav(prev["start_lat"], prev["start_lon"], cur["end_lat"], cur["end_lon"]) < PAIR_DIST

                and hav(prev["end_lat"], prev["end_lon"], cur["start_lat"], cur["start_lon"]) < PAIR_DIST

            )

            if round_trip:

                continue

            gk = "rc:" + (cur["route_key"][:8] or "?")

            detail = (

                f"通勤路线变化（同日相邻出行指纹不同）: "

                f"{prev['start_lat']:.4f},{prev['start_lon']:.4f} → "

                f"{cur['end_lat']:.4f},{cur['end_lon']:.4f}"

            )

            changes.append((

                cur["day"], "route_change", _device_id, gk, "", detail, cur["start_ts"],

            ))

    conn.executemany(

        "INSERT OR IGNORE INTO anomalies(day, kind, device_id, grid_key, poi, detail, ts) "

        "VALUES (?,?,?,?,?,?,?)",

        changes,

    )

    conn.commit()

    return len(changes)





def _group_stays_by_day(conn: sqlite3.Connection) -> list[tuple[str, str, list[tuple]]]:
    """按覆盖日（时间相交）+ device_id 分组停驻点（Task 5c 设备隔离：设备互不合并）。

    跨天 stay 出现在其覆盖的每个 CST 自然日（半开区间 [当日 00:00, 次日 00:00)），
    而非仅 stays.day 记录的起始日——否则 off_schedule 在后续天查正午时会漏掉
    跨天 stay，与 fact_card 时间线的窗口裁剪口径自相矛盾（Task 12）。

    返回 [(day, device_id, [(start_ts, end_ts, grid_key, place_id), ...])]；
    v1 stays 无 place_id 列时以 NULL 占位，条目结构两版本一致。
    """
    conn.row_factory = sqlite3.Row  # 按名取列，不依赖调用方预设
    has_pid = "place_id" in {r[1] for r in conn.execute("PRAGMA table_info(stays)")}
    pid_sel = "place_id" if has_pid else "NULL AS place_id"
    by_key: dict[tuple[str, str], list] = defaultdict(list)
    for r in conn.execute(
        f"SELECT day, device_id, start_ts, end_ts, grid_key, {pid_sel} FROM stays"
    ):
        entry = (r["start_ts"], r["end_ts"], r["grid_key"], r["place_id"])
        day0 = datetime.datetime.fromtimestamp(r["start_ts"] / 1000, tz=_TZ_CST).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        while True:
            day_start_ms = int(day0.timestamp() * 1000)
            if r["end_ts"] <= day_start_ms:
                break
            if r["start_ts"] < day_start_ms + 86400000:
                by_key[(day0.date().isoformat(), r["device_id"])].append(entry)
            day0 += datetime.timedelta(days=1)
    return [(day, dev, stays) for (day, dev), stays in sorted(by_key.items())]





def migrate_places(conn: sqlite3.Connection) -> None:

    """places 表迁移：补语义/候选列；修复旧库 device_id='' 归并到实际设备。"""

    cols = {r[1] for r in conn.execute("PRAGMA table_info(places)")}

    for col, ddl in {

        "address": "TEXT",

        "poi": "TEXT",

        "district": "TEXT",

        "township": "TEXT",

        "business_area": "TEXT",

        "poi_type": "TEXT",

        "poi_l1": "TEXT",

        "poi_l2": "TEXT",

        "poi_l3": "TEXT",

        "poi_signal": "TEXT",

        "poi_fallback": "TEXT",

        "matched_level": "TEXT",

        "behavior": "TEXT",

        "geocoded_at": "INTEGER",

        "candidate_label": "TEXT",

        "confidence_home": "REAL DEFAULT 0",

        "confidence_work": "REAL DEFAULT 0",

    }.items():

        if col not in cols:

            conn.execute(f"ALTER TABLE places ADD COLUMN {col} {ddl}")



    # 修复旧库空 device_id：归并到 events 中实际设备（多设备时取最新活跃）

    empty = conn.execute("SELECT id, grid_key, lat, lon, label, first_seen, last_seen, visit_count, is_primary "

                         "FROM places WHERE device_id='' OR device_id IS NULL").fetchall()

    if empty:

        real = conn.execute(

            "SELECT device_id FROM events WHERE type='location' ORDER BY ts DESC LIMIT 1"

        ).fetchone()

        target = real[0] if real else "unknown"

        for (pid, gk, lat, lon, label, first, last, vc, isp) in empty:

            exists = conn.execute(

                "SELECT id FROM places WHERE device_id=? AND grid_key=?", (target, gk)

            ).fetchone()

            if exists:

                conn.execute(

                    "UPDATE places SET first_seen=MIN(first_seen,?), last_seen=MAX(last_seen,?), "

                    "visit_count=visit_count+?, is_primary=MAX(is_primary,?) WHERE id=?",

                    (first, last, vc, isp, exists[0]),

                )

                conn.execute("DELETE FROM places WHERE id=?", (pid,))

            else:

                conn.execute("UPDATE places SET device_id=? WHERE id=?", (target, pid))

        print(f"[etl] 修复旧库空 device_id 常驻点: {len(empty)} 条 → {target}")





def _load_device_aliases() -> dict[str, str]:

    """读取设备别名映射（别名 → 主设备）。"""

    if not DEVICE_ALIASES_PATH.exists():

        return {}

    try:

        data = json.loads(DEVICE_ALIASES_PATH.read_text(encoding="utf-8"))

    except (json.JSONDecodeError, UnicodeDecodeError, OSError):

        return {}

    return {str(k).strip(): str(v).strip() for k, v in (data or {}).items() if k and v}





def canonical_device_id(device_id: str) -> str:
    """/ingest 层别名归一：alias → 主设备（Task 12b）。

    原始层入库即写规范 device_id，消除"两次 ETL 之间新事件带别名"的窗口
    （期间 report/devices 会误判多设备）；存量历史行仍由 merge_device_aliases
    在 ETL 中改写。
    """
    return _load_device_aliases().get(device_id, device_id)


def merge_device_aliases(conn: sqlite3.Connection) -> int:

    """同设备重装前后 device_id 归一：把别名设备的数据并入主设备。



    - events：别名 → 主设备（device_id 直接改写）

    - places：同一 (device, grid) 合并统计（首末时间取并集、访问数累加、

      非"未知"label 优先、语义字段主设备缺失时补别名），别名行删除

    - devices：主设备时间范围吸收别名，删除别名行

    返回归并的别名设备数。必须在 load_events 之前调用（build 系列以 device_id 分组）。

    """

    aliases = _load_device_aliases()

    if not aliases:

        return 0

    merged = 0

    for alias, primary in aliases.items():

        if alias == primary:

            continue

        # events 归一

        cur = conn.execute(

            "UPDATE events SET device_id=? WHERE device_id=?", (primary, alias)

        )

        # places 归一（先合并统计再删别名行）

        alias_rows = conn.execute(

            "SELECT id, grid_key, lat, lon, label, first_seen, last_seen, visit_count, "

            "is_primary, address, poi, poi_type, behavior, matched_level "

            "FROM places WHERE device_id=?", (alias,)

        ).fetchall()

        for r in alias_rows:

            # 元组索引: 0=id 1=grid_key 2=lat 3=lon 4=label 5=first_seen

            #           6=last_seen 7=visit_count 8=is_primary 9=address 10=poi

            r_id, r_gk, r_lat, r_lon, r_label, r_first, r_last, r_vc, r_isp = r[:9]

            r_addr, r_poi = r[9], r[10]

            exists = conn.execute(

                "SELECT id, label, address, poi FROM places "

                "WHERE device_id=? AND grid_key=?", (primary, r_gk)

            ).fetchone()

            if exists:

                pid, plabel, paddr, ppoi = exists

                label = plabel if plabel and plabel != "未知" else (r_label or "未知")

                conn.execute(

                    "UPDATE places SET first_seen=MIN(first_seen,?), last_seen=MAX(last_seen,?), "

                    "visit_count=visit_count+?, is_primary=MAX(is_primary,?), label=?, "

                    "address=COALESCE(NULLIF(?, ''), address), "

                    "poi=COALESCE(NULLIF(?, ''), poi) WHERE id=?",

                    (r_first, r_last, r_vc, r_isp, label, r_addr, r_poi, pid),

                )

                conn.execute("DELETE FROM places WHERE id=?", (r_id,))

            else:

                conn.execute("UPDATE places SET device_id=? WHERE id=?", (primary, r_id))

        # devices 时间范围吸收 + 删除别名行

        conn.execute(

            "UPDATE devices SET first_seen=MIN(first_seen, (SELECT first_seen FROM devices WHERE device_id=?)), "

            "last_seen=MAX(last_seen, (SELECT last_seen FROM devices WHERE device_id=?)), "

            "updated_at=datetime('now','+8 hours') WHERE device_id=?",

            (alias, alias, primary),

        )

        conn.execute("DELETE FROM devices WHERE device_id=?", (alias,))

        merged += 1

        print(f"[etl] 设备归一: {alias} → {primary} (events {cur.rowcount} 条, places {len(alias_rows)} 条)")

    conn.commit()

    return merged





def build_contract_coverage(conn: sqlite3.Connection) -> int:
    """A① 契约覆盖校验：比对期望事件类型契约(contract.EXPECTED_EVENT_TYPES)与
    events 实际到达类型，产出 contract_coverage 事实表（全量重建，幂等）。

    - 期望类型不在 events 中 → missing
    - 期望类型到达但 last_seen 超过 STALE_DAYS 天 → stale
    - 期望类型近期到达 → ok
    - events 中出现但不在契约中的类型 → unexpected（expected=0）
    """
    from gacore.langTrack.contract import EXPECTED_EVENT_TYPES, STALE_DAYS
    from gacore.jsonl_logger import get_logger

    log = get_logger("langTrack.etl")

    # 实际到达类型：type -> (count, max_ts)
    actual: dict[str, tuple[int, int]] = {}
    try:
        rows = conn.execute(
            "SELECT type, COUNT(*), MAX(ts) FROM events GROUP BY type"
        ).fetchall()
    except sqlite3.OperationalError as e:
        # events 表不存在（极端情况）时不阻塞 ETL
        log.warning("contract_coverage 跳过: events 表不可读", error_type=type(e).__name__, error=str(e))
        return 0
    for type_, cnt, max_ts in rows:
        if type_ is not None:
            actual[type_] = (cnt, max_ts)

    now_ms = int(time.time() * 1000)
    stale_ms = STALE_DAYS * 86400000

    out: list[tuple] = []
    for type_, meta in EXPECTED_EVENT_TYPES.items():
        desc = meta.get("desc", "")
        consumed = meta.get("consumed", "false")
        if type_ not in actual:
            out.append((type_, 1, consumed, desc, 0, 0, None, "missing"))
        else:
            cnt, last_seen = actual[type_]
            status = "stale" if (now_ms - last_seen) > stale_ms else "ok"
            out.append((type_, 1, consumed, desc, 1, cnt, last_seen, status))

    # 实际到达但不在契约中的类型 → unexpected
    for type_, (cnt, last_seen) in actual.items():
        if type_ not in EXPECTED_EVENT_TYPES:
            out.append((type_, 0, "false", None, 1, cnt, last_seen, "unexpected"))

    conn.execute("DELETE FROM contract_coverage")
    conn.executemany(
        "INSERT OR REPLACE INTO contract_coverage"
        "(type, expected, consumed, desc, arrived, event_count, last_seen_ts, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        out,
    )
    conn.commit()
    log.info("contract_coverage 重建完成", type_count=len(out))
    return len(out)


def purge_dirty(db_path: Path) -> None:

    """删除 events 表中的异常数据：ts 脏数据 + payload 非 JSON。幂等，可重复执行。"""

    conn = sqlite3.connect(db_path)

    c1 = conn.execute("DELETE FROM events WHERE ts < 1000000000000")

    # payload 非 JSON 的（防御）

    rows = conn.execute("SELECT id, payload FROM events").fetchall()

    bad_ids = []

    for eid, raw in rows:

        try:

            json.loads(raw)

        except (json.JSONDecodeError, TypeError):

            bad_ids.append(eid)

    if bad_ids:

        conn.executemany("DELETE FROM events WHERE id=?", [(i,) for i in bad_ids])

    conn.commit()

    print(f"[purge] 删除 ts 脏数据 {c1.rowcount} 条, 非 JSON payload {len(bad_ids)} 条")

    conn.close()





# P2 沿途 POI 每轮 ETL 查询网格上限（around 免费配额仅 100 次/日，克制）

_POI_MAX_PER_RUN = int(os.environ.get("LANGTRACK_POI_MAX_PER_RUN", "5"))





def _now_cst(conn: sqlite3.Connection) -> str:
    """东八区当前时间字符串（与 schema 默认值格式一致）。"""
    return conn.execute("SELECT datetime('now','+8 hours')").fetchone()[0]


def _migrate_fact_tables(conn: sqlite3.Connection) -> None:
    """PRAGMA 守卫的事实表迁移：补齐 device_id / created_at / updated_at / etl_version。

    位置事实 v2 激活后（user_version>=2）places/stays/trips/anomalies/grid_pois/
    route_grids 结构冻结（无 etl_version 列），v1 迁移不得触碰，仅处理 sessions/daily_stats。
    """

    # daily_stats：SQLite 不支持 ALTER PRIMARY KEY，需重建表修正主键为 (device_id, day)
    _migrate_daily_stats_pk(conn)

    v2 = conn.execute("PRAGMA user_version").fetchone()[0] >= 2
    fact_tables = ["sessions", "daily_stats"]
    if not v2:
        fact_tables += ["stays", "trips", "places", "anomalies", "grid_pois", "route_grids"]
    for t in fact_tables:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        for col in ("created_at", "updated_at", "etl_version"):
            if col not in cols:
                conn.execute(f"ALTER TABLE {t} ADD COLUMN {col} TEXT")
    conn.commit()


def _migrate_daily_stats_pk(conn: sqlite3.Connection) -> None:
    """将 daily_stats 主键由 (day) 修正为 (device_id, day)；若已正确则跳过。
    旧库可能：a) 无 device_id 列且 PK=day；b) 已有 device_id 列但 PK 仍=day。
    两种都需重建。保留全部历史数据。重建时按旧表实际列动态拼接，避免引用
    不存在的 P0 派生列（旧库可能无）导致 SELECT 失败。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_stats)")}
    pk_cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_stats)") if r[5]]
    if pk_cols == ["day"] or "device_id" not in cols:
        has_dev = "device_id" in cols
        sel_dev = "COALESCE(device_id, 'unknown')" if has_dev else "'unknown'"
        conn.execute("DROP TABLE IF EXISTS daily_stats_old")
        conn.execute("ALTER TABLE daily_stats RENAME TO daily_stats_old")
        old_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_stats_old)")}
        p0_cols = [c for c in ("sleep_start_hhmm", "sleep_end_hhmm", "sleep_duration_min", "time_app_json")
                   if c in old_cols]
        p0_typ = {"sleep_start_hhmm": "TEXT", "sleep_end_hhmm": "TEXT",
                  "sleep_duration_min": "INTEGER", "time_app_json": "TEXT"}
        p0_def = ""
        for c in p0_cols:
            p0_def += "\n                " + c + " " + p0_typ[c]
        conn.execute(
            """
            CREATE TABLE daily_stats (
                device_id TEXT NOT NULL DEFAULT 'unknown',
                day TEXT NOT NULL,
                total_screen_ms INTEGER NOT NULL DEFAULT 0,
                app_ranking_json TEXT,
                notification_count INTEGER NOT NULL DEFAULT 0,
                notification_clicked INTEGER NOT NULL DEFAULT 0,
                top_notification_apps_json TEXT,
                screen_on_count INTEGER NOT NULL DEFAULT 0,
                screen_off_count INTEGER NOT NULL DEFAULT 0,
                unlock_count INTEGER NOT NULL DEFAULT 0,
                switch_count INTEGER NOT NULL DEFAULT 0,
                location_count INTEGER NOT NULL DEFAULT 0,
                audio_clip_count INTEGER NOT NULL DEFAULT 0""" + p0_def + """,
                updated_at TEXT DEFAULT (datetime('now', '+8 hours')),
                created_at TEXT,
                etl_version TEXT,
                PRIMARY KEY(device_id, day)
            )
            """
        )
        p0_list = ", ".join(p0_cols)
        sel_p0 = (", " + p0_list) if p0_list else ""
        conn.execute(
            """
            INSERT INTO daily_stats (
                device_id, day, total_screen_ms, app_ranking_json,
                notification_count, notification_clicked, top_notification_apps_json,
                screen_on_count, screen_off_count, unlock_count,
                switch_count, location_count, audio_clip_count""" + sel_p0 + """, updated_at
            )
            SELECT """ + sel_dev + """, day, total_screen_ms, app_ranking_json,
                notification_count, notification_clicked, top_notification_apps_json,
                screen_on_count, screen_off_count, unlock_count,
                switch_count, location_count, audio_clip_count""" + sel_p0 + """, updated_at
            FROM daily_stats_old
            """
        )
        conn.execute("DROP TABLE daily_stats_old")
        conn.commit()


def _migrate_anomalies_unique(conn: sqlite3.Connection) -> None:
    """anomalies 唯一键加入 device_id（Task 5c 设备隔离修复）。

    旧表 UNIQUE(day, kind, grid_key)：两设备同日同类同网格异常互相吞行
    （off_schedule 的 grid_key 恒为空串，跨设备必撞 → INSERT OR IGNORE 丢行）。
    anomalies 为全量重算的派生表，重建迁移保留历史行与 id。v2 已激活
    （user_version>=2）时 schema 冻结——其唯一索引本就含 device_id，跳过。
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='anomalies'"
    ).fetchone():
        return
    for idx in conn.execute("PRAGMA index_list(anomalies)").fetchall():
        if not idx[2]:  # unique 标志位（tuple/Row 位置访问双兼容）
            continue
        cols = {r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})")}
        if "device_id" in cols:
            return  # 已是含 device_id 的唯一键（新库 / 已迁移）
    # 显式事务：RENAME→CREATE→INSERT→DROP 原子执行，中途崩溃不留
    # anomalies_old 残留（否则下次 _SCHEMA 先建空新表会让本函数提前 return）
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS anomalies_old")
        conn.execute("ALTER TABLE anomalies RENAME TO anomalies_old")
        conn.execute(
            """
            CREATE TABLE anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                kind TEXT NOT NULL,
                device_id TEXT NOT NULL,
                grid_key TEXT,
                poi TEXT,
                detail TEXT,
                ts INTEGER,
                UNIQUE(day, kind, device_id, grid_key)
            )
            """
        )
        conn.execute(
            "INSERT INTO anomalies(id, day, kind, device_id, grid_key, poi, detail, ts) "
            "SELECT id, day, kind, device_id, grid_key, poi, detail, ts FROM anomalies_old"
        )
        conn.execute("DROP TABLE anomalies_old")
        # 旧表索引随重命名仍占用 idx_anomalies_day 名，必须等旧表删除后再建
        conn.execute("CREATE INDEX IF NOT EXISTS idx_anomalies_day ON anomalies(day)")
        conn.commit()
    except Exception:
        if own_tx:
            conn.rollback()
        raise


def _stamp_fact_tables(conn: sqlite3.Connection) -> None:
    """B8：标注 etl_version/updated_at，并回填 created_at（按各表自然时间字段）。

    位置事实 v2 激活后位置表结构冻结（无 etl_version 列），仅标注 sessions/daily_stats。
    """

    # (表, 自然时间字段, 是否毫秒时间戳)
    spec = {
        "sessions": ("start_ms", True),
        "stays": ("start_ts", True),
        "trips": ("start_ts", True),
        "daily_stats": ("day", False),
        "places": ("first_seen", True),
        "anomalies": ("ts", True),
        "grid_pois": ("queried_at", True),
        "route_grids": ("day", False),
    }
    v2 = conn.execute("PRAGMA user_version").fetchone()[0] >= 2
    if v2:
        frozen = {"stays", "trips", "places", "anomalies", "grid_pois", "route_grids"}
        spec = {t: s for t, s in spec.items() if t not in frozen}
    for t, (time_col, is_ms) in spec.items():
        conn.execute(
            f"UPDATE {t} SET etl_version=?, updated_at=datetime('now','+8 hours') "
            f"WHERE etl_version IS NULL OR etl_version != ?",
            (ETL_VERSION, ETL_VERSION),
        )
        if is_ms:
            conn.execute(
                f"UPDATE {t} SET created_at=datetime({time_col}/1000,'unixepoch','+8 hours') "
                f"WHERE created_at IS NULL AND {time_col} IS NOT NULL"
            )
        else:
            conn.execute(
                f"UPDATE {t} SET created_at={time_col} WHERE created_at IS NULL AND {time_col} IS NOT NULL"
            )
    conn.commit()


def _read_watermarks(conn: sqlite3.Connection) -> dict[str, int]:
    """B1：读取 etl_state 中的 per-device 水位线（上次处理的最大事件 ts）。"""
    rows = conn.execute("SELECT device_id, last_event_ts FROM etl_state").fetchall()
    return {r[0]: r[1] for r in rows if r[1] is not None}


def _compute_affected_days(conn: sqlite3.Connection, watermarks: dict[str, int], lookback_days: int = 3) -> set[str]:
    """B1：以水位线为锚，回看 lookback_days 天，得到需要重建的日期集合。"""
    min_ts = min(watermarks.values())
    start = datetime.datetime.fromtimestamp(min_ts / 1000, _TZ_CST).date() - datetime.timedelta(days=lookback_days)
    end = datetime.datetime.now(_TZ_CST).date()
    days: set[str] = set()
    cur = start
    while cur <= end:
        days.add(cur.isoformat())
        cur += datetime.timedelta(days=1)
    return days


def _update_etl_state(conn: sqlite3.Connection) -> None:
    """B1：成功重建后写入 per-device 水位线。"""
    now = _now_cst(conn)
    rows = conn.execute("SELECT device_id, MAX(ts) FROM events GROUP BY device_id").fetchall()
    for device_id, last_ts in rows:
        if last_ts is None:
            continue
        conn.execute(
            "INSERT INTO etl_state(device_id, last_event_ts, last_run_at, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "last_event_ts=excluded.last_event_ts, last_run_at=excluded.last_run_at, updated_at=excluded.updated_at",
            (device_id, last_ts, now, now),
        )
    conn.commit()


def _write_daily_quality(
    conn: sqlite3.Connection, rows: list[tuple], incremental_active: bool, affected_days: set[str]
) -> None:
    """§3.2 坐标质量日表幂等写入：增量只重建 affected days，全量 DELETE 重建。

    rows 为 location_facts.daily_quality_rows 产物（v1/v2 管线共用本入口，
    表名固定不随 user_version 切换）。写入前幂等补建表——shadow 等自建连接
    的路径可能未执行过 etl._SCHEMA（P1：旧库跑 --location-shadow 崩溃）。
    用 execute 而非 executescript：后者会隐式 COMMIT 挂起事务，破坏 v1
    管线中途失败的整体回滚。
    """
    conn.execute(_DAILY_QUALITY_DDL)
    if incremental_active and affected_days:
        ph = ",".join("?" * len(affected_days))
        conn.execute(f"DELETE FROM daily_location_quality WHERE day IN ({ph})", list(affected_days))
    else:
        conn.execute("DELETE FROM daily_location_quality")
    conn.executemany(
        "INSERT INTO daily_location_quality(day, device_id, points_total, points_valid, "
        "accuracy_known, accuracy_le_50, accuracy_51_150, accuracy_gt_150, "
        "observed_half_hour_bins, median_interval_sec, providers_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day, device_id) DO UPDATE SET "
        "points_total=excluded.points_total, points_valid=excluded.points_valid, "
        "accuracy_known=excluded.accuracy_known, accuracy_le_50=excluded.accuracy_le_50, "
        "accuracy_51_150=excluded.accuracy_51_150, accuracy_gt_150=excluded.accuracy_gt_150, "
        "observed_half_hour_bins=excluded.observed_half_hour_bins, "
        "median_interval_sec=excluded.median_interval_sec, "
        "providers_json=excluded.providers_json, "
        "updated_at=datetime('now','+8 hours')",
        rows,
    )
    print(f"[etl] daily_location_quality(坐标质量日表): {len(rows)} 行")


def _migrate_trips_coord_columns(conn: sqlite3.Connection) -> None:
    """Task 6：旧库 trips 补坐标制两列（新库 schema 已含；v2 冻结表自带，跳过）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trips)")}
    if "endpoint_coord_system" not in cols:
        conn.execute("ALTER TABLE trips ADD COLUMN endpoint_coord_system TEXT NOT NULL DEFAULT 'unknown'")
    if "polyline_coord_system" not in cols:
        conn.execute("ALTER TABLE trips ADD COLUMN polyline_coord_system TEXT")


def _build_location_v1(conn: sqlite3.Connection, events, incremental_active: bool, affected_days: set[str]) -> None:
    """v1 位置管线：stays/trips/places 构建 + 家/公司候选推断 + top2 主点标记。

    v2 激活后由 run() 分流跳过（v1 写入会破坏 place_id 语义并复活 visit_count
    累加事故）；本函数保持 v1 库行为不变。结束时提交并关闭传入连接。

    Task 6：全链路共用 location_facts.location_points 解析（非法坐标统一拒绝）、
    §3.1 accuracy filter（默认只观测）、§3.2 日质量表、trips 坐标制两列；
    坐标制配置错误（坏 JSON / 重叠 period）在入口拒绝 ETL。
    """

    from gacore.langTrack import location_facts as lf
    from gacore.langTrack.etl_config import load_coord_systems, load_etl_config, resolve_coord_system

    coord_cfg = load_coord_systems()
    _write_daily_quality(
        conn, lf.daily_quality_rows(events, coord_cfg), incremental_active, affected_days
    )

    points = lf.location_points(events, coord_cfg)

    loc_cfg = load_etl_config().get("location", {})

    geom_points = lf.accuracy_filter(points, loc_cfg)

    if len(geom_points) < len(points):
        dropped = len(points) - len(geom_points)
        print(f"[etl] accuracy filter: 剔除 {dropped} 点（仅几何构建输入，质量统计仍全量观测）")

    # L1：停驻点检测 → stays 表（全量重建）

    if incremental_active:
        ph = ",".join("?" * len(affected_days))
        conn.execute(f"DELETE FROM stays WHERE day IN ({ph})", list(affected_days))
    else:
        conn.execute("DELETE FROM stays")

    stays = build_stays(geom_points)
    if incremental_active:
        stays = [r for r in stays if r[-1] in affected_days]

    conn.executemany(

        "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, center_lon, "

        "min_lat, min_lon, max_lat, max_lon, n_points, radius_m, grid_key, day) "

        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",

        stays,

    )

    print(f"[etl] stays(L1 停驻点): {len(stays)}")



    # L3：移动轨迹段（trips）——全量重建结构，已编码路线按 (device,start_ts,end_ts) 带回，

    # 已编码列保留（不烧高德配额）；新移动段编码列为空，由下方增量补路补齐。

    from gacore.langTrack.routes import build_trips

    old_route: dict[tuple, tuple] = {}

    for r in conn.execute(

        "SELECT device_id, start_ts, end_ts, polyline, route_key, route_mode, route_encoded_at FROM trips"

    ):

        old_route[(r[0], r[1], r[2])] = (r[3], r[4], r[5], r[6])

    if incremental_active:
        ph = ",".join("?" * len(affected_days))
        conn.execute(f"DELETE FROM trips WHERE day IN ({ph})", list(affected_days))
    else:
        conn.execute("DELETE FROM trips")

    # Task 6：阈值统一从 etl_config trips 节点读取（与 v2 shadow 同一配置来源）
    trips_cfg = load_etl_config().get("trips", {})
    trips = build_trips(
        geom_points, stays,
        min_duration_ms=int(trips_cfg.get("min_duration_ms", 60000)),
        min_dist_m=float(trips_cfg.get("min_dist_m", 300.0)),
        max_infer_gap_ms=int(trips_cfg.get("max_infer_gap_ms", 7200000)),
    )
    if incremental_active:
        trips = [t for t in trips if t[10] in affected_days]

    for t in trips:

        poly, rk, mode, enc = old_route.get((t[0], t[1], t[2]), (None, None, None, None))

        conn.execute(

            "INSERT INTO trips(device_id, start_ts, end_ts, duration_ms, start_lat, start_lon, "

            "end_lat, end_lon, dist_m, n_points, day, polyline, route_key, route_mode, "
            "route_encoded_at, endpoint_coord_system, polyline_coord_system) "

            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",

            (t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7], t[8], t[9], t[10],

             poly, rk, mode, enc,

             # 起终点为 source 坐标（不转换落库）；polyline 来自高德固定 GCJ02
             resolve_coord_system(t[0], t[1], coord_cfg),

             "gcj02" if poly else None),

        )

    print(f"[etl] trips(L3 移动段): {len(trips)}")



    # places：保留已标注 label（家/公司/未知），只更新统计；新点以"未知"插入
    # 用原始点（未过滤）：v1 visit_count 一直是"网格内原始 location 点数"，
    # 与 v2 point_count 同口径（§2.3）；精度过滤只作用于 stays/trips 几何构建
    places = build_places(points)

    for p in places:

        conn.execute(

            """

            INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, last_seen, visit_count)

            VALUES (?,?,?,?,?,?,?,?)

            ON CONFLICT(device_id, grid_key) DO UPDATE SET

              lat=excluded.lat, lon=excluded.lon,

              first_seen=MIN(places.first_seen, excluded.first_seen),

              last_seen=MAX(places.last_seen, excluded.last_seen),

              visit_count=places.visit_count + excluded.visit_count

            """,

            p,

        )

    print(f"[etl] places: {len(places)}")



    # L1：家/公司置信度候选（不覆盖用户已确认标签）

    n_cand = infer_home_work_candidates(conn)

    print(f"[etl] 家/公司置信度候选更新: {n_cand} 个")



    # 自动标记 top2 主常驻点（按访问次数），不覆盖已确认的家/公司标签

    top2 = conn.execute(

        "SELECT id, grid_key, label FROM places ORDER BY visit_count DESC LIMIT 2"

    ).fetchall()

    for tid, _, tlabel in top2:

        # 仅当该点尚未被人工确认（label 仍为未知）时才提示，主标记始终置位

        conn.execute("UPDATE places SET is_primary=1 WHERE id=?", (tid,))

    conn.commit()

    conn.close()


def run(db_path: Path = DB_PATH, device_id: str | None = None, run_geocode: bool = True, run_route: bool = True, run_poi: bool = True, incremental: bool = False) -> None:

    conn = sqlite3.connect(db_path)

    conn.executescript(_SCHEMA)

    # 位置事实 v2 守卫：激活后（user_version>=2）places/stays/trips 为 v2 冻结
    # schema（place_id 主键 + 三个计数语义）。v1 位置管线禁止触碰——否则
    # visit_count 累加事故（P0）复活、place_id/stay 引用被洗掉。位置事实改由
    # rebuild_location_v2 全量重建（见下方 L1 段分支）。
    v2_location = conn.execute("PRAGMA user_version").fetchone()[0] >= 2

    if v2_location:

        print("[etl] location schema v2 active: 位置事实走 v2 全量重建分支")

    else:

        # 迁移：旧 places 表补 is_primary 列（新库已含）

        cols = {r[1] for r in conn.execute("PRAGMA table_info(places)")}

        if "is_primary" not in cols:

            conn.execute("ALTER TABLE places ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0")

        # 迁移：places 语义/候选列 + 旧库空 device_id 归并

        migrate_places(conn)

        # 迁移：anomalies 唯一键加入 device_id（跨设备同日同类异常不再互相吞行）
        _migrate_anomalies_unique(conn)

        # 迁移：Task 6 trips 坐标制两列（v2 冻结表自带，无需迁移）
        _migrate_trips_coord_columns(conn)

    # B migration: ensure fact tables have device_id / created_at / updated_at / etl_version
    # （v2 下内部自动跳过位置事实表，仅处理 sessions/daily_stats）
    _migrate_fact_tables(conn)

    # B1 incremental watermark: read etl_state; if watermark exists, rebuild only lookback window
    incremental_active = False
    affected_days: set[str] = set()
    if incremental:
        watermarks = _read_watermarks(conn)
        if watermarks:
            affected_days = _compute_affected_days(conn, watermarks, lookback_days=3)
            if affected_days:
                incremental_active = True
                print(f"[etl] incremental: rebuild {len(affected_days)} affected days")
            else:
                print("[etl] incremental: no affected days, full rebuild")
        else:
            print("[etl] incremental: no watermark in etl_state, full rebuild")

    # 同设备重装前后 device_id 归一（必须在 load_events 之前，build 系列按 device_id 分组）

    n_alias = merge_device_aliases(conn)

    if n_alias:

        print(f"[etl] 设备别名归并完成: {n_alias} 个")



    events = load_events(conn)

    print(f"[etl] 事件总数(清洗后): {len(events)}")



    # 重置表（全量重建，简单可靠）；places 用 upsert 保留已标注标签（家/公司）

    # P0 派生列保护：DELETE 前快照，INSERT 基础列后按 (device_id, day) 还原
    p0_snap = {}
    try:
        for _d, _day, _s1, _s2, _dur, _taj in conn.execute(
            "SELECT device_id, day, sleep_start_hhmm, sleep_end_hhmm, "
            "sleep_duration_min, time_app_json FROM daily_stats"
        ):
            p0_snap[(_d, _day)] = (_s1, _s2, _dur, _taj)
    except sqlite3.OperationalError:
        p0_snap = {}

    if incremental_active:
        ph = ",".join("?" * len(affected_days))
        conn.execute(f"DELETE FROM sessions WHERE day IN ({ph})", list(affected_days))
        conn.execute(f"DELETE FROM daily_stats WHERE day IN ({ph})", list(affected_days))
    else:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM daily_stats")
    sessions = build_sessions(events)
    if incremental_active:
        sessions = [r for r in sessions if r[1] in affected_days]

    conn.executemany(

        "INSERT INTO sessions(device_id, day, pkg, app, activity, start_ms, end_ms, duration_ms) "

        "VALUES (?,?,?,?,?,?,?,?)",

        sessions,

    )

    print(f"[etl] sessions: {len(sessions)}")



    daily = build_daily_stats(events, sessions)
    if incremental_active:
        daily = [r for r in daily if r[1] in affected_days]

    conn.executemany(

        "INSERT INTO daily_stats(device_id, day, total_screen_ms, app_ranking_json, notification_count, "

        "notification_clicked, top_notification_apps_json, screen_on_count, screen_off_count, "

        "unlock_count, switch_count, location_count, audio_clip_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",

        daily,

    )

    print(f"[etl] daily_stats: {len(daily)}")

    if p0_snap:
        conn.executemany(
            "UPDATE daily_stats SET sleep_start_hhmm=?, sleep_end_hhmm=?, "
            "sleep_duration_min=?, time_app_json=? WHERE device_id=? AND day=?",
            [(_s1, _s2, _dur, _taj, _d, _day) for (_d, _day), (_s1, _s2, _dur, _taj) in p0_snap.items()],
        )
        conn.commit()
        print(f"[etl] daily_stats P0 派生列还原: {len(p0_snap)} 行")



    # ===== 位置事实构建：v1 管线 / v2 全量重建分流 =====
    if v2_location:

        # v2：全量重建正式 places/place_cells/stays/trips（单一计算来源，保留
        # 人工 tag / geocode 缓存 / 已编码路线）；首版 v2 位置只允许全量，
        # incremental 仅记录。v1 候选推断/top2 基于全表 grid_key UPDATE（无设备
        # 隔离），v2 下跳过——v2 版按 (device_id, place_id) 的候选推断尚未实现
        # （candidate_label/confidence_* 在 v2 下保持 NULL，label CLI 手动打标签
        # 不受影响），待后续任务补齐。

        conn.commit()

        conn.close()

        from gacore.langTrack.location_migration import rebuild_location_v2

        n_stays_v2 = rebuild_location_v2(db_path, incremental=incremental)

        print(f"[etl] location v2 全量重建: stays={n_stays_v2}")

    else:

        _build_location_v1(conn, events, incremental_active, affected_days)

    # 恢复持久化的家/公司标签（data/place_labels.json）

    try:

        from gacore.langTrack.label_places import apply_labels

        n = apply_labels(db_path)

        if n:

            print(f"[etl] 恢复持久化地点标签: {n} 个")

    except ImportError:

        pass

    # P1-3：新地点/异常事件探测（依赖 places 标签已恢复，家/公司网格集合准确）

    conn = sqlite3.connect(db_path)

    n_anom = detect_anomalies(conn)

    conn.close()

    print(f"[etl] anomalies 异常事件: {n_anom} 条")

    # L2：增量 regeo 编码（仅未编码常驻点，ETL 重跑零新增调用）

    if run_geocode:

        try:

            from gacore.langTrack import geocode

            n = geocode.incremental_encode(db_path)

            print(f"[etl] 增量 regeo 编码: {n} 个常驻点")

        except SystemExit as e:

            print(f"[etl] 跳过增量 regeo 编码: {e}")

        except Exception as e:

            print(f"[etl] 增量 regeo 编码失败(不影响 ETL): {e}")

    # L3：增量路径规划补路（仅未编码新移动段，单次上限节流；失败跳过不阻塞）

    if run_route:

        try:

            from gacore.langTrack import routes

            n = routes.incremental_encode_trips(db_path)

            print(f"[etl] 增量补路(路径规划): {n} 段")

        except SystemExit as e:

            print(f"[etl] 跳过增量补路: {e}")

        except Exception as e:

            print(f"[etl] 增量补路失败(不影响 ETL): {e}")

    # L3：路线变化事件（复用 anomalies 叙事表，依赖 trips 已完成补路）

    conn = sqlite3.connect(db_path)

    n_rc = detect_route_changes(conn)

    conn.close()

    print(f"[etl] route_change 路线变化事件: {n_rc} 条")

    # P1：路过网格统计（通勤带）——纯本地零配额，全量重建

    try:

        from gacore.langTrack import routes as _routes_p1

        n_g = _routes_p1.build_route_grids(db_path)

        print(f"[etl] 路过网格统计(通勤带): {n_g} 行")

    except Exception as e:

        print(f"[etl] 路过网格统计失败(不影响 ETL): {e}")

    # P2：沿途 POI（around 100 次/日受限，默认每轮最多 5 网格，网格级缓存去重）

    if run_poi:

        try:

            n_p = routes.encode_belt_pois(db_path, limit=_POI_MAX_PER_RUN)

            print(f"[etl] 沿途 POI 编码: {n_p} 个网格")

        except SystemExit as e:

            print(f"[etl] 跳过沿途 POI 编码: {e}")

        except Exception as e:
            print(f"[etl] 沿途 POI 编码失败(不影响 ETL): {e}")

    # A① 契约覆盖校验：期望事件类型 vs 实际到达（全量重建，写在最后）
    conn = sqlite3.connect(db_path)
    n_cov = build_contract_coverage(conn)
    conn.close()
    print(f"[etl] contract_coverage: {n_cov} 种类型覆盖校验完成")

    # B8 版本标注 + B1 增量水位线（最后统一打标，覆盖全部事实表）
    conn = sqlite3.connect(db_path)
    _stamp_fact_tables(conn)
    _update_etl_state(conn)
    conn.close()

    print("[etl] 完成")




def main() -> None:

    parser = argparse.ArgumentParser(description="langTrack ETL")

    parser.add_argument("--db", type=Path, default=DB_PATH)

    parser.add_argument("--purge", action="store_true", help="先清理异常事件再重建事实表")

    parser.add_argument("--no-geocode", action="store_true", help="跳过高德增量 regeo 编码")

    parser.add_argument("--no-route", action="store_true", help="跳过高德路径规划补路(L3)")

    parser.add_argument("--no-poi", action="store_true", help="跳过沿途 POI 编码(P2)")

    parser.add_argument("--incremental", action="store_true", help="incremental rebuild from watermark")

    parser.add_argument("--location-shadow", action="store_true", help="构建位置事实 v2 shadow 对比表(只读,不切换生产表)")

    parser.add_argument("--location-prepare", action="store_true",
                        help="Task 4: 迁移稳定 place_id/人工 tag/geocode 缓存, 写标签 v3 pending(不切换生产表)")

    parser.add_argument("--location-activate", action="store_true",
                        help="Task 4: 事务切换 v2 正式表并原子替换标签文件(需先 --location-prepare)")

    parser.add_argument("--location-rollback", action="store_true",
                        help="Task 4: 回滚到 v1 六张业务表并恢复标签 v2 backup")

    parser.add_argument("--location-recover", action="store_true",
                        help="Task 4: 服务启动恢复 pending_label_swap(崩溃后自动补标签切换)")
    args = parser.parse_args()

    if args.location_shadow:
        from gacore.langTrack import location_migration
        location_migration.build_location_shadow(args.db, incremental=args.incremental)
        return

    if args.location_prepare:
        from gacore.langTrack import location_migration
        location_migration.prepare_location_migration(
            args.db, labels_path=location_migration.DEFAULT_LABELS_PATH, run_id=location_migration.new_run_id()
        )
        return

    if args.location_activate:
        from gacore.langTrack import label_places
        from gacore.langTrack import location_migration
        labels_path = location_migration.DEFAULT_LABELS_PATH
        pending = labels_path.with_name(labels_path.name + label_places.PENDING_SUFFIX)
        conn = sqlite3.connect(args.db)
        try:
            location_migration.activate_location_v2(conn, location_migration.new_run_id(),
                                                    pending_labels_path=str(pending))
        finally:
            conn.close()
        location_migration.finalize_label_swap(args.db, labels_path=labels_path)
        print("[etl] location v2 activate 完成: user_version=2, 标签文件已切换为 v3")
        return

    if args.location_rollback:
        from gacore.langTrack import label_places
        from gacore.langTrack import location_migration
        run_id = location_migration.new_run_id()
        conn = sqlite3.connect(args.db)
        try:
            location_migration.rollback_location_v2(conn, run_id)
        finally:
            conn.close()
        # DB 已回滚到 v1：用 v2 backup 原子恢复标签文件，并删除残留 pending
        labels_path = location_migration.DEFAULT_LABELS_PATH
        backup = labels_path.with_name(labels_path.name + label_places.BACKUP_SUFFIX)
        pending = labels_path.with_name(labels_path.name + label_places.PENDING_SUFFIX)
        if backup.exists():
            label_places.restore_labels_backup(backup, labels_path)
        pending.unlink(missing_ok=True)
        print("[etl] location v2 rollback 完成: user_version=1, 标签文件已恢复 v2 backup")
        return

    if args.location_recover:
        from gacore.langTrack import location_migration
        action = location_migration.recover_pending_swap(
            args.db, labels_path=location_migration.DEFAULT_LABELS_PATH
        )
        print(f"[etl] location v2 recover: {action}")
        return

    if args.purge:

        purge_dirty(args.db)

    run(args.db, run_geocode=not args.no_geocode, run_route=not args.no_route, run_poi=not args.no_poi, incremental=args.incremental)





if __name__ == "__main__":

    main()

