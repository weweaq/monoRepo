"""当日信息包（Daily Info Pack）：注册表驱动的确定性预取 + 裁剪 + 逐源降级 + 双文件体检落盘。

对齐 docs/daily-report-redesign.md C1（v0.7）/ C2（A′）/ C3 的落地实现：

- **SourceSpec 注册表**：每个信息源的 key/cap/priority/builder 收拢在 ``SOURCES`` 一处，
  新增一个源 = 加一条 SourceSpec，预算熔断/状态元信息/体检落盘全部自动生效。
- **builder 契约三元组** ``(title, pack_body, detail_body)``：
  * pack_body = 挑选/压缩后渲染进信息包的块文本（现行为）；
  * detail_body = 该 builder 当日取数**全部结果**的可读渲染（不做 top-N 挑选、不做条数压缩），
    供 ``data/logs/pack_detail/{date}/{key}.md`` 三节详情（完整取数详情/渲染文本/实际进包）；
  * detail 只到 builder 自己的查询窗口为止，不含 L3 原始数据。
- **classify_body**：把"该源失败/该日无数据"的文案约定收敛为机器可读状态（ok/empty/failed/missing_data），
  驱动 header 状态元信息（C3）与体检 jsonl（C1）。
- **_cap_lines 行级截断**：按行累积、行末截停，不断章；``_assemble_blocks`` 超预算整块丢弃。
- **逐源 try/except 兜底**：单源失败只在本节标注"该源失败/无今日数据"，绝不中断整包、不导致 run_job 失败。
- **双文件落盘（全部 best-effort）**：``info_pack_health.jsonl``（每日一行元数据）+
  ``pack_detail/{date}/{key}.md``（三节详情，保留 90 天）；事实卡支线 ``_FACT_CARD.md`` 由
  context.build_system_prompt 经 ``write_fact_card_detail`` 覆盖写。
- langTrack 手机事实源（v3.3，C2 A′ 局部修订）：_PHONE_PLACE/_PHONE_USAGE/_PHONE_NOTIF
  进包（当日全天视角）；fact_card compact（"当前时刻"卡）仍是 chat system prompt 的唯一
  注入出口，睡眠/时段×应用语义字段仍下沉在 fact_card section。

模块级 ``_*_FN`` 名字是对外接入点：测试通过 monkeypatch 这些名字注入 fake 取数（空/满双向），
不触碰真实 CLI / 数据库。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("daily_info_pack")

_CN_TZ: Final = _dt.timezone(_dt.timedelta(hours=8))  # 东八区：日志 ts / 保留期判定统一口径

# --------------------------------------------------------------------------- #
# 预算与各源硬上限（字符数，含中文）。整包 8000 字上限，单源超限按行截断。      #
# 2026-09-04 扩容：2000 → 8000（8 路被动信号压得太狠，日期/标题/画像被截掉）； #
# 同时新增第 9 路「当日 QQ 对话摘录」（用户第一人称信号，详见 _build_chat）。   #
# --------------------------------------------------------------------------- #
PACK_BUDGET: int = 8000  # 整包硬上限
_HEAD_CAP: int = 400      # 消费指令块
_LONG_TERM_CAP: int = 1600  # 长期画像 compact（40 行内）
_CHAT_CAP: int = 1200     # 当日 QQ 对话摘录（用户侧）
_BILI_CAP: int = 1200     # B站当日 top20
_EDGE_CAP: int = 900      # Edge 域名归并 top10
_GIT_CAP: int = 1200      # git 当日提交（mono + 附加仓库，按仓库分组）
_FILES_CAP: int = 1000    # 当日文件活动 top15
_NCM_CAP: int = 700       # ncm 歌单/收藏静态基线
_MEDIA_CAP: int = 900     # 当日听歌与视频伴音（music_play 分两类）
_MEMORY_CAP: int = 900    # 前日日报摘要（可选）
_PHONE_PLACE_CAP: int = 800  # 手机·位置轨迹（见文件尾手机事实源区）
_PHONE_USAGE_CAP: int = 600  # 手机·使用统计
_PHONE_NOTIF_CAP: int = 800  # 手机·通知摘要

_LONG_TERM_LINES: int = 40  # 画像 compact 行数上限（对齐 _summarize_long_term 默认）
_BILI_TOP: int = 20         # B站当日观看 top N
_BILI_FETCH_LIMIT: int = 100  # _BILI 单次拉取条数默认值（可被 config `fetch_limit` 覆盖，见 _FETCH_LIMIT_SPECS）
_EDGE_TOP: int = 10         # Edge 域名归并 top N
_FILES_TOP: int = 15        # 文件活动目录聚合 top N
_NCM_TOP: int = 10          # ncm 歌单 top N
_CHAT_TOP: int = 15         # 当日对话摘录条数上限
_MEMORY_SNIPPET_CHARS: int = 260    # 前日日报进包摘要（现行为）
_MEMORY_DETAIL_CHARS: int = 1000    # 前日日报详情摘要（C1 v0.7：取更长摘要）

_PACK_DETAIL_RETENTION_DAYS: Final = 90  # pack_detail 详情目录保留期（重日 100-300KB，90 天约 30MB）

# 逐源预算配置（v3.2.2）：config/info_pack.json 覆盖代码默认（cap/priority/enabled/pack_budget）。
# 每次构建现读现用——评审页 /config 改完下一次构建即生效，无需重启；文件缺失/损坏 → 全走代码默认。
# 采样方向（取最近/聚合 topN）是每源固有策略，不属于配置面（防把头部截断类 bug 做成可配项）。
_SOURCE_CONFIG_RELPATH: Final = Path("config") / "info_pack.json"
_CAP_MIN, _CAP_MAX = 200, 8000
_BUDGET_MIN, _BUDGET_MAX = 3000, 20000


def load_source_config(cfg: Config) -> dict[str, Any]:
    """读逐源预算覆盖配置；返回原始 dict（无文件/坏文件/非 dict → 空覆盖）。"""
    path = cfg.root / _SOURCE_CONFIG_RELPATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("source config unreadable, using code defaults", error=str(exc))
        return {}
    return data if isinstance(data, dict) else {}


def _clamped_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def effective_source_caps(cfg: Config) -> dict[str, int]:
    """各源生效 cap（config 覆盖 + 边界钳制）；供 builder 内部单一截断取用。"""
    ov = (load_source_config(cfg).get("sources") or {})
    return {
        spec.key: _clamped_int(
            (ov.get(spec.key) or {}).get("cap"), spec.cap, _CAP_MIN, _CAP_MAX
        )
        for spec in SOURCES
    }


# 逐源"单次拉取条数"旋钮（v3.2.4）：key → (默认, min, max)。
# 窗口大小可配、方向仍固化为取最近（采样方向不开放配置，防头部截断回归）；
# max 受工具硬上限约束（bili_history._MAX_LIMIT=100）；不在表内的源无此旋钮。
_FETCH_LIMIT_SPECS: dict[str, tuple[int, int, int]] = {
    "_BILI": (_BILI_FETCH_LIMIT, 10, 100),
    "_EDGE": (100, 10, 100),
}


def effective_fetch_limit(cfg: Config, key: str) -> int:
    """该源生效的单次拉取条数（config `sources[key].fetch_limit` 覆盖 + 钳制）。"""
    default, lo, hi = _FETCH_LIMIT_SPECS[key]
    ov = (load_source_config(cfg).get("sources") or {})
    return _clamped_int((ov.get(key) or {}).get("fetch_limit"), default, lo, hi)

# os.walk 时跳过的目录（仓库内部噪音 / 依赖 / 构建缓存 / 运行日志）
_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".venv", ".idea", "__pycache__", ".ruff_cache", ".pytest_cache",
        ".tmpp", "node_modules", "temp", "logs", "botpy",
    }
)

# --------------------------------------------------------------------------- #
# tools 的 .func 引用（模块级名字，测试可 monkeypatch）                        #
# --------------------------------------------------------------------------- #
from gacore.tools.bili_history import bili_history as _bili_tool
from gacore.tools.browser_history import browser_history as _browser_tool
from gacore.tools.ncm_tools import ncm_me as _ncm_me_tool
from gacore.tools.ncm_tools import ncm_playlist_list as _ncm_playlist_tool

_BILLI_FN = _bili_tool.func
_BROWSER_FN = _browser_tool.func
_NCM_ME_FN = _ncm_me_tool.func
_NCM_PLAYLIST_FN = _ncm_playlist_tool.func


# --------------------------------------------------------------------------- #
# 消费指令模板                                                                #
# --------------------------------------------------------------------------- #
def _instruction_head(date: str) -> str:
    return (
        f"〔当日信息包·{date}〕以下内容由系统在 {date} 当日预取生成，仅供本次日报写作使用。\n"
        f"- 时间戳语义：包内所有“观看时间 / 提交时间 / 文件时间 / 手机信号”均为 {date} 当天的数据时间戳，"
        "并非当前真实时刻——可直接引用为当日事实，不要因时间“不在现在”而把它当成旧事忽略。\n"
        "- 写作素材：信息包是今日写作的素材库——请把关键点引用进日报正文（用于加深人物刻画），"
        "但不要整段复制原文。\n"
        "- 消费覆盖（C7）：每个标注“状态:全量”的信息源至少被正文消费一次；"
        "确无可用信息的源，在 daily note 归档节点名跳过原因（不进邮件正文）。\n"
        "- 降级说明：单个信息源失败会标注“该源失败/无今日数据”，属正常降级，不影响整体写作；"
        "近 2 日 daily notes 摘要已随系统提示注入，此处不重复。当日生活事实卡（fact_card）"
        "注入的是“当前时刻”视角；当日的全天轨迹/使用统计/通知以 _PHONE_* 源为准，两者不重复。\n"
    )


# --------------------------------------------------------------------------- #
# 信息源 builders：契约三元组 (title, pack_body, detail_body)                  #
# 每个 builder 自带 try/except；失败时 pack_body 以 "- 该源失败：" 开头（classify_body 约定） #
# --------------------------------------------------------------------------- #
def _build_long_term_picture(date: str, cfg: Config) -> tuple[str, str, str]:
    """长期画像：pack=固定锚+编年史尾部窗口（单一截断，≤_LONG_TERM_CAP），detail=锚+画像全文。

    v3.2 采样修正（2026-10-04）：此前 head 截断两层叠加（前 40 行→再砍 1600 字符），
    实测日报 LLM 每天只看到编年史开头 ~08-02~08-27，最近数周新增（含对旧事实的修订）全部
    丢失。现改为：锚点文件全量保留 + 编年史尾部窗口，builder 内一次性截到源预算。
    """
    from gacore.scheduler import _compact_long_term, _long_term_anchor, _long_term_insight  # 延迟 import 避循环

    try:
        text = _long_term_insight(cfg)
        anchor = _long_term_anchor(cfg)
        if not text and not anchor:
            return (
                "〔长期画像·compact〕",
                "- 无长期画像（memory/global_mem_anchor.txt 与 global_mem_insight.txt 均缺失），本日仅凭当日信号写作。",
                "",
            )
        compact = _compact_long_term(
            anchor, text, max_chars=effective_source_caps(cfg).get("_LONG_TERM", _LONG_TERM_CAP),
            limit_lines=_LONG_TERM_LINES,
        )
        detail_parts: list[str] = []
        if anchor:
            detail_parts.append(f"〔固定锚〕\n{anchor}")
        if text:
            detail_parts.append(f"〔编年史〕\n{text}")
        return "〔长期画像·compact〕", compact, "\n\n".join(detail_parts)
    except Exception as exc:  # noqa: BLE001 - 单源降级
        logger.warning("daily_info_pack: long-term picture failed", error_type=type(exc).__name__, error=str(exc))
        return "〔长期画像·compact〕", f"- 该源失败：{exc}", ""


def _build_chat(date: str, cfg: Config) -> tuple[str, str, str]:
    """当日 QQ 对话摘录（用户侧原话）：画像链路里唯一的第一人称信号源。

    数据来源 memory/qq_chat_log.jsonl（qq.py 收发消息时逐行 append，schema 见
    _persist_chat_log）。只取 direction=user 且非 "/" 命令的行，按时间正序列。
    pack=最近 _CHAT_TOP 条（每条截 90 字；日志按时间追加，取尾部=保留最新状态，
    v3.2 修正：此前取"前 15 条"会丢掉晚间消息）；detail=当日全部摘录（不截条数不截字数）。
    """
    title = "〔对话·当日 QQ 摘录〕"
    path = cfg.memory_dir / "qq_chat_log.jsonl"
    if not path.is_file():
        return title, "- 当日无 QQ 对话记录（qq_chat_log.jsonl 不存在）", ""
    day_lines: list[str] = []
    detail_lines: list[str] = []
    total = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # 坏行跳过，不中断整源
                if not isinstance(entry, dict):
                    continue
                ts = str(entry.get("ts") or "")
                if ts[:10] != date:
                    continue
                if str(entry.get("direction") or "") != "user":
                    continue
                text = str(entry.get("text") or "").strip()
                if not text or text.startswith("/"):
                    continue
                total += 1
                hhmm = ts[11:16] if len(ts) >= 16 else ""
                detail_lines.append(f"- {hhmm} {text}")
                day_lines.append(f"- {hhmm} {text[:90]}")
    except OSError as exc:
        return title, f"- 该源失败：{exc}", ""
    if not day_lines:
        if total:
            return title, f"- 当日 QQ 对话仅 {total} 条命令类消息，无正文摘录", ""
        return title, "- 当日无 QQ 对话记录", ""
    pack_lines = day_lines[-_CHAT_TOP:]
    body = "\n".join(pack_lines)
    if total > _CHAT_TOP:
        body += f"\n（当日共 {total} 条用户消息，仅列最近 {len(pack_lines)}）"
    return title, body, "\n".join(detail_lines)


def _build_media(date: str, cfg: Config) -> tuple[str, str, str]:
    """当日听歌与视频伴音：music_play 按 pkg 分流（音乐类=听歌，B站/短视频=视频伴音）。

    pack=top5；detail=全部榜单条目（不截条数）。复用 report._listen_music 的粗档口径，读 langTrack.db。
    """
    title = "〔今日·听歌与视频伴音〕"
    try:
        import sqlite3

        from gacore.langTrack.report import _listen_music

        db = cfg.root / "data" / "langTrack.db"
        if not db.exists():
            return title, "- 无 langTrack 音乐数据", ""
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row  # _listen_music 按列名读 r["payload"]
        try:
            data = _listen_music(conn, date)
        finally:
            conn.close()
        lines: list[str] = []

        def _fmt(e: dict) -> str:
            s = f"《{e['title']}》"
            if e.get("singer"):
                s += f"-{e['singer']}"
            if (e.get("count") or 0) > 1:
                s += f"×{e['count']}"
            return s

        music = data.get("ranking") or []
        if music:
            lines.append("- 听歌 Top：" + "、".join(_fmt(e) for e in music[:5]))
            if data.get("singer_top"):
                lines.append("  常听歌手：" + "、".join(f"{s}×{c}" for s, c in data["singer_top"]))
            if data.get("sessions"):
                lines.append("  连播段：" + "；".join(
                    f"{s['start']}-{s['end']}({s['song_count']}首)" for s in data["sessions"]))
            if data.get("hour_hist"):
                lines.append("  听歌时段：" + "、".join(f"{h}({n})" for h, n in data["hour_hist"]))
        else:
            lines.append("- 今日无听歌记录（music_play 无音乐类事件）")
        video = data.get("video_ranking") or []
        if video:
            lines.append("- 视频伴音（B站/短视频）Top：" + "、".join(_fmt(e) for e in video[:5])
                         + f"（共 {data.get('video_count', 0)} 个）")
        else:
            lines.append("- 今日无视频伴音记录")
        # detail：全部榜单条目，不做 top-N 挑选
        detail_lines: list[str] = []
        if music:
            detail_lines.append(f"- 听歌全量（{len(music)} 首）：" + "、".join(_fmt(e) for e in music))
        if data.get("singer_top"):
            detail_lines.append("  常听歌手：" + "、".join(f"{s}×{c}" for s, c in data["singer_top"]))
        if data.get("sessions"):
            detail_lines.append("  连播段：" + "；".join(
                f"{s['start']}-{s['end']}({s['song_count']}首)" for s in data["sessions"]))
        if data.get("hour_hist"):
            detail_lines.append("  听歌时段：" + "、".join(f"{h}({n})" for h, n in data["hour_hist"]))
        if video:
            detail_lines.append(f"- 视频伴音全量（{len(video)} 个）：" + "、".join(_fmt(e) for e in video))
        return title, "\n".join(lines), "\n".join(detail_lines)
    except Exception as exc:  # noqa: BLE001 - 最后防线：源失败不中断整包
        logger.warning("daily_info_pack: media failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败：{exc}", ""


def _build_bili(date: str, cfg: Config) -> tuple[str, str, str]:
    """B站当日观看：pack=top20，detail=当日每一笔观看（include_duration=False，避免逐条拉时长 CLI 慢调用）。"""
    title = "〔浏览·B站观看 top〕"
    try:
        fetch_n = effective_fetch_limit(cfg, "_BILI")
        res = _BILLI_FN(limit=fetch_n, page=1, include_duration=False)
        if not isinstance(res, dict):
            return title, "- 该源失败：返回格式异常", ""
        if "error" in res:
            msg = res.get("message") or res.get("error")
            return title, f"- 该源失败或未登录：{msg}", ""
        entries = res.get("entries") or []
        today_entries = [e for e in entries if str(e.get("viewed_at", ""))[:10] == date]
        if not today_entries:
            return title, "- 今日无 B 站观看记录", ""

        def _line(e: dict) -> str:
            t = str(e.get("title") or "(无标题)")
            author = str(e.get("author") or "")
            ts = str(e.get("viewed_at") or "")[5:16].replace("T", " ")
            return f"- {ts} {t}｜UP:{author}"

        detail_body = "\n".join(_line(e) for e in today_entries)
        if len(entries) >= fetch_n:
            detail_body += f"\n（工具单次拉取上限 {fetch_n} 条，当日更早观看可能未覆盖）"
        lines = [_line(e) for e in today_entries[:_BILI_TOP]]
        body = "\n".join(lines)
        if len(today_entries) > _BILI_TOP:
            body += f"\n（当日共 {len(today_entries)} 条，仅列前 {min(_BILI_TOP, len(today_entries))}）"
        return title, body, detail_body
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: bili failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败：{exc}", ""


def _build_edge(date: str, cfg: Config) -> tuple[str, str, str]:
    """Edge 浏览器历史：pack=域名归并 top10，detail=当日全部页面逐条。"""
    title = "〔浏览·Edge 域名〕"
    try:
        fetch_n = effective_fetch_limit(cfg, "_EDGE")
        res = _BROWSER_FN(browser="edge", days=1, limit=fetch_n)
        if not isinstance(res, dict):
            return title, "- 该源失败：返回格式异常", ""
        if "error" in res:
            if res.get("error") == "db_not_found":
                return title, "- 该源失败/不可用：Edge 历史库不存在（今日无浏览器历史可用）", ""
            return title, f"- 该源失败：{res.get('message') or res.get('error')}", ""
        entries = res.get("entries") or []
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            host = urlparse(str(entry.get("url") or "")).netloc or "(未知)"
            groups.setdefault(host, []).append(entry)
        if not groups:
            return title, "- 当日无 Edge 浏览记录", ""
        top = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:_EDGE_TOP]
        lines = []
        for host, items in top:
            t = ""
            for it in items:
                cand = str(it.get("title") or "").strip()
                if cand:
                    t = cand[:36]
                    break
            lines.append(f"- {host}：{len(items)} 次{f'（{t}）' if t else ''}")
        # detail：全部页面逐条（host + 标题 + url），不做归并挑选
        detail_lines = []
        for entry in entries:
            host = urlparse(str(entry.get("url") or "")).netloc or "(未知)"
            t = str(entry.get("title") or "").strip() or "(无标题)"
            detail_lines.append(f"- {host} {t}｜{entry.get('url')}")
        detail_body = "\n".join(detail_lines)
        if len(entries) >= fetch_n:
            detail_body += f"\n（工具单次拉取上限 {fetch_n} 条，当日更早记录可能未覆盖）"
        return title, "\n".join(lines), detail_body
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: edge failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败：{exc}", ""


def _run_git(root: Path, date: str) -> tuple[int, str]:
    """直接 subprocess 跑 git log（避开 code_run_header preamble）。返回 (returncode, stdout)。"""
    args = [
        "git", "-C", str(root), "log",
        f"--since={date} 00:00", f"--until={date} 23:59:59",
        "--pretty=format:%h|%s|%an", "--no-merges", "--date-order",
    ]
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return proc.returncode, proc.stdout


def _repo_label(root: Path) -> str:
    """Return the git repository root's directory name (first ancestor containing .git)."""
    for p in (root, *root.parents):
        if (p / ".git").exists():
            return p.name
    return root.name


