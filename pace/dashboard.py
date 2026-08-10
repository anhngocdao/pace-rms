"""Static dashboard.

One self-contained HTML file with the run data embedded.  No build step, no
CDN, no network calls: it opens from the filesystem and it will still open in
ten years.  Everything on screen is read from out/run.json, so the numbers
cannot drift away from what the engine actually decided.
"""

import json
import os
from typing import Any, Dict

from .levels import as_payload as level_payload

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
</head><body>
<div id="app"></div>
<script id="payload" type="application/json">__DATA__</script>
<script id="levels" type="application/json">__LEVELS__</script>
<script>__JS__</script>
</body></html>
"""

CSS = r"""
:root{
  --bg:#f5f7f8; --panel:#ffffff; --ink:#14181d; --muted:#67717d; --line:#e0e5e8;
  --accent:#1f5f8b; --accent-soft:#e8f0f6; --series:#9a5b3d; --warn:#a8492e;
  --good:#2f6b4f; --grid:#e9edef; --focus:#1f5f8b;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#0f1216;--panel:#171b21;--ink:#e7eaee;--muted:#98a1ad;--line:#272c34;
        --accent:#6fb0dc;--accent-soft:#1b2c39;--series:#d09a7a;--warn:#e08a6c;
        --good:#7cc4a1;--grid:#212630;--focus:#8cc4e6;}
}
:root[data-theme=light]{--bg:#f5f7f8;--panel:#fff;--ink:#14181d;--muted:#67717d;--line:#e0e5e8;
  --accent:#1f5f8b;--accent-soft:#e8f0f6;--series:#9a5b3d;--warn:#a8492e;--good:#2f6b4f;
  --grid:#e9edef;--focus:#1f5f8b;}
:root[data-theme=dark]{--bg:#0f1216;--panel:#171b21;--ink:#e7eaee;--muted:#98a1ad;--line:#272c34;
  --accent:#6fb0dc;--accent-soft:#1b2c39;--series:#d09a7a;--warn:#e08a6c;--good:#7cc4a1;
  --grid:#212630;--focus:#8cc4e6;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
#app{max-width:1180px;margin:0 auto;padding:28px 20px 72px}
h1{font-size:23px;margin:0 0 2px;letter-spacing:-.01em;font-weight:640}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);
   margin:34px 0 12px;font-weight:640}
.sub{color:var(--muted);font-size:13.5px;margin:0 0 4px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(158px,1fr))}
.kpi .label{font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.kpi .value{font:600 27px/1.15 var(--mono);margin-top:6px;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.kpi .note{font-size:12px;color:var(--muted);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:11.5px;text-transform:uppercase;
   letter-spacing:.06em;padding:7px 9px;border-bottom:1px solid var(--line)}
td{padding:7px 9px;border-bottom:1px solid var(--grid);white-space:nowrap}
td:last-child{white-space:normal}
td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
tr.click{cursor:pointer} tr.click:hover td{background:var(--accent-soft)}
tr.click:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
tr.on td{background:var(--accent-soft)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
.cal .h{font-size:10.5px;color:var(--muted);text-align:center;padding-bottom:2px;letter-spacing:.05em}
.cell{border:1px solid transparent;border-radius:5px;padding:5px 4px 6px 8px;cursor:pointer;
  min-height:58px;position:relative;transition:transform .08s;display:block;width:100%;
  font:inherit;color:inherit;text-align:left}
.cell:hover{transform:translateY(-1px);border-color:var(--ink)}
.cell:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.cell.on{border-color:var(--ink);box-shadow:0 0 0 1px var(--ink) inset}
/* Severity reads as form before it reads as text: a full night and a night
   carrying a restriction each earn a stripe, so the calendar can be scanned
   rather than parsed. */
.cell.flag::before{content:'';position:absolute;left:2px;top:6px;bottom:6px;width:3px;
  border-radius:2px;background:var(--warn)}
.cell.full::before{background:var(--ink)}
.cell .d{font-size:10.5px;opacity:.8}
.cell .r{font:600 14px/1.25 var(--mono);margin-top:2px;font-variant-numeric:tabular-nums}
.cell .f{font-size:9.5px;letter-spacing:.03em;margin-top:3px;opacity:.9;
  display:flex;gap:3px;flex-wrap:wrap}
.chip{border:1px solid currentColor;border-radius:3px;padding:0 3px;opacity:.85;
  font-size:9px;line-height:1.5;white-space:nowrap}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
  .cell:hover{transform:none}
}
.tag{display:inline-block;font-size:10px;padding:1px 5px;border-radius:99px;
  border:1px solid var(--line);color:var(--muted);margin-right:4px;white-space:nowrap}
.tag.hot{border-color:var(--warn);color:var(--warn)}
.tag.ok{border-color:var(--good);color:var(--good)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:10px}
/* A ninety night window crosses four months. Without a month label a reader
   has to count weekday columns to work out where they are, so each month gets
   its own grid and its own name. */
.monthlab{font:600 12px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);margin:20px 0 7px}
.monthlab:first-child{margin-top:0}
.key{display:grid;gap:6px;margin-top:12px;font-size:12px;color:var(--muted)}
.key b{color:var(--ink);font-weight:600;font-family:var(--mono);font-size:11.5px}
.blurb{color:var(--muted);font-size:13px;margin:-6px 0 12px;max-width:76ch;line-height:1.55}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.pair{grid-template-columns:1fr}}
.panel h3{margin:0 0 10px;font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--muted);font-weight:640}
.mini{font-size:12.5px}
.mini td,.mini th{padding:5px 7px}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--ink)}
th.sortable:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
th[aria-sort]{color:var(--accent)}
th .caret{font-size:9px;margin-left:3px;opacity:.8}
.swatch{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}
ul.drivers{margin:10px 0 0;padding-left:17px} ul.drivers li{margin:5px 0;font-size:13.5px}
.headline{font-size:16.5px;font-weight:620;margin:2px 0 6px;letter-spacing:-.01em}
.narr{color:var(--muted);font-size:13.5px;margin-top:10px;border-left:2px solid var(--line);padding-left:12px}
.two{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}
@media(max-width:860px){.two{grid-template-columns:1fr}}
.bar{height:9px;border-radius:99px;background:var(--grid);overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent)}
.foot{color:var(--muted);font-size:12.5px;margin-top:30px;border-top:1px solid var(--line);padding-top:14px}
.toggle{position:absolute;top:26px;right:20px;font-size:12px;color:var(--muted);
  background:none;border:1px solid var(--line);border-radius:99px;padding:5px 11px;cursor:pointer}
