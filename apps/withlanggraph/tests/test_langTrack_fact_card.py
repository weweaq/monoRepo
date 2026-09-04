"""fact_card.py 单元测试：纯合成内存库，断言 FactCard 契约 / 数据水位 / compact 段落 / 降级。

运行需 PYTHONPATH=src（见文件末尾 sys.path）。不依赖真实 data/langTrack.db。

覆盖（§2.6 Task1 清单）：
- 双出口：build 完整卡 + render_compact 只读
- 数据水位：etl_state.last_event_ts 优先，fallback=stays/trips 最大 end_ts；不扫 events
- current_known：覆盖 cutoff 闭区间命中的最后一段 stay
- stays/trips 时间窗相交裁剪；trip 匹配前后最近 stay
- compact：600 字预算、section 优先级整段省略、轨迹 260 字内折叠
- 降级：无库 / 缺表 / 异常 / 多设备歧义 / 未来日 → available=False
- 维测日志：built / degraded，日志失败不抛
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


import gacore.langTrack.fact_card as fc

_TZ = timezone(timedelta(hours=8))


def _ts(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=_TZ).timestamp() * 1000)


def _make_db(device_id: str = "dev1", day: str = "2026-08-18", with_device_col: bool = True,
             include_places_semantic: bool = True) -> sqlite3.Connection:
    """合成 v2 事实库（Task 7 契约）：places 带 place_id/命名证据，label∈{家,公司,未知}。

    默认 8-18 一整天（对齐文档示例）：家 00:00-08:32 → 公司 09:04-12:03 → 餐馆 12:11-12:47 →
    公司 13:02-17:06，trips 3 段，当日 daily_stats 7h 屏幕，两条 new_place 异常（一条含网格 poi）。
    v2：user_version=2，stays.place_id / trips.from,to_place_id JOIN；place_cells 成员网格展开；
    附 daily_location_quality 日行与 1 条 place_tag_conflicts（tag_conflict_count=1）。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA user_version=2")
    if with_device_col:
        cur.execute(
            "CREATE TABLE daily_stats ("
            "device_id TEXT, day TEXT, total_screen_ms INTEGER, app_ranking_json TEXT,"
            "notification_count INTEGER, notification_clicked INTEGER, top_notification_apps_json TEXT,"
            "screen_on_count INTEGER, screen_off_count INTEGER, unlock_count INTEGER,"
            "switch_count INTEGER, location_count INTEGER, audio_clip_count INTEGER,"
            "sleep_start_hhmm INTEGER, sleep_end_hhmm INTEGER, sleep_duration_min INTEGER,"
            "time_app_json TEXT)"
        )
    else:
        cur.execute(
            "CREATE TABLE daily_stats ("
            "day TEXT, total_screen_ms INTEGER, app_ranking_json TEXT)"
        )

    cur.execute("CREATE TABLE etl_state (device_id TEXT PRIMARY KEY, last_event_ts INTEGER)")
    cur.execute(
        "CREATE TABLE places ("
        "id INTEGER, device_id TEXT, place_id TEXT, grid_key TEXT, label TEXT,"
        "visit_count INTEGER, point_count INTEGER, stay_ms INTEGER,"
        "poi TEXT, address TEXT, district TEXT, behavior TEXT,"
        "name_confidence REAL, name_evidence TEXT, parent_poi TEXT,"
        "township TEXT, business_area TEXT)"
    )
    cur.execute(
        "CREATE TABLE stays ("
        "id INTEGER, device_id TEXT, place_id TEXT, grid_key TEXT,"
        "start_ts INTEGER, end_ts INTEGER, day TEXT, avg_accuracy_m INTEGER)"
    )
    cur.execute(
        "CREATE TABLE trips ("
        "id INTEGER, device_id TEXT, start_ts INTEGER, end_ts INTEGER, dist_m INTEGER, day TEXT,"
        "from_place_id TEXT, to_place_id TEXT, route_dist_m INTEGER)"
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
    # v2 成员网格 / Task 6 质量与冲突表
    cur.execute(
        "CREATE TABLE place_cells (device_id TEXT, place_id TEXT, grid_key TEXT)"
    )
    cur.execute(
        "CREATE TABLE daily_location_quality ("
        "day TEXT, device_id TEXT, total_points INTEGER, accuracy_p25 REAL, accuracy_median REAL,"
        "gps_sampling_ms INTEGER, network_fallback_rate REAL, created_at INTEGER, updated_at INTEGER)"
    )
    cur.execute(
        "CREATE TABLE place_tag_conflicts ("
        "device_id TEXT, place_id TEXT, label_a TEXT, label_b TEXT)"
    )

    ranking = json.dumps([
        {"app": "飞书", "ms": 3_600_000},
        {"app": "微信", "ms": 1_800_000},
        {"app": "Edge", "ms": 600_000},
    ])
    notif_apps = json.dumps([{"app": "微信", "n": 5}, {"app": "飞书", "n": 3}])
    if with_device_col:
        cur.execute(
            "INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, day, 25_200_000, ranking, 47, 6, notif_apps, 3, 2, 12, 46, 50, 0,
             2340, 390, 480, "[]"),
        )
    else:
        cur.execute(
            "INSERT INTO daily_stats VALUES (?,?,?)",
            (day, 25_200_000, ranking),
        )
    # ETL 水位：17:06（当日未跑完）
    cur.execute("INSERT INTO etl_state VALUES (?,?)", (device_id, _ts(2026, 8, 18, 17, 6)))

    # 真实 poi + 人工 label（label∈{家,公司,未知}）：命名证据靠 name_confidence 决定粒度
    places = [
        # id, device_id, place_id, grid_key, label, visit_count, point_count, stay_ms,
        #   poi, address, district, behavior, name_confidence, name_evidence, parent_poi, township, business_area
        (1, device_id, "p_home", "g_home", "家", 260, 260, 5_000_000,
         "家小区", "XX路1号", "玄武区", "home", 0.0, "", "", "", ""),
        (2, device_id, "p_work", "g_work", "公司", 440, 440, 7_000_000,
         "公司大厦", "YY路2号", "雨花台区", "work", 0.0, "", "", "", ""),
        (3, device_id, "p_rest", "g_rest", "未知", 30, 30, 500_000,
         "快餐店", "ZZ路3号", "雨花台区", "dining", 0.80, "高德POI", "", "", ""),
        (4, device_id, "p_mall", "g_mall", "未知", 8, 8, 300_000,
         "德基广场", "AA路4号", "玄武区", "shopping", 0.0, "", "", "", ""),
    ]
    if include_places_semantic:
        cur.executemany(
            "INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", places
        )
    else:
        cur.executemany(
            "INSERT INTO places(id,device_id,place_id,grid_key,label,visit_count,point_count,stay_ms) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7]) for p in places],
        )
    # 成员网格（含代表网格自身）→ by_grid 展开路径
    cur.executemany(
        "INSERT INTO place_cells VALUES (?,?,?)",
        [(device_id, p_id, gk) for (_, _, p_id, gk, *_rest) in places],
    )

    stays = [
        (1, device_id, "p_home", "g_home", _ts(2026, 8, 18, 0, 0), _ts(2026, 8, 18, 8, 32), day, 18),
        (2, device_id, "p_work", "g_work", _ts(2026, 8, 18, 9, 4), _ts(2026, 8, 18, 12, 3), day, 25),
        (3, device_id, "p_rest", "g_rest", _ts(2026, 8, 18, 12, 11), _ts(2026, 8, 18, 12, 47), day, 30),
        (4, device_id, "p_work", "g_work", _ts(2026, 8, 18, 13, 2), _ts(2026, 8, 18, 17, 6), day, 20),
    ]
    cur.executemany("INSERT INTO stays VALUES (?,?,?,?,?,?,?,?)", stays)
    trips = [
        (1, device_id, _ts(2026, 8, 18, 8, 32), _ts(2026, 8, 18, 9, 4), 3200, day, "p_home", "p_work", 3400),
        (2, device_id, _ts(2026, 8, 18, 12, 3), _ts(2026, 8, 18, 12, 11), 800, day, "p_work", "p_rest", None),
        (3, device_id, _ts(2026, 8, 18, 12, 47), _ts(2026, 8, 18, 13, 2), 900, day, "p_rest", "p_work", None),
    ]
    cur.executemany("INSERT INTO trips VALUES (?,?,?,?,?,?,?,?,?)", trips)
    # 两条新地点异常：poi 真名 + poi 网格（网格应被 compact 过滤）
    cur.execute(
        "INSERT INTO anomalies VALUES (?,?,?,?,?,?,?)",
        (1, device_id, day, "new_place", "德基广场", "访问 1 次", _ts(2026, 8, 18, 20, 0)),
    )
    cur.execute(
        "INSERT INTO anomalies VALUES (?,?,?,?,?,?,?)",
        (2, device_id, day, "new_place", "31.97,118.76", "访问 1 次", _ts(2026, 8, 18, 21, 0)),
    )
    # 凌晨音频：6 条 00:00 起
    for i in range(6):
        cur.execute(
            "INSERT INTO events(id,device_id,ts,type,payload,received_at) VALUES (?,?,?,?,?,?)",
            (i + 1, device_id, _ts(2026, 8, 18, 0, 0) + i * 60_000, "audio_env",
             json.dumps({"is_silent": True}), _ts(2026, 8, 18, 0, 0)),
        )
    cur.execute(
        "INSERT INTO contract_coverage VALUES (?,?,?,?,?)",
        ("screen", "屏幕事件", "stalled", _ts(2026, 8, 18, 10, 0), 0),
    )
    # Task 6 §3.2：当日定位质量 + 1 条人工 tag 冲突
    cur.execute(
        "INSERT INTO daily_location_quality VALUES (?,?,?,?,?,?,?,?,?)",
        (day, device_id, 1024, 12.0, 20.0, 5000, 0.15,
         _ts(2026, 8, 18, 17, 10), _ts(2026, 8, 18, 17, 10)),
    )
    cur.execute(
        "INSERT INTO place_tag_conflicts VALUES (?,?,?,?)",
        (device_id, "p_mall", "未知", "公司"),
    )
    conn.commit()
    return conn



