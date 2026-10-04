# -*- coding: utf-8 -*-
"""Generate 3 self-contained HTML demos that visualize LLM runs from llm_requests.jsonl.

Usage (cwd = apps/withlanggraph):
    uv run python docs/llm-view-demos/build_demos.py [--date 2026-10-03]

Reads logs/<date>/llm_requests.jsonl, reconstructs runs (session + >10min gap),
and writes demo_a_timeline.html / demo_b_table.html / demo_c_inspector.html
next to this script, each with the day's data embedded (no server needed).

Data caveats shown honestly in the demos: no latency/token/cost recorded;
run boundary is a heuristic (session id + 10min gap); tool results live in the
NEXT call's message list (matched by tool_call_id).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # apps/withlanggraph
RUN_GAP_SECONDS = 600


def load_day(date: str) -> list[dict]:
    path = ROOT / "logs" / date / "llm_requests.jsonl"
    if not path.is_file():
        sys.exit(f"not found: {path}")
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    records.sort(key=lambda r: r.get("ts") or "")
    return records


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            parts.append(b if isinstance(b, str) else json.dumps(b, ensure_ascii=False))
        return "\n".join(parts)
    return str(content or "")


def _one_line(s: str, n: int) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def classify_run(calls: list[dict]) -> tuple[str, str, str]:
    """(kind, badge title, task excerpt) — pure heuristics over the first request."""
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
    runs: list[dict] = []
    cur: list[dict] = []
    prev = None
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

    out = []
    for ri, calls in enumerate(runs, 1):
        call_objs = []
        for ci, r in enumerate(calls, 1):
            msgs = r.get("messages") or []
            prev_msgs = calls[ci - 2].get("messages") or [] if ci >= 2 else []
            prev_count = len(prev_msgs)
            new_msgs = msgs[prev_count:] if len(msgs) > prev_count else []

            # 工具往返 = 本请求"新增窗口"内的 AI(tool_calls) 与 ToolMessage 配对：
            # 上一次调用的模型响应发起工具调用，工具结果随后进入本请求的消息列表
            new_msgs = msgs[prev_count:] if len(msgs) > prev_count else []
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
            })
        tools_used: list[str] = []
        last_ai_text = ""
        for c in call_objs:
            for tc in c["turnToolCalls"]:
                if tc["name"] and tc["name"] not in tools_used:
                    tools_used.append(tc["name"])
        # 产出线索：最后一次请求新增窗口里的末条 AI 文本（真正的最终回复不落本日志，
        # 已在 logs/scheduled 存档——此处仅为侧栏摘要线索）
        last_calls_msgs = call_objs[-1]["msgs"] if call_objs else []
        for m in reversed(last_calls_msgs[call_objs[-1]["newIdxStart"]:] if call_objs else []):
            if m["role"] == "ai" and m["text"].strip():
                last_ai_text = m["text"]
                break
        kind, title, task = classify_run(calls)
        out.append({
            "id": ri,
            "kind": kind,
            "title": title,
            "task": task,
            "lastAi": _one_line(last_ai_text, 70),
            "start": calls[0].get("ts"),
            "end": calls[-1].get("ts"),
            "toolsUsed": tools_used,
            "calls": call_objs,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-03")
    args = ap.parse_args()

    records = load_day(args.date)
    if not records:
        sys.exit("no parseable records")
    data = {
        "date": args.date,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "source": f"logs/{args.date}/llm_requests.jsonl",
        "runs": build_runs(records),
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    out_dir = Path(__file__).resolve().parent
    tpl_a_file = out_dir / "template_a.html"
    for name, tpl in TEMPLATES.items():
        if name == "demo_a_timeline.html" and tpl_a_file.is_file():
            tpl = tpl_a_file.read_text(encoding="utf-8")
        html = tpl.replace("__DATA__", blob).replace("__DATE__", args.date)
        path = out_dir / name
        path.write_text(html, encoding="utf-8")
        print(f"wrote {path} ({path.stat().st_size // 1024}KB)")
    return 0


# ---------------------------------------------------------------------------
# shared CSS + JS helpers, then three templates
# ---------------------------------------------------------------------------
_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f6f8;color:#2D3142;font-size:13px}
header{position:sticky;top:0;z-index:9;background:#fff;padding:10px 16px;box-shadow:0 1px 3px rgba(0,0,0,.12);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px}
header .meta{color:#888;font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;margin-right:4px}
.role-system{background:#EDE7F6;color:#4527A0}.role-human{background:#E3F2FD;color:#1565C0}
.role-ai{background:#E8F5E9;color:#2E7D32}.role-tool{background:#FFF3E0;color:#E65100}
.chip{display:inline-block;background:#EEF1F5;border-radius:4px;padding:1px 6px;font-size:11px;margin:1px 2px;color:#455A64}
pre{background:#263238;color:#ECEFF1;padding:10px;border-radius:6px;overflow:auto;max-height:380px;font-family:Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-all}
.exp{cursor:pointer}.exp:hover{background:#ECEFF1}
button{border:1px solid #ccc;background:#fff;border-radius:6px;padding:3px 10px;cursor:pointer;font-size:12px}
button:hover{background:#eef1f5}
.muted{color:#999}
"""

