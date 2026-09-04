"""Task 8 长期空间画像（spatial_profile）测试。

运行需 PYTHONPATH=src（见文件末尾 sys.path）。不依赖真实 data/langTrack.db。

覆盖（Task 8 清单）：
- fixture：≥90 天、2 个设备、确认家/公司、工作日通勤、周末地点、定位缺失日
- frequent_places：7/30/90 天 visit_days/episodes/stay_ms/时段分布
- spatial_extent：加权球面质心、home distance 分位数、radius_of_gyration、entropy；无家 label → None
- 跨午夜裁剪：窗口与自然日均按 CST 半开区间裁剪 stay
- commute/rhythm：同日直接相邻、gap 5min–4h；rhythm 仅纳入 coverage≥0.5 且有 anchor 的日
- scene_exposure/place_change：旧窗口为 0 时 change_pct=None；previously seen 定义
- 每指标 Evidence：components 可复算、score∈[0,1]
- SQL 全部带 device_id（devB 数据不影响 devA 结果）
- 90 天冷查询性能 <1s
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import gacore.langTrack.spatial_profile as sp

_TZ = timezone(timedelta(hours=8))

_AS_OF = "2026-08-31"  # 周一


def _ts(y, mo, d, h=0, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=_TZ).timestamp() * 1000)


def _day_ts(day_str: str, h=0, mi=0):
    y, mo, d = (int(x) for x in day_str.split("-"))
    return _ts(y, mo, d, h, mi)


def _all_days(as_of: str, n: int) -> list[str]:
    y, mo, d = (int(x) for x in as_of.split("-"))
    base = date(y, mo, d)
    return [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def _add_days(day_str: str, n: int) -> str:
    y, mo, d = (int(x) for x in day_str.split("-"))
    return (date(y, mo, d) + timedelta(days=n)).strftime("%Y-%m-%d")


def _weekday(day_str: str) -> bool:
    y, mo, d = (int(x) for x in day_str.split("-"))
    return date(y, mo, d).weekday() < 5


# 地点坐标（同类地点共享坐标，方便验证距离）
HOME = (39.9000, 116.4000)     # 家
WORK = (39.9200, 116.4200)     # 公司
PARK = (39.9500, 116.4500)     # 周末公园（无 tag，真实 poi）
FRESH = (39.9900, 116.4900)    # 前窗口后才首次出现的地点（place_change 用）

# 完全缺定位日：无 quality 行、也无 stay（frequent_places/空间指标体现当日缺测）
NO_DATA_DAYS = {"2026-08-05"}
# 低质量日：有 stay 但无 quality 行（rhythm 的 coverage=0 → 计为 missing_days）
POOR_QUALITY_DAYS = {"2026-08-12"}


def _make_db(device_id: str = "devA", as_of: str = _AS_OF,
             with_work: bool = True, with_park: bool = True,
             days_back: int = 95) -> sqlite3.Connection:
    """合成 v2 长期库：每天 1 套家/公司(工作日)/公园(周末) stays。

    - daily_location_quality 按真实 ETL DDL 建，含 90+ 天行；
    - 缺失日（MISSING_DAYS）无 quality 行（coverage=0 → rhythm 缺测）；
    - devices.first_seen = as_of-90 天前（构造 90 天 available 窗口）；
    - 返回带 conn.row_factory=sqlite3.Row 的内存库。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA user_version=2")
    cur.execute("CREATE TABLE devices (device_id TEXT PRIMARY KEY, first_seen INTEGER)")
    cur.execute("CREATE TABLE etl_state (device_id TEXT PRIMARY KEY, last_event_ts INTEGER)")
    cur.execute(
        "CREATE TABLE places ("
        "id INTEGER, device_id TEXT, place_id TEXT, grid_key TEXT, lat REAL, lon REAL,"
        "label TEXT, first_seen INTEGER, last_seen INTEGER, visit_count INTEGER,"
        "is_primary INTEGER, address TEXT, poi TEXT, poi_fallback TEXT, district TEXT,"
        "township TEXT, business_area TEXT, poi_type TEXT, behavior TEXT,"
        "matched_level TEXT, candidate_label TEXT, confidence_home REAL,"
        "confidence_work REAL, geocoded_at INTEGER, point_count INTEGER, stay_ms INTEGER,"
        "name_confidence REAL, name_evidence TEXT, parent_poi TEXT,"
        "poi_l1 TEXT, poi_l2 TEXT, poi_l3 TEXT)"
    )
    cur.execute(
        "CREATE TABLE stays ("
        "id INTEGER, device_id TEXT, place_id TEXT, grid_key TEXT,"
        "start_ts INTEGER, end_ts INTEGER, duration_ms INTEGER,"
        "center_lat REAL, center_lon REAL, min_lat REAL, min_lon REAL,"
        "max_lat REAL, max_lon REAL, n_points INTEGER, radius_m REAL,"
        "day TEXT, avg_accuracy_m INTEGER)"
    )
    cur.execute(
        "CREATE TABLE daily_location_quality ("
        "day TEXT NOT NULL, device_id TEXT NOT NULL,"
        "points_total INTEGER NOT NULL, points_valid INTEGER NOT NULL,"
        "accuracy_known INTEGER NOT NULL, accuracy_le_50 INTEGER NOT NULL,"
        "accuracy_51_150 INTEGER NOT NULL, accuracy_gt_150 INTEGER NOT NULL,"
        "observed_half_hour_bins INTEGER NOT NULL, median_interval_sec REAL,"
        "providers_json TEXT NOT NULL, created_at TEXT, updated_at TEXT,"
        "PRIMARY KEY(day, device_id))"
    )

    days = _all_days(as_of, days_back)
    first_seen = _day_ts(days[0])
    cur.execute("INSERT INTO devices VALUES (?,?)", (device_id, first_seen))
    cur.execute("INSERT INTO etl_state VALUES (?,?)", (device_id, _day_ts(as_of, 23, 59)))
    # 第二设备：数据极少，验证 SQL 均带 device_id（不影响 devA）
    cur.execute("INSERT INTO devices VALUES ('devB', ?)", (_day_ts(days[0]) - 1,))
    cur.execute("INSERT INTO etl_state VALUES ('devB', ?)", (_day_ts(as_of, 10, 0),))
    cur.execute(
        "INSERT INTO places (id, device_id, place_id, grid_key, lat, lon, label,"
        "point_count, stay_ms, name_confidence, name_evidence, poi_l1) "
        "VALUES (1, 'devB', 'b_only', 'g', 0.0, 0.0, '未知', 1, 60000, 0.0, '', '')"
    )
    cur.execute(
        "INSERT INTO stays VALUES (998,'devB','b_only','g',?,?,60000,0.0,0.0,"
        "0.0,0.0,0.0,0.0,1,10.0,'?',30)",
        (_day_ts(as_of) - 3600000, _day_ts(as_of)),
    )
    cur.execute(
        "INSERT OR REPLACE INTO daily_location_quality "
        "VALUES (?, 'devB', 20,18,10,10,6,2,6,300.0,'{}',"
        "datetime('now','+8 hours'), datetime('now','+8 hours'))",
        (as_of,),
    )

    # places：家 / 公司 / 公园（家公司用确认 label；公园为真实 poi + label=未知）
    places = []
    pid = 1
    if with_work:
        places.append((pid, "home", HOME, "家"))
        pid += 1
        places.append((pid, "work", WORK, "公司"))
        pid += 1
    else:
        places.append((pid, "home", HOME, "家"))
        pid += 1
    if with_park:
        places.append((pid, "park", PARK, "未知"))  # poi 名“某某公园”
        pid += 1
    # fresh：出现在当前窗口开始后（place_change 的 new canonical）
    fresh_first_seen = _day_ts(_add_days(as_of, -10, ))
    places.append((pid, "fresh", FRESH, "未知"))
    fresh_row = pid
    pid += 1

    p_idx = 0
    stay_id = 1
    for daystr in days:
        if daystr in NO_DATA_DAYS:
            continue  # 完全缺定位日：无 quality 行、无 stay
        # daily quality 行（工作日/周末都给足采样；POOR_QUALITY_DAYS 刻意不写）
        if daystr not in POOR_QUALITY_DAYS:
            observed = 46 if daystr == as_of else 30
            cur.execute(
                "INSERT OR REPLACE INTO daily_location_quality "
                "VALUES (?, ?, 60,58,50,40,14,4,?,500.0,'{}',"
                "datetime('now','+8 hours'), datetime('now','+8 hours'))",
                (daystr, device_id, observed),
            )

        if _weekday(daystr):
            # 家 00:00-08:00 → 公司 08:10-12:00 → 公司 13:00-17:40 → 家 18:00-24:00
            segs = [
                ("home", 0, 0, 8, 0),
                ("work", 8, 10, 12, 0),
                ("work", 13, 0, 17, 40),
                ("home", 18, 0, 24, 0),
            ]
        else:
            # 周末去公园 09:30-11:00（其余在家）
            segs = [
                ("home", 0, 0, 9, 30),
                ("park", 9, 30, 11, 0),
                ("home", 11, 0, 24, 0),
            ]

        for place_key, h0, m0, h1, m1 in segs:
            if place_key not in {"home", "work", "park"}:
                continue
            if place_key == "work" and not with_work:
                continue
            if place_key == "park" and not with_park:
                continue
            lat, lon = {"home": HOME, "work": WORK, "park": PARK}[place_key]
            start = _day_ts(daystr, h0, m0)
            if h1 == 24:
                end = _day_ts(_add_days(daystr, 1))
            else:
                end = _day_ts(daystr, h1, m1)
            cur.execute(
                "INSERT INTO stays VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (stay_id, device_id, None, "g", start, end, end - start,
                 lat, lon, lat, lon, lat, lon, 3, 25.0, daystr, 40),
            )
            stay_id += 1

    # 打上 place_id（read_stays join 用 place 表 place_id；直接 UPDATE stays）
    place_map = {"home": "home_p", "work": "work_p", "park": "park_p", "fresh": "fresh_p"}
    for idx, (pid, key, coord, label) in enumerate(places, start=1):
        visit_count = sum(1 for _ in ())  # placeholder
        first_s = fresh_first_seen if key == "fresh" else first_seen
        poi = "某某公园" if key == "park" else None
        l1 = "公园" if key == "park" else ("住宅" if key == "home" else "办公")
        cur.execute(
            "INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (idx, device_id, place_map[key], "g", coord[0], coord[1], label, first_s,
             _day_ts(as_of), 1, 0, None, poi, None, None, None, None, None, None,
             None, None, 0.8, 0.8, None, 1, 0, 0.8, "geocode", None, l1, "", ""),
        )
    # fresh 地点在当前窗口内出现 1 次（place_change 的 new canonical 用）
    cur.execute(
        "INSERT INTO stays VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (stay_id, device_id, "fresh_p", "g",
         _day_ts(as_of, 12, 0), _day_ts(as_of, 12, 30), 1800000,
         FRESH[0], FRESH[1], FRESH[0], FRESH[1], FRESH[0], FRESH[1], 3, 25.0, as_of, 40),
    )
    stay_id += 1

    # stays.place_id 关联：按坐标反查（家/公司/公园）
    cur.execute(
        "UPDATE stays SET place_id='home_p' WHERE device_id=? AND place_id IS NULL "
        "AND ABS(center_lat-?)<0.0001 AND ABS(center_lon-?)<0.0001",
        (device_id, HOME[0], HOME[1]),
    )
    cur.execute(
        "UPDATE stays SET place_id='work_p' WHERE device_id=? AND place_id IS NULL "
        "AND ABS(center_lat-?)<0.0001 AND ABS(center_lon-?)<0.0001",
        (device_id, WORK[0], WORK[1]),
    )
    cur.execute(
        "UPDATE stays SET place_id='park_p' WHERE device_id=? AND place_id IS NULL "
        "AND ABS(center_lat-?)<0.0001 AND ABS(center_lon-?)<0.0001",
        (device_id, PARK[0], PARK[1]),
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# fixture 基础
# ---------------------------------------------------------------------------

def test_fixture_has_90_days_and_two_devices():
    conn = _make_db()
    n_quality = conn.execute("SELECT COUNT(*) c FROM daily_location_quality WHERE device_id='devA'").fetchone()["c"]
    n_stays = conn.execute("SELECT COUNT(*) c FROM stays WHERE device_id='devA'").fetchone()["c"]
    devs = {r["device_id"] for r in conn.execute("SELECT device_id FROM devices").fetchall()}
    assert n_quality >= 80          # 90+ 天窗口，扣除缺失日仍 ≥80
    assert n_stays > 4 * 30         # 工作日 4 段 + 周末 3 段
    assert devs == {"devA", "devB"}


# ---------------------------------------------------------------------------
# 主入口：空骨架子段 / 全链路
# ---------------------------------------------------------------------------

def test_build_spatial_profile_full_smoke():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    assert prof["as_of_day"] == _AS_OF
    assert prof["data_as_of"] is not None
    # 6 大指标齐全
    assert prof["frequent_places"]
    assert prof["spatial_extent"] is not None
    assert prof["commute_profile"] is not None
    assert prof["home_work_rhythm"] is not None
    assert prof["scene_exposure"]
    assert prof["place_change"] is not None
    # per_window 有 7/30/90
    assert set(prof["per_window"]) == {"7", "30", "90"}


def test_build_spatial_profile_empty_db_no_crash():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    assert prof["frequent_places"] == []
    assert prof["spatial_extent"] is None
    assert prof["commute_profile"] is None
    assert prof["home_work_rhythm"] is None
    assert prof["scene_exposure"] == []
    assert prof["place_change"] is None


# ---------------------------------------------------------------------------
# frequent_places
# ---------------------------------------------------------------------------

def _freq_by_window(prof, days):
    idx = f"{days}"
    found = [p for p in prof["frequent_places"] if p["window_days"] == days]
    return found


def test_frequent_places_windows_and_metrics():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    all_f = prof["frequent_places"]
    assert all_f, "frequent_places 不应为空"
    assert all(0 <= p["window_days"] in (7, 30, 90) for p in all_f)
    # 30 天窗口：家/公司/公园在 30 天窗内都有到访（家+公司=工作日、公园=周末）
    f30 = _freq_by_window(prof, 30)
    by_name = {p["place_name"]: p for p in f30}
    assert "家" in by_name or any(p["place_id"] == "home_p" for p in f30)
    home = next(p for p in f30 if p["place_id"] == "home_p")
    work = next(p for p in f30 if p["place_id"] == "work_p")
    park = next(p for p in f30 if p["place_id"] == "park_p")
    # visit_days：30 天窗内 21 个工作日（剔除缺失日 8-05/8-12 → 19 天在家）
    assert home["visit_days"] >= 18
    assert work["visit_days"] >= 18
    assert park["visit_days"] >= 7      # 周末
    assert home["visit_episodes"] >= 2 * home["visit_days"] - 2
    assert home["stay_ms"] > 0 and work["stay_ms"] > 0
    assert home["weekday_visits"] > home["weekend_visits"]
    assert park["weekend_visits"] > park["weekday_visits"]
    # 时段分布：家的 stay 覆盖 凌晨/晚上；公司覆盖 上午/下午
    assert home["period_dist"].get("凌晨") or home["period_dist"].get("晚上")
    assert work["period_dist"].get("上午") and work["period_dist"].get("下午")
    # 常去门槛：公司考点满（visit_days≥3）
    assert work["qualified"] is True
    # 命名：家为公司真实 label
    home = next(p for p in f30 if p["place_id"] == "home_p")
    assert home["user_tag"] == "家"


def test_frequent_places_dedupe_keeps_30day_and_windows_tag():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    f_all = prof["frequent_places"]
    # 去重后每个 place 一条，window_days 保留 30 天口径；windows 标记含 7/30/90
    assert all(p["window_days"] == 30 for p in f_all)
    home = next(p for p in f_all if p["place_id"] == "home_p")
    assert set(home["windows"]) == {7, 30, 90}


# ---------------------------------------------------------------------------
# spatial_extent
# ---------------------------------------------------------------------------

def test_spatial_extent_home_distance_and_radius():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    se = prof["spatial_extent"]
    assert se is not None
    assert se["home_distance"] is not None
    d = se["home_distance"]
    # 家到公司直距 ~2800m，P50/P90/max 应在合理上界内（家到公园 ~7km 为 max）
    assert 0 <= d["p50_m"] <= 8000
    assert d["p90_m"] >= d["p50_m"]
    assert d["max_m"] >= d["p90_m"]
    assert d["home_place_id"] == "home_p"
    # radius > 0；weighted_center 存经纬度
    assert se["radius_of_gyration_m"] is not None and se["radius_of_gyration_m"] > 0
    assert se["place_count"] >= 3
    assert se["place_entropy"] > 0
    assert len(se["weighted_center"]) == 2


def test_spatial_extent_no_home_label_home_distance_none():
    conn = _make_db(with_work=False, with_park=True)
    # 去掉“家”标签（改为未知）——用直接改库模拟未确认家
    conn.execute("UPDATE places SET label='未知' WHERE place_id='home_p'")
    conn.commit()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    se = prof["spatial_extent"]
    assert se["home_distance"] is None           # 无家：home distance=None
    assert se["radius_of_gyration_m"] is not None  # 其余指标照常


# ---------------------------------------------------------------------------
# 跨午夜裁剪
# ---------------------------------------------------------------------------

def test_clip_midnight_stay_by_window_and_natural_day():
    conn = _make_db()
    # 手工插入一条跨午夜 stay：8-30 23:30 → 8-31 01:30
    cur = conn.cursor()
    max_id = conn.execute("SELECT COALESCE(MAX(id),0) m FROM stays").fetchone()["m"]
    cur.execute(
        "INSERT INTO stays VALUES (?, 'devA', 'home_p', 'g', ?, ?, 7200000,"
        "39.9000,116.4000,39.9000,116.4000,39.9000,116.4000,3,25.0,?,40)",
        (max_id + 1, _day_ts("2026-08-30", 23, 30), _day_ts("2026-08-31", 1, 30), "2026-08-30"),
    )
    conn.commit()
    # 30 天窗口 + 自然日展开
    w30_s, w30_e = sp._win_bounds(_AS_OF, 30)
    day30s = [_add_days(_AS_OF, i - 29) for i in range(30)]
    stays = __import__("gacore.langTrack.location_reader", fromlist=["read_stays"]).read_stays(
        conn, device_id="devA", overlap=(w30_s, w30_e), with_place=True
    )
    clipped = sp._clip_to_window_and_days(stays, w30_s, w30_e, day30s)
    # 找到那段 23:30-01:30 的裁剪产物：应切成 8-30(23:30-24:00) 与 8-31(00:00-01:30) 两条
    tail = [c for c in clipped if c["end_ts"] - c["start_ts"] == 30 * 60 * 1000
            and c["start_ts"] == _day_ts("2026-08-30", 23, 30)]
    head = [c for c in clipped if c["start_ts"] == _day_ts("2026-08-31")
            and c["end_ts"] == _day_ts("2026-08-31", 1, 30)]
    assert tail and head
    assert tail[0]["day"] == "2026-08-30"
    assert head[0]["day"] == "2026-08-31"


# ---------------------------------------------------------------------------
# commute_profile
# ---------------------------------------------------------------------------

def test_commute_profile_same_day_adjacent_gap():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    cp = prof["commute_profile"]
    assert cp is not None
    # 有效通勤日：工作日数 - 缺失日（8-05/8-12 无 stay）
    # 8-25..8-31 7 天窗：工作日 8-25,26,27,28,31=5 天，无缺失 → valid_days 应为这些中的
    # 30 天窗内周一~周五共 21 个工作日，缺 8-05/12 两天 → 19 个有效通勤日
    assert 15 <= cp["valid_days"] <= 21
    # 家 08:00 出发、公司 08:10 到达；gap 10min
    assert cp["depart_hhmm_median"] == "08:00"
    assert cp["arrive_hhmm_median"] == "08:10"
    assert 5 * 60 * 1000 <= cp["duration_ms_median"] <= 4 * 60 * 60 * 1000
    # 端点直距 ~2800m
    assert cp["endpoint_dist_m"] and 2500 <= cp["endpoint_dist_m"] <= 3200


def test_commute_profile_no_work_returns_none():
    conn = _make_db(with_work=False)
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    assert prof["commute_profile"] is None
    assert prof["home_work_rhythm"] is None


# ---------------------------------------------------------------------------
# home_work_rhythm
# ---------------------------------------------------------------------------

def test_home_work_rhythm_weekday_buckets_and_missing_days():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    hr = prof["home_work_rhythm"]
    assert hr is not None
    wd, we = hr["weekday"], hr["weekend"]
    # 工作日中位在家时长：8h 早上 + 6h 晚上 = 14h
    assert wd["home_ms"]["median_ms"] is not None
    assert 12 * 3600 * 1000 <= wd["home_ms"]["median_ms"] <= 16 * 3600 * 1000
    assert wd["work_ms"]["median_ms"] >= 6 * 3600 * 1000
    # 首次离家 08:00、最后回家 18:00
    assert wd["first_leave"]["median_hhmm"] == "08:00"
    assert wd["last_back"]["median_hhmm"] == "18:00"
    assert wd["calendar_basis"] == "weekday（周一至周五，不冒充法定工作日）" if False else hr["calendar_basis"] == "weekday（周一至周五，不冒充法定工作日）"
    # 周末在家时长 > 工作日、无公司样本
    assert we["home_ms"]["median_ms"] > wd["home_ms"]["median_ms"]
    assert we["work_ms"]["median_ms"] is None
    # 缺测日：MISSING_DAYS 8-05（完全无数据）/8-12（低质量无 quality 行）均为
    # 工作日，都被计入 missing_days（P3-2 补全口径后应==2）
    assert hr["missing_days"] >= 2   # 8-05（无数据）+ 8-12（低质量）
    assert hr["missing_ratio"] is not None and 0 <= hr["missing_ratio"] <= 1


def test_rhythm_evidence_components_recomputable():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    hr = prof["home_work_rhythm"]
    ev = hr["evidence"]
    assert set(ev) >= {"expected_bins", "observed_bins", "coverage_ratio",
                       "sample_score", "quality_score", "confidence_score"}
    assert 0 <= ev["coverage_ratio"] <= 1
    assert 0 <= ev["confidence_score"] <= 1


# ---------------------------------------------------------------------------
# scene_exposure / place_change
# ---------------------------------------------------------------------------

def test_scene_exposure_poi_l1_and_change_none_when_prev_zero():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    se_list = prof["scene_exposure"]
    assert se_list
    by = {(e["poi_l1"]): e for e in se_list}
    # 家=住宅，公司=办公，公园=公园 均应出现
    assert "住宅" in by
    assert "办公" in by
    assert "公园" in by
    home_e = by["住宅"]
    assert home_e["classification_basis"] == "current_place_semantics"
    assert home_e["cur_stay_ms"] > 0
    # 前 30 天窗口也有家/公司/公园 → change_pct 有具体值（约 0%）
    # 但旧窗口为 0 的场景由下方单测覆盖


def test_scene_exposure_change_pct_none_when_prev_zero():
    conn = _make_db()
    # 只留当前窗口内有、前窗口无的 poi_l1：把前窗口 stay 的 place 全删
    # 做法：直接把 park 的 last 停留之前的都删掉不可行——直接构造：把 devA 的
    # 前窗口 park stays 删掉比较麻烦。改为场景化：新增一个“夜宵”poi_l1 地点，仅当前窗后出现。
    cur = conn.cursor()
    cur.execute("PRAGMA user_version=2")
    # 加一个仅当前窗口出现的 place（first_seen 落在当前窗口）：poi_l1='餐饮'
    cur.execute(
        "INSERT INTO places VALUES (999,'devA','night_p','g',39.9600,116.4600,"
        "'未知',?,?,1,0,NULL,'某餐馆',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0.8,0.8,NULL,"
        "1,0,0.8,'geocode',NULL,'餐饮','','')",
        (_day_ts(_add_days(_AS_OF, -3)), _day_ts(_AS_OF)),
    )
    cur.execute(
        "INSERT INTO stays VALUES (997,'devA','night_p','g',?,?,5400000,"
        "39.9600,116.4600,39.9600,116.4600,39.9600,116.4600,3,25.0,?,40)",
        (_day_ts(_AS_OF, 19, 0), _day_ts(_AS_OF, 20, 30), _AS_OF),
    )
    conn.commit()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    by = {(e["poi_l1"]): e for e in prof["scene_exposure"]}
    night = by["餐饮"]
    # 餐饮当前窗口有、前窗口无 → change_pct=None（旧窗口 0），abs 有值
    assert night["prev_stay_ms"] == 0
    assert night["change_pct"] is None
    assert night["abs_change_ms"] > 0


def test_place_change_new_place_and_jaccard():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    pc = prof["place_change"]
    assert pc is not None
    # fresh 地点 first_seen 在当前窗口（8-21），应计为新 canonical
    assert "fresh_p" in pc["new_place_ids"]
    assert pc["new_place_count"] >= 1
    assert 0 <= pc["repeat_visit_ratio"] <= 1
    assert 0 <= pc["place_set_jaccard"] <= 1
    # 前窗口有 home/work/park → 重复到访率 > 0
    assert pc["repeat_visit_ratio"] > 0


# ---------------------------------------------------------------------------
# Evidence 单调性（可选：样本量下降 → 分数不升）
# ---------------------------------------------------------------------------

def test_evidence_score_bounds_all_metrics():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    # frequent_places 每项带 evidence
    for p in prof["frequent_places"]:
        ev = p["evidence"]
        assert 0 <= ev["confidence_score"] <= 1
        assert 0 <= ev["coverage_ratio"] <= 1
        assert ev["parse_validity_score"] >= 0 and ev["accuracy_known_score"] >= 0
    # spatial_extent
    ev = prof["spatial_extent"]["evidence"]
    assert 0 <= ev["confidence_score"] <= 1
    # commute 与 rhythm、scene、place_change
    assert 0 <= prof["commute_profile"]["evidence"]["confidence_score"] <= 1
    assert 0 <= prof["home_work_rhythm"]["evidence"]["confidence_score"] <= 1
    for e in prof["scene_exposure"]:
        assert 0 <= e["evidence"]["confidence_score"] <= 1
    assert 0 <= prof["place_change"]["evidence"]["confidence_score"] <= 1


def test_expected_bins_respect_first_seen_and_data_as_of():
    # 新设备：first_seen 靠近窗口末端 → expected_bins 显著小于满窗
    ws, we = sp._win_bounds(_AS_OF, 30)
    full_exp, full_days = sp._expected_bins(ws, we, None, we - 1)
    short_exp, short_days = sp._expected_bins(ws, we, ws + 5 * sp._DAY_MS, we - 1)
    assert full_exp > short_exp > 0
    assert full_days >= 29
    # 未来部分不计数：data_as_of 在窗口中间
    mid_exp, _ = sp._expected_bins(ws, we, None, ws + 10 * sp._DAY_MS)
    assert mid_exp < full_exp


# ---------------------------------------------------------------------------
# SQL 均带 device_id（devB 不污染）
# ---------------------------------------------------------------------------

def test_sql_scoped_by_device_id():
    conn = _make_db()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    # devB 只有 1 条 stay 且 place 在 b_only——若 SQL 漏带 device_id，frequent_places
    # 会出现 b_only / 无名地点；devA 结果中不应出现
    b_items = [p for p in prof["frequent_places"] if p["place_id"] == "b_only"]
    assert b_items == []
    names = [(p["place_id"], p["place_name"]) for p in prof["frequent_places"]]
    assert all(n[1] != "某处" for n in names)


# ---------------------------------------------------------------------------
# 90 天性能（冷查询）< 1s
# ---------------------------------------------------------------------------

def test_90day_cold_query_performance():
    conn = _make_db(days_back=95)
    t0 = time.perf_counter()
    prof = sp.build_spatial_profile(conn, "devA", _AS_OF)
    elapsed = time.perf_counter() - t0
    assert prof["frequent_places"], "应有数据"
    assert elapsed < 1.0, f"90 天冷查询耗时 {elapsed:.3f}s 未达标(<1s)"


# ---------------------------------------------------------------------------
# Evidence coverage：按有记录日 48-bin 中位数（缺失日不稀释，三窗单调不升）
# ---------------------------------------------------------------------------

def test_coverage_by_recorded_days_median_ignores_missing_days():
    # 30 天窗恰好 1 个缺测日（8-05）→ 该日无 quality 行；
    # 其余记录日均为满 48 bin。中位法应得到 1.0，而非被缺失日稀释到 ~0.967。
    daylist = [_add_day_str("2026-08-02", i) for i in range(30)]
    quality = {
        d: 48 for d in daylist if d != "2026-08-05"
    }  # 8-05 缺测
    cov = sp._coverage_by_recorded_days(daylist, quality)
    assert cov == 1.0
    # 部分日（如周末）采样不足 24 bin → 中位下降
    q2 = dict(quality)
    q2["2026-08-08"] = 24
    cov2 = sp._coverage_by_recorded_days(daylist, q2)
    assert cov2 == 1.0  # 中位数不受单个异常日影响
    q3 = {d: 48 for d in daylist[:15]}
    assert sp._coverage_by_recorded_days(daylist, q3) == 1.0


def _add_day_str(day: str, n: int) -> str:
    y, mo, d = (int(x) for x in day.split("-"))
    dt = date(y, mo, d) + timedelta(days=n)
    return dt.isoformat()


def test_build_evidence_coverage_uses_recorded_day_median():
    conn = _make_db()
    daylist = [_add_day_str("2026-08-02", i) for i in range(30)]
    qbd = {}
    for i, d in enumerate(daylist):
        qbd[d] = 48 if d != "2026-08-05" else 0
    daily_agg = {"observed_bins": 48 * 29, "points_total": 30, "points_valid": 30,
                 "accuracy_known": 30}
    ev = sp.build_evidence(
        requested_window_days=30, win_start=0, win_end=10**13,
        first_seen=None, data_as_of=None, daily_agg=daily_agg,
        sample_count=29, required_samples=20,
        daylist=daylist, quality_by_day=qbd,
    )
    # 记录日中位数 1.0（8-05 不稀释）；旧公式 sum(obs)/sum(expected) 会偏低
    assert ev["coverage_ratio"] == 1.0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# Task 12d：小时 × 地点分布（生活轨迹时段分布）
# ---------------------------------------------------------------------------

class TestTimeSpaceMatrix:
    def _matrix(self, clipped, daily_agg=None):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        places = {"home": {"label": "家", "place_id": "home"},
                  "work": {"label": "公司", "place_id": "work"}}
        win_s, win_e = _ts(2026, 8, 30), _ts(2026, 9, 1)
        return sp._time_space_matrix(
            conn, "devA", places, clipped, ["2026-08-30", "2026-08-31"],
            win_s, win_e, win_s, _ts(2026, 8, 31, 23, 59),
            daily_agg or {"points_total": 10, "points_valid": 10,
                          "accuracy_known": 10, "observed_bins": 48},
        )

    def test_threshold_and_cross_midnight(self):
        """>=15 分钟计入该地点；<15 分钟计无数据；跨午夜按自然日分摊。"""
        clipped = [
            # 家 08-30 22:00 → 08-31 02:00（跨午夜，已按自然日切两条）
            {"day": "2026-08-30", "start_ts": _ts(2026, 8, 30, 22, 0),
             "end_ts": _ts(2026, 8, 30, 23, 59, 59), "place_id": "home"},
            {"day": "2026-08-31", "start_ts": _ts(2026, 8, 31, 0, 0),
             "end_ts": _ts(2026, 8, 31, 2, 0), "place_id": "home"},
            # 公司 08-31 09:00-09:05：5 分钟 < 15 分钟阈值 → 该小时无数据
            {"day": "2026-08-31", "start_ts": _ts(2026, 8, 31, 9, 0),
             "end_ts": _ts(2026, 8, 31, 9, 5), "place_id": "work"},
            # 08-30 23:00-24:00 家/公司各 30 分钟 → hour23 同桶双计
            {"day": "2026-08-30", "start_ts": _ts(2026, 8, 30, 23, 0),
             "end_ts": _ts(2026, 8, 30, 23, 30), "place_id": "home"},
            {"day": "2026-08-30", "start_ts": _ts(2026, 8, 30, 23, 30),
             "end_ts": _ts(2026, 8, 30, 23, 59, 59), "place_id": "work"},
        ]
        out = self._matrix(clipped)
        by_h = {it["hour"]: it for it in out["hours"]}
        assert by_h[22]["home"] == 1 and by_h[22]["no_data"] == 1
        assert by_h[0]["home"] == 1          # 08-31 凌晨（跨午夜分摊）
        assert by_h[9]["work"] == 0 and by_h[9]["no_data"] == 2
        assert by_h[23]["home"] == 1 and by_h[23]["work"] == 1
        assert by_h[15]["no_data"] == 2 and by_h[15]["home"] == 0
        assert out["days"] == 2
        assert out["evidence"]["confidence_level"] in ("low", "medium", "high")

    def test_profile_includes_time_space_and_device_isolation(self):
        """build_spatial_profile 输出 time_space；无 tag 公园归"其他"；设备隔离。"""
        conn = _make_db()
        profile = sp.build_spatial_profile(conn, "devA", _AS_OF)
        ts = profile["time_space"]
        assert ts and len(ts["hours"]) == 24 and ts["days"] == 30
        h0 = ts["hours"][0]
        assert h0["hour"] == 0 and h0["home"] >= 25   # 每日 00-08 在家
        h10 = ts["hours"][10]
        assert h10["work"] >= 19                      # 工作日上午在公司
        assert h10["other"] >= 1                      # 周末公园 → 其他
        h12 = ts["hours"][12]
        assert h12["no_data"] >= 19                   # 工作日午休(12-13点)fixture 无定位 → 如实显示
        assert h12["home"] >= 8                       # 周末 12 点在家
        # 设备隔离：devB 只有一条 09:00-10:00 的未知地点 stay
        b = sp.build_spatial_profile(conn, "devB", _AS_OF)
        bh = {it["hour"]: it for it in b["time_space"]["hours"]}
        assert bh[23]["other"] == 1 and bh[23]["no_data"] == 29
        assert bh[0]["home"] == 0 and bh[0]["no_data"] == 30
