# 一次性 demo 生成器：用 2026-10-03 _EDGE 的真实 pack_detail 数据生成三个 UI demo（评审用，不入库逻辑）
import html
import json
import math
import re
from pathlib import Path

root = Path(__file__).resolve().parents[2]
raw = (root / "data/logs/pack_detail/2026-10-03/_EDGE.md").read_text(encoding="utf-8")
sections, cur = {}, None
for ln in raw.splitlines():
    if ln.startswith("## "):
        cur = ln[3:].strip()
        sections[cur] = []
    elif cur is not None:
        sections[cur].append(ln)
detail = "\n".join(sections["完整取数详情"]).strip()
pack = "\n".join(sections["渲染文本"]).strip()
packed = "\n".join(sections["实际进包"]).strip()

items = []
for ln in detail.splitlines():
    m = re.match(r"- (\S+) (.+)｜(.+)", ln.strip())
    if m:
        items.append({"domain": m.group(1), "title": m.group(2), "url": m.group(3)})
pack_domains = []
for ln in pack.splitlines():
    m = re.match(r"- (\S+)：", ln.strip())
    if m:
        pack_domains.append(m.group(1))
pack_set = set(pack_domains)
for it in items:
    it["in_pack"] = it["domain"] in pack_set
kept_n = sum(1 for it in items if it["in_pack"])
dropped_n = len(items) - kept_n
chars = {"detail": 11351, "full": 440, "packed": 440}
print(f"items={len(items)} kept={kept_n} dropped={dropped_n} pack_domains={len(pack_domains)}")

E = html.escape
CSS = """
*{box-sizing:border-box} body{margin:0;background:#f6f8fa;color:#1f2328;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.top{background:#fff;border-bottom:1px solid #d0d7de;padding:12px 24px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10}
.top h1{font-size:16px;margin:0}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600}
.pill.ok{background:#dafbe1;color:#1a7f37}.pill.warn{background:#fff8c5;color:#9a6700}.pill.miss{background:#eaeef2;color:#57606a}
.dnav{display:flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}
.dnav button{border:1px solid #d0d7de;background:#fff;border-radius:6px;padding:2px 9px;cursor:pointer}
.dnav .cur{font-weight:600}
.wrap{max-width:1180px;margin:0 auto;padding:18px 24px 60px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:16px 20px;margin-bottom:16px}
.muted{color:#57606a}.small{font-size:12px}
a{color:#0969da;text-decoration:none}a:hover{text-decoration:underline}
.bar{height:22px;border-radius:5px;background:#0969da;color:#fff;font-size:12px;line-height:22px;padding:0 8px;white-space:nowrap}
.bar.l2{background:#3fb950}.bar.l3{background:#8250df}
"""
TOP = CSS + """
<header class="top">
  <h1>源体检 · _EDGE</h1><span class="pill ok">ok</span>
  <div class="dnav"><button>‹</button><span class="cur">2026-10-03</span><button>›</button>
    <input type="date" value="2026-10-03" style="border:1px solid #d0d7de;border-radius:6px;padding:2px 6px"></div>
  <div style="margin-left:auto;display:flex;gap:12px" class="small">
    <a href="#">查最终 LLM 输入（scheduled 存档）</a><a href="#">llm_requests.jsonl</a><a href="#">← 总览矩阵</a>
  </div>
</header>
"""

# ---------- Demo A：漏斗 + Tab ----------
rows = []
for label, v, cls in [
    ("L2b 完整取数详情", chars["detail"], ""),
    ("L1b 渲染文本（top10 挑选）", chars["full"], "l2"),
    ("L1a 实际进包（预算后）", chars["packed"], "l3"),
]:
    w = max(6, int(100 * math.sqrt(v / max(chars.values()))))
    rows.append(
        f'<div style="display:flex;align-items:center;gap:12px;margin:8px 0">'
        f'<div style="width:200px;text-align:right" class="small muted">{label}</div>'
        f'<div style="flex:1"><div class="bar {cls}" style="width:{w}%">{v:,} 字符</div></div></div>'
    )
funnel = "".join(rows)

