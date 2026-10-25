"""KG 可视化前端的 CSS / HTML / JS 资产（由 demo 原型固化，纯字符串常量）。

与 kg_view.py 的数据层解耦：改样式/交互只动本文件，改 SQL/快照只动 kg_view.py。
所有页面自包含，不引用 CDN；cytoscape.min.js 由 kg_view.py 内联。
"""

from __future__ import annotations


NAV_CSS = r"""
.kgnav{display:flex;gap:4px;align-items:center;flex-wrap:wrap;margin:0 4px}
.kgnav a{font:11px/1.2 "Segoe UI","Microsoft YaHei",sans-serif;padding:5px 10px;border:1px solid rgba(128,128,128,.35);
  border-radius:99px;color:inherit;text-decoration:none;opacity:.7;white-space:nowrap}
.kgnav a:hover{opacity:1}
.kgnav a.on{opacity:1;border-color:currentColor;font-weight:700}
.kgnav .kgoff{font:11px/1.2 "Segoe UI","Microsoft YaHei",sans-serif;padding:5px 10px;border:1px dashed rgba(128,128,128,.3);
  border-radius:99px;opacity:.35;white-space:nowrap}
"""


COMMON = r"""
const DATA = JSON.parse(document.getElementById('kg-data').textContent);
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clip = (s, n) => { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const dayLabel = (d) => d ? d.slice(5).replace('-', '/') : '';
function typeColor(t) { return (DATA.meta.type_color[t] || '#64748b'); }
function typeLabel(t) { return (DATA.meta.type_label[t] || t); }
function levelLabel(l) { return (DATA.meta.level_label[l] || l); }
"""

D1_CSS = r"""
:root{
  --bg:#070b12;--panel:#0b1220;--panel2:#0e1728;--line:#1a2a3f;--text:#dbe7f3;
  --muted:#71869e;--accent:#5eead4;--amber:#f2b84b;--violet:#b98cff;--blue:#7aa2ff;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.5 "Segoe UI","Microsoft YaHei",sans-serif;overflow:hidden}
header{height:58px;display:flex;align-items:center;gap:18px;padding:0 18px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#0b1422,#070b12)}
.brand{display:flex;align-items:baseline;gap:10px;font-weight:650;letter-spacing:.02em;white-space:nowrap}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);box-shadow:0 0 14px var(--accent)}
.brand .sub{color:var(--muted);font-weight:400;font-size:11px}
.stats{display:flex;gap:18px;color:var(--muted);font-size:11px;white-space:nowrap}
.stats b{color:var(--text);font-size:13px;font-variant-numeric:tabular-nums}
.actions{margin-left:auto;display:flex;gap:8px;align-items:center}
input[type=search],input[type=text]{background:#0a1424;border:1px solid var(--line);color:var(--text);
  border-radius:6px;padding:6px 10px;font:12px inherit;outline:none;width:210px}
input:focus{border-color:#2c4a6b}
button{background:#0f1d30;border:1px solid var(--line);color:var(--text);border-radius:6px;padding:6px 12px;
  font:12px inherit;cursor:pointer}
button:hover{border-color:#35577d;background:#13253c}
main{height:calc(100% - 58px);display:grid;grid-template-columns:236px minmax(0,1fr) 320px}
aside{background:var(--panel);overflow:auto}
.left{border-right:1px solid var(--line);padding:16px 14px}
.right{border-left:1px solid var(--line);padding:16px}
section{margin-bottom:20px}
h3{margin:0 0 9px;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);font-weight:600}
.check{display:flex;align-items:center;gap:8px;padding:4px 0;color:#b9c9da;cursor:pointer;user-select:none}
.check input{accent-color:var(--accent)}
.swatch{width:9px;height:9px;border-radius:2px;flex:none}
.legend{font-size:11px;color:var(--muted);line-height:1.9}
.legend .row{display:flex;align-items:center;gap:8px}
.legend .line{width:26px;height:0;border-top:2px solid var(--blue);flex:none}
.legend .line.l2{border-color:var(--amber)}
.legend .line.l3{border-color:var(--violet);border-top-style:dashed}
#cy{position:relative;background:radial-gradient(ellipse at 50% 42%,#0d1728 0%,#070b12 68%)}
#cy::after{content:"";position:absolute;inset:0;pointer-events:none;
  background-image:linear-gradient(#ffffff05 1px,transparent 1px),linear-gradient(90deg,#ffffff05 1px,transparent 1px);
  background-size:44px 44px}
.right .empty{color:var(--muted);font-size:12px;margin-top:26px;text-align:center;line-height:1.9}
.node-title{font-size:16px;font-weight:650;line-height:1.35;margin:2px 0 6px}
.chip{display:inline-block;font-size:10px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);
  color:#a9bccf;margin:0 4px 4px 0}
.chip.strong{color:#06121a;background:var(--accent);border-color:transparent;font-weight:700}
.kv{display:grid;grid-template-columns:72px 1fr;gap:4px 8px;font-size:11px;margin:10px 0;color:#b8c8d9}
.kv span:nth-child(odd){color:var(--muted)}
.mono{font-family:Consolas,monospace;font-size:10px;color:#8fa6bd;word-break:break-all}
.incident{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.incident h4{margin:0 0 8px;font-size:10px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}
.link-row{display:flex;gap:7px;align-items:flex-start;padding:6px 6px;border-radius:5px;cursor:pointer;font-size:11px;line-height:1.45}
.link-row:hover{background:#0f1c2e}
.link-row .rel{color:var(--accent);flex:none;font-family:Consolas,monospace;font-size:10px;padding-top:1px}
.link-row .other{color:#c8d7e6}
.link-row time{margin-left:auto;color:#5f758c;font-size:10px;flex:none}
.tip{position:fixed;z-index:99;pointer-events:none;background:#0b1626f2;border:1px solid #26405c;border-radius:6px;
  padding:7px 9px;max-width:300px;font-size:11px;line-height:1.5;opacity:0;transition:opacity .08s;box-shadow:0 8px 30px #0008}
.tip b{color:var(--accent)}
.faded{opacity:.12!important}
"""