class _StubLogger:
    """记录调用的假 logger；build 只依赖 info/warning 接口。"""

    def __init__(self):
        self.infos = []
        self.warnings = []

    def info(self, message, **fields):
        self.infos.append((message, fields))

    def warning(self, message, **fields):
        self.warnings.append((message, fields))


# ---------------------------------------------------------------------------
# build 基本契约
# ---------------------------------------------------------------------------


def test_build_returns_full_fact_card_fields():
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="full", outlet="test")
    assert card["day"] == "2026-08-18"
    assert card["device_id"] == "dev1"
    assert card["available"] is True
    assert card["has_facts"] is True
    assert card["screen_ms"] == 25_200_000
    assert card["screen_hours"] == round(25_200_000 / 3600000, 2)
    assert card["top_apps"][0]["app"] == "飞书"
    assert card["notification_count"] == 47
    assert card["notification_clicked"] == 6
    assert card["midnight_audio_n"] == 6
    assert "疑似熬夜" in card["sleep_signal"]
    assert card["persona"] != {}


def test_build_db_path_opens_connection(tmp_path):
    """db_path 打开连接。"""
    db = tmp_path / "langTrack.db"
    conn = _make_db()
    disk = sqlite3.connect(str(db))
    conn.backup(disk)
    disk.close()
    conn.close()
    card = fc.build(db_path=str(db), day="2026-08-18", device_id="dev1")
    assert card["available"] is True


