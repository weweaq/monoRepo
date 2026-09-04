"""langTrack ETL 配置加载：阈值 / 回看窗口 / 默认设备等外置到 data/etl_config.json。

默认配置集中在 DEFAULTS；若 data/etl_config.json 存在且可解析，则按层级合并用户配置，
任一节点解析失败都回退到默认（不会因坏配置崩 ETL）。
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[3] / "data" / "etl_config.json"

DEFAULTS: dict = {
    "location": {
        # §3.1 坐标质量过滤：首版只观测（filter=False），dashboard 实测分布后由用户确认开启
        "max_accuracy_m": 150.0,
        "accept_missing_accuracy": True,
        "apply_accuracy_filter": False,
        # §2.5 geocode 失效阈值：新旧中心偏移超过该值时清空派生字段待重编
        "regeo_shift_m": 50.0,
    },
    "stays": {
        "large_radius_m": 120.0,
        "small_radius_m": 60.0,
        "min_duration_ms": 600000,
        "merge_gap_ms": 300000,
        "merge_radius_m": 150.0,
        "max_jump_m": 500.0,
        "max_speed_mps": 40.0,
    },
    "trips": {
        "min_duration_ms": 60000,
        "min_dist_m": 300.0,
        # 相邻 stay 间隙超过该值不推断为 trip（§3.1）
        "max_infer_gap_ms": 7200000,
    },
    "incremental": {
        "lookback_days": 2,
    },
    "anomaly": {
        "night_start_h": 23,
        "night_end_h": 5,
        "new_place_lookback_days": 7,
    },
    "device": {
        # 默认设备覆盖（留空则取 events 中最近活跃设备）；dashboard / report / 工具层共用
        "default_device": None,
    },
}


def _deep_update(base: dict, override: dict) -> dict:
    """层级合并 override 到 base（仅 dict 节点递归，其他直接覆盖）。"""
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def load_etl_config() -> dict:
    """读取 ETL 配置；文件缺失 / 损坏 → 返回 DEFAULTS 深拷贝。"""
    cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝，避免修改默认单例
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _deep_update(cfg, user)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            cfg = json.loads(json.dumps(DEFAULTS))
    return cfg


# ---------------------------------------------------------------------------
# 坐标制配置（§3.1）：data/location_coord_systems.json，支持设备 + 历史区间
# ---------------------------------------------------------------------------

COORD_SYSTEMS_PATH = Path(__file__).resolve().parents[3] / "data" / "location_coord_systems.json"

VALID_COORD_SYSTEMS: tuple[str, ...] = ("unknown", "wgs84", "gcj02")


class CoordSystemConfigError(ValueError):
    """坐标制配置错误（非法 source / period 重叠），必须拒绝 ETL 而不是静默猜测。"""


def load_coord_systems(path: Path | None = None) -> dict:
    """读取坐标制配置；文件缺失返回 {"default": "unknown", "periods": []}。

    解析失败（坏 JSON / 非法 source / period 结构错误）抛 CoordSystemConfigError——
    坐标制错读会导致全链路位移，宁可拒绝也不回退默认。
    """
    p = path or COORD_SYSTEMS_PATH
    if not p.exists():
        return {"default": "unknown", "periods": []}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        raise CoordSystemConfigError(f"coord systems file unreadable: {e}") from e

    default = raw.get("default", "unknown")
    if default not in VALID_COORD_SYSTEMS:
        raise CoordSystemConfigError(f"invalid default coord system: {default!r}")

    periods_raw = raw.get("periods", [])
    if not isinstance(periods_raw, list):
        raise CoordSystemConfigError("periods must be a list")

    periods: list[dict] = []
    for i, pr in enumerate(periods_raw):
        if not isinstance(pr, dict):
            raise CoordSystemConfigError(f"periods[{i}] must be an object")
        source = pr.get("source")
        if source not in VALID_COORD_SYSTEMS:
            raise CoordSystemConfigError(f"periods[{i}].source invalid: {source!r}")
        device_id = pr.get("device_id")
        if not isinstance(device_id, str) or not device_id.strip():
            raise CoordSystemConfigError(
                f"periods[{i}].device_id missing/empty (any non-empty device string)"
            )
        start_ts = pr.get("start_ts")
        end_ts = pr.get("end_ts")
        if not isinstance(start_ts, (int, float)) or isinstance(start_ts, bool):
            raise CoordSystemConfigError(f"periods[{i}].start_ts must be a number")
        if end_ts is not None and (not isinstance(end_ts, (int, float)) or isinstance(end_ts, bool)):
            raise CoordSystemConfigError(f"periods[{i}].end_ts must be a number or null")
        if end_ts is not None and end_ts <= start_ts:
            raise CoordSystemConfigError(
                f"periods[{i}] empty interval: end_ts ({end_ts}) <= start_ts ({start_ts})"
            )
        periods.append(
            {
                "device_id": device_id,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "source": source,
            }
        )

    # 同设备 period 重叠检测（半开区间 [start,end)，end=None 无上界）
    by_device: dict[str, list[dict]] = {}
    for pr in periods:
        by_device.setdefault(pr["device_id"], []).append(pr)
    for device_id, plist in by_device.items():
        plist.sort(key=lambda x: x["start_ts"])
        for a, b in itertools.pairwise(plist):
            a_end = a["end_ts"]
            if a_end is None or b["start_ts"] < a_end:
                raise CoordSystemConfigError(
                    f"overlapping coord system periods for device {device_id!r}: "
                    f"[{a['start_ts']},{a_end}) vs [{b['start_ts']},{b['end_ts']})"
                )
    return {"default": default, "periods": periods}


def resolve_coord_system(device_id: str, ts: int, cfg: dict) -> str:
    """解析 (device_id, ts) 的坐标制：返回覆盖该时刻的唯一 period source，无匹配用 default。

    period 为半开区间 [start_ts, end_ts)；end_ts=None 表示无上界。
    """
    for pr in cfg.get("periods", []):
        if pr.get("device_id") != device_id:
            continue
        start_ts = pr.get("start_ts")
        end_ts = pr.get("end_ts")
        if ts >= start_ts and (end_ts is None or ts < end_ts):
            return pr["source"]
    return cfg.get("default", "unknown")