def _build_git(date: str, cfg: Config) -> tuple[str, str, str]:
    """当日 git 提交，按仓库分组。pack=hash 截 8 位 + subject 截 60 字；detail=完整 log 行。

    主开发仓库 = cfg.root（mono），附加仓库来自 cfg.extra_git_repos（如 weiCheckApp）。
    """
    repos: list[Path] = [cfg.root, *cfg.extra_git_repos]
    pack_sections: list[str] = []
    detail_sections: list[str] = []
    for root in repos:
        rc, stdout = _run_git(root, date)
        if rc != 0:
            pack_sections.append(f"- {_repo_label(root)}：git log 读取失败（非 git 仓库？）")
            continue
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            continue
        shown = []
        for ln in lines:
            parts = ln.split("|")
            if len(parts) >= 2:
                short_hash = parts[0][:8]
                subject = parts[1][:60]
                author = parts[2] if len(parts) > 2 else ""
                shown.append(f"- {short_hash} {subject}（{author}）")
        if shown:
            label = _repo_label(root)
            pack_sections.append(f"【{label}】\n" + "\n".join(shown))
            detail_sections.append(f"【{label}】\n" + "\n".join(lines))
    if not pack_sections:
        return "〔工作·当日 git 提交〕", "- 今日无 git 提交", ""
    return "〔工作·当日 git 提交〕", "\n\n".join(pack_sections), "\n\n".join(detail_sections)


