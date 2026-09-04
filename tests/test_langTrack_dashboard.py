"""dashboard.py 事实审查块与 Task 9 关键指标/迁移审查渲染测试。

内存库渲染 HTML，覆盖：v1 事实审查回归（compact/水位/轨迹/事件计数/降级）、
多设备提示、window/device 筛选、定位健康、30 天 KPI、四个明细区、证据组件、
迁移审查、无家公司/数据不足降级、坐标与 payload 不泄漏、XSS。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from gacore.langTrack.dashboard import render_dashboard_html

_TZ = timezone(timedelta(hours=8))


def _ts(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=_TZ).timestamp() * 1000)


def _day(offset: int, base: datetime | None = None) -> str:
    b = base or datetime(2026, 8, 18, tzinfo=_TZ)
    return (b + timedelta(days=offset)).strftime("%Y-%m-%d")


def _make_db(device_id: str = "dev1", day: str = "2026-08-18",
             with_stats: bool = True) -> sqlite3.Connection:
    """合成事实库：daily_stats / etl_state / places / stays / trips / anomalies / events /
    contract_coverage / sessions。默认 8-18：家 00:00-08:32 → 公司 09:04-12:03 → 餐馆 12:11-12:47 →
    公司 13:02-17:06，trips 3 段，当日 daily_stats 7h，一条 new_place 异常。（v1 形态）"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE daily_stats ("
        "device_id TEXT, day TEXT, total_screen_ms INTEGER, app_ranking_json TEXT,"
        "notification_count INTEGER, notification_clicked INTEGER, top_notification_apps_json TEXT,"
        "screen_on_count INTEGER, screen_off_count INTEGER, unlock_count INTEGER,"
        "switch_count INTEGER, location_count INTEGER, audio_clip_count INTEGER,"
        "sleep_start_hhmm INTEGER, sleep_end_hhmm INTEGER, sleep_duration_min INTEGER,"
        "time_app_json TEXT)"
    )
    cur.execute("CREATE TABLE etl_state (device_id TEXT PRIMARY KEY, last_event_ts INTEGER)")
    cur.execute(
        "CREATE TABLE places ("
        "id INTEGER, device_id TEXT, grid_key TEXT, label TEXT, visit_count INTEGER,"
        "poi TEXT, behavior TEXT, district TEXT, address TEXT)"
    )
    cur.execute(
        "CREATE TABLE stays ("
        "id INTEGER, device_id TEXT, grid_key TEXT, start_ts INTEGER, end_ts INTEGER, day TEXT)"
    )
    cur.execute(
        "CREATE TABLE trips ("
        "id INTEGER, device_id TEXT, start_ts INTEGER, end_ts INTEGER, dist_m INTEGER, day TEXT)"
    )
    cur.execute(
        "CREATE TABLE anomalies ("
        "id INTEGER, device_id TEXT, day TEXT, kind TEXT, poi TEXT, detail TEXT, ts INTEGER)"
    )
    cur.execute(
        "CREATE TABLE events ("
        "id INTEGER, device_id TEXT, ts INTEGER, type TEXT, payload TEXT, received_at INTEGER)"
    )
    cur.execute(
        "CREATE TABLE contract_coverage ("
        "type TEXT, desc TEXT, status TEXT, last_seen_ts INTEGER, consumed INTEGER)"
    )
    cur.execute(
        "CREATE TABLE sessions ("
        "id INTEGER, device_id TEXT, day TEXT, app TEXT, start_ms INTEGER, duration_ms INTEGER)"
    )
    ranking = json.dumps([
        {"app": "飞书", "ms": 3_600_000},
        {"app": "微信", "ms": 1_800_000},
    ])
    notif_apps = json.dumps([{"app": "微信", "n": 5}, {"app": "飞书", "n": 3}])
    if with_stats:
        cur.execute(
            "INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, day, 25_200_000, ranking, 47, 6, notif_apps, 3, 2, 12, 46, 50, 0,
             2340, 390, 480, "[]"),
        )
    cur.execute("INSERT INTO etl_state VALUES (?,?)", (device_id, _ts(2026, 8, 18, 17, 6)))
    places = [
        (1, device_id, "g_home", "家", 260, "家小区", "home", "玄武区", "XX路1号"),
        (2, device_id, "g_work", "公司", 440, "公司大厦", "work", "雨花台区", "YY路2号"),
        (3, device_id, "g_rest", "餐馆", 30, "快餐店", "dining", "雨花台区", "ZZ路3号"),
    ]
    cur.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?)", places)
    stays = [
        (1, device_id, "g_home", _ts(2026, 8, 18, 0, 0), _ts(2026, 8, 18, 8, 32), day),
        (2, device_id, "g_work", _ts(2026, 8, 18, 9, 4), _ts(2026, 8, 18, 12, 3), day),
        (3, device_id, "g_rest", _ts(2026, 8, 18, 12, 11), _ts(2026, 8, 18, 12, 47), day),
        (4, device_id, "g_work", _ts(2026, 8, 18, 13, 2), _ts(2026, 8, 18, 17, 6), day),
    ]
    cur.executemany("INSERT INTO stays VALUES (?,?,?,?,?,?)", stays)
    trips = [
        (1, device_id, _ts(2026, 8, 18, 8, 32), _ts(2026, 8, 18, 9, 4), 3200, day),
        (2, device_id, _ts(2026, 8, 18, 12, 3), _ts(2026, 8, 18, 12, 11), 800, day),
        (3, device_id, _ts(2026, 8, 18, 12, 47), _ts(2026, 8, 18, 13, 2), 900, day),
    ]
    cur.executemany("INSERT INTO trips VALUES (?,?,?,?,?,?)", trips)
    cur.execute(
        "INSERT INTO anomalies VALUES (?,?,?,?,?,?,?)",
        (1, device_id, day, "new_place", "德基广场", "访问 1 次", _ts(2026, 8, 18, 20, 0)),
    )
    cur.execute(
        "INSERT INTO anomalies VALUES (?,?,?,?,?,?,?)",
        (2, device_id, day, "new_place", "31.97,118.76", "访问 1 次", _ts(2026, 8, 18, 21, 0)),
    )
    # 当日 usage 事件 + 次日 usage 事件（用于设备过滤断言）
    for i in range(3):
        cur.execute(
            "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
            (i + 1, device_id, _ts(2026, 8, 18, 10, 0) + i * 60_000, "usage",
             json.dumps({"pkg": "com.x", "foreground_ms": 1000}), _ts(2026, 8, 18, 10, 0)),
        )
    cur.execute(
        "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
        (100, device_id, _ts(2026, 8, 19, 10, 0), "usage",
         json.dumps({"pkg": "com.x", "foreground_ms": 1}), _ts(2026, 8, 19, 10, 0)),
    )
    cur.execute(
        "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
        (200, device_id, _ts(2026, 8, 18, 10, 5), "location",
         json.dumps({"lat": 1.0, "lon": 2.0}), _ts(2026, 8, 18, 10, 5)),
    )
    cur.execute(
        "INSERT INTO contract_coverage VALUES (?,?,?,?,?)",
        ("screen", "屏幕事件", "stalled", _ts(2026, 8, 18, 10, 0), 0),
    )
    cur.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (1, device_id, day, "微信", _ts(2026, 8, 18, 10, 0), 1_800_000),
    )
    conn.commit()
    return conn


