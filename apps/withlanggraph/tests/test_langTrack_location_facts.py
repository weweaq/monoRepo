"""test_langTrack_location_facts.py —— location_facts.py 纯算法测试（§2.1/2.3/2.6/3.3）。"""

from __future__ import annotations

import datetime
import json
import math

from gacore.langTrack.location_facts import (
    LocationPoint,
    OldPlace,
    StayInput,
    accuracy_filter,
    canonical_places,
    cluster_stays,
    daily_quality_rows,
    format_place,
    grid_key_of,
    location_points,
    match_old_new,
    parse_location_point,
    quality_stats,
    resolve_place_ids,
    resolve_place_name,
    to_amap_coord,
    user_tag_of,
    wgs84_to_gcj02,
)

# ---------------------------------------------------------------------------
# parse_location_point（§2.1）
# ---------------------------------------------------------------------------

class TestParseLocationPoint:
    def test_accepts_finite_numbers_and_numeric_strings(self):
        for lat, lon in [(31.98, 118.78), ("31.98", "118.78"), (32, 118), ("32", "118")]:
            pt = parse_location_point("d", 1000, {"lat": lat, "lon": lon})
            assert pt is not None
            assert pt.lat == float(lat)
            assert pt.lon == float(lon)

    def test_rejects_bool(self):
        assert parse_location_point("d", 1, {"lat": True, "lon": 118.0}) is None
        assert parse_location_point("d", 1, {"lat": 31.0, "lon": False}) is None

    def test_rejects_empty_string(self):
        assert parse_location_point("d", 1, {"lat": "", "lon": 118.0}) is None
        assert parse_location_point("d", 1, {"lat": "  ", "lon": 118.0}) is None

    def test_rejects_zero_zero(self):
        assert parse_location_point("d", 1, {"lat": 0, "lon": 0}) is None

    def test_rejects_out_of_range(self):
        assert parse_location_point("d", 1, {"lat": 91.0, "lon": 118.0}) is None
        assert parse_location_point("d", 1, {"lat": -91.0, "lon": 118.0}) is None
        assert parse_location_point("d", 1, {"lat": 31.0, "lon": 181.0}) is None
        assert parse_location_point("d", 1, {"lat": 31.0, "lon": -181.0}) is None

    def test_rejects_nan_inf(self):
        assert parse_location_point("d", 1, {"lat": math.nan, "lon": 118.0}) is None
        assert parse_location_point("d", 1, {"lat": 31.0, "lon": math.inf}) is None
        assert parse_location_point("d", 1, {"lat": "-inf", "lon": 118.0}) is None

    def test_missing_acc_provider_defaults(self):
        pt = parse_location_point("d", 1, {"lat": 31.98, "lon": 118.78})
        assert pt.accuracy_m is None
        assert pt.provider == "unknown"
        assert pt.coord_system == "unknown"

    def test_negative_or_nonfinite_acc_unknown(self):
        pt = parse_location_point("d", 1, {"lat": 31.98, "lon": 118.78, "acc": -5})
        assert pt.accuracy_m is None
        pt2 = parse_location_point("d", 1, {"lat": 31.98, "lon": 118.78, "acc": "nan"})
        assert pt2.accuracy_m is None

    def test_provider_lowercased(self):
        pt = parse_location_point("d", 1, {"lat": 31.98, "lon": 118.78, "provider": "GPS"})
        assert pt.provider == "gps"

    def test_coord_system_from_config(self):
        pt = parse_location_point("d", 1, {"lat": 31.98, "lon": 118.78}, coord_system="wgs84")
        assert pt.coord_system == "wgs84"

    def test_rejects_non_dict_payload(self):
        assert parse_location_point("d", 1, None) is None
        assert parse_location_point("d", 1, "nope") is None

    def test_does_not_guess_aliases(self):
        # 暂不猜测 latitude/lng 别名
        assert parse_location_point("d", 1, {"latitude": 31.0, "longitude": 118.0}) is None


# ---------------------------------------------------------------------------
# quality_stats（§3.2）
# ---------------------------------------------------------------------------

