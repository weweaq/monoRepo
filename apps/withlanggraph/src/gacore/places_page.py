"""/places 语义地点编辑页（v3.5）：页面模板与交互脚本。

设计定稿 = D（待办驱动主从式）+ C（地图点选）：
- 左：待办 / 全部 / 区县 / 地图 四 tab；右：编辑面板（自定义标签 + 地点 note +
  区县 note + 实时渲染预览），保存即写库。
- 自定义标签直写 places.label（DB 即真源，v3.5 无叠加层）；「家」「公司」精确值
  参与计算，UI 上以提示与快捷按钮防错别字。
- 数据层 gacore.langTrack.place_semantics.editor_data；
  保存 API：POST /api/places/labels、POST /api/places/districts（token 保护）。
"""
from __future__ import annotations

import html
import json

_PLACES_CSS = """
.pl-layout{display:flex;gap:14px;align-items:flex-start}
.pl-side{width:470px;flex:none;background:var(--card,#fff);border:1px solid var(--line,#d0d7de);border-radius:6px;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto}
.pl-tabs{display:flex;border-bottom:1px solid var(--line,#d0d7de)}
.pl-tabs button{flex:1;border:0;background:none;padding:8px 0;font:inherit;cursor:pointer;border-bottom:2px solid transparent}
.pl-tabs button.on{border-bottom-color:var(--acc,#0969da);color:var(--acc,#0969da);font-weight:600}
.pl-list{max-height:66vh;overflow:auto;position:relative}
.pl-card{padding:8px 12px;border-bottom:1px solid var(--line,#d0d7de);cursor:pointer}
.pl-card:hover{background:#eff2f5}
.pl-card.on{background:#ddf4ff;box-shadow:inset 3px 0 0 var(--acc,#0969da)}
.pl-card .l1{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.pl-card .name{font-weight:600}
.pl-card .meta{font-size:12px;color:var(--mut,#57606a)}
.pl-tag{display:inline-block;font-size:12px;line-height:18px;border-radius:10px;padding:0 8px;white-space:nowrap}
.pl-tag.t-home{background:#dafbe1;color:#1a7f37}
.pl-tag.t-work{background:#ddf4ff;color:#0969da}
.pl-tag.t-unk{background:#eff2f5;color:#57606a}
.pl-tag.t-custom{background:#fff1e5;color:#9a6700;border:1px solid #ffb964}
.pl-pane{flex:1;background:var(--card,#fff);border:1px solid var(--line,#d0d7de);border-radius:6px;padding:16px 18px;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto;min-width:0}
.pl-kv{display:grid;grid-template-columns:96px 1fr;gap:4px 12px;margin:10px 0;font-size:13px}
.pl-kv .k{color:var(--mut,#57606a)}
.pl-fld{margin:10px 0}
.pl-fld label{display:block;font-size:12px;color:var(--mut,#57606a);margin-bottom:3px}
.pl-fld input,.pl-fld textarea{width:100%;padding:5px 8px;border:1px solid var(--line,#d0d7de);border-radius:4px;font:inherit}
.pl-fld textarea{resize:vertical;min-height:64px}
.pl-prev{border:1px dashed var(--line,#d0d7de);border-radius:6px;padding:10px 12px;background:#f6f8fa;margin:10px 0}
.pl-prev pre{background:#0d1117;color:#c9d1d9;padding:10px 12px;border-radius:6px;font-size:12.5px;white-space:pre-wrap;word-break:break-all;margin:4px 0 0}
.pl-hint{font-size:12px;color:#9a6700;background:#fff8c5;border-radius:4px;padding:1px 8px}
.pl-map{width:100%;height:520px;border-radius:6px;border:1px solid var(--line,#d0d7de)}
.pl-mapwrap{position:relative;margin:8px}
.pl-todo-h{font-size:12px;color:var(--mut,#57606a);padding:8px 12px 2px}
.pl-drow{padding:10px 12px;border-bottom:1px solid var(--line,#d0d7de);cursor:pointer}
.pl-drow:hover{background:#eff2f5}
.pl-note{color:#9a6700;font-size:13px;margin:6px 0}
.pl-ghost{background:#fff;color:#0969da;border:1px solid #d0d7de;border-radius:6px;padding:2px 10px;font-size:12px;cursor:pointer}
.pl-fld select{width:100%;padding:5px 8px;border:1px solid var(--line,#d0d7de);border-radius:4px;font:inherit;background:#fff}
.pl-cur{font-size:12px;color:var(--mut,#57606a);margin:4px 0 0}
.pl-cur b{color:#1f2328}
.pl-row{display:flex;gap:8px;align-items:center;margin-top:4px;flex-wrap:wrap}
.pl-box{border:1px solid var(--line,#d0d7de);border-radius:6px;padding:10px 12px;margin:10px 0;background:#fbfdff}
.pl-box h4{margin:0 0 6px;font-size:13px}
.pl-warn{color:#9a6700}
.pl-chip{display:inline-block;font-size:12px;line-height:18px;border:1px solid var(--line,#d0d7de);border-radius:10px;padding:0 8px;margin:2px 4px 2px 0;background:#fff;cursor:pointer}
.pl-chip:hover{background:#ddf4ff;border-color:#0969da}
.pl-chip b{font-weight:600}
"""