D1_BODY = r"""
<header>
  <div class="brand"><span class="dot"></span>KG 全景图 <span class="sub">langTrack · kg_* · 只读快照</span></div>
  {{KG_NAV}}
  <div class="stats">
    <span>节点 <b id="s-nodes">–</b></span>
    <span>边 <b id="s-edges">–</b></span>
    <span>孤立 <b id="s-iso">–</b></span>
    <span>消解候选 <b id="s-cand">–</b></span>
  </div>
  <div class="actions">
    <input type="search" id="search" placeholder="搜节点名，回车定位">
    <button id="relayout">重排</button>
  </div>
</header>
<main>
  <aside class="left">
    <section><h3>节点类型</h3><div id="types"></div></section>
    <section><h3>边层级</h3><div id="levels"></div></section>
    <section><h3>显示</h3>
      <label class="check"><input type="checkbox" id="hide-iso" checked> 隐藏孤立节点</label>
      <label class="check"><input type="checkbox" id="show-rel" checked> 显示稳定关系</label>
      <label class="check"><input type="checkbox" id="dim" checked> 选中时压暗其余</label>
    </section>
    <section><h3>图例</h3>
      <div class="legend">
        <div class="row"><span class="line"></span>L1 事实</div>
        <div class="row"><span class="line l2"></span>L2 观点</div>
        <div class="row"><span class="line l3"></span>L3 推断</div>
        <div class="row" style="margin-top:8px">形状 = 节点类型</div>
        <div class="row">点击节点看明细 · 滚轮缩放</div>
      </div>
    </section>
  </aside>
  <div id="cy"></div>
  <aside class="right" id="inspector"><div class="empty">点一个节点或一条边<br>右侧显示它的全部关系</div></aside>
</main>
<div class="tip" id="tip"></div>
"""

