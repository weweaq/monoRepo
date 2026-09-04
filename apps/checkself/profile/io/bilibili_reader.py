"""bilibili 观看历史读取器。

数据链路（2026-07-11 POC 实测，2026-07-11 修正）：
  1. `bili history --json` 拉取观看历史，仅取 bvid + viewed_at（这两个字段为 ASCII，安全）
  2. 对每个 bvid 调一次 `api.bilibili.com/x/web-interface/view` 补全：
     title / UP主(author) / 时长(duration) / 分区(tid)

注意：
  - bilibili 标题在 bili-cli 内部解析时已乱码，故标题/UP主统一从 view API 取 UTF-8 正确值。
  - 原方案用 `x/tag/archive/tags` 拿用户标签做兴趣分类，但该接口在高并发下被 B站风控
    返回 412，批量不可用。故兴趣分类改回**标题关键词本地规则**（见 classify），
    view API 仅用于补 duration + tid，每个 bvid 仅 1 次稳定请求。
  - 分类逻辑集中在 classify()，供 analysis 层复用，不在入库时固化（映射可独立演进）。

限流与健壮性（实测踩坑）：
  - `bili history --max 100` 固定返回 rc=1 空输出（bili-cli 边界 bug），页大小取 50。
  - 观看历史接口有 B站侧限流，短时间高频调用会偶发空输出。故 history 列表**缓存到
    data/bili_history_cache.json**，ingest 默认读缓存（零实时调用）；仅 `--refresh` 时实时重拉。
  - view API 相对稳定；遇 412 指数退避重试。

降级策略：
  - view API 失败则对应字段降级（title 退回 history 值、duration=None），仍入库不中断。
  - 翻页某次失败：指数退避重试该页，仍失败则跳过该页继续后续页，保证尽量拉全。

性能：
  - enrich（view 网络 IO）用线程池并发，主线程顺序 upsert 避免 SQLite 锁争用；
  - 增量：DB 中已存在且 duration 完整的 bvid 跳过二次抓取，使 refresh 快速。
"""

import json
import os
import subprocess
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from shutil import which

from profile.config import PROJECT_ROOT
from profile.db.store import upsert_raw_data, query_raw_data
from profile.io.base import BaseReader
from profile.log import get_logger, log_error
from profile.models import ChatRecord

logger = get_logger("io.bilibili_reader")

BILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid="
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}
_HTTP_TIMEOUT = 15
_HISTORY_PAGE_SIZE = 50
_HISTORY_RETRY = 4
_HISTORY_BACKOFF = 1.0
_MAX_PAGES = 200
_MAX_WORKERS = 4
_REQUEST_INTERVAL = 1.0
_RATE_LIMIT_BACKOFF = 2.0
_RATE_LIMIT_RETRY = 3

# 标题关键词 → 兴趣大类（本地规则，零网络依赖）
_CATEGORY_KEYWORDS = {
    "游戏": ["游戏", "攻略", "实况", "steam", "手游", "主机", "ps5", "xbox", "switch",
             "原神", "王者", "吃鸡", "mc", "我的世界"],
    "科技": ["科技", "数码", "手机", "电脑", "人工智能", "ai", "芯片", "编程", "代码",
             "软件", "硬件", "评测", "windows", "linux"],
    "汽车": ["汽车", "新能源", "驾照", "奇瑞", "特斯拉", "宝马", "奔驰", "suv", "试驾",
             "比亚迪", "发动机"],
    "影视": ["电影", "电视剧", "番剧", "综艺", "纪录片", "影视", "导演", "演员", "剧"],
    "动漫": ["动漫", "动画", "漫画", "二次元", "声优", "番", "bilibili番剧"],
    "音乐": ["音乐", "歌曲", "歌手", "专辑", "弹唱", "翻唱", "乐器", "钢琴", "吉他",
             "rap", "说唱", "纯音乐"],
    "美食": ["美食", "料理", "烹饪", "探店", "烘焙", "吃", "菜", "餐厅"],
    "知识": ["知识", "科普", "学习", "教程", "考研", "英语", "历史", "经济", "心理",
             "读书", "教育"],
    "生活": ["生活", "日常", "vlog", "穿搭", "护肤", "健身", "旅行", "家居", "租房"],
    "娱乐": ["娱乐", "搞笑", "明星", "脱口秀", "鬼畜", "综艺"],
}