# 交互脚本。__DATA__ 占位符在 _places_page 内替换为内嵌 JSON（已做 </ 转义）。
_PLACES_JS = r"""
(function(){
var D=__DATA__;
var map=null;
function esc(s){var d=document.createElement('div');d.textContent=String(s);return d.innerHTML;}
function tagCls(l){return l==='家'?'t-home':(l==='公司'?'t-work':(l==='未知'?'t-unk':'t-custom'));}
function tagHtml(l){return '<span class="pl-tag '+tagCls(l)+'">'+esc(l)+'</span>';}
function regionOf(p){var d=p.district||'';if(!d)return '';var a=(p.address||'');var i=a.indexOf(d);return i>=0?a.slice(0,i+d.length):d;}
function effTag(p){return (p.label&&p.label!=='未知')?p.label:'';}
function fmtLine(p,eff){var name=p.poi||'未知地点';var r=regionOf(p);
  return name+(eff?('〔'+eff+'〕'):'')+(r?('（'+r+'）'):'');}
function fmtDay(ms){if(!ms)return '-';var d=new Date(ms);
  return ('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2);}
function configured(p){return (p.label&&p.label!=='未知')||!!(p.note||'').trim();}
function firstSeenRecent(p){return !!p.first_seen&&(Date.now()-p.first_seen)<14*86400000;}
function todoList(){
  var conf=D.places.filter(function(p){return !configured(p);});
  var nw=conf.filter(firstSeenRecent);
  var rest=conf.filter(function(p){return nw.indexOf(p)<0;}).slice(0,5);
  var items=nw.concat(rest);
  var names={};D.places.forEach(function(p){var k=(p.poi||'').trim();if(k)names[k]=(names[k]||0)+1;});
  var dups=Object.keys(names).filter(function(k){return names[k]>1;});
  D.places.forEach(function(p){if(dups.indexOf((p.poi||'').trim())>=0&&items.indexOf(p)<0)items.push(p);});
  return {items:items,dups:dups};
}
var cur=null,curDist=-1,tab='todo',q='';
function cardHtml(p){
  var rc=(p.recent||[]).map(function(x){return x.day.slice(5);}).join(' ');
  var isNew=firstSeenRecent(p)&&!configured(p);
  return '<div class="l1"><span class="name">'+esc(p.poi||'(无名)')+'</span>'+tagHtml(p.label)
    +(isNew?'<span class="pl-tag t-custom">新</span>':'')+'</div>'
    +'<div class="meta">'+esc(regionOf(p))+' · 访问 '+p.vc+' 次 · 最近停留 '
    +esc(p.last_day?fmtDay(p.last_day):fmtDay(p.last_seen))
    +(rc?(' · 近14天: '+esc(rc)):'')+'</div>';
}
function renderList(){
  var el=document.getElementById('pllist');el.innerHTML='';
  var qs=q.toLowerCase();
  function match(p){if(!qs)return true;
    var hay=(p.poi+' '+p.address+' '+p.district+' '+p.label+' '+(p.note||'')).toLowerCase();
    return hay.indexOf(qs)>=0;}
  if(tab==='dist'){
    D.districts.forEach(function(d,i){
      var dv=document.createElement('div');dv.className='pl-drow';
      dv.innerHTML='<b>'+esc(d.district)+'</b> <span class="meta">'+d.n+' 个点</span>'
        +'<div class="meta" style="margin-top:2px">'+(d.note?esc(d.note.slice(0,50)):'<span style="color:#9a6700">未写背景，点此填写</span>')+'</div>';
      dv.onclick=function(){pickDistrict(i);};el.appendChild(dv);});
    return;
  }
  if(tab==='map'){
    var wrap=document.createElement('div');wrap.className='pl-mapwrap';
    wrap.innerHTML='<div class="pl-map" id="plmap"></div>';
    el.appendChild(wrap);initMap();return;
  }
  var items, hint=null;
  if(tab==='todo'){
    var tl=todoList();items=tl.items;
    hint=document.createElement('div');hint.className='pl-todo-h';
    hint.textContent='先配这几个就够了：新出现的点 + 高频未配置 Top5'
      +(tl.dups.length?(' + 同名歧义：'+tl.dups.join('、')):'')+'。配置完自动出队。';
    el.appendChild(hint);
  }else{items=D.places;}
  items=items.filter(match);
  if(!items.length){el.innerHTML+='<div style="padding:14px" class="meta">（无——待办已清空或搜索无匹配）</div>';return;}
  items.forEach(function(p){
    var dv=document.createElement('div');dv.className='pl-card'+(cur===p?' on':'');
    dv.innerHTML=cardHtml(p);dv.onclick=function(){pickPlace(p);};el.appendChild(dv);});
}
function pickPlace(p){cur=p;curDist=-1;renderList();renderEditor();}
function pickDistrict(i){cur=null;curDist=i;renderList();renderDistrictEditor(i);}
function candLabel(c){
  var d=(c.distance_m==null)?'':(' '+Math.round(c.distance_m)+'m');
  return '['+c.kind+d+']';
}
function candList(p){
  var cs=(p.poi_candidates||[]).filter(function(c){return c&&c.name;});
  if(cs.length)return cs;
  return (p._cands||[]);   // 本次会话按有效坐标现查回来的候选
}
function ruleText(){
  var m=(D.aoi_contains_max_m==null)?25:D.aoi_contains_max_m;
  return '规则：POI 与 AOI 按高德距离比较，AOI ≤'+m
    +'m（说明你确实在该地块内）时取 AOI 名，否则取最近 POI；人工改判优先于两者。';
}
function sourceText(p){
  var ev=(p.name_evidence||'').trim();
  if(!ev)return '';
  if(ev==='regeo:AOI')return '高德 regeo 命中 AOI（点在该地块内）';
  if(ev==='regeo:POI')return '高德 regeo 命中 POI';
  if(ev==='regeo:around')return '高德 regeo 无命中，周边搜索兜底';
  return ev.replace('regeo:','高德 regeo 仅命中');
}
function selName(p){
  // 与后端 resolve_place_name 同序：人工覆盖 > 自动 poi > 地址
  return ((p.poi_override||'').trim()||(p.poi||'').trim()||(p.address||'').trim());
}
function renderNameBox(p){
  var el=document.getElementById('pl_namebox');if(!el)return;
  var cs=candList(p);
  var ov=(p.poi_override||'').trim();
  var chips=cs.slice(0,6).map(function(c){
    return '<span class="pl-chip" data-n="'+esc(c.name)+'" data-loc="'+esc(c.location||'')+'">'
      +esc(c.name)+'<b> '+candLabel(c)+'</b></span>';}).join('');
  el.innerHTML='<div class="pl-cur">当前采用名：<b>'+esc(selName(p)||'（空）')+'</b>'
      +(ov?' <span class="pl-tag t-custom">人工改判</span>':'')
      +' ｜ 自动判定：<b>'+esc(p.poi||'（空）')+'</b>'
      +(p.name_evidence?(' · '+esc(p.name_evidence)):'')
      +'</div>'
    +'<div class="pl-cur">'+esc(sourceText(p))+'</div>'
    +(chips?('<div style="margin-top:4px"><span class="pl-cur" style="display:block">候选（点一个即填入下面改判框）：</span>'+chips+'</div>')
      :'<div class="pl-cur">（该点无候选记录：用下面「按坐标校准」向高德现查）</div>')
    +'<div class="pl-cur" style="color:#57606a">'+esc(ruleText())+'</div>';
  el.querySelectorAll('.pl-chip').forEach(function(b){b.onclick=function(){
    document.getElementById('pl_pov').value=b.dataset.n;
    var loc=(b.dataset.loc||'').split(',');
    if(loc.length===2){   // 候选自带坐标（搜索候选）→ 顺便填坐标修正
      document.getElementById('pl_lon').value=parseFloat(loc[0]).toFixed(6);
      document.getElementById('pl_lat').value=parseFloat(loc[1]).toFixed(6);
    }
    document.getElementById('pl_pov').dispatchEvent(new Event('input'));
    toast2('已填入「'+b.dataset.n+'」，点「保存修正」生效');};});
}
function effName(p){
  // 显示名 = 人工覆盖 > 自动 poi > 地址（与后端 resolve_place_name 同序）
  if((p.poi_override||'').trim())return p.poi_override.trim();
  if((p.poi||'').trim())return p.poi.trim();
  return (p.address||'').trim();
}
function named(p){return p.poi||'';}
function mergeOptions(p){
  // 候选被并点：同名优先，其余按距离升序（<=800m），排除自己
  var out=[];
  D.places.forEach(function(x){
    if(x===p||x.place_id===p.place_id)return;
    if(x.device_id&&p.device_id&&x.device_id!==p.device_id)return;
    var d=hav(x.eff_lat,x.eff_lon,p.eff_lat,p.eff_lon);
    if(d>800)return;
    out.push({p:x,d:d,same:!!(named(x)&&named(p)&&named(x)===named(p))});
  });
  out.sort(function(a,b){
    if(a.same!==b.same)return a.same?-1:1;
    return a.d-b.d;});
  return out.slice(0,30);
}
function hav(la1,lo1,la2,lo2){
  if(la1==null||lo1==null||la2==null||lo2==null)return 1e9;
  var R=6371000,r=Math.PI/180;
  var dla=(la2-la1)*r,dlo=(lo2-lo1)*r;
  var a=Math.sin(dla/2)*Math.sin(dla/2)
    +Math.cos(la1*r)*Math.cos(la2*r)*Math.sin(dlo/2)*Math.sin(dlo/2);
  return 2*R*Math.asin(Math.sqrt(a));
}
function renderEditor(){
  var el=document.getElementById('plpane');
  if(!cur){el.innerHTML='<h2 style="margin-top:0">编辑</h2><div class="meta">← 左侧选一个地点（键盘 ↑↓ 可导航）。「待办」tab 列出了最值得先配的点。</div>';return;}
  var p=cur;var dnote='';
  D.districts.forEach(function(d){if(d.district===p.district)dnote=d.note||'';});
  var opts=mergeOptions(p);
  var optHtml=opts.map(function(o,i){
    return '<option value="'+i+'">'+esc((o.same?'同名 · ':'')+(named(o.p)||'(无名)'))+' · '
      +Math.round(o.d)+'m · '+o.p.vc+' 次</option>';}).join('');
  el.innerHTML='<h2 style="margin-top:0">'+esc(effName(p)||'(无名地点)')+'</h2>'
    +'<div class="pl-kv">'
    +'<span class="k">place_id</span><span>'+(p.place_id?esc(p.place_id):'<i>无（v1 网格点，按名称匹配）</i>')+'</span>'
    +'<span class="k">区域</span><span>'+esc(regionOf(p))+(p.township?(' · '+esc(p.township)):'')+'</span>'
    +'<span class="k">完整地址</span><span>'+esc(p.address||'—')+(p.address_override?' <span class="pl-tag t-custom">已人工改判</span>':'')+'</span>'
    +'<span class="k">访问</span><span>'+p.vc+' 次 · 最近停留 '+esc(p.last_day?fmtDay(p.last_day):fmtDay(p.last_seen))+'</span>'
    +'<span class="k">坐标</span><span class="num">'+p.eff_lat+', '+p.eff_lon
      +(p.has_coord_override?' <span class="pl-tag t-custom">已修正</span>':'')+'</span>'
    +'</div>'

    // ---- 地名（自动 vs 人工改判）----
    +'<div class="pl-box"><h4>地点名</h4>'
    +'<div id="pl_namebox"></div>'
    +'<div class="pl-fld"><label>人工改判地名（写库即真源；留空 = 回到自动判定）</label>'
    +'<input id="pl_pov" maxlength="120" value="'+esc(p.poi_override||'')+'" placeholder="如：观云润府">'
    +'<div class="pl-row"><button class="pl-ghost" id="pl_lookup">按坐标校准（问高德）</button>'
    +'<input id="pl_kw" placeholder="关键词搜索（可空）" style="flex:1;min-width:120px;padding:4px 8px;border:1px solid #d0d7de;border-radius:4px">'
    +'</div><select id="pl_cands" style="display:none"></select>'
    +'<div class="pl-cur" id="pl_candinfo"></div></div></div>'

    +'<div class="pl-fld"><label>自定义标签（写库即真源；<span class="pl-hint">「家」「公司」参与计算：深夜在外判定/停留分桶/异地基准</span>；留空恢复「未知」）</label>'
    +'<input id="pl_label" maxlength="24" value="'+esc((p.label&&p.label!=='未知')?p.label:'')+'" placeholder="如：张威的老家 / 公司 / 家">'
    +'<div style="margin-top:4px"><button class="pl-ghost" data-q="家">标为家</button> <button class="pl-ghost" data-q="公司">标为公司</button></div></div>'
    +'<div class="pl-fld"><label>地点 note（背景故事，进日报「地点背景」行；不参与命名与计算）</label>'
    +'<textarea id="pl_note" maxlength="500">'+esc(p.note||'')+'</textarea></div>'
    +'<div class="pl-fld"><label>区县背景——'+esc(p.district||'（无区县）')+'（按区县共享，随本表单一并保存）</label>'
    +'<textarea id="pl_dnote" maxlength="500">'+esc(dnote)+'</textarea></div>'

    // ---- 坐标修正 ----
    +'<div class="pl-box"><h4>坐标 / 地址修正</h4>'
    +'<div class="pl-cur">聚类中心：'+p.lat+', '+p.lon+'（原始上报经纬度，不随修正改变）</div>'
    +'<div class="pl-row"><input id="pl_lat" value="'+p.eff_lat+'" style="width:130px;padding:4px 8px;border:1px solid #d0d7de;border-radius:4px">'
    +'<input id="pl_lon" value="'+p.eff_lon+'" style="width:130px;padding:4px 8px;border:1px solid #d0d7de;border-radius:4px">'
    +'<button class="pl-ghost" id="pl_resetcoord">恢复自动坐标</button></div>'
    +'<div class="pl-fld"><label>地址覆盖（留空 = 用高德地址）</label>'
    +'<input id="pl_aov" maxlength="120" value="'+esc(p.address_override||'')+'"></div>'
    +'<div class="pl-row"><button id="pl_savecorr">保存修正</button>'
    +'<button class="pl-ghost" id="pl_clearcorr">清空修正</button>'
    +'<span class="meta" id="pl_corrsaved"></span></div></div>'

    // ---- 合并同名地点 ----
    +'<div class="pl-box"><h4>合并同名地点</h4>'
    +'<div class="pl-cur">把另一处并到这里（它的停留/网格/行程全部改挂本点，并删除该点）。'
      +'同名歧义（如"张垛"被切成两行）用这里收口。</div>'
    +(opts.length?('<div class="pl-row"><select id="pl_merge" style="flex:1">'+optHtml+'</select>'
      +'<button id="pl_domerge">合并到这里</button></div>'
      +'<div class="pl-cur" id="pl_mergeinfo"></div>')
      :'<div class="pl-cur">800m 内没有可合并的其它点</div>')
    +'</div>'

    +'<div class="pl-prev"><b style="font-size:12px">渲染预览（实时）</b><pre id="pl_prev"></pre></div>'
    +'<button id="pl_save">保存标签与 note</button> <span class="meta" id="pl_saved"></span>';

  function upd(){
    var lbl=document.getElementById('pl_label').value.trim();
    var eff=lbl||(cur.label!=='未知'?cur.label:'');
    var nm=document.getElementById('pl_pov').value.trim()||effName(cur);
    var shown={poi:nm,address:cur.address,district:cur.district};
    var r=regionOf(cur);
    var lines=['今日轨迹：'+fmtLine(shown,eff)+' 00:00-24:00；短出 2 次合计 2.4km'];
    var nt=document.getElementById('pl_note').value.trim();
    var dn2=document.getElementById('pl_dnote').value.trim();
    if(nt)lines.push('地点背景：'+fmtLine(shown,eff)+'——'+nt);
    if(dn2)lines.push('区县背景：'+cur.district+'——'+dn2);
    document.getElementById('pl_prev').textContent=lines.join('\n');
  }
  document.getElementById('pl_label').oninput=upd;
  document.getElementById('pl_note').oninput=upd;
  document.getElementById('pl_dnote').oninput=upd;
  document.getElementById('pl_pov').oninput=upd;
  el.querySelectorAll('.pl-ghost[data-q]').forEach(function(b){b.onclick=function(){
    document.getElementById('pl_label').value=b.dataset.q;upd();};});
  upd();

  // 选名依据对照（候选池来自库；缺则按有效坐标拉一次并缓存到页面数据）
  renderNameBox(cur);
  if(!(cur.poi_candidates||[]).length)fillCandidates(cur);

  // 保存标签/note（行为与 v3.5 一致）
  document.getElementById('pl_save').onclick=savePlace;

  // 按坐标校准地名
  document.getElementById('pl_lookup').onclick=lookupNames;

  // 保存人工修正
  document.getElementById('pl_savecorr').onclick=saveCorrection;
  document.getElementById('pl_resetcoord').onclick=function(){
    cur.has_coord_override=false;
    document.getElementById('pl_lat').value=cur.lat;
    document.getElementById('pl_lon').value=cur.lon;
    document.getElementById('pl_resetcoord').dataset.reset='1';
    toast2('已标记恢复自动坐标，点「保存修正」生效');};
  document.getElementById('pl_clearcorr').onclick=function(){
    document.getElementById('pl_pov').value='';
    document.getElementById('pl_aov').value='';
    document.getElementById('pl_lat').value=cur.lat;
    document.getElementById('pl_lon').value=cur.lon;
    cur.has_coord_override=false;
    document.getElementById('pl_resetcoord').dataset.reset='1';
    upd();toast2('已清空改判，点「保存修正」生效');};

  // 合并
  var mb=document.getElementById('pl_domerge');
  if(mb)mb.onclick=function(){
    var o=opts[parseInt(document.getElementById('pl_merge').value,10)];
    if(!o)return;
    if(!confirm('把「'+(named(o.p)||'(无名)')+'」('+o.p.place_id+') 合并到「'
      +(named(p)||'(无名)')+'」('+p.place_id+')？'
      +'它的 '+o.p.vc+' 次停留 / 网格将改挂到本点，该点被删除。'))return;
    postJSON('/api/places/merge',{keep_id:p.place_id,drop_id:o.p.place_id},function(j){
      if(j.ok){
        toast2('已合并：移入停留 '+(j.moved.stays||0)+' 段');
        cur=null;refreshData();
      }else toast2('合并失败：'+(j.error||''));
    });};
}
function lookupNames(){
  var kw=document.getElementById('pl_kw').value.trim();
  var info=document.getElementById('pl_candinfo');
  info.textContent='查询中…';
  var u='/api/places/name_candidates?place_id='+encodeURIComponent(cur.place_id)
    +'&device_id='+encodeURIComponent(cur.device_id||'')
    +'&keyword='+encodeURIComponent(kw);
  fetch(u).then(function(r){return r.json();}).then(function(j){
    var sel=document.getElementById('pl_cands');
    if(!j.ok){info.innerHTML='<span class="pl-warn">'+esc(j.error||'查询失败')+'</span>';sel.style.display='none';return;}
    if(!j.candidates||!j.candidates.length){info.textContent='高德未返回候选（可换关键词再搜）';sel.style.display='none';return;}
    sel.innerHTML='<option value="">— 选一个候选（按距离）—</option>'
      +j.candidates.map(function(c,i){
        return '<option value="'+i+'">'+esc(c.name)+' · '+esc(c.kind)
          +(c.distance_m!=null?(' · '+Math.round(c.distance_m)+'m'):'')
          +(c.source==='search'?' · 搜索':'')+'</option>';}).join('');
    sel.style.display='block';
    info.textContent='共 '+j.candidates.length+' 个候选（坐标 '+j.lat+', '+j.lon+'）';
    sel.onchange=function(){
      var c=j.candidates[parseInt(this.value,10)];
      if(!c)return;
      document.getElementById('pl_pov').value=c.name;
      // 搜索候选带自身坐标：一并填进坐标修正，方便"位置也偏了"的场景
      var loc=(c.location||'').split(',');
      if(c.source==='search'&&loc.length===2){
        document.getElementById('pl_lon').value=parseFloat(loc[0]).toFixed(6);
        document.getElementById('pl_lat').value=parseFloat(loc[1]).toFixed(6);
      }
      document.getElementById('pl_pov').dispatchEvent(new Event('input'));
    };
  }).catch(function(){info.textContent='网络错误';});
}
function fillCandidates(p){
  // 库里没存候选（老点/编码失败）→ 按有效坐标现查一次，缓存到页面数据
  var u='/api/places/name_candidates?place_id='+encodeURIComponent(p.place_id)
    +'&device_id='+encodeURIComponent(p.device_id||'');
  fetch(u).then(function(r){return r.json();}).then(function(j){
    if(!j.ok||!(j.candidates||[]).length)return;
    p._cands=j.candidates;
    if(cur===p)renderNameBox(p);
  }).catch(function(){});
}
function saveCorrection(){
  var body={place_id:cur.place_id,device_id:cur.device_id||'',
    poi_override:document.getElementById('pl_pov').value.trim(),
    address_override:document.getElementById('pl_aov').value.trim()};
  if(document.getElementById('pl_resetcoord').dataset.reset==='1'){body.reset_coord=true;}
  else{
    body.lat=parseFloat(document.getElementById('pl_lat').value);
    body.lon=parseFloat(document.getElementById('pl_lon').value);
    if(isNaN(body.lat)||isNaN(body.lon)){toast2('坐标非法');return;}
  }
  postJSON('/api/places/correction',body,function(j){
    if(j.ok){
      document.getElementById('pl_corrsaved').textContent='已写入数据库';
      toast2('已保存修正');
      document.getElementById('pl_resetcoord').dataset.reset='';
      refreshData();
    }else toast2('保存失败：'+(j.error||''));
  });
}
function refreshData(){
  fetch('/api/places/data').then(function(r){return r.json();}).then(function(j){
    if(!j||!j.places)return;
    D=j;cur=null;curDist=-1;renderList();renderEditor();
  }).catch(function(){});
}
function renderDistrictEditor(i){
  var d=D.districts[i];var el=document.getElementById('plpane');
  el.innerHTML='<h2 style="margin-top:0">区县背景 · '+esc(d.district)+'</h2>'
    +'<div class="pl-kv"><span class="k">常驻点</span><span>'+d.n+' 个</span></div>'
    +'<div class="pl-fld"><label>背景 note（该区县所有地点共享，进日报「区县背景」行）</label>'
    +'<textarea id="pl_dnote2" maxlength="500" style="min-height:120px">'+esc(d.note||'')+'</textarea></div>'
    +'<button id="pl_dsave">保存区县背景</button> <span class="meta" id="pl_dsaved"></span>';
  document.getElementById('pl_dsave').onclick=function(){
    var val=document.getElementById('pl_dnote2').value;
    postJSON('/api/places/districts',{items:D.districts.map(function(x,xi){
      return {district:x.district,note:xi===i?val:(x.note||'')};})},function(j){
      if(j.ok){D.districts[i].note=val;document.getElementById('pl_dsaved').textContent='已保存';
        toast2('区县背景已保存');}else toast2('保存失败：'+(j.error||''));});};
}
function savePlace(){
  var lbl=document.getElementById('pl_label').value.trim();
  var nt=document.getElementById('pl_note').value.trim();
  var dnEl=document.getElementById('pl_dnote');
  var dn=dnEl?dnEl.value.trim():'';
  postJSON('/api/places/labels',{items:[{place_id:cur.place_id||'',poi:cur.poi||'',label:lbl,note:nt}]},function(j){
    if(j.ok){
      cur.label=lbl||'未知';cur.note=nt;
      document.getElementById('pl_saved').textContent='已写入数据库';toast2('已保存');
      if(dn&&cur.district){
        var di=D.districts.findIndex(function(x){return x.district===cur.district;});
        if(di>=0){D.districts[di].note=dn;
          postJSON('/api/places/districts',{items:D.districts.map(function(x,xi){
            return {district:x.district,note:xi===di?dn:(x.note||'')};})},function(){});}}
      renderList();
    }else toast2('保存失败：'+(j.error||''));
  });
}
function postJSON(url,body,cb){
  var t=localStorage.getItem('review_token')||'';
  if(!t){t=prompt('请输入 REVIEW_TOKEN（见 .env）')||'';if(t)localStorage.setItem('review_token',t);}
  fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':t},
    body:JSON.stringify(body)}).then(function(r){return r.json();}).then(function(j){
    if(j&&j.error==='unauthorized'){localStorage.removeItem('review_token');toast2('令牌无效，请重试');}
    cb(j||{});}).catch(function(){toast2('网络错误');});
}
var _tt;
function toast2(m){var d=document.createElement('div');
  d.style.cssText='position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1f2328;color:#fff;padding:8px 18px;border-radius:8px;font-size:13px;z-index:99';
  d.textContent=m;document.body.appendChild(d);
  clearTimeout(_tt);_tt=setTimeout(function(){d.remove();},1800);}
function initMap(){
  var holder=document.getElementById('plmap');if(!holder)return;
  if(!D.amap_js_key){holder.innerHTML='<div class="meta" style="padding:14px">地图不可用：缺 AMAP_JS_KEY（.env 配置后自动启用）</div>';return;}
  if(window._AMapSecurityConfig===undefined&&D.amap_js_sec){window._AMapSecurityConfig={securityJsCode:D.amap_js_sec};}
  if(window.AMap){buildMap();return;}
  var s=document.createElement('script');
  s.src='https://webapi.amap.com/maps?v=2.0&key='+encodeURIComponent(D.amap_js_key);
  s.onload=function(){if(document.getElementById('plmap'))buildMap();};
  document.head.appendChild(s);
}
function buildMap(){
  var holder=document.getElementById('plmap');if(!holder||map)return;
  var home=D.places.find(function(p){return p.label==='家';});
  map=new AMap.Map('plmap',{zoom:home?11:9,mapStyle:'amap://styles/normal'});
  D.places.forEach(function(p){
    var color=p.label==='家'?'#1a7f37':(p.label==='公司'?'#0969da':(effTag(p)?'#e08b00':'#8c959f'));
    var m=new AMap.CircleMarker({center:[p.lon,p.lat],radius:9,fillColor:color,fillOpacity:.9,strokeColor:'#fff',strokeWeight:1.5});
    m.on('click',function(){pickPlace(p);});map.add(m);});
  var btn=document.createElement('button');btn.textContent='全国视野';
  btn.style.cssText='position:absolute;top:6px;right:14px;z-index:5;border:1px solid #d0d7de;background:#fff;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:12px';
  btn.onclick=function(){map.setFitView(null,false,[50,50,50,50]);};
  holder.parentNode.appendChild(btn);
}
document.querySelectorAll('.pl-tabs button').forEach(function(b){b.onclick=function(){
  document.querySelectorAll('.pl-tabs button').forEach(function(x){x.classList.remove('on');});
  b.classList.add('on');tab=b.dataset.tab;cur=null;curDist=-1;
  renderList();renderEditor();};});
document.getElementById('plq').oninput=function(){q=this.value.trim();renderList();};
document.addEventListener('keydown',function(ev){
  if(ev.target.tagName==='INPUT'||ev.target.tagName==='TEXTAREA')return;
  var cards=[].slice.call(document.querySelectorAll('.pl-card'));
  var i=cards.findIndex(function(x){return x.classList.contains('on');});
  if(ev.key==='ArrowDown'&&i<cards.length-1){cards[i+1].click();ev.preventDefault();}
  if(ev.key==='ArrowUp'&&i>0){cards[i-1].click();ev.preventDefault();}});
renderList();renderEditor();
})();
"""


