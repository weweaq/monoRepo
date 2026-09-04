"""C1 persona.py 单元测试：纯合成内存库，断言聚合/健康/节奏/规律/兼容。

不依赖真实 data/langTrack.db（198MB）。运行需 PYTHONPATH=src（见文件末尾 sys.path）。

覆盖：
- 应用分类聚合：按时长求和、pct 归一、未登记 app 落入"其他"+uncategorized
- 屏幕健康度：重度天数 / heavy_user / trend / avg_hours
- 使用时段分布：夜猫子判定 / night_pct / peak_segment
- 生活规律：家/公司 stay JOIN 路径（数据不足 → 非规律）
- device_id 过滤：查无此设备 → available=False
- 旧库兼容：daily_stats 无 device_id 列，device_id=None 仍可用
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gacore.langTrack import etl, persona, storage  # noqa: E402

_TZ = timezone(timedelta(hours=8))


def _ts(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=_TZ).timestamp() * 1000)


def _make_db(device_id: str = "dev1", with_device_col: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    if with_device_col:
        cur.execute(
            "CREATE TABLE daily_stats ("
            "device_id TEXT, day TEXT, total_screen_ms INTEGER, app_ranking_json TEXT)"
        )
    else:
        cur.execute(
            "CREATE TABLE daily_stats ("
            "day TEXT, total_screen_ms INTEGER, app_ranking_json TEXT)"
        )
    cur.execute(
        "CREATE TABLE sessions ("
        "id INTEGER, device_id TEXT, day TEXT, start_ms INTEGER, duration_ms INTEGER)"
    )
    cur.execute(
        "CREATE TABLE stays ("
        "id INTEGER, device_id TEXT, grid_key TEXT, start_ts INTEGER, day TEXT)"
    )
    cur.execute(
        "CREATE TABLE trips ("
        "id INTEGER, device_id TEXT, day TEXT, start_ts INTEGER)"
    )
    cur.execute(
        "CREATE TABLE places ("
        "id INTEGER, device_id TEXT, grid_key TEXT, label TEXT)"
    )
    ranking = json.dumps([
        {"app": "抖音", "ms": 3_600_000},
        {"app": "微信", "ms": 1_800_000},
        {"app": "Edge", "ms": 600_000},
        {"app": "未知App", "ms": 1_000_000},
    ])
    # 7 天：5 天重度(>5h，20M ms)，2 天轻(10M ms)
    for i in range(7):
        day = f"2024-01-0{i + 1}"
        ms = 20_000_000 if i < 5 else 10_000_000
        if with_device_col:
            cur.execute(
                "INSERT INTO daily_stats VALUES (?,?,?,?)",
                (device_id, day, ms, ranking),
            )
        else:
            cur.execute(
                "INSERT INTO daily_stats VALUES (?,?,?)",
                (day, ms, ranking),
            )
    # 节奏：凌晨/深夜为主 → 夜猫子
    sessions = [
        (1, device_id, "2024-01-01", _ts(2024, 1, 1, 2, 0), 3_600_000),
        (2, device_id, "2024-01-01", _ts(2024, 1, 1, 2, 30), 3_600_000),
        (3, device_id, "2024-01-01", _ts(2024, 1, 1, 3, 0), 3_600_000),
        (4, device_id, "2024-01-01", _ts(2024, 1, 1, 14, 0), 1_000_000),
        (5, device_id, "2024-01-01", _ts(2024, 1, 1, 23, 30), 2_000_000),
    ]
    cur.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", sessions)
    # 规律：1 个家 stay（work=0 → 非规律，但验证 JOIN 路径）
    cur.execute("INSERT INTO places VALUES (1,?,?,?)", (device_id, "g1", "家"))
    cur.execute(
        "INSERT INTO stays VALUES (1,?,?,?,?)",
        (device_id, "g1", _ts(2024, 1, 1, 8, 0), "2024-01-01"),
    )
    conn.commit()
    return conn


def test_category_aggregation_sums_and_pct():
    conn = _make_db()
    p = persona.build(conn=conn, device_id="dev1", days=7)
    assert p["available"] is True
    by_cat = {c["category"]: c for c in p["category_usage"]}
    # 聚合跨 7 天，时长各 ×7
    assert by_cat["视频"]["ms"] == 3_600_000 * 7
    assert by_cat["社交"]["ms"] == 1_800_000 * 7
    assert by_cat["工具"]["ms"] == 600_000 * 7
    # 总时长 = (抖音+微信+Edge+未知) × 7 = 7,000,000 × 7
    total = sum(c["ms"] for c in p["category_usage"])
    assert total == 7_000_000 * 7
    # pct 之和 ~100
    assert abs(sum(c["pct"] for c in p["category_usage"]) - 100) < 0.1
    # 未知App 不在 app_categories.json → 归"其他"且出现在 uncategorized
    assert "未知App" in p["uncategorized"]


def test_screen_health_heavy_user():
    conn = _make_db()
    p = persona.build(conn=conn, device_id="dev1", days=7)
    sh = p["screen_health"]
    # 5/7 天重度(>5h) → heavy_user
    assert sh["heavy_days"] == 5
    assert sh["heavy_user"] is True
    assert sh["trend"] in ("up", "down", "flat")
    assert isinstance(sh["avg_hours"], float)


def test_rhythm_night_owl():
    conn = _make_db()
    p = persona.build(conn=conn, device_id="dev1", days=7)
    rh = p["rhythm"]
    assert rh["night_owl"] is True
    assert rh["night_pct"] > 50
    assert rh["peak_segment"] == "凌晨"


def test_routine_data_sufficient_path():
    conn = _make_db()
    p = persona.build(conn=conn, device_id="dev1", days=7)
    rt = p["routine"]
    # 仅 1 个家 stay，work=0 → 非规律
    assert rt["regular"] is False
    assert "作息" in rt["note"]


def test_device_filter_no_data():
    conn = _make_db()
    p = persona.build(conn=conn, device_id="nonexistent", days=7)
    assert p["available"] is False


def test_old_schema_no_device_col():
    # 旧库 daily_stats 无 device_id 列：device_id=None 仍可用
    conn = _make_db(with_device_col=False)
    p = persona.build(conn=conn, device_id=None, days=7)
    assert p["available"] is True
    by_cat = {c["category"]: c for c in p["category_usage"]}
    assert by_cat["视频"]["ms"] == 3_600_000 * 7
    assert "未知App" in p["uncategorized"]


def test_commute_stable_needs_spatial_evidence_not_trip_count():
    """契约（P1-3）：仅 trip 条数≥3、无 SpatialProfile 有效往返样本 → commute_stable=False，note 无'通勤稳定'。"""
    conn = _make_db()
    # 3 条 trip 记录：如果凭条数断言"通勤稳定"就会误判；必须依赖 SpatialProfile 的 valid_days
    for i in range(3):
        conn.execute(
            "INSERT INTO trips VALUES (?,?,?,?)",
            (i + 1, "dev1", f"2024-01-0{i + 1}", _ts(2024, 1, i + 1, 9, 0)),
        )
    conn.commit()
    p = persona.build(conn=conn, device_id="dev1", days=7)
    rt = p["routine"]
    # 无 spatial 骨架（缺往返样本）→ 不凭 trip 条数断言"通勤稳定"
    assert rt["commute_stable"] is False
    assert "通勤稳定" not in rt.get("note", "")
    ev = rt.get("commute_evidence") or {}
    assert isinstance(ev, dict)
    assert ev.get("stable") is not True


def test_home_days_work_days_independent():
    """P2-2：home_days/work_days 独立按各自出现的本地日期数统计，不共用合并口径 anchor_days。"""
    conn = _make_home_work_full_db()
    p = persona.build(conn=conn, device_id="dev1", days=7)
    rt = p["routine"]
    # 家在 5 个不同日期、公司在 2 个不同日期 → 独立口径 (5, 2)
    assert rt["home_days"] == 5
    assert rt["work_days"] == 2
    conn.close()


def _make_home_work_full_db() -> sqlite3.Connection:
    """完整 schema 库：家在 1-5 日各一段、公司在 6-7 日各一段（独立天数 5/2 可区分）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(storage._SCHEMA)
    conn.executescript(etl._SCHEMA)
    # 与真实调用链一致：read_stays 依赖 row_factory=Row 才能读列
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    ranking = json.dumps([{"app": "微信", "ms": 1_800_000}])
    for i in range(7):
        day = f"2024-01-0{i + 1}"
        cur.execute(
            "INSERT INTO daily_stats(device_id, day, total_screen_ms, unlock_count, "
            "switch_count, notification_count, notification_clicked, screen_on_count, "
            "app_ranking_json, top_notification_apps_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("dev1", day, 3_600_000, 5, 8, 0, 0, 3, ranking, "[]"),
        )
    cur.execute(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, "
        "last_seen, visit_count, is_primary, poi, poi_type, behavior, "
        "poi_fallback, address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", "gk_home", 31.3, 118.3, "家", _ts(2024, 1, 1, 8, 0),
         _ts(2024, 1, 5, 23, 0), 5, 1, "", "", "", "", ""),
    )
    cur.execute(
        "INSERT INTO places(device_id, grid_key, lat, lon, label, first_seen, "
        "last_seen, visit_count, is_primary, poi, poi_type, behavior, "
        "poi_fallback, address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev1", "gk_work", 31.9, 118.9, "公司", _ts(2024, 1, 6, 9, 0),
         _ts(2024, 1, 7, 20, 0), 2, 1, "", "", "", "", ""),
    )
    for i in range(5):
        day = f"2024-01-0{i + 1}"
        cur.execute(
            "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
            "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, "
            "grid_key, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dev1", _ts(2024, 1, i + 1, 21, 0), _ts(2024, 1, i + 1, 23, 0),
             7_200_000, 31.3, 118.3, 31.3, 118.3, 31.3, 118.3, 5, 10.0, "gk_home", day),
        )
    for i in (5, 6):
        day = f"2024-01-0{i + 1}"
        cur.execute(
            "INSERT INTO stays(device_id, start_ts, end_ts, duration_ms, center_lat, "
            "center_lon, min_lat, min_lon, max_lat, max_lon, n_points, radius_m, "
            "grid_key, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dev1", _ts(2024, 1, i + 1, 9, 0), _ts(2024, 1, i + 1, 18, 0),
             32_400_000, 31.9, 118.9, 31.9, 118.9, 31.9, 118.9, 12, 15.0, "gk_work", day),
        )
    conn.commit()
    return conn
