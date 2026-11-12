"""Command line: python -m wallstreet <command>

  refresh            download prices (10 years daily), SEC filings + insider trades + fundamentals, Fed releases
  backtest           walk-forward test every strategy; results saved for the scanner
  scan               rank today's opportunities (needs refresh + backtest first)
  status             paper account: cash, positions, equity, drawdown
  buy SYM QTY STOP [TARGET]   simulated market buy through the risk engine
  sell SYM QTY       simulated market sell
  exits              sell positions whose stop or target was hit
  demo               the full simulated session: data -> regime -> scan -> analysis -> risk -> order -> portfolio
"""
import json
import logging
import sys
import time

from . import backtest, config, db, risk
from .analysis import indicators as ind
from .broker.paper import OrderError, PaperBroker
from .data import fed, prices, sec
from .data.http import DataError
from .scanner import scan
from .strategies import STRATEGIES


def setup_logging():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(config.LOG_PATH), logging.StreamHandler(sys.stderr)])
    logging.getLogger("pws.http").setLevel(logging.WARNING)


def ts(sec_):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(sec_)) if sec_ else "n/a"


def cmd_refresh(con, symbols=None, sec_data=True):
    symbols = symbols or config.UNIVERSE
    ok, failed = prices.refresh_many(con, symbols, rng="10y")
    print("Prices: %d symbols refreshed%s" % (len(ok), ", failed: %s" % failed if failed else ""))
    if sec_data:
        n_f = n_i = 0
        for s in symbols:
            if risk.sector(s) == "Index":
                continue
            try:
                a, b = sec.refresh(con, s)
                sec.fundamentals(con, s)
                n_f, n_i = n_f + a, n_i + b
            except DataError as e:
                print("  SEC %s: %s" % (s, e))
        print("SEC: %d new filings, %d insider transaction lines" % (n_f, n_i))
    try:
        print("Fed: %d new releases" % fed.refresh(con))
    except DataError as e:
        print("Fed feed failed: %s" % e)
    return ok


def cmd_backtest(con):
    bb = {s: prices.bars(con, s) for s in config.UNIVERSE}
    out = {}
    for name in STRATEGIES:
        r = backtest.walk_forward(bb, name)
        db.kv_set(con, "wf_" + name, r)
        out[name] = r
        print_wf(r)
    return out


def print_wf(r):
    if "error" in r:
        print(r["error"])
        return
    w = r["windows"]
    f = lambda m: "%3d trades · win %2.0f%% · avg %+.2f%%/trade · profit factor %.2f · return %+.0f%% · worst drop %.0f%% · Sharpe %.2f" % (  # noqa: E731
        m["trades"], m["win_rate"], m["expectancy"], m["profit_factor"], m["total_return"], m["max_drawdown"], m["sharpe"])
    print("\n%s (%s) · parameters chosen on train only: %s · %s" % (r["name"], r["strategy"], r["params"], r["verdict"].upper()))
    for k, a, b, bk in (("train", w[0], w[1], "bench_train"), ("validation", w[1], w[2], "bench_val"), ("test", w[2], w[3], "bench_test")):
        bm = r.get(bk) or {}
        print("  %-10s %s to %s  %s  | SPY %+.0f%% (worst %.0f%%)" % (k, time.strftime("%Y-%m-%d", time.gmtime(a)), time.strftime("%Y-%m-%d", time.gmtime(b)),
                                                                   f(r[k]), bm.get("total_return", 0), bm.get("max_drawdown", 0)))


def wf_results(con):
    return {n: db.kv_get(con, "wf_" + n) or {} for n in STRATEGIES}