def _build_files(date: str, cfg: Config) -> tuple[str, str, str]:
    """当日文件活动：扫描仓库（跳过依赖/缓存/日志目录）按目录聚合。pack=top15，detail=全量目录。"""
    title = "〔工作·当日文件活动〕"
    try:
        day_start = _dt.datetime.strptime(date, "%Y-%m-%d").astimezone().timestamp()
    except ValueError:
        day_start = 0.0
    counts: Counter[str] = Counter()
    samples: dict[str, str] = {}
    try:
        for dirpath, dirnames, filenames in os.walk(cfg.root):
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                if mtime < day_start:
                    continue
                try:
                    rel_dir = fp.parent.relative_to(cfg.root)
                except ValueError:
                    rel_dir = Path(".")
                key = str(rel_dir) if str(rel_dir) != "." else "(根目录)"
                counts[key] += 1
                samples.setdefault(key, fn)
    except Exception as exc:  # noqa: BLE001
        return title, f"- 该源失败：{exc}", ""
    if not counts:
        return title, "- 今日仓库内无文件改动", ""
    lines = []
    for key, n in counts.most_common(_FILES_TOP):
        sample = samples.get(key, "")
        lines.append(f"- {key}：{n} 个文件{f'（样例 {sample}）' if sample else ''}")
    detail_lines = []
    for key, n in counts.most_common():
        sample = samples.get(key, "")
        detail_lines.append(f"- {key}：{n} 个文件{f'（样例 {sample}）' if sample else ''}")
    return title, "\n".join(lines), "\n".join(detail_lines)


