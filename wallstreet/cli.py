"""Command line: python -m wallstreet <command>

  daily              the whole pipeline: refresh everything, grade, scan, paper-trade, alerts, briefing, dashboard
  refresh            prices (10 years daily), SEC filings + insider trades + fundamentals, Fed releases
  intel              news, earnings calendar, economic calendar, macro (FRED), 13F holdings
  backtest           walk-forward test every strategy; results saved for the scanner
  scan               rank today's opportunities and record them as predictions
  grade              grade open predictions against what happened since
  replay             re-run the scanner on every day of the test window (historical predictions, labelled 'replay')
  report [YYYY-MM]   monthly performance report
  briefing           write today's briefing (Markdown)
  dashboard [--static]  serve the dashboard on 127.0.0.1:8765, or write a self-contained HTML snapshot
  catalysts          rank the universe by catalyst score
  status             paper account: cash, positions, equity, drawdown
  buy SYM QTY STOP [TARGET]   simulated market buy through the risk engine
  sell SYM QTY       simulated market sell
  exits              apply stops, targets and strategy exit rules to open paper positions
  demo               the full 10-step simulated session
"""
import json
import logging
import logging.handlers
import sys
import time

from . import alerts, autopilot, backtest, briefing, catalysts, config, dashboard, db, performance, risk
from .analysis import regime as regime_mod
from .broker.paper import OrderError, PaperBroker
from .data import calendar, fed, news, prices, sec, thirteenf
from .data.http import DataError
from .scanner import scan
from .strategies import STRATEGIES

INDEX_TICKERS = ["^VIX", "^GSPC", "^IXIC", "^DJI", "^RUT", "^TNX"]
log = logging.getLogger("pws.cli")


def setup_logging():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.handlers.RotatingFileHandler(config.LOG_PATH, maxBytes=5 << 20, backupCount=5),
                                  logging.StreamHandler(sys.stderr)])
    logging.getLogger("pws.http").setLevel(logging.WARNING)


def ts(sec_):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(sec_)) if sec_ else "n/a"


def cmd_refresh(con, symbols=None, sec_data=True, quiet=False):
    symbols = symbols or config.UNIVERSE
    # full history the first time, then a short window (overlapping bars are replaced, so revisions are picked up)
    have = {r[0]: r[1] for r in con.execute("SELECT symbol, COUNT(*) FROM bars GROUP BY symbol")}
    full = [s for s in symbols if have.get(s, 0) < 1000]
    ok, failed = prices.refresh_many(con, full, rng="10y") if full else ([], {})
    ok2, failed2 = prices.refresh_many(con, [s for s in symbols if s not in full], rng="3mo")
    ok, failed = ok + ok2, failed | failed2
    prices.refresh_many(con, INDEX_TICKERS, rng="1y")
    out = ["Prices: %d symbols refreshed%s" % (len(ok), ", failed: %s" % failed if failed else "")]
    if sec_data:
        n_f = n_i = 0
        errs = []
        for s in symbols:
            if risk.sector(s) == "Index":
                continue
            try:
                a, b = sec.refresh(con, s)
                sec.fundamentals(con, s)
                n_f, n_i = n_f + a, n_i + b
            except DataError as e:
                errs.append("%s: %s" % (s, e))
        out.append("SEC: %d new filings, %d insider transaction lines%s" % (n_f, n_i, (" (%d failed)" % len(errs)) if errs else ""))
    try:
        out.append("Fed: %d new releases" % fed.refresh(con))
    except DataError as e:
        out.append("Fed feed failed: %s" % e)
    if not quiet:
        print("\n".join(out))
    return ok, out


def cmd_intel(con, symbols=None, quiet=False):
    symbols = [s for s in (symbols or config.UNIVERSE) if risk.sector(s) != "Index"]
    n_news = sum(news.refresh(con, s, sec.title_for(s)) for s in symbols)
    for s in symbols:
        news.prune(con, s, sec.title_for(s))
    n_earn = calendar.refresh_earnings(con, symbols)
    n_econ = calendar.refresh_economic(con)
    macro = calendar.refresh_macro(con)
    last13 = db.kv_get(con, "13f_refreshed", 0)
    n13 = None
    if time.time() - last13 > 7 * 86400:                 # 13Fs change quarterly; weekly is plenty
        n13 = thirteenf.refresh(con, set(symbols))
        db.kv_set(con, "13f_refreshed", int(time.time()))
    out = ["News: %d new headlines (deduplicated)" % n_news, "Earnings calendar: %d dates written" % n_earn,
           "Economic calendar: %d events written" % n_econ, "Macro: %d FRED series" % len(macro),
           "13F: %s" % ("%d holdings rows" % n13 if n13 is not None else "fresh (refreshed weekly)")]
    if not quiet:
        print("\n".join(out))
    return out