def _places_page(cfg) -> str:
    """语义地点编辑页：自定义标签直写 places.label/note（DB 即真源，v3.5）。"""
    from gacore.langTrack import place_semantics
    from gacore.langTrack.dashboard import _amap_js_key, _amap_js_security_code
    from gacore.langTrack.geocode import AOI_CONTAINS_MAX_M

    data = place_semantics.editor_data(
        db_path=cfg.root / "data" / "langTrack.db",
        config_path=cfg.root / "data" / "place_semantics.json",
    )
    data["amap_js_key"] = _amap_js_key() or ""
    data["amap_js_sec"] = _amap_js_security_code() or ""
    # 选名规则阈值随数据下发（页面"选名依据"解释用，前端不写死 magic number）
    data["aoi_contains_max_m"] = AOI_CONTAINS_MAX_M
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")  # 防 </script> 提前闭合
    note = f'<div class="pl-note">{html.escape(data["db_note"])}</div>' if data["db_note"] else ""
    body = (
        "<h1>语义地点 · 自定义标签与背景</h1>"
        '<div class="sub">给常驻点起你自己的名字（写进数据库，日报/轨迹/停留累计直接用）。'
        "「家」「公司」两个词有计算含义（深夜在外判定、停留分桶、异地基准），其余标签纯显示与聚合。"
        "区县背景按区县共享，进日报注脚。</div>"
        + note
        + "<style>" + _PLACES_CSS + "</style>"
        '<div class="pl-layout"><div class="pl-side">'
        '<div class="pl-tabs">'
        '<button class="on" data-tab="todo">待办</button><button data-tab="all">全部</button>'
        '<button data-tab="dist">区县</button><button data-tab="map">地图</button></div>'
        '<input id="plq" placeholder="搜索名称/地址/标签…" '
        'style="width:calc(100% - 16px);margin:8px;padding:4px 8px;border:1px solid #d0d7de;border-radius:4px;font:inherit">'
        '<div class="pl-list" id="pllist"></div></div>'
        '<div class="pl-pane" id="plpane"></div></div>'
        "<script>" + _PLACES_JS.replace("__DATA__", payload) + "</script>"
    )
    return body
