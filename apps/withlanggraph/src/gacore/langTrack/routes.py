"""langTrack L3 移动轨迹段（trips）与路线变化事件。

给相邻停驻点（stays）之间的移动段用高德路径规划 API 补路线坐标（polyline），
生成无向规范化路线指纹 route_key，比对相邻出行识别通勤路线变化。

配额节流（个人认证开发者免费档）：
- 路径规划 walking 5000 次/日：仅对未编码新段增量调用，单次 ETL 默认上限 100 段
- 周边搜索 around 仅 100 次/日：本模块 P0 不调用（沿途 POI 留待 P2 网格缓存低频）
- 逆地理 regeo 5000 次/日：不动

用法：
    python -m gacore.langTrack.routes            # 增量补路（仅未编码新段）
    python -m gacore.langTrack.routes --all      # 强制全部重编（慎用，烧配额）
依赖环境变量 AMAP_KEY（.env 中配置，复用 geocode 的 Key）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import datetime  # noqa: E402  (routes 仅在此模块用 datetime，局部已无顶层导入)

from gacore.langTrack.location_facts import to_amap_coord, warn_unknown_coord_once

_TZ_CST = datetime.timezone(datetime.timedelta(hours=8))

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "langTrack.db"
# 步行路径规划（上限 100km，最宽）；骑行/驾车可经 LANGTRACK_ROUTE_MODE 切换
DIRECTION_URL = "https://restapi.amap.com/v3/direction/walking"
# 周边搜索（免费配额仅 100 次/日，P2 沿途 POI 必须网格级缓存 + 低频克制）
AROUND_URL = "https://restapi.amap.com/v3/place/around"
# 网格边长（route_key 与路过网格统计共用，≈330m，吸收端点微差）
_GRID = 0.003

# 移动段识别阈值
TRIP_MIN_DURATION_MS = 60_000          # 移动段最短时长 60s
TRIP_MIN_DIST_M = 300.0                # 起终点直线距离 >= 300m 才算移动段
# 无采样点"推断移动段"的间隔时长上限（默认 2h）：
# 相邻停驻点 gap 内没有任何 location 采样点、且间隔超长时，无法证明存在单次端到点移动，
# 通常对应设备采集空窗（如下班后 GPS 停采到深夜），跳过以免把停留间隔误记为移动时长
TRIP_MAX_INFER_GAP_MS = int(os.environ.get("LANGTRACK_TRIP_MAX_INFER_GAP_MS", "7200000"))
# 配额节流：单次 ETL 增量补路上限（远低于 5000 次/日）
_MAX_ENCODE_PER_RUN = int(os.environ.get("LANGTRACK_ROUTE_MAX_PER_RUN", "100"))
_ROUTE_MODE = os.environ.get("LANGTRACK_ROUTE_MODE", "walking")


def _amap_key() -> str:
    """复用 geocode 的 Key 读取逻辑（环境变量 AMAP_KEY 或 .env）。"""
    from gacore.langTrack import geocode
    return geocode._amap_key()


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def build_trips(
    points,
    stays,
    min_duration_ms: int = TRIP_MIN_DURATION_MS,
    min_dist_m: float = TRIP_MIN_DIST_M,
    max_infer_gap_ms: int = TRIP_MAX_INFER_GAP_MS,
) -> list[tuple]:
    """识别移动段：相邻两个停留段之间的区间。

    points: location_facts.location_points 产物（LocationPoint 列表，Task 6 共用解析）；
    stays: build_stays 的 tuple 列表。阈值全部使用传入参数（Task 6：不再引用模块
    常量，与 etl_config trips 节点同一配置来源）。
    规则：
    - 对每设备按 start_ts 排序 stays，相邻对 (A, B) 的 gap=[A.end_ts, B.start_ts]
    - gap 内的 location 点即移动采样点；起终点坐标取 gap 内首/末点（无采样点则用 A/B 中心兜底）
    - 双阈值过滤：duration >= min_duration_ms 且起终点直线距离 >= min_dist_m（滤小区内挪动与 GPS 抖动）
    返回: [(device_id, start_ts, end_ts, duration_ms, start_lat, start_lon,
            end_lat, end_lon, dist_m, n_points, day)]
    """
    # location 点按设备、按时间索引
    by_dev: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for p in points:
        by_dev[p.device_id].append((p.ts, p.lat, p.lon))
    for evs in by_dev.values():
        evs.sort(key=lambda e: e[0])

    # stays 按设备、按 start_ts 排序
    stays_by_dev: dict[str, list[tuple]] = defaultdict(list)
    for s in stays:
        stays_by_dev[s[0]].append(s)
    for lst in stays_by_dev.values():
        lst.sort(key=lambda s: s[1])
    trips: list[tuple] = []
    for device_id, segs in stays_by_dev.items():
        pts = by_dev.get(device_id, [])
        # 指针式取 gap 内点（stays 有序、pts 有序）
        for i in range(len(segs) - 1):
            a, b = segs[i], segs[i + 1]
            gap_start, gap_end = a[2], b[1]  # a.end_ts, b.start_ts
            if gap_end - gap_start < min_duration_ms:
                continue
            # gap 内采样点
            lo = 0
            in_gap: list[tuple[int, float, float]] = []
            for j in range(lo, len(pts)):
                if pts[j][0] >= gap_end:
                    break
                if pts[j][0] > gap_start:
                    in_gap.append(pts[j])
                lo = j
            if in_gap:
                start_ts, start_lat, start_lon = in_gap[0]
                end_ts, end_lat, end_lon = in_gap[-1]
            else:
                # 无采样点：仅当间隔在合理推断时长内才用前后停留中心兜底构造"推断移动段"；
                # 超长间隔常见于设备采集空窗（如下班后 GPS 停采到深夜），无法证明存在单次
                # 端到点移动，跳过以免把停留间隔误记为移动时长（08-21 17:51→00:08 6.28h 案例）
                if gap_end - gap_start > max_infer_gap_ms:
                    continue
                start_ts, start_lat, start_lon = gap_start, a[4], a[5]
                end_ts, end_lat, end_lon = gap_end, b[4], b[5]
            dist = _haversine(start_lat, start_lon, end_lat, end_lon)
            if dist < min_dist_m:
                continue
            # 时长统一取端点到点时刻差，保证 duration_ms 与 (end_ts - start_ts) 自洽
            # （原实现时长取 gap 全程，导致有采样旅行段 duration 与端点矛盾，如 08-19 4.12h vs 2.50h）
            duration = end_ts - start_ts
            day = datetime.datetime.fromtimestamp(start_ts / 1000, tz=_TZ_CST).strftime("%Y-%m-%d")
            trips.append((
                device_id, start_ts, end_ts, duration,
                round(start_lat, 6), round(start_lon, 6),
                round(end_lat, 6), round(end_lon, 6),
                round(dist, 1), len(in_gap), day,
            ))
    trips.sort(key=lambda t: (t[0], t[1]))
    return trips


def _normalize_polyline(points: list[tuple[float, float]], max_points: int = 20, grid: float = 0.003) -> str:
    """等距抽稀 + 网格量化，返回无向无序网格集合点串。

    - 等距抽稀到最多 max_points 个点（消除步长相位噪声，抽稀结果与采样密度无关）
    - 网格量化 round(*1000)/1000 → 0.003° ≈ 330m。粗网格吸收端点微差
      （每次出行起终点是停留中心/最后采样点，可能差几百米；0.003° 网格下
      同一条通勤路往返、不同天重复走，网格集合基本一致）
    - 无序集合：去重后排序。与方向（去/回）、途经顺序无关——
      只有真正绕路换线（经过的网格集合显著变化）才会产生不同指纹。
    """
    if not points:
        return ""
    pts = points
    if len(pts) > max_points:
        idx = [round(i * (len(pts) - 1) / (max_points - 1)) for i in range(max_points)]
        pts = [pts[i] for i in idx]
    cells = set()
    for la, lo in pts:
        gk_la = round(la / grid) * grid
        gk_lo = round(lo / grid) * grid
        cells.add(f"{gk_la:.3f},{gk_lo:.3f}")
    return ";".join(sorted(cells))


def route_key_of(polyline_points: list[tuple[float, float]]) -> str:
    """由 polyline 点列生成 route_key（sha256 前 16 位）。无点返回空串。"""
    norm = _normalize_polyline(polyline_points)
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def direction_polyline(
    lat1: float, lon1: float, lat2: float, lon2: float,
    key: str, mode: str = "walking",
    coord_system: str = "unknown",
    coord_system_end: str | None = None,
) -> list[tuple[float, float]] | None:
    """高德路径规划：返回路线坐标点列表 [(lat, lon), ...]；失败返回 None。

    起终点按各自 coord_system 经 to_amap_coord 转换后请求（§3.3：所有高德
    入口统一转换）；coord_system_end 为 None 时终点沿用 coord_system（两端
    同制）。跨坐标制时段边界的行程两端可分别声明，避免按起点制二次偏移
    终点。返回的 polyline 固定为高德 GCJ02 坐标（由调用方落
    polyline_coord_system，本函数不落库）。
    """
    cs_end = coord_system_end if coord_system_end is not None else coord_system
    if (coord_system or "unknown").strip().lower() == "unknown" or (cs_end or "unknown").strip().lower() == "unknown":
        warn_unknown_coord_once("routes")
    lat1, lon1 = to_amap_coord(lat1, lon1, coord_system)
    lat2, lon2 = to_amap_coord(lat2, lon2, cs_end)
    url = DIRECTION_URL if mode == "walking" else DIRECTION_URL.replace("/walking", f"/{mode}")
    params = urllib.parse.urlencode({
        "origin": f"{lon1},{lat1}",
        "destination": f"{lon2},{lat2}",
        "key": key,
    })
    try:
        with urllib.request.urlopen(f"{url}?{params}", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[routes] 路径规划失败 ({lat1},{lon1})->({lat2},{lon2}): {e}")
        return None
    if data.get("status") != "1":
        print(f"[routes] 路径规划业务失败: {data.get('info')}")
        return None
    try:
        steps = data["route"]["paths"][0]["steps"]
    except (KeyError, IndexError):
        print(f"[routes] 路径规划响应无路线: {data.get('info')}")
        return None
    points: list[tuple[float, float]] = []
    for st in steps:
        seg = (st.get("polyline") or "").strip()
        if not seg:
            continue
        for token in seg.split(";"):
            if not token:
                continue
            try:
                lon_s, lat_s = token.split(",")
                points.append((float(lat_s), float(lon_s)))
            except (ValueError, AttributeError):
                continue
    return points or None


def incremental_encode_trips(db_path: Path = DB_PATH, max_n: int = _MAX_ENCODE_PER_RUN) -> int:
    """增量补路：仅对 route_encoded_at IS NULL 的新移动段调高德路径规划。

    成功才写 polyline/route_key/route_mode/route_encoded_at（polyline 为高德
    GCJ02，同步标记 polyline_coord_system='gcj02'，§3.3）；失败跳过留待下次
    重试。起终点各自按 (device_id, start_ts/end_ts) 现行配置解析坐标制并经
    to_amap_coord 转换（跨坐标制时段边界的行程两端独立转换；trips. endpoint_coord_system 单列记录起点制，配置在 ETL 后被修正时以本次
    重解析为准）。单次默认最多 max_n 段（配额节流，远低于 5000 次/日）。
    返回成功编码数。
    """
    from gacore.langTrack.etl_config import load_coord_systems, resolve_coord_system

    key = _amap_key()
    coord_cfg = load_coord_systems()  # 配置错误（重叠 period 等）直接拒绝本次补路
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, device_id, start_ts, end_ts, start_lat, start_lon, end_lat, end_lon FROM trips "
        "WHERE route_encoded_at IS NULL ORDER BY start_ts LIMIT ?", (max_n,)
    ).fetchall()
    if not rows:
        print("[routes] 无新增待补路移动段（增量缓存命中，零调用）")
        conn.close()
        return 0
    print(f"[routes] 待补路移动段: {len(rows)} 段（本次上限 {max_n}）")
    now = int(time.time() * 1000)
    n = 0
    for r in rows:
        # 高德个人档 QPS 较低，连续快速调用会触发 CUQPS 限流；每段间隔 0.5s
        time.sleep(0.5)
        cs_start = resolve_coord_system(r["device_id"], r["start_ts"], coord_cfg)
        cs_end = resolve_coord_system(r["device_id"], r["end_ts"], coord_cfg)
        pts = direction_polyline(
            r["start_lat"], r["start_lon"], r["end_lat"], r["end_lon"],
            key, _ROUTE_MODE, coord_system=cs_start, coord_system_end=cs_end,
        )
        if not pts:
            print(f"[routes] 跳过段 id={r['id']}（规划失败，留待下次）")
            continue
        rk = route_key_of(pts)
        conn.execute(
            "UPDATE trips SET polyline=?, route_key=?, route_mode=?, route_encoded_at=?, "
            "polyline_coord_system='gcj02' WHERE id=?",
            (json.dumps(pts, ensure_ascii=False), rk, _ROUTE_MODE, now, r["id"]),
        )
        n += 1
    conn.commit()
    conn.close()
    print(f"[routes] 补路完成: {n} 段")
    return n


def run(db_path: Path = DB_PATH, force_all: bool = False) -> None:
    _amap_key()  # 提前校验 Key
    if force_all:
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE trips SET route_encoded_at=NULL, polyline=NULL, route_key=NULL, route_mode=NULL, polyline_coord_system=NULL")
        conn.commit()
        conn.close()
    incremental_encode_trips(db_path)


def _grid_of(lat: float, lon: float, grid: float = _GRID) -> tuple[float, float]:
    """坐标 → 网格键（与 route_key 网格量化一致，≈330m）。"""
    return round(lat / grid) * grid, round(lon / grid) * grid


def build_route_grids(db_path: Path = DB_PATH) -> int:
    """P1 路过网格统计（通勤带）：从 trips.polyline 全量重建 route_grids 表。

    纯本地计算（网格量化 + 聚合），零高德配额。每段路线的网格集合去重后，
    按 (device_id, day, grid) 累计 n_pass（每段经过该网格计 1 次）。
    高频多日网格 = 通勤走廊/常走路段。返回写入行数。
    """
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM route_grids")
    rows = conn.execute(
        "SELECT device_id, day, polyline FROM trips WHERE polyline IS NOT NULL"
    ).fetchall()
    agg: dict[tuple, int] = defaultdict(int)
    for device_id, day, pl in rows:
        try:
            pts = json.loads(pl)
        except Exception:
            continue
        cells = set()
        for lat, lon in pts:
            cells.add(_grid_of(lat, lon))
        for gk_lat, gk_lon in cells:
            agg[(device_id, day, gk_lat, gk_lon)] += 1
    now = int(time.time() * 1000)
    data = [
        (dev, day, gk_lat, gk_lon, cnt, now)
        for (dev, day, gk_lat, gk_lon), cnt in agg.items()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO route_grids(device_id, day, grid_lat, grid_lon, n_pass, updated_at) "
        "VALUES (?,?,?,?,?,?)", data,
    )
    conn.commit()
    conn.close()
    print(f"[routes] 路过网格统计(通勤带): {len(data)} 行")
    return len(data)


def encode_belt_pois(db_path: Path = DB_PATH, limit: int = 5) -> int:
    """P2 沿途 POI：对通勤带高频网格增量 around 查询（网格级缓存，低频克制）。

    around 免费配额仅 100 次/日，必须克制：只取 route_grids 中按累计路过次数
    排名靠前、且 grid_pois 尚未缓存的网格；每网格查最近 1 个 POI 落库。
    默认单次最多 limit 个（5），多轮 ETL 累积覆盖整个通勤带。返回新增 POI 网格数。
    """
    key = _amap_key()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cand = conn.execute(
        "SELECT g.grid_lat AS glat, g.grid_lon AS glon, SUM(g.n_pass) AS total "
        "FROM route_grids g LEFT JOIN grid_pois p "
        "  ON g.grid_lat = p.grid_lat AND g.grid_lon = p.grid_lon "
        "WHERE p.grid_lat IS NULL "
        "GROUP BY g.grid_lat, g.grid_lon ORDER BY total DESC LIMIT ?", (limit,)
    ).fetchall()
    if not cand:
        print("[routes] 无新增沿途 POI 待查网格（网格级缓存命中，零调用）")
        conn.close()
        return 0
    n = 0
    now = int(time.time() * 1000)
    for r in cand:
        time.sleep(0.5)
        # 网格坐标来自高德 polyline 量化，已是 GCJ02（§3.3 belt POI 固定按
        # source="gcj02" 放行，禁止再次 WGS84→GCJ02 造成二次偏移）
        glat, glon = to_amap_coord(r["glat"], r["glon"], "gcj02")
        params = urllib.parse.urlencode({
            "location": f"{glon},{glat}",
            "key": key, "radius": 300, "offset": 1, "extensions": "base",
        })
        try:
            with urllib.request.urlopen(f"{AROUND_URL}?{params}", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[routes] around 失败 ({r['glat']},{r['glon']}): {e}")
            continue
        if data.get("status") != "1":
            print(f"[routes] around 业务失败: {data.get('info')}")
            continue
        pois = data.get("pois") or []
        if not pois:
            conn.execute(
                "INSERT OR REPLACE INTO grid_pois(grid_lat, grid_lon, name, type, distance, queried_at) "
                "VALUES (?,?,?,?,?,?)",
                (r["glat"], r["glon"], "", "", None, now),
            )
            continue
        po = pois[0]
        conn.execute(
            "INSERT OR REPLACE INTO grid_pois(grid_lat, grid_lon, name, type, distance, queried_at) "
            "VALUES (?,?,?,?,?,?)",
            (r["glat"], r["glon"], po.get("name", ""), po.get("type", ""), po.get("distance"), now),
        )
        n += 1
    conn.commit()
    conn.close()
    print(f"[routes] 沿途 POI 编码: {n} 个网格")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="高德路径规划补路（L3 移动轨迹段增量编码）")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--all", action="store_true", help="强制全部重编（慎用，烧配额）")
    args = parser.parse_args()
    run(args.db, force_all=args.all)


if __name__ == "__main__":
    main()