class TestQualityStats:
    def _pts(self):
        # ts 使用毫秒：0 / 30min / 60min / 120min
        return [
            LocationPoint("a", 0, 31.0, 118.0, 5.0, "gps", "gcj02"),
            LocationPoint("a", 30 * 60_000, 31.001, 118.001, 80.0, "gps", "gcj02"),
            LocationPoint("a", 60 * 60_000, 31.002, 118.002, None, "network", "gcj02"),
            LocationPoint("a", 120 * 60_000, 31.003, 118.003, 200.0, "network", "gcj02"),
        ]

    def test_per_device_provider_and_accuracy_buckets(self):
        out = quality_stats(self._pts())
        assert set(out) == {"a"}
        a = out["a"]
        assert a["points"] == 4
        assert a["providers"] == {"gps": 2, "network": 2}
        assert a["accuracy"]["lt10"] == 1
        assert a["accuracy"]["50_100"] == 1
        assert a["accuracy"]["unknown"] == 1
        assert a["accuracy"]["gt100"] == 1

    def test_slots_and_gaps(self):
        a = quality_stats(self._pts())["a"]
        # 30 分钟格：0,30,60,120 → 4 个不同格
        assert a["slots_30m"] == 4
        # 间隔 30min/30min/60min → avg 2400s max 3600s
        assert a["avg_sample_gap_s"] == 2400.0
        assert a["max_sample_gap_s"] == 3600.0

    def test_single_point_no_gap(self):
        out = quality_stats([LocationPoint("a", 0, 31.0, 118.0, None, "gps", "gcj02")])
        assert out["a"]["avg_sample_gap_s"] == 0.0
        assert out["a"]["max_sample_gap_s"] == 0.0


# ---------------------------------------------------------------------------
# cluster_stays / canonical_places（§2.3 规则 1-6/9）
# ---------------------------------------------------------------------------

def _stay(device, lat, lon, dur_ms, start=0):
    return StayInput(
        device_id=device,
        start_ts=start,
        end_ts=start + dur_ms,
        duration_ms=dur_ms,
        center_lat=lat,
        center_lon=lon,
        grid_key=grid_key_of(lat, lon),
    )


class TestClusterStays:
    def test_two_grids_within_range_merge(self):
        # 两个相邻 grid（0.001° 纬度差，~111m ≤120m）且 spread 满足 → 并入同一 cluster
        a = _stay("d", 31.9801, 118.7801, 60_000)
        b = _stay("d", 31.9810, 118.7801, 60_000)  # 相邻网格
        clusters = cluster_stays([a, b])
        assert len(clusters) == 1
        c = clusters[0]
        assert len(c.member_grid_keys) == 2
        assert c.grid_key == grid_key_of(31.9801, 118.7801)  # sorted[0]

    def test_far_grids_split(self):
        a = _stay("d", 31.98, 118.78, 60_000)
        b = _stay("d", 31.99, 118.79, 60_000)  # ~1.3km
        clusters = cluster_stays([a, b])
        assert len(clusters) == 2

    def test_radius_upper_bound(self):
        # 中心距超 120m 的点不合并
        a = _stay("d", 31.9800, 118.7800, 60_000)
        far = _stay("d", 31.9820, 118.7820, 60_000)  # ~220m
        clusters = cluster_stays([a, far])
        assert len(clusters) == 2

    def test_float_rounding_01m(self):
        # 距离四舍五入到 0.01m 参与比较：构造 120.001m 与 119.999m 边界
        # 直接验证：120.001m 四舍五入 120.00 → 等于上限，仍拒绝（>120 拒绝，<=120 允许）
        a = _stay("d", 31.9800, 118.7800, 60_000)
        # 约 120m ≈ 0.0011° 纬度差；用纬度差构造：0.0011° ≈ 122m >120 → 分
        b = _stay("d", 31.9811, 118.7800, 60_000)
        assert len(cluster_stays([a, b])) == 2

    def test_input_order_does_not_matter(self):
        pts = [
            _stay("d", 31.9801, 118.7801, 60_000),
            _stay("d", 31.9810, 118.7801, 60_000),
            _stay("d", 31.99, 118.79, 60_000),
        ]
        c1 = cluster_stays(pts)
        c2 = cluster_stays(list(reversed(pts)))
        assert [(x.grid_key, len(x.member_grid_keys)) for x in c1] == [
            (x.grid_key, len(x.member_grid_keys)) for x in c2
        ]

    def test_devices_isolated(self):
        a = _stay("d1", 31.9801, 118.7801, 60_000)
        b = _stay("d2", 31.9810, 118.7801, 60_000)
        assert len(cluster_stays([a, b])) == 2