def test_build_no_conn_no_db_returns_degraded(tmp_path):
    """无库 → 降级空卡，不抛。"""
    card = fc.build(db_path=str(tmp_path / "nope.db"), day="2026-08-18")
    assert card["available"] is False
    assert card["has_facts"] is False
    assert card["persona"] == {}
    assert card["compact"] == ""


def test_build_default_day_is_today():
    conn = _make_db()
    card = fc.build(conn=conn, device_id="dev1")
    assert card["day"] == datetime.now(_TZ).strftime("%Y-%m-%d")
    assert card["available"] is False  # 今天无数据


def test_build_ambiguous_multi_device_degrades():
    conn = _make_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO etl_state VALUES (?,?)", ("dev2", _ts(2026, 8, 18, 12, 0))
    )
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18")
    assert card["ambiguous_device"] is True
    assert "dev1" in card["candidate_device_ids"]
    assert card["available"] is False
    assert card["compact"] == ""


def test_build_device_filter_ignores_other_device():
    conn = _make_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO etl_state VALUES (?,?)", ("dev2", _ts(2026, 8, 18, 12, 0))
    )
    cur.execute(
        "INSERT INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev2", "2026-08-18", 1_000_000, "[]", 0, 0, "[]", 0, 0, 0, 0, 0, 0, None, None, None, "[]"),
    )
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["device_id"] == "dev1"
    assert card["screen_ms"] == 25_200_000  # dev2 数据不混入


def test_build_legacy_no_device_col():
    """旧库 daily_stats 无 device_id 列：整表视为单设备仍可用。"""
    conn = _make_db(with_device_col=False)
    card = fc.build(conn=conn, day="2026-08-18")
    assert card["available"] is True
    assert card["screen_ms"] == 25_200_000


def test_build_future_day_empty():
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-09-30", device_id="dev1")
    assert card["has_facts"] is False
    assert card["compact"] == ""