def print_card(c):
    print("\n%s · score %d/100 · %s%s" % (c["symbol"], c["score"], c["strategy_label"],
                                         " (signal %d day%s ago)" % (c["signal_age_days"], "s" if c["signal_age_days"] > 1 else "") if c["signal_age_days"] else " (signal on the last close)"))
    print("  Price $%.2f (quote %s, %s)" % (c["price"], ts(c["quote_time"]), c["quote_source"]))
    print("  Volume %s (%.1fx its 20-day average)" % (format(int(c["volume"]), ","), c["rel_volume"] or 0))
    for x in c["technical"]:
        print("  - " + x)
    for x in c["fundamental"]:
        print("  - " + x)
    for n in c["news"]:
        print("  - %s %s (%s)" % (n["date"], n["text"], n["url"]))
    print("  Bull case: " + c["bull"])
    print("  Bear case: " + c["bear"])
    print("  Entry zone %.2f-%.2f · stop %.2f · targets %s · reward/risk %s" % (c["entry_zone"][0], c["entry_zone"][1], c["stop"],
                                                                                 ", ".join("%.2f" % t for t in c["targets"]), c["risk_reward"]))
    print("  Confidence: " + c["confidence"])
    print("  Could fail because: " + "; ".join(c["fail_reasons"]))


def regime(con):
    """Market regime from SPY and the VIX, facts only."""
    spy = [b[4] for b in prices.bars(con, "SPY")]
    vix = prices.last_quote(con, "^VIX")
    s50, s200 = ind.sma(spy, 50)[-1], ind.sma(spy, 200)[-1]
    trend = "uptrend" if spy[-1] > s50 > s200 else "downtrend" if spy[-1] < s50 < s200 else "mixed"
    v = vix["price"] if vix else None
    vol = "calm" if v and v < 15 else "normal" if v and v < 22 else "stressed" if v else "unknown"
    return {"spy": spy[-1], "sma50": s50, "sma200": s200, "trend": trend, "vix": v, "volatility": vol}


def demo_candidate(con, symbols):
    """Strongest 3-month relative strength among stocks in an uptrend. Used only to exercise the order path in the demo."""
    from .analysis import technical
    spy = prices.bars(con, "SPY")
    best = None
    for s in symbols:
        if risk.sector(s) == "Index":
            continue
        snap = technical.snapshot(prices.bars(con, s), bench=spy)
        if snap and snap["trend"] == "up" and snap["rel_strength_3m"] is not None and (not best or snap["rel_strength_3m"] > best[1]["rel_strength_3m"]):
            best = (s, snap)
    if not best:
        return None
    s, snap = best
    q = prices.last_quote(con, s)
    return {"symbol": s, "score": 0, "strategy": "demo", "strategy_label": "demonstration only (no live setup)", "signal_age_days": 0,
            "price": q["price"], "quote_time": q["quote_time"], "quote_source": q["source"], "bar_time": snap["ts"],
            "volume": snap["volume"], "rel_volume": snap["rel_volume"], "technical": technical.describe(snap), "fundamental": [],
            "news": [], "bull": "n/a (demo)", "bear": "n/a (demo)", "entry_zone": [q["price"], q["price"]],
            "stop": round(q["price"] - 2 * snap["atr"], 2), "targets": [round(q["price"] + 3 * snap["atr"], 2)], "risk_reward": 1.5,
            "confidence": "none: demonstration order", "fail_reasons": ["No strategy signal; this order only tests the pipeline"],
            "evidence": {"demo": True}}