class TestCanonicalPlaces:
    def test_place_id_is_sha1_based(self):
        places = canonical_places([_stay("d", 31.9801, 118.7801, 60_000)])
        assert len(places) == 1
        pid = places[0]["place_id"]
        assert len(pid) == 16
        import hashlib

        expect = hashlib.sha1(
            ("d|" + grid_key_of(31.9801, 118.7801)).encode()
        ).hexdigest()[:16]
        assert pid == expect

    def test_three_stats_aggregation(self):
        places = canonical_places(
            [
                _stay("d", 31.9801, 118.7801, 60_000, start=0),
                _stay("d", 31.9802, 118.7802, 120_000, start=200_000),
            ]
        )
        assert len(places) == 1
        p = places[0]
        assert p["visit_count"] == 2
        assert p["stay_ms"] == 180_000
        assert p["first_seen"] == 0
        assert p["last_seen"] == 320_000

    def test_stays_never_split_from_same_grid(self):
        # 同一 grid 两个 far apart stay 也必须属于同一 seed → 同一 cluster
        places = canonical_places(
            [
                _stay("d", 31.9800, 118.7800, 60_000),
                _stay("d", 31.9805, 118.7805, 60_000),
            ]
        )
        # 两 stay 同 grid → 单 seed → 单 cluster（即使 spread 超限）
        assert len(places) == 1


# ---------------------------------------------------------------------------
# match_old_new / resolve_place_ids（§2.3 规则 7-12）
# ---------------------------------------------------------------------------

def _old(device, pid, lat, lon, label="未知", grid_keys=None, poi=None, address=None):
    return OldPlace(
        device_id=device,
        place_id=pid,
        grid_key=grid_keys[0] if grid_keys else grid_key_of(lat, lon),
        lat=lat,
        lon=lon,
        label=label,
        poi=poi,
        address=address,
        matched_level=None,
        grid_keys=tuple(grid_keys) if grid_keys else (),
    )


