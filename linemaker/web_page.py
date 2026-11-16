"""The dashboard's single page (HTML, CSS and JS inline; Chart.js from jsDelivr)."""

SIGNED_OUT = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Linemaker</title><style>body{background:#1e1f22;color:#dbdee1;font:16px/1.5 system-ui,sans-serif;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px;text-align:center}b{color:#fff}</style></head>
<body><div><div style="font-size:42px">🎯</div><p><b>Linemaker</b></p><p>Type <b>/web</b> in Discord and tap your link to sign in.<br>
Links work once and expire after 15 minutes.</p></div></body></html>"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Linemaker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#1e1f22;--card:#2b2d31;--line:#3f4147;--text:#dbdee1;--dim:#949ba4;--gold:#f1c40f;--green:#2ecc71;--red:#e74c3c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;background:#111214;padding:10px 16px;display:flex;align-items:center;gap:10px;z-index:2}
header b{color:#fff;font-size:17px}header .me{margin-left:auto;color:var(--dim);font-size:13px}header a{color:var(--dim)}
nav{display:flex;gap:6px;overflow-x:auto;padding:8px 16px;background:#111214;border-bottom:1px solid var(--line);position:sticky;top:44px;z-index:2}
nav button{background:var(--card);color:var(--text);border:0;border-radius:16px;padding:6px 14px;font:inherit;white-space:nowrap}
nav button.on{background:var(--gold);color:#111}
main{max-width:860px;margin:0 auto;padding:12px 16px 40px}
.card{background:var(--card);border-left:4px solid var(--gold);border-radius:8px;padding:12px 14px;margin:10px 0}
.card h3{margin:0 0 6px;font-size:16px;color:#fff}.dim{color:var(--dim);font-size:13px}
.row{padding:7px 0;border-top:1px solid var(--line)}.row:first-of-type{border-top:0}
.row b{color:#fff}.tags{color:#b5bac1;font-size:13px}.pos{color:var(--green)}.neg{color:var(--red)}
.sq{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--dim);font-weight:500}td.best{color:#fff;font-weight:700}.wrap{overflow-x:auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}
.stat{background:var(--card);border-radius:8px;padding:10px}.stat .v{font-size:20px;color:#fff;font-weight:700}
canvas{background:var(--card);border-radius:8px;padding:8px;margin-top:10px;max-height:280px}
.empty{color:var(--dim);padding:20px 0;text-align:center}
.bet{display:inline-block;margin:5px 6px 0 0;padding:4px 10px;border-radius:14px;background:#383a40;color:#fff;text-decoration:none;font-size:13px}
.bet.good{outline:1px solid var(--green)}.flag{color:#f0b232;font-size:13px}.week b{color:#fff}.week p{margin:6px 0}
</style></head><body>
<header><b>🎯 Linemaker</b><span class="me" id="me"></span><a href="/logout">sign out</a></header>
<nav id="tabs"></nav><main id="main"><div class="empty">Loading…</div></main>
<script>
const COLORS={fanduel:"#1493FF",draftkings:"#53D337",polymarket:"#F57C00",prizepicks:"#8E44AD",underdog:"#F1C40F",kalshi:"#00B386"};
const TAGS={fanduel:"FD",draftkings:"DK",polymarket:"PM",prizepicks:"PP",underdog:"UD",kalshi:"Kalshi"};
const key=b=>(b||"").toLowerCase().replace(/[^a-z]/g,"").replace("polymarketus","polymarket").replace("underdogfantasy","underdog");
const sq=b=>`<span class="sq" style="background:${COLORS[key(b)]||"#80848e"}"></span>`;
const odds=o=>o==null?"–":(o>0?"+"+o:""+o);const pct=p=>p==null?"–":Math.round(p*1000)/10+"%";
const money=v=>(v<0?"-$":"$")+Math.abs(v).toFixed(2);const cls=v=>v>0?"pos":v<0?"neg":"";
const esc=s=>(s??"").toString().replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const chip=(b,o,u,good,txt)=>{const t=(txt||((TAGS[key(b)]||b)+(o!=null?" "+odds(o):"")))+(good?" ✅":"");
 return u?`<a class="bet${good?" good":""}" href="${esc(u)}" target="_blank" rel="noopener">${sq(b)}${esc(t)}</a>`:`<span class="bet${good?" good":""}">${sq(b)}${esc(t)}</span>`};
const md=t=>esc(t).replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/`(.+?)`/g,"<code>$1</code>").replace(/_(Tracked.+?)_/g,"<i>$1</i>").replace(/\n/g,"<br>");
const ufcCard=u=>{const f=(u.fight||[]).map(r=>`<div class="row"><b>${r.n}. ${esc(r.label)}</b> · <b>${pct(r.chance)}</b><br>
  <span class="dim">${esc(r.fight)} · fair ${esc(r.fair)} · bet at <b>${esc(r.bet_at)}</b>+ · Kalshi ${Math.round(r.kalshi*100)}¢ · best at ${esc(r.best)}</span><br>
  ${r.quotes.map(q=>chip(q.book,q.odds,q.url,q.good)).join("")}</div>`).join("");
 const st=(u.strike||[]).map(r=>`<div class="row"><b>${r.n}. ${esc(r.label)}</b> · <b>${pct(r.chance)}</b><br>
  <span class="dim">${esc(r.book)}${r.odds!=null?" "+odds(r.odds):""} · projects ${r.proj}</span>${r.flags&&r.flags.length?` <span class="flag">⚠️ ${esc(r.flags.join(", "))}</span>`:""}<br>
  ${r.quotes.map(q=>chip(q.book,q.odds,q.url,q.good)).join("")}${(r.pages||[]).map(p=>chip(p.book,null,p.url,false,(TAGS[key(p.book)]||p.book)+" fight")).join("")}</div>`).join("");
 return (f?`<h3 style="margin-top:8px">🎯 Fight props</h3>${f}`:"")+(st?`<h3 style="margin-top:10px">🥊 Strike & takedown</h3>${st}`:"")};
const api=async p=>{const r=await fetch(p,{credentials:"same-origin"});if(r.status==401){location.href="/";throw 0}return r.json()};
let chart=[];const killCharts=()=>{chart.forEach(c=>c.destroy());chart=[]};
const VIEWS={
 "Today":async()=>{const cs=await api("/api/cards");if(!cs.length)return`<div class="empty">No cards yet today. Ask the bot for picks or parlays in Discord.</div>`;
  return cs.map(c=>{const rows=(c.picks.length?c.picks:c.legs).map((p,i)=>`<div class="row">${sq(p.book)}<b>${i+1}. ${esc(p.label)}</b> <span class="dim">${esc(p.game)}</span><br>
   ${odds(p.odds)} ${esc(TAGS[key(p.book)]||p.book)} · ours <b>${pct(p.ours)}</b> vs ${pct(p.implied)} · gap <span class="${cls(p.gap)}">${(p.gap*100).toFixed(1)}</span>
   ${p.tags?`<div class="tags">${esc(p.tags)}</div>`:""}${(p.links||[]).map(l=>chip(l.book,(p.books.find(b=>b.book==l.book)||{}).odds,l.url,false,"Bet "+(TAGS[key(l.book)]||l.book))).join("")}</div>`).join("");
   const pars=c.parlays.map(p=>`<div class="row" style="border-left:3px solid ${COLORS[key(p.book)]||"#80848e"};padding-left:8px">${sq(p.book)}<b>${esc(p.book)} · ${p.odds!=null?p.legs.length+"-leg "+esc(p.kind)+" · "+odds(p.odds):esc(p.kind)+(p.pays?" · pays "+p.pays+"x":"")}</b> · ${pct(p.prob)} hit<br>
   <span class="dim">${p.legs.map(l=>l.url?`<a href="${esc(l.url)}" target="_blank" rel="noopener" style="color:#b5bac1">${esc(l.label)}</a>`:esc(l.label)+(l.odds!=null?" ("+odds(l.odds)+")":"")).join(" · ")}</span>
   ${p.url?`<br>${chip(p.book,null,p.url,false,"Open the slip on "+p.book)}`:""}</div>`).join("");
   const border=c.ufc?"#e74c3c":"var(--gold)";
   return`<div class="card" style="border-left-color:${border}"><h3>${esc(c.title)}</h3><div class="dim">${esc(c.at.replace("T"," "))}</div>${rows}${c.ufc?ufcCard(c.ufc):""}${pars?`<h3 style="margin-top:10px">Parlays & entries</h3>${pars}`:""}</div>`}).join("")},
 "Line shop":async()=>{const cs=await api("/api/cards");const picks=cs.flatMap(c=>c.picks.length?c.picks:c.legs);
  if(!picks.length)return`<div class="empty">Nothing to compare yet.</div>`;const seen=new Set();const list=picks.filter(p=>!seen.has(p.label)&&seen.add(p.label));
  const books=[...new Set(list.flatMap(p=>p.books.map(b=>b.book)))].sort((a,b)=>(TAGS[key(a)]?0:1)-(TAGS[key(b)]?0:1)||a.localeCompare(b)).slice(0,8);
  const dec=o=>o>0?1+o/100:1+100/-o;
  return`<div class="card"><h3>Every pick's price, book by book</h3><div class="dim">Bold = the best price</div><div class="wrap"><table><tr><th>Pick</th>${books.map(b=>`<th>${sq(b)}${esc(TAGS[key(b)]||b)}</th>`).join("")}</tr>
  ${list.map(p=>{const m=Object.fromEntries(p.books.map(b=>[b.book,b.odds]));const best=Math.max(...p.books.map(b=>dec(b.odds)));
   return`<tr><td>${esc(p.label)}<div class="dim">ours ${pct(p.ours)}</div></td>${books.map(b=>`<td class="${m[b]!=null&&dec(m[b])==best?"best":""}">${odds(m[b])}</td>`).join("")}</tr>`}).join("")}</table></div></div>`},
 "Record":async()=>{const [d,wk]=await Promise.all([api("/api/record"),api("/api/weekly")]);let run=0;const cum=d.series.map(s=>(run+=s.units,Math.round(run*100)/100));
  const w=d.series.reduce((a,s)=>a+s.won,0),l=d.series.reduce((a,s)=>a+s.lost,0);const mine=d.mine.length?d.mine[d.mine.length-1].profit:0;
  setTimeout(()=>{chart.push(new Chart(document.getElementById("c1"),{type:"line",data:{labels:d.series.map(s=>s.date.slice(5)),datasets:[{label:"Model picks, units (running)",data:cum,borderColor:"#f1c40f",tension:.25}]},options:{plugins:{legend:{labels:{color:"#dbdee1"}}},scales:{x:{ticks:{color:"#949ba4"}},y:{ticks:{color:"#949ba4"}}}}}));
   if(d.mine.length)chart.push(new Chart(document.getElementById("c2"),{type:"line",data:{labels:d.mine.map(s=>s.date.slice(5)),datasets:[{label:"Your bets, $ (running)",data:d.mine.map(s=>s.profit),borderColor:"#2ecc71",tension:.25}]},options:{plugins:{legend:{labels:{color:"#dbdee1"}}},scales:{x:{ticks:{color:"#949ba4"}},y:{ticks:{color:"#949ba4"}}}}}))},0);
  return`<div class="stats"><div class="stat"><div class="dim">Model picks, 14 days</div><div class="v">${w}-${l}</div><div class="${cls(run)}">${run>=0?"+":""}${run.toFixed(1)}u</div></div>
  <div class="stat"><div class="dim">Beat the closing line</div><div class="v">${d.clv.n?pct(d.clv.beat):"–"}</div><div class="dim">${d.clv.n?((d.clv.pts*100).toFixed(2)+" pts avg · "+d.clv.n+" picks"):""}</div></div>
  <div class="stat"><div class="dim">Your bets, all time</div><div class="v ${cls(mine)}">${money(mine)}</div></div></div>
  <canvas id="c1"></canvas>${d.mine.length?'<canvas id="c2"></canvas>':""}
  <div class="card week" style="border-left-color:#9b59b6"><h3>🗓️ This week</h3>${wk.map(l=>`<p>${md(l)}</p>`).join("")}</div>`},
 "My bets":async()=>{const d=await api("/api/bets");const row=b=>`<div class="row">${sq(b.book)}<b>${esc(b.what)}</b>${b.pm?' <span class="dim">· synced from Polymarket</span>':""}<br>
  ${odds(b.odds)} ${esc(b.book)} · $${(b.stake||0).toFixed(2)}${b.status!="open"?` · <span class="${cls(b.profit)}">${b.status} ${money(b.profit||0)}</span>`:""} <span class="dim">· ${esc(b.placed.replace("T"," "))}</span></div>`;
  return`<div class="card"><h3>Open (${d.open.length})</h3>${d.open.map(row).join("")||'<div class="empty">No open bets.</div>'}</div>
  <div class="card" style="border-left-color:#80848e"><h3>Settled</h3>${d.settled.map(row).join("")||'<div class="empty">Nothing settled yet.</div>'}</div>`},
 "Group":async()=>{const g=await api("/api/group");if(!g.length)return`<div class="empty">No settled bets in the last 30 days.</div>`;
  return`<div class="card"><h3>Last 30 days</h3><table><tr><th>#</th><th>Who</th><th>Record</th><th>Profit</th><th>ROI</th></tr>${g.map((p,i)=>`<tr><td>${i+1}</td><td>${esc(p.name)}</td><td>${p.won}-${p.lost}</td><td class="${cls(p.profit)}">${money(p.profit)}</td><td>${p.roi==null?"–":(p.roi*100).toFixed(1)+"%"}</td></tr>`).join("")}</table></div>`}};
let current="Today";
async function show(name){current=name;document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.textContent==name));
 killCharts();const m=document.getElementById("main");m.innerHTML='<div class="empty">Loading…</div>';
 try{m.innerHTML=await VIEWS[name]()}catch(e){if(e)m.innerHTML='<div class="empty">Couldn\'t load this. Try again in a minute.</div>'}}
document.getElementById("tabs").innerHTML=Object.keys(VIEWS).map(n=>`<button>${n}</button>`).join("");
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>show(b.textContent));
api("/api/me").then(d=>document.getElementById("me").textContent=d.name);show("Today");
setInterval(()=>{if(current=="Today")show("Today")},120000);
</script></body></html>"""