D1_JS = r"""
const ALL_TYPES = [...new Set(DATA.nodes.map(n=>n.type))].sort();
const state = {types:new Set(ALL_TYPES), levels:new Set(['L1','L2','L3']),
  hideIso:true, showRel:true, dim:true};
let cy=null, selected=null;

function nodeColor(t){ return typeColor(t); }
function nodeSize(n){ return 12 + Math.min(30, Math.sqrt(n.degree||0)*3.2); }
function showLabel(n){ return n.type==='Person'||n.type==='Project'||(n.degree||0)>=5; }

function buildElements(){
  const nodes = DATA.nodes.filter(n => state.types.has(n.type) && (!state.hideIso || !n.isolated));
  const ids = new Set(nodes.map(n=>n.id));
  const edges = DATA.edges.filter(e =>
    ids.has(e.from) && ids.has(e.to) &&
    state.levels.has(e.level) &&
    (state.showRel || e.kind!=='relation')
  );
  const nEls = nodes.map(n=>({group:'nodes',data:{
    id:n.id, label:n.label, short:clip(n.label, showLabel(n)?16:0), type:n.type,
    degree:n.degree, isolated:n.isolated, first_day:n.first_day, last_day:n.last_day,
    canonical:n.canonical, aliases:(Array.isArray(n.aliases)?n.aliases:[]).join('、'),
    color:nodeColor(n.type), shape:DATA.meta.type_shape[n.type]||'ellipse', size:nodeSize(n),
    show:showLabel(n)?1:0
  }}));
  const eEls = edges.map(e=>({group:'edges',data:{
    id:e.id, source:e.from, target:e.to, relation:e.relation, level:e.level, kind:e.kind,
    day:e.day, assertion:e.assertion, evidence:e.evidence, src:e.src,
    valid_from:e.valid_from, valid_to:e.valid_to, strength:e.strength,
    color: e.level==='L1' ? '#3f5f82' : e.level==='L2' ? '#f2b84b' : '#b98cff',
    line: e.level==='L3' ? 'dashed' : 'solid',
    width: e.kind==='relation' ? 1.7 : 1.15,
    arrow: e.kind==='relation' ? 'triangle' : 'none',
    alts: e.alts||1
  }}));
  return nEls.concat(eEls);
}

const STYLE = [
  {selector:'node',style:{
    'background-color':'data(color)','shape':'data(shape)',
    'width':'data(size)','height':'data(size)',
    'label':'data(short)','font-size':9,'color':'#d5e4f3','font-weight':500,
    'text-outline-width':2.5,'text-outline-color':'#070b12','text-valign':'bottom','text-margin-y':4,
    'border-width':1,'border-color':'#0a1322'
  }},
  {selector:'node[show = 0]',style:{'label':''}},
  {selector:'node:selected',style:{'border-width':3,'border-color':'#ffffff'}},
  {selector:'edge',style:{
    'width':'data(width)','line-color':'data(color)','target-arrow-color':'data(color)',
    'target-arrow-shape':'data(arrow)','arrow-scale':.55,'curve-style':'bezier',
    'line-style':'data(line)','opacity':.8
  }},
  {selector:'edge[kind = "relation"]',style:{'line-opacity':.95}},
  {selector:'edge:selected',style:{'width':3.2,'line-color':'#ffffff','target-arrow-color':'#ffffff','opacity':1}}
];

const LAYOUT = {name:'cose',animate:false,fit:true,padding:36,randomize:true,
  nodeRepulsion:9000,idealEdgeLength:72,edgeElasticity:120,gravity:.22,numIter:900,
  nodeOverlap:12,componentSpacing:70};

function render(){
  if(cy) cy.destroy();
  const els = buildElements();
  cy = cytoscape({container:$('#cy'),elements:els,style:STYLE,layout:LAYOUT,
    wheelSensitivity:.18,minZoom:.1,maxZoom:3.2});
  bindGraph();
  $('#s-nodes').textContent = els.filter(e=>e.group==='nodes').length;
  $('#s-edges').textContent = els.filter(e=>e.group==='edges').length;
  if(selected){ const n=cy.$id(selected); if(n.length){ n.select(); showNode(n); } else selected=null; }
}

function bindGraph(){
  cy.on('tap','node',evt=>{
    if(evt.target===cy) return;
    selected=evt.target.id();
    showNode(evt.target);
    if(state.dim){
      cy.elements().addClass('faded');
      evt.target.closedNeighborhood().removeClass('faded');
      evt.target.removeClass('faded');
    }
  });
  cy.on('tap','edge',evt=>{
    showEdge(evt.target);
    if(state.dim){
      cy.elements().addClass('faded');
      evt.target.removeClass('faded');
      evt.target.connectedNodes().removeClass('faded');
    }
  });
  cy.on('tap',evt=>{
    if(evt.target!==cy) return;
    selected=null; cy.elements().removeClass('faded');
    $('#inspector').innerHTML='<div class="empty">点一个节点或一条边<br>右侧显示它的全部关系</div>';
  });
  cy.on('mouseover','node',evt=>{
    const d=evt.target.data();
    tip(`<b>${esc(d.label)}</b><br>${esc(typeLabel(d.type))} · degree ${d.degree}<br><span style="color:#71869e">${esc(d.id)}</span>`,evt);
  });
  cy.on('mouseover','edge',evt=>{
    const d=evt.target.data();
    tip(`<b>${esc(d.relation)}</b> · ${esc(d.level)}<br>${esc(d.assertion||'（无断言文本）')}`,evt);
  });
  cy.on('mouseout','node edge',hideTip);
  cy.on('drag',hideTip);
}

function tip(html,evt){
  const t=$('#tip'); t.innerHTML=html;
  const e=evt.originalEvent||{};
  const x=(e.clientX||0)+14, y=(e.clientY||0)+14;
  t.style.left=Math.min(x,innerWidth-320)+'px'; t.style.top=Math.min(y,innerHeight-120)+'px';
  t.style.opacity=1;
}
function hideTip(){ $('#tip').style.opacity=0; }

function showNode(n){
  const d=n.data();
  const rows=n.connectedEdges().map(e=>{
    const o=e.source().id()===d.id?e.target():e.source();
    return `<div class="link-row" data-edge="${esc(e.id())}">
      <span class="rel">${esc(e.data('relation'))}</span>
      <span class="other">${esc(o.data('label'))}</span>
      <time>${esc((e.data('day')||'').slice(5))}</time></div>`;
  });
  const aliases=d.aliases?`<div class="kv"><span>别名</span><span>${esc(d.aliases)}</span></div>`:'';
  $('#inspector').innerHTML=`
    <div><span class="chip strong" style="background:${esc(d.color)}">${esc(typeLabel(d.type))}</span>
    ${d.isolated?'<span class="chip">孤立</span>':''}<span class="chip">degree ${d.degree}</span></div>
    <div class="node-title">${esc(d.label)}</div>
    <div class="mono">${esc(d.id)}</div>
    <div class="kv"><span>首见</span><span>${esc(d.first_day)}</span><span>末见</span><span>${esc(d.last_day)}</span></div>
    ${aliases}
    ${d.canonical?`<div class="kv"><span>canonical</span><span>${esc(d.canonical)}</span></div>`:''}
    <div class="incident"><h4>关联边 ${rows.length}</h4>${rows.join('')||'<div class="empty">没有边</div>'}</div>`;
  $('#inspector').querySelectorAll('.link-row').forEach(el=>{
    el.onclick=()=>{ const edge=cy.$id(el.dataset.edge); cy.fit(edge,140); edge.select(); showEdge(edge); };
  });
}

function showEdge(e){
  const d=e.data();
  const from=e.source().data('label'), to=e.target().data('label');
  $('#inspector').innerHTML=`
    <div><span class="chip strong" style="background:${d.level==='L1'?'#7aa2ff':d.level==='L2'?'#f2b84b':'#b98cff'}">${esc(d.level)} ${esc(levelLabel(d.level))}</span>
    <span class="chip">${esc(d.kind==='relation'?'稳定关系':d.src)}</span></div>
    <div class="node-title" style="font-size:14px">${esc(from)} <span style="color:#5eead4">—${esc(d.relation)}→</span> ${esc(to)}</div>
    <div class="kv"><span>断言</span><span>${esc(d.assertion||'—')}</span>
      <span>日期</span><span>${esc(d.day)}</span>
      ${d.valid_from?`<span>生效</span><span>${esc(d.valid_from)}</span>`:''}
      ${d.valid_to?`<span>失效</span><span>${esc(d.valid_to)}</span>`:''}
      ${d.strength!=null?`<span>强度</span><span>${esc(d.strength)}</span>`:''}
      <span>id</span><span class="mono">${esc(d.id)}</span></div>
    ${d.evidence?`<div class="kv"><span>证据</span><span>${esc(d.evidence)}</span></div>`:''}`;
}

function initControls(){
  const types=$('#types');
  ALL_TYPES.forEach(t=>{
    const n=DATA.nodes.filter(x=>x.type===t).length;
    if(!n) return;
    const el=document.createElement('label'); el.className='check';
    el.innerHTML=`<input type="checkbox" checked><span class="swatch" style="background:${typeColor(t)}"></span>${esc(typeLabel(t))}<span style="margin-left:auto;color:#5f758c">${n}</span>`;
    el.querySelector('input').onchange=ev=>{ ev.target.checked?state.types.add(t):state.types.delete(t); render(); };
    types.appendChild(el);
  });
  const levels=$('#levels');
  [['L1','#7aa2ff'],['L2','#f2b84b'],['L3','#b98cff']].forEach(([l,c])=>{
    const el=document.createElement('label'); el.className='check';
    el.innerHTML=`<input type="checkbox" checked><span class="swatch" style="background:${c};${l==='L3'?'border-radius:50%':''}"></span>${l} ${esc(levelLabel(l))}`;
    el.querySelector('input').onchange=ev=>{ ev.target.checked?state.levels.add(l):state.levels.delete(l); render(); };
    levels.appendChild(el);
  });
  $('#hide-iso').onchange=ev=>{ state.hideIso=ev.target.checked; render(); };
  $('#show-rel').onchange=ev=>{ state.showRel=ev.target.checked; render(); };
  $('#dim').onchange=ev=>{ state.dim=ev.target.checked; if(!state.dim) cy.elements().removeClass('faded'); };
  $('#relayout').onclick=()=>cy.layout(LAYOUT).run();
  $('#search').addEventListener('keydown',ev=>{
    if(ev.key!=='Enter') return;
    const q=ev.target.value.trim().toLowerCase();
    if(!q) return;
    const hits=DATA.nodes.filter(n=>n.label.toLowerCase().includes(q)||n.id.toLowerCase().includes(q));
    hits.sort((a,b)=>{
      const rank=(n)=>n.label.toLowerCase()===q?0:n.label.toLowerCase().startsWith(q)?1:2;
      return rank(a)-rank(b) || (a.type==='Person'?0:1)-(b.type==='Person'?0:1) || b.degree-a.degree;
    });
    const hit=hits[0];
    if(!hit){ ev.target.value=''; return; }
    if(!cy.$id(hit.id).length){ state.types.add(hit.type); state.hideIso=false; $('#hide-iso').checked=false; render(); }
    const el=cy.$id(hit.id); cy.fit(el,140); cy.elements().removeClass('faded'); el.select();
    if(state.dim){ cy.elements().addClass('faded'); el.closedNeighborhood().removeClass('faded'); el.removeClass('faded'); }
    showNode(el);
  });
}

$('#s-iso').textContent=DATA.audit.counts.isolated;
$('#s-cand').textContent=DATA.audit.counts.alias_candidates;
initControls();
render();
"""