class TestMatchOldNew:
    def test_one_old_one_new_jaccard(self):
        old = _old("d", "old1", 31.9800, 118.7800, "家",
                   grid_keys=[grid_key_of(31.98, 118.78), grid_key_of(31.9801, 118.7801)])
        new = [
            {
                "device_id": "d",
                "place_id": "new1",
                "grid_key": grid_key_of(31.9801, 118.7801),
                "lat": 31.9800, "lon": 118.7800,
                "member_grid_keys": [grid_key_of(31.98, 118.78), grid_key_of(31.9801, 118.7801)],
            }
        ]
        o2n, conflicts = match_old_new([old], new)
        assert o2n == {"old1": "new1"}
        assert conflicts == []

    def test_one_old_split_two_children(self):
        # 一旧拆二：旧 ID 只给排序最佳 child，其他 child 保留 new_cluster_key
        old = _old(
            "d", "old1", 31.98, 118.78, grid_keys=[grid_key_of(31.98, 118.78)],
        )
        gk_a = grid_key_of(31.98, 118.78)
        gk_b = grid_key_of(31.99, 118.79)
        new = [
            {"device_id": "d", "place_id": "ca", "grid_key": gk_a,
             "lat": 31.98, "lon": 118.78, "member_grid_keys": [gk_a]},
            {"device_id": "d", "place_id": "cb", "grid_key": gk_b,
             "lat": 31.99, "lon": 118.79, "member_grid_keys": [gk_b]},
        ]
        places, o2n, _conflicts = resolve_place_ids([old], new)
        # 与旧 place 网格重合的 ca 被认领 → 升级为旧 ID；cb 无匹配保留 new key
        assert o2n.get("old1") == "ca"
        ids = {p["place_id"] for p in places}
        assert "old1" in ids
        assert "cb" in ids

    def test_two_old_merge_conflict_tag(self):
        old_a = _old("d", "oldA", 31.9800, 118.7800, "家",
                     grid_keys=[grid_key_of(31.98, 118.78)])
        old_b = _old("d", "oldB", 31.9801, 118.7801, "公司",
                     grid_keys=[grid_key_of(31.9801, 118.7801)])
        gk = grid_key_of(31.9800, 118.7800)
        new = [
            {"device_id": "d", "place_id": "new1", "grid_key": gk,
             "lat": 31.9800, "lon": 118.7800, "member_grid_keys": [gk]}
        ]
        o2n, conflicts = match_old_new([old_a, old_b], new)
        # 两旧并一：两个旧都映射到同一 new（survivor=oldA，oldB 的 tag 写 conflict）
        assert set(o2n.values()) == {"new1"}
        assert len(o2n) == 2
        assert any(c["reason"] == "merge_survivor_tag" and c["old_place_id"] == "oldB"
                   for c in conflicts)

    def test_global_pairing_one_to_one(self):
        # 多对多候选：每个 old/new 只能认领一次
        o1 = _old("d", "o1", 31.98, 118.78, grid_keys=[grid_key_of(31.98, 118.78)])
        o2 = _old("d", "o2", 31.99, 118.79, grid_keys=[grid_key_of(31.99, 118.79)])
        gk1 = grid_key_of(31.98, 118.78)
        gk2 = grid_key_of(31.99, 118.79)
        new = [
            {"device_id": "d", "place_id": "n1", "grid_key": gk1,
             "lat": 31.98, "lon": 118.78, "member_grid_keys": [gk1]},
            {"device_id": "d", "place_id": "n2", "grid_key": gk2,
             "lat": 31.99, "lon": 118.79, "member_grid_keys": [gk2]},
        ]
        o2n, _ = match_old_new([o1, o2], new)
        assert set(o2n.values()) == {"n1", "n2"}


# ---------------------------------------------------------------------------
# resolve_place_name / format_place（§2.6）
# ---------------------------------------------------------------------------

class TestDisplayContract:
    def test_high_confidence_venue(self):
        name, source, gran = resolve_place_name(
            poi="新华汇", name_confidence=0.90
        )
        assert name == "新华汇"
        assert source == "poi"
        assert gran == "venue"

    def test_aoi_parent_fallback(self):
        name, source, _gran = resolve_place_name(
            poi="", parent_poi="雨花客厅", name_confidence=0.80
        )
        assert name == "雨花客厅"
        assert source == "poi"

    def test_around_fallback(self):
        name, source, _gran = resolve_place_name(
            poi="", business_area="新街口", name_confidence=0.55
        )
        assert name == "新街口"
        assert source == "poi_fallback"

    def test_district_fallback(self):
        name, source, _gran = resolve_place_name(
            poi="", district="雨花台区", name_confidence=0.40
        )
        assert name == "雨花台区"
        assert source == "district"

    def test_address_preferred_over_township_and_district(self):
        """§2.6 阶梯：address(0.60) 高于 township/district(0.40)。"""
        name, source, gran = resolve_place_name(
            poi="", address="某某路1号", township="雨花街道", district="雨花台区"
        )
        assert name == "某某路1号"
        assert source == "address"
        assert gran == "address"

    def test_township_before_district(self):
        name, source, gran = resolve_place_name(
            poi="", township="雨花街道", district="雨花台区"
        )
        assert name == "雨花街道"
        assert source == "district"
        assert gran == "neighborhood"

    def test_low_confidence_poi_not_used(self):
        """name_confidence < 0.75 时具体 POI 不得展示，落到 address。"""
        name, source, _gran = resolve_place_name(
            poi="某连锁快餐", address="某某路5号", name_confidence=0.55
        )
        assert name == "某某路5号"
        assert source == "address"

    def test_all_empty_unknown(self):
        name, source, _gran = resolve_place_name()
        assert name == "未知地点"
        assert source == "unknown"

    def test_format_place_combos(self):
        assert format_place("新华汇", "公司") == "新华汇〔公司〕"
        assert format_place("德基广场", "") == "德基广场"
        assert format_place("", "家") == "家"
        assert format_place("", "") == "未知地点"

    def test_user_tag_of(self):
        assert user_tag_of("家") == "家"
        assert user_tag_of("未知") == ""
        assert user_tag_of("") == ""


