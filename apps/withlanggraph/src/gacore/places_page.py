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
function renderEditor(){
  var el=document.getElementById('plpane');
  if(!cur){el.innerHTML='<h2 style="margin-top:0">编辑</h2><div class="meta">← 左侧选一个地点（键盘 ↑↓ 可导航）。「待办」tab 列出了最值得先配的点。</div>';return;}
  var p=cur;var dnote='';
  D.districts.forEach(function(d){if(d.district===p.district)dnote=d.note||'';});
  el.innerHTML='<h2 style="margin-top:0">'+esc(p.poi||'(无名地点)')+'</h2>'
    +'<div class="pl-kv">'
    +'<span class="k">place_id</span><span>'+(p.place_id?esc(p.place_id):'<i>无（v1 网格点，按名称匹配）</i>')+'</span>'
    +'<span class="k">区域</span><span>'+esc(regionOf(p))+(p.township?(' · '+esc(p.township)):'')+'</span>'
    +'<span class="k">完整地址</span><span>'+esc(p.address||'—')+'</span>'
    +'<span class="k">访问</span><span>'+p.vc+' 次 · 最近停留 '+esc(p.last_day?fmtDay(p.last_day):fmtDay(p.last_seen))+'</span>'
    +'<span class="k">坐标</span><span class="num">'+p.lat+', '+p.lon+'</span>'
    +'</div>'
    +'<div class="pl-fld"><label>自定义标签（写库即真源；<span class="pl-hint">「家」「公司」参与计算：深夜在外判定/停留分桶/异地基准</span>；留空恢复「未知」）</label>'
    +'<input id="pl_label" maxlength="24" value="'+esc((p.label&&p.label!=='未知')?p.label:'')+'" placeholder="如：张威的老家 / 公司 / 家">'
    +'<div style="margin-top:4px"><button class="pl-ghost" data-q="家">标为家</button> <button class="pl-ghost" data-q="公司">标为公司</button></div></div>'
    +'<div class="pl-fld"><label>地点 note（背景故事，进日报「地点背景」行；不参与命名与计算）</label>'
    +'<textarea id="pl_note" maxlength="500">'+esc(p.note||'')+'</textarea></div>'
    +'<div class="pl-fld"><label>区县背景——'+esc(p.district||'（无区县）')+'（按区县共享，随本表单一并保存）</label>'
    +'<textarea id="pl_dnote" maxlength="500">'+esc(dnote)+'</textarea></div>'
    +'<div class="pl-prev"><b style="font-size:12px">渲染预览（实时）</b><pre id="pl_prev"></pre></div>'
    +'<button id="pl_save">保存</button> <span class="meta" id="pl_saved"></span>';
  function upd(){
    var lbl=document.getElementById('pl_label').value.trim();
    var eff=lbl||(cur.label!=='未知'?cur.label:'');
    var r=regionOf(cur);
    var lines=['今日轨迹：'+fmtLine(cur,eff)+' 00:00-24:00；短出 2 次合计 2.4km'];
    var nt=document.getElementById('pl_note').value.trim();
    var dn2=document.getElementById('pl_dnote').value.trim();
    if(nt)lines.push('地点背景：'+fmtLine(cur,eff)+'——'+nt);
    if(dn2)lines.push('区县背景：'+cur.district+'——'+dn2);
    document.getElementById('pl_prev').textContent=lines.join('\n');
  }
  document.getElementById('pl_label').oninput=upd;
  document.getElementById('pl_note').oninput=upd;
  document.getElementById('pl_dnote').oninput=upd;
  el.querySelectorAll('.pl-ghost').forEach(function(b){b.onclick=function(){
    document.getElementById('pl_label').value=b.dataset.q;upd();};});
  upd();
  document.getElementById('pl_save').onclick=savePlace;
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

    data = place_semantics.editor_data(
        db_path=cfg.root / "data" / "langTrack.db",
        config_path=cfg.root / "data" / "place_semantics.json",
    )
    data["amap_js_key"] = _amap_js_key() or ""
    data["amap_js_sec"] = _amap_js_security_code() or ""
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