def _build_ncm(date: str, cfg: Config) -> tuple[str, str, str]:
    """ncm 歌单/收藏静态基线（兴趣基线，非当日动态）。pack=top10，detail=拉到的全部歌单。"""
    title = "〔基线·网易云歌单/收藏〕"
    try:
        me = _NCM_ME_FN()
        nickname = ""
        if isinstance(me, dict) and not me.get("error"):
            nickname = f"（{me.get('nickname') or ''}）" if me.get("nickname") else ""
        pl = _NCM_PLAYLIST_FN(limit=50, offset=0)
        if not isinstance(pl, dict):
            return title, "- 该源失败：返回格式异常", ""
        if "error" in pl:
            msg = pl.get("message") or pl.get("error")
            return title, f"- 该源失败/未登录：{msg}", ""
        playlists = pl.get("playlists") or []
        if not playlists:
            return title, "- 该账号无可列歌单", ""

        def _line(p: dict) -> str:
            name = str(p.get("name") or "未命名")
            count = p.get("track_count")
            tag = "·收藏" if p.get("subscribed") else "·自建"
            return f"- {name}{tag}" + (f"（{count} 首）" if count else "")

        head = f"{title}{nickname}"
        return head, "\n".join(_line(p) for p in playlists[:_NCM_TOP]), "\n".join(_line(p) for p in playlists)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: ncm failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败/跳过：{exc}", ""


def _latest_prev_report(cfg: Config) -> tuple[str, str] | None:
    """读 logs/scheduled/ 下最新一份 daily-report 输出（昨日/最近一次），返回 (时间戳, Reply 全文)。"""
    scheduled_dir = cfg.logs_dir / "scheduled"
    if not scheduled_dir.is_dir():
        return None
    files = sorted(
        (p for p in scheduled_dir.glob("daily-report_*.md")),
        key=lambda p: (p.stat().st_mtime if hasattr(p.stat(), "st_mtime") else 0, p.name),
        reverse=True,
    )
    if not files:
        return None
    try:
        text = files[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ts = ""
    for line in text.splitlines():
        if line.startswith("- time:"):
            ts = line.split("time:", 1)[1].strip()
            break
    # 取 ## Reply 之后的正文；在下一个 "## " 小节边界截断，
    # 兼容 2026-09-04 起输出文件里 Reply 之后还有 "(raw model output)" 小节的格式
    body = ""
    if "## Reply" in text:
        body = text.split("## Reply", 1)[1]
        body = body.split("\n## ", 1)[0].strip()
    else:
        body = text.strip()
    if not body:
        return None
    return (ts, body)


def _build_memory(date: str, cfg: Config) -> tuple[str, str, str]:
    """记忆信号：前日日报摘要（可选，零成本）。pack=260 字摘要（现行为），detail=1000 字更长摘要。"""
    title = "〔记忆·前日日报〕"
    try:
        prev = _latest_prev_report(cfg)
        if prev is None:
            return title, "- 无历史日报输出可作基准（首次运行）", ""
        ts, body = prev
        ts_note = f"（{ts}）" if ts else ""
        snippet = body[:_MEMORY_SNIPPET_CHARS].strip()
        detail = body[:_MEMORY_DETAIL_CHARS].strip()
        pack_body = f"- 前一次日报生成于 {ts_note}，其正文起点如下，供本日“微变化”对照：\n  “{snippet}”"
        return title, pack_body, detail
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: prev report failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败：{exc}", ""


# --------------------------------------------------------------------------- #
# 手机事实源（v3.3）：复用 fact_card.build 纯读卡（地名解析/裁剪/降级全复用）， #
# 通知摘要另查 events（fact_card 不读通知内容）。与 fact_card 的分工：fact_card #
# 是"当前时刻"卡（chat system prompt 注入），本组源是"当日全天"事实（日报进包）。#
# --------------------------------------------------------------------------- #
_PHONE_PLACE_CAP: int = 800   # 当日位置轨迹（stays/trips 时间线+停留累计+异常）
_PHONE_USAGE_CAP: int = 600   # 当日手机使用统计（屏幕/App Top/解锁/通知计数）
_PHONE_NOTIF_CAP: int = 800   # 当日通知摘要（点击过的消息，最多 8 条）
_PHONE_NOTIF_TOP: int = 8     # 进包点击消息条数上限（取最近，军规①尾窗）
_PHONE_TEXT_CLIP: int = 50    # 单条消息文本截断长度（唯一截断点，军规②）


def _lang_track_card(date: str, cfg: Config) -> dict[str, Any] | None:
    """读 langTrack 事实卡（纯读、支持任意历史日）；库缺失返回 None，读失败降级空卡不抛。"""
    from gacore.langTrack import fact_card

    db = cfg.root / "data" / "langTrack.db"
    if not db.exists():
        return None
    return fact_card.build(day=date, detail="full", db_path=db, outlet="info_pack")


def _lang_track_notifs(date: str, cfg: Config, device_id: str | None) -> list[dict[str, Any]]:
    """当日通知内容事件（含 title/text 的载体事件，按时间升序）；库缺失返回 []。

    payload 里 removed/无文本事件是通知移除标记，不是消息，跳过。
    """
    import sqlite3

    db = cfg.root / "data" / "langTrack.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT ts, payload FROM events WHERE type='notification' "
            "AND date(ts/1000,'unixepoch','+8 hours')=?"
        )
        args: list[Any] = [date]
        if device_id:
            sql += " AND device_id=?"
            args.append(device_id)
        sql += " ORDER BY ts ASC"
        out: list[dict[str, Any]] = []
        for row in conn.execute(sql, args):
            try:
                p = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            text = str(p.get("text") or "")
            title = str(p.get("title") or "")
            if not text and not title:
                continue
            out.append({
                "hhmm": _dt.datetime.fromtimestamp(row["ts"] / 1000, _CN_TZ).strftime("%H:%M"),
                "app": str(p.get("app") or p.get("pkg") or ""),
                "title": title,
                "text": text,
                "clicked": p.get("clicked") == "True",
            })
        return out
    finally:
        conn.close()