def cmd_backtest(con, quiet=False):
    bb = {s: prices.bars(con, s) for s in config.UNIVERSE}
    out = {}
    for name in STRATEGIES:
        r = backtest.walk_forward(bb, name)
        db.kv_set(con, "wf_" + name, r)
        out[name] = r
        if not quiet:
            print_wf(r)
    db.kv_set(con, "wf_at", int(time.time()))
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
    return briefing.wf_results(con)


def print_card(c):
    print("\n%s · score %d/100 · %s%s" % (c["symbol"], c["score"], c["strategy_label"],
                                         " (signal %d day%s ago)" % (c["signal_age_days"], "s" if c["signal_age_days"] > 1 else "") if c["signal_age_days"] else " (signal on the last close)"))
    print("  Price $%.2f (quote %s, %s)" % (c["price"], ts(c["quote_time"]), c["quote_source"]))
    print("  Volume %s (%.1fx its 20-day average)" % (format(int(c["volume"]), ","), c["rel_volume"] or 0))
    p = c.get("score_parts") or {}
    if p:
        print("  Score: " + " + ".join("%s %s" % (k, v) for k, v in p.items()) + " - penalties %s" % c.get("penalty", 0))
    for x in c["technical"]:
        print("  - " + x)
    for x in c["fundamental"]:
        print("  - " + x)
    for n in c["news"][:6]:
        print("  - %s %s [%s%s] %s" % (n.get("date"), n["text"][:110], n.get("source"), "" if n.get("confirmed") else ", unconfirmed", n.get("url")))
    cat = c.get("catalyst") or {}
    for h in cat.get("holders_13f", [])[:2]:
        print("  - 13F: " + h["text"])
    print("  Bull case: " + c["bull"])
    print("  Bear case: " + c["bear"])
    print("  Entry zone %.2f-%.2f · stop %.2f · targets %s · reward/risk %s" % (c["entry_zone"][0], c["entry_zone"][1], c["stop"],
                                                                                 ", ".join("%.2f" % t for t in c["targets"]), c["risk_reward"]))
    print("  Confidence: " + c["confidence"])
    print("  Could fail because: " + "; ".join(c["fail_reasons"] + cat.get("risks", [])))


def regime(con):
    vix = prices.last_quote(con, "^VIX")
    return regime_mod.at(prices.bars(con, "SPY"), vix=vix["price"] if vix else None)


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
    cat = catalysts.for_symbol(con, s)
    return {"symbol": s, "score": 0, "strategy": "demo", "strategy_label": "demonstration only (no live setup)", "signal_age_days": 0,
            "price": q["price"], "quote_time": q["quote_time"], "quote_source": q["source"], "bar_time": snap["ts"], "signal_time": snap["ts"],
            "volume": snap["volume"], "rel_volume": snap["rel_volume"], "technical": technical.describe(snap), "fundamental": [],
            "news": cat["items"][:6], "catalyst": cat, "bull": "n/a (demo)", "bear": "n/a (demo)", "entry_zone": [q["price"], q["price"]],
            "stop": round(q["price"] - 2 * snap["atr"], 2), "targets": [round(q["price"] + 3 * snap["atr"], 2)], "risk_reward": 1.5,
            "confidence": "none: demonstration order", "fail_reasons": ["No strategy signal; this order only tests the pipeline"],
            "exit_rule": "target", "params": {}, "evidence": {"demo": True}, "score_parts": {}, "penalty": 0}


