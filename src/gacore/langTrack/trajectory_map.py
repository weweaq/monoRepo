"""当日运动轨迹地图：把 trips 的 polyline 经高德「静态地图」API 渲染成 PNG。

背景（2026-09-07）：langTrack 已有完整轨迹能力（trips.polyline, GCJ02），
dashboard 用高德 JS API 在浏览器画线；但邮件不支持 JS，日报里看不到轨迹图。
此模块复用同源 trips 数据，调 restapi.amap.com/v3/staticmap 把当日轨迹静态化成
一张 PNG，供日报邮件内嵌（send_email image_paths → cid:photoN）。

要点：
- 坐标顺序：API 要求 lon,lat；trips.polyline 存的是 [lat,lon]，须反转。
- 取景：不传 location/zoom，让静态图接口按覆盖物几何自动取景（不覆盖任何
  trips 点时仍可手动给 zoom/location 兜底）。
- 稳定性：无 key / 无轨迹 / 网络失败一律返回 None，绝不抛异常拖垮日报。
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

from gacore.jsonl_logger import get_logger

logger = get_logger("trajectory_map")

# 高德 WebService 型 Key（静态地图接口只认这个；AMAP_JS_KEY 是 JS 型，不可混用）
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
_TRAJ_COLOR = "0x00B3A4"       # 主轨迹线（青绿，浅底图上对比足够）
_PATH_WEIGHT = 7               # 主线线宽
_ZOOM_FALLBACK = "12"          # 无任何覆盖物时的兜底缩放
_LOC_FALLBACK = "118.80,31.97"  # 无轨迹时的兜底中心（南京西→马鞍山东一带）

# 抽稀上限：静态图对整条 URL 长度有硬限制（约 ≤ 8K），点太多会撑爆返回 20003。
# 单段 paths 均匀取 40 点，轨迹多段时进一步压缩。markers/paths 各自独立计数。
_MAX_PTS_PER_TRIP = 40
_MAX_PTS_LOW = 24              # 多段轨迹时每段降到的点数（控制总 URL）
_MAX_TRIPS = 4


def _read_env_value(var: str) -> str:
    """字节查找 .env（规避 GBK/UTF-8 编码坑，编码无关）。"""
    try:
        data = _ENV_PATH.read_bytes()
    except OSError:
        return ""
    for line in data.split(b"\n"):
        if line.startswith(var.encode() + b"="):
            return line.split(b"=", 1)[1].strip().decode()
    return ""


def _load_paths(conn: sqlite3.Connection, day: str, device_id: str | None = None) -> list[list[list[float]]]:
    """取当日 trips 的 polyline 轨迹线（每条为 [lat,lon] 列表）。

    device_id 为空时读该 day 全部 trips —— trips 已由 ETL 归并到主设备，无需纠结别名。
    """
    try:
        if device_id:
            rows = conn.execute(
                "SELECT polyline FROM trips "
                "WHERE day=? AND device_id=? AND polyline IS NOT NULL AND polyline!='' "
                "ORDER BY start_ts",
                (day, device_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT polyline FROM trips "
                "WHERE day=? AND polyline IS NOT NULL AND polyline!='' "
                "ORDER BY start_ts",
                (day,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    paths: list[list[list[float]]] = []
    for (ply,) in rows:
        try:
            raw = json.loads(ply)
        except (ValueError, TypeError):
            continue
        path = [
            [float(p[0]), float(p[1])]
            for p in raw
            if isinstance(p, (list, tuple)) and len(p) == 2
        ]
        if len(path) >= 2:
            paths.append(path)
    return paths


def _thin(path: list[list[float]], maxpts: int) -> list[list[float]]:
    """均匀抽稀到 maxpts 个点（保首尾）。"""
    if len(path) <= maxpts:
        return path
    keep = sorted({0, len(path) - 1, *(round(i * (len(path) - 1) / (maxpts - 1)) for i in range(1, maxpts - 1))})
    return [path[i] for i in keep]


def _build_params(paths: list[list[list[float]]]) -> dict[str, str]:
    """paths 与 markers 参数：单条主线（不叠加描边控 URL），起点终点用系统标记。"""
    n = len(paths)
    per = _MAX_PTS_PER_TRIP if n <= 2 else _MAX_PTS_LOW
    lonlat = [_thin([[lon, lat] for lat, lon in pts], per) for pts in paths[: _MAX_TRIPS]]
    # 静态图对整条 URL 有硬限制（约 ≤ 8K），按总点数再次压缩到预算内
    total = sum(len(s) for s in lonlat)
    budget = 70
    if total > budget:
        k = budget / total
        lonlat = [_thin(s, max(4, int(len(s) * k))) for s in lonlat]

    path_segs: list[str] = []
    for seg in lonlat:
        coords = ";".join(f"{p[0]:.6f},{p[1]:.6f}" for p in seg)
        path_segs.append(f"{_PATH_WEIGHT},{_TRAJ_COLOR},1,,:{coords}")
    paths_param = "|".join(path_segs)

    markers: list[str] = []
    if lonlat:
        first = lonlat[0][0]
        last = lonlat[-1][-1]
        markers.append(f"mid,0x00B3A4,A:{first[0]:.6f},{first[1]:.6f}")
        markers.append(f"mid,0xFF4D4D,B:{last[0]:.6f},{last[1]:.6f}")
    markers_param = "|".join(markers)

    params = {"key": _read_env_value("AMAP_KEY"), "size": "800*520"}
    if paths_param:
        params["paths"] = paths_param
        if markers_param:
            params["markers"] = markers_param
    else:
        # 无轨迹：给个兜底中心/缩放（open）
        params["location"] = _LOC_FALLBACK
        params["zoom"] = _ZOOM_FALLBACK
    return params


def render_day_trajectory(
    conn: sqlite3.Connection, day: str, out_path: Path, device_id: str | None = None
) -> Path | None:
    """把当日运动轨迹渲染成一张 PNG 存到 out_path；成功返回 Path，任何失败返回 None。

    device_id 为空时展示当日全部（ETL 已归并主设备）。语义：轨迹是画像的可视化
    佐证（C2），宁可缺图也不因地图失败影响日报正文。
    """
    key = _read_env_value("AMAP_KEY")
    if not key:
        logger.warning("trajectory_map skipped: AMAP_KEY not configured")
        return None
    try:
        paths = _load_paths(conn, day, device_id)
    except sqlite3.Error as exc:
        logger.error("trajectory_map load failed", error_type=type(exc).__name__, stack_trace=str(exc))
        return None
    if not paths:
        logger.info("trajectory_map no trips", day=day)
        return None

    params = _build_params(paths)
    url = "https://restapi.amap.com/v3/staticmap?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning(
            "trajectory_map fetch failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
        )
        return None
    if not ctype.startswith("image/"):
        logger.warning("trajectory_map non-image response", content_type=ctype, body=data[:300].decode("utf-8", "replace"))
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    logger.info("trajectory_map rendered", day=day, bytes=len(data), path=str(out_path))
    return out_path


def trip_summary_text(conn: sqlite3.Connection, day: str, device_id: str | None = None) -> str:
    """当日行程文字摘要（一条），供日报正文引用；无行程返回空串。

    直接用 trips 实例化过的 dist_m / duration_ms（比 polyline 重算可靠）。
    """
    try:
        if device_id:
            rows = conn.execute(
                "SELECT dist_m,duration_ms,route_mode FROM trips "
                "WHERE day=? AND device_id=? ORDER BY start_ts",
                (day, device_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT dist_m,duration_ms,route_mode FROM trips "
                "WHERE day=? ORDER BY start_ts",
                (day,),
            ).fetchall()
    except sqlite3.OperationalError:
        return ""
    if not rows:
        return ""
    n = len(rows)
    total_km = sum((r[0] or 0) for r in rows) / 1000.0
    total_min = sum((r[1] or 0) for r in rows) / 60000.0
    return (
        f"- 当日运动行程：共 {n} 段移动，累计约 {total_km:.1f} km、约 {total_min:.0f} 分钟"
        f"（A 起点 → B 终点，地图见文末）"
    )