def _merge_consecutive(notifs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """连续同源（app+title 相同）事件合并为一条：保留最新文本与时刻，记 merged 条数。

    微信等连发场景一次轰炸十几条同会话消息，不合并会把尾窗整段吃掉。
    """
    out: list[dict[str, Any]] = []
    for n in notifs:
        if out and out[-1].get("app") == n["app"] and out[-1].get("title") == n["title"]:
            out[-1]["text"] = n["text"]
            out[-1]["hhmm"] = n["hhmm"]
            out[-1]["clicked"] = out[-1].get("clicked") or n["clicked"]
            out[-1]["merged"] = out[-1].get("merged", 1) + 1
        else:
            out.append({**n, "merged": 1})
    return out


def _notif_head(n: dict[str, Any]) -> str:
    """单条通知的 "应用·标题" 头，缺标题只留应用，全缺则空串。"""
    head = n.get("app") or ""
    if n.get("title"):
        head = f"{head}·{n['title']}" if head else str(n["title"])
    return head


def _build_phone_place(date: str, cfg: Config) -> tuple[str, str, str]:
    """当日位置轨迹（v3.3 新增）：stays/trips 时间线+停留累计+异常标记。

    pack=时间线（超长折叠复用 fact_card 同款）+停留累计+数据窗口+异常 top5；
    detail=stays/trips/anomalies 全量逐条。措辞口径与 fact_card 不失真（复用其 section 文本）。
    """
    title = "〔手机·位置轨迹〕"
    try:
        from gacore.langTrack.fact_card import _build_stay_section, _build_timeline_section

        card = _lang_track_card(date, cfg)
        if card is None:
            return title, "- 无 langTrack 位置数据（data/langTrack.db 不存在）", ""
        stays = card.get("stays") or []
        trips = card.get("trips") or []
        if not stays and not trips:
            return title, "- 当日无手机位置数据（无 stays/trips）", ""
        lines: list[str] = []
        tl = _build_timeline_section(card)
        if tl:
            lines.append(f"- {tl['text']}")
        st = _build_stay_section(card)
        if st:
            lines.append(f"- {st['text']}")
        if not card.get("day_window_closed") and card.get("data_as_of"):
            lines.append(f"- 数据窗口未闭合（数据至 {str(card['data_as_of'])[11:16]}）")
        anomalies = card.get("anomalies") or []
        for a in anomalies[:5]:
            lines.append(f"- 异常：{a.get('kind', '')} {a.get('poi', '')}（{a.get('detail', '')}）")
        detail_lines = [f"- stays 全量（{len(stays)} 段）："]
        detail_lines += [
            f"  - {s['label']}{s.get('region', '')} {s['start_hhmm']}-{s['end_hhmm']}"
            f"（{s['mins']}min，{s['point_count']}点）"
            for s in stays
        ]
        detail_lines.append(f"- trips 全量（{len(trips)} 段）：")
        detail_lines += [
            f"  - {t['start_hhmm']}-{t['end_hhmm']} {t['from_label'] or '?'}→"
            f"{t['to_label'] or '?'}（{t['dist_m']}m）"
            for t in trips
        ]
        detail_lines += [
            f"- 异常：{a.get('kind', '')} {a.get('poi', '')}（{a.get('detail', '')}）"
            for a in anomalies
        ]
        # 手工语义背景（place_semantics.json）：地点 note + 区县 note，仅 detail 呈现不占 compact 预算
        from gacore.langTrack import place_semantics

        sem = place_semantics.load()
        seen_place_notes: list[str] = []
        seen_district_notes: list[str] = []
        districts_seen: set[str] = set()
        for s in stays:
            n = place_semantics.note_for(s.get("place_id"), s.get("poi") or "", cfg=sem)
            if n and f"{s['label']}——{n}" not in seen_place_notes:
                seen_place_notes.append(f"{s['label']}——{n}")
            d = (s.get("district") or "").strip()
            if d and d not in districts_seen:
                districts_seen.add(d)
                dn = place_semantics.district_note(d, cfg=sem)
                if dn:
                    seen_district_notes.append(f"{d}——{dn}")
        detail_lines += [f"- 地点背景：{t}" for t in seen_place_notes]
        detail_lines += [f"- 区县背景：{t}" for t in seen_district_notes]
        return title, "\n".join(lines), "\n".join(detail_lines)
    except Exception as exc:  # noqa: BLE001 - 最后防线：源失败不中断整包
        logger.warning("daily_info_pack: phone_place failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败：{exc}", ""


def _build_phone_usage(date: str, cfg: Config) -> tuple[str, str, str]:
    """当日手机使用统计（v3.3 新增）：daily_stats 聚合（屏幕/App/解锁/通知计数/作息）。

    pack=聚合行+App Top5；detail=App 排行全量+通知来源分布+作息字段。睡眠字段 ETL 未
    产出时自然缺行；"未见熬夜信号"属默认态不写，只有疑似熬夜才进包。
    """
    title = "〔手机·使用统计〕"
    try:
        from gacore.langTrack.fact_card import _hhmm_text

        card = _lang_track_card(date, cfg)
        if card is None:
            return title, "- 无 langTrack 使用数据（data/langTrack.db 不存在）", ""
        if not card.get("available"):
            return title, "- 当日无手机使用统计（daily_stats 缺失）", ""
        parts = [f"屏幕 {card.get('screen_hours', 0.0):.1f}h"]
        if card.get("unlock_count"):
            parts.append(f"解锁 {card['unlock_count']}")
        if card.get("switch_count"):
            parts.append(f"切换 {card['switch_count']}")
        lines = ["- " + " · ".join(parts)]
        apps = card.get("top_apps") or []
        if apps:
            top = "、".join(f"{a.get('app', '')} {(a.get('ms') or 0) / 3600000:.1f}h" for a in apps[:5])
            lines.append(f"- App Top：{top}")
        n, c = card.get("notification_count", 0), card.get("notification_clicked", 0)
        if n:
            src = "、".join(a.get("app", "") for a in (card.get("top_notification_apps") or [])[:3])
            lines.append(f"- 通知 {n} 条 · 点击 {c} 条" + (f" · 来源 {src}" if src else ""))
        start, end = card.get("sleep_start_hhmm"), card.get("sleep_end_hhmm")
        if start or end:
            w = "作息"
            if start:
                w += f" 睡 {_hhmm_text(start)}"
            if end:
                w += f" 起 {_hhmm_text(end)}"
            if card.get("sleep_duration_min"):
                w += f"（{card['sleep_duration_min']}min）"
            lines.append(f"- {w}")
        signal = str(card.get("sleep_signal") or "")
        if signal.startswith("凌晨"):
            lines.append(f"- 熬夜信号：{signal}")
        # detail：App 排行全量直查 daily_stats（fact_card 卡内截 8），另附通知来源分布
        detail_lines: list[str] = []
        import sqlite3

        db = cfg.root / "data" / "langTrack.db"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT app_ranking_json, top_notification_apps_json FROM daily_stats WHERE day=?",
                (date,),
            ).fetchall()
        finally:
            conn.close()
        if len(rows) == 1:
            full_apps = json.loads(rows[0]["app_ranking_json"] or "[]")
            if full_apps:
                detail_lines.append(
                    f"- App 排行全量（{len(full_apps)} 个）："
                    + "、".join(f"{a.get('app', '')} {(a.get('ms') or 0) / 3600000:.2f}h" for a in full_apps)
                )
            full_src = json.loads(rows[0]["top_notification_apps_json"] or "[]")
            if full_src:
                detail_lines.append(
                    "- 通知来源分布：" + "、".join(f"{a.get('app', '')}×{a.get('n', 0)}" for a in full_src)
                )
        elif len(rows) > 1:
            detail_lines.append(f"- 当日 daily_stats 有 {len(rows)} 行（多设备），detail 不合并只提示")
        return title, "\n".join(lines), "\n".join(detail_lines)
    except Exception as exc:  # noqa: BLE001 - 最后防线：源失败不中断整包
        logger.warning("daily_info_pack: phone_usage failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败：{exc}", ""


def _build_phone_notif(date: str, cfg: Config) -> tuple[str, str, str]:
    """当日通知摘要（v3.3 新增）：内容事件尾窗（最近 N 条，连续同源合并）。

    采样策略实证（2026-10-05，近 10 天 1359 事件）：payload clicked=True 只出现在无文本的
    移除标记上，"只取点击过的"会永远空转 → 改为尾窗取最近内容事件（军规①取尾）；点击
    行为计数由 _PHONE_USAGE 的 daily_stats 聚合行承载，本源不重复。
    隐私边界（2026-10-05 用户拍板）：涉敏事件统一边界=仅进本地日报与本人邮箱，绝不出网
    （AGENTS.md 采样军规第 7 条）。
    """
    title = "〔手机·通知摘要〕"
    try:
        card = _lang_track_card(date, cfg)
        device_id = (card or {}).get("device_id") or None
        notifs = _merge_consecutive(_lang_track_notifs(date, cfg, device_id))
        if not notifs:
            return title, "- 当日无通知事件（notification 无内容载体）", ""
        lines: list[str] = [
            f"- 当日消息摘录（内容事件 {len(notifs)} 条，取最近 {_PHONE_NOTIF_TOP}）："
        ]
        for n in notifs[-_PHONE_NOTIF_TOP:]:
            head = _notif_head(n)
            text = n["text"][:_PHONE_TEXT_CLIP]
            body = f"[{n['hhmm']}] {head}：{text}" if head else f"[{n['hhmm']}] {text}"
            if n.get("merged", 1) > 1:
                body += f"（连续 {n['merged']} 条）"
            lines.append(f"  - {body}")
        detail_lines = [f"- 通知内容事件全量（{len(notifs)} 条，同源已合并）："]
        for n in notifs:
            head = _notif_head(n)
            mark = "点击" if n["clicked"] else "未点击"
            extra = f"，连续 {n['merged']} 条" if n.get("merged", 1) > 1 else ""
            detail_lines.append(f"  - [{n['hhmm']}] {head}：{n['text']}（{mark}{extra}）")
        return title, "\n".join(lines), "\n".join(detail_lines)
    except Exception as exc:  # noqa: BLE001 - 最后防线：源失败不中断整包
        logger.warning("daily_info_pack: phone_notif failed", error_type=type(exc).__name__, error=str(exc))
        return title, f"- 该源失败：{exc}", ""


# --------------------------------------------------------------------------- #
# 状态分类（C1）：builder 文案约定 → 机器可读状态的唯一执行点                  #
# --------------------------------------------------------------------------- #
_MISSING_DATA_RE = re.compile(
    r"该日无|今日无|当日无|未登录|不可用|无长期画像|无历史日报|无可列歌单|无文件改动|无.*数据"
)


def classify_body(body: str) -> tuple[str, str]:
    """把 builder 渲染文本分为四态：ok / empty / failed / missing_data。

    - 空 → ("empty", "")
    - 以 "- 该源失败" 开头（含 "该源失败或未登录："、"该源失败/不可用：" 变体）→ ("failed", 冒号后内容)
    - 状态行命中缺失模式 → ("missing_data", 首个命中行)。状态行约定（v3.2）：以 "- " 开头
      且缺失关键词出现在行首 8 字符内——此前 "无.*数据" 会命中正文引文（如编年史里的
      "手机端无采集数据"），导致 _LONG_TERM 全天误报 missing_data，故加位置守卫。
    - 否则 ("ok", "")
    """
    if not body.strip():
        return "empty", ""
    stripped = body.lstrip()
    if stripped.startswith("- 该源失败"):
        colon = stripped.find("：")
        return "failed", stripped[colon + 1:].strip() if colon != -1 else ""
    for ln in body.splitlines():
        s = ln.strip()
        if not s.startswith("- "):
            continue
        m = _MISSING_DATA_RE.search(s)
        if m and m.start() <= 8:
            return "missing_data", s
    return "ok", ""


_STATUS_TEXT: Final = {"ok": "全量", "failed": "失败", "missing_data": "无数据"}


def _header_with_status(title: str, status: str, note: str) -> str:
    """C3：非空 body 的标题追加状态元信息，如 〔BILI｜状态:失败:CLI 未登录〕。"""
    base = _STATUS_TEXT.get(status)
    if not base:
        return title
    if status == "failed" and note:
        base = f"失败:{note}"
    suffix = f"｜状态:{base}"
    idx = title.rfind("〕")
    if idx != -1:
        return title[:idx] + suffix + title[idx:]
    return title + suffix


# --------------------------------------------------------------------------- #
# 裁剪与装配（C3）：行级截断 + 超预算整块丢弃                                  #
# --------------------------------------------------------------------------- #
_TRUNCATION_TAIL = "（已截断 {n} 行，完整数据可经 langTrack_stats / search_daily 补查）"


def _cap_lines_counted(body: str, cap: int) -> tuple[str, int]:
    """行级截断：按行累积、行末截停（不断章）。返回 (截断后文本, 被截掉的行数)。"""
    if len(body) <= cap:
        return body, 0
    lines = body.splitlines()
    out: list[str] = []
    used = 0
    for ln in lines:
        extra = len(ln) + (1 if out else 0)
        if used + extra > cap:
            break
        out.append(ln)
        used += extra
    dropped = len(lines) - len(out)
    if not out:
        # 首行自身超 cap：退回字符裁剪，避免整块变空（无整行被丢弃，不加行数提示）
        out.append(lines[0][:max(cap, 0)])
        dropped = 0
    text = "\n".join(out)
    if dropped:
        text += "\n" + _TRUNCATION_TAIL.format(n=dropped)
    return text, dropped


def _cap_lines(body: str, cap: int) -> str:
    """单源行级截断（含头部块等非源调用点）；截断尾部带补查提示。"""
    return _cap_lines_counted(body, cap)[0]


def _assemble_blocks(blocks: list[tuple[str, str]], budget: int) -> tuple[str, list[str]]:
    """按预算装配：超预算即熔断，本块与其后全部整块丢弃（断章比缺源更误导模型）。

    仅当首个（非空）块自身超预算时才按字符裁剪兜底，避免整包为空。
    返回 (装配文本, 与 blocks 对齐的每块实际进包文本——被丢弃/跳过的块为 "")。
    """
    out: list[str] = []
    packed: list[str] = []
    total = 0
    broken = False
    for header, body in blocks:
        if broken or not body.strip():
            packed.append("")
            continue
        seg = f"{header}\n{body}\n\n"
        if total + len(seg) > budget:
            if total == 0 and budget > 16:
                # 预留省略号的位置：clip 后总长仍 ≤ budget
                clipped = seg.rstrip()[: budget - 1].rstrip() + "…"
                out.append(clipped)
                packed.append(clipped)
            else:
                packed.append("")
            broken = True  # 熔断：其后全弃
            continue
        out.append(seg)
        total += len(seg)
        packed.append(body)
    return "".join(out).strip(), packed


def _assemble_pack(blocks: list[tuple[str, str]], budget: int) -> str:
    """按预算装配（仅返回文本的兼容入口；需要逐块进包痕迹时用 _assemble_blocks）。"""
    return _assemble_blocks(blocks, budget)[0]


# --------------------------------------------------------------------------- #
# SourceSpec 注册表（C1）：声明收拢一处，新增源 = 加一条                       #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceSpec:
    """单个信息源的声明：key 供 jsonl/详情文件命名，cap 供行级截断，priority 供预算熔断排序。"""

    key: str      # "_CHAT"，jsonl/监控用
    cap: int
    priority: int  # 越小越优先保留，现顺序 ×10（10..100；_LANGTRACK 移除后留空位不重排）
    builder: Callable[[str, Config], tuple[str, str, str]]


SOURCES: Final[tuple[SourceSpec, ...]] = (
    # 注册顺序即 priority 升序（stats 顺序与之对齐）；手机事实源（v3.3）填 ×10 序列空位，
    # 细维度仍不做源（C2 A′ 局部修订：当日全天视角进包，fact_card 保留"当前时刻"出口）
    SourceSpec("_LONG_TERM", _LONG_TERM_CAP, 10, _build_long_term_picture),
    SourceSpec("_PHONE_PLACE", _PHONE_PLACE_CAP, 15, _build_phone_place),
    SourceSpec("_CHAT", _CHAT_CAP, 20, _build_chat),
    SourceSpec("_BILI", _BILI_CAP, 40, _build_bili),
    SourceSpec("_PHONE_USAGE", _PHONE_USAGE_CAP, 45, _build_phone_usage),
    SourceSpec("_EDGE", _EDGE_CAP, 50, _build_edge),
    SourceSpec("_MEDIA", _MEDIA_CAP, 60, _build_media),
    SourceSpec("_PHONE_NOTIF", _PHONE_NOTIF_CAP, 65, _build_phone_notif),
    SourceSpec("_GIT", _GIT_CAP, 70, _build_git),
    SourceSpec("_FILES", _FILES_CAP, 80, _build_files),
    SourceSpec("_NCM", _NCM_CAP, 90, _build_ncm),
    SourceSpec("_MEMORY", _MEMORY_CAP, 100, _build_memory),
)

# 最近一次装配的逐源统计（含 title 与三节正文，供 scheduler 落盘钩子取用）。
# 为什么放模块级：scheduler 测试的 seam 是 monkeypatch build_info_pack（不走 report），
# 落盘所需的 stats 只能由装配函数顺手暂存，scheduler 经 last_pack_stats() 取用。
_LAST_PACK_STATS: list[dict[str, Any]] = []


def last_pack_stats() -> list[dict[str, Any]]:
    """最近一次 build_info_pack_report 的逐源统计快照；测试替换 seam 时为空 → scheduler 跳过落盘。"""
    return list(_LAST_PACK_STATS)


def build_info_pack_report(date: str, cfg: Config | None = None) -> tuple[str, list[dict[str, Any]]]:
    """装配当日信息包并产出逐源体检统计。任取数源失败均降级标注，不抛异常。

    返回 (pack 文本, stats)。stats 每源含 key/title/status/chars/full_chars/detail_chars/note
    及 detail_body/pack_body/packed_body 三节正文：
    - full_chars = 渲染文本（挑选/压缩后、截断前）长度
    - chars      = 实际进包块文本长度（预算熔断整块丢弃时 0 且 note="未进包:预算熔断"）
    - detail_chars = 完整取数详情（未挑选未压缩）长度
    """
    resolved = cfg or Config.default()
    blocks: list[tuple[str, str]] = []

    # 头部（消费指令）单独装配：受 _HEAD_CAP 行级截断、也受整包 budget 约束。
    blocks.append(("〔当日信息包·" + date + "〕", _cap_lines(_instruction_head(date), _HEAD_CAP)))

    # v3.2.2 逐源预算覆盖：config/info_pack.json 现读现用（页面 /config 改完下次构建即生效）
    ov = load_source_config(resolved)
    src_ov: dict[str, dict[str, Any]] = {
        k: v for k, v in (ov.get("sources") or {}).items() if isinstance(v, dict)
    }
    budget = _clamped_int(ov.get("pack_budget"), PACK_BUDGET, _BUDGET_MIN, _BUDGET_MAX)

    stats: list[dict[str, Any]] = []
    ordered = sorted(  # priority 可被 config 覆盖（越小越先装配、越晚被熔断）；稳定排序保持同值原序
        SOURCES,
        key=lambda s: _clamped_int((src_ov.get(s.key) or {}).get("priority"), s.priority, 1, 999),
    )
    for spec in ordered:
        sov = src_ov.get(spec.key) or {}
        if sov.get("enabled") is False:
            stats.append({
                "key": spec.key, "title": "", "status": "disabled", "note": "已停用(config)",
                "chars": 0, "full_chars": 0, "detail_chars": 0, "budget": budget,
                "dropped_lines": 0, "block_pos": -1,
                "pack_body": "", "detail_body": "", "packed_body": "",
            })
            continue
        cap = _clamped_int(sov.get("cap"), spec.cap, _CAP_MIN, _CAP_MAX)
        try:
            title, pack_body, detail_body = spec.builder(date, resolved)
        except Exception as exc:  # noqa: BLE001 - 最后防线：绝不中断整包
            logger.error(
                "daily_info_pack: builder raised",
                builder=spec.key,
                error_type=type(exc).__name__,
                stack_trace=str(exc),
            )
            title, pack_body, detail_body = f"〔{spec.key.strip('_')}〕", f"- 该源失败：{exc}", ""
        status, note = classify_body(pack_body)
        capped, dropped_n = _cap_lines_counted(pack_body, cap)
        block_pos = -1
        if status != "empty":
            block_pos = len(blocks)
            blocks.append((_header_with_status(title, status, note), capped))
        stats.append({
            "key": spec.key,
            "title": title,
            "status": status,
            "note": note,
            "dropped_lines": dropped_n,
            "block_pos": block_pos,
            "budget": budget,
            "full_chars": len(pack_body),
            "detail_chars": len(detail_body),
            "pack_body": pack_body,
            "detail_body": detail_body,
        })

    pack, packed_bodies = _assemble_blocks(blocks, budget)
    for st in stats:
        packed_body = packed_bodies[st["block_pos"]] if st["block_pos"] >= 0 else ""
        if st["status"] != "empty" and not packed_body:
            st["chars"] = 0
            st["note"] = "未进包:预算熔断"
        else:
            st["chars"] = len(packed_body)
            if not st["note"] and st.get("dropped_lines"):
                st["note"] = f"truncated={st['dropped_lines']}"
        st["packed_body"] = packed_body
        # 落盘后不再需要的装配内部字段
        st.pop("block_pos", None)
        st.pop("dropped_lines", None)

    if not pack.strip():
        # 极端兜底：任何源都失败也返回最小可用文本，绝不抛异常
        pack = (
            f"〔当日信息包·{date}〕\n"
            "- 全部信息源取数失败，本次日报请基于系统提示注入的长期画像与近 2 日笔记、"
            "以及 run 过程中按需调用 search_daily / langTrack_stats 补足素材。"
        )
    global _LAST_PACK_STATS
    _LAST_PACK_STATS = stats
    logger.info("daily info pack built", date=date, chars=len(pack), budget=budget)
    return pack, stats


def build_info_pack(date: str, cfg: Config | None = None) -> str:
    """生成当日信息包文本（≤8000 字）。薄包装：签名/返回值不变（scheduler 测试 monkeypatch 该名），
    stats 由 build_info_pack_report 顺手暂存到 last_pack_stats()。"""
    return build_info_pack_report(date, cfg)[0]


# --------------------------------------------------------------------------- #
# 双文件落盘（C1 v0.7）：jsonl 元数据 + 三节详情，全部 best-effort             #
# --------------------------------------------------------------------------- #
def write_pack_health(
    cfg: Config,
    date: str,
    job: str,
    trigger: str,
    stats: list[dict[str, Any]],
    total_chars: int,
    correction_chars: int = 0,
) -> None:
    """追加一行体检元数据 JSON 到 data/logs/info_pack_health.jsonl（best-effort，失败仅告警）。"""
    try:
        path = cfg.root / "data" / "logs" / "info_pack_health.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": _dt.datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "date": date,
            "job": job,
            "trigger": trigger,
            "total_chars": total_chars,
            # v3.2.2：记录本次实际生效的整包预算（config 可覆盖 PACK_BUDGET；stats 由 build 装配时带上）
            "budget": (stats[0].get("budget") if stats and stats[0].get("budget") else PACK_BUDGET),
            "correction_chars": correction_chars,
            "sources": [
                {
                    "key": s.get("key", ""),
                    "status": s.get("status", ""),
                    "chars": s.get("chars", 0),
                    "full_chars": s.get("full_chars", 0),
                    "detail_chars": s.get("detail_chars", 0),
                    "note": s.get("note", ""),
                }
                for s in stats
            ],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("daily_info_pack: write_pack_health failed", error=str(exc))


def _cleanup_pack_detail(root: Path) -> None:
    """按目录名解析日期清理超过保留期的详情目录；解析失败（非日期目录）跳过。"""
    if not root.is_dir():
        return
    today = _dt.datetime.now(_CN_TZ).date()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            d = _dt.datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - d).days > _PACK_DETAIL_RETENTION_DAYS:
            shutil.rmtree(child, ignore_errors=True)


def write_pack_detail(
    cfg: Config,
    date: str,
    key: str,
    title: str,
    status: str,
    detail_body: str,
    pack_body: str,
    packed_body: str,
) -> None:
    """写单源三节详情 data/logs/pack_detail/{date}/{key}.md（best-effort，失败仅告警）。

    三节 = 完整取数详情（未挑选未压缩）/ 渲染文本（挑选压缩后、截断前）/ 实际进包（截断与熔断后）。
    写前顺带清理超过 90 天的日期目录。
    """
    try:
        root = cfg.root / "data" / "logs" / "pack_detail"
        _cleanup_pack_detail(root)
        day_dir = root / date
        day_dir.mkdir(parents=True, exist_ok=True)
        text = (
            f"# {key} · {date} · {status}\n"
            f"\n"
            f"## 完整取数详情\n{detail_body.strip() or '（无）'}\n"
            f"\n"
            f"## 渲染文本\n{pack_body.strip()}\n"
            f"\n"
            f"## 实际进包\n{packed_body.strip() or '未进包（预算熔断）'}\n"
        )
        (day_dir / f"{key}.md").write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning("daily_info_pack: write_pack_detail failed", key=key, error=str(exc))


def write_fact_card_detail(cfg: Config, card: Mapping[str, Any] | None) -> None:
    """C1 v0.7 事实卡支线：compact 全文 + 预算省略记录 + 水位 落到 pack_detail/{day}/_FACT_CARD.md。

    每次构建覆盖写（保证最新）；失败静默——绝不影响 QQ 注入与日报主路径。
    """
    try:
        if not card:
            return
        day = str(card.get("day") or "")
        if not day:
            return
        omitted = card.get("compact_omitted") or {}
        if isinstance(omitted, dict) and omitted:
            omitted_text = "\n".join(f"- {sid}: {reason}" for sid, reason in omitted.items())
        else:
            omitted_text = "（无省略）"
        compact_text = str(card.get("compact") or "").strip() or "（无）"
        waterline = (
            f"etl_watermark: {card.get('etl_watermark') or '（无）'}\n"
            f"data_as_of: {card.get('data_as_of') or '（无）'}"
        )
        text = (
            f"# _FACT_CARD · {day} · fact-card支线\n"
            f"\n"
            f"## compact 全文\n{compact_text}\n"
            f"\n"
            f"## compact_omitted（预算省略的 section）\n{omitted_text}\n"
            f"\n"
            f"## 水位\n{waterline}\n"
        )
        path = cfg.root / "data" / "logs" / "pack_detail" / day / "_FACT_CARD.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - 静默：支线绝不挡主路径
        logger.warning("daily_info_pack: write_fact_card_detail failed", error=str(exc))


def cap_lines(body: str, cap: int) -> str:
    """Public line-level truncation primitive (C3) for cross-module pack builders —
    scheduler._build_job_prompt uses it for the 〔人工订正〕/〔用户偏好〕 blocks so every
    injected block truncates on line boundaries with the same tail note."""
    return _cap_lines(body, cap)


__all__ = (
    "PACK_BUDGET",
    "SOURCES",
    "SourceSpec",
    "build_info_pack",
    "build_info_pack_report",
    "cap_lines",
    "classify_body",
    "last_pack_stats",
    "write_fact_card_detail",
    "write_pack_detail",
    "write_pack_health",
)