def cmd_demo(con):
    print("=" * 70 + "\nSIMULATED TRADING SESSION (paper only, no real orders possible)\n" + "=" * 70)
    print("\n1) Market data entering the platform")
    ok = cmd_refresh(con, sec_data=True)
    prices.refresh_many(con, ["^VIX", "^GSPC", "^IXIC", "^DJI", "^RUT", "^TNX"], rng="1y")
    for s in ("SPY", "CSCO", "^VIX"):
        q = prices.last_quote(con, s)
        print("   %s $%.2f · quote time %s · fetched %s · %s" % (s, q["price"], ts(q["quote_time"]), ts(q["fetched_at"] / 1000), q["source"]))
    print("\n2) Market regime")
    rg = regime(con)
    print("   S&P 500 (SPY) %.2f vs 50-day %.2f and 200-day %.2f: %s · VIX %s (%s)" % (rg["spy"], rg["sma50"], rg["sma200"], rg["trend"],
                                                                                    "%.1f" % rg["vix"] if rg["vix"] else "n/a", rg["volatility"]))
    print("\n3) Strategy lab (walk-forward)")
    wf = cmd_backtest(con)
    print("\n4) Scanner")
    cards = scan(con, ok, wf)
    print("   %d setups fired in the last 5 trading days out of %d symbols" % (len(cards), len(ok)))
    for c in cards[:5]:
        print("   %3d  %-6s %s, signal %s" % (c["score"], c["symbol"], c["strategy_label"], "on the last close" if not c["signal_age_days"] else "%d day(s) ago" % c["signal_age_days"]))
    if not cards:
        print("   No live setups right now. Nothing to trade is a valid answer, and the scanner says so.")
        best = demo_candidate(con, ok)
        if not best:
            return
        print("   To exercise the rest of the pipeline, the demo continues with the strongest uptrend on the list, %s." % best["symbol"])
        print("   This is a DEMONSTRATION order (strategy 'demo'), not a recommendation.")
    else:
        best = cards[0]
    print("\n5) Analysis of the top candidate")
    print_card(best)
    print("\n6) Risk engine")
    br = PaperBroker(con)
    st = br.mark()
    qty = risk.size_for_risk(st["equity"], best["price"], best["stop"], st["cash"])
    ok_, why, nums = risk.check_buy(con, st, best["symbol"], qty, best["price"], best["stop"])
    print("   Equity $%.2f · sized to %s shares (risking %.2f%% if stopped) · %s" % (st["equity"], qty, nums.get("risk_pct", 0),
                                                                                   "APPROVED" if ok_ else "REJECTED: " + "; ".join(why)))
    print("\n7) Simulated order")
    cid = "demo-%s-%s" % (best["symbol"], best["bar_time"])
    o = br.submit(best["symbol"], "buy", qty, stop=best["stop"], target=best["targets"][0], client_id=cid,
                  strategy=best["strategy"], evidence={"score": best["score"], "card": best["evidence"]})
    print("   %s: buy %s x%s at %s · %s" % (o["status"].upper(), o["symbol"], o["qty"], "%.2f" % o["fill_price"] if o["fill_price"] else "-", o["reason"] or "ok"))
    o2 = br.submit(best["symbol"], "buy", qty, stop=best["stop"], client_id=cid)
    print("   Same order sent again: %s (order #%s, no second fill)" % ("ignored" if o2["id"] == o["id"] else "DUPLICATE FILL", o2["id"]))
    print("\n8) Portfolio")
    st = br.mark()
    print("   Cash $%.2f · equity $%.2f · total return %+.2f%% · drawdown %.2f%%" % (st["cash"], st["equity"], st["total_return_pct"], st["drawdown_pct"]))
    for p in st["positions"]:
        print("   %s x%s @ %.2f · stop %.2f · target %s · P&L $%+.2f" % (p["symbol"], p["qty"], p["avg_price"], p["stop_price"] or 0,
                                                                     "%.2f" % p["target_price"] if p["target_price"] else "-", p["pnl"]))
    print("\n9) Performance records")
    for t in ("orders", "predictions", "audit", "equity"):
        print("   %s: %d rows" % (t, con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    setup_logging()
    con = db.connect()
    cmd = argv[0] if argv else "help"
    try:
        if cmd == "refresh":
            cmd_refresh(con)
        elif cmd == "backtest":
            cmd_backtest(con)
        elif cmd == "scan":
            for c in scan(con, config.UNIVERSE, wf_results(con)):
                print_card(c)
        elif cmd == "status":
            st = PaperBroker(con).mark()
            print(json.dumps({k: v for k, v in st.items()}, indent=2, default=str))
        elif cmd in ("buy", "sell"):
            sym, qty = argv[1], float(argv[2])
            stop = float(argv[3]) if len(argv) > 3 else None
            target = float(argv[4]) if len(argv) > 4 else None
            print(json.dumps(PaperBroker(con).submit(sym, cmd, qty, stop=stop, target=target), indent=2, default=str))
        elif cmd == "exits":
            print(json.dumps(PaperBroker(con).check_exits(), indent=2, default=str))
        elif cmd == "demo":
            cmd_demo(con)
        else:
            print(__doc__)
    except (OrderError, DataError) as e:
        print("Error: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
