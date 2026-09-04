"""网易云听歌历史读取器。

数据链路（2026-07-11 POC 实测）：
   1. `ncm record --json --all` 拉取听歌记录，返回 {code, weekData[], allData[]}
   2. weekData = 本周聚合；allData = 累计全量（每首歌一条，含 playCount/score/song）
   3. 用 allData（累计复听强度更稳），按 song.id 去重后入库

字段审计（实测）：
   - 可用：playCount(累计播放次数)、score、song{id,name,ar[],al,dt时长}
   - 缺失：播放时间戳、风格/标签（ncm 无 album/artist 子命令，私有风格接口需 cookie）

降级策略（设计文档 §4.5）：
   - 音乐风格分布：❌ 降级（无风格字段）
   - 播放时段：❌ 降级（无时间戳）
   - 重复播放模式：✅ 保留（playCount 识别单曲循环 / 高复听）
   - 时长分布：✅ 保留（song.dt，标注为间接代理，非真实收听行为）

健壮性（对齐 bilibili_reader）：
   - `ncm record` 为登录态网络调用，结果缓存到 data/ncm_record_cache.json，
     日常 ingest 读缓存（零实时调用）；仅 `NETEASE_FORCE_REFRESH=1` 时重拉。
   - subprocess 调 ncm 需 env PYTHONUTF8=1 + PYTHONIOENCODING=utf-8；decode 依次试 utf-8/gbk。
   - 退出码非零或 JSON 非 code 200 视为失败，降级为空（不入库）。
   - 无真实播放时间戳，入库 timestamp 用拉取时刻（仅排序用，不影响画像）。
"""

import json
import os
import subprocess
from collections import Counter
from datetime import datetime
from shutil import which

from profile.config import PROJECT_ROOT
from profile.db.store import upsert_raw_data, query_raw_data
from profile.io.base import BaseReader
from profile.log import get_logger, log_error
from profile.models import ChatRecord

logger = get_logger("io.netease_reader")

_DT_BUCKETS = [
    ("短 (<3min)", 0, 180_000),
    ("中 (3-5min)", 180_000, 300_000),
    ("长 (>5min)", 300_000, float("inf")),
]


def _infer_tendency(avg_pc: float, heavy_ratio: float, avg_dt_min: float) -> dict:
    notes = []
    if avg_pc >= 5 or heavy_ratio >= 0.2:
        notes.append("高复听：对特定曲目有强情感依恋/怀旧倾向，偏好反复沉浸而非广撒网")
    else:
        notes.append("复听分散：听歌面广，探索型消费，单曲情感锚定较弱")
    if avg_dt_min >= 4:
        notes.append("偏好中长曲：更能接受完整叙事/器乐铺陈，审美偏沉浸")
    else:
        notes.append("偏好短曲：碎片化收听，审美偏轻量快消费")
    return {
        "结论": notes,
        "说明": "基于 playCount + dt 的间接代理，未做歌词情感分析（风格/时段维度降级）",
    }


_NCM_RECORD_LIMIT = 100000
_NCM_TIMEOUT = "120s"
_CACHE_PATH = PROJECT_ROOT / "data" / "ncm_record_cache.json"