.toggle:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
#app{position:relative}
svg{display:block;width:100%;height:auto}
.axis{font:10.5px var(--mono);fill:var(--muted)}

/* depth control: the same account of the system, pitched four ways */
.seg{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 4px}
.seg button{font:inherit;font-size:12.5px;color:var(--muted);background:var(--bg);
  border:1px solid var(--line);border-radius:99px;padding:6px 14px;cursor:pointer;
  transition:color .12s,border-color .12s,background .12s}
.seg button:hover{color:var(--ink);border-color:var(--muted)}
.seg button:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.seg button[aria-pressed=true]{background:var(--accent-soft);border-color:var(--accent);
  color:var(--accent);font-weight:640}
.seg-note{font-size:12.5px;color:var(--muted);margin:2px 0 16px;min-height:1.3em}
.topic{padding:14px 0;border-top:1px solid var(--line)}
.topic:first-of-type{border-top:0;padding-top:4px}
.topic h3{margin:0 0 6px;font-size:14.5px;font-weight:640;letter-spacing:-.005em}
.topic p{margin:0;font-size:14px;line-height:1.62;max-width:66ch;color:var(--ink)}
@media(prefers-reduced-motion:reduce){.seg button{transition:none}}
"""

JS = r"""
const D = JSON.parse(document.getElementById('payload').textContent);
const cur = D.hotel.currency;
const money = v => cur + ' ' + Math.round(v).toLocaleString();
const pct = (v,d=0) => (v*100).toFixed(d) + '%';
/* The cell already sits in a weekday column, so printing the weekday inside it
   says the same thing twice. An ordinal reads as a date on its own. */
const ord = n => n + (['th','st','nd','rd'][(n%100-20)%10] || ['th','st','nd','rd'][n%100] || 'th');
const el = (t,a,...k)=>{const n=document.createElement(t);
  for(const q in (a||{})) q==='html'?n.innerHTML=a[q]:n.setAttribute(q,a[q]);
  k.flat().forEach(c=>n.append(c&&c.nodeType?c:document.createTextNode(c==null?'':c)));return n;};
const S=(t,a)=>{const n=document.createElementNS('http://www.w3.org/2000/svg',t);
  for(const q in (a||{}))n.setAttribute(q,a[q]);return n;};

const L = JSON.parse(document.getElementById('levels').textContent);

/* Every block says what question it answers before it answers it. A heading
   like "Every night, every decision" tells a reader what they are looking at
   and nothing about why they should. */
function section(app, title, blurb, attrs){
  app.append(el('h2', attrs||{}, title));
  if(blurb) app.append(el('p',{class:'blurb'}, blurb));
}

const recs = D.recommendations;
let picked = recs.findIndex(r=>r.bid_price>0);
if(picked<0) picked = 0;
let depth = L.order[1];        /* opens at the working level, not the shallowest */
let sortKey = 'date', sortDir = 1;

