"""Web 日报评审服务（C6/S4）：/review 锚点批注页 + /health 信息包源体检。

FastAPI 单文件服务，骨架沿用 langTrack/server.py（工厂函数 create_app + __main__ uvicorn 入口）。
页面全部为模块内嵌 HTML 常量，不引入前端依赖；风格克制（白底、细边框、系统字体）。

端点一览：
- GET  /review/{date}                  评审页：已投递日报全文，[节-序号] 锚点为可点角标
- GET  /health                         源体检总览：最近 14 天 × 源 状态矩阵
- GET  /health/source/{date}/{key}     单源详情：pack_detail 三节 + 字符量对比 + L0 外链
- GET  /logs/{path:path}               白名单静态文件（scheduled/*.md、{date}/llm_requests.jsonl）
- GET  /api/corrections/{date}         当日订正（active + all）
- POST /api/corrections                仅落盘一条订正/偏好（不触发修订）
- POST /api/revise                     默认动作：统一入口 apply_correction 逐条生效
                                       （auto=能原样就原样替换、定位不到自动升级 LLM 最小修订；
                                       verbatim/llm 强制指定）→ 任一正文变更即时重发
- POST /api/rerun                      整体重生成（后台线程 run_job(for_day=date)）
- GET  /api/rerun/{date}/status        重生成任务状态

鉴权（dev-console 惯例）：GET 公开；POST 一律要求请求头 X-Review-Token 等于
REVIEW_TOKEN（load_dotenv 后取；未配置或不匹配 → 401 {"ok": false}）。

/health 读取的信息包体检产物（info_pack_health.jsonl 与 pack_detail/{date}/{key}.md）
由 scheduler 的 daily job 落盘（C1/S1），本模块只读渲染，文件缺失时优雅降级为空态。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from gacore import scheduler
from gacore.config import Config, load_dotenv
from gacore.feedback import (
    apply_correction,
    current_report_version,
    list_active_corrections,
    load_delivered,
    record_correction,
    _corrections_file,
    _read_json_array,
)

_UTC8 = timezone(timedelta(hours=8))

# 与 feedback.stamp_report_bullets 的行锚点约定一致：行首 "- [节-序号] 内容"。
_ANCHOR_LINE_RE = re.compile(r"^(\s*)[-*]\s*\[([^-;\]]+?)-(\d+)\]\s*(.*)$")
# 路径参数校验：日期与源 key 只放行安全字符，防穿越/注入。
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_KEY_RE = re.compile(r"[A-Za-z0-9_\-]+")

_HEALTH_DAYS = 14
# C1/S1 落盘位置（design v0.7）：cfg.root/data/logs 下的元数据行与逐源详情。
_HEALTH_STATUS_CLASS = {
    "ok": "st-ok",
    "empty": "st-empty",
    "failed": "st-failed",
    "missing_data": "st-missing",
}
_FACT_CARD_KEY = "_FACT_CARD"
_FACT_CARD_TITLE = "生活事实卡支线"

# 整体重生成后台任务状态：date -> {state, started_at, email, exit_reason, ...}。
# running 期间同日再启动返回 409（防并发重复投递）。
_RERUN_LOCK = threading.Lock()
_RERUN_STATE: dict[str, dict] = {}


def _health_root(cfg: Config) -> Path:
    """信息包体检产物目录：cfg.root/data/logs（info_pack_health.jsonl 与 pack_detail/）。"""
    return cfg.root / "data" / "logs"


def _now_hhmmss() -> str:
    return datetime.now(_UTC8).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- 鉴权


def _guard(request: Request) -> JSONResponse | None:
    """POST 鉴权闸：X-Review-Token 必须等于 REVIEW_TOKEN（load_dotenv 后取）。

    未配置 REVIEW_TOKEN（空）或不匹配一律 401 {"ok": false}；GET 不经此闸。
    """
    load_dotenv()
    expected = (os.environ.get("REVIEW_TOKEN") or "").strip()
    supplied = (request.headers.get("X-Review-Token") or "").strip()
    if not expected or supplied != expected:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return None


# --------------------------------------------------------------------------- 页面骨架


_CSS = """
:root{--line:#e2e2e0;--ink:#1f2328;--mut:#57606a;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--miss:#8c959f;--acc:#0969da}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
header{border-bottom:1px solid var(--line);padding:10px 0;background:#fff;position:sticky;top:0;z-index:5}
header .wrap{display:flex;align-items:center;gap:16px}
.brand{font-weight:600}
.brand a{color:inherit}
nav a{color:var(--acc);margin-right:10px}
h1{font-size:18px;margin:18px 0 4px}
.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
a{color:var(--acc);text-decoration:none}
button{font:inherit;border:1px solid var(--line);background:#f6f8fa;color:var(--ink);border-radius:6px;padding:5px 14px;cursor:pointer}
button.primary{background:#1f883d;border-color:#1f883d;color:#fff}
button.danger{color:var(--bad)}
button:disabled{opacity:.5;cursor:default}
.empty{color:var(--mut);border:1px dashed var(--line);border-radius:8px;padding:28px;text-align:center;margin:24px 0}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;white-space:nowrap}
tbody th{font-family:ui-monospace,Consolas,monospace;font-weight:500}
.cell{display:inline-block;min-width:38px;text-align:center;border-radius:4px;padding:1px 6px;font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#fff}
.cell-none{background:#f6f8fa;border:1px solid var(--line)}
.st-ok{background:var(--ok)}
.st-empty{background:var(--warn)}
.st-failed{background:var(--bad)}
.st-missing{background:var(--miss)}
.legend{color:var(--mut);font-size:12px;margin:10px 0}
.legend .cell{margin:0 2px}
.report{border:1px solid var(--line);border-radius:8px;padding:8px 20px;margin:10px 0}
.report h2{font-size:16px;border-bottom:1px solid var(--line);padding-bottom:4px}
.report ul{margin:6px 0;padding-left:20px}
.report p{margin:6px 0}
.anchor{color:var(--acc);cursor:pointer;font-size:11px;font-family:ui-monospace,Consolas,monospace;border:1px solid #d0d7de;border-radius:4px;padding:0 4px;margin-right:4px;background:#f6f8fa;white-space:nowrap}
.anchor:hover{background:#ddf4ff}
.actions{display:flex;gap:10px;align-items:center;margin:12px 0}
#pending .chip{display:inline-block;border:1px solid var(--line);border-radius:12px;padding:1px 10px;margin:2px 4px;font-size:12px;background:#f6f8fa}
#pending .chip button{border:none;background:none;padding:0 0 0 6px;color:var(--bad)}
.side{position:fixed;top:0;right:0;width:340px;max-width:92vw;height:100%;background:#fff;border-left:1px solid var(--line);box-shadow:-4px 0 16px rgba(0,0,0,.06);padding:14px 16px;z-index:10;overflow:auto}
.side.hidden{display:none}
.side h3{margin:0 0 10px;font-size:15px}
.side label{display:block;margin:8px 0;font-size:13px;color:var(--mut)}
.side input[type=text],.side select,.side textarea{display:block;width:100%;margin-top:3px;font:inherit;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 8px;background:#fff}
.side textarea[readonly]{background:#f6f8fa}
.side .chk{font-size:12px}
.side .chk input{margin-right:5px}
.dsec{border:1px solid var(--line);border-radius:8px;margin:12px 0;padding:8px 16px}
.dsec.changed{border-color:#d4a72c;background:#fff8c5}
.dsec h3{margin:6px 0;font-size:14px}
.tag{color:var(--warn);font-size:12px;border:1px solid var(--warn);border-radius:4px;padding:0 6px;margin-left:6px}
.dlabel{font-size:12px;color:var(--mut);margin-top:8px}
pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;margin:4px 0;background:#f6f8fa;border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.bar-row{display:flex;align-items:center;gap:8px;margin:6px 0}
.bar-label{width:64px;color:var(--mut);font-size:12px;text-align:right}
.bar-track{flex:1;background:#f6f8fa;border:1px solid var(--line);border-radius:4px;height:14px;overflow:hidden}
.bar-fill{display:block;height:100%;background:var(--acc)}
.bar-num{width:80px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
.links{margin:12px 0}
.links a{display:inline-block;margin:2px 8px 2px 0}
.md-h{font-weight:600;margin:10px 0 4px}
.md-li{margin:2px 0 2px 14px}
.md-t{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.warn{color:var(--warn)}
code{font-family:ui-monospace,Consolas,monospace;background:#f6f8fa;border-radius:4px;padding:0 4px}
"""

_PAGE_TMPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<header><div class="wrap">
<span class="brand"><a href="/health">日报评审</a></span>
<nav><a href="/health">源体检</a></nav>
</div></header>
<main class="wrap">
__BODY__
</main>
</body>
</html>"""


def _page(title: str, body: str) -> str:
    return _PAGE_TMPL.replace("__TITLE__", html.escape(title)).replace("__CSS__", _CSS).replace("__BODY__", body)


# --------------------------------------------------------------------------- /review 评审页


def _extract_reply_section(text: str) -> str:
    """从 scheduled 存档里抽出 "## Reply" 一节（回退路径的降渲染）；无该节时原样返回。"""
    out: list[str] = []
    in_reply = False
    for ln in text.splitlines():
        if ln.strip() == "## Reply":
            in_reply = True
            out = []
            continue
        if in_reply and ln.startswith("## "):
            break
        if in_reply:
            out.append(ln)
    return "\n".join(out).strip("\n") if in_reply else text


def _resolve_report(cfg: Config, date: str) -> tuple[str, str] | None:
    """该日已投递日报全文：(text, 来源标签)。

    首选 load_delivered（logs/delivered_report/{date}.md）；缺失时回退
    logs/scheduled/daily-report_*.md 中内容含该日期的最新一份（取其 Reply 节）；
    再无则 None（页面显示空态）。
    """
    body = load_delivered(cfg, date)
    if body is not None:
        return body, "delivered_report"
    sched = cfg.logs_dir / "scheduled"
    if sched.is_dir():
        for path in sorted(sched.glob("daily-report_*.md"), reverse=True):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if date in content:
                return _extract_reply_section(content), f"scheduled/{path.name}"
    return None


def _render_report_html(text: str) -> str:
    """日报文本 → 克制 HTML：# 标题、- bullet（锚点渲染为可点角标）、其余按段落。"""
    out: list[str] = []
    in_list = False

    def _close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            _close_list()
            continue
        if s.startswith("#"):
            _close_list()
            level = 2 if s.startswith("# ") else 3
            out.append(f"<h{level}>{html.escape(s.lstrip('#').strip())}</h{level}>")
            continue
        m = _ANCHOR_LINE_RE.match(s)
        if m:
            anchor = f"[{m.group(2)}-{m.group(3)}]"
            badge = (
                f'<a class="anchor" data-anchor="{html.escape(anchor)}" title="点此批注">'
                f"{html.escape(anchor)}</a>"
            )
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{badge}{html.escape(m.group(4))}</li>")
            continue
        _close_list()
        out.append(f"<p>{html.escape(s)}</p>")
    _close_list()
    return "\n".join(out)


_REVIEW_TMPL = """<h1>日报评审 · __DATE__</h1>
<div class="sub">来源 __SRC__ · 版本 v__VER__ · 点正文里的 [节-序号] 角标添加批注</div>
<div class="actions">
  <button id="btn-revise" class="primary">提交修订</button>
  <button id="btn-rerun" class="danger">整体重生成</button>
  <span id="pending"></span>
</div>
<div id="rerun-status" class="sub"></div>
<div id="redeliver-status" class="sub"></div>
__REPORT__
<div id="diff"></div>
<aside id="side" class="side hidden">
  <h3>批注 <button id="side-close" style="float:right">×</button></h3>
  <label>锚点<input type="text" id="f-anchor" readonly></label>
  <label>类型<select id="f-kind"><option value="fact">事实订正</option><option value="pref">偏好</option></select></label>
  <label>替换方式<select id="f-mode"><option value="llm">LLM 改写（默认，输入是改写指令）</option><option value="verbatim">原样替换（输入是成品，逐字生效）</option></select></label>
  <label>内容<textarea id="f-text" rows="5" placeholder="改成什么 / 补充什么 / 偏好描述"></textarea></label>
  <label>备注（可选，仅入审计记录）<input type="text" id="f-note" placeholder="为什么改 / 备注"></label>
  <label class="chk"><input type="checkbox" id="f-only">仅落盘不修订（record_correction）</label>
  <div class="actions"><button id="f-add" class="primary">提交</button></div>
</aside>
<script>
const DATE = "__DATE__";
const RAW = __RAW__;
let items = [];
let token = localStorage.getItem("review_token") || "";
const $ = (id) => document.getElementById(id);

function esc(s){const d=document.createElement("div");d.textContent=s==null?"":String(s);return d.innerHTML;}

function needToken(msg){
  const t = prompt(msg || "请输入 REVIEW_TOKEN（见 .env）") || "";
  if (t) { token = t; localStorage.setItem("review_token", t); }
  return t;
}

async function post(url, body){
  if (!token && !needToken()) return null;
  for (let i = 0; i < 2; i++){
    const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json","X-Review-Token":token}, body: JSON.stringify(body)});
    if (r.status !== 401) return r;
    if (!needToken("令牌无效或未授权，请重新输入 REVIEW_TOKEN")) return null;
  }
  return null;
}

function renderPending(){
  $("pending").innerHTML = items.length
    ? "待修订 " + items.length + " 条：" + items.map((it,i) =>
        '<span class="chip">' + esc(it.anchor) + " " + esc(it.kind) + "/" + esc(it.mode || "llm") + " " + esc(it.text.slice(0,12)) +
        ' <button data-i="'+i+'" title="移除">×</button></span>').join("")
    : "";
  $("pending").querySelectorAll("button").forEach(b => b.onclick = () => { items.splice(+b.dataset.i,1); renderPending(); });
}

document.querySelectorAll(".anchor").forEach(el => el.addEventListener("click", () => {
  $("f-anchor").value = el.dataset.anchor;
  $("f-text").value = "";
  $("side").classList.remove("hidden");
  $("f-text").focus();
}));
$("side-close").onclick = () => $("side").classList.add("hidden");

$("f-add").onclick = async () => {
  const item = {anchor: $("f-anchor").value, kind: $("f-kind").value, text: $("f-text").value.trim(),
                mode: $("f-mode").value, note: $("f-note").value.trim()};
  if (!item.text) { alert("请填写批注内容"); return; }
  if ($("f-only").checked) {
    const r = await post("/api/corrections", {date: DATE, ...item});
    if (!r) return;
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) { alert("落盘失败: " + esc(j.error || r.status)); return; }
    alert("已落盘: " + (j.record && j.record.id || ""));
  } else {
    items.push(item);
    renderPending();
  }
  $("side").classList.add("hidden");
};

function splitSections(text){
  const secs = []; let cur = null;
  for (const ln of String(text).split("\\n")) {
    if (ln.startsWith("# ")) { cur = {name: ln.slice(2).trim(), lines: [ln]}; secs.push(cur); }
    else {
      if (!cur) { cur = {name: "preamble", lines: []}; secs.push(cur); }
      cur.lines.push(ln);
    }
  }
  return secs.map(s => ({name: s.name, text: s.lines.join("\\n")}));
}

function renderDiff(rev){
  const a = splitSections(RAW), b = splitSections(rev.text || "");
  let h = '<h1>修订 diff</h1>';
  h += '<div class="sub">变更分节: ' + esc((rev.sections_changed||[]).join("、") || "无") +
       " · diff 门禁 " + (rev.diff_ok ? "通过" : "未通过") + (rev.fallback ? " · 走了降级追加路径" : "") + "</div>";
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++){
    const x = a[i], y = b[i];
    const changed = !x || !y || x.name !== y.name ||
      x.text.replace(/\\s+$/,"") !== (y ? y.text.replace(/\\s+$/,"") : "");
    h += '<section class="dsec' + (changed ? " changed" : "") + '"><h3>' + esc(x ? x.name : (y ? y.name : "?")) +
         (changed ? '<span class="tag">已变更</span>' : "") + '</h3>';
    if (changed){
      if (x) h += '<div class="dlabel">旧</div><pre>' + esc(x.text) + '</pre>';
      if (y) h += '<div class="dlabel">新</div><pre>' + esc(y.text) + '</pre>';
    } else {
      h += '<details><summary style="cursor:pointer;color:var(--mut)">未变更（展开）</summary><pre>' + esc(x.text) + '</pre></details>';
    }
    h += '</section>';
  }
  $("diff").innerHTML = h;
  $("diff").scrollIntoView({behavior: "smooth"});
}

$("btn-revise").onclick = async () => {
  if (!items.length) { alert("清单为空：先点正文里的 [节-序号] 角标添加批注"); return; }
  const btn = $("btn-revise");
  btn.disabled = true; btn.textContent = "修订中…";
  try {
    const r = await post("/api/revise", {date: DATE, items: items});
    if (!r) return;
    const j = await r.json().catch(() => ({}));
    if (r.status !== 200 || j.ok !== true) { alert("修订失败: " + esc((j.revise && j.revise.error) || j.error || r.status)); return; }
    items = []; renderPending();
    renderDiff(j.revise);
    $("redeliver-status").textContent = "重投: " + (j.redeliver ? (j.redeliver.status + (j.redeliver.msg ? " · " + j.redeliver.msg : "")) : "n/a");
  } finally { btn.disabled = false; btn.textContent = "提交修订"; }
};

function pollRerun(){
  const timer = setInterval(async () => {
    const j = await fetch("/api/rerun/" + DATE + "/status").then(r => r.json()).catch(() => null);
    if (!j) return;
    $("rerun-status").textContent = "整体重生成: " + j.state +
      (j.exit_reason ? " · exit=" + esc(j.exit_reason) : "") +
      (j.error ? " · " + esc(j.error) : "") +
      (j.output_path ? " · " + esc(j.output_path) : "");
    if (j.state !== "running") clearInterval(timer);
  }, 2000);
}

$("btn-rerun").onclick = async () => {
  const c = await fetch("/api/corrections/" + DATE).then(r => r.json()).catch(() => null);
  const n = c && c.active ? c.active.length : 0;
  if (!confirm("当前生效订正 " + n + " 条。\\n整体重生成会整版重写日报并发送邮件（主题带重生成版本号），未被点名的内容也可能变化。确定继续？")) return;
  const r = await post("/api/rerun", {date: DATE, email: true});
  if (!r) return;
  const j = await r.json().catch(() => ({}));
  if (r.status === 409) { alert("该日已有重生成任务在运行"); return; }
  if (r.status !== 202 || j.ok !== true) { alert("启动失败: " + esc(j.error || r.status)); return; }
  $("rerun-status").textContent = "整体重生成: running";
  pollRerun();
};
</script>"""


def _review_page(cfg: Config, date: str) -> str:
    resolved = _resolve_report(cfg, date)
    if resolved is None:
        body = (
            f"<h1>日报评审 · {html.escape(date)}</h1>"
            '<p class="empty">暂无数据：该日没有已投递日报存档（delivered_report 与 scheduled 均未命中）</p>'
        )
        return _page(f"日报评审 · {date}", body)
    text, src = resolved
    report_html = _render_report_html(text)
    ver = current_report_version(cfg, date)
    page = (
        _REVIEW_TMPL.replace("__DATE__", date)
        .replace("__SRC__", html.escape(src))
        .replace("__VER__", str(ver))
        .replace("__REPORT__", f'<div class="report">{report_html}</div>')
        .replace("__RAW__", json.dumps(text, ensure_ascii=False))
    )
    return _page(f"日报评审 · {date}", page)


# --------------------------------------------------------------------------- /health 源体检


def _load_health_records(cfg: Config) -> list[dict]:
    """读 info_pack_health.jsonl，容错：缺文件/坏行跳过，返回按文件顺序的记录列表。"""
    path = _health_root(cfg) / "info_pack_health.jsonl"
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("sources"), list):
            out.append(rec)
    return out


def _source_of(rec: dict, key: str) -> dict | None:
    for s in rec.get("sources", []):
        if isinstance(s, dict) and str(s.get("key") or "") == key:
            return s
    return None


def _health_matrix(cfg: Config) -> str:
    recs = _load_health_records(cfg)
    if not recs:
        return '<p class="empty">暂无数据：data/logs/info_pack_health.jsonl 不存在或为空（daily job 运行后生成）</p>'
    by_date: dict[str, dict] = {}
    for rec in recs:
        d = str(rec.get("date") or "")
        if d:
            by_date[d] = rec  # 后行覆盖先行 → 同日取最新
    dates = sorted(by_date, reverse=True)[:_HEALTH_DAYS]
    if not dates:
        return '<p class="empty">暂无数据：jsonl 内没有可解析的 date 字段</p>'
    newest = by_date[dates[0]]
    rows: list[str] = []
    for s in newest.get("sources", []):
        key = str(s.get("key") or "")
        if key and key not in rows:
            rows.append(key)
    for d in dates[1:]:  # 近期消失的源排在末尾，不丢信息
        for s in by_date[d].get("sources", []):
            key = str(s.get("key") or "")
            if key and key not in rows:
                rows.append(key)

    head = "<tr><th>源</th>" + "".join(f"<th>{html.escape(d)}</th>" for d in dates) + "</tr>"
    body_rows: list[str] = []
    for key in rows:
        cells: list[str] = []
        for d in dates:
            s = _source_of(by_date[d], key)
            if s is None:
                cells.append('<td><span class="cell cell-none" title="该日无记录">-</span></td>')
                continue
            status = str(s.get("status") or "missing_data")
            cls = _HEALTH_STATUS_CLASS.get(status, "st-missing")
            chars = s.get("chars")
            note = str(s.get("note") or "")
            tip = html.escape(f"{status}" + (f" · {note}" if note else ""))
            label = html.escape(str(chars) if isinstance(chars, (int, float)) else status)
            cells.append(
                f'<td><a class="cell {cls}" title="{tip}" href="/health/source/{d}/{html.escape(key)}">{label}</a></td>'
            )
        body_rows.append(f"<tr><th>{html.escape(key)}</th>{''.join(cells)}</tr>")

    body = (
        "<h1>信息包源体检</h1>"
        f'<div class="sub">最近 {len(dates)} 天 × 源 状态矩阵 · 点单元格进单源详情 · 行序按最新一天</div>'
        '<div class="legend">图例：<span class="cell st-ok">ok</span><span class="cell st-empty">empty</span>'
        '<span class="cell st-failed">failed</span><span class="cell st-missing">missing_data</span>'
        "（单元格文字为 chars）</div>"
        f'<table><thead>{head}</thead><tbody>{"".join(body_rows)}</tbody></table>'
    )
    return _page("信息包源体检", body)


def _split_h2(text: str) -> list[tuple[str, str]]:
    """按 "## " 标题把 md 文本切成 (节标题, 节体)；标题前的散段归 ""。"""
    sections: list[tuple[str, list[str]]] = []
    for ln in (text or "").splitlines():
        if ln.startswith("## "):
            sections.append((ln[3:].strip(), []))
        elif not sections:
            sections.append(("", [ln]))
        else:
            sections[-1][1].append(ln)
    return [(name, "\n".join(lines)) for name, lines in sections]


def _mini_md_html(text: str) -> str:
    """极简 md 渲染：标题行加粗、fenced code 成 pre、列表行加缩进、正文等宽。"""
    out: list[str] = []
    fence = False
    for ln in (text or "").splitlines():
        if ln.strip().startswith("```"):
            out.append('<pre class="code">' if not fence else "</pre>")
            fence = not fence
            continue
        if fence:
            out.append(html.escape(ln))
            continue
        s = ln.strip()
        if s.startswith("#"):
            out.append(f'<div class="md-h">{html.escape(s.lstrip("#").strip())}</div>')
        elif s.startswith("- "):
            out.append(f'<div class="md-li">• {html.escape(s[2:])}</div>')
        elif not s:
            out.append('<div class="md-t">&nbsp;</div>')
        else:
            out.append(f'<div class="md-t">{html.escape(ln)}</div>')
    return "\n".join(out)


def _chars_bars(entry: dict | None) -> str:
    """三级字符对比条（完整取数 vs 渲染文本 vs 实际进包）；条目缺失时不渲染数字。"""
    if not isinstance(entry, dict):
        return ""
    labeled: list[tuple[str, int]] = []
    for label, field in (("完整取数", "detail_chars"), ("渲染文本", "full_chars"), ("实际进包", "chars")):
        v = entry.get(field)
        if isinstance(v, (int, float)) and v >= 0:
            labeled.append((label, int(v)))
    if not labeled:
        return ""
    mx = max(v for _, v in labeled) or 1
    rows = "".join(
        f'<div class="bar-row"><span class="bar-label">{html.escape(label)}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{int(v * 100 / mx)}%"></span></span>'
        f"<span class=\"bar-num\">{v}</span></div>"
        for label, v in labeled
    )
    return f'<section class="dsec"><h3>字符量对比（逐层）</h3>{rows}</section>'


def _health_source_page(cfg: Config, date: str, key: str) -> str:
    title = _FACT_CARD_TITLE if key == _FACT_CARD_KEY else key
    md_path = _health_root(cfg) / "pack_detail" / date / f"{key}.md"
    if md_path.is_file():
        try:
            md_text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            md_text = ""
    else:
        md_text = ""
    if md_text.strip():
        parts = [
            f'<section class="dsec"><h3>{html.escape(name or "（引言）")}</h3>{_mini_md_html(body)}</section>'
            for name, body in _split_h2(md_text)
        ]
        detail_html = "".join(parts)
    else:
        detail_html = (
            f'<p class="empty">暂无数据：data/logs/pack_detail/{html.escape(date)}/{html.escape(key)}.md 不存在'
            "（daily job 运行该日后生成）</p>"
        )

    recs = [r for r in _load_health_records(cfg) if str(r.get("date") or "") == date]
    entry = _source_of(recs[-1], key) if recs else None

    links = [f'<a href="/review/{html.escape(date)}">查看最终 LLM 输入：{html.escape(date)} scheduled 存档（评审页）</a>']
    llm_req = cfg.logs_dir / date / "llm_requests.jsonl"
    if llm_req.is_file():
        links.append(f'<a href="/logs/{html.escape(date)}/llm_requests.jsonl">查看最终 LLM 输入：{html.escape(date)}/llm_requests.jsonl</a>')

    body = (
        f"<h1>源体检 · {html.escape(title)}</h1>"
        f'<div class="sub">{html.escape(date)} · key <code>{html.escape(key)}</code></div>'
        f'<div class="links">{" ".join(links)}</div>'
        f"{_chars_bars(entry)}"
        f"{detail_html}"
    )
    return _page(f"源体检 · {title} · {date}", body)


# --------------------------------------------------------------------------- FastAPI app


class CorrectionIn(BaseModel):
    date: str
    anchor: str = ""
    kind: str = "fact"
    text: str
    mode: str = ""
    note: str = ""


class ReviseItem(BaseModel):
    anchor: str
    kind: str = "fact"
    text: str
    mode: str = "llm"    # llm=语义改写（默认）；verbatim=原样替换（订正词逐字生效）
    note: str = ""


class ReviseIn(BaseModel):
    date: str
    items: list[ReviseItem]


def _deliver_revised(cfg: Config, date: str) -> dict:
    """修订后邮件重发：与 redeliver_day 同通道（scheduler._deliver → _deliver_email），
    但不走它的 applied-pending 守卫——评审页批注落的是 corrections，不是 pending 草稿，
    该守卫永远 noop。版本号由 _deliver_email 的 C5 逻辑按历史天递增。失败仅返回
    error dict，绝不抛出（投递失败不影响修订已落存档的事实）。"""
    try:
        job = next((j for j in scheduler.load_jobs(cfg) if j.name == "daily-report"), None)
        if job is None:
            return {"status": "error", "msg": "daily-report job 不存在"}
        body = load_delivered(cfg, date)
        if body is None:
            return {"status": "error", "msg": "no delivered report"}
        scheduler._deliver(job, cfg, body, None, for_day=date)
        return {"status": "ok", "date": date}
    except Exception as exc:  # noqa: BLE001 — 重发失败不抛出
        return {"status": "error", "msg": f"{type(exc).__name__}: {exc}"[:200]}


class RerunIn(BaseModel):
    date: str
    email: bool = True


def _run_rerun_task(cfg: Config, date: str, job: scheduler.Job, email: bool) -> None:
    """后台线程体：run_job 并把结果写回 _RERUN_STATE（任何异常都落为 error 态）。"""
    t0 = time.monotonic()
    update: dict = {}
    try:
        result = scheduler.run_job(job, cfg, for_day=date, deliver=email)
        update.update(
            state="done",
            exit_reason=result.exit_reason,
            output_path=result.output_path,
            error=result.error,
        )
    except Exception as exc:  # noqa: BLE001 — 后台线程兜底，状态可查
        update.update(state="error", error=f"{type(exc).__name__}: {exc}"[:200])
    # 耗时/结束时刻在任务收尾时取值——启动时算会把 duration 固定为 0
    update.update(
        duration_seconds=round(time.monotonic() - t0, 1),
        finished_at=_now_hhmmss(),
    )
    with _RERUN_LOCK:
        _RERUN_STATE[date] = {**_RERUN_STATE.get(date, {}), **update}


def create_app(cfg: Config | None = None) -> FastAPI:
    """工厂：cfg 为 None 时用 Config.default()（供 uvicorn 入口与模块级 app）。"""
    config = cfg or Config.default()
    app = FastAPI(title="daily report review")

    # ---- 页面（GET 公开） ----

    @app.get("/review")
    def review_latest() -> Response:
        """/review 无日期 → 跳最近一篇已投递日报（dev-console「打开」按钮的落地页）。"""
        latest = ""
        delivered = config.logs_dir / "delivered_report"
        if delivered.is_dir():
            dated = sorted(p.stem for p in delivered.glob("*.md") if _DATE_RE.fullmatch(p.stem))
            latest = dated[-1] if dated else ""
        if not latest:
            from datetime import datetime as _dt

            latest = _dt.now().astimezone().date().isoformat()
        return RedirectResponse(f"/review/{latest}", status_code=307)

    @app.get("/review/{date}", response_class=HTMLResponse)
    def review_page(date: str) -> Response:
        if not _DATE_RE.fullmatch(date):
            return JSONResponse({"ok": False, "error": "invalid date"}, status_code=400)
        return HTMLResponse(_review_page(config, date))

    @app.get("/health", response_class=HTMLResponse)
    def health_page() -> Response:
        return HTMLResponse(_health_matrix(config))

    @app.get("/health/source/{date}/{key}", response_class=HTMLResponse)
    def health_source_page(date: str, key: str) -> Response:
        if not _DATE_RE.fullmatch(date) or not _KEY_RE.fullmatch(key):
            return JSONResponse({"ok": False, "error": "invalid path param"}, status_code=400)
        return HTMLResponse(_health_source_page(config, date, key))

    @app.get("/logs/{path:path}")
    def logs_file(path: str) -> Response:
        """白名单静态文件：仅 scheduled/*.md 与 {date}/llm_requests.jsonl，防目录穿越。"""
        p = PurePosixPath(path)
        parts = p.parts
        if p.is_absolute() or ".." in parts or len(parts) != 2:
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        head, tail = parts
        allowed = (head == "scheduled" and tail.endswith(".md")) or (
            tail == "llm_requests.jsonl" and _DATE_RE.fullmatch(head) is not None
        )
        if not allowed:
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        full = config.logs_dir / head / tail
        if not full.is_file():
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        media = "text/markdown; charset=utf-8" if tail.endswith(".md") else "application/x-ndjson; charset=utf-8"
        return FileResponse(full, media_type=media)

    # ---- API（POST 需 token） ----

    @app.get("/api/corrections/{date}")
    def api_corrections(date: str) -> dict:
        active = list_active_corrections(config, date)
        all_records = _read_json_array(_corrections_file(config, date))
        return {"ok": True, "active": active, "all": all_records}

    @app.post("/api/corrections")
    def api_record_correction(request: Request, payload: CorrectionIn):
        guard = _guard(request)
        if guard is not None:
            return guard
        rec = record_correction(config, payload.date, payload.anchor, payload.kind, payload.text,
                                mode=payload.mode, note=payload.note)
        return {"ok": True, "record": rec}

    @app.post("/api/revise")
    def api_revise(request: Request, payload: ReviseIn):
        guard = _guard(request)
        if guard is not None:
            return guard
        # 统一入口 apply_correction：与 QQ 确认同语义（auto=能原样就原样、定位不到升级
        # LLM 最小修订；verbatim/llm 强制指定）。pref 只落偏好库不改正文。
        results = [
            apply_correction(config, payload.date, it.anchor, it.kind, it.text,
                             mode=it.mode, note=it.note)
            for it in payload.items
        ]
        changed = [r for r in results if r["status"] == "ok" and r["mode_used"] in ("verbatim", "llm")]
        rd: dict = {"status": "skipped"}
        if changed:
            rd = _deliver_revised(config, payload.date)
        # revise 聚合字段保持前端 diff 视图的既有形状（text=最新存档全文）
        revise = {
            "ok": all(r["status"] == "ok" for r in results),
            "error": next((r["error"] for r in results if r["status"] != "ok"), ""),
            "text": load_delivered(config, payload.date) or "" if changed else "",
            "sections_changed": [r["anchor"] for r in changed],
            "diff_ok": bool(changed),
            "fallback": any((r.get("revise") or {}).get("fallback", False) for r in results),
        }
        return {"ok": revise["ok"], "results": results, "revise": revise, "redeliver": rd}

    @app.post("/api/rerun")
    def api_rerun(request: Request, payload: RerunIn):
        guard = _guard(request)
        if guard is not None:
            return guard
        if not _DATE_RE.fullmatch(payload.date):
            return JSONResponse({"ok": False, "error": "invalid date"}, status_code=400)
        jobs = scheduler.load_jobs(config)
        job = next((j for j in jobs if j.name == "daily-report"), None)
        if job is None:
            return JSONResponse({"ok": False, "error": "job_not_found"}, status_code=404)
        with _RERUN_LOCK:
            cur = _RERUN_STATE.get(payload.date)
            if cur and cur.get("state") == "running":
                return JSONResponse({"ok": False, "error": "already_running"}, status_code=409)
            _RERUN_STATE[payload.date] = {
                "state": "running",
                "started_at": _now_hhmmss(),
                "email": payload.email,
                "exit_reason": None,
                "output_path": None,
                "error": None,
            }
        threading.Thread(
            target=_run_rerun_task, args=(config, payload.date, job, payload.email), daemon=True, name="review-rerun"
        ).start()
        return JSONResponse({"ok": True, "status": "started", "state": _RERUN_STATE[payload.date]}, status_code=202)

    @app.get("/api/rerun/{date}/status")
    def api_rerun_status(date: str) -> dict:
        with _RERUN_LOCK:
            state = dict(_RERUN_STATE.get(date) or {})
        return {"ok": True, "date": date, **(state or {"state": "idle"})}

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="daily report review server (C6)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    load_dotenv()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