def cmd_daily(con, quiet=False):
    """The full pipeline. Safe to run any number of times a day (every step is idempotent).
    A lock file stops two runs (say cron plus a manual run) from overlapping."""
    import fcntl
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(config.DATA_DIR / "daily.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another daily run is in progress; exiting.")
        return []
    try:
        return _daily(con, quiet)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _daily(con, quiet=False):
    log_lines = []

    def say(x):
        log_lines.append(x)
        if not quiet:
            print(x)

    t0 = time.time()
    ok, out = cmd_refresh(con, quiet=True)
    [say(x) for x in out]
    try:
        [say(x) for x in cmd_intel(con, ok, quiet=True)]
    except Exception as e:                               # intel is context; a failure must not stop trading checks
        log.exception("intel failed")
        say("Intel step failed: %s" % e)
    broker = PaperBroker(con)
    broker.mark()
    exits = broker.check_exits()
    say("Exits: %d" % len(exits))
    if time.time() - (db.kv_get(con, "wf_at") or 0) > 7 * 86400:
        cmd_backtest(con, quiet=True)
        say("Walk-forward re-run (weekly)")
    wf = wf_results(con)
    g, still = performance.grade_all(con)
    say("Predictions graded: %d (still open: %d)" % (g, still))
    cards = scan(con, ok, wf)
    db.kv_set(con, "last_scan", cards)
    say("Scanner: %d setups (%d new predictions)" % (len(cards), sum(1 for c in cards if c.get("new"))))
    orders, skipped = autopilot.run(con, broker, cards, wf)
    say("Paper autopilot: %d orders (%s filled)%s" % (len(orders), sum(o["status"] == "filled" for o in orders),
                                                      "; skipped " + ", ".join("%s (%s)" % s for s in skipped[:6]) if skipped else ""))
    st = broker.mark()
    cats = {s: catalysts.for_symbol(con, s) for s in ok if risk.sector(s) != "Index"}
    new_alerts = alerts.generate(con, cards, exits, st, cats)
    say("Alerts: %d new" % len(new_alerts))
    p, _, _ = briefing.write(con, cards)
    d = dashboard.write_static(con)
    rp, _ = performance.monthly_report(con, wf)
    say("Wrote %s, %s, %s" % (p, d, rp))
    say("Done in %.0fs" % (time.time() - t0))
    db.audit(con, "daily", "run", {"lines": log_lines})
    return cards


