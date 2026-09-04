"""langTrack 天气语境（P2-2）：高德天气接口，按日缓存，供画像加环境语境。

用法：
    python -m gacore.langTrack.weather --day 2026-08-20

历史画像日（如 8-19）不在接口返回的预报窗内时，回退到最近可用预报日并在
返回中加入 approx 标记，report 侧据此标注。
"""
from __future__ import annotations

import argparse
import datetime
import json
import urllib.parse
import urllib.request
from pathlib import Path

WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
# 常驻活动区（雨花台区）行政区划代码
ADCODE = "320114"


def _amap_key() -> str:
    env = Path(__file__).resolve().parents[3] / ".env"
    key = ""
    for line in env.read_bytes().splitlines():
        line = line.strip()
        if line.startswith(b"AMAP_KEY="):
            key = line.split(b"=", 1)[1].decode("utf-8", errors="ignore").strip().strip('"').strip("'")
            break
    if not key:
        raise RuntimeError("AMAP_KEY 未配置（.env）")
    return key


def get_weather(day: str, cache_path: Path | None = None) -> dict:
    """获取指定日天气：优先缓存 data/weather_cache.json，未命中才调高德天气接口。

    高德天气 extensions=all 仅返回当天起未来 3 天预报；若画像日不在预报窗内，
    回退到窗口内最早预报日并打 approx 标记（表示用近期预报近似当日天气）。
    """
    cache_path = cache_path or (Path(__file__).resolve().parents[3] / "data" / "weather_cache.json")
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    if day in cache and cache[day].get("status") == "1":
        return cache[day]

    params = urllib.parse.urlencode({
        "city": ADCODE,
        "key": _amap_key(),
        "extensions": "all",
    })
    try:
        with urllib.request.urlopen(f"{WEATHER_URL}?{params}", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[weather] 天气接口失败: {e}")
        return {}
    if data.get("status") != "1" or not data.get("forecasts"):
        print(f"[weather] 天气接口异常: {data.get('info')}")
        return {}
    fc = data["forecasts"][0]
    casts = {c["date"]: c for c in fc.get("casts", [])}
    if not casts:
        return {}
    target = day if day in casts else sorted(casts)[0]  # 历史日回退到窗口内最早预报日
    c = casts[target]
    result = {
        "status": "1",
        "city": fc.get("city", ""),
        "date": target,
        "text_day": c.get("dayweather", ""),
        "text_night": c.get("nightweather", ""),
        "temp_max": c.get("daytemp", ""),
        "temp_min": c.get("nighttemp", ""),
        "wind": c.get("daywind", ""),
        "approx": target != day,
    }
    cache[target] = result
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[weather] 缓存写入失败: {e}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="langTrack 天气语境（P2-2）")
    parser.add_argument("--day", default=datetime.date.today().isoformat())
    args = parser.parse_args()
    w = get_weather(args.day)
    if w:
        print(json.dumps(w, ensure_ascii=False))
    else:
        print("无天气数据")


if __name__ == "__main__":
    main()