D3_CSS = r"""
:root{--bg:#0a0918;--panel:#111026;--line:#252147;--text:#e7e3f5;--muted:#8c86ab;--amber:#fbbf24;--cyan:#22d3ee}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.5 "Segoe UI","Microsoft YaHei",sans-serif;overflow:hidden}
header{height:70px;display:flex;align-items:center;gap:26px;padding:0 22px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#14122c,#0a0918)}
h1{font:600 17px/1.2 Georgia,"Songti SC",serif;margin:0;letter-spacing:.02em}
.kicker{font-size:9px;letter-spacing:.28em;text-transform:uppercase;color:var(--amber);font-weight:700;margin-bottom:3px}
.readout{margin-left:auto;display:flex;align-items:baseline;gap:14px}
.readout .day{font:600 30px/1 Consolas,monospace;color:var(--amber);letter-spacing:.04em}
.readout .meta{color:var(--muted);font-size:11px;text-align:right;line-height:1.5}
main{height:calc(100% - 70px - 92px);display:grid;grid-template-columns:250px minmax(0,1fr) 330px}
.rail{border-right:1px solid var(--line);background:var(--panel);padding:16px 14px;overflow:auto}
.rail h3,.detail h3{margin:0 0 10px;font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.iso-toggle{display:flex;align-items:center;gap:7px;font-size:11px;color:#b8b1d6;margin:0 0 16px;cursor:pointer;user-select:none}
.iso-toggle input{accent-color:var(--cyan)}
.dayrow{display:grid;grid-template-columns:54px 1fr;gap:8px;align-items:center;padding:7px 8px;border-radius:4px;
  cursor:pointer;border:1px solid transparent}
.dayrow:hover{background:#181635}
.dayrow.on{background:#1b1838;border-color:#3a3266}
.dayrow .date{font-family:Consolas,monospace;font-size:11px;color:#b8b1d6}
.dayrow.on .date{color:var(--amber)}
.dayrow .bars{display:flex;flex-direction:column;gap:3px}
.bar{height:4px;border-radius:9px;background:#2a2650;position:relative}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:9px;display:block}
.bar.n i{background:var(--cyan)}
.bar.e i{background:var(--amber)}
.dayrow .nums{font-size:9px;color:var(--muted);display:flex;gap:7px;margin-top:1px}
#cy{background:radial-gradient(ellipse at 50% 45%,#14122c 0%,#0a0918 70%)}
.detail{border-left:1px solid var(--line);background:var(--panel);padding:16px;overflow:auto}
.status{display:inline-block;font-size:9px;letter-spacing:.1em;padding:2px 8px;border-radius:99px;margin-bottom:10px}
.status.ok{background:#0d3b2e;color:#5eead4}.status.partial{background:#3d2e0c;color:#fbbf24}
.status.failed{background:#43151b;color:#fb7185}.status.running{background:#152a44;color:#7aa2ff}
.kv{display:grid;grid-template-columns:78px 1fr;gap:4px 8px;font-size:11px;color:#c5c0dc;margin:8px 0 14px}
.kv span:nth-child(odd){color:var(--muted)}
.chan{display:grid;grid-template-columns:66px 1fr 34px;gap:6px;font-size:10.5px;padding:4px 0;border-top:1px dashed #252147;align-items:center}
.chan .k{font-family:Consolas,monospace;color:#a49ec4}
.chan .err{grid-column:1/-1;color:#fb7185;font-size:9.5px;word-break:break-all}
.pill{font-size:8.5px;padding:1px 6px;border-radius:99px;text-align:center}
.pill.ok{background:#0d3b2e;color:#5eead4}.pill.failed{background:#43151b;color:#fb7185}
.pill.skipped{background:#2a2650;color:#b8b1d6}.pill.partial{background:#3d2e0c;color:#fbbf24}
.newlist{margin-top:14px;border-top:1px solid var(--line);padding-top:10px}
.newlist h4{margin:0 0 6px;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.newlist .item{font-size:10.5px;padding:3px 0;color:#c5c0dc;border-bottom:1px dotted #221f40;line-height:1.4}
.newlist .item b{color:var(--amber);font-weight:600}
footer{height:92px;border-top:1px solid var(--line);background:#0d0c1f;display:flex;align-items:center;gap:18px;padding:0 24px}
#play{width:38px;height:38px;border-radius:50%;border:1px solid #3a3266;background:#181635;color:var(--text);
  font-size:13px;cursor:pointer}
#play:hover{border-color:var(--amber);color:var(--amber)}
.scrub{flex:1;display:flex;flex-direction:column;gap:10px}
input[type=range]{width:100%;accent-color:var(--amber);background:transparent}
.ticks{display:flex;justify-content:space-between;font-family:Consolas,monospace;font-size:9px;color:var(--muted)}
.ticks span{cursor:pointer}.ticks span.on{color:var(--amber)}
.faded{opacity:.13}
.newnode{border-width:3px!important;border-color:#fbbf24!important}
.tip{position:fixed;z-index:9;pointer-events:none;background:#171533f2;border:1px solid #3a3266;border-radius:5px;
  padding:6px 9px;font-size:10.5px;opacity:0;transition:opacity .1s;max-width:280px}
"""

