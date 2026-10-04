"""LLM 运行视图数据重建（review_server /llm-requests 与 llm-view-demos 的单一实现）。

从三类现成日志重建"一次 agent 运行"的完整叙事，供运维排障逐层下钻：
- logs/{date}/llm_requests.jsonl   每次真实模型调用：请求+响应+耗时（llm_request_log 落盘）
- logs/{date}/app.jsonl            系统侧动作：邮件投递、ERROR 级事件（jsonl_logger 落盘）
- logs/scheduled/{job}_{ts}.md     日报产出存档（Job finished 时写，含 Reply 节；响应不落
  llm_requests——最终正文在这里）

重建启发式（均有实测依据，见 ROADMAP 2026-10-04）：
- run 边界 = session 变化或相邻调用间隔 > RUN_GAP_SECONDS（llm_requests 无显式 run_id）
- 工具往返 = 本次请求"新增窗口"内的 AI(tool_calls) 与 ToolMessage 按 tool_call_id 配对
  （上一次调用的模型响应发起调用，工具结果随本次请求到达）
- 跨日志进程关联 = session 命中优先、pid 兜底（session 同源前历史日志两 id 不同期，
  不匹配不构成异进程证据；pid 会被进程重启复用，故仅作兜底）
- 批量重发判别 = run 结束窗口内多主题爆发（QQ 反馈触发重发旧报），不计为该 run 自身的投递
- 产出存档归属 = 文件名时间戳（Job finished 时刻）落在 run 结束后 ARCHIVE_WINDOW_SECONDS 内

所有读取均容错：文件缺失/坏行降级为空，绝不抛出到页面。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Final

_UTC8 = timezone(timedelta(hours=8))
RUN_GAP_SECONDS: Final = 600
SYSTEM_EVENT_WINDOW_SECONDS: Final = 300
ARCHIVE_WINDOW_SECONDS: Final = 300

_KIND_BADGE: Final = {
    "daily": "日报任务",
    "proactive": "主动推送",
    "chat": "QQ 对话",
    "internal": "内部调用",
    "other": "其他调用",
}


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC8)
    return dt


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("type") or ""))
        return "".join(parts)
    return str(content or "")


def _one_line(s: str, n: int) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def _resp_summary(resp: dict | None) -> dict | None:
    """Condense the logged response for the timeline view (pre-2026-10-04 lines lack it)."""
    if not resp:
        return None
    if resp.get("error"):
        return {"error": resp["error"], "chars": 0, "text": "", "usage": None}
    content = resp.get("content")
    text = content if isinstance(content, str) else _content_text(content)
    return {
        "chars": resp.get("content_chars") or len(text),
        "text": text if len(text) <= 4000 else text[:4000] + "…[truncated]",
        "toolCalls": len(resp.get("tool_calls") or []),
        "usage": resp.get("usage") or None,
        "error": None,
    }


def load_llm_requests(cfg: Any, date: str) -> list[dict]:
    path = cfg.logs_dir / date / "llm_requests.jsonl"
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    records.sort(key=lambda r: r.get("ts") or "")
    return records


def load_deliveries_and_errors(cfg: Any, date: str) -> tuple[list[dict], list[dict]]:
    """app.jsonl → (deliveries, ERROR-level events)，供运行泳道与红色告警行。"""
    deliveries: list[dict] = []
    errors: list[dict] = []
    path = cfg.logs_dir / date / "app.jsonl"
    if not path.is_file():
        return deliveries, errors
    for line in path.read_text(encoding="utf-8").splitlines():
        if "deliver_email sent" in line:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            deliveries.append({
                "ts": e.get("ts"),
                "pid": e.get("pid"),
                "session": e.get("session"),
                "subject": str(e.get("subject") or ""),
                "failed": "[FAILED]" in str(e.get("subject") or ""),
            })
            continue
        if '"level": "ERROR"' not in line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        detail = str(e.get("error") or e.get("stack_trace") or "")
        errors.append({
            "ts": e.get("ts"),
            "pid": e.get("pid"),
            "session": e.get("session"),
            "module": str(e.get("module") or ""),
            "message": str(e.get("message") or "")[:200],
            "detail": detail[:300],
        })
    return deliveries, errors


def load_output_archives(cfg: Any, date: str) -> list[dict]:
    """scheduled 存档（文件名时间戳 = Job finished 时刻）→ {file, ts, replyChars, reply}。"""
    out: list[dict] = []
    scheduled = cfg.logs_dir / "scheduled"
    if not scheduled.is_dir():
        return out
    for p in sorted(scheduled.glob(f"*_{date.replace('-', '')}_*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"## Reply[^\n]*\n(.*?)(?=\n## |\Z)", text, re.S)
        ts_match = re.search(r"_((?:19|20)\d{6}_\d{6})\.md$", p.name)
        if not m or not ts_match:
            continue
        out.append({
            "file": p.name,
            "ts": datetime.strptime(ts_match.group(1), "%Y%m%d_%H%M%S").isoformat(),
            "replyChars": len(m.group(1).strip()),
            "reply": m.group(1).strip(),
        })
    return out


def classify_run(calls: list[dict]) -> tuple[str, str, str]:
    """(kind, badge title, task excerpt) — 首条请求的纯启发式，零 LLM 成本。"""
    first = calls[0]
    msgs = first.get("messages") or []
    if not msgs:
        return "internal", "内部调用", "非消息列表输入（memory judge 等）"
    human = next((m for m in msgs if m.get("role") == "human"), None)
    htext = _content_text(human.get("content")) if human else ""
    stext = _content_text(msgs[0].get("content")) if msgs and msgs[0].get("role") == "system" else ""
    m = re.search(r"〔当日信息包·(\d{4}-\d{2}-\d{2})〕", htext)
    if m:
        return "daily", "日报任务 · 数据日 " + m.group(1), _one_line(htext, 70)
    if "主动给" in htext and "qq_push" in htext.lower():
        return "proactive", "主动推送", _one_line(htext, 70)
    if "聊天搭子" in stext + htext or "口语化即兴回应" in stext + htext:
        return "chat", "QQ 快答", _one_line(htext, 70)
    if human:
        return "chat", "QQ 对话", _one_line(htext, 70)
    return "other", "其他调用", _one_line(htext, 70)


def build_runs(records: list[dict]) -> list[dict]:
    """重建运行序列：分组、工具往返配对、响应摘要、运行摘要（标题/任务/末条AI/工具集）。"""
    runs: list[list[dict]] = []
    cur: list[dict] = []
    prev: dict | None = None
    for r in records:
        gap = (
            (_parse_ts(r["ts"]) - _parse_ts(prev["ts"])).total_seconds()
            if prev else None
        )
        new_run = prev is None or r.get("session") != prev.get("session") or gap > RUN_GAP_SECONDS
        if new_run and cur:
            runs.append(cur)
            cur = []
        cur.append(r)
        prev = r
    if cur:
        runs.append(cur)

    out: list[dict] = []
    for ri, calls in enumerate(runs, 1):
        kind, title, task = classify_run(calls)
        call_objs: list[dict] = []
        last_ai_text = ""
        for ci, r in enumerate(calls, 1):
            msgs = r.get("messages") or []
            prev_msgs = calls[ci - 2].get("messages") or [] if ci >= 2 else []
            prev_count = len(prev_msgs)
            new_msgs = msgs[prev_count:] if len(msgs) > prev_count else []

            # 工具往返：新增窗口内 AI(tool_calls) 发起、ToolMessage 按 tool_call_id 配对
            results = {
                m.get("tool_call_id"): m
                for m in new_msgs
                if m.get("role") == "tool"
            }
            tcs = []
            for m in new_msgs:
                if m.get("role") == "ai" and m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        res = results.get(tc.get("id"))
                        tcs.append({
                            "id": tc.get("id"),
                            "name": tc.get("name"),
                            "args": tc.get("args"),
                            "result": _content_text(res.get("content")) if res else None,
                            "resultChars": len(_content_text(res.get("content"))) if res else 0,
                        })
            for m in new_msgs:
                if m.get("role") == "ai" and not m.get("tool_calls"):
                    t = _content_text(m.get("content")).strip()
                    if t:
                        last_ai_text = t
            call_objs.append({
                "i": ci,
                "ts": r.get("ts"),
                "model": r.get("model"),
                "provider": r.get("provider"),
                "runKind": r.get("run_kind"),
                "msgsCount": len(msgs),
                "prevCount": prev_count,
                "newCount": len(new_msgs),
                "toolsBound": len(r.get("tools") or []),
                "params": r.get("params") or {},
                "msgs": [
                    {
                        "role": m.get("role"),
                        "chars": len(_content_text(m.get("content"))),
                        "text": _content_text(m.get("content")),
                        "name": m.get("name"),
                        "toolCallId": m.get("tool_call_id"),
                        "toolCalls": m.get("tool_calls") or [],
                    }
                    for m in msgs
                ],
                "newIdxStart": prev_count,
                "turnToolCalls": tcs,
                "durationMs": r.get("duration_ms"),
                "resp": _resp_summary(r.get("response")),
            })

        tools_used: list[str] = []
        for c in call_objs:
            for tc in c["turnToolCalls"]:
                if tc["name"] and tc["name"] not in tools_used:
                    tools_used.append(tc["name"])
        last_calls_msgs = call_objs[-1]["msgs"] if call_objs else []
        for m in reversed(last_calls_msgs[call_objs[-1]["newIdxStart"]:] if call_objs else []):
            if m["role"] == "ai" and m["text"].strip():
                last_ai_text = m["text"]
                break
        out.append({
            "id": ri,
            "kind": kind,
            "title": title,
            "task": task,
            "lastAi": _one_line(last_ai_text, 70),
            "start": calls[0].get("ts"),
            "end": calls[-1].get("ts"),
            "pids": sorted({c.get("pid") for c in calls if c.get("pid")}),
            "sessions": sorted({c.get("session") for c in calls if c.get("session")}),
            "toolsUsed": tools_used,
            "calls": call_objs,
        })
    return out


def attach_system_events(runs: list[dict], deliveries: list[dict]) -> None:
    """投递归属：同进程（session 命中或 pid 兜底）+ run 结束后 300s 窗口。

    单一主题 = 该 run 自身的投递；多主题爆发 = QQ 反馈触发的批量重发（含旧报），
    记为 systemBatch 摘要而非 run 产出。近重复（同主题同秒）折叠。
    """
    for run in runs:
        end = _parse_ts(run["end"])
        seen: set[tuple[str, str]] = set()
        evs: list[dict] = []
        for e in deliveries:
            if e.get("session"):
                same_proc = e["session"] in run["sessions"] or e["pid"] in run["pids"]
            else:
                same_proc = e["pid"] in run["pids"]
            if not same_proc:
                continue
            ts = _parse_ts(e["ts"])
            if not (end <= ts <= end + timedelta(seconds=SYSTEM_EVENT_WINDOW_SECONDS)):
                continue
            key = (e["subject"], e["ts"][:19])
            if key in seen:
                continue
            seen.add(key)
            evs.append(e)
        subjects = {e["subject"] for e in evs}
        if len(subjects) > 1:
            run["systemEvents"] = []
            run["systemBatch"] = {"ts": min(e["ts"] for e in evs), "count": len(evs)}
        else:
            run["systemEvents"] = sorted(evs, key=lambda x: x["ts"])


def attach_errors(runs: list[dict], errors: list[dict]) -> None:
    """ERROR 事件归属：同进程 + [run.start - 60s, run.end + 300s] 窗口，红色告警行。

    同 module+message 的重复按计数聚合（开发日窗口内可能有几十条同类错误，
    逐条渲染会淹没时间轴；聚合后一眼可见错误种类与频次）。
    """
    for run in runs:
        start = _parse_ts(run["start"]) - timedelta(seconds=60)
        end = _parse_ts(run["end"]) + timedelta(seconds=300)
        seen: set[str] = set()
        evs = []
        for e in errors:
            if e.get("session"):
                same = e["session"] in run["sessions"] or e["pid"] in run["pids"]
            else:
                same = e["pid"] in run["pids"]
            if not same:
                continue
            ts = _parse_ts(e["ts"])
            if not (start <= ts <= end):
                continue
            key = e["module"] + "|" + e["message"][:50]
            if key in seen:
                for existing in evs:
                    if existing["module"] == e["module"] and existing["message"] == e["message"]:
                        existing["count"] += 1
                        existing["lastTs"] = e["ts"]
                        break
                continue
            seen.add(key)
            evs.append({**e, "count": 1, "lastTs": e["ts"]})
        run["errors"] = sorted(evs, key=lambda x: x["ts"])


def attach_output_archives(runs: list[dict], archives: list[dict]) -> None:
    """产出存档归属：文件名时间戳（Job finished）落在 run 结束后 300s 内，取最早匹配。"""
    for run in runs:
        end = _parse_ts(run["end"])
        best: dict | None = None
        for a in archives:
            ts = _parse_ts(a["ts"])
            if end <= ts <= end + timedelta(seconds=ARCHIVE_WINDOW_SECONDS):
                if best is None or ts < _parse_ts(best["ts"]):
                    best = a
        if best is not None:
            run["output"] = best


def run_view(cfg: Any, date: str) -> dict:
    """单一入口：该日期的完整运行视图（llm_requests + app.jsonl + scheduled 存档）。"""
    records = load_llm_requests(cfg, date)
    runs = build_runs(records)
    deliveries, errors = load_deliveries_and_errors(cfg, date)
    attach_system_events(runs, deliveries)
    attach_errors(runs, errors)
    attach_output_archives(runs, load_output_archives(cfg, date))
    return {
        "date": date,
        "runs": runs,
        "counts": {
            "runs": len(runs),
            "calls": sum(len(r["calls"]) for r in runs),
            "toolRounds": sum(len(c["turnToolCalls"]) for r in runs for c in r["calls"]),
        },
    }


__all__ = (
    "classify_run",
    "build_runs",
    "attach_system_events",
    "attach_errors",
    "attach_output_archives",
    "load_llm_requests",
    "load_deliveries_and_errors",
    "load_output_archives",
    "run_view",
)