def test_build_missing_optional_tables_keeps_phone_facts():
    """缺可选事实表（etl_state/stays/trips/places/anomalies）→ 不降级，
    有 daily_stats 仍能生成手机事实（Task 0 止血契约）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE daily_stats (device_id TEXT, day TEXT, total_screen_ms INTEGER)")
    conn.execute("INSERT INTO daily_stats VALUES ('dev1','2026-08-18',1000)")
    conn.commit()
    # 缺 etl_state/stays/trips/places/anomalies 表：读取缺表 → 空列表，不清空手机事实
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["available"] is True
    assert card["has_facts"] is True
    assert "手机累计" in card["compact"]


def test_build_never_calls_etl(monkeypatch):
    """注入路径必须零 ETL：模块不存在 subprocess/etl 引用。"""
    conn = _make_db()
    assert not hasattr(fc, "subprocess")
    assert not hasattr(fc, "etl")
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["has_facts"] is True


# ---------------------------------------------------------------------------
# 数据水位
# ---------------------------------------------------------------------------


def test_watermark_etl_state_priority(monkeypatch):
    monkeypatch.setattr(fc, "_today_str", lambda: "2026-08-18")
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", now_ms=_ts(2026, 8, 18, 17, 40))
    assert card["etl_watermark_ms"] == _ts(2026, 8, 18, 17, 6)
    assert card["data_as_of_source"] == "etl_state"
    assert card["data_as_of_ms"] == _ts(2026, 8, 18, 17, 6)
    assert card["data_age_min"] == 34  # 17:40 - 17:06


def test_watermark_fallback_stay_trip_max_end():
    """无 etl_state → 用当日 stays/trips 最大 end_ts（17:06 公司）。"""
    conn = _make_db()
    conn.execute("DELETE FROM etl_state")
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["data_as_of_source"] == "stay_trip_fallback"
    assert card["data_as_of_ms"] == _ts(2026, 8, 18, 17, 6)


def test_watermark_clipped_by_now():
    conn = _make_db()
    conn.execute("UPDATE etl_state SET last_event_ts=?", (_ts(2026, 8, 18, 20, 0),))
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", now_ms=_ts(2026, 8, 18, 19, 0))
    assert card["data_as_of_ms"] == _ts(2026, 8, 18, 19, 0)


def test_watermark_before_day_start_no_cutoff():
    """水位在当日开始前 → 不设 cutoff、无 current_known。"""
    conn = _make_db()
    conn.execute("UPDATE etl_state SET last_event_ts=?", (_ts(2026, 8, 17, 23, 50),))
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["data_as_of_ms"] is None
    assert card["current_known"] is None
    # 无 cutoff → 当日全部 stay 保留
    assert [s["label"] for s in card["stays"]] == ["XX路1号〔家〕", "YY路2号〔公司〕", "快餐店", "YY路2号〔公司〕"]


def test_future_watermark_no_negative_age():
    conn = _make_db()
    conn.execute("UPDATE etl_state SET last_event_ts=?", (_ts(2026, 8, 18, 20, 0),))
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", now_ms=_ts(2026, 8, 18, 17, 40))
    assert card["data_age_min"] is None  # 未来水位 → 不产生负年龄


def test_day_window_closed_only_historical_and_past_end():
    conn = _make_db()
    conn.execute("UPDATE etl_state SET last_event_ts=?", (_ts(2026, 8, 19, 0, 0),))
    conn.commit()
    # 历史日 + 真实水位越过日末（次日 00:00 即日界）→ closed
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["day_window_closed"] is True
    # 水位未越过日末 → 不 closed
    conn.execute("UPDATE etl_state SET last_event_ts=?", (_ts(2026, 8, 18, 17, 6),))
    conn.commit()
    card2 = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card2["day_window_closed"] is False


# ---------------------------------------------------------------------------
# stays / trips 裁剪与 current_known
# ---------------------------------------------------------------------------


def test_stays_clipped_to_cutoff():
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    labels = [s["label"] for s in card["stays"]]
    assert labels == ["XX路1号〔家〕", "YY路2号〔公司〕", "快餐店", "YY路2号〔公司〕"]
    assert card["stays"][0]["start_hhmm"] == "00:00"
    assert card["stays"][0]["end_hhmm"] == "08:32"


def test_current_known_at_fact_cutoff():
    """cutoff 17:06 落在最后一段公司 stay 内 → current_known=公司。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    ck = card["current_known"]
    assert ck is not None
    assert ck["label"] == "YY路2号〔公司〕"
    assert ck["place_id"] == "p_work"
    assert ck["place_name"] == "YY路2号"
    assert ck["user_tag"] == "公司"
    assert ck["since_hhmm"] == "13:02"
    assert ck["observed_until_hhmm"] == "17:06"
    assert ck["district"] == "雨花台区"