D3_BODY = r"""
<header>
  <div><div class="kicker">Temporal graph</div><h1>图谱演化 · 每天长出来什么</h1></div>
  {{KG_NAV}}
  <div class="readout"><div class="day" id="dayBig">--</div>
    <div class="meta" id="dayMeta"></div></div>
</header>
<main>
  <aside class="rail">
    <label class="iso-toggle"><input type="checkbox" id="hide-iso" checked> 隐藏孤立节点</label>
    <h3>抽取日</h3><div id="rail"></div>
  </aside>
  <div id="cy"></div>
  <aside class="detail" id="detail"></aside>
</main>
<footer>
  <button id="play">▶</button>
  <div class="scrub"><input type="range" id="slider" min="0" max="0" value="0"><div class="ticks" id="ticks"></div></div>
</footer>
<div class="tip" id="tip"></div>
"""

D3_JS = r"""
const DAYS = DATA.days;
const RUN = new Map(DATA.runs.map(r=>[r.day,r]));
const STAT = new Map(DATA.day_stats.map(s=>[s.day,s]));
const state = {hideIso:true};
let idx = DAYS.length-1, cy=null, timer=null;

function build(){
  const els=[];
  DATA.nodes.forEach(n=>{
    if(state.hideIso && n.isolated) return;
    els.push({group:'nodes',data:{id:n.id,label:n.label,short:clip(n.label,13),type:n.type,degree:n.degree,
      first_day:n.first_day,last_day:n.last_day,color:typeColor(n.type),
      shape:DATA.meta.type_shape[n.type]||'ellipse',size:11+Math.min(26,Math.sqrt(n.degree||0)*3)}});
  });
  DATA.edges.forEach(e=>{
    els.push({group:'edges',data:{id:e.id,source:e.from,target:e.to,relation:e.relation,level:e.level,kind:e.kind,
      day:e.day,assertion:e.assertion,color:e.level==='L1'?'#4a6b91':e.level==='L2'?'#fbbf24':'#a78bfa',
      line:e.level==='L3'?'dashed':'solid',width:e.kind==='relation'?1.6:1.05}});
  });
  return els;
}

const STYLE=[
  {selector:'node',style:{'background-color':'data(color)','shape':'data(shape)','width':'data(size)','height':'data(size)',
    'label':'data(short)','font-size':8.5,'color':'#d8d3ec','text-outline-width':2.5,'text-outline-color':'#0a0918',
    'text-valign':'bottom','text-margin-y':3,'border-width':1,'border-color':'#0a0918',
    'transition-property':'opacity','transition-duration':'200ms'}},
  {selector:'edge',style:{'width':'data(width)','line-color':'data(color)','line-style':'data(line)',
    'curve-style':'bezier','opacity':.55}},
  {selector:'.new',style:{'border-width':3,'border-color':'#fbbf24','opacity':1}},
  {selector:'edge.new',style:{'line-color':'#fbbf24','width':2.4,'opacity':1}},
  {selector:'.gone',style:{'opacity':.12}}
];

function apply(day, prevDay){
  const newNodes=new Set(DATA.nodes.filter(n=>n.first_day===day).map(n=>n.id));
  const newEdges=new Set(DATA.edges.filter(e=>e.day===day).map(e=>e.id));
  cy.batch(()=>{
    cy.nodes().forEach(n=>{
      const d=n.data();
      const visible=d.first_day<=day;
      n.style('display',visible?'element':'none');
      n.toggleClass('gone',visible && d.last_day<day);
      n.toggleClass('new',d.first_day===day);
      if(prevDay && d.first_day<day && d.first_day===prevDay) n.removeClass('new');
    });
    cy.edges().forEach(e=>{
      const d=e.data();
      const visible=d.day<=day;
      e.style('display',visible?'element':'none');
      e.toggleClass('new',d.day===day);
    });
  });
  const st=STAT.get(day)||{};
  $('#dayBig').textContent=day;
  const run=RUN.get(day)||{};
  $('#dayMeta').innerHTML=`${esc(run.status||'—')} · ${st.nodes_total||0} 节点 / ${st.edges_total||0} 边<br>新增 ${st.new_nodes||0} 节点 · ${st.new_edges||0} 边`;
  renderRail(day); renderDetail(day,run,st);
  $('#slider').value=idx;
  document.querySelectorAll('#ticks span').forEach((el,i)=>el.classList.toggle('on',i===idx));
}

function renderRail(day){
  const maxN=Math.max(1,...DATA.day_stats.map(s=>s.new_nodes));
  const maxE=Math.max(1,...DATA.day_stats.map(s=>s.new_edges));
  $('#rail').innerHTML=DATA.day_stats.map((s,i)=>{
    const run=RUN.get(s.day)||{};
    return `<div class="dayrow ${s.day===day?'on':''}" data-i="${i}">
      <div><div class="date">${esc(dayLabel(s.day))}</div><div class="nums"><span style="color:#22d3ee">${s.new_nodes}n</span><span style="color:#fbbf24">${s.new_edges}e</span></div></div>
      <div class="bars"><div class="bar n"><i style="width:${Math.max(4,s.new_nodes/maxN*100)}%"></i></div>
      <div class="bar e"><i style="width:${Math.max(4,s.new_edges/maxE*100)}%"></i></div></div>
    </div>`;
  }).join('');
  document.querySelectorAll('.dayrow').forEach(el=>el.onclick=()=>{ stop(); idx=+el.dataset.i; apply(DAYS[idx],DAYS[idx-1]); });
}

function renderDetail(day,run,st){
  const chan=(run.channels||[]).map(c=>`<div class="chan"><span class="k">${esc(c.key)}</span>
    <span>${c.rows||0} 行 ${c.error?`<span class="err">${esc(c.error)}</span>`:''}</span>
    <span class="pill ${esc(c.status)}">${esc(c.status)}</span></div>`).join('');
  const newN=DATA.nodes.filter(n=>n.first_day===day).slice(0,10);
  const newE=DATA.edges.filter(e=>e.day===day).slice(0,10);
  $('#detail').innerHTML=`<h3>当日详情</h3>
    <span class="status ${esc(run.status||'running')}">${esc(run.status||'—')}</span>
    <div class="kv"><span>素材</span><span>${(run.material_chars||0).toLocaleString()} 字符</span>
      <span>来源</span><span>${(run.sources||[]).length} 个</span>
      <span>开始</span><span>${esc((run.started_at||'').slice(5))}</span>
      <span>结束</span><span>${esc((run.finished_at||'').slice(5)||'—')}</span>
      <span>累计</span><span>${st.nodes_total||0} 节点 · ${st.edges_total||0} 边</span>
      ${state.hideIso?`<span>视图</span><span style="color:var(--muted)">已隐藏 ${DATA.audit.counts.isolated} 个孤立节点</span>`:''}</div>
    ${run.reap&&run.reap.error?`<div style="color:#fb7185;font-size:10.5px;margin-bottom:10px">${esc(run.reap.error)}</div>`:''}
    ${chan||'<div style="color:var(--muted);font-size:11px">该日只有 reaped 运行行，无通道统计</div>'}
    <div class="newlist"><h4>新增节点 ${st.new_nodes||0}</h4>
      ${newN.map(n=>`<div class="item"><b>${esc(typeLabel(n.type))}</b> ${esc(clip(n.label,28))}</div>`).join('')||'<div class="item" style="color:#8c86ab">无</div>'}</div>
    <div class="newlist"><h4>新增边 ${st.new_edges||0}（含稳定关系）</h4>
      ${newE.map(e=>`<div class="item">${esc(e.relation)} · ${esc(clip(byLabelFallback(e.from),18))} → ${esc(clip(byLabelFallback(e.to),18))}</div>`).join('')||'<div class="item" style="color:#8c86ab">无</div>'}</div>`;
}

const LABELS=new Map(DATA.nodes.map(n=>[n.id,n.label]));
function byLabelFallback(id){return LABELS.get(id)||id;}

function mount(){
  if(cy) cy.destroy();
  cy=cytoscape({container:$('#cy'),elements:build(),style:STYLE,
    layout:{name:'cose',animate:false,fit:true,padding:28,randomize:true,nodeRepulsion:8000,idealEdgeLength:66,gravity:.2,numIter:700},
    wheelSensitivity:.18,minZoom:.08,maxZoom:3});
  cy.on('mouseover','node',evt=>{
    const d=evt.target.data();
    const tipEl=$('#tip'); tipEl.innerHTML=`<b>${esc(d.label)}</b><br>${esc(typeLabel(d.type))} · ${esc(d.first_day)} → ${esc(d.last_day)}<br><span style="color:#8c86ab">${esc(d.id)}</span>`;
    const e=evt.originalEvent||{}; tipEl.style.left=((e.clientX||0)+14)+'px'; tipEl.style.top=((e.clientY||0)+14)+'px'; tipEl.style.opacity=1;
  });
  cy.on('mouseout','node',()=>{$('#tip').style.opacity=0});
  apply(DAYS[idx],DAYS[idx-1]);
}

function init(){
  if(!DAYS.length){
    $('#dayBig').textContent='—';
    $('#dayMeta').innerHTML='暂无抽取日';
    $('#rail').innerHTML='<div style="color:var(--muted);font-size:11px;padding:8px">暂无抽取日</div>';
    $('#detail').innerHTML='<h3>当日详情</h3><div style="color:var(--muted);font-size:11px">还没有 KG 抽取运行记录。先跑一次抽取。</div>';
    $('#slider').max=0; $('#slider').disabled=true; $('#play').disabled=true;
    return;
  }
  $('#slider').max=DAYS.length-1;
  $('#slider').oninput=ev=>{ stop(); idx=+ev.target.value; apply(DAYS[idx],DAYS[idx-1]); };
  $('#ticks').innerHTML=DAYS.map((d,i)=>`<span data-i="${i}">${esc(dayLabel(d))}</span>`).join('');
  document.querySelectorAll('#ticks span').forEach(el=>el.onclick=()=>{ stop(); idx=+el.dataset.i; apply(DAYS[idx],DAYS[idx-1]); });
  $('#play').onclick=()=>{ if(timer){stop();return;} idx=0; apply(DAYS[0]); $('#play').textContent='❚❚';
    timer=setInterval(()=>{ idx++; if(idx>=DAYS.length){stop();return;} apply(DAYS[idx],DAYS[idx-1]); },1300); };
  $('#hide-iso').onchange=ev=>{ state.hideIso=ev.target.checked; mount(); };
  mount();
}
function stop(){ if(timer){clearInterval(timer);timer=null;} $('#play').textContent='▶'; }
init();
"""