class NeteaseReader(BaseReader):
    @property
    def source_name(self) -> str:
        return "netease"

    @property
    def analysis_type(self) -> str:
        return "topic"

    @property
    def profile_target(self) -> str:
        return "consumption"

    @property
    def consumption_slot(self) -> str:
        return "emotion_aesthetic"

    def analyze_consumption(self) -> dict:
        """情绪审美画像（内容消费画像维度之一），逻辑内联自 analysis.emotion_aesthetic。

        数据来源：本源听歌历史（raw_data.source=self.source_name）。
        """
        rows = query_raw_data(source=self.source_name)
        logger.info("情绪审美分析（netease）", extra={"extra": {"raw_count": len(rows)}})
        if not rows:
            return {"status": "无 netease 数据", "count": 0}

        songs = []
        for r in rows:
            rj = r.get("raw_json") or {}
            song = rj.get("song") or {}
            pc = rj.get("playCount")
            dt = song.get("dt")
            if pc is None or dt is None:
                continue
            songs.append({
                "name": song.get("name", ""),
                "artists": song.get("artists") or [],
                "album": song.get("album"),
                "playCount": pc,
                "dt": dt,
            })

        if not songs:
            return {"status": "无有效听歌记录", "count": 0}

        total = len(songs)
        playcounts = [s["playCount"] for s in songs]
        dts = [s["dt"] for s in songs]
        artist_counter = Counter(a for s in songs for a in s["artists"])

        avg_pc = sum(playcounts) / total
        replay_heavy = sum(1 for pc in playcounts if pc >= 10)
        top_replay = sorted(songs, key=lambda s: s["playCount"], reverse=True)[:10]
        top_replay_list = [
            {"name": s["name"], "artists": s["artists"], "playCount": s["playCount"]}
            for s in top_replay
        ]

        dt_buckets = Counter()
        for dt in dts:
            for label, lo, hi in _DT_BUCKETS:
                if lo <= dt < hi:
                    dt_buckets[label] += 1
                    break
        dt_dist = {label: round(dt_buckets[label] / total * 100, 1) for label, _, _ in _DT_BUCKETS}
        avg_dt_min = round(sum(dts) / total / 1000 / 60, 1)

        tendency = _infer_tendency(avg_pc, replay_heavy / total, avg_dt_min)

        return {
            "status": "ok",
            "样本量": total,
            "复听强度": {
                "平均播放次数": round(avg_pc, 1),
                "高复听占比(>=10次)": round(replay_heavy / total * 100, 1),
            },
            "高复听Top10": top_replay_list,
            "时长分布": dt_dist,
            "平均时长(min)": avg_dt_min,
            "偏好歌手Top10": artist_counter.most_common(10),
            "情绪倾向(间接)": tendency,
        }

    def is_available(self) -> bool:
        return which("ncm") is not None

    def ingest(self) -> int:
        logger.info("开始 netease 数据入库")
        if not self.is_available():
            logger.warning("ncm 命令不可用，跳过", extra={
                "extra": {"hint": "请确认 ncm-cli 已安装并在 PATH 中且已 ncm login"}
            })
            return 0

        data = self._fetch_record(force=self._force_refresh())
        items = self._merge(data)
        logger.info("网易云听歌记录获取完成", extra={"extra": {"record_count": len(items)}})
        if not items:
            logger.warning("听歌记录为空，可能未登录 ncm 或接口异常", extra={"extra": {}})
            return 0

        count = 0
        skipped_empty = 0
        for r in items:
            song = r.get("song") or {}
            sid = song.get("id")
            if not sid:
                skipped_empty += 1
                continue
            artists = [a.get("name") for a in song.get("ar", []) if a.get("name")]
            record = {
                "playCount": r.get("playCount"),
                "score": r.get("score"),
                "song": {
                    "id": sid,
                    "name": song.get("name"),
                    "artists": artists,
                    "album": (song.get("al") or {}).get("name"),
                    "dt": song.get("dt"),
                },
            }
            upsert_raw_data(
                source=self.source_name,
                source_id=str(sid),
                content=song.get("name", ""),
                actions=None,
                outcome="",
                learned=None,
                timestamp=datetime.now().isoformat(timespec="seconds"),
                raw_json=record,
            )
            count += 1

        logger.info("netease 入库完成", extra={
            "extra": {
                "source": "netease",
                "ingested": count,
                "skipped_empty": skipped_empty,
                "record_total": len(items),
            }
        })
        return count

    def read(self) -> list[ChatRecord]:
        records = []
        data = self._fetch_record()
        for r in self._merge(data):
            song = r.get("song") or {}
            name = song.get("name", "")
            if not name:
                continue
            records.append(ChatRecord(time=datetime.now(), content=name, source=self.source_name))
        logger.info("netease 读取完成", extra={"extra": {"records_count": len(records)}})
        return records

    def _force_refresh(self) -> bool:
        return os.environ.get("NETEASE_FORCE_REFRESH") == "1"

    def _merge(self, data: dict | None) -> list[dict]:
        if not data:
            return []
        all_data = data.get("allData") or []
        if all_data:
            return all_data
        return data.get("weekData") or []

    def _fetch_record(self, force: bool = False) -> dict | None:
        if not force and _CACHE_PATH.exists():
            try:
                data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                if data.get("code") == 200 and (data.get("allData") or data.get("weekData")):
                    logger.info("从缓存读取网易云听歌记录", extra={
                        "extra": {"cached_count": len(data.get("allData") or data.get("weekData"))}
                    })
                    return data
            except Exception as e:
                log_error(logger, "读取 ncm 缓存失败，转实时拉取", exc=e, context={})
        data = self._call_ncm()
        if data and data.get("code") == 200:
            try:
                _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                _CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                logger.info("ncm 缓存已写入", extra={"extra": {"cached_count": len(data.get("allData") or data.get("weekData"))}})
            except Exception as e:
                log_error(logger, "写入 ncm 缓存失败", exc=e, context={})
        return data

    def _call_ncm(self) -> dict | None:
        try:
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                ["ncm", "--timeout", _NCM_TIMEOUT, "record", "--json", "--all", "--limit", str(_NCM_RECORD_LIMIT)],
                capture_output=True, env=env, timeout=180,
            )
            if proc.returncode != 0:
                log_error(logger, "ncm record 非零退出码", exc=None, context={
                    "returncode": proc.returncode,
                    "stderr": proc.stderr.decode("utf-8", "replace")[:200],
                })
                return None
            raw = proc.stdout
            text = None
            for enc in ("utf-8", "gbk"):
                try:
                    text = raw.decode(enc)
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            if text is None:
                log_error(logger, "ncm record 输出解码失败", exc=None, context={"bytes": len(raw)})
                return None
            data = json.loads(text)
            if data.get("code") != 200:
                logger.warning("ncm record 返回非 200", extra={
                    "extra": {"code": data.get("code"), "msg": data.get("message") or data.get("msg")}
                })
                return None
            return data
        except subprocess.TimeoutExpired:
            log_error(logger, "ncm record 调用超时", exc=None, context={})
            return None
        except Exception as e:
            log_error(logger, "ncm record 调用异常", exc=e, context={})
            return None