def cmd_demo(con):
    W = 74
    print("=" * W + "\nINTEGRATED SIMULATED SESSION (paper only: no code path can place a real order)\n" + "=" * W)
    print("\n1) DATA INGESTION")
    ok, out = cmd_refresh(con, quiet=True)
    for x in out + cmd_intel(con, ok, quiet=True):
        print("   " + x)
    for s in ("SPY", "CSCO", "^VIX", "^TNX"):
        q = prices.last_quote(con, s)
        if q:
            print("   %-5s %10.2f · quote time %s · fetched %s · %s" % (s, q["price"], ts(q["quote_time"]), ts(q["fetched_at"] / 1000), q["source"]))
    print("\n2) MARKET REGIME")
    rg = regime(con)
    print("   SPY %.2f vs 50-day %.2f and 200-day %.2f: %s · VIX %s (%s)" % (rg["spy"], rg["sma50"], rg["sma200"], rg["trend"],
                                                                         "%.1f" % rg["vix"] if rg["vix"] else "n/a", rg["volatility"]))
    m = (db.kv_get(con, "macro") or {}).get("series", {})
    for sid in ("FEDFUNDS", "CPIAUCSL", "UNRATE", "DGS10", "T10Y2Y"):
        if sid in m:
            x = m[sid]
            print("   %s: %.2f (as of %s, FRED)" % (x["label"], x.get("yoy_pct", x["value"]), x["date"]))
    for e in calendar.upcoming(con, "econ", 10)[:4]:
        print("   Coming: %s %s, %s" % (e["detail"]["date"], e["title"], e["detail"].get("time")))
    print("\n3) OPPORTUNITY SCANNER")
    wf = wf_results(con)
    if not all((wf.get(n) or {}).get("verdict") for n in STRATEGIES):
        wf = cmd_backtest(con, quiet=True)
    cards = scan(con, ok, wf)
    db.kv_set(con, "last_scan", cards)
    print("   %d setups in the last 5 trading days across %d symbols" % (len(cards), len(ok)))
    for c in cards[:6]:
        print("   %3d  %-6s %s, signal %s" % (c["score"], c["symbol"], c["strategy_label"], "on the last close" if not c["signal_age_days"] else "%d day(s) ago" % c["signal_age_days"]))
    ranked = catalysts.rank(con, [s for s in ok if risk.sector(s) != "Index"])
    print("   Catalyst leaders: " + ", ".join("%s %d" % (c["symbol"], c["score"]) for c in ranked[:5]))
    demo = False
    if cards:
        best = cards[0]
    else:
        print("   No live setups. Nothing to trade is a valid answer, and the scanner says so.")
        best = demo_candidate(con, ok)
        demo = True
        if not best:
            return
        print("   To exercise the rest of the pipeline the session continues with the strongest uptrend, %s," % best["symbol"])
        print("   as a DEMONSTRATION order (strategy 'demo'), not a recommendation.")
    print("\n4) ANALYSIS (rules-based and source-linked; no language model writes these facts)")
    print_card(best)
    print("\n5) RISK DEPARTMENT")
    br = PaperBroker(con)
    st = br.mark()
    qty = risk.size_for_risk(st["equity"], best["price"], best["stop"], st["cash"])
    fill_px = best["price"] * (1 + config.SLIPPAGE_BPS / 10000)
    ok_, why, nums = risk.check_buy(con, st, best["symbol"], qty, fill_px, best["stop"])
    print("   Equity $%.2f · sized to %s shares at about %.2f (risking %.2f%% at the stop, position %.1f%% of account)" % (
        st["equity"], qty, fill_px, nums.get("risk_pct", 0), nums.get("position_pct", 0)))
    print("   Checks: kill switch, daily loss, open positions, position size, cash, stop, loss per trade, sector, correlation")
    print("   Verdict: %s" % ("APPROVED" if ok_ else "REJECTED: " + "; ".join(why)))
    over = risk.check_buy(con, st, best["symbol"], qty * 3, fill_px, best["stop"])
    print("   Same trade at 3x size: %s" % ("APPROVED (unexpected!)" if over[0] else "REJECTED: " + "; ".join(over[1])))
    print("\n6) BACKTEST EVIDENCE (walk-forward; test window never used for fitting)")
    for n in STRATEGIES:
        r = wf.get(n) or {}
        t = r.get("test") or {}
        bm = r.get("bench_test") or {}
        print("   %-46s %-4s test: %3d trades, win %2.0f%%, avg %+.2f%%, PF %.2f, return %+.0f%%, worst %.0f%% | SPY %+.0f%%" % (
            STRATEGIES[n][1], (r.get("verdict") or "").upper(), t.get("trades", 0), t.get("win_rate", 0), t.get("expectancy", 0),
            t.get("profit_factor", 0), t.get("total_return", 0), t.get("max_drawdown", 0), bm.get("total_return", 0)))
    print("\n7) SIMULATED ORDER")
    cid = "demo-%s-%s" % (best["symbol"], best["bar_time"])
    o = br.submit(best["symbol"], "buy", qty, stop=best["stop"], target=best["targets"][0], client_id=cid,
                  strategy=best["strategy"], evidence={"score": best["score"], "card": best["evidence"]})
    print("   %s: buy %s x%s at %s %s" % (o["status"].upper(), o["symbol"], o["qty"], "%.2f" % o["fill_price"] if o["fill_price"] else "-", o["reason"] or ""))
    o2 = br.submit(best["symbol"], "buy", qty, stop=best["stop"], client_id=cid)
    print("   Same order resent (network retry): %s" % ("ignored, no second fill" if o2["id"] == o["id"] else "DUPLICATE FILL"))
    if not demo:
        orders, skipped = autopilot.run(con, br, cards, wf)
        print("   Autopilot: %d orders; skipped %s" % (len(orders), ", ".join("%s (%s)" % s for s in skipped[:5]) or "none"))
    print("\n8) PORTFOLIO")
    st = br.mark()
    print("   Cash $%.2f · equity $%.2f · total return %+.2f%% · drawdown %.2f%%" % (st["cash"], st["equity"], st["total_return_pct"], st["drawdown_pct"]))
    for p in st["positions"]:
        print("   %s x%s @ %.2f · last %.2f · stop %.2f · target %s · P&L $%+.2f" % (
            p["symbol"], p["qty"], p["avg_price"], p["last"], p["stop_price"] or 0, "%.2f" % p["target_price"] if p["target_price"] else "rule", p["pnl"]))
    ex = br.check_exits()
    print("   Exit check: %d positions closed" % len(ex))
    print("\n9) PERFORMANCE INTELLIGENCE")
    g, still = performance.grade_all(con, "live")
    print("   Live predictions: %d graded now, %d open (live calls need days to play out)" % (g, still))
    if not con.execute("SELECT 1 FROM predictions WHERE source='replay' LIMIT 1").fetchone():
        n = performance.replay(con, wf, ok)
        print("   Replay: scanner re-run on every day of the test window: %d historical calls" % n)
    performance.grade_all(con, "replay")
    rep = performance.report(con, "replay", wf)
    o = rep["overall"]
    if o.get("n"):
        print("   Replay graded: %s" % performance._fmt(o))
        for s, d in rep["by_strategy"].items():
            st_ = d["stats"]
            print("     %-16s n=%4d win %2.0f%% avg %+.2f%% | drift check: %s" % (s, st_["n"], st_["win_rate"], st_["avg_ret"], d["drift"]["verdict"]))
        for b in rep["by_score"]:
            if b["n"]:
                print("     score %-6s n=%4d win %2.0f%% avg %+.2f%%" % (b["range"], b["n"], b["win_rate"], b["avg_ret"]))
        print("   " + rep["score_vs_result"]["reading"])
    rp, _ = performance.monthly_report(con, wf)
    print("   Monthly report: %s" % rp)
    print("\n10) DASHBOARD AND BRIEFING")
    st = br.mark()
    new_alerts = alerts.generate(con, cards, ex, st, {c["symbol"]: c for c in ranked})
    print("   Alerts: %d new (deduplicated; rerunning creates none)" % len(new_alerts))
    print("   Alerts on an immediate rerun: %d" % len(alerts.generate(con, cards, ex, st, {c["symbol"]: c for c in ranked})))
    p, _, _ = briefing.write(con, cards)
    d = dashboard.write_static(con)
    print("   Briefing: %s" % p)
    print("   Dashboard snapshot: %s (live view: python -m wallstreet dashboard)" % d)
    print("\n   Records: " + ", ".join("%s %d" % (t, con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0])
                                     for t in ("orders", "predictions", "alerts", "audit", "equity", "events", "filings")))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    setup_logging()
    con = db.connect()
    cmd = argv[0] if argv else "help"
    try:
        if cmd == "daily":
            cmd_daily(con)
        elif cmd == "refresh":
            cmd_refresh(con)
        elif cmd == "intel":
            cmd_intel(con)
        elif cmd == "backtest":
            cmd_backtest(con)
        elif cmd == "scan":
            cards = scan(con, config.UNIVERSE, wf_results(con))
            db.kv_set(con, "last_scan", cards)
            for c in cards:
                print_card(c)
            if not cards:
                print("No setups fired in the last 5 trading days.")
        elif cmd == "grade":
            print("Graded %d, still open %d" % performance.grade_all(con))
        elif cmd == "replay":
            print("Replay predictions added: %d" % performance.replay(con, wf_results(con), config.UNIVERSE))
            print("Graded %d, still open %d" % performance.grade_all(con, "replay"))
        elif cmd == "report":
            y, m = (int(x) for x in argv[1].split("-")) if len(argv) > 1 else (None, None)
            p, text = performance.monthly_report(con, wf_results(con), y, m)
            print(text)
            print("Saved to %s" % p)
        elif cmd == "briefing":
            p, md, _ = briefing.write(con)
            print(md)
            print("Saved to %s" % p)
        elif cmd == "dashboard":
            if "--static" in argv:
                print("Wrote %s" % dashboard.write_static(con))
            else:
                con.close()
                dashboard.serve()
        elif cmd == "catalysts":
            for c in catalysts.rank(con, [s for s in config.UNIVERSE if risk.sector(s) != "Index"])[:15]:
                print("%3d %-6s %s" % (c["score"], c["symbol"], "; ".join(i["text"][:80] for i in c["items"][:2])))
        elif cmd == "status":
            st = PaperBroker(con).mark()
            print(json.dumps(st, indent=2, default=str))
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