D4_CSS = r"""
:root{--bg:#f3f1ec;--card:#fffefa;--ink:#191814;--muted:#736f66;--line:#ddd8cd;--red:#b3261e;--amber:#a16207;--teal:#0f766e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:13.5px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}
header{padding:40px 48px 26px;border-bottom:2px solid var(--ink)}
.kicker{font-size:10px;letter-spacing:.3em;text-transform:uppercase;color:var(--red);font-weight:700}
h1{font:600 42px/1.05 Georgia,"Songti SC","SimSun",serif;margin:8px 0 10px}
.lede{color:var(--muted);max-width:820px}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-top:24px}
.kpi{background:var(--card);border:1px solid var(--line);padding:12px 14px}
.kpi b{display:block;font:600 26px/1.1 Georgia,serif;font-variant-numeric:tabular-nums}
.kpi span{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.kpi.alert b{color:var(--red)}
main{max-width:1380px;margin:0 auto;padding:28px 48px 80px;display:grid;grid-template-columns:1fr 1fr;gap:18px}
.card{background:var(--card);border:1px solid var(--line);padding:20px 22px}
.card.wide{grid-column:1/-1}
h2{font:600 20px/1.2 Georgia,serif;margin:0 0 4px}
.sub{color:var(--muted);font-size:11.5px;margin-bottom:16px}
.warn{border-left:3px solid var(--red);background:#b3261e08;padding:9px 12px;font-size:11.5px;margin:10px 0 16px;line-height:1.65}
.warn b{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:600}
td.mono,span.mono{font-family:Consolas,monospace;font-size:10px;color:#6b675e}
.score{display:inline-block;width:64px;height:5px;background:#e7e2d7;vertical-align:middle;margin-right:6px}
.score i{display:block;height:5px;background:var(--amber)}
.badge{display:inline-block;font-size:9px;padding:1px 6px;border:1px solid var(--line);border-radius:99px;color:var(--muted)}
.badge.red{color:var(--red);border-color:#e6b8b4}
.badge.green{color:var(--teal);border-color:#b7d9d5}
.badge.amber{color:var(--amber);border-color:#e5d3ae}
.cluster{display:grid;grid-template-columns:150px 1fr;gap:10px;padding:9px 0;border-top:1px dashed var(--line)}
.cluster .name{font-weight:650}
.members{display:flex;flex-wrap:wrap;gap:5px}
.member{font-size:10px;border:1px solid var(--line);padding:2px 7px;border-radius:99px;background:#faf7ef}
.member b{font-weight:600}
.bars{display:flex;flex-direction:column;gap:7px;margin-top:6px}
.brow{display:grid;grid-template-columns:88px 1fr 42px;gap:8px;align-items:center;font-size:11px}
.brow .t{color:var(--muted)}
.track{height:8px;background:#ece7dc;position:relative;border-radius:2px}
.track i{position:absolute;inset:0 auto 0 0;background:var(--ink);border-radius:2px}
.track.alt i{background:var(--teal)}
.iso-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:5px;max-height:260px;overflow:auto;padding-right:4px}
.iso{font-size:10px;border:1px solid var(--line);padding:3px 7px;background:#faf7ef;line-height:1.35}
.iso b{display:block;font-size:8.5px;color:var(--muted);font-weight:500;letter-spacing:.06em;text-transform:uppercase}
.run{display:grid;grid-template-columns:70px 64px 88px 1fr;gap:8px;align-items:center;padding:6px 0;border-top:1px dashed var(--line);font-size:11px}
.run .d{font-family:Consolas,monospace}
.run .s{font-size:9px;letter-spacing:.08em;text-transform:uppercase}
.run .s.ok{color:var(--teal)}.run .s.partial{color:var(--amber)}.run .s.failed{color:var(--red)}
.run .chan{display:flex;flex-wrap:wrap;gap:4px}
.run .chan span{font-size:9px;border:1px solid var(--line);padding:1px 5px;border-radius:99px}
.run .chan span.bad{color:var(--red);border-color:#e6b8b4}
.split{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media (max-width:1000px){main{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(3,1fr)}.split{grid-template-columns:1fr}}
"""