def test_current_known_includes_equal_stay_end():
    """cutoff == stay.end_ts → 闭区间命中。"""
    conn = _make_db()
    conn.execute("UPDATE etl_state SET last_event_ts=?", (_ts(2026, 8, 18, 12, 3),))
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    ck = card["current_known"]
    assert ck is not None
    assert ck["label"] == "YY路2号〔公司〕"
    assert ck["observed_until_hhmm"] == "12:03"


def test_trip_matches_nearest_prev_next_stay():
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    trips = card["trips"]
    assert len(trips) == 3
    # 08:32→09:04：XX路1号〔家〕 → YY路2号〔公司〕
    assert trips[0]["from_label"] == "XX路1号〔家〕"
    assert trips[0]["to_label"] == "YY路2号〔公司〕"
    assert trips[0]["from_place"]["place_id"] == "p_home"
    assert trips[0]["to_place"]["user_tag"] == "公司"
    assert trips[0]["route_dist_m"] == 3400
    # 12:47→13:02：快餐店（无 tag 不标） → 公司
    assert trips[2]["from_label"] == "快餐店"
    assert trips[2]["to_label"] == "YY路2号〔公司〕"
    assert trips[2]["from_place"]["place_name"] == "快餐店"
    assert trips[2]["from_place"]["user_tag"] == ""


def test_stay_minutes_aggregate_by_label():
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    sm = card["stay_minutes"]
    # 家 512min；公司 179+244=423min；快餐店 36min
    assert sm["家"] == 512
    assert sm["公司"] == 423
    assert sm["其他"] == 36


def test_anomalies_only_current_day():
    conn = _make_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO anomalies VALUES (?,?,?,?,?,?,?)",
        (9, "dev1", "2026-08-19", "new_place", "他日地点", "访问 1 次", _ts(2026, 8, 19, 9, 0)),
    )
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert len(card["anomalies"]) == 2  # 只含当日两条


def test_trip_only_day_has_facts():
    conn = _make_db()
    conn.execute("DELETE FROM daily_stats")
    conn.execute("DELETE FROM stays")
    conn.execute("DELETE FROM etl_state")
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["has_facts"] is True  # trips 存在
    assert card["available"] is False


# ---------------------------------------------------------------------------
# compact 段落
# ---------------------------------------------------------------------------


def test_compact_contains_all_sections():
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    text = fc.render_compact(card)
    assert "=== 生活事实（" in text
    assert "今日轨迹：" in text
    assert "当前已知：" in text
    assert "停留累计：" in text
    assert "手机累计：" in text
    assert "通知累计：" in text
    assert "系统标记：" in text


def test_compact_waterline_has_age(monkeypatch):
    monkeypatch.setattr(fc, "_today_str", lambda: "2026-08-18")
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact",
                    now_ms=_ts(2026, 8, 18, 17, 40))
    line = fc.render_compact(card).splitlines()[0]
    assert "今日未完；数据至 17:06，距现在 34 分" in line


def test_compact_waterline_historical_day(monkeypatch):
    monkeypatch.setattr(fc, "_today_str", lambda: "2026-08-31")
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    line = fc.render_compact(card).splitlines()[0]
    assert "历史日" in line


def test_compact_phone_section_example_format():
    """对照 §2.4 示例：屏幕/解锁/切换/App 前二。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    text = fc.render_compact(card)
    assert "手机累计：屏幕 7.0h · 解锁 12 · 切换 46 · 飞书 1.0h / 微信 0.5h" in text


def test_compact_timeline_example_format():
    """对照 §2.4 示例：轨迹行 + 移动段数。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    text = fc.render_compact(card)
    assert (
        "今日轨迹：XX路1号〔家〕 00:00-08:32 → YY路2号〔公司〕 09:04-12:03 → 快餐店 12:11-12:47 → YY路2号〔公司〕 13:02-17:06；移动 3 段"
        in text
    )