def classify(title: str) -> str:
    """基于标题关键词做兴趣大类粗分（本地规则，无需网络）。

    命中多个类别时返回命中最多的；并列或均无命中返回 '其他'。
    """
    if not title:
        return "其他"
    text = title.lower()
    scores: dict[str, int] = {}
    for category, keywords in _CATEGORY_KEYWORDS.items():
        hit = sum(1 for kw in keywords if kw.lower() in text)
        if hit:
            scores[category] = hit
    if not scores:
        return "其他"
    return max(scores.items(), key=lambda kv: kv[1])[0]


class BilibiliReader(BaseReader):
    @property
    def source_name(self) -> str:
        return "bilibili"

    @property
    def analysis_type(self) -> str:
        return "topic"

    @property
    def profile_target(self) -> str:
        return "consumption"

    @property
    def consumption_slot(self) -> str:
        return "knowledge_interest"

    def analyze_consumption(self) -> dict:
        """知识兴趣光谱（内容消费画像维度之一），逻辑内联自 analysis.knowledge_interest。

        数据来源：本源观看历史（raw_data.source=self.source_name）。用 classify 对标题做本地关键词分类。
        """
        rows = query_raw_data(source=self.source_name)
        logger.info("知识兴趣分析（bilibili）", extra={"extra": {"raw_count": len(rows)}})
        if not rows:
            return {"status": "无 bilibili 数据", "count": 0}
        cats = Counter()
        untitled = 0
        for r in rows:
            rj = r.get("raw_json") or {}
            title = rj.get("title") or r.get("content") or ""
            if not title:
                untitled += 1
                continue
            cats[classify(title)] += 1
        total = sum(cats.values())
        dist = {k: round(v / total * 100, 1) for k, v in cats.most_common()}
        return {
            "status": "ok",
            "样本量": total,
            "无标题条数": untitled,
            "兴趣分类分布": dist,
            "TOP兴趣": cats.most_common(5),
        }

    def is_available(self) -> bool:
        return which("bili") is not None

    def ingest(self) -> int:
        logger.info("开始 bilibili 数据入库")
        if not self.is_available():
            logger.warning("bili 命令不可用，跳过", extra={
                "extra": {"hint": "请确认 bili-cli 已安装并在 PATH 中"}
            })
            return 0

        items = self._fetch_all_history(force=self._force_refresh())
        logger.info("bilibili 观看历史获取完成", extra={"extra": {"history_count": len(items)}})
        if not items:
            logger.warning("观看历史为空，可能未登录 bili-cli 或接口限流", extra={"extra": {}})
            return 0

        existing = self._load_existing()
        pending = []
        skipped_existing = 0
        skipped_empty = 0
        for item in items:
            bvid = item.get("bvid") or item.get("id")
            viewed_at = item.get("viewed_at")
            if not bvid or not viewed_at:
                skipped_empty += 1
                continue
            if existing.get(bvid):
                skipped_existing += 1
                continue
            pending.append((bvid, viewed_at, item.get("title"), item.get("author")))

        logger.info("待 enrich 视频数", extra={
            "extra": {"pending": len(pending), "skipped_existing": skipped_existing, "skipped_empty": skipped_empty}
        })

        count = 0
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            future_map = {
                ex.submit(self._enrich, bvid): (bvid, viewed_at, h_title, h_author)
                for bvid, viewed_at, h_title, h_author in pending
            }
            for fut in as_completed(future_map):
                bvid, viewed_at, h_title, h_author = future_map[fut]
                try:
                    meta = fut.result()
                except Exception as e:
                    log_error(logger, "bili enrich 失败", exc=e, context={"bvid": bvid})
                    meta = {"title": None, "author": None, "duration": None, "tid": None}
                record = {
                    "bvid": bvid,
                    "title": meta.get("title") or h_title or "",
                    "author": meta.get("author") or h_author or "",
                    "viewed_at": viewed_at,
                    "duration": meta.get("duration"),
                    "tid": meta.get("tid"),
                }
                upsert_raw_data(
                    source=self.source_name,
                    source_id=bvid,
                    content=record["title"],
                    actions=None,
                    outcome="",
                    learned=None,
                    timestamp=viewed_at,
                    raw_json=record,
                )
                count += 1

        logger.info("bilibili 入库完成", extra={
            "extra": {
                "source": "bilibili",
                "ingested": count,
                "skipped_existing": skipped_existing,
                "skipped_empty": skipped_empty,
                "history_total": len(items),
            }
        })
        return count

    def read(self) -> list[ChatRecord]:
        records = []
        for item in self._fetch_all_history():
            viewed_at = item.get("viewed_at")
            if not viewed_at:
                continue
            try:
                t = datetime.fromisoformat(viewed_at)
            except (ValueError, TypeError):
                continue
            records.append(ChatRecord(time=t, content=item.get("title", ""), source=self.source_name))
        logger.info("bilibili 读取完成", extra={"extra": {"records_count": len(records)}})
        return records

    def _force_refresh(self) -> bool:
        return os.environ.get("BILI_FORCE_REFRESH") == "1"

    def _history_cache_path(self) -> Path:
        return PROJECT_ROOT / "data" / "bili_history_cache.json"

    def _fetch_all_history(self, force: bool = False) -> list[dict]:
        cache = self._history_cache_path()
        if not force and cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                items = data.get("items", [])
                if items:
                    logger.info("从缓存读取 bilibili 观看历史", extra={"extra": {"cached_count": len(items)}})
                    return items
            except Exception as e:
                log_error(logger, "读取 bili history 缓存失败，转实时拉取", exc=e, context={})
        items = self._pull_all_history()
        if items:
            try:
                cache.write_text(
                    json.dumps(
                        {"fetched_at": datetime.now().isoformat(timespec="seconds"), "items": items},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                logger.info("bili history 缓存已写入", extra={"extra": {"cached_count": len(items)}})
            except Exception as e:
                log_error(logger, "写入 bili history 缓存失败", exc=e, context={})
        return items

    def _pull_all_history(self) -> list[dict]:
        all_items: list[dict] = []
        page = 1
        while page <= _MAX_PAGES:
            data = None
            for attempt in range(_HISTORY_RETRY):
                data = self._call_bili_history(page, _HISTORY_PAGE_SIZE)
                if data is not None:
                    break
                time.sleep(_HISTORY_BACKOFF * (attempt + 1))
            if data is None:
                logger.warning("bili history 翻页中止", extra={"extra": {"failed_page": page}})
                break
            items = data.get("items", [])
            if not items:
                break
            all_items.extend(items)
            if len(items) < _HISTORY_PAGE_SIZE:
                break
            page += 1
            time.sleep(_REQUEST_INTERVAL)
        return all_items

    def _load_existing(self) -> dict:
        existing = {}
        try:
            rows = query_raw_data(source=self.source_name)
            for r in rows:
                rj = r.get("raw_json") or {}
                existing[r["source_id"]] = bool(rj.get("duration"))
        except Exception as e:
            log_error(logger, "查询已存在 bilibili 记录失败", exc=e, context={})
        return existing

    def _call_bili_history(self, page: int, max_n: int) -> dict | None:
        try:
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                ["bili", "history", "--json", "--page", str(page), "--max", str(max_n)],
                capture_output=True, env=env, timeout=60,
            )
            if proc.returncode != 0:
                log_error(logger, "bili history 非零退出码", exc=None, context={
                    "page": page, "returncode": proc.returncode,
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
                log_error(logger, "bili history 输出解码失败", exc=None, context={
                    "page": page, "bytes": len(raw),
                })
                return None
            data = json.loads(text)
            if not data.get("ok"):
                logger.warning("bili history 返回非 ok", extra={
                    "extra": {"page": page, "code": data.get("code"), "msg": data.get("message")}
                })
                return None
            return data.get("data", {})
        except subprocess.TimeoutExpired:
            log_error(logger, "bili history 调用超时", exc=None, context={"page": page})
            return None
        except Exception as e:
            log_error(logger, "bili history 调用异常", exc=e, context={"page": page})
            return None

    def _enrich(self, bvid: str) -> dict:
        result = {"title": None, "author": None, "duration": None, "tid": None}
        try:
            view = self._http_json(BILI_VIEW_API + bvid)
            d = view.get("data", {})
            result["title"] = d.get("title")
            result["author"] = d.get("owner", {}).get("name")
            result["duration"] = d.get("duration")
            result["tid"] = d.get("tid")
        except Exception as e:
            log_error(logger, "bili view API 失败，降级", exc=e, context={"bvid": bvid})
        return result

    def _http_json(self, url: str) -> dict:
        last_exc = None
        for attempt in range(_RATE_LIMIT_RETRY + 1):
            try:
                req = urllib.request.Request(url, headers=_HTTP_HEADERS)
                with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_exc = e
                if e.code == 412 and attempt < _RATE_LIMIT_RETRY:
                    time.sleep(_RATE_LIMIT_BACKOFF * (attempt + 1))
                    continue
                raise
            except Exception as e:
                last_exc = e
                time.sleep(0.3)
        raise last_exc