/* ------------------------------------------------------- the depth control */
function levels(app){
  section(app, 'What this is, at four depths',
    'The same five ideas, written out four times. Pick the one pitched at you: '+
    'nobody should have to read a level below their own to follow the one they are on.');
  const p = el('div',{class:'panel'});
  const seg = el('div',{class:'seg',role:'group','aria-label':'Level of explanation'});
  const note = el('p',{class:'seg-note'});
  const body = el('div',{});

  function paint(){
    seg.replaceChildren();
    L.order.forEach(key=>{
      const b = el('button',{type:'button','aria-pressed':String(key===depth)},
                   L.labels[key].name);
      b.onclick = ()=>{ depth = key; paint(); };
      seg.append(b);
    });
    note.replaceChildren(document.createTextNode(L.labels[depth].for));
    body.replaceChildren();
    L.topics.forEach(t=>{
      body.append(el('div',{class:'topic '+depth},
        el('h3',{}, t.title), el('p',{}, t.levels[depth])));
    });
  }
  paint();
  p.append(seg, note, body);
  app.append(p);
}

/* ---------------------------------------------------------------- charts */
function chart(host, series, opt){
  opt = opt||{};
  const W=900, H=opt.h||230, L=46, R=opt.right?46:14, T=12, B=26;
  const svg=S('svg',{viewBox:`0 0 ${W} ${H}`,preserveAspectRatio:'none'});
  const n = series[0].v.length;
  const xs = i => L + (n<2?0:i*(W-L-R)/(n-1));
  const groups = {};
  series.forEach(s=>{(groups[s.axis||'l']=groups[s.axis||'l']||[]).push(s)});
  const scale = {};
  for(const g in groups){
    let mx = 0; groups[g].forEach(s=>s.v.forEach(v=>{if(v!=null&&v>mx)mx=v}));
    mx = mx||1; scale[g]= v => H-B-(v/(mx*1.08))*(H-T-B);
    scale[g].max = mx*1.08;
  }
  for(let i=0;i<=4;i++){
    const y=T+i*(H-T-B)/4;
    svg.append(S('line',{x1:L,x2:W-R,y1:y,y2:y,stroke:'var(--grid)','stroke-width':1}));
    const lv=(scale.l.max*(1-i/4));
    const t=S('text',{x:L-7,y:y+3.5,class:'axis','text-anchor':'end'});
    t.textContent = opt.lfmt?opt.lfmt(lv):Math.round(lv); svg.append(t);
  }
  if(scale.r) for(let i=0;i<=4;i++){
    const y=T+i*(H-T-B)/4, rv=scale.r.max*(1-i/4);
    const t=S('text',{x:W-R+7,y:y+3.5,class:'axis'});
    t.textContent = opt.rfmt?opt.rfmt(rv):Math.round(rv); svg.append(t);
  }
  series.forEach(s=>{
    const sc = scale[s.axis||'l'];
    if(s.type==='area'){
      let d='M'+xs(0)+','+(H-B);
      s.v.forEach((v,i)=>{d+='L'+xs(i)+','+sc(v||0)});
      d+='L'+xs(n-1)+','+(H-B)+'Z';
      svg.append(S('path',{d:d,fill:s.color,opacity:s.opacity||.18}));
    }
    let d='', open=false;
    s.v.forEach((v,i)=>{ if(v==null){open=false;return;}
      d += (open?'L':'M')+xs(i)+','+sc(v); open=true; });
    svg.append(S('path',{d:d,fill:'none',stroke:s.color,'stroke-width':s.w||1.8,
      'stroke-linejoin':'round','stroke-dasharray':s.dash||'none'}));
  });
  (opt.xticks||[]).forEach(([i,lab])=>{
    const t=S('text',{x:xs(i),y:H-8,class:'axis','text-anchor':'middle'});
    t.textContent=lab; svg.append(t);
  });
  if(opt.mark!=null) svg.append(S('line',{x1:xs(opt.mark),x2:xs(opt.mark),y1:T,y2:H-B,
    stroke:'var(--ink)','stroke-width':1,'stroke-dasharray':'3 3',opacity:.55}));
  host.replaceChildren(svg);
  if(opt.legend){
    const lg=el('div',{class:'legend'});
    series.filter(s=>s.name).forEach(s=>lg.append(el('span',{},
      el('span',{class:'swatch',style:'background:'+s.color}), s.name)));
    host.append(lg);
  }
}

/* ------------------------------------------------------------ rate colour */
const rates = recs.map(r=>r.rate);
const rlo = Math.min(...rates), rhi = Math.max(...rates);
function heat(r){
  const t = rhi>rlo ? (r-rlo)/(rhi-rlo) : .5;
  const a = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  return `color-mix(in srgb, ${a} ${(12+t*62).toFixed(0)}%, var(--panel))`;
}