def test_compact_tag_filters_grid_poi():
    """网格 poi 不进 compact 标记；真名 poi 进入。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    text = fc.render_compact(card)
    assert "系统标记：#new_place 德基广场" in text
    assert "31.97,118.76" not in text


def test_compact_omits_current_and_tag_lines():
    """无 cutoff 重叠 / 无 anomalies → 文本无「当前已知」「系统标记」。"""
    conn = _make_db()
    conn.execute("UPDATE etl_state SET last_event_ts=?", (_ts(2026, 8, 17, 23, 50),))
    conn.execute("DELETE FROM anomalies")
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    text = fc.render_compact(card)
    assert "当前已知" not in text
    assert "系统标记" not in text


def test_compact_max_600_chars_without_mid_field_cut():
    """compact 总长度 ≤600，且每段为整行（无截半）。"""
    conn = _make_db()
    conn.execute("UPDATE etl_state SET last_event_ts=?", (_ts(2026, 8, 18, 23, 59),))
    conn.commit()
    cur = conn.cursor()
    for i in range(40):
        cur.execute(
            "INSERT INTO stays VALUES (?,?,?,?,?,?,?,?)",
            (100 + i, "dev1", "p_mall", "g_mall", _ts(2026, 8, 18, 13, 2) + i * 90_000,
             _ts(2026, 8, 18, 13, 2) + (i + 1) * 90_000, "2026-08-18", 20),
        )
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["compact_chars"] <= 600
    assert "…" in card["compact"]  # 轨迹折叠生效


def test_budget_omits_whole_low_priority_sections(monkeypatch):
    """预算收紧 → 整段省略低优先级（tags），被省略段绝不残留在文本中。"""
    monkeypatch.setattr(fc, "_MAX_COMPACT_CHARS", 90)
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert "budget" in card["compact_omitted"].values()
    text = fc.render_compact(card)
    for sid, reason in card["compact_omitted"].items():
        if reason == "budget":
            sec = next(s for s in card["compact_sections"] if s["id"] == sid)
            assert sec["text"] not in text
    assert card["compact"] != ""  # 至少标题在


def test_new_registered_section_needs_no_packer_change(monkeypatch):
    """新增 section 只需注册 builder，预算器自动纳入。"""
    conn = _make_db()

    def _extra(card):
        return fc.CompactSection(id="extra", text="额外段：ok", priority=5)

    monkeypatch.setattr(fc, "_SECTION_BUILDERS", fc._SECTION_BUILDERS + (_extra,))
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert "额外段：ok" in fc.render_compact(card)
    assert "extra" in card["compact_lines"]


def test_section_builders_do_not_query_or_mutate():
    """每个 section builder 只读 card，不碰 DB、不改 card 字段。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    snapshot = dict(card)
    for builder in fc._SECTION_BUILDERS:
        sec = builder(card)
        assert dict(card) == snapshot  # 不 mutate
        assert sec is None or isinstance(sec, dict)


def test_compact_ignores_persona_card(monkeypatch):
    """compact 注入路径不读 persona：persona 返回夸张内容不进 compact。"""
    conn = _make_db()
    monkeypatch.setattr(
        fc, "build_persona", lambda **kw: {"available": True, "cat": "海量文本" * 500}
    )
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert "海量文本" not in fc.render_compact(card)


def test_render_compact_read_only():
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    text = fc.render_compact(card)
    assert text == card["compact"]
    assert fc.render_compact(card) == text  # 幂等


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------


def test_build_db_corrupt_returns_degraded(tmp_path):
    db = tmp_path / "langTrack.db"
    db.write_bytes(b"not a database")
    card = fc.build(db_path=str(db), day="2026-08-18")
    assert card["available"] is False
    assert card["compact"] == ""


def test_build_missing_places_semantic_cols():
    """places 缺 poi/behavior 语义列 → 不抛，label 退化用 visit_count 聚合。"""
    conn = _make_db(include_places_semantic=False)
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["available"] is True
    assert card["stays"][0]["label"] == "未知地点〔家〕"  # 缺语义列 → 名退化，人工 tag 仍在