tab_detail = "".join(
    f'<div style="padding:7px 12px;border-bottom:1px solid #eaeef2">'
    f'<span class="pill {"ok" if it["in_pack"] else "miss"}" style="font-size:11px">'
    f'{"✓ 进包" if it["in_pack"] else "✂ 剔除"}</span> <b>{E(it["domain"])}</b> '
    f'<span class="muted">{E(it["title"][:60])}</span></div>'
    for it in items
)
tab_pack = "".join(f'<div style="padding:6px 12px">{E(ln)}</div>' for ln in pack.splitlines())

demo_a = TOP + """<div class="wrap">
<div class="card"><h3 style="margin:0 0 4px">字符漏斗：从取数到进包发生了什么</h3>
<div class="small muted">渲染文本被 top10 域名挑选裁掉约 10,911 字符（67 条页面 → 10 条域名归并）；预算未再裁剪（440 ≤ 900 上限）。</div>
__FUNNEL__</div>
<div class="card" style="padding:0">
<div style="display:flex;border-bottom:1px solid #d0d7de">
  <button class="tabbtn active" data-t="d">完整取数详情 · 67 条</button>
  <button class="tabbtn" data-t="p">渲染文本 · 10 条</button>
  <button class="tabbtn" data-t="p2">实际进包 · 10 条</button>
</div>
<div id="t-d" class="tab">__DETAIL__</div>
<div id="t-p" class="tab hidden">__PACK__</div>
<div id="t-p2" class="tab hidden">__PACKED__</div>
</div>
<p class="small muted">同一数据源三节内容高度重合时（如 _BILI），Tab 自动折叠为一个「三节一致」标签，不再三块堆叠。</p>
</div>
<style>.tabbtn{border:none;background:none;padding:10px 18px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent}
.tabbtn.active{border-bottom-color:#0969da;color:#0969da;font-weight:600}.hidden{display:none}</style>
<script>
document.querySelectorAll(".tabbtn").forEach(function(b){b.onclick=function(){
  document.querySelectorAll(".tabbtn").forEach(function(x){x.classList.remove("active")});
  document.querySelectorAll(".tab").forEach(function(x){x.classList.add("hidden")});
  b.classList.add("active"); document.getElementById("t-"+b.dataset.t).classList.remove("hidden");
};});
</script>"""
demo_a = demo_a.replace("__FUNNEL__", funnel).replace("__DETAIL__", tab_detail).replace("__PACK__", tab_pack).replace("__PACKED__", tab_pack)

# ---------- Demo B：逐条归因表 ----------
rows = []
for it in items:
    ok = it["in_pack"]
    rows.append(
        f'<tr class="{"kept" if ok else "dropped"}">'
        f'<td><span class="pill {"ok" if ok else "warn"}" style="font-size:11px">{"✓ 进包" if ok else "✂ 被剔除"}</span></td>'
        f'<td><b>{E(it["domain"])}</b></td>'
        f'<td class="t" title="{E(it["title"])}">{E(it["title"])}</td>'
        f'<td class="small muted">{E(it["url"][:60])}{"…" if len(it["url"]) > 60 else ""}</td></tr>'
    )