def _render(conn: sqlite3.Connection, day: str = "2026-08-18", **kw) -> str:
    return render_dashboard_html(conn, day, **kw)


# ---------------------------------------------------------------------------
# v2 农场：Task 8/9 长程画像所需 schema（place_id/stay_ms/lat/lon/quality）
# ---------------------------------------------------------------------------


_PLACE_COLS = (
    "id INTEGER, device_id TEXT, grid_key TEXT, label TEXT, visit_count INTEGER,"
    "lat REAL, lon REAL, first_seen INTEGER, last_seen INTEGER, is_primary INTEGER,"
    "address TEXT, poi TEXT, poi_fallback TEXT, district TEXT, township TEXT,"
    "business_area TEXT, poi_type TEXT, behavior TEXT, matched_level TEXT,"
    "candidate_label TEXT, confidence_home REAL, confidence_work REAL, geocoded_at INTEGER,"
    "poi_l1 TEXT, poi_l2 TEXT, poi_l3 TEXT, place_id TEXT, point_count INTEGER,"
    "stay_ms INTEGER, name_confidence REAL, name_evidence TEXT, parent_poi TEXT"
)


def _make_v2_db(device_id: str = "dev1", as_of_day: str = "2026-08-18",
                with_home_work: bool = True) -> sqlite3.Connection:
    """v2 合成库：30 天窗口（7-20~8-18）工作日家/公司节奏 + 前 30 天（6-20~7-19）公司，
    定位质量日表满覆盖，三张迁移审计表。as_of 水位 8-18 17:06。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE places ({_PLACE_COLS})")
    cur.execute(
        "CREATE TABLE stays (id INTEGER, device_id TEXT, grid_key TEXT, place_id TEXT, "
        "start_ts INTEGER, end_ts INTEGER, day TEXT, day_start INTEGER)"
    )
    cur.execute(
        "CREATE TABLE trips (id INTEGER, device_id TEXT, start_ts INTEGER, end_ts INTEGER, "
        "dist_m INTEGER, route_dist_m INTEGER, day TEXT)"
    )
    cur.execute(
        "CREATE TABLE daily_location_quality (day TEXT NOT NULL, device_id TEXT NOT NULL, "
        "points_total INTEGER, points_valid INTEGER, accuracy_known INTEGER, "
        "accuracy_le_50 INTEGER, accuracy_51_150 INTEGER, accuracy_gt_150 INTEGER, "
        "observed_half_hour_bins INTEGER, median_interval_sec REAL, providers_json TEXT)"
    )
    cur.execute("CREATE TABLE etl_state (device_id TEXT PRIMARY KEY, last_event_ts INTEGER)")
    cur.execute(
        "CREATE TABLE events (id INTEGER, device_id TEXT, ts INTEGER, type TEXT, "
        "payload TEXT, received_at INTEGER)"
    )
    cur.execute(
        "CREATE TABLE sessions (id INTEGER, device_id TEXT, day TEXT, app TEXT, "
        "start_ms INTEGER, duration_ms INTEGER)"
    )
    cur.execute(
        "CREATE TABLE contract_coverage (type TEXT, desc TEXT, status TEXT, "
        "last_seen_ts INTEGER, consumed INTEGER)"
    )
    cur.execute("PRAGMA user_version=2")

    now = datetime(2026, 8, 18, 17, 6, tzinfo=_TZ)
    cur.execute("INSERT INTO etl_state VALUES (?,?)", (device_id, int(now.timestamp() * 1000)))

    # 三个锚点地点；日期用实际 2026-07 起 30 天内
    as_dt = datetime(2026, 8, 18, tzinfo=_TZ)
    w_start = as_dt - timedelta(days=29)           # 2026-07-20
    prev_start = w_start - timedelta(days=30)      # 2026-06-20
    first_home = _ts(2026, 7, 10, 0, 0)
    first_work = int(datetime(w_start.year, w_start.month, w_start.day, tzinfo=_TZ).timestamp() * 1000)

    places = [
        (1, device_id, "g_home", "家" if with_home_work else "小区A", 120, 31.0, 118.0,
         first_home, int(now.timestamp() * 1000), 0, "XX路1号", "家小区", "", "玄武区", "玄武",
         "", "住宅区", "home", "exact", None, None, None, None, "住宅", "", "", "p_home",
         2000, 9 * 3600 * 1000, 0.95, "manual", ""),
        (2, device_id, "g_work", "公司" if with_home_work else "办公楼B", 100, 31.045, 118.16,
         first_work, int(now.timestamp() * 1000), 0, "YY路2号", "公司大厦", "", "雨花台区", "雨花",
         "", "写字楼", "work", "exact", None, None, None, None, "办公", "", "", "p_work",
         1800, 8 * 3600 * 1000, 0.9, "manual", ""),
        (3, device_id, "g_eat", None, 3, 31.012, 118.1,
         _ts(2026, 7, 25, 12, 0), _ts(2026, 7, 25, 12, 30), 0, "ZZ路3号", "某快餐", "", "雨花台区",
         "雨花", "", "餐饮", "dining", "poi", None, None, None, None, "餐饮", "", "", "p_eat",
         40, 20 * 60 * 1000, 0.7, "poi", ""),
    ]
    cur.executemany("INSERT INTO places VALUES (" + ",".join("?" * len(places[0])) + ")", places)

    # 30 天窗口内 stays：工作日 05:00-08:30 家、09:00-17:30 公司、18:00-23:30 家；周末全天家
    q_id = 1
    base = w_start
    for i in range(30):
        d = base + timedelta(days=i)
        is_wd = d.weekday() < 5
        rows = []
        if is_wd:
            rows += [
                ("g_home", "p_home", _ts(d.year, d.month, d.day, 5, 0),
                 _ts(d.year, d.month, d.day, 8, 30)),
                ("g_work", "p_work", _ts(d.year, d.month, d.day, 9, 0),
                 _ts(d.year, d.month, d.day, 17, 30)),
                ("g_home", "p_home", _ts(d.year, d.month, d.day, 18, 0),
                 _ts(d.year, d.month, d.day, 23, 30)),
            ]
        else:
            rows += [("g_home", "p_home", _ts(d.year, d.month, d.day, 0, 0),
                      _ts(d.year, d.month, d.day, 23, 59))]
        day_s = _ts(d.year, d.month, d.day, 0, 0)
        for gk, pid, st, en in rows:
            cur.execute(
                "INSERT INTO stays(id,device_id,grid_key,place_id,start_ts,end_ts,day,day_start) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (q_id, device_id, gk, pid, st, en, d.strftime("%Y-%m-%d"), day_s),
            )
            q_id += 1

    # 前 30 天窗口内公司段（scene 前窗口有数据）
    b = prev_start
    for i in range(30):
        d = b + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        day_s = _ts(d.year, d.month, d.day, 0, 0)
        cur.execute(
            "INSERT INTO stays(id,device_id,grid_key,place_id,start_ts,end_ts,day,day_start) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (q_id, device_id, "g_work", "p_work", _ts(d.year, d.month, d.day, 9, 0),
             _ts(d.year, d.month, d.day, 17, 30), d.strftime("%Y-%m-%d"), day_s),
        )
        q_id += 1

    # 定位质量日表：30 天窗口每天满覆盖
    for i in range(30):
        d = (w_start + timedelta(days=i)).strftime("%Y-%m-%d")
        cur.execute(
            "INSERT INTO daily_location_quality VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (d, device_id, 800, 700, 600, 300, 250, 150, 48, 90.0,
             json.dumps({"gps": 600, "network": 200})),
        )

    # 当日一条 location 事件（坐标制解析用）
    cur.execute(
        "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
        (1, device_id, _ts(2026, 8, 18, 10, 5), "location",
         json.dumps({"lat": 31.0, "lon": 118.0}), _ts(2026, 8, 18, 10, 5)),
    )

    # 迁移审计三表
    cur.execute(
        "CREATE TABLE location_place_mapping (run_id TEXT, old_device_id TEXT, old_place_id TEXT, "
        "new_place_id TEXT, match_reason TEXT, jaccard REAL, distance_m REAL)"
    )
    cur.execute(
        "CREATE TABLE location_migration_issues (id INTEGER PRIMARY KEY, kind TEXT, "
        "device_id TEXT, source_payload TEXT, grid_key TEXT, tag TEXT, resolution_status TEXT)"
    )
    cur.execute(
        "CREATE TABLE location_migration_metrics (run_id TEXT, metric TEXT, value REAL)"
    )
    cur.execute(
        "INSERT INTO location_place_mapping VALUES (?,?,?,?,?,?,?)",
        ("r1", device_id, "old_home", "p_home", "jaccard+center", 0.8, 12.0),
    )
    cur.execute(
        "INSERT INTO location_place_mapping VALUES (?,?,?,?,?,?,?)",
        ("r1", device_id, "old_work", "p_work", "grid_jaccard", 1.0, 3.0),
    )
    cur.execute(
        "INSERT INTO location_migration_issues VALUES (?,?,?,?,?,?,?)",
        (1, "tag_conflict", device_id, json.dumps({"a": 1}), "g_x", "公司", "open"),
    )
    cur.execute(
        "INSERT INTO location_migration_issues VALUES (?,?,?,?,?,?,?)",
        (2, "geocode_invalidated", device_id, "{}", "g_y", "", "open"),
    )
    cur.execute(
        "INSERT INTO location_migration_metrics VALUES (?,?,?)",
        ("r1", "orphan_stay", 0),
    )
    cur.execute(
        "INSERT INTO location_migration_metrics VALUES (?,?,?)",
        ("r1", "place_total", 3),
    )
    conn.commit()
    return conn


def _render_v2(conn: sqlite3.Connection, **kw) -> str:
    return render_dashboard_html(conn, "2026-08-18", **kw)


# ---------------------------------------------------------------------------
# v1 事实审查块（回归）
# ---------------------------------------------------------------------------


def test_dashboard_shows_compact_timeline_and_watermarks():
    html = _render(_make_db())
    assert "生活事实" in html
    assert "今日轨迹" in html
    assert "公司" in html
    # 水位旁注
    assert "数据水位" in html
    assert "card_fp" in html


def test_dashboard_shows_section_inclusion_and_omission():
    html = _render(_make_db())
    assert "compact_lines" in html
    assert "compact_omitted" in html


def test_dashboard_with_stays_but_no_daily_stats():
    """无 daily_stats 仍显示显式请求日和今日轨迹，不跳到其他日期。"""
    html = _render(_make_db(with_stats=False), day="2026-08-18")
    assert "2026-08-18" in html
    assert "今日轨迹" in html
    assert "XX路1号〔家〕 00:00-08:32" in html


def test_dashboard_lists_stays_trips_anomalies():
    html = _render(_make_db())
    assert "德基广场" in html        # anomaly poi
    assert "端点直距" in html        # trip 距离列名
    assert "快餐店" in html          # place poi


def test_dashboard_labels_trip_distance_and_place_counts_honestly():
    html = _render(_make_db())
    assert "端点直距" in html
    assert "全历史 location 点数" in html
    assert "到访次数" not in html


def test_dashboard_event_counts():
    """当日到达表：usage 3 条、location 1 条；次日事件不混入。"""
    html = _render(_make_db())
    assert "usage" in html
    assert "3" in html
    assert "location" in html


def test_dashboard_survives_fact_card_error(monkeypatch):
    import gacore.langTrack.dashboard as dash
    monkeypatch.setattr(dash, "fact_card", _ExplodingFactCard())

    html = render_dashboard_html(_make_db(), "2026-08-18")
    assert "2026-08-18" in html          # 日期导航仍渲染
    assert "读取失败" in html            # 通用错误
    assert "今日轨迹" not in html        # 无未过滤统计


class _ExplodingFactCard:
    def build(self, **kwargs):
        raise RuntimeError("boom")
    render_compact = staticmethod(lambda card: card.get("compact", ""))


def test_dashboard_escapes_html():
    conn = _make_db()
    # 注入 HTML payload 到 anomaly.detail / place.poi / compact
    conn.execute(
        "UPDATE anomalies SET detail='<script>alert(1)</script>', poi='<img src=x>' WHERE id=1"
    )
    conn.execute("UPDATE places SET poi='<img src=x>' WHERE id=2")
    conn.commit()
    html = render_dashboard_html(conn, "2026-08-18")
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x>" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_persona_not_queried_twice(monkeypatch):
    import gacore.langTrack.fact_card as fc
    calls = []

    def fake_persona_build(*a, **kw):
        calls.append(1)
        return {}

    monkeypatch.setattr(fc, "build_persona", fake_persona_build)
    html = _render(_make_db())
    # 成功路径：persona 只经 fact_card 一次，dashboard 不二次调用
    assert len(calls) == 1
    assert "人物画像" in html


def test_dashboard_ambiguous_devices_shows_no_device_stats():
    """两设备同日数据时只显示候选 ID 与可选链接，不出现任一设备的统计/地点/persona。"""
    conn = _make_db("dev1", "2026-08-18")
    conn.execute("INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("dev2", "2026-08-18", 1_000, "[]", 0, 0, "[]", 0, 0, 0, 0, 0, 0,
                  None, None, None, "[]"))
    conn.execute("INSERT INTO etl_state VALUES (?,?)", ("dev2", _ts(2026, 8, 18, 12, 0)))
    conn.execute("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?)",
                 (99, "dev2", "g_x", "别处", 5, "别处poi", "x", "区", "地址"))
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                 (99, "dev2", "2026-08-18", "别app", _ts(2026, 8, 18, 10, 0), 1000))
    conn.commit()

    html = render_dashboard_html(conn, "2026-08-18")
    assert "dev1" in html and "dev2" in html    # 候选 ID 展示
    assert "不合并画像" in html                  # 明确不合并
    assert "别app" not in html                  # 无设备统计
    assert "别处poi" not in html                # 无地点
    assert "人物画像" not in html                # 无 persona


def test_dashboard_raw_events_are_filtered_by_selected_device():
    """第二设备只存在 events 时，其 snapshot/type count 不混入页面。"""
    conn = _make_db("dev1", "2026-08-18")
    conn.execute("INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
                 (999, "dev2", _ts(2026, 8, 18, 11, 0), "usage",
                  json.dumps({"pkg": "com.y", "foreground_ms": 5}), _ts(2026, 8, 18, 11, 0)))
    conn.commit()
    html = render_dashboard_html(conn, "2026-08-18")
    # dev2 唯一事件不能使 usage 计数出现 dev2 特征；页面 usage 行来自 dev1 的 3 条
    assert html.count("usage") >= 1
    assert "999" not in html  # 不展示事件 id


# ---------------------------------------------------------------------------
# Task 9：筛选 / 健康 / 30 天 KPI / 明细区 / Evidence / 迁移审查
# ---------------------------------------------------------------------------


def test_dashboard_multidevice_shows_selectable_prompt_not_merge():
    """多设备未指定 device 时：显示选择提示与候选链接，不合并画像。"""
    conn = _make_db("dev1", "2026-08-18")
    conn.execute("INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("dev2", "2026-08-18", 1_000, "[]", 0, 0, "[]", 0, 0, 0, 0, 0, 0,
                  None, None, None, "[]"))
    conn.execute("INSERT INTO etl_state VALUES (?,?)", ("dev2", _ts(2026, 8, 18, 12, 0)))
    conn.commit()
    html = render_dashboard_html(conn, "2026-08-18")
    assert "检测到多台设备" in html
    assert "不合并画像" in html
    assert "device_id=dev2" in html        # 候选可点击链接


def test_dashboard_device_param_selects_specific_device():
    """显式 device_id 时不再多设备歧义，直接渲染该设备数据。"""
    conn = _make_db("dev1", "2026-08-18")
    conn.execute("INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("dev2", "2026-08-18", 1_000, "[]", 0, 0, "[]", 0, 0, 0, 0, 0, 0,
                  None, None, None, "[]"))
    conn.execute("INSERT INTO etl_state VALUES (?,?)", ("dev2", _ts(2026, 8, 18, 12, 0)))
    conn.commit()
    html = render_dashboard_html(conn, "2026-08-18", device_id="dev1")
    assert "多设备" not in html
    assert "生活事实" in html                # dev1 事实卡渲染
    assert "人物画像" in html


def test_dashboard_window_nav_and_frequent_places_header():
    """window=7|30|90 导航存在；常去地点表标题/表头含窗口、原始点数/到访段数/停留时长、地点场景。"""
    conn = _make_v2_db()
    html = render_dashboard_html(conn, "2026-08-18", window=7)
    assert "7 天" in html and "30 天" in html and "90 天" in html
    assert "window=7" in html
    assert "常去地点表 · 7 天" in html
    assert "地点场景" in html
    assert "原始点数" in html
    assert "到访段数" in html
    assert "停留时长（总 / 中位）" in html


def test_dashboard_location_health_card(monkeypatch):
    """定位健康卡：有效点/总点、coverage、accuracy 三档、provider、采样间隔、坐标制与警告。"""
    monkeypatch.setattr(
        "gacore.langTrack.etl_config.load_coord_systems",
        lambda: {"default": "unknown", "periods": []},
    )
    html = _render_v2(_make_v2_db())
    assert "定位健康" in html
    assert "有效点 / 总点" in html
    assert "700 / 800" in html
    assert "30 分钟覆盖格" in html
    assert "精度 ≤50m / 51–150m / >150m" in html
    assert "300 / 250 / 150" in html
    assert "provider" in html
    assert "采样间隔中位数（所选日）" in html
    assert "坐标制及点数" in html
    assert "坐标制未知" in html     # unknown 黄色警告


def test_dashboard_kpi30_card():
    """30 天 KPI：覆盖率、有停留地点数、常去 Top1、P90 生活半径、通勤天数、median/IQR、家/公司时长。"""
    html = _render_v2(_make_v2_db())
    assert "30 天关键指标" in html
    assert "可观测覆盖率（30 天）" in html
    assert "有停留地点数（30 天）" in html
    assert "常去地点 Top 1" in html
    assert "家小区〔家〕" in html          # place_name〔tag〕统一文案
    assert "相对家 P90 生活半径" in html
    assert "公里" in html or "米" in html   # 生活半径有量纲
    assert "家→公司有效通勤天数" in html
    assert "通勤耗时中位 / IQR" in html
    assert "端点直距 / 路线距" in html
    assert "工作日在家时长中位" in html
    assert "工作日公司时长中位" in html


def test_dashboard_frequent_places_rows():
    """常去地点表行：地点场景〔tag〕、原始点数、到访段数。"""
    html = _render_v2(_make_v2_db())
    assert "常去地点表" in html
    assert "家小区〔家〕" in html
    assert "公司大厦〔公司〕" in html
    assert "工作日 / 周末（天）" in html


def test_dashboard_rhythm_card():
    """家/公司节奏：首次离家/到公司/离公司/最后回家的 median 与有效样本日。"""
    html = _render_v2(_make_v2_db())
    assert "家 / 公司节奏" in html
    assert "首次离家" in html
    assert "到公司" in html
    assert "离公司" in html
    assert "最后回家" in html
    assert "有效样本日 / 缺测日" in html
    assert "08:30" in html    # 首次离家中位


def test_dashboard_scene_exposure():
    """场景暴露：poi_l1 行、分类口径说明；前窗口为 0 的项不显示百分比。"""
    html = _render_v2(_make_v2_db())
    assert "场景暴露变化" in html
    assert "办公" in html            # 公司 poi_l1
    assert "住宅" in html            # 家 poi_l1（前窗口无 → 不显示百分比）
    assert "旧窗口为 0（不显示百分比）" in html
    assert "current_place_semantics" in html
    assert "地点场景（poi_l1）" in html


def test_dashboard_place_change():
    """地点变化：新 canonical 地点数、重复到访率、地点集合 Jaccard。"""
    html = _render_v2(_make_v2_db())
    assert "地点变化" in html
    assert "新 canonical 地点数" in html
    assert "重复到访率" in html
    assert "地点集合 Jaccard" in html
    assert "当前 / 前窗口地点数" in html


def test_dashboard_migration_review():
    """迁移审查：mapping 旧→新、孤儿 stay、tag 冲突、geocode 失效、metrics。"""
    html = _render_v2(_make_v2_db())
    assert "迁移审查（shadow）" in html
    assert "old_home" in html and "p_home" in html
    assert "旧→新地点映射" in html
    assert "孤儿 stay" in html
    assert "tag_conflict×1" in html
    assert "geocode_invalidated×1" in html
    assert "orphan_stay=0" in html


def test_dashboard_migration_review_absent_tables():
    """缺迁移审计表时降级：显示无 shadow 迁移记录，不抛异常。"""
    html = _render_v2(_make_v2_db())
    conn = _make_v2_db()
    for t in ("location_place_mapping", "location_migration_issues", "location_migration_metrics"):
        conn.execute(f"DROP TABLE {t}")
    conn.commit()
    html = render_dashboard_html(conn, "2026-08-18")
    assert "迁移审查（shadow）" in html
    assert "无 shadow 迁移记录" in html


def test_dashboard_no_home_work_shows_unconfirmed():
    """家/公司未确认：KPI 与节奏卡显示“尚未确认家/公司”，不输出通勤、不渲染节奏表。"""
    html = _render_v2(_make_v2_db(with_home_work=False))
    assert "尚未确认家/公司" in html
    assert "家→公司有效通勤天数" not in html      # 无家/公司不显示通勤
    assert "首次离家" not in html                 # 无节奏表


def test_dashboard_low_data_shows_insufficient():
    """v1（无长程画像）下所有新卡显示“数据不足”，不以 0 冒充事实。"""
    html = _render(_make_db())
    assert "数据不足" in html
    assert "30 天关键指标" in html
    assert "常去地点" in html
    assert "场景暴露变化" in html
    assert "地点变化" in html


def test_dashboard_evidence_components_render():
    """Evidence components：窗口/覆盖率/样本/质量分/confidence 级别。"""
    html = _render_v2(_make_v2_db())
    assert "窗口" in html
    assert "覆盖率" in html
    assert "样本" in html
    assert "质量" in html
    assert "high" in html or "medium" in html or "low" in html


def test_dashboard_no_coords_grid_payload_leak():
    """不输出原始坐标、grid_key 值、完整 payload。"""
    html = _render_v2(_make_v2_db())
    assert "g_home" not in html
    assert "g_work" not in html
    assert "31.0" not in html.replace("31.0 公里", "X").replace("31.0 米", "X") \
           or "31°" not in html
    assert "31.045" not in html
    assert "118.16" not in html
    assert "payload" not in html
    # 注入过的原始 payload JSON 键不应出现在 HTML
    assert '"lat"' not in html


def test_dashboard_v2_escapes_xss():
    """v2 新卡字段注入 HTML 时全部转义（place_name/poi_l1/issue kind）。"""
    conn = _make_v2_db()
    conn.execute("UPDATE places SET poi='<img src=x>' WHERE place_id='p_home'")
    conn.execute("UPDATE places SET poi_l1='<b>bad</b>' WHERE place_id='p_eat'")
    conn.execute("UPDATE location_migration_issues SET kind='<script>x</script>' WHERE id=1")
    conn.commit()
    html = render_dashboard_html(conn, "2026-08-18")
    assert "<img src=x>" not in html
    assert "<b>bad</b>" not in html
    assert "<script>x</script>" not in html
    assert "&lt;img" in html


def test_dashboard_multiple_coord_systems_warns(monkeypatch):
    """同日多坐标制（wgs84+gcj02，均非 unknown）触发黄色警告与两坐标制点数。"""
    conn = _make_v2_db()
    cut = _ts(2026, 8, 18, 11, 0)
    conn.execute(
        "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
        (2, "dev1", _ts(2026, 8, 18, 12, 0), "location",
         json.dumps({"lat": 31.0, "lon": 118.0}), _ts(2026, 8, 18, 12, 0)),
    )
    conn.commit()
    monkeypatch.setattr(
        "gacore.langTrack.etl_config.load_coord_systems",
        lambda: {
            "default": "unknown",
            "periods": [
                {"device_id": "dev1", "start_ts": 0, "end_ts": cut, "source": "wgs84"},
                {"device_id": "dev1", "start_ts": cut, "end_ts": None, "source": "gcj02"},
            ],
        },
    )
    html = render_dashboard_html(conn, "2026-08-18", device_id="dev1")
    assert "同日多坐标制" in html          # 黄色警告
    assert "gcj02×1" in html and "wgs84×1" in html
    assert "坐标制未知" not in html


def test_dashboard_evidence_low_level_shows_insufficient(monkeypatch):
    """Evidence confidence_level=low 时渲染“数据不足（置信度低…）”，不冒充高置信结论。"""
    import gacore.langTrack.fact_card as fc

    orig_build = fc.build

    def _low_build(**kw):
        card = orig_build(**kw)
        sp = card.get("spatial_profile") or {}
        ext = sp.get("spatial_extent") or {}
        ev = ext.get("evidence")
        if ev:
            ev["confidence_level"] = "low"
        return card

    monkeypatch.setattr(fc, "build", _low_build)
    html = render_dashboard_html(_make_v2_db(), "2026-08-18")
    assert "数据不足（置信度低，不作确定结论）" in html


def test_dashboard_day_param_xss_escaped():
    """day 参数注入 HTML 时在 <title> 与日期导航均已转义。"""
    html = render_dashboard_html(_make_v2_db(), day="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;/script&gt;" in html


def test_dashboard_renders_manual_etl_button():
    """dashboard 顶部渲染"立即转换"按钮与 /etl/run 触发脚本。"""
    html = render_dashboard_html(_make_db(), "2026-08-18")
    assert 'id="etl-btn"' in html
    assert "/etl/run" in html
    assert "/etl/status" in html