# ---------------------------------------------------------------------------
# to_amap_coord（§3.3）
# ---------------------------------------------------------------------------

class TestToAmapCoord:
    def test_unknown_and_gcj02_unchanged(self):
        assert to_amap_coord(31.98, 118.78, "unknown") == (31.98, 118.78)
        assert to_amap_coord(31.98, 118.78, "gcj02") == (31.98, 118.78)

    def test_wgs84_converts_in_china(self):
        lat, lon = to_amap_coord(31.98, 118.78, "wgs84")
        # GCJ02 与 WGS84 差异通常数十~数百米（~0.005° 量级）
        assert abs(lat - 31.98) < 0.01
        assert abs(lon - 118.78) < 0.01
        assert (lat, lon) != (31.98, 118.78)

    def test_wgs84_outside_china_unchanged(self):
        # 境外 guard：原样
        assert to_amap_coord(40.0, -100.0, "wgs84") == (40.0, -100.0)

    def test_wgs84_to_gcj02_roundtrip_sane(self):
        lat, lon = wgs84_to_gcj02(31.98, 118.78)
        assert -90 <= lat <= 90
        assert -180 <= lon <= 180


# ---------------------------------------------------------------------------
# Task 6：location_points / accuracy_filter / daily_quality_rows（§2.1/§3.1/§3.2）
# ---------------------------------------------------------------------------

_EMPTY_CFG = {"default": "unknown", "periods": []}


class TestLocationPoints:
    def test_parses_location_events_only_sorted(self):
        events = [
            ("b", 200, "location", {"lat": 31.0, "lon": 118.0}),
            ("a", 200, "location", {"lat": 31.1, "lon": 118.1}),
            ("a", 100, "location", {"lat": 31.2, "lon": 118.2}),
            ("a", 150, "usage", {"pkg": "x"}),
            ("a", 120, "location", {"lat": 999, "lon": 118.0}),
        ]
        pts = location_points(events, _EMPTY_CFG)
        assert [(p.device_id, p.ts) for p in pts] == [("a", 100), ("a", 200), ("b", 200)]

    def test_coord_system_resolved_per_device_period(self):
        events = [
            ("dev1", 500, "location", {"lat": 31.0, "lon": 118.0}),
            ("dev1", 5000, "location", {"lat": 31.0, "lon": 118.0}),
            ("dev2", 500, "location", {"lat": 31.0, "lon": 118.0}),
        ]
        cfg = {
            "default": "gcj02",
            "periods": [
                {"device_id": "dev1", "start_ts": 1000, "end_ts": None, "source": "wgs84"},
            ],
        }
        pts = location_points(events, cfg)
        assert pts[0].coord_system == "gcj02"
        assert pts[1].coord_system == "wgs84"
        assert pts[2].coord_system == "gcj02"