/* ------------------------------------------------------------------- view */
function render(){
  const app = document.getElementById('app');
  app.replaceChildren();
  const s = D.backtest.scores;
  const eng = s.engine||{}, lad = s.ladder||{}, sta = s.static||{};

  app.append(el('button',{class:'toggle',id:'themebtn'},'theme'));
  app.append(el('h1',{}, D.hotel.name + ' · revenue controls'),
    el('p',{class:'sub'}, D.hotel.rooms + ' rooms, ' + D.hotel.city +
      ' · decisions as of ' + D.generated + ' for ' + D.window.first + ' to ' + D.window.last));

  /* KPIs */
  const fr = recs.reduce((a,r)=>a+r.forecast_rooms,0);
  const frev = recs.reduce((a,r)=>a+r.forecast_revpar,0)*D.hotel.rooms;
  const cap = recs.length*D.hotel.rooms;
  const tight = recs.filter(r=>r.sellout||r.forecast_occ>=.95).length;
  const restricted = recs.filter(r=>r.mlos>1||r.cta||r.closed.length).length;
  const k = el('div',{class:'grid kpis'});
  const kpi=(l,v,n)=>el('div',{class:'panel kpi'},el('div',{class:'label'},l),
    el('div',{class:'value'},v),el('div',{class:'note'},n));
  k.append(
    kpi('Forecast occupancy', pct(fr/cap,1), Math.round(fr).toLocaleString()+' room nights of '+cap.toLocaleString()),
    kpi('Forecast ADR', money(frev/fr), 'across the next '+recs.length+' nights'),
    kpi('Forecast RevPAR', money(frev/cap), 'total '+money(frev)),
    kpi('Nights forecast tight', tight, tight+' at or above 95% · '+restricted+' carry a restriction'),
    kpi('RevPAR vs incumbent', (eng.revpar_lift_vs_ladder>=0?'+':'')+pct(eng.revpar_lift_vs_ladder||0,1),
        'measured on a settled '+D.backtest.first+' to '+D.backtest.last)
  );
  app.append(k);

  levels(app);

  /* calendar, one grid per month: a ninety night window crosses four of them */
  section(app, 'The next '+recs.length+' nights',
    'One button per night, in calendar order. Click any night to see the reasoning '+
    'that produced its rate. Nights that need a human to look at them are marked, '+
    'so the window can be scanned rather than read.');
  const pan = el('div',{class:'panel'});
  const MONTHS=['January','February','March','April','May','June',
                'July','August','September','October','November','December'];
  const newGrid=()=>{const g=el('div',{class:'cal'});
    ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].forEach(d=>g.append(el('div',{class:'h'},d)));
    return g;};
  let cursor=null, cal=null;
  recs.forEach((r,i)=>{
    const dt = new Date(r.date+'T00:00:00');
    const key = dt.getFullYear()+'-'+dt.getMonth();
    if(key!==cursor){
      if(cal) pan.append(cal);
      pan.append(el('div',{class:'monthlab'}, MONTHS[dt.getMonth()]+' '+dt.getFullYear()));
      cal = newGrid();
      for(let q=0;q<(dt.getDay()+6)%7;q++) cal.append(el('div',{}));
      cursor = key;
    }
    const restricted = r.cta || r.mlos>1 || r.closed.length;
    const chips = el('div',{class:'f'});
    chips.append(el('span',{}, r.sellout?'full':pct(r.forecast_occ)));
    if(r.cta) chips.append(el('span',{class:'chip'},'CTA'));
    else if(r.mlos>1) chips.append(el('span',{class:'chip'},'MLOS '+r.mlos));
    if(r.closed.length) chips.append(el('span',{class:'chip'},'−'+r.closed.length));
    const cls = 'cell' + (i===picked?' on':'') + (r.sellout?' flag full':(restricted?' flag':''));
    const spoken = r.date+' '+r.dow+', rate '+Math.round(r.rate)+', '+
      (r.sellout?'forecast full':'forecast '+pct(r.forecast_occ))+
      (restricted?', restricted':'');
    const c = el('button',{class:cls,type:'button',style:'background:'+heat(r.rate),
      title:r.date+' '+r.dow+', '+r.headline,'aria-label':spoken},
      el('div',{class:'d'}, ord(dt.getDate())),
      el('div',{class:'r'}, Math.round(r.rate)),
      chips);
    c.onclick=()=>{picked=i;render();
      document.getElementById('detail').scrollIntoView({behavior:'smooth',block:'start'});
      const t=document.querySelector('.cell.on'); if(t) t.focus({preventScroll:true});};
    cal.append(c);
  });
  if(cal) pan.append(cal);

  /* What the marks mean. Two numbers sit in every cell and neither was
     labelled, which meant the densest thing on the page was the least
     explained thing on it. */
  pan.append(el('div',{class:'key'},
    el('div',{}, el('b',{},String(Math.round(recs[picked].rate))),
      ' the rate the engine would publish that night, in '+cur),
    el('div',{}, el('b',{}, recs[picked].sellout?'full':pct(recs[picked].forecast_occ)),
      ' occupancy it expects at that rate, or "full" when it forecasts a sellout'),
    el('div',{}, el('b',{},'MLOS 3'),' shortest stay accepted · ',
      el('b',{},'CTA'),' closed to arrival · ',
      el('b',{},'−2'),' two rate categories closed'),
    el('div',{class:'legend',style:'margin-top:2px'},
      el('span',{},el('span',{class:'swatch',style:'background:'+heat(rlo)}),
        'lowest rate in the window, '+money(rlo)),
      el('span',{},el('span',{class:'swatch',style:'background:'+heat(rhi)}),
        'highest, '+money(rhi)),
      el('span',{},el('span',{class:'swatch',style:'background:var(--ink)'}),
        'stripe: forecast full'),
      el('span',{},el('span',{class:'swatch',style:'background:var(--warn)'}),
        'stripe: a restriction is in force'))));
  app.append(pan);

  /* rate vs bid */
  section(app, 'Rate against the value of the room',
    'The published rate and the bid price, night by night. The gap between the two '+
    'lines is the margin the engine is holding: where they close, the rate is being '+
    'held up by the value of the room rather than by what demand will pay.');
  const c1 = el('div',{class:'panel'});
  const ticks=[]; recs.forEach((r,i)=>{ if(i%10===0) ticks.push([i,r.date.slice(5)]); });
  const host1=el('div',{}); c1.append(host1);
  c1.append(el('p',{class:'sub',style:'margin-top:8px'},
    'The bid price is what the last available room is worth if it is kept for later demand. '+
    'Where it rises above the rate line, price alone has run out of room and the restrictions take over.'));
  app.append(c1);
  chart(host1,[
    {name:'Recommended rate', v:recs.map(r=>r.rate), color:'var(--accent)', w:2},
    {name:'Bid price', v:recs.map(r=>r.bid_price), color:'var(--series)', type:'area', w:1.4},
    {name:'Forecast occupancy', v:recs.map(r=>r.forecast_occ), color:'var(--muted)', axis:'r', w:1.2, dash:'4 3'}
  ],{xticks:ticks,right:true,mark:picked,legend:true,h:250,
     lfmt:v=>Math.round(v),rfmt:v=>Math.round(v*100)+'%'});

  /* detail + pace */
  const r = recs[picked];
  section(app, 'Why this night is priced this way',
    'The night selected in the calendar above, unpacked. Every recommendation carries '+
    'its own reasoning, because a revenue system that cannot say why it moved the rate '+
    'gets overridden until somebody switches it off.', {id:'detail'});
  const two = el('div',{class:'two'});
  const dp = el('div',{class:'panel'});
  dp.append(el('div',{class:'sub'}, r.date+' · '+r.dow+' · '+r.lead+' days out · confidence '+r.confidence));
  dp.append(el('div',{class:'headline'}, r.headline));
  const tags=el('div',{});
  tags.append(el('span',{class:'tag'},'on the books '+r.otb+'/'+D.hotel.rooms));
  tags.append(el('span',{class:'tag'},'authorised '+r.authorized));
  if(r.bid_price>0) tags.append(el('span',{class:'tag hot'},'bid '+money(r.bid_price)));
  if(r.mlos>1) tags.append(el('span',{class:'tag hot'},'MLOS '+r.mlos));
  if(r.cta) tags.append(el('span',{class:'tag hot'},'closed to arrival'));
  r.closed.forEach(c=>tags.append(el('span',{class:'tag hot'},'closed '+c)));
  r.events.forEach(e=>tags.append(el('span',{class:'tag ok'},e)));
  dp.append(tags);
  const ul=el('ul',{class:'drivers'});
  r.drivers.forEach(d=>ul.append(el('li',{},d)));
  dp.append(ul);
  dp.append(el('div',{class:'narr'}, r.narrative));
  two.append(dp);

  const pp = el('div',{class:'panel'});
  pp.append(el('div',{class:'label',style:'font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)'},'Booking pace'));
  const hostP=el('div',{style:'margin-top:8px'}); pp.append(hostP);
  const lp = D.live_pace.find(x=>x.date===r.date);
  const cls = lp ? D.pace_curves[lp.class] : null;
  const house = D.pace_curves['House average'];
  const norm = cls||house;
  const LEADS=121;
  const obs=[], nrm=[];
  for(let i=LEADS-1;i>=0;i--){
    obs.push(lp?lp.otb_by_lead[i]:null);
    nrm.push(norm? norm.ratio[i]*r.forecast_rooms : null);
  }
  chart(hostP,[
    {name:'This night', v:obs, color:'var(--accent)', w:2},
    {name:(cls?('Typical '+lp.class):'House average'), v:nrm, color:'var(--muted)', w:1.4, dash:'4 3'}
  ],{h:200,legend:true,xticks:[[0,'120 days out'],[60,'60'],[120,'arrival']]});
  pp.append(el('p',{class:'sub',style:'margin-top:6px'},
    'Above the dashed line the night is booking ahead of its class, below it the night is running late. '+
    'The engine weighs this signal by how much of the business is normally on the books by that point.'));
  two.append(pp);
  app.append(two);

  /* backtest */
  section(app, 'Backtest · '+D.backtest.first+' to '+D.backtest.last,
    'Whether any of this works. Three pricing policies replayed against one identical '+
    'stream of booking requests, on settled nights where the outcome is already known.');
  const bp=el('div',{class:'panel'});
  const tb=el('table');
  tb.append(el('thead',{},el('tr',{},el('th',{},'Policy'),el('th',{class:'num'},'Occupancy'),
    el('th',{class:'num'},'ADR'),el('th',{class:'num'},'RevPAR'),el('th',{class:'num'},'GOPPAR'),
    el('th',{class:'num'},'Walks'),el('th',{class:'num'},'RevPAR vs incumbent'))));
  const tbody=el('tbody');
  const names={static:'Static BAR, one rate all year',ladder:'Seasonal ladder (the incumbent)',engine:'Pace engine'};
  ['static','ladder','engine'].forEach(key=>{
    const x=s[key]; if(!x) return;
    tbody.append(el('tr',{},el('td',{},names[key]),el('td',{class:'num'},pct(x.occupancy,1)),
      el('td',{class:'num'},money(x.adr)),el('td',{class:'num'},money(x.revpar)),
      el('td',{class:'num'},money(x.goppar)),el('td',{class:'num'},x.walked),
      el('td',{class:'num'},key==='ladder'?'—':((x.revpar_lift_vs_ladder>=0?'+':'')+pct(x.revpar_lift_vs_ladder,1)))));
  });
  tb.append(tbody); bp.append(el('div',{class:'scroll'},tb));
  const dm = D.backtest.denials_by_reason||{};
  const labels = {price:'walked away on price (never visible to a real hotel)',
    capacity:'refused, no room left', mlos:'refused, below the minimum stay',
    cta:'refused, closed to arrival', segment_closed:'refused, that rate was closed'};
  const dl = el('div',{style:'margin-top:16px'});
  dl.append(el('div',{class:'label',style:'font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:8px'},
    'Room nights the engine turned away, and why'));
  const dmax = Math.max(1,...Object.values(dm));
  Object.entries(dm).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>{
    const row = el('div',{style:'display:grid;grid-template-columns:64px 1fr;gap:10px;align-items:center;margin:5px 0'},
      el('div',{style:'font-family:var(--mono);font-size:12.5px;text-align:right'}, v.toLocaleString()),
      el('div',{},
        el('div',{class:'bar'}, el('i',{style:'width:'+(v/dmax*100)+'%'})),
        el('div',{style:'font-size:12px;color:var(--muted);margin-top:3px'}, labels[k]||k)));
    dl.append(row);
  });
  bp.append(dl);
  bp.append(el('p',{class:'sub',style:'margin-top:12px'},
    'All three policies were run against the identical stream of booking requests, each carrying its own '+
    'willingness to pay, so a request refused by one policy is genuinely available to another. '+
    'The market is synthetic: this measures whether the decision logic is sound, not what any real hotel would earn.'));
  app.append(bp);

  /* model */
  section(app, 'What the engine learned',
    'The parameters the engine fitted from history rather than being given. If these '+
    'look wrong, everything above them is wrong too, which is why they are on the page.');
  const mp=el('div',{class:'panel'});
  const mt=el('table');
  mt.append(el('thead',{},el('tr',{},el('th',{},'Segment'),el('th',{class:'num'},'Rooms sold'),
    el('th',{class:'num'},'ADR'),el('th',{class:'num'},'Fitted elasticity'),el('th',{class:'num'},'Prior'),
    el('th',{class:'num'},'Nights used'))));
  const mb=el('tbody');
  Object.entries(D.segments).forEach(([code,v])=>{
    mb.append(el('tr',{},el('td',{},v.name),el('td',{class:'num'},v.rooms.toLocaleString()),
      el('td',{class:'num'},money(v.adr)),
      el('td',{class:'num'}, v.elasticity_at_reference? v.elasticity_at_reference.toFixed(2):'contracted'),
      el('td',{class:'num'}, v.elasticity_prior.toFixed(2)),
      el('td',{class:'num'}, v.elasticity_observations)));
  });
  mt.append(mb); mp.append(el('div',{class:'scroll'},mt));
  const last = D.fit_log[D.fit_log.length-1]||{};
  mp.append(el('p',{class:'sub',style:'margin-top:12px'},
    'Refitted every 28 nights, most recently on '+(last.asof||'n/a')+' from '+(last.nights_of_history||0)+
    ' settled nights. Unconstrained demand runs '+
    (((last.censoring_uplift_house||1)-1)*100).toFixed(1)+'% above booked demand house-wide: that is the '+
    'business the booked history never recorded because the night was already full. '+
    'Contracted segments have no fitted elasticity because the engine does not set their rate.'));
  if(D.plugins && D.plugins.loaded.length)
    mp.append(el('p',{class:'sub'},'Extensions loaded: '+D.plugins.loaded.join(', ')+
      ' · signals: '+(D.plugins.signals.join(', ')||'none')+
      ' · rules: '+(D.plugins.rules.join(', ')||'none')));
  app.append(mp);

  /* summaries. Ninety rows is a record, not an answer: nobody reads a
     forward window one night at a time. A revenue team reads it two ways,
     down the months and across the week, so both are on the page. */
  section(app, 'How the window breaks down',
    'The same ninety nights, aggregated the two ways a revenue team actually reads '+
    'them. Down the months for the seasonal shape, across the weekday for the shape '+
    'that repeats every seven days and drives most of the restrictions.');
  const summarise = rows => {
    const n = rows.length, capacity = n*D.hotel.rooms;
    const rooms = rows.reduce((a,x)=>a+x.forecast_rooms,0);
    const revenue = rows.reduce((a,x)=>a+x.forecast_revpar,0)*D.hotel.rooms;
    return {n:n, rate:rows.reduce((a,x)=>a+x.rate,0)/n,
            occ:rooms/capacity, adr:rooms?revenue/rooms:0, revpar:revenue/capacity,
            bid:rows.reduce((a,x)=>a+x.bid_price,0)/n,
            tight:rows.filter(x=>x.sellout||x.forecast_occ>=.95).length,
            restricted:rows.filter(x=>x.mlos>1||x.cta||x.closed.length).length};
  };
  const summaryTable = (title, groups, firstHead) => {
    const box = el('div',{class:'panel'});
    box.append(el('h3',{}, title));
    const t = el('table',{class:'mini'});
    t.append(el('thead',{},el('tr',{},el('th',{},firstHead),el('th',{class:'num'},'Nights'),
      el('th',{class:'num'},'Rate'),el('th',{class:'num'},'Occ'),el('th',{class:'num'},'RevPAR'),
      el('th',{class:'num'},'Bid'),el('th',{class:'num'},'Tight'))));
    const body = el('tbody');
    groups.forEach(([label, rows])=>{
      const a = summarise(rows);
      body.append(el('tr',{}, el('td',{},label), el('td',{class:'num'},a.n),
        el('td',{class:'num'},Math.round(a.rate)), el('td',{class:'num'},pct(a.occ)),
        el('td',{class:'num'},Math.round(a.revpar)), el('td',{class:'num'},Math.round(a.bid)),
        el('td',{class:'num'}, a.tight+(a.restricted?(' / '+a.restricted):''))));
    });
    t.append(body); box.append(el('div',{class:'scroll'},t));
    return box;
  };
  const byMonth = new Map();
  recs.forEach(x=>{ const d=new Date(x.date+'T00:00:00');
    const key = MONTHS[d.getMonth()]+' '+d.getFullYear();
    if(!byMonth.has(key)) byMonth.set(key,[]); byMonth.get(key).push(x); });
  const DOW=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const byDow = DOW.map(d=>[d, recs.filter(x=>x.dow===d)]).filter(g=>g[1].length);
  const pair = el('div',{class:'pair'});
  pair.append(summaryTable('Down the months', [...byMonth.entries()], 'Month'));
  pair.append(summaryTable('Across the week', byDow, 'Day'));
  app.append(pair);
  app.append(el('p',{class:'sub',style:'margin-top:10px'},
    'Tight counts nights forecast at 95% or above. Where such a night also carries a '+
    'restriction the count appears after a slash, and ' +
    (restricted ? (restricted+' nights in this window do.')
                : 'no night in this window does: the engine is pricing its way '+
                  'through without needing to close anything.') +
    ' Bid is the average value of the last available room, which is the number the '+
    'rate has to clear.'));

  /* the record itself, sortable, because the useful question is rarely
     "what happens on the fourteenth" but "which nights are worth the most" */
  section(app, 'Every night, every decision',
    'The full record, one row per night. Sort by any column: the bid price column '+
    'ranks the window by what a room is actually worth, which is the order a revenue '+
    'manager would work through it in. Click a row to open its reasoning above.');
  const COLS=[
    {k:'date',   h:'Date',  get:x=>x.date,  cell:x=>x.date},
    {k:'dow',    h:'Day',   get:x=>DOW.indexOf(x.dow), cell:x=>x.dow},
    {k:'lead',   h:'Lead',  num:1, get:x=>x.lead, cell:x=>x.lead},
    {k:'otb',    h:'OTB',   num:1, get:x=>x.otb,  cell:x=>x.otb},
    {k:'rate',   h:'Rate',  num:1, get:x=>x.rate, cell:x=>Math.round(x.rate)},
    {k:'bid',    h:'Bid',   num:1, get:x=>x.bid_price,
     cell:x=>x.bid_price>0?Math.round(x.bid_price):'·'},
    {k:'occ',    h:'Forecast occ', num:1, get:x=>x.sellout?1.001:x.forecast_occ,
     cell:x=>x.sellout?'full':pct(x.forecast_occ)},
    {k:'restr',  h:'Restrictions',
     get:x=>(x.cta?4:0)+(x.mlos>1?2:0)+x.closed.length,
     cell:x=>(x.cta?'CTA ':'')+(x.mlos>1?('MLOS '+x.mlos+' '):'')+(x.closed.join(' ')||'')||'·'},
    {k:'bound',  h:'Binding constraint', get:x=>x.bound_by, cell:x=>x.bound_by},
  ];
  const order = recs.map((_,i)=>i);
  const col = COLS.find(c=>c.k===sortKey) || COLS[0];
  order.sort((a,b)=>{
    const va=col.get(recs[a]), vb=col.get(recs[b]);
    if(va<vb) return -sortDir; if(va>vb) return sortDir; return a-b;
  });
  const tp=el('div',{class:'panel scroll'});
  const t2=el('table');
  const hr=el('tr',{});
  COLS.forEach(c=>{
    const active = c.k===sortKey;
    const th=el('th',{class:'sortable'+(c.num?' num':''),tabindex:'0',role:'button'},
      c.h, active?el('span',{class:'caret'}, sortDir>0?'\u25b2':'\u25bc'):'');
    if(active) th.setAttribute('aria-sort', sortDir>0?'ascending':'descending');
    const flip=()=>{ if(sortKey===c.k) sortDir=-sortDir; else {sortKey=c.k; sortDir=1;} render(); };
    th.onclick=flip;
    th.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){e.preventDefault();flip();} };
    hr.append(th);
  });
  t2.append(el('thead',{},hr));
  const b2=el('tbody');
  order.forEach(i=>{
    const x=recs[i];
    const tr=el('tr',{class:'click'+(i===picked?' on':''),tabindex:'0'});
    COLS.forEach(c=>tr.append(el('td',{class:c.num?'num':''}, c.cell(x))));
    const go=()=>{picked=i;render();
      document.getElementById('detail').scrollIntoView({behavior:'smooth',block:'start'})};
    tr.onclick=go;
    tr.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){e.preventDefault();go();} };
    b2.append(tr);
  });
  t2.append(b2); tp.append(t2); app.append(tp);

  app.append(el('div',{class:'foot'},
    'Pace · generated in '+D.runtime_seconds+'s from '+D.demand_total_requests.toLocaleString()+
    ' simulated booking requests and '+D.solves.toLocaleString()+' optimizer solves. '+
    'Python standard library only. Synthetic property and synthetic market throughout.'));

  document.getElementById('themebtn').onclick=()=>{
    const r=document.documentElement;
    const now=r.getAttribute('data-theme')||
      (matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
    r.setAttribute('data-theme', now==='dark'?'light':'dark');
    render();
  };
}
render();
"""


def build_artifact(root: str, payload: Dict[str, Any]) -> str:
    """The same page, as a document fragment.

    Artifact hosting supplies its own document skeleton, so this emits the
    content only: title, styles, mount point, data and script.  Same CSS, same
    JavaScript, same embedded payload, so the hosted page and the local file
    can never disagree about what the engine decided.
    """
    out = os.path.join(root, "out")
    os.makedirs(out, exist_ok=True)
    body = (
        "<title>%s, revenue controls</title>\n" % payload["hotel"]["name"]
        + "<style>" + CSS + "</style>\n"
        + "<div id=\"app\"></div>\n"
        + "<script id=\"payload\" type=\"application/json\">"
        + json.dumps(payload, default=str) + "</script>\n"
        + "<script id=\"levels\" type=\"application/json\">"
        + json.dumps(level_payload()) + "</script>\n"
        + "<script>" + JS + "</script>\n"
    )
    path = os.path.join(out, "dashboard-artifact.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def build(root: str, payload: Dict[str, Any]) -> str:
    out = os.path.join(root, "out")
    os.makedirs(out, exist_ok=True)
    html = (TEMPLATE
            .replace("__TITLE__", "%s revenue controls" % payload["hotel"]["name"])
            .replace("__CSS__", CSS)
            .replace("__JS__", JS)
            .replace("__DATA__", json.dumps(payload, default=str))
            .replace("__LEVELS__", json.dumps(level_payload())))
    path = os.path.join(out, "dashboard.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path
