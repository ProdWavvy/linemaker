"""The dashboard's single page (HTML, CSS and JS inline; Chart.js from jsDelivr)."""

SIGNED_OUT = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Linemaker</title><style>body{background:radial-gradient(1200px 600px at 20% -10%,#3b2a6b 0,#141518 55%);color:#dbdee1;
font:16px/1.5 system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px;text-align:center}
.box{background:#1e1f22cc;border:1px solid #ffffff14;border-radius:18px;padding:28px 22px;max-width:360px}b{color:#fff}</style></head>
<body><div class="box"><div style="font-size:46px">🎯</div><p style="font-size:20px"><b>Linemaker</b></p>
<p>Type <b>/web</b> in Discord and tap your link to sign in.<br><span style="color:#949ba4">Links work once and expire after 15 minutes.</span></p></div></body></html>"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#141518"><title>Linemaker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#141518;--card:#1e1f24;--card2:#26282e;--line:#ffffff12;--text:#dbdee1;--dim:#8e949c;--gold:#f5c542;--green:#3ddc84;--red:#ff5d5d;--purple:#8b6cff}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:inherit}
header{position:sticky;top:0;z-index:5;padding:12px 16px 10px;background:linear-gradient(120deg,#2a1f4f 0%,#1b1c22 60%,#2b2414 100%);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px}
.logo{font-weight:800;font-size:18px;color:#fff;letter-spacing:.2px;white-space:nowrap}
.me span:not(.badge){max-width:96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}header a{white-space:nowrap}.live{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 #3ddc8488;animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 #3ddc8488}70%{box-shadow:0 0 0 8px #3ddc8400}100%{box-shadow:0 0 0 0 #3ddc8400}}
.me{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:13px;color:var(--dim)}.me img,.av{width:28px;height:28px;border-radius:50%;object-fit:cover}
.badge{font-size:11px;padding:2px 7px;border-radius:9px;background:var(--purple);color:#fff;font-weight:700}
nav{position:sticky;top:51px;z-index:4;display:flex;gap:6px;overflow-x:auto;padding:9px 16px;background:#141518ee;backdrop-filter:blur(8px);border-bottom:1px solid var(--line);scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{flex:none;background:var(--card2);color:var(--text);border:1px solid var(--line);border-radius:18px;padding:7px 14px;font:inherit;font-size:14px;cursor:pointer}
nav button.on{background:linear-gradient(120deg,var(--gold),#ffdf7a);color:#1a1405;border-color:transparent;font-weight:700}
main{max-width:900px;margin:0 auto;padding:12px 16px 60px}
.fade{animation:fade .25s ease-out}@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:4px 0 12px}
.kpi{background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:14px;padding:12px}
.kpi .t{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.6px}.kpi .v{font-size:24px;font-weight:800;color:#fff;margin-top:2px}
.sec{display:flex;align-items:center;gap:8px;margin:18px 0 8px;color:#fff;font-weight:700;font-size:16px}.sec .dim{font-weight:400}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:10px 0;position:relative;overflow:hidden}
.card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent,var(--gold))}
.card h3{margin:0 0 2px;font-size:16px;color:#fff}.dim{color:var(--dim);font-size:13px}
.pick{padding:10px 0;border-top:1px solid var(--line)}.pick:first-of-type{border-top:0}
.ptop{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.ptop b{color:#fff;font-size:15px}.stars{font-size:12px;letter-spacing:-1px}
.gap{margin-left:auto;font-weight:800;font-size:13px;padding:2px 8px;border-radius:9px;background:#3ddc8422;color:var(--green)}
.bar{position:relative;height:8px;background:#ffffff10;border-radius:5px;margin:8px 0 4px;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:linear-gradient(90deg,#3ddc84,#9be15d)}
.bar u{position:absolute;top:-2px;bottom:-2px;width:2px;background:#fff;opacity:.85}
.meta{display:flex;gap:10px;flex-wrap:wrap;font-size:13px;color:var(--dim)}.meta b{color:#fff}
.tags{color:#c9ccd1;font-size:13px;margin-top:4px}
.chip{display:inline-flex;align-items:center;gap:5px;margin:7px 6px 0 0;padding:5px 11px;border-radius:15px;background:var(--card2);border:1px solid var(--line);color:#fff;text-decoration:none;font-size:13px}
.chip.good{border-color:#3ddc8499;background:#3ddc8414}.sq{display:inline-block;width:10px;height:10px;border-radius:3px}
.ticket{border:1px dashed #ffffff2a;border-radius:12px;padding:10px 12px;margin:8px 0;background:#ffffff05}
.ticket .hd{display:flex;align-items:center;gap:8px;color:#fff;font-weight:700}.ring{margin-left:auto;font-size:12px;color:var(--dim)}
.legs{font-size:13px;color:#c9ccd1;margin-top:4px}.flag{color:#f0b232;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--dim);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.5px}td.best{color:#fff;font-weight:800;background:#3ddc8414}
.wrap{overflow-x:auto}td:first-child,th:first-child{position:sticky;left:0;background:var(--card);white-space:normal;min-width:150px}
.pill{font-size:12px;font-weight:700;padding:2px 8px;border-radius:9px}.won{background:#3ddc8422;color:var(--green)}.lost{background:#ff5d5d22;color:var(--red)}.push,.open{background:#ffffff14;color:var(--dim)}
.pos{color:var(--green)}.neg{color:var(--red)}
.lb{display:flex;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}.lb:first-of-type{border-top:0}
.ini{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;flex:none}
.lb .n{color:#fff;font-weight:700}.lb .r{margin-left:auto;text-align:right}
.member{cursor:pointer}.member:hover{background:#ffffff06}.detail{padding:6px 0 4px 44px}
canvas{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px;margin-top:10px;max-height:290px}
.empty{color:var(--dim);padding:28px 0;text-align:center}.week p{margin:7px 0}.week b{color:#fff}
.viewing{background:#8b6cff22;border:1px solid #8b6cff66;color:#d9d0ff;border-radius:12px;padding:8px 12px;margin:6px 0 10px;font-size:14px;display:flex;align-items:center;gap:8px}
dialog{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:16px;padding:16px;width:min(92vw,380px)}
dialog::backdrop{background:#000a}dialog h3{margin:0 0 8px;color:#fff}dialog label{display:block;font-size:13px;color:var(--dim);margin-top:10px}
dialog input,dialog select{width:100%;margin-top:4px;padding:9px 10px;border-radius:10px;border:1px solid var(--line);background:#141518;color:#fff;font:inherit}
.btns{display:flex;gap:8px;margin-top:14px}.btn{flex:1;padding:10px;border-radius:11px;border:0;font:inherit;font-weight:700;cursor:pointer;background:var(--card2);color:#fff}
.btn.go{background:linear-gradient(120deg,#3ddc84,#9be15d);color:#0b1a10}.btn.pm{background:#F57C00;color:#fff}.msg{margin-top:10px;font-size:14px}
.inp{width:100%;margin-top:4px;padding:9px;border-radius:10px;border:1px solid var(--line);background:#141518;color:#fff;font:inherit}
button.chip{font:inherit;font-size:13px;cursor:pointer}
.viewing button{margin-left:auto;background:#8b6cff;color:#fff;border:0;border-radius:10px;padding:4px 10px;font:inherit;font-size:13px}
</style></head><body>
<header><span class="live" title="auto-refreshing"></span><span class="logo">🎯 Linemaker</span><span class="me" id="me"></span><a href="/logout" class="dim" style="font-size:13px">sign out</a></header>
<nav id="tabs"></nav><main id="main"><div class="empty">Loading…</div></main><dialog id="dlg"></dialog>
<script>
const COLORS={fanduel:"#1493FF",draftkings:"#53D337",polymarket:"#F57C00",prizepicks:"#8E44AD",underdog:"#F1C40F",kalshi:"#00B386"};
const TAGS={fanduel:"FD",draftkings:"DK",polymarket:"PM",prizepicks:"PP",underdog:"UD",kalshi:"Kalshi"};
const SPORT={NBA:"🏀",WNBA:"🏀",NCAAB:"🏀",NFL:"🏈",NCAAF:"🏈",NHL:"🏒",MLB:"⚾",UFC:"🥊",EPL:"⚽",MLS:"⚽",UCL:"⚽",LALIGA:"⚽"};
const key=b=>(b||"").toLowerCase().replace(/[^a-z]/g,"").replace("polymarketus","polymarket").replace("underdogfantasy","underdog");
const col=b=>COLORS[key(b)]||"#80848e";const sq=b=>`<span class="sq" style="background:${col(b)}"></span>`;
const odds=o=>o==null?"–":(o>0?"+"+o:""+o);const pct=p=>p==null?"–":Math.round(p*1000)/10+"%";
const money=v=>(v<0?"-$":"$")+Math.abs(v||0).toFixed(2);const cls=v=>v>0?"pos":v<0?"neg":"";
const esc=s=>(s??"").toString().replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const stars=n=>n?`<span class="stars" title="${n} of 5 signals agree">${"⭐".repeat(n)}</span>`:"";
const hue=s=>{let h=0;for(const c of s||"")h=(h*31+c.charCodeAt(0))%360;return h};
const ini=n=>`<span class="ini" style="background:hsl(${hue(n)},55%,42%)">${esc((n||"?").replace(/[^A-Za-z0-9]/g,"").slice(0,2).toUpperCase())}</span>`;
const when=s=>{if(!s)return"";const d=new Date(s);if(isNaN(d))return"";const m=(d-Date.now())/60000;
 const t=d.toLocaleTimeString([],{hour:"numeric",minute:"2-digit"});return m>0&&m<180?`⏱️ in ${Math.round(m)}m`:m<=0&&m>-200?"🔴 live":`🕒 ${t}`};
const ago=s=>{if(!s)return"never";const m=(Date.now()-new Date(s))/60000;return m<1?"just now":m<60?Math.round(m)+"m ago":m<1440?Math.round(m/60)+"h ago":Math.round(m/1440)+"d ago"};
const md=t=>esc(t).replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/`(.+?)`/g,"<code>$1</code>").replace(/_(Tracked.+?)_/g,"<i>$1</i>").replace(/\n/g,"<br>");
const chip=(b,o,u,good,txt)=>{const t=(txt||((TAGS[key(b)]||b)+(o!=null?" "+odds(o):"")))+(good?" ✅":"");
 return u?`<a class="chip${good?" good":""}" href="${esc(u)}" target="_blank" rel="noopener">${sq(b)}${esc(t)}</a>`:`<span class="chip${good?" good":""}">${sq(b)}${esc(t)}</span>`};
const api=async p=>{const r=await fetch(p,{credentials:"same-origin"});if(r.status==401){location.href="/";throw 0}if(!r.ok)throw new Error(r.status);return r.json()};
let ME={},VIEW=null,charts=[],AFTER=[],PICKS={},PM=null;let NEXT=0;
const post=async(p,b)=>{const r=await fetch(p,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-Linemaker":"1"},body:JSON.stringify(b)});
 const d=await r.json().catch(()=>({error:"no answer"}));if(r.status==401){location.href="/";throw 0}if(!r.ok)throw new Error(d.error||r.status);return d};
const dlg=document.getElementById("dlg");const shut=()=>dlg.close();
const keep=o=>{const id="p"+(NEXT++);PICKS[id]=o;return id};
const val=id=>document.getElementById(id).value;
function openLog(id){const o=PICKS[id];dlg.innerHTML=`<h3>📝 Log this bet</h3><div class="dim">${esc(o.label)}</div>
 <label>Stake ($)<input id="lstake" inputmode="decimal" placeholder="10"></label><label>Odds you got<input id="lodds" value="${o.odds??""}"></label>
 <label>Book<input id="lbook" value="${esc(o.book||"")}"></label><div class="msg" id="lmsg"></div>
 <div class="btns"><button class="btn" id="lno">Cancel</button><button class="btn go" id="lgo">Log it</button></div>`;
 dlg.showModal();document.getElementById("lno").onclick=shut;
 document.getElementById("lgo").onclick=async()=>{const m=document.getElementById("lmsg");
  try{await post("/api/bet",{legs:o.legs,stake:val("lstake"),odds:val("lodds"),book:val("lbook")});
   m.innerHTML='<span class="pos">✅ Logged in your bets.</span>';setTimeout(shut,900)}catch(e){if(e)m.innerHTML=`<span class="neg">⚠️ ${esc(e.message)}</span>`}}}
async function openBuy(id){const o=PICKS[id];if(!PM)PM=await api("/api/pm");
 if(!PM.linked){dlg.innerHTML=`<h3>🟧 Polymarket</h3><p>Link your own Polymarket US account first (it's in the <b>My bets</b> tab).</p><div class="btns"><button class="btn" id="bok">OK</button></div>`;
  dlg.showModal();document.getElementById("bok").onclick=shut;return}
 dlg.innerHTML=`<h3>🟧 Buy on Polymarket</h3><div class="dim">${esc(o.label)}</div><label>Dollars to spend (max $${PM.max})<input id="bstake" inputmode="decimal" value="5"></label>
 <div class="msg" id="bmsg"></div><div class="btns"><button class="btn" id="bno">Cancel</button><button class="btn pm" id="bgo">Get price</button></div>`;
 dlg.showModal();document.getElementById("bno").onclick=shut;
 let plan=null;const m=()=>document.getElementById("bmsg");const go=document.getElementById("bgo");
 go.onclick=async()=>{try{if(!plan){plan=await post("/api/pm/quote",{slug:o.pm.pm_slug,long:o.pm.pm_long,tick:o.pm.pm_tick,min:o.pm.pm_min,stake:val("bstake")});
   m().innerHTML=`Buy <b>${plan.qty} contracts</b> at <b>${(plan.ask*100).toFixed(1)}¢</b> (${odds(plan.odds)} with the fee)<br>Cost <b>$${plan.cost.toFixed(2)}</b> · pays <b>$${plan.payout.toFixed(2)}</b> if it hits<br><span class="dim">fills now at this price or better, or not at all</span>`;
   go.textContent=`Confirm $${plan.cost.toFixed(2)}`;return}
   go.disabled=true;const r=await post("/api/pm/buy",{slug:o.pm.pm_slug,long:o.pm.pm_long,tick:o.pm.pm_tick,ask:plan.ask,qty:plan.qty,cost:plan.cost,leg:o.leg,label:o.label});
   m().innerHTML=r.ok?`<span class="pos">✅ Bought ${r.qty} contracts at ${(r.avg*100).toFixed(1)}¢ ($${r.cost.toFixed(2)} with the fee). Logged.</span>`:`<span class="neg">⚠️ ${esc(r.msg)}</span>`;
   go.textContent="Done";go.onclick=shut;go.disabled=false}catch(e){if(e)m().innerHTML=`<span class="neg">⚠️ ${esc(e.message)}</span>`;plan=null;go.textContent="Get price";go.disabled=false}}}
async function pmLink(){const m=document.getElementById("pmsg");try{await post("/api/pm/link",{key_id:val("pkid"),secret:val("psec")});PM=null;show("My bets")}
 catch(e){if(e)m.innerHTML=`<span class="neg">⚠️ ${esc(e.message)}</span>`}}
async function pmUnlink(){await post("/api/pm/unlink",{});PM=null;show("My bets")}const kill=()=>{charts.forEach(c=>c.destroy());charts=[]};
const q=p=>VIEW?p+(p.includes("?")?"&":"?")+"user="+encodeURIComponent(VIEW):p;
const pickRow=(p,i)=>`<div class="pick"><div class="ptop">${SPORT[p.sport]||"🎯"} <b>${i}. ${esc(p.label)}</b>${stars(p.stars)}<span class="gap">+${(p.gap*100).toFixed(1)}</span></div>
 <div class="bar"><i style="width:${Math.min(100,p.ours*100)}%"></i><u style="left:${Math.min(99,p.implied*100)}%"></u></div>
 <div class="meta">${sq(p.book)}<span><b>${odds(p.odds)}</b> ${esc(TAGS[key(p.book)]||p.book)}</span><span>ours <b>${pct(p.ours)}</b> · price ${pct(p.implied)}</span><span>${esc(p.game||"")}</span><span>${when(p.commence)}</span></div>
 ${p.tags?`<div class="tags">${esc(p.tags)}</div>`:""}${(p.links||[]).map(l=>chip(l.book,(p.books.find(b=>b.book==l.book)||{}).odds,l.url,false,"Bet "+(TAGS[key(l.book)]||l.book)+" "+odds((p.books.find(b=>b.book==l.book)||{}).odds))).join("")}
 ${p.leg?`<button class="chip" onclick="openLog('${keep({label:p.label,legs:[p.leg],odds:p.odds,book:p.book})}')">📝 Log</button>`:""}${p.pm?`<button class="chip" style="border-color:#F57C0099" onclick="openBuy('${keep({label:p.label,pm:p.pm,leg:p.leg})}')">🟧 Buy</button>`:""}</div>`;
const ticket=p=>`<div class="ticket" style="border-color:${col(p.book)}66"><div class="hd">${sq(p.book)}${esc(p.book)} · ${p.odds!=null?p.legs.length+"-leg "+esc(p.kind):esc(p.kind)} <span class="ring">${p.odds!=null?"<b style='color:#fff'>"+odds(p.odds)+"</b> · ":p.pays?"pays <b style='color:#fff'>"+p.pays+"x</b> · ":""}${pct(p.prob)} hit</span></div>
 <div class="legs">${p.legs.map(l=>l.url?`<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)}</a>`:esc(l.label)+(l.odds!=null?" ("+odds(l.odds)+")":"")).join(" · ")}</div>${p.url?chip(p.book,null,p.url,false,"Open the slip on "+p.book):""}
 ${p.legs_raw&&p.legs_raw.length?`<button class="chip" onclick="openLog('${keep({label:p.legs.map(l=>l.label).join(" + "),legs:p.legs_raw,odds:p.odds,book:p.book})}')">📝 Log</button>`:""}</div>`;
const ufcCard=u=>{const f=(u.fight||[]).map(r=>`<div class="pick"><div class="ptop">🥊 <b>${r.n}. ${esc(r.label)}</b><span class="gap" style="background:#ffffff10;color:#fff">${pct(r.chance)}</span></div>
  <div class="bar"><i style="width:${r.chance*100}%;background:linear-gradient(90deg,#ff5d5d,#f5c542)"></i><u style="left:${r.kalshi*100}%"></u></div>
  <div class="meta"><span>${esc(r.fight)}</span><span>fair <b>${esc(r.fair)}</b></span><span>bet at <b>${esc(r.bet_at)}</b>+</span><span>Kalshi ${Math.round(r.kalshi*100)}¢</span></div>
  ${r.quotes.map(x=>chip(x.book,x.odds,x.url,x.good)).join("")}</div>`).join("");
 const st=(u.strike||[]).map(r=>`<div class="pick"><div class="ptop">🥊 <b>${r.n}. ${esc(r.label)}</b><span class="gap" style="background:#ffffff10;color:#fff">${pct(r.chance)}</span></div>
  <div class="meta"><span>${esc(r.book)}${r.odds!=null?" "+odds(r.odds):""}</span><span>projects <b>${r.proj}</b></span>${r.flags&&r.flags.length?`<span class="flag">⚠️ ${esc(r.flags.join(", "))}</span>`:""}</div>
  ${r.quotes.map(x=>chip(x.book,x.odds,x.url,x.good)).join("")}${(r.pages||[]).map(p=>chip(p.book,null,p.url,false,(TAGS[key(p.book)]||p.book)+" fight")).join("")}</div>`).join("");
 return (f?`<div class="sec">🎯 Fight props</div>${f}`:"")+(st?`<div class="sec">🥊 Strike & takedown</div>${st}`:"")};
const betRow=b=>`<div class="pick"><div class="ptop">${sq(b.book)}<b>${esc(b.what)}</b><span class="pill ${b.status}" style="margin-left:auto">${b.status=="open"?"open":b.status+" "+money(b.profit)}</span></div>
 <div class="meta"><span><b>${odds(b.odds)}</b> ${esc(b.book)}</span><span>$${(b.stake||0).toFixed(2)}</span>${b.pm?"<span>🟧 synced</span>":""}<span>${ago(b.placed)}</span></div></div>`;
const viewing=()=>VIEW?`<div class="viewing">👁️ Viewing <b>${esc(VIEW)}</b>'s dashboard <button onclick="VIEW=null;show(current)">Back to mine</button></div>`:"";
async function kpis(){const d=await api(q("/api/record"));let u=d.series.reduce((a,s)=>a+s.units,0);const mine=d.mine.length?d.mine[d.mine.length-1].profit:0;
 return`<div class="kpis"><div class="kpi"><div class="t">Model · 14 days</div><div class="v ${cls(u)}">${u>=0?"+":""}${u.toFixed(1)}u</div><div class="dim">${d.series.reduce((a,s)=>a+s.won,0)}-${d.series.reduce((a,s)=>a+s.lost,0)}</div></div>
 <div class="kpi"><div class="t">Beat the close</div><div class="v">${d.clv.n?pct(d.clv.beat):"–"}</div><div class="dim">${d.clv.n?d.clv.n+" picks":""}</div></div>
 <div class="kpi"><div class="t">${VIEW?esc(VIEW)+"'s":"Your"} bets</div><div class="v ${cls(mine)}">${money(mine)}</div><div class="dim">all time</div></div></div>`}
const VIEWS={
 "Today":async()=>{const [cs,k]=await Promise.all([api("/api/cards"),kpis()]);
  if(!cs.length)return k+`<div class="empty">No cards yet today.<br>Ask the bot for picks, parlays or UFC props in Discord.</div>`;
  return k+cs.map(c=>{const list=c.picks.length?c.picks:c.legs;const acc=c.ufc?"#ff5d5d":list.length&&list[0].sport?"var(--gold)":"var(--purple)";
   return`<div class="card fade" style="--accent:${acc}"><h3>${esc(c.title)}</h3><div class="dim">${ago(c.at)}</div>
   ${list.map((p,i)=>pickRow(p,i+1)).join("")}${c.ufc?ufcCard(c.ufc):""}${c.parlays.length?`<div class="sec">🎰 Parlays & entries <span class="dim">${c.parlays.length}</span></div>${c.parlays.map(ticket).join("")}`:""}</div>`}).join("")},
 "Line shop":async()=>{const cs=await api("/api/cards");const seen=new Set();const list=cs.flatMap(c=>c.picks.length?c.picks:c.legs).filter(p=>!seen.has(p.label)&&seen.add(p.label));
  if(!list.length)return`<div class="empty">Nothing to compare yet.</div>`;
  const books=[...new Set(list.flatMap(p=>p.books.map(b=>b.book)))].sort((a,b)=>(TAGS[key(a)]?0:1)-(TAGS[key(b)]?0:1)||a.localeCompare(b)).slice(0,8);const dec=o=>o>0?1+o/100:1+100/-o;
  return`<div class="card fade"><h3>🛒 Line shop</h3><div class="dim">Every pick's price, book by book · highlighted = best</div><div class="wrap"><table><tr><th>Pick</th>${books.map(b=>`<th>${sq(b)} ${esc(TAGS[key(b)]||b)}</th>`).join("")}</tr>
  ${list.map(p=>{const m=Object.fromEntries(p.books.map(b=>[b.book,b.odds]));const best=Math.max(...p.books.map(b=>dec(b.odds)));
   return`<tr><td>${SPORT[p.sport]||""} ${esc(p.label)} ${stars(p.stars)}<div class="dim">ours ${pct(p.ours)}</div></td>${books.map(b=>`<td class="${m[b]!=null&&dec(m[b])==best?"best":""}">${odds(m[b])}</td>`).join("")}</tr>`}).join("")}</table></div></div>`},
 "Record":async()=>{const [d,wk,k]=await Promise.all([api(q("/api/record")),api(q("/api/weekly")),kpis()]);let run=0;const cum=d.series.map(s=>(run+=s.units,Math.round(run*100)/100));
  AFTER.push(()=>{const ax={x:{ticks:{color:"#8e949c"},grid:{color:"#ffffff08"}},y:{ticks:{color:"#8e949c"},grid:{color:"#ffffff10"}}};
   charts.push(new Chart(document.getElementById("c1"),{data:{labels:d.series.map(s=>s.date.slice(5)),datasets:[
    {type:"bar",label:"Units that day",data:d.series.map(s=>s.units),backgroundColor:d.series.map(s=>s.units>=0?"#3ddc8488":"#ff5d5d88"),borderRadius:4},
    {type:"line",label:"Running",data:cum,borderColor:"#f5c542",backgroundColor:"#f5c542",tension:.3,pointRadius:2}]},
    options:{plugins:{legend:{labels:{color:"#dbdee1"}}},scales:ax}}));
   if(d.mine.length)charts.push(new Chart(document.getElementById("c2"),{type:"line",data:{labels:d.mine.map(s=>s.date.slice(5)),datasets:[{label:(VIEW?VIEW+"'s":"Your")+" bets, $ running",data:d.mine.map(s=>s.profit),borderColor:"#3ddc84",backgroundColor:"#3ddc8422",fill:true,tension:.3}]},options:{plugins:{legend:{labels:{color:"#dbdee1"}}},scales:ax}}))});
  return viewing()+k+`<canvas id="c1"></canvas>${d.mine.length?'<canvas id="c2"></canvas>':""}<div class="card week fade" style="--accent:var(--purple)"><h3>🗓️ This week</h3>${wk.map(l=>`<p>${md(l)}</p>`).join("")}</div>`},
 "My bets":async()=>{const d=await api(q("/api/bets"));if(!VIEW)PM=await api("/api/pm");
  const pmPanel=VIEW?"":PM.linked?`<div class="card fade" style="--accent:#F57C00"><h3>🟧 Polymarket linked</h3><div class="dim">Max $${PM.max} per order · your positions sync into your bets every 10 minutes</div>
   <button class="chip" onclick="pmUnlink()">Unlink</button></div>`:
   `<div class="card fade" style="--accent:#F57C00"><h3>🟧 Link your Polymarket US account</h3><div class="dim">From polymarket.us/developer. Then 🟧 Buy works on any pick with a Polymarket price, and your positions sync in.</div>
   <label class="dim">Key ID<input id="pkid" class="inp"></label><label class="dim">Secret key<input id="psec" type="password" class="inp"></label>
   <div class="msg" id="pmsg"></div><button class="btn pm" style="margin-top:10px;width:100%" onclick="pmLink()">Link</button></div>`;
  return viewing()+pmPanel+`<div class="card fade"><h3>🎟️ Open <span class="dim">${d.open.length}</span></h3>${d.open.map(betRow).join("")||'<div class="empty">No open bets.</div>'}</div>
  <div class="card fade" style="--accent:#5c6370"><h3>📜 Settled</h3>${d.settled.map(betRow).join("")||'<div class="empty">Nothing settled yet.</div>'}</div>`},
 "Group":async()=>{const g=await api("/api/group");if(!g.length)return`<div class="empty">No settled bets in the last 30 days.</div>`;const medal=["🥇","🥈","🥉"];
  return`<div class="card fade"><h3>🏆 Leaderboard <span class="dim">last 30 days</span></h3>${g.map((p,i)=>`<div class="lb">${ini(p.name)}<div><div class="n">${medal[i]||"#"+(i+1)} ${esc(p.name)}</div><div class="dim">${p.won}-${p.lost}</div></div>
   <div class="r"><div class="${cls(p.profit)}" style="font-weight:800">${money(p.profit)}</div><div class="dim">${p.roi==null?"–":(p.roi*100).toFixed(1)+"% ROI"}</div></div></div>`).join("")}</div>`},
 "Members":async()=>{const [m,a]=await Promise.all([api("/api/members"),api("/api/activity")]);
  const rows=m.map(p=>`<div class="lb member" onclick="VIEW='${esc(p.name).replace(/'/g,"\\'")}';show('My bets')">${ini(p.name)}<div><div class="n">${esc(p.name)} ${p.pm?'<span class="pill open">🟧 Polymarket</span>':""}</div>
   <div class="dim">seen ${ago(p.seen)} · last bet ${ago(p.last_bet)} · ${p.open} open</div></div><div class="r"><div class="${cls(p.profit)}" style="font-weight:800">${money(p.profit)}</div><div class="dim">${p.won}-${p.lost} · 30d</div></div></div>`).join("");
  return`<div class="card fade" style="--accent:var(--purple)"><h3>👥 Members <span class="dim">tap one to open their dashboard</span></h3>${rows||'<div class="empty">Nobody yet.</div>'}</div>
  <div class="card fade" style="--accent:var(--purple)"><h3>⚡ Activity <span class="dim">everyone's newest bets</span></h3>${a.map(b=>`<div class="pick"><div class="ptop">${ini(b.user)}<b>${esc(b.user)}</b><span class="pill ${b.status}" style="margin-left:auto">${b.status=="open"?"open":b.status+" "+money(b.profit)}</span></div>
   <div class="meta">${sq(b.book)}<span>${esc(b.what)}</span><span><b>${odds(b.odds)}</b> · $${(b.stake||0).toFixed(2)}</span><span>${ago(b.placed)}</span></div></div>`).join("")||'<div class="empty">No bets yet.</div>'}</div>`}};
let current="Today";
async function show(name){current=name;document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.dataset.v==name));
 kill();const m=document.getElementById("main");if(!m.innerHTML.includes("card"))m.innerHTML='<div class="empty">Loading…</div>';
 AFTER=[];try{m.innerHTML=`<div class="fade">${await VIEWS[name]()}</div>`;AFTER.splice(0).forEach(f=>f())}catch(e){if(e)m.innerHTML='<div class="empty">Couldn\'t load this. Try again in a minute.</div>'}}
api("/api/me").then(d=>{ME=d;document.getElementById("me").innerHTML=(d.avatar?`<img src="${esc(d.avatar)}" alt="">`:ini(d.name))+`<span>${esc(d.name)}</span>`+(d.owner?'<span class="badge">OWNER</span>':"")+(d.guest?'<span class="badge" style="background:#3a6df0">GUEST</span>':"");
 const tabs=Object.keys(VIEWS).filter(n=>n!="Members"||d.owner);
 document.getElementById("tabs").innerHTML=tabs.map(n=>`<button data-v="${n}">${{"Today":"🎯 Today","Line shop":"🛒 Line shop","Record":"📈 Record","My bets":"🎟️ My bets","Group":"🏆 Group","Members":"👥 Members"}[n]}</button>`).join("");
 document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>show(b.dataset.v));show("Today")});
setInterval(()=>{if(!document.hidden&&!dlg.open&&(current=="Today"||current=="Members"))show(current)},90000);   // only while you're looking
</script></body></html>"""