def test_build_never_raises_on_any_exception(monkeypatch):
    conn = _make_db()
    monkeypatch.setattr(
        fc, "_fill_card", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["available"] is False
    assert card["compact"] == ""


def test_build_never_raises_on_logger_failure(monkeypatch):
    """日志抛异常也不影响 build 主路径。"""
    conn = _make_db()

    class _BadLogger:
        def info(self, *a, **k):
            raise RuntimeError("log boom")

        def warning(self, *a, **k):
            raise RuntimeError("log boom")

    monkeypatch.setattr(fc, "logger", _BadLogger())
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["available"] is True


# ---------------------------------------------------------------------------
# 维测日志
# ---------------------------------------------------------------------------


def test_log_built_emits_info(monkeypatch):
    stub = _StubLogger()
    monkeypatch.setattr(fc, "logger", stub)
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["available"] is True
    assert any(m == "fact card built" for m, _ in stub.infos)
    _, fields = stub.infos[0]
    assert fields["day"] == "2026-08-18"
    assert fields["data_as_of_source"] == "etl_state"
    assert fields["card_fp"] == card["card_fp"]
    assert fields["compact"] == card["compact"]


def test_log_degraded_emits_warning(monkeypatch, tmp_path):
    stub = _StubLogger()
    monkeypatch.setattr(fc, "logger", stub)
    card = fc.build(db_path=str(tmp_path / "nope.db"), day="2026-08-18")
    assert card["available"] is False
    assert any(m == "fact card degraded" for m, _ in stub.warnings)


def test_card_fp_stable_and_unique():
    conn = _make_db()
    c1 = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    c2 = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert c1["card_fp"] == c2["card_fp"]
    assert c1["card_fp"] != ""


# ---------------------------------------------------------------------------
# Task 0：止血（无 stats 不报通知 0 / 无事实 compact 为空 / 缺表不清空手机事实）
# ---------------------------------------------------------------------------


def test_no_stats_with_stay_no_notification_zero():
    """无 daily_stats、有 stay 时，compact 不得含「通知累计：0 条」。"""
    conn = _make_db()
    conn.execute("DELETE FROM daily_stats")
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["available"] is False
    assert card["has_facts"] is True  # 有 stay 仍算有事实
    assert "通知累计" not in card["compact"]
    assert "0 条" not in card["compact"]
    assert "今日轨迹" in card["compact"]  # 手机事实仍保留


def test_no_facts_compact_empty():
    """has_facts=False（无 stats/stay/trip，仅水位）→ compact 为空。"""
    conn = _make_db()
    conn.execute("DELETE FROM daily_stats")
    conn.execute("DELETE FROM stays")
    conn.execute("DELETE FROM trips")
    conn.execute("DELETE FROM anomalies")
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["has_facts"] is False
    assert card["compact"] == ""
    assert card["compact_sections"] == []


def test_only_anomaly_compact_empty():
    """仅 anomaly（无 stats/stay/trip）→ compact 为空，tag 不能单独成卡。"""
    conn = _make_db()
    conn.execute("DELETE FROM daily_stats")
    conn.execute("DELETE FROM stays")
    conn.execute("DELETE FROM trips")
    conn.commit()
    # 保留一条 anomaly（非网格 poi）
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["anomalies"] != []
    assert card["has_facts"] is False
    assert card["compact"] == ""
    assert card["compact_sections"] == []


def test_missing_stays_table_keeps_phone_facts():
    """缺 stays 表时，已有 daily_stats 仍能生成手机事实（不整卡降级）。"""
    conn = _make_db()
    conn.execute("DROP TABLE stays")
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["available"] is True
    assert card["has_facts"] is True
    assert "手机累计" in card["compact"]


def test_missing_trips_table_keeps_phone_facts():
    conn = _make_db()
    conn.execute("DROP TABLE trips")
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["available"] is True
    assert "手机累计" in card["compact"]


def test_missing_anomalies_table_keeps_phone_facts():
    conn = _make_db()
    conn.execute("DROP TABLE anomalies")
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    assert card["available"] is True
    assert "手机累计" in card["compact"]


# ---------------------------------------------------------------------------
# Task 7：v2 地点契约（PlaceRef / format_place / full 卡透传）
# ---------------------------------------------------------------------------


def test_v2_stay_brief_place_ref_contract():
    """StayBrief 增 PlaceRef 载荷，旧 label 兼容为 format_place 展示名。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    s0 = card["stays"][0]
    assert s0["place_id"] == "p_home"
    assert s0["place_name"] == "XX路1号"
    assert s0["user_tag"] == "家"
    assert s0["name_source"] == "address"
    assert s0["label"] == "XX路1号〔家〕"   # 旧字段兼容：format_place(place_name, user_tag)
    assert s0["poi"] == "家小区"
    assert s0["poi_fallback"] == ""
    assert s0["district"] == "玄武区"
    assert s0["point_count"] == 260
    assert s0["avg_accuracy_m"] == 18
    # 无 tag 真实地点不被过滤：餐馆 poi 高置信 → 仅真名
    s2 = card["stays"][2]
    assert s2["place_id"] == "p_rest"
    assert s2["place_name"] == "快餐店"
    assert s2["user_tag"] == ""
    assert s2["label"] == "快餐店"


def test_v2_trip_place_ref_and_route_dist():
    """TripBrief 增 from_place/to_place PlaceRef 与 route_dist_m。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    t0 = card["trips"][0]
    assert t0["from_place"]["place_id"] == "p_home"
    assert t0["from_place"]["user_tag"] == "家"
    assert t0["to_place"]["place_id"] == "p_work"
    assert t0["to_place"]["place_name"] == "YY路2号"
    assert t0["route_dist_m"] == 3400
    # 未落 route 距离 → None（禁止用直距冒充）
    assert card["trips"][1]["route_dist_m"] is None
    # 旧 from_label/to_label 仍兼容
    assert t0["from_label"] == "XX路1号〔家〕"
    assert t0["to_label"] == "YY路2号〔公司〕"


def test_v2_full_card_transparent_quality_and_place_stats():
    """full 卡透传 daily_location_quality / tag_conflict_count / PlaceBrief 三项统计。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="full")
    assert card["daily_location_quality"] is not None
    assert card["daily_location_quality"]["total_points"] == 1024
    assert "created_at" not in card["daily_location_quality"]  # 审计列不透传
    assert card["tag_conflict_count"] == 1
    p0 = card["places"][0]  # visit_count 降序：公司 440 最高
    assert p0["place_id"] == "p_work"
    assert p0["label"] == "YY路2号〔公司〕"
    assert p0["visits"] == 440            # 旧字段兼容（deprecated）
    assert p0["point_count"] == 440
    assert p0["visit_episodes"] == 440
    assert p0["stay_ms"] == 7_000_000
    assert p0["place_name"] == "YY路2号"
    assert p0["user_tag"] == "公司"


def test_v2_compact_keeps_untagged_real_place():
    """compact timeline：无 tag 的餐馆真名保留（不被过滤），坐标串仍被过滤。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="compact")
    text = fc.render_compact(card)
    assert "快餐店 12:11-12:47" in text
    assert "系统标记：#new_place 德基广场" in text
    assert "31.97,118.76" not in text