D4_BODY = r"""
<header>
  <div class="kicker">KG audit · 全量快照</div>
  <h1>图谱体检报告</h1>
  <p class="lede">不追求“好看”：把 <code>kg_*</code> 表的结构性问题直接摆出来。所有结论都由当前 <code>langTrack.db</code> 现算，不是示例数据。</p>
  {{KG_NAV}}
  <div class="kpis" id="kpis"></div>
</header>
<main>
  <section class="card wide">
    <h2 id="eff-title">① 实体消解</h2>
    <div class="sub"><span class="mono">kg_aliases</span> 只追加不修改；“生效”按 <span class="mono">resolve_alias</span> 的规则判定：fuzzy 分数 &lt; 0.85 的候选会被跳过。</div>
    <div id="alias-effective"></div>
    <div class="warn" id="alias-warn"></div>
    <div id="alias-candidates"></div>
  </section>

  <section class="card">
    <h2>② 同名实体</h2>
    <div class="sub">Person 表里 label 完全相同、id 不同的节点。exact 规则会把它们合并，但节点本体仍保留原 id。</div>
    <div id="dups"></div>
  </section>

  <section class="card">
    <h2>③ 孤立节点</h2>
    <div class="sub" id="iso-sub"></div>
    <div class="iso-grid" id="iso"></div>
  </section>

  <section class="card wide">
    <h2>④ 图分布</h2>
    <div class="split">
      <div><div class="sub">节点类型</div><div class="bars" id="type-bars"></div></div>
      <div><div class="sub">边层级 / 关系类型 TOP</div><div class="bars" id="rel-bars"></div></div>
    </div>
  </section>

  <section class="card wide">
    <h2>⑤ 运行健康</h2>
    <div class="sub">每天取最新一次 <span class="mono">kg_runs</span>；通道失败会直接体现在右侧。</div>
    <div id="runs"></div>
  </section>
</main>
"""

