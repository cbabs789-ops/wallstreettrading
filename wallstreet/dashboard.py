"""Dashboard: one dark, terminal-style page. Read-only by design.

  python -m wallstreet dashboard            serve on http://127.0.0.1:8765 (local machine only)
  python -m wallstreet dashboard --static   write var/reports/dashboard.html, a self-contained snapshot

Security:
  - Binds to 127.0.0.1 unless PWS_DASH_HOST is set. There are no write endpoints: GET only, anything else is 405.
  - Every value is inserted as text (textContent), never as HTML. Links are allowed only for http/https URLs.
  - Strict Content-Security-Policy with a per-response nonce; no external scripts, fonts or styles.
  - No account numbers, tokens or keys are ever in the state it serves (it only reads the paper database).
"""
import http.server
import json
import secrets
import threading
import time
from pathlib import Path

from . import config, db
from .briefing import gather

HOST = config.env("PWS_DASH_HOST", "127.0.0.1")
PORT = config.env("PWS_DASH_PORT", 8765, int)

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wall Street Terminal</title>
<style nonce="__NONCE__">
:root{--bg:#07090a;--panel:#0d1214;--line:#1b2428;--text:#c8d3d5;--dim:#6a7b80;--green:#39d98a;--red:#ff5c5c;--amber:#f5b83d;--cyan:#56c2f0;--hl:#e8f0f1}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:12.5px/1.45 ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
header{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline;padding:10px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
header h1{font-size:13px;margin:0;color:var(--hl);letter-spacing:.12em}
.badge{border:1px solid var(--amber);color:var(--amber);padding:0 6px;font-size:11px;letter-spacing:.08em}
.note{color:var(--dim);font-size:11px;flex:1 1 300px}
main{display:grid;grid-template-columns:repeat(12,1fr);gap:10px;padding:10px 16px 40px}
section{background:var(--panel);border:1px solid var(--line);padding:10px 12px;min-width:0;grid-column:span 4}
section.w6{grid-column:span 6}section.w8{grid-column:span 8}section.w12{grid-column:span 12}
@media(max-width:1100px){section,section.w6,section.w8{grid-column:span 6}}
@media(max-width:720px){main{padding:8px}section,section.w6,section.w8,section.w12{grid-column:span 12}}
h2{font-size:11px;color:var(--cyan);margin:0 0 8px;letter-spacing:.14em;font-weight:600}
table{width:100%;border-collapse:collapse}td,th{padding:3px 6px 3px 0;text-align:left;vertical-align:top;white-space:nowrap}
th{color:var(--dim);font-weight:400;border-bottom:1px solid var(--line)}td.wrap{white-space:normal}
.scroll{overflow-x:auto}.up{color:var(--green)}.cyan{color:var(--cyan)}.down{color:var(--red)}.dim{color:var(--dim)}.amber{color:var(--amber)}.hl{color:var(--hl)}
.big{font-size:20px;color:var(--hl)}.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px}
.card{border-top:1px solid var(--line);padding:8px 0}.card:first-of-type{border-top:0;padding-top:0}
.pill{display:inline-block;padding:0 5px;border:1px solid var(--line);margin-right:4px;font-size:11px}
.pass{color:var(--green);border-color:var(--green)}.fail{color:var(--red);border-color:var(--red)}
.sev-critical{color:var(--red)}.sev-action{color:var(--amber)}.sev-watch{color:var(--cyan)}.sev-info{color:var(--dim)}
a{color:var(--cyan);text-decoration:none}a:hover{text-decoration:underline}
.bar{height:6px;background:var(--line);position:relative}.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--cyan)}
svg{display:block;width:100%;height:160px}.empty{color:var(--dim);font-style:italic}
</style></head><body>
<header><h1>PROJECT WALL STREET</h1><span class="badge">PAPER ONLY</span><span id="gen" class="dim"></span><span id="note" class="note"></span></header>
<main id="main"></main>
<script id="state" type="application/json">__STATE__</script>
<script nonce="__NONCE__">
(function(){
"use strict";
function el(tag, cls, text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined&&text!==null)e.textContent=String(text);return e;}
function link(url,text){if(typeof url==="string"&&/^https?:\/\//i.test(url)){var a=el("a",null,text);a.href=url;a.target="_blank";a.rel="noopener noreferrer";return a;}return el("span",null,text);}
function n(x,d){return (x===null||x===undefined||isNaN(x))?"n/a":Number(x).toLocaleString(undefined,{minimumFractionDigits:d===undefined?2:d,maximumFractionDigits:d===undefined?2:d});}
function pct(x){if(x===null||x===undefined)return el("span","dim","n/a");var s=el("span",x>0?"up":x<0?"down":"dim",(x>0?"+":"")+n(x)+"%");return s;}
function t(ms){if(!ms)return "never";var d=new Date(ms);return d.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});}
function sec(title,w){var s=el("section",w||"");s.appendChild(el("h2",null,title));document.getElementById("main").appendChild(s);return s;}
function table(head,rows){var w=el("div","scroll"),tb=el("table"),tr=el("tr");head.forEach(function(h){tr.appendChild(el("th",null,h));});tb.appendChild(tr);
 rows.forEach(function(r){var tr=el("tr");r.forEach(function(c){var td=el("td");if(c instanceof Node)td.appendChild(c);else td.textContent=c===undefined||c===null?"":String(c);tr.appendChild(td);});tb.appendChild(tr);});w.appendChild(tb);return w;}
function kv(pairs){var d=el("div","kv");pairs.forEach(function(p){d.appendChild(el("span","dim",p[0]));var v=el("span");if(p[1] instanceof Node)v.appendChild(p[1]);else v.textContent=p[1];d.appendChild(v);});return d;}
function empty(s,msg){s.appendChild(el("div","empty",msg));}

function render(g){
 document.getElementById("main").textContent="";
 document.getElementById("gen").textContent="updated "+t(g.generated_at*1000);
 document.getElementById("note").textContent=g.data_note;
 // market
 var s=sec("MARKET"),rg=g.regime||{};
 s.appendChild(kv([["regime",el("span",rg.trend==="uptrend"?"up":rg.trend==="downtrend"?"down":"amber",(rg.trend||"n/a").toUpperCase())],
  ["SPY vs 50/200d",n(rg.spy)+" / "+n(rg.sma50)+" / "+n(rg.sma200)],["VIX",(rg.vix?n(rg.vix,1):"n/a")+" "+(rg.volatility||"")],
  ["10y yield",g.ten_year?n(g.ten_year.yield_pct)+"% (Yahoo ^TNX, "+t(g.ten_year.quote_time*1000)+")":"n/a"]]));
 s.appendChild(table(["","last","chg","quote"],(g.indexes||[]).map(function(x){return [x.symbol,n(x.price),pct(x.change_pct),t(x.quote_time*1000)];})));
 // macro
 s=sec("ECONOMY (FRED)");var ms=(g.macro||{}).series||{},rows=[];
 ["FEDFUNDS","CPIAUCSL","UNRATE","DGS2","DGS10","T10Y2Y","ICSA"].forEach(function(k){var x=ms[k];if(x)rows.push([link(x.url,x.label),k==="ICSA"?n(x.value,0):n(x.yoy_pct!==undefined?x.yoy_pct:x.value),el("span","dim",x.date)]);});
 rows.length?s.appendChild(table(["series","value","as of"],rows)):empty(s,"No macro data yet.");
 // account
 s=sec("PAPER ACCOUNT");var a=g.account||{};
 s.appendChild(el("div","big","$"+n(a.equity)));
 s.appendChild(kv([["cash","$"+n(a.cash)],["total return",pct(a.total_return_pct)],["today",pct(a.day_pnl_pct)],
  ["drawdown",el("span",a.drawdown_pct>g.risk_limits.max_drawdown_pct/2?"amber":"dim",n(a.drawdown_pct)+"% (kill switch "+g.risk_limits.max_drawdown_pct+"%)")],
  ["positions",(g.positions||[]).length+" / "+g.risk_limits.max_open_positions]]));
 // equity curve
 s=sec("EQUITY CURVE","w6");var ec=g.equity_curve||[];
 if(ec.length<2){empty(s,"Needs at least two equity marks. One is recorded each run.");}else{
  var NS="http://www.w3.org/2000/svg",svg=document.createElementNS(NS,"svg"),W=600,H=160,P=4;svg.setAttribute("viewBox","0 0 "+W+" "+H);svg.setAttribute("preserveAspectRatio","none");
  var vs=ec.map(function(p){return p[1];}),lo=Math.min.apply(null,vs.concat([a.start])),hi=Math.max.apply(null,vs.concat([a.start]));
  var minSpan=a.start*0.02;if(hi-lo<minSpan){var mid=(hi+lo)/2;hi=mid+minSpan/2;lo=mid-minSpan/2;}  /* pennies must not look like a crash */
  var t0=ec[0][0],t1=ec[ec.length-1][0]||t0+1,pts=ec.map(function(p){return (P+(W-2*P)*(p[0]-t0)/Math.max(1,t1-t0)).toFixed(1)+","+(H-P-(H-2*P)*(p[1]-lo)/(hi-lo)).toFixed(1);}).join(" ");
  var base=document.createElementNS(NS,"line"),y0=H-P-(H-2*P)*(a.start-lo)/(hi-lo);base.setAttribute("x1",0);base.setAttribute("x2",W);base.setAttribute("y1",y0);base.setAttribute("y2",y0);base.setAttribute("stroke","#1b2428");base.setAttribute("stroke-dasharray","4 4");svg.appendChild(base);
  var pl=document.createElementNS(NS,"polyline");pl.setAttribute("points",pts);pl.setAttribute("fill","none");pl.setAttribute("stroke",vs[vs.length-1]>=a.start?"#39d98a":"#ff5c5c");pl.setAttribute("stroke-width","1.5");pl.setAttribute("vector-effect","non-scaling-stroke");svg.appendChild(pl);
  s.appendChild(svg);s.appendChild(el("div","dim","$"+n(lo)+" to $"+n(hi)+" · "+ec.length+" marks · dashed line = starting cash"));}
 // positions
 s=sec("POSITIONS","w6");
 (g.positions||[]).length?s.appendChild(table(["sym","qty","avg","last","P&L","stop","target","strategy"],g.positions.map(function(p){
  var en=((g.held_catalysts||{})[p.symbol]||{}).earnings_next;var sym=el("span","hl",p.symbol);if(en&&en.days<=7){sym=el("span","amber",p.symbol+" ⚠ earnings "+en.date);}
  return [sym,n(p.qty,4),n(p.avg_price),n(p.last),pct(p.pnl_pct),p.stop_price?n(p.stop_price):"none",p.target_price?n(p.target_price):"rule",p.strategy];}))):empty(s,"No open positions.");
 // opportunities
 s=sec("OPPORTUNITIES","w8");
 if(!(g.cards||[]).length)empty(s,"No strategy setups fired. Nothing to buy is a valid answer.");
 (g.cards||[]).slice(0,8).forEach(function(c){var d=el("div","card"),h=el("div");
  h.appendChild(el("span","hl",c.symbol+"  "));h.appendChild(el("span",c.score>=60?"up":"amber",c.score+"/100  "));h.appendChild(el("span",null,c.strategy_label));
  if(c.signal_age_days)h.appendChild(el("span","dim","  (signal "+c.signal_age_days+"d ago)"));d.appendChild(h);
  d.appendChild(el("div","dim","entry "+n(c.entry_zone[0])+"-"+n(c.entry_zone[1])+" · stop "+n(c.stop)+" · target "+n(c.targets[0])+" · R/R "+(c.risk_reward||"n/a")+" · "+c.confidence));
  var p=c.score_parts||{};d.appendChild(el("div","dim","score: setup "+p.setup+" + evidence "+p.evidence+" + trend "+p.trend+" + fundamentals "+p.fundamentals+" + catalyst "+p.catalyst+" − penalties "+c.penalty));
  (c.technical||[]).slice(0,3).forEach(function(x){d.appendChild(el("div",null,"· "+x));});
  d.appendChild(el("div","wrap amber","risks: "+(c.fail_reasons||[]).slice(0,3).join("; ")));s.appendChild(d);});
 // alerts
 s=sec("ALERTS · 7 DAYS");
 (g.alerts||[]).length?(g.alerts||[]).slice(0,14).forEach(function(x){var d=el("div","card");d.appendChild(el("span","sev-"+x.severity,"["+x.severity+"] "));
  d.appendChild(x.url?link(x.url,x.title):el("span",null,x.title));d.appendChild(el("div","dim",t(x.created_at)));s.appendChild(d);}):empty(s,"No alerts.");
 // catalysts
 s=sec("CATALYST WATCH","w6");s.appendChild(el("div","dim","score = how much is happening, not direction · news is a keyword read, unconfirmed"));
 var cs=(g.catalysts||[]).filter(function(c){return c.score>0;});
 cs.length?cs.slice(0,8).forEach(function(c){var d=el("div","card"),h=el("div");h.appendChild(el("span","hl",c.symbol+" "));h.appendChild(el("span","amber",c.score+"/100"));d.appendChild(h);
  (c.items||[]).slice(0,3).forEach(function(i){var r=el("div","wrap");r.appendChild(el("span",i.confirmed?"up":"dim",i.confirmed?"✓ ":"· "));r.appendChild(el("span","dim",(i.date||"")+" "));r.appendChild(link(i.url,i.text));r.appendChild(el("span","dim"," ("+i.source+")"));d.appendChild(r);});
  (c.risks||[]).slice(0,1).forEach(function(r){d.appendChild(el("div","wrap down","risk: "+r));});
  (c.holders_13f||[]).slice(0,1).forEach(function(h){d.appendChild(el("div","wrap dim","13F: "+h.text));});s.appendChild(d);}):empty(s,"Nothing notable.");
 // calendar
 s=sec("CALENDAR · 10-14 DAYS","w6");var ev=[];
 (g.econ_calendar||[]).forEach(function(e){ev.push([e.detail.date,el("span","cyan",e.title),e.detail.time||""]);});
 (g.earnings_calendar||[]).forEach(function(e){ev.push([e.detail.date,link(e.url,e.symbol+" earnings"),(e.detail.time||"")+(e.detail.eps_forecast?" · EPS est "+e.detail.eps_forecast:"")]);});
 ev.sort(function(a,b){return a[0]<b[0]?-1:1;});ev.length?s.appendChild(table(["date","event","detail"],ev)):empty(s,"Nothing scheduled that we track.");
 // strategy lab
 s=sec("STRATEGY LAB · WALK-FORWARD","w8");
 s.appendChild(table(["strategy","verdict","window","trades","win","avg/trade","PF","return","max dd","SPY"],[].concat.apply([],(g.strategies||[]).map(function(x){
  return ["train","validation","test"].map(function(w,i){var m=x[w]||{};return [i?"":x.label,i?"":el("span","pill "+(x.verdict==="pass"?"pass":"fail"),(x.verdict||"untested").toUpperCase()),w,m.trades,n(m.win_rate,0)+"%",pct(m.expectancy),n(m.profit_factor),pct(m.total_return),pct(m.max_drawdown),i===2&&x.bench_test?pct(x.bench_test.total_return):""];});}))));
 s.appendChild(el("div","dim","parameters chosen on train only · test window never used for fitting · universe is today's large caps (survivorship bias)"));
 // performance
 s=sec("CALL ACCURACY");var pf=g.performance||{};
 ["live","replay"].forEach(function(k){var r=pf[k]||{},o=r.overall||{};var d=el("div","card");d.appendChild(el("div","hl",k==="live"?"Live calls":"Replay (test window)"));
  if(!o.n)d.appendChild(el("div","dim","none graded yet · "+(r.open||0)+" open"));else{
   d.appendChild(kv([["graded",o.n+" ("+r.open+" open)"],["win rate",n(o.win_rate,0)+"%"],["avg result",pct(o.avg_ret)],["SPY same days",pct(o.avg_bench)],["avg R",o.avg_r===null?"n/a":n(o.avg_r)]]));
   (r.by_score||[]).forEach(function(b){if(!b.n)return;var row=el("div");row.appendChild(el("span","dim","score "+b.range+" "));var bar=el("div","bar"),i=el("i");i.style.width=Math.max(0,Math.min(100,b.win_rate))+"%";bar.appendChild(i);row.appendChild(el("span",null,n(b.win_rate,0)+"% win, n="+b.n));row.appendChild(bar);d.appendChild(row);});
   if(r.score_vs_result)d.appendChild(el("div","wrap amber",r.score_vs_result.reading));}
  s.appendChild(d);});
 // orders
 s=sec("ORDERS","w6");(g.orders||[]).length?s.appendChild(table(["#","time","side","sym","qty","status","fill","strategy","reason"],g.orders.map(function(o){
  return [o.id,t(o.created_at),o.side,o.symbol,n(o.qty,4),el("span",o.status==="filled"?"up":"down",o.status),o.fill_price?n(o.fill_price):"",o.strategy,el("span","dim",o.reason||"")];}))):empty(s,"No orders yet.");
 // freshness
 s=sec("DATA FRESHNESS","w6");s.appendChild(table(["source","last updated"],(g.freshness||[]).map(function(f){var old=!f.as_of_ms||(Date.now()-f.as_of_ms)>4*86400000;return [f.what,el("span",old?"amber":"dim",t(f.as_of_ms))];})));
 s.appendChild(el("h2",null,"AUDIT TRAIL"));s.appendChild(table(["time","actor","action"],(g.audit||[]).slice(0,10).map(function(x){return [t(x.ts),x.actor,x.action];})));
}
var raw=document.getElementById("state").textContent;
if(raw&&raw.trim()&&raw.indexOf("__STATE")<0){render(JSON.parse(raw));}
else{var load=function(){fetch("/api/state",{cache:"no-store"}).then(function(r){return r.json();}).then(render).catch(function(e){document.getElementById("note").textContent="Could not load state: "+e;});};load();setInterval(load,60000);}
})();
</script></body></html>
"""


def _safe_json(obj):
    # inside <script type="application/json">: stop "</script>" and "<!--" from ending the block early
    return json.dumps(obj, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def page(state=None, nonce=None):
    nonce = nonce or secrets.token_urlsafe(16)
    return PAGE.replace("__NONCE__", nonce).replace("__STATE__", _safe_json(state) if state is not None else ""), nonce


def csp(nonce):
    return ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % (nonce, nonce))


def write_static(con, out=None):
    g = gather(con)
    html, _ = page(g)
    out = Path(out or config.DATA_DIR / "reports" / "dashboard.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "WallStreet"
    sys_version = ""
    cache = {"at": 0, "body": b""}
    lock = threading.Lock()

    def log_message(self, fmt, *args):                  # keep the console quiet; nothing sensitive is logged
        pass

    def _send(self, code, body, ctype, nonce=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if nonce:
            self.send_header("Content-Security-Policy", csp(nonce))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            html, nonce = page(None)
            return self._send(200, html.encode(), "text/html; charset=utf-8", nonce)
        if path == "/api/state":
            with self.lock:
                if time.time() - self.cache["at"] > 30:
                    con = db.connect()
                    try:
                        _Handler.cache = {"at": time.time(), "body": json.dumps(gather(con), default=str).encode()}
                    finally:
                        con.close()
                body = self.cache["body"]
            return self._send(200, body, "application/json")
        return self._send(404, b"not found", "text/plain")

    def _no(self):
        self._send(405, b"read-only", "text/plain")

    do_POST = do_PUT = do_DELETE = do_PATCH = _no


def serve(host=HOST, port=PORT):
    srv = http.server.ThreadingHTTPServer((host, port), _Handler)
    print("Dashboard on http://%s:%d (read-only). Ctrl+C to stop." % (host, port))
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: listening beyond this machine. There is no login; put it behind a VPN or an authenticating proxy.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv
