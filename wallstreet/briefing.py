"""Daily briefing. gather() builds one snapshot of everything the platform knows; the briefing (Markdown) and the
dashboard both render from it, so they can never disagree."""
import json
import time
from pathlib import Path

from . import alerts, catalysts, config, db, performance
from .analysis import regime
from .broker.paper import PaperBroker
from .data import calendar, prices
from .strategies import STRATEGIES

INDEXES = [("SPY", "S&P 500 ETF"), ("QQQ", "Nasdaq 100 ETF"), ("IWM", "Russell 2000 ETF"), ("DIA", "Dow ETF")]


def _chg(con, sym):
    b = prices.bars(con, sym)
    q = prices.last_quote(con, sym)
    if len(b) < 2:
        return None
    last = q["price"] if q else b[-1][4]
    # bars hold finished days only; a quote newer than the last bar is today's price, so compare it to that bar
    prev = b[-1][4] if q and q["quote_time"] > b[-1][0] + 12 * 3600 else b[-2][4]
    return {"symbol": sym, "price": last, "change_pct": (last / prev - 1) * 100,
            "quote_time": q["quote_time"] if q else b[-1][0], "source": q["source"] if q else "daily bar"}


def wf_results(con):
    return {n: db.kv_get(con, "wf_" + n) or {} for n in STRATEGIES}


def gather(con, cards=None, symbols=None):
    symbols = symbols or config.UNIVERSE
    wf = wf_results(con)
    vix = prices.last_quote(con, "^VIX")
    tnx = prices.last_quote(con, "^TNX")
    rg = regime.at(prices.bars(con, "SPY"), vix=vix["price"] if vix else None)
    broker = PaperBroker(con)
    st = broker.state()
    held = [p["symbol"] for p in st["positions"]]
    cats = {s: catalysts.for_symbol(con, s) for s in symbols if s not in ("SPY", "QQQ", "IWM", "DIA")}
    eq = [(r["ts"], r["value"]) for r in con.execute("SELECT ts, value FROM equity ORDER BY ts")]
    perf = performance.summary(con, wf)
    if cards is None:
        cards = db.kv_get(con, "last_scan", [])
    # policy items (FOMC statements, minutes, rate decisions) only; bank merger approvals and enforcement orders are noise here
    fed = [dict(r) for r in con.execute("SELECT title, url, published_at FROM events WHERE kind='fed_policy' AND published_at>=? "
                                        "ORDER BY published_at DESC LIMIT 5", (db.now_ms() - 60 * 86400000,))]
    spy_q = prices.last_quote(con, "SPY")
    freshness = []
    for label, sql in (("Prices (SPY quote)", "SELECT MAX(quote_time)*1000 FROM quotes WHERE symbol='SPY'"),
                       ("SEC filings", "SELECT MAX(fetched_at) FROM filings"),
                       ("News", "SELECT MAX(fetched_at) FROM events WHERE kind='news'"),
                       ("Earnings calendar", "SELECT MAX(fetched_at) FROM events WHERE kind='earnings'"),
                       ("Economic calendar", "SELECT MAX(fetched_at) FROM events WHERE kind='econ'"),
                       ("Fed releases", "SELECT MAX(fetched_at) FROM events WHERE kind LIKE 'fed_%'"),
                       ("13F holdings", "SELECT MAX(fetched_at) FROM holdings13f")):
        freshness.append({"what": label, "as_of_ms": con.execute(sql).fetchone()[0]})
    macro = db.kv_get(con, "macro") or {}
    return {
        "generated_at": int(time.time()), "paper_only": True,
        "data_note": "Prices: %s. Delayed, not real-time. News headlines are unconfirmed; SEC and Fed items are primary sources." % (
            spy_q["source"] if spy_q else "none"),
        "regime": rg, "vix": vix, "ten_year": {"yield_pct": tnx["price"], "quote_time": tnx["quote_time"], "source": tnx["source"]} if tnx else None,
        "indexes": [x | {"name": n} for s, n in INDEXES for x in [_chg(con, s)] if x],
        "macro": macro, "econ_calendar": calendar.upcoming(con, "econ", 10),
        "earnings_calendar": [e for e in calendar.upcoming(con, "earnings", 14) if e["symbol"] in symbols],
        "fed": fed, "cards": cards, "catalysts": sorted(cats.values(), key=lambda c: -c["score"])[:12],
        "held_catalysts": {s: cats[s] for s in held if s in cats},
        "account": {k: v for k, v in st.items() if k != "positions"}, "positions": st["positions"],
        "equity_curve": eq[-500:],
        "orders": [dict(r) for r in con.execute("SELECT id, client_id, symbol, side, qty, status, reason, created_at, fill_price, strategy "
                                                 "FROM orders ORDER BY id DESC LIMIT 25")],
        "alerts": alerts.recent(con, 7),
        "strategies": [{"name": n, "label": STRATEGIES[n][1], "verdict": (wf.get(n) or {}).get("verdict"), "params": (wf.get(n) or {}).get("params"),
                        "windows": (wf.get(n) or {}).get("windows"),
                        **{k: (wf.get(n) or {}).get(k) for k in ("train", "validation", "test", "bench_test")}} for n in STRATEGIES],
        "performance": perf, "freshness": freshness, "risk_limits": config.RISK,
        "audit": [dict(r) for r in con.execute("SELECT ts, actor, action FROM audit ORDER BY id DESC LIMIT 15")],
    }


def _t(ms):
    return time.strftime("%b %d %H:%M", time.localtime(ms / 1000)) if ms else "never"


def render_md(g):
    L = ["# Daily briefing, %s" % time.strftime("%A %B %d, %Y", time.localtime(g["generated_at"])), "",
         "_Paper trading only. Not financial advice. %s_" % g["data_note"], ""]
    rg = g["regime"] or {}
    L += ["## Market", ""]
    if rg:
        L.append("- **Regime: %s.** SPY %.2f vs 50-day %.2f and 200-day %.2f. VIX %s (%s)." % (
            rg["trend"], rg["spy"], rg["sma50"], rg["sma200"], "%.1f" % rg["vix"] if rg.get("vix") else "n/a", rg["volatility"]))
    for x in g["indexes"]:
        L.append("- %s (%s): %.2f, %+.2f%% (quote %s)" % (x["name"], x["symbol"], x["price"], x["change_pct"], _t(x["quote_time"] * 1000)))
    if g["ten_year"]:
        L.append("- 10-year Treasury yield: %.2f%% (Yahoo ^TNX, delayed, %s; FRED's official daily value is below and lags a day or two)" % (
            g["ten_year"]["yield_pct"], _t(g["ten_year"]["quote_time"] * 1000)))
    ms = (g["macro"] or {}).get("series", {})
    if ms:
        L += ["", "Economy (FRED, each with its own date):"]
        for sid in ("FEDFUNDS", "CPIAUCSL", "UNRATE", "DGS2", "DGS10", "T10Y2Y", "ICSA"):
            x = ms.get(sid)
            if x:
                v = x.get("yoy_pct", x["value"])
                L.append("- %s: %s (as of %s)" % (x["label"], format(v, ",.0f") if sid == "ICSA" else "%.2f" % v, x["date"]))
    L += ["", "## Calendar (next 10 days)", ""]
    ev = sorted([(e["detail"].get("date"), "%s, %s" % (e["title"], e["detail"].get("time", ""))) for e in g["econ_calendar"]] +
                [(e["detail"].get("date"), "%s earnings (%s)%s" % (e["symbol"], e["detail"].get("time"),
                  ", EPS est %s" % e["detail"]["eps_forecast"] if e["detail"].get("eps_forecast") else "")) for e in g["earnings_calendar"]])
    L += ["- %s: %s" % e for e in ev] or ["- Nothing scheduled that we track."]
    if g["fed"]:
        L += ["", "Fed policy (last 60 days):"] + ["- %s: [%s](%s)" % (_t(f["published_at"]), f["title"], f["url"]) for f in g["fed"][:3]]
    L += ["", "## Opportunities", ""]
    if not g["cards"]:
        L.append("No strategy setups fired. Nothing to buy is a valid answer.")
    for c in g["cards"][:6]:
        L.append("- **%s, score %d/100**: %s. Entry %.2f-%.2f, stop %.2f, target %.2f. Confidence %s." % (
            c["symbol"], c["score"], c["strategy_label"], c["entry_zone"][0], c["entry_zone"][1], c["stop"], c["targets"][0], c["confidence"]))
        L.append("  Risks: " + "; ".join(c["fail_reasons"][:3]))
    L += ["", "## Catalyst watch", "", "Score = how much is happening, not which way. Headlines are a keyword read and unconfirmed.", ""]
    for c in [c for c in g["catalysts"] if c["score"] > 0][:8]:
        top = "; ".join("%s (%s)" % (i["text"][:90], i["source"]) for i in c["items"][:2])
        L.append("- **%s %d/100**: %s" % (c["symbol"], c["score"], top))
        for r in c["risks"][:2]:
            L.append("  - Risk: " + r)
    if not any(c["score"] for c in g["catalysts"]):
        L.append("- Nothing notable.")
    a = g["account"]
    L += ["", "## Paper portfolio", "",
          "Equity $%s (cash $%s) · total return %+.2f%% · drawdown %.2f%% · today %+.2f%%" % (
              format(a["equity"], ",.2f"), format(a["cash"], ",.2f"), a["total_return_pct"], a["drawdown_pct"], a["day_pnl_pct"]), ""]
    for p in g["positions"]:
        en = (g["held_catalysts"].get(p["symbol"]) or {}).get("earnings_next")
        L.append("- %s: %s sh @ %.2f, last %.2f, P&L $%+.2f (%+.1f%%), stop %s%s" % (
            p["symbol"], ("%.4f" % p["qty"]).rstrip("0").rstrip("."), p["avg_price"], p["last"], p["pnl"], p["pnl_pct"],
            "%.2f" % p["stop_price"] if p["stop_price"] else "none", " · EARNINGS %s" % en["date"] if en and en["days"] <= 7 else ""))
    if not g["positions"]:
        L.append("- No open positions.")
    L += ["", "## Alerts (last 7 days)", ""]
    L += ["- %s [%s] %s" % (_t(x["created_at"]), x["severity"], x["title"]) for x in g["alerts"][:12]] or ["- None."]
    L += ["", "## How the calls are doing", ""]
    for src in ("live", "replay"):
        r = g["performance"][src]
        o = r["overall"]
        L.append("- %s: %s" % ("Live" if src == "live" else "Replay (test window, historical)", performance._fmt(o) + " · %d open" % r["open"]))
    L.append("- " + g["performance"]["replay"]["score_vs_result"]["reading"])
    L += ["", "## Data freshness", ""] + ["- %s: %s" % (f["what"], _t(f["as_of_ms"])) for f in g["freshness"]]
    return "\n".join(L) + "\n"


def write(con, cards=None, out_dir=None):
    g = gather(con, cards)
    md = render_md(g)
    out_dir = Path(out_dir or config.DATA_DIR / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / time.strftime("briefing_%Y-%m-%d.md")
    p.write_text(md)
    (out_dir / time.strftime("state_%Y-%m-%d.json")).write_text(json.dumps(g, default=str))
    return p, md, g
