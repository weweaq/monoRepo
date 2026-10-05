"""手工语义地点配置（v3.4）：给常驻点/区县挂"人的语义"——标签、背景、人文注记。

动机：ETL/regeo 只能给出地理事实（POI 名、行政区划），"张垛=张威的老家"这类
只有用户知道的语义必须由用户手工配置。本模块读 data/place_semantics.json
（gitignore 内的用户数据，与 place_labels.json 同理），显示层叠加，不改 DB。

配置格式（JSON，全部字段可选、匹配键至少给一个）：
    {
      "places": [
        {"place_id": "7afcc1e4576ce7fc", "poi": "张垛",
         "tag": "张威的老家", "note": "安徽省马鞍山市当涂县乌溪镇"}
      ],
      "districts": [
        {"district": "当涂县", "note": "（手写人文/背景注记）"}
      ]
    }

匹配规则：
- places 条目：place_id 命中优先，其次 poi 名命中（条目内可同时给两个键做双保险）；
  多条命中取第一条；tag 覆盖 DB label（含 家/公司），note 追加展示；
- districts 条目：按 district 名精确匹配，note 作为区县背景注记。

读取容错：文件缺失/坏 JSON/坏结构 → 空配置（显示层无叠加，不抛异常挡卡）。
mtime 缓存：同一 mtime 不重复解析，手工改文件即生效（ETL 重跑/刷新页面无需重启）。
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[3] / "data" / "place_semantics.json"

_EMPTY: dict = {"places": [], "districts": []}
_cache_path: Path | None = None
_cache_mtime: float | None = None
_cache_doc: dict = _EMPTY


def _load_doc(path: Path) -> dict:
    """读文件 → 规范化 doc；缺失/坏文件返回空配置（显示层静默无叠加）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _EMPTY
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return _EMPTY
    return normalize_doc(raw)


def normalize_doc(raw: object) -> dict:
    """规范化配置文档：非法条目跳过，不抛（手写文件允许局部出错）。"""
    if not isinstance(raw, dict):
        return _EMPTY
    places: list[dict] = []
    for r in raw.get("places") or []:
        if not isinstance(r, dict):
            continue
        pid = r.get("place_id")
        poi = r.get("poi")
        if not isinstance(pid, str) or not pid:
            pid = ""
        if not isinstance(poi, str) or not poi:
            poi = ""
        if not pid and not poi:
            continue  # 无匹配键的条目无法命中，跳过
        places.append({
            "place_id": pid,
            "poi": poi,
            "tag": r.get("tag") if isinstance(r.get("tag"), str) else "",
            "note": r.get("note") if isinstance(r.get("note"), str) else "",
        })
    districts: list[dict] = []
    for r in raw.get("districts") or []:
        if not isinstance(r, dict):
            continue
        name = r.get("district")
        if not isinstance(name, str) or not name:
            continue
        districts.append({
            "district": name,
            "note": r.get("note") if isinstance(r.get("note"), str) else "",
        })
    return {"places": places, "districts": districts}


def load(path: Path | str | None = None) -> dict:
    """加载语义配置（mtime 缓存）；path 可注入（测试隔离，避免读仓库真实 data/）。"""
    global _cache_path, _cache_mtime, _cache_doc
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return _EMPTY
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _EMPTY
    if p == _cache_path and mtime == _cache_mtime:
        return _cache_doc
    doc = _load_doc(p)
    _cache_path, _cache_mtime, _cache_doc = p, mtime, doc
    return doc


def resolve_tag(place_id: str | None, poi: str, db_label: str, cfg: dict | None = None) -> str:
    """显示标签：语义配置 tag 优先，其次 DB label（"未知"不计）。"""
    doc = cfg if cfg is not None else load()
    pid = str(place_id or "")
    for r in doc["places"]:
        if (r["place_id"] and r["place_id"] == pid) or (r["poi"] and r["poi"] == (poi or "")):
            if r["tag"]:
                return r["tag"]
            break
    return db_label if db_label and db_label != "未知" else ""


def note_for(place_id: str | None, poi: str, cfg: dict | None = None) -> str:
    """地点背景注记（配置 note），无则空串。"""
    doc = cfg if cfg is not None else load()
    pid = str(place_id or "")
    for r in doc["places"]:
        if (r["place_id"] and r["place_id"] == pid) or (r["poi"] and r["poi"] == (poi or "")):
            return r["note"]
    return ""


def district_note(district: str, cfg: dict | None = None) -> str:
    """区县背景注记（配置 note），无则空串。"""
    doc = cfg if cfg is not None else load()
    d = (district or "").strip()
    if not d:
        return ""
    for r in doc["districts"]:
        if r["district"] == d:
            return r["note"]
    return ""
