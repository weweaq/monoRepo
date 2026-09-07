"""当日信息包（Daily Info Pack）：scheduler 侧确定性预取 + 裁剪 + 逐源降级，随 user prompt 注入。

对齐 docs 设计文档《daily-report-redesign-v2》的落地实现：

- 只做编排，不重复实现取数：
  * 长期画像复用 scheduler 现有 ``_long_term_insight`` / ``_summarize_long_term``（延迟 import 避免循环依赖）；
  * 近 2 日 daily notes 摘要已由 ``context.build_system_prompt`` 注入（"保持注入"，此处不再重复内容，只给引用提示）；
  * 其余确定性源复用 tools 下的 ``.func``：bili_history / browser_history / langTrack_stats / ncm_me / ncm_playlist_list。
- 逐源 try/except 兜底：单源失败只在本节标注"该源失败/无今日数据"，绝不中断整包、不导致 run_job 失败。
- 输出自带消费指令模板（时间戳语义 / 素材须引用 / ≤8000 字硬控）。
- 模块内聚：取数 / 裁剪 / 降级 / 装配都在本模块，对外只暴露 ``build_info_pack(date, cfg=None) -> str``。

模块级 ``_*_FN`` 名字是对外接入点：测试通过 monkeypatch 这些名字注入 fake 取数（空/满双向），
不触碰真实 CLI / 数据库。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gacore.config import Config
from gacore.jsonl_logger import get_logger

logger = get_logger("daily_info_pack")

# --------------------------------------------------------------------------- #
# 预算与各源硬上限（字符数，含中文）。整包 8000 字上限，单源超限会被截断。    #
# 2026-09-04 扩容：2000 → 8000（8 路被动信号压得太狠，日期/标题/画像被截掉）； #
# 同时新增第 9 路「当日 QQ 对话摘录」（用户第一人称信号，详见 _build_chat）。   #
# --------------------------------------------------------------------------- #
PACK_BUDGET: int = 8000  # 整包硬上限
_HEAD_CAP: int = 400      # 消费指令块
_LONG_TERM_CAP: int = 1600  # 长期画像 compact（40 行内）
_CHAT_CAP: int = 1200     # 当日 QQ 对话摘录（用户侧）
_LANGTRACK_CAP: int = 800  # langTrack 手机细维度
_BILI_CAP: int = 1200      # B站当日 top20
_EDGE_CAP: int = 900       # Edge 域名归并 top10
_GIT_CAP: int = 700        # git 当日提交
_FILES_CAP: int = 1000     # 当日文件活动 top15
_NCM_CAP: int = 700        # ncm 歌单/收藏静态基线
_MEMORY_CAP: int = 900     # 前日日报摘要（可选）

_LONG_TERM_LINES: int = 40  # 画像 compact 行数上限（对齐 _summarize_long_term 默认）
_BILI_TOP: int = 20         # B站当日观看 top N
_EDGE_TOP: int = 10         # Edge 域名归并 top N
_FILES_TOP: int = 15        # 文件活动目录聚合 top N
_NCM_TOP: int = 10          # ncm 歌单 top N
_CHAT_TOP: int = 15         # 当日对话摘录条数上限

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
from gacore.tools.langTrack_tools import langTrack_stats as _langtrack_tool
from gacore.tools.ncm_tools import ncm_me as _ncm_me_tool
from gacore.tools.ncm_tools import ncm_playlist_list as _ncm_playlist_tool

_BILLI_FN = _bili_tool.func
_BROWSER_FN = _browser_tool.func
_LANGTRACK_FN = _langtrack_tool.func
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
        "- 降级说明：单个信息源失败会标注“该源失败/无今日数据”，属正常降级，不影响整体写作；"
        "近 2 日 daily notes 摘要与当日生活事实卡 compact 已随系统提示注入，此处不重复。\n"
    )


# --------------------------------------------------------------------------- #
# 信息源 builders：每个 builder 自带 try/except，返回 (header, body)           #
# --------------------------------------------------------------------------- #
def _build_long_term_picture(date: str, cfg: Config) -> tuple[str, str]:
    """长期画像 compact。复用 scheduler 的 _long_term_insight / _summarize_long_term。"""
    from gacore.scheduler import _long_term_insight, _summarize_long_term  # 延迟 import 避循环

    try:
        text = _long_term_insight(cfg)
        if not text:
            return "〔长期画像·compact〕", "- 无长期画像文件（memory/global_mem_insight.txt 缺失），本日仅凭当日信号写作。"
        compact = _summarize_long_term(text, limit_lines=_LONG_TERM_LINES)
        return "〔长期画像·compact〕", compact
    except Exception as exc:  # noqa: BLE001 - 单源降级
        logger.warning("daily_info_pack: long-term picture failed", error_type=type(exc).__name__, error=str(exc))
        return "〔长期画像·compact〕", f"- 该源失败：{exc}"


def _build_chat(date: str, cfg: Config) -> tuple[str, str]:
    """当日 QQ 对话摘录（用户侧原话）：画像链路里唯一的第一人称信号源。

    数据来源 memory/qq_chat_log.jsonl（qq.py 收发消息时逐行 append，schema 见
    _persist_chat_log）。只取 direction=user 且非 "/" 命令的行，按时间正序列
    最多 _CHAT_TOP 条。该源是 2026-09-04 新增的第 9 路。
    """
    path = cfg.memory_dir / "qq_chat_log.jsonl"
    if not path.is_file():
        return "〔对话·当日 QQ 摘录〕", "- 当日无 QQ 对话记录（qq_chat_log.jsonl 不存在）"
    lines: list[str] = []
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
                if len(lines) < _CHAT_TOP:
                    hhmm = ts[11:16] if len(ts) >= 16 else ""
                    lines.append(f"- {hhmm} {text[:90]}")
    except OSError as exc:
        return "〔对话·当日 QQ 摘录〕", f"- 该源失败：{exc}"
    if not lines:
        if total:
            return "〔对话·当日 QQ 摘录〕", f"- 当日 QQ 对话仅 {total} 条命令类消息，无正文摘录"
        return "〔对话·当日 QQ 摘录〕", "- 当日无 QQ 对话记录"
    body = "\n".join(lines)
    if total > _CHAT_TOP:
        body += f"\n（当日共 {total} 条用户消息，仅列前 {_CHAT_TOP}）"
    return "〔对话·当日 QQ 摘录〕", body


def _build_langtrack(date: str, cfg: Config) -> tuple[str, str]:
    """langTrack 手机细维度：睡眠 / 解锁 / 通知 / 时段×应用（compact 已在系统提示，不重复）。"""
    try:
        stats = _LANGTRACK_FN(day=date)
        if not isinstance(stats, dict):
            return "〔手机使用·langTrack〕", "- 该源失败：返回格式异常"
        if "error" in stats:
            msg = stats.get("message") or stats.get("error")
            return "〔手机使用·langTrack〕", f"- 该源失败：{msg}"
        if not stats.get("available"):
            return "〔手机使用·langTrack〕", "- 该日无 langTrack 手机数据（可能未采集/未同步）"
        lines: list[str] = []
        try:
            hours = stats.get("screen_hours")
            if hours:
                lines.append(f"- 当日屏幕时长：{hours:.1f}h")
        except (TypeError, ValueError):
            pass
        unlock = stats.get("unlock_count")
        if unlock:
            lines.append(f"- 解锁 {unlock} 次")
        notification_clicked = stats.get("notification_clicked")
        if notification_clicked:
            lines.append(f"- 点击通知 {notification_clicked} 条")
        sleep_signal = stats.get("sleep_signal")
        if sleep_signal:
            lines.append(f"- 睡眠信号：{sleep_signal}")
        sleep_window = _fmt_sleep_window(stats)
        if sleep_window:
            lines.append(f"- 作息窗口：{sleep_window}")
        top_apps = stats.get("top_apps") or []
        if top_apps:
            app_desc = ", ".join(str(_app_label(a)) for a in list(top_apps)[:5])
            lines.append(f"- App 活跃 Top：{app_desc}")
        time_app = stats.get("time_app") or []
        if time_app:
            seg_desc = "、".join(str(_time_seg_label(t)) for t in list(time_app)[:4])
            lines.append(f"- 时段×应用：{seg_desc}")
        if not lines:
            return "〔手机使用·langTrack〕", "- 该日有 langTrack 数据但无可用细维度信号"
        return "〔手机使用·langTrack〕", "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 - 单源降级
        logger.warning("daily_info_pack: langtrack failed", error_type=type(exc).__name__, error=str(exc))
        return "〔手机使用·langTrack〕", f"- 该源失败：{exc}"


def _fmt_sleep_window(stats: dict[str, Any]) -> str:
    """作息窗口：优先用 P0 语义字段（sleep_start/end_hhmm），缺失回退 sleep_signal。"""
    start = stats.get("sleep_start_hhmm")
    end = stats.get("sleep_end_hhmm")
    duration = stats.get("sleep_duration_min")
    if not start and not end:
        return ""
    parts = []
    if start:
        parts.append(f"睡 {start}")
    if end:
        parts.append(f"起 {end}")
    if duration:
        parts.append(f"时长 {duration}min")
    return " ".join(parts)


def _app_label(app: Any) -> str:
    """将 top_apps / time_app 的条目压成紧凑标签，兼容 dict / 标量。"""
    if isinstance(app, dict):
        name = app.get("app") or app.get("name") or app.get("package") or str(app.get("label") or "")
        value = app.get("value") if "value" in app else app.get("minutes") if "minutes" in app else ""
        return f"{name}{f'({value})' if value else ''}" if name else str(app)
    return str(app)


def _time_seg_label(item: Any) -> str:
    """时段×应用条目的紧凑标签：优先取 segment 字段，否则 _app_label。"""
    if isinstance(item, dict):
        seg = item.get("segment") or item.get("period") or item.get("time_slot")
        if seg:
            return f"{seg}:{_app_label(item)}"
    return _app_label(item)


def _build_bili(date: str, cfg: Config) -> tuple[str, str]:
    """B站当日观看 top20（include_duration=False，避免逐条拉时长 CLI 慢调用）。"""
    try:
        res = _BILLI_FN(limit=50, page=1, include_duration=False)
        if not isinstance(res, dict):
            return "〔浏览·B站观看 top〕", "- 该源失败：返回格式异常"
        if "error" in res:
            msg = res.get("message") or res.get("error")
            return "〔浏览·B站观看 top〕", f"- 该源失败或未登录：{msg}"
        entries = res.get("entries") or []
        today_entries = [e for e in entries if str(e.get("viewed_at", ""))[:10] == date]
        if not today_entries:
            return "〔浏览·B站观看 top〕", "- 今日无 B 站观看记录"
        lines = []
        for e in today_entries[:_BILI_TOP]:
            title = str(e.get("title") or "(无标题)")
            author = str(e.get("author") or "")
            ts = str(e.get("viewed_at") or "")[5:16].replace("T", " ")
            lines.append(f"- {ts} {title}｜UP:{author}")
        body = "\n".join(lines)
        if len(today_entries) > _BILI_TOP:
            body += f"\n（当日共 {len(today_entries)} 条，仅列前 {min(_BILI_TOP, len(today_entries))}）"
        return "〔浏览·B站观看 top〕", body
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: bili failed", error_type=type(exc).__name__, error=str(exc))
        return "〔浏览·B站观看 top〕", f"- 该源失败：{exc}"


def _build_edge(date: str, cfg: Config) -> tuple[str, str]:
    """Edge 浏览器历史按域名归并 top10。"""
    try:
        res = _BROWSER_FN(browser="edge", days=1, limit=100)
        if not isinstance(res, dict):
            return "〔浏览·Edge 域名〕", "- 该源失败：返回格式异常"
        if "error" in res:
            if res.get("error") == "db_not_found":
                return "〔浏览·Edge 域名〕", "- 该源失败/不可用：Edge 历史库不存在（今日无浏览器历史可用）"
            return "〔浏览·Edge 域名〕", f"- 该源失败：{res.get('message') or res.get('error')}"
        entries = res.get("entries") or []
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            host = urlparse(str(entry.get("url") or "")).netloc or "(未知)"
            groups.setdefault(host, []).append(entry)
        if not groups:
            return "〔浏览·Edge 域名〕", "- 当日无 Edge 浏览记录"
        top = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:_EDGE_TOP]
        lines = []
        for host, items in top:
            title = ""
            for it in items:
                t = str(it.get("title") or "").strip()
                if t:
                    title = t[:36]
                    break
            lines.append(f"- {host}：{len(items)} 次{f'（{title}）' if title else ''}")
        return "〔浏览·Edge 域名〕", "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: edge failed", error_type=type(exc).__name__, error=str(exc))
        return "〔浏览·Edge 域名〕", f"- 该源失败：{exc}"


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


def _build_git(date: str, cfg: Config) -> tuple[str, str]:
    """当日 git 提交（hash 截断 + 消息精简）。"""
    try:
        rc, stdout = _run_git(cfg.root, date)
        if rc != 0:
            return "〔工作·当日 git 提交〕", "- 该源失败/不可用：git log 读取失败（可能非 git 仓库）"
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            return "〔工作·当日 git 提交〕", "- 今日无 git 提交"
        shown = []
        for ln in lines:
            parts = ln.split("|")
            if len(parts) >= 2:
                short_hash = parts[0][:8]
                subject = parts[1][:60]
                author = parts[2] if len(parts) > 2 else ""
                shown.append(f"- {short_hash} {subject}（{author}）")
        return "〔工作·当日 git 提交〕", "\n".join(shown)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: git failed", error_type=type(exc).__name__, error=str(exc))
        return "〔工作·当日 git 提交〕", f"- 该源失败：{exc}"


def _build_files(date: str, cfg: Config) -> tuple[str, str]:
    """当日文件活动：扫描仓库（跳过依赖/缓存/日志目录）按目录聚合 top15。"""
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
        return "〔工作·当日文件活动〕", f"- 该源失败：{exc}"
    if not counts:
        return "〔工作·当日文件活动〕", "- 今日仓库内无文件改动"
    lines = []
    for key, n in counts.most_common(_FILES_TOP):
        sample = samples.get(key, "")
        lines.append(f"- {key}：{n} 个文件{f'（样例 {sample}）' if sample else ''}")
    return "〔工作·当日文件活动〕", "\n".join(lines)


def _build_ncm(date: str, cfg: Config) -> tuple[str, str]:
    """ncm 歌单/收藏静态基线（兴趣基线，非当日动态；失败静默跳过）。"""
    try:
        me = _NCM_ME_FN()
        nickname = ""
        if isinstance(me, dict) and not me.get("error"):
            nickname = f"（{me.get('nickname') or ''}）" if me.get("nickname") else ""
        pl = _NCM_PLAYLIST_FN(limit=50, offset=0)
        if not isinstance(pl, dict):
            return "〔基线·网易云歌单/收藏〕", "- 该源失败：返回格式异常"
        if "error" in pl:
            msg = pl.get("message") or pl.get("error")
            return "〔基线·网易云歌单/收藏〕", f"- 该源失败/未登录：{msg}"
        playlists = pl.get("playlists") or []
        if not playlists:
            return "〔基线·网易云歌单/收藏〕", "- 该账号无可列歌单"
        lines = []
        for p in playlists[:_NCM_TOP]:
            name = str(p.get("name") or "未命名")
            count = p.get("track_count")
            tag = "·收藏" if p.get("subscribed") else "·自建"
            lines.append(f"- {name}{tag}" + (f"（{count} 首）" if count else ""))
        head = f"〔基线·网易云歌单/收藏〕{nickname}"
        return head, "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: ncm failed", error_type=type(exc).__name__, error=str(exc))
        return "〔基线·网易云歌单/收藏〕", f"- 该源失败/跳过：{exc}"


def _latest_prev_report(cfg: Config) -> tuple[str, str] | None:
    """读 logs/scheduled/ 下最新一份 daily-report 输出（昨日/最近一次），返回 (时间戳, 摘要)。"""
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
    # 取 ## Reply 之后的正文前约 260 字作为摘要；在下一个 "## " 小节边界截断，
    # 兼容 2026-09-04 起输出文件里 Reply 之后还有 "(raw model output)" 小节的格式
    body = ""
    if "## Reply" in text:
        body = text.split("## Reply", 1)[1]
        body = body.split("\n## ", 1)[0].strip()
    else:
        body = text.strip()
    snippet = body[:260].strip()
    if not snippet:
        return None
    return (ts, snippet)


def _build_memory(date: str, cfg: Config) -> tuple[str, str]:
    """记忆信号：前日日报摘要（可选，零成本）。近 2 日 notes 摘要由系统提示注入，不重复。"""
    try:
        prev = _latest_prev_report(cfg)
        if prev is None:
            return "〔记忆·前日日报〕", "- 无历史日报输出可作基准（首次运行）"
        ts, snippet = prev
        ts_note = f"（{ts}）" if ts else ""
        return "〔记忆·前日日报〕", f"- 前一次日报生成于 {ts_note}，其正文起点如下，供本日“微变化”对照：\n  “{snippet}”"
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_info_pack: prev report failed", error_type=type(exc).__name__, error=str(exc))
        return "〔记忆·前日日报〕", f"- 该源失败：{exc}"


# --------------------------------------------------------------------------- #
# 装配：预算熔断 + 单源截断                                                   #
# --------------------------------------------------------------------------- #
def _cap_text(body: str, cap: int) -> str:
    """单源硬上限截断：超出 cap 时保留前 cap 字符并加省略标记。"""
    if len(body) <= cap:
        return body
    return body[:cap].rstrip() + "…"


def _assemble_pack(blocks: list[tuple[str, str]], budget: int) -> str:
    """按预算装配：逐块累加，总长超 budget 即熔断，单块超上限由调用方已截断。"""
    out: list[str] = []
    total = 0
    for header, body in blocks:
        if not body.strip():
            continue
        seg = f"{header}\n{body}\n\n"
        if total + len(seg) > budget:
            remain = budget - total
            if remain > 16:
                # 预留省略号的位置：clip 后总长仍 ≤ budget
                clipped = seg.rstrip()[: remain - 1].rstrip() + "…"
                out.append(clipped)
            break
        out.append(seg)
        total += len(seg)
    return "".join(out).strip()


def build_info_pack(date: str, cfg: Config | None = None) -> str:
    """生成当日信息包文本（≤8000 字）。任取数源失败均降级标注，不抛异常。"""
    resolved = cfg or Config.default()
    blocks: list[tuple[str, str]] = []

    # 头部（消费指令）单独装配：受 _HEAD_CAP 单源截断、也受整包 budget 约束。
    blocks.append(("〔当日信息包·" + date + "〕", _cap_text(_instruction_head(date), _HEAD_CAP)))

    builders: list[tuple[str, Callable[[str, Config], tuple[str, str]]]] = [
        ("_LONG_TERM", _build_long_term_picture),
        ("_CHAT", _build_chat),
        ("_LANGTRACK", _build_langtrack),
        ("_BILI", _build_bili),
        ("_EDGE", _build_edge),
        ("_GIT", _build_git),
        ("_FILES", _build_files),
        ("_NCM", _build_ncm),
        ("_MEMORY", _build_memory),
    ]
    caps: dict[str, int] = {
        "_LONG_TERM": _LONG_TERM_CAP,
        "_CHAT": _CHAT_CAP,
        "_LANGTRACK": _LANGTRACK_CAP,
        "_BILI": _BILI_CAP,
        "_EDGE": _EDGE_CAP,
        "_GIT": _GIT_CAP,
        "_FILES": _FILES_CAP,
        "_NCM": _NCM_CAP,
        "_MEMORY": _MEMORY_CAP,
    }
    for key, builder in builders:
        try:
            header, body = builder(date, resolved)
            blocks.append((header, _cap_text(body, caps[key])))
        except Exception as exc:  # noqa: BLE001 - 最后防线：绝不中断整包
            logger.error(
                "daily_info_pack: builder raised",
                builder=key,
                error_type=type(exc).__name__,
                stack_trace=str(exc),
            )
            blocks.append((f"〔{key.strip('_')}〕", f"- 该源失败：{exc}"))
    pack = _assemble_pack(blocks, PACK_BUDGET)
    if not pack.strip():
        # 极端兜底：任何源都失败也返回最小可用文本，绝不抛异常
        pack = (
            f"〔当日信息包·{date}〕\n"
            "- 全部信息源取数失败，本次日报请基于系统提示注入的长期画像与近 2 日笔记、"
            "以及 run 过程中按需调用 search_daily / langTrack_stats 补足素材。"
        )
    logger.info("daily info pack built", date=date, chars=len(pack), budget=PACK_BUDGET)
    return pack


__all__ = ("PACK_BUDGET", "build_info_pack")