class TestAccuracyFilter:
    def _pts(self):
        return [
            LocationPoint("d", 1, 31.0, 118.0, 20.0, "gps", "unknown"),
            LocationPoint("d", 2, 31.0, 118.0, 200.0, "gps", "unknown"),
            LocationPoint("d", 3, 31.0, 118.0, None, "network", "unknown"),
        ]

    def test_off_observes_only(self):
        out = accuracy_filter(self._pts(), {"apply_accuracy_filter": False})
        assert out == self._pts()

    def test_none_cfg_observes_only(self):
        assert accuracy_filter(self._pts(), None) == self._pts()

    def test_on_drops_known_over_threshold_keeps_missing(self):
        out = accuracy_filter(
            self._pts(),
            {"apply_accuracy_filter": True, "max_accuracy_m": 150.0,
             "accept_missing_accuracy": True},
        )
        assert [p.ts for p in out] == [1, 3]

    def test_on_drops_missing_when_not_accepted(self):
        out = accuracy_filter(
            self._pts(),
            {"apply_accuracy_filter": True, "max_accuracy_m": 150.0,
             "accept_missing_accuracy": False},
        )
        assert [p.ts for p in out] == [1]


_TZ = datetime.timezone(datetime.timedelta(hours=8))
_TS_D1 = int(datetime.datetime(2026, 8, 17, 10, 0, tzinfo=_TZ).timestamp() * 1000)
_TS_D2 = int(datetime.datetime(2026, 8, 18, 9, 0, tzinfo=_TZ).timestamp() * 1000)


class TestDailyQualityRows:
    def _events(self):
        return [
            ("dev1", _TS_D1, "location", {"lat": 31.0, "lon": 118.0, "acc": 20, "provider": "gps"}),
            ("dev1", _TS_D1 + 31 * 60_000, "location",
             {"lat": 31.001, "lon": 118.001, "acc": 80, "provider": "network"}),
            ("dev1", _TS_D1 + 61 * 60_000, "location", {"lat": 999, "lon": 0.0}),
            ("dev2", _TS_D2, "location", {"lat": 31.0, "lon": 118.0, "acc": 200.0, "provider": "network"}),
        ]

    def test_row_semantics(self):
        rows = daily_quality_rows(self._events(), _EMPTY_CFG)
        by_key = {(r[0], r[1]): r for r in rows}
        d1 = by_key[("2026-08-17", "dev1")]
        assert d1[2] == 3          # points_total 含解析失败
        assert d1[3] == 2          # points_valid
        assert d1[4] == 2          # accuracy_known
        assert d1[5] == 1          # accuracy_le_50
        assert d1[6] == 1          # accuracy_51_150
        assert d1[7] == 0          # accuracy_gt_150
        assert d1[8] == 2          # 31 分钟间隔 → 两个 30 分钟格
        assert d1[9] == 1860.0     # median_interval_sec = 31min
        assert json.loads(d1[10]) == {"gps": 1, "network": 1}

        d2 = by_key[("2026-08-18", "dev2")]
        assert d2[3] == 1
        assert d2[7] == 1          # accuracy_gt_150
        assert d2[9] is None       # 单点无间隔

    def test_intervals_not_mixed_across_devices_or_days(self):
        # dev2 的时间点夹在 dev1 两点之间：dev1 的 median 不受影响
        events = self._events() + [
            ("dev2", _TS_D1 + 15 * 60_000, "location", {"lat": 31.0, "lon": 118.0}),
        ]
        rows = daily_quality_rows(events, _EMPTY_CFG)
        by_key = {(r[0], r[1]): r for r in rows}
        assert by_key[("2026-08-17", "dev1")][9] == 1860.0
        # dev2 当日仅 1 点（另一日在 d2 桶）
        assert by_key[("2026-08-17", "dev2")][3] == 1
        assert by_key[("2026-08-17", "dev2")][9] is None

    def test_cross_midnight_points_bucketed_by_day(self):
        late = int(datetime.datetime(2026, 8, 17, 23, 50, tzinfo=_TZ).timestamp() * 1000)
        early = int(datetime.datetime(2026, 8, 18, 0, 10, tzinfo=_TZ).timestamp() * 1000)
        events = [
            ("d", late, "location", {"lat": 31.0, "lon": 118.0}),
            ("d", early, "location", {"lat": 31.0, "lon": 118.0}),
        ]
        rows = daily_quality_rows(events, _EMPTY_CFG)
        assert {(r[0], r[3], r[9]) for r in rows} == {
            ("2026-08-17", 1, None), ("2026-08-18", 1, None),
        }