def test_v1_legacy_db_still_readable():
    """v1 旧库（无 place_id/无命名证据）→ 不抛，展示降级为「未知地点〔tag〕」且旧字段在。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA user_version=0")
    cur = conn.cursor()
    cur.execute("CREATE TABLE daily_stats (device_id TEXT, day TEXT, total_screen_ms INTEGER)")
    cur.execute("INSERT INTO daily_stats VALUES ('dev1','2026-08-18',1000)")
    cur.execute(
        "CREATE TABLE places (id INTEGER, device_id TEXT, grid_key TEXT, label TEXT, "
        "visit_count INTEGER, poi TEXT, behavior TEXT, district TEXT, address TEXT)"
    )
    cur.execute(
        "CREATE TABLE stays (id INTEGER, device_id TEXT, grid_key TEXT, "
        "start_ts INTEGER, end_ts INTEGER, day TEXT)"
    )
    cur.execute(
        "CREATE TABLE trips (id INTEGER, device_id TEXT, start_ts INTEGER, end_ts INTEGER, "
        "dist_m INTEGER, day TEXT)"
    )
    cur.execute(
        "INSERT INTO places VALUES (1,'dev1','g_home','家',260,'家小区','home','玄武区','XX路1号')"
    )
    cur.execute(
        "INSERT INTO stays VALUES (1,'dev1','g_home',?,?,?)",
        (_ts(2026, 8, 18, 0, 0), _ts(2026, 8, 18, 8, 32), "2026-08-18"),
    )
    cur.execute(
        "INSERT INTO trips VALUES (1,'dev1',?,?,3200,'2026-08-18')",
        (_ts(2026, 8, 18, 8, 32), _ts(2026, 8, 18, 9, 4)),
    )
    cur.execute(
        "CREATE TABLE etl_state (device_id TEXT PRIMARY KEY, last_event_ts INTEGER)"
    )
    cur.execute("INSERT INTO etl_state VALUES ('dev1', ?)", (_ts(2026, 8, 18, 17, 0),))
    conn.commit()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    assert card["available"] is True
    assert card["stays"][0]["place_id"] in (None, "")  # v1 无 place_id → 空
    assert card["stays"][0]["label"] == "XX路1号〔家〕"
    assert card["trips"][0]["from_place"] is None      # v1 无 from/to place
    assert card["trips"][0]["from_label"] == "XX路1号〔家〕"
    assert card["trips"][0]["route_dist_m"] is None
    assert card["daily_location_quality"] is None
    assert card["tag_conflict_count"] == 0


def test_v2_stay_bucket_by_user_tag_not_label():
    """stay_minutes 聚合键改用 user_tag（家/公司），无 tag 真名落入「其他」。"""
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1")
    sm = card["stay_minutes"]
    assert sm["家"] == 512
    assert sm["公司"] == 423
    assert sm["其他"] == 36    # 快餐店 36min，无名「家/公司」→ 其他
    assert "未知" not in sm    # 无 tag 真名不进「未知」桶


def test_v2_log_built_reports_quality_fields(monkeypatch):
    """维测日志新增质量字段。"""
    stub = _StubLogger()
    monkeypatch.setattr(fc, "logger", stub)
    conn = _make_db()
    card = fc.build(conn=conn, day="2026-08-18", device_id="dev1", detail="full")
    assert card["available"] is True
    _, fields = stub.infos[0]
    assert fields["daily_quality_present"] == 1
    assert fields["tag_conflict_count"] == 1