D4_JS = r"""
const C=DATA.audit.counts;
const kpis=[
  ['节点',C.nodes,''],['事件/待办边',C.edges,''],['稳定关系行',C.relation_rows,'去重后 '+C.relation_unique],
  ['孤立节点',C.isolated,'占 '+(C.nodes?Math.round(C.isolated/C.nodes*100):0)+'%'],
  ['消解候选',C.alias_candidates,'alias 行 '+C.alias_rows],
  ['同名组',C.duplicate_groups,'Person']
];
$('#kpis').innerHTML=kpis.map(([k,v,s])=>`<div class="kpi ${k==='孤立节点'||k==='消解候选'?'alert':''}">
  <b>${v}</b><span>${esc(k)}</span>${s?`<div style="font-size:9.5px;color:var(--muted)">${esc(s)}</div>`:''}</div>`).join('');

const eff=DATA.aliases.effective;
const cand=DATA.aliases.candidates;
$('#eff-title').textContent=`① 实体消解：生效 ${eff.length} 条 / 候选 ${cand.length} 条`;
const topCand=cand.slice(0,3).map(c=>`${esc(c.alias_label)} → ${esc(c.canonical_label)}`).join('、');
$('#alias-warn').innerHTML=`<b>候选 ≠ 生效。</b> 表里还留着 ${cand.length} 条 0.6~0.85 的 fuzzy 候选${topCand?`（当前最高分：${topCand}）`:''}。如果某个消费方直接按 <span class="mono">kg_aliases</span> JOIN 而不调用 <span class="mono">resolve_alias</span>，就会把低分候选误合并。`;
$('#alias-effective').innerHTML=eff.length?`<table><tr><th>alias</th><th>→ canonical</th><th>最终</th><th>规则</th></tr>
  ${eff.map(e=>`<tr><td>${esc(e.alias_label)} <span class="mono">${esc(e.alias)}</span></td>
  <td>${esc(e.canonical_label)} <span class="mono">${esc(e.canonical)}</span></td>
  <td>${esc(e.final_label)}</td><td><span class="badge green">生效</span></td></tr>`).join('')}</table>`
  :'<div class="sub">没有生效映射</div>';

$('#alias-candidates').innerHTML=`<table><tr><th>候选 alias</th><th>→ canonical</th><th>score</th><th>判定</th></tr>
  ${cand.map(c=>`<tr><td>${esc(c.alias_label)} <span class="mono">${esc(c.alias)}</span></td>
  <td>${esc(c.canonical_label)} <span class="mono">${esc(c.canonical)}</span></td>
  <td><span class="score"><i style="width:${Math.round(c.score*100)}%"></i></span>${c.score}</td>
  <td><span class="badge amber">候选 · 不生效</span></td></tr>`).join('')}</table>`;

$('#dups').innerHTML=DATA.aliases.duplicate_groups.map(g=>`<div class="cluster">
  <div><div class="name">${esc(g.label)}</div><div class="sub" style="margin:0">${g.ids.length} 个 id</div></div>
  <div class="members">${g.ids.map((id,i)=>`<span class="member"><b>${esc(id)}</b> · degree ${g.degrees[i]}</span>`).join('')}</div>
</div>`).join('')||'<div class="sub">没有同名 Person</div>';

const iso=DATA.audit.isolated;
const byType=Object.entries(iso.by_type).sort((a,b)=>b[1]-a[1]);
$('#iso-sub').innerHTML=`共 ${iso.count} 个（${C.nodes?Math.round(iso.count/C.nodes*100):0}%）。按类型：${byType.map(([t,n])=>`${esc(typeLabel(t))} ${n}`).join(' · ')}。这些节点只存在于抽取结果里，没有任何边引用它们。`;
$('#iso').innerHTML=iso.sample.map(n=>`<div class="iso" title="${esc(n.id)}"><b>${esc(typeLabel(n.type))} · ${esc(n.first_day)}</b>${esc(clip(n.label,30))}</div>`).join('')
  + (iso.sample.length<iso.count?`<div class="iso" style="display:flex;align-items:center;color:var(--muted)">还有 ${iso.count-iso.sample.length} 个未展示</div>`:'');

function bars(obj, el, alt){
  const max=Math.max(1,...Object.values(obj));
  $(el).innerHTML=Object.entries(obj).map(([k,v])=>`<div class="brow"><span class="t">${esc(k.includes('_')?k:typeLabel(k))}</span>
    <span class="track ${alt?'alt':''}"><i style="width:${Math.max(2,v/max*100)}%"></i></span><span style="text-align:right">${v}</span></div>`).join('');
}
bars(DATA.audit.type_dist,'#type-bars');
bars(DATA.audit.relation_dist,'#rel-bars',true);

$('#runs').innerHTML=DATA.runs.map(r=>{
  const chan=r.channels.map(c=>`<span class="${c.status==='failed'?'bad':''}" title="${esc(c.error||'')}">${esc(c.key)} ${c.rows||0}</span>`).join('');
  return `<div class="run"><span class="d">${esc(r.day)}</span><span class="s ${esc(r.status)}">${esc(r.status)}</span>
    <span style="color:var(--muted);font-size:10px">${(r.material_chars||0).toLocaleString()} 字符</span>
    <span class="chan">${chan||'<span style="border:0;color:var(--muted)">仅 reaped 行</span>'}</span></div>`;
}).join('');
"""