_JS_HELPERS = """
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function fmtC(n){return n>=1000?(n/1000).toFixed(1)+"k":n}
function pretty(o){try{return JSON.stringify(o,null,2)}catch(e){return String(o)}}
function hh(ts){return (ts||"").replace("T"," ").slice(5,19)}
function badge(role){return '<span class="badge role-'+role+'">'+role+'</span>'}
"""

_TPL_TIMELINE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>Demo A 运行回放 · __DATE__</title>
<style>""" + _CSS + """
#wrap{display:flex;height:calc(100vh - 44px)}
#side{width:250px;background:#fff;border-right:1px solid #e3e5e8;overflow:auto;padding:8px}
.run{padding:8px;border-radius:8px;cursor:pointer;margin-bottom:6px;border:1px solid transparent}
.run:hover{background:#F4F6F9}.run.on{background:#E3F2FD;border-color:#90CAF9}
.run .t{font-weight:600}.run .s{font-size:11px;color:#888}
#main{flex:1;overflow:auto;padding:16px}
.call{background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:14px;overflow:hidden}
.call>.hd{padding:8px 14px;border-bottom:1px solid #eef0f3;display:flex;gap:8px;align-items:center;flex-wrap:wrap;cursor:pointer}
.call>.bd{padding:10px 14px;display:none}.call.open>.bd{display:block}
.tcard{border:1px solid #FFE0B2;border-radius:8px;margin:8px 0;overflow:hidden}
.tcard>.th{background:#FFF8E1;padding:6px 10px;font-weight:600;cursor:pointer;display:flex;gap:8px;align-items:center}
.tcard>.tb{display:none;padding:8px 10px}.tcard.open>.tb{display:block}
.msgline{padding:4px 6px;border-radius:6px;margin:2px 0;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#drawer{position:fixed;right:0;top:0;width:52%;height:100%;background:#fff;box-shadow:-4px 0 16px rgba(0,0,0,.18);display:none;flex-direction:column;z-index:20}
#drawer .dh{padding:10px 14px;border-bottom:1px solid #eee;display:flex;justify-content:space-between}
#drawer .db{flex:1;overflow:auto;padding:14px}
</style></head><body>
<header><h1>Demo A · 运行回放（trace 时间轴）</h1><span class="meta">__DATE__ · 数据源 logs/__DATE__/llm_requests.jsonl · 无耗时/token 记录（jsonl 仅记请求时刻）</span></header>
<div id="wrap"><div id="side"></div><div id="main"></div></div>
<div id="drawer"><div class="dh"><b id="dt"></b><button onclick="document.getElementById('drawer').style.display='none'">关闭</button></div><div class="db" id="db"></div></div>
<script>const DATA=__DATA__;
""" + _JS_HELPERS + """
function drawer(t, html){document.getElementById('dt').textContent=t;document.getElementById('db').innerHTML=html;document.getElementById('drawer').style.display='flex'}
function msgHtml(m,i){return '<div class="msgline exp" onclick="showMsg('+i+')">'+badge(m.role)+(m.name?' <span class=chip>'+esc(m.name)+'</span>':'')+(m.toolCallId?' <span class=chip>id:'+esc(String(m.toolCallId).slice(0,10))+'</span>':'')+' <span class=muted>'+fmtC(m.chars)+'字</span> '+esc(m.text.slice(0,150))+'</div>'}
function showMsg(i){const c=CUR.calls[CALLIDX];const m=c.msgs[i];drawer(c.i+'#'+(i+1)+' · '+m.role+' · '+fmtC(m.chars)+'字','<pre>'+esc(m.text)+'</pre>'+(m.toolCalls&&m.toolCalls.length?'<h4>tool_calls</h4><pre>'+esc(pretty(m.toolCalls))+'</pre>':''))}
let CUR=null,CALLIDX=0;
function renderMain(run){CUR=run;const el=document.getElementById('main');let h='';
run.calls.forEach(function(c){CALLIDX=c.i-1;
h+='<div class="call" id="call'+c.i+'"><div class="hd" onclick="this.parentNode.classList.toggle(\\'open\\')"><b>调用 #'+c.i+'</b><span class=chip>'+hh(c.ts)+'</span><span class=chip>'+esc(c.provider)+'/'+esc(c.model)+'</span><span class=chip>'+(c.msgsCount?'消息 '+c.msgsCount+' 条':'无消息列表输入')+'</span><span class=chip>新增 '+c.newCount+'</span><span class=chip>绑定工具 '+c.toolsBound+'</span>'+(c.runKind?'<span class=chip>'+esc(c.runKind)+'</span>':'')+'</div><div class="bd">';
if(c.newIdxStart>0)h+='<div class=muted>前文 '+(c.newIdxStart)+' 条消息与上次调用相同（点击消息行看全文）</div>';
h+=c.msgs.slice(Math.min(c.newIdxStart,c.msgs.length)).map(function(m,j){return msgHtml(m,c.newIdxStart+j)}).join('');
if(c.turnToolCalls.length){h+='<h4 style="margin:8px 0 2px">本轮工具调用 '+c.turnToolCalls.length+' 次（模型响应发起 → 工具结果随本请求到达）</h4>';
c.turnToolCalls.forEach(function(tc,j){h+='<div class="tcard" id="tc'+c.i+'_'+j+'"><div class="th" onclick="this.parentNode.classList.toggle(\\'open\\')">🔧 '+esc(tc.name)+' <span class=muted>'+(tc.result?tc.resultChars+'字结果':'（无结果记录）')+'</span></div><div class="tb"><div><b>args</b></div><pre>'+esc(pretty(tc.args))+'</pre><div><b>result</b></div><pre>'+esc(tc.result==null?"（无）":tc.result)+'</pre></div></div>'});}
else h+='<div class=muted style="margin-top:6px">本轮无工具调用</div>';
h+='</div></div>';});
el.innerHTML='<div class=muted style="margin-bottom:10px">运行 '+run.id+' · '+hh(run.start)+' → '+hh(run.end)+' · '+run.calls.length+' 次调用 · 按时间序，展开看增量消息与工具往返</div>'+h;}
function renderSide(){document.getElementById('side').innerHTML=DATA.runs.map(function(r){return '<div class=run id="run'+r.id+'" onclick="pick('+r.id+')"><div class=t>运行 '+r.id+' · '+hh(r.start).slice(0,11)+'</div><div class=s>'+r.calls.length+' 次调用 · '+(function(a){const s={};a.forEach(function(x){s[x]=1});return Object.keys(s).join(', ')||'无'})(r.calls.flatMap(function(c){return c.turnToolCalls.map(function(t){return t.name})}))+'</div></div>'}).join('')}
function pick(id){document.querySelectorAll('.run').forEach(function(e){e.classList.remove('on')});document.getElementById('run'+id).classList.add('on');renderMain(DATA.runs[id-1]);document.getElementById('main').scrollTop=0}
renderSide();if(DATA.runs.length)pick(DATA.runs[DATA.runs.length-1].id);
</script></body></html>"""

_TPL_TABLE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>Demo B 全天检索工作台 · __DATE__</title>
<style>""" + _CSS + """
#bar{padding:8px 16px;background:#fff;border-bottom:1px solid #e3e5e8;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
#bar input,#bar select{border:1px solid #ccc;border-radius:6px;padding:4px 8px;font-size:12px}
#bar input{width:260px}
table{width:100%;border-collapse:collapse;background:#fff}
th{position:sticky;top:0;background:#F4F6F9;text-align:left;padding:6px 10px;font-size:12px;border-bottom:2px solid #e3e5e8}
td{padding:5px 10px;border-bottom:1px solid #f0f2f5;vertical-align:top}
tr.callrow{cursor:pointer}tr.callrow:hover{background:#F4F9FF}
#drawer{position:fixed;right:0;top:0;width:55%;height:100%;background:#fff;box-shadow:-4px 0 16px rgba(0,0,0,.18);display:none;flex-direction:column;z-index:20}
#drawer .dh{padding:10px 14px;border-bottom:1px solid #eee;display:flex;justify-content:space-between;gap:8px;align-items:center}
#drawer .db{flex:1;overflow:auto;padding:14px}
.mline{padding:4px 6px;border-radius:6px;margin:2px 0}
</style></head><body>
<header><h1>Demo B · 全天检索工作台（维护主力）</h1><span class="meta">__DATE__ · 全部调用一屏过滤：全文搜索 / 按工具 / 按 run / 只看有工具调用</span></header>
<div id="bar">
<input id="q" placeholder="全文搜索消息内容…" oninput="render()">
<select id="ftool" onchange="render()"></select>
<select id="frun" onchange="render()"></select>
<label><input type="checkbox" id="ftc" onchange="render()"> 只看有工具调用的</label>
<span class="meta" id="cnt"></span>
</div>
<div style="overflow:auto;height:calc(100vh - 92px)"><table><thead><tr>
<th>时间</th><th>run/轮</th><th>model</th><th>消息</th><th>新增</th><th>本轮工具调用</th><th>绑定工具数</th><th>总字符</th></tr></thead><tbody id="tb"></tbody></table></div>
<div id="drawer"><div class="dh"><b id="dt"></b><span><button onclick="copyJSON()">复制 JSON</button> <button onclick="document.getElementById('drawer').style.display='none'">关闭</button></span></div><div class="db" id="db"></div></div>
<script>const DATA=__DATA__;
""" + _JS_HELPERS + """
const CALLS=[];DATA.runs.forEach(function(r){r.calls.forEach(function(c){c.run=r.id;c.search=c.msgs.map(function(m){return m.text}).join("\\n");c.totalChars=c.msgs.reduce(function(a,m){return a+m.chars},0);CALLS.push(c)})});
let CUR=null;
function drawerHtml(c){let h='<div>'+badge('system')+'系统 <span class=muted>'+fmtC(c.msgs[0]?c.msgs[0].chars:0)+'字</span></div>';
h+=c.msgs.slice(1).map(function(m,i){return '<div class="mline exp" onclick="tg(this)">'+badge(m.role)+(m.name?' <span class=chip>'+esc(m.name)+'</span>':'')+(m.toolCallId?' <span class=chip>tool_id:'+esc(String(m.toolCallId).slice(0,12))+'</span>':'')+' <span class=muted>'+fmtC(m.chars)+'字</span><div class=full style="display:none;margin-top:4px"><pre>'+esc(m.text)+'</pre>'+(m.toolCalls.length?'<pre>'+esc(pretty(m.toolCalls))+'</pre>':'')+'</div></div>'}).join('');
if(c.turnToolCalls.length)h+='<h4 style="margin:10px 0 4px">本轮工具调用</h4>'+c.turnToolCalls.map(function(tc){return '<div style="border:1px solid #FFE0B2;border-radius:8px;margin:6px 0;padding:8px"><b>🔧 '+esc(tc.name)+'</b><div><b>args</b></div><pre>'+esc(pretty(tc.args))+'</pre><div><b>result ('+(tc.result?tc.resultChars:0)+'字)</b></div><pre>'+esc(tc.result==null?"（无结果记录）":tc.result)+'</pre></div>'}).join('');
h+='<h4 style="margin:10px 0 4px">params</h4><pre>'+esc(pretty(c.params))+'</pre>';return h}
function tg(e){const f=e.querySelector('.full');f.style.display=f.style.display==='none'?'block':'none'}
function show(c){CUR=c;document.getElementById('dt').textContent='run '+c.run+' 调用 #'+c.i+' · '+hh(c.ts)+' · '+c.provider+'/'+c.model;document.getElementById('db').innerHTML=drawerHtml(c);document.getElementById('drawer').style.display='flex'}
function copyJSON(){if(CUR){const t=JSON.stringify(CUR,null,2);navigator.clipboard.writeText(t).then(function(){alert('已复制')},function(){})}}
function render(){const q=document.getElementById('q').value.toLowerCase();const ft=document.getElementById('ftool').value;const fr=document.getElementById('frun').value;const fc=document.getElementById('ftc').checked;
const rows=CALLS.filter(function(c){if(fr!=='*'&&String(c.run)!==fr)return false;if(fc&&!c.turnToolCalls.length)return false;if(ft!=='*'&&!c.turnToolCalls.some(function(t){return t.name===ft}))return false;if(q&&c.search.toLowerCase().indexOf(q)<0)return false;return true});
document.getElementById('cnt').textContent=rows.length+' / '+CALLS.length+' 次调用';
document.getElementById('tb').innerHTML=rows.map(function(c){return '<tr class=callrow onclick="show(CALLS['+(CALLS.indexOf(c))+'])"><td>'+hh(c.ts)+'</td><td>run'+c.run+' #'+c.i+'</td><td>'+esc(c.model)+'</td><td>'+c.msgsCount+'</td><td>'+c.newCount+'</td><td>'+(c.turnToolCalls.length?c.turnToolCalls.map(function(t){return '<span class=chip>'+esc(t.name)+'</span>'}).join(''):'<span class=muted>-</span>')+'</td><td>'+c.toolsBound+'</td><td>'+fmtC(c.totalChars)+'</td></tr>'}).join('')}
(function(){const tools={};CALLS.forEach(function(c){c.turnToolCalls.forEach(function(t){tools[t.name]=1})});const ts=Object.keys(tools).sort();
document.getElementById('ftool').innerHTML='<option value="*">全部工具</option>'+ts.map(function(t){return '<option>'+esc(t)+'</option>'}).join('');
document.getElementById('frun').innerHTML='<option value="*">全部 run</option>'+DATA.runs.map(function(r){return '<option value="'+r.id+'">run '+r.id+'（'+hh(r.start).slice(5,11)+'，'+r.calls.length+'次）</option>'}).join('');
render();})();
</script></body></html>"""

_TPL_INSPECTOR = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>Demo C payload 检查器 · __DATE__</title>
<style>""" + _CSS + """
#top{padding:8px 16px;background:#fff;border-bottom:1px solid #e3e5e8;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
#top select{border:1px solid #ccc;border-radius:6px;padding:4px 8px;font-size:12px;min-width:340px}
#wrap{display:flex;height:calc(100vh - 92px)}
#left{flex:1.1;overflow:auto;padding:12px 14px}
#right{flex:1;border-left:1px solid #e3e5e8;overflow:auto;padding:12px 14px;background:#FBFCFD}
.bubble{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-bottom:8px;overflow:hidden;border-left:4px solid #ccc}
.bubble.system{border-left-color:#8E24AA}.bubble.human{border-left-color:#1E88E5}.bubble.ai{border-left-color:#43A047}.bubble.tool{border-left-color:#F9A825}
.bubble>.bh{padding:6px 10px;cursor:pointer;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.bubble>.bb{display:none;padding:0 10px 10px}.bubble.open>.bb{display:block}
.bubble.new>.bh{background:#E8F5E9}
.bubble.new::after{content:"NEW";font-size:10px;color:#2E7D32;margin-left:auto;font-weight:700}
h3.sec{margin:12px 0 6px;font-size:13px}
</style></head><body>
<header><h1>Demo C · payload 检查器（prompt 调试）</h1><span class="meta">__DATE__ · 左=对话还原（绿色 NEW=相对上次调用新增）· 右=raw JSON</span></header>
<div id="top">
<select id="sel" onchange="pick(this.value)"></select>
<label><input type="checkbox" id="onlynew" onchange="render()"> 只看新增消息</label>
<label><input type="checkbox" id="autonew" onchange="render()"> 新消息自动展开</label>
<button onclick="copyJSON()">复制该调用 JSON</button>
</div>
<div id="wrap"><div id="left"></div><div id="right"><h3 class="sec">raw JSON</h3><pre id="raw"></pre></div></div>
<script>const DATA=__DATA__;
""" + _JS_HELPERS + """
let CUR=null;
function pick(v){const p=v.split(':');CUR=DATA.runs[p[0]-1].calls[p[1]-1];document.getElementById('raw').textContent=JSON.stringify(CUR,null,2);render()}
function render(){const c=CUR;const only=document.getElementById('onlynew').checked;const auto=document.getElementById('autonew').checked;const start=only?Math.min(c.newIdxStart,c.msgs.length):0;
let h='<div class=muted>调用 #'+c.i+' · run 内第 '+c.i+' 次 · '+hh(c.ts)+' · '+c.provider+'/'+c.model+' · '+(c.msgsCount?'消息 '+c.msgsCount+' 条':'无消息列表输入')+'（新增 '+c.newCount+'） · 绑定工具 '+c.toolsBound+'</div>';
c.msgs.forEach(function(m,i){if(i<start)return;const isNew=i>=c.newIdxStart;
h+='<div class="bubble '+esc(m.role)+(isNew&&i>=c.newIdxStart?' new':'')+((isNew&&auto)||!isNew?' open':'')+'"><div class="bh" onclick="this.parentNode.classList.toggle(\\'open\\')">'+badge(m.role)+(m.name?'<span class=chip>'+esc(m.name)+'</span>':'')+(m.toolCallId?'<span class=chip>id:'+esc(String(m.toolCallId).slice(0,12))+'</span>':'')+'<span class=muted>'+fmtC(m.chars)+'字 · 第'+(i+1)+'条</span></div><div class="bb"><pre>'+esc(m.text)+'</pre>'+(m.toolCalls.length?'<h4>tool_calls</h4><pre>'+esc(pretty(m.toolCalls))+'</pre>':'')+'</div></div>'});
if(c.turnToolCalls.length){h+='<h3 class="sec">本轮工具调用（结果在下一次调用的消息里）</h3>'+c.turnToolCalls.map(function(tc){return '<div class="bubble tool open"><div class="bh">🔧 <b>'+esc(tc.name)+'</b><span class=muted>result '+(tc.result?tc.resultChars:0)+'字</span></div><div class="bb"><div><b>args</b></div><pre>'+esc(pretty(tc.args))+'</pre><div><b>result</b></div><pre>'+esc(tc.result==null?"（无结果记录）":tc.result)+'</pre></div></div>'}).join('')}
document.getElementById('left').innerHTML=h}
function copyJSON(){if(CUR)navigator.clipboard.writeText(JSON.stringify(CUR,null,2)).then(function(){alert('已复制')},function(){})}
(function(){const s=document.getElementById('sel');let h='';DATA.runs.forEach(function(r){h+='<optgroup label="run '+r.id+' · '+hh(r.start).slice(5,11)+'（'+r.calls.length+'次）">';r.calls.forEach(function(c){h+='<option value="'+r.id+':'+c.i+'">run'+r.id+' 调用#'+c.i+' · '+hh(c.ts).slice(6)+' · '+(c.turnToolCalls.length?c.turnToolCalls.length+'个工具':'无工具')+' · 新增'+c.newCount+'</option>'});h+='</optgroup>'});s.innerHTML=h;pick(s.value)})();
</script></body></html>"""

TEMPLATES = {
    "demo_a_timeline.html": _TPL_TIMELINE,
    "demo_b_table.html": _TPL_TABLE,
    "demo_c_inspector.html": _TPL_INSPECTOR,
}

if __name__ == "__main__":
    raise SystemExit(main())