demo_b = TOP + """<div class="wrap">
<div style="display:flex;gap:12px;margin-bottom:16px">
  <div class="card" style="flex:1;text-align:center;margin:0"><div style="font-size:26px;font-weight:700">67</div><div class="small muted">L2b 取数条目</div></div>
  <div class="card" style="flex:1;text-align:center;margin:0"><div style="font-size:26px;font-weight:700;color:#1a7f37">__KEPT__</div><div class="small muted">命中进包域名</div></div>
  <div class="card" style="flex:1;text-align:center;margin:0"><div style="font-size:26px;font-weight:700;color:#9a6700">__DROPPED__</div><div class="small muted">被挑选剔除</div></div>
  <div class="card" style="flex:1;text-align:center;margin:0"><div style="font-size:26px;font-weight:700">10</div><div class="small muted">进包域名归并行</div></div>
</div>
<div class="card" style="padding:0">
<div style="padding:12px 16px;display:flex;gap:8px;align-items:center;border-bottom:1px solid #d0d7de">
  <b>逐条归因：每条取数数据落在哪一层</b>
  <button class="f active" data-f="all">全部 67</button>
  <button class="f" data-f="kept">✓ 进包 __KEPT__</button>
  <button class="f" data-f="dropped">✂ 被剔除 __DROPPED__</button>
  <span class="small muted" style="margin-left:auto">归因口径：该条所属域名是否进入 top10 归并行</span>
</div>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead><tr style="text-align:left;background:#f6f8fa"><th style="padding:8px 12px">层级</th><th style="padding:8px 12px">域名</th><th style="padding:8px 12px">页面标题</th><th style="padding:8px 12px">URL</th></tr></thead>
<tbody>__ROWS__</tbody></table></div>
<p class="small muted">这正是「某条浏览记录为什么没进日报」的下钻答案：不是丢了，是被 top10 挑选规则排除。</p>
</div>
<style>tr.dropped{background:#fff8f8}tr.dropped .t{color:#8c959f;text-decoration:line-through}
td{padding:7px 12px;border-top:1px solid #eaeef2}.t{max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.f{border:1px solid #d0d7de;background:#fff;border-radius:99px;padding:3px 12px;cursor:pointer;font-size:12px}
.f.active{background:#0969da;color:#fff;border-color:#0969da}</style>
<script>
document.querySelectorAll(".f").forEach(function(b){b.onclick=function(){
  document.querySelectorAll(".f").forEach(function(x){x.classList.remove("active")}); b.classList.add("active");
  var f=b.dataset.f;
  document.querySelectorAll("tbody tr").forEach(function(tr){
    tr.style.display = f==="all" || tr.classList.contains(f) ? "" : "none";});
});});
</script>"""
demo_b = demo_b.replace("__ROWS__", "".join(rows)).replace("__KEPT__", str(kept_n)).replace("__DROPPED__", str(dropped_n))

# ---------- Demo C：双栏对比 ----------
left = "".join(
    f'<div class="li {"hit" if it["in_pack"] else "missrow"}"><span class="dm">{E(it["domain"])}</span> {E(it["title"][:48])}</div>'
    for it in items
)
right = "".join(f'<div class="li">{E(ln)}</div>' for ln in pack.splitlines())
demo_c = TOP + """<div class="wrap">
<div class="card" style="display:flex;gap:18px;align-items:center">
  <div><span class="small muted">取数</span><div style="font-size:20px;font-weight:700">11,351</div></div>
  <div class="small muted">→ top10 挑选 −10,911（67条→10条域名）→</div>
  <div><span class="small muted">渲染</span><div style="font-size:20px;font-weight:700;color:#1a7f37">440</div></div>
  <div class="small muted">→ 预算 −0 →</div>
  <div><span class="small muted">实际进包</span><div style="font-size:20px;font-weight:700;color:#8250df">440</div></div>
</div>
<div style="display:flex;gap:16px;align-items:stretch">
  <div class="card" style="flex:1.4;margin:0;padding:12px 0 0;overflow:hidden">
    <div style="padding:0 16px 10px"><b>完整取数详情</b> <span class="small muted">67 条 · 灰色删除线 = 被挑选剔除</span></div>
    <div class="scroll">__LEFT__</div>
  </div>
  <div class="card" style="flex:1;margin:0;padding:12px 0 0;overflow:hidden">
    <div style="padding:0 16px 10px"><b>实际进包</b> <span class="small muted">10 条域名归并 · 440 字符</span></div>
    <div class="scroll" style="background:#f6f8fa">__RIGHT__</div>
  </div>
</div>
<p class="small muted">左栏命中行高亮、剔除行删除线，与右栏对照阅读；若三节一致（如 _BILI），左栏自动折叠、只显示单栏 +「三节一致」徽章。</p>
</div>
<style>.scroll{max-height:560px;overflow:auto}
.li{padding:6px 16px;border-bottom:1px solid #eaeef2;font-size:13px}
.li .dm{display:inline-block;background:#eaeef2;border-radius:4px;padding:0 6px;font-size:11px;margin-right:6px}
.li.missrow{color:#8c959f;text-decoration:line-through}
.li.hit{background:#f0fdf4}</style>"""
demo_c = demo_c.replace("__LEFT__", left).replace("__RIGHT__", right)

out = root / "docs/health-demos"
for name, doc in [
    ("demo-a-funnel-tabs.html", demo_a),
    ("demo-b-attribution.html", demo_b),
    ("demo-c-side-by-side.html", demo_c),
]:
    (out / name).write_text(doc, encoding="utf-8")
    print("wrote", name, len(doc), "bytes")
