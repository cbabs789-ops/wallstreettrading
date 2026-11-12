"""Opportunity scanner. Ranks the universe 0-100 and writes every pick to the predictions table so it can be graded later.

Score parts (100 total), every one traceable to data:
  Setup (35): a strategy's entry rule fired on the last finished bar.
  Strategy evidence (20): that strategy's OUT-OF-SAMPLE result from walk-forward testing (0 if it failed the test).
  Trend and relative strength (20): price vs 50/200-day averages, 3-month return vs the S&P 500.
  Fundamentals (15): revenue growth, operating margin, free cash flow from SEC filings.
  Catalyst (10): recent material 8-K or real insider buying (open-market P, not 10b5-1 plan).
Penalties: earnings 8-K in the last 3 days (gap risk already played out unknown), RSI over 75, extended far above the 50-day.
"""
import json
import time

from . import db
from .analysis import fundamental, technical
from .data import prices
from .backtest import trades_for
from .strategies import STRATEGIES, features


def _catalysts(con, symbol, days=14):
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    out = []
    for r in con.execute("SELECT form, filed_at, accepted_at, items, url FROM filings WHERE symbol=? AND filed_at>=? ORDER BY accepted_at DESC",
                         (symbol, since)):
        if r["form"] in ("8-K", "SC 13D", "SCHEDULE 13D", "10-Q", "10-K"):
            from .data.sec import describe
            out.append({"kind": "filing", "text": describe(r["form"], r["items"]), "date": r["filed_at"], "url": r["url"],
                        "earnings": "2.02" in (r["items"] or "")})
    for r in con.execute("SELECT owner, role, txn_date, code, kind, value, plan_10b5_1, url FROM insider_txns "
                         "WHERE symbol=? AND txn_date>=? AND code IN ('P','S') AND derivative=0", (symbol, since)):
        out.append({"kind": "insider", "text": "%s (%s): %s of $%s%s" % (r["owner"], r["role"], r["kind"], format(round(r["value"]), ","),
                    ", pre-scheduled 10b5-1 plan" if r["plan_10b5_1"] else ""), "date": r["txn_date"], "url": r["url"],
                    "buy": r["code"] == "P" and not r["plan_10b5_1"]})
    return out


def evaluate(con, symbol, wf_results, spy_bars=None, lookback=5):
    """One symbol's full opportunity card, or None if no strategy setup fired in the last `lookback` trading days.
    A signal from an earlier day is reported with its age and scores lower (the move may already be under way)."""
    b = prices.bars(con, symbol)
    if len(b) < 260:
        return None
    f = features(b)
    last = len(b) - 1
    fired, age = [], None
    for back in range(lookback):
        i = last - back
        for name, (fn, label, _) in STRATEGIES.items():
            wf = wf_results.get(name) or {}
            p = wf.get("params") or STRATEGIES[name][2][0]
            sig = fn(f, i, p)
            if sig and back:
                # an older signal only counts if the trade it started would still be open today
                tr = trades_for(f, name, p, i, i + 1)
                if not tr or tr[0]["exit_i"] < last or tr[0]["why"] != "open at end of data":
                    continue
            if sig:
                fired.append((name, label, sig, wf))
        if fired:
            age = back
            break
    if not fired:
        return None
    snap = technical.snapshot(b, bench=spy_bars)
    q = prices.last_quote(con, symbol)
    fdat, as_of = fundamental.load(con, symbol)
    fm = fundamental.metrics(fdat, price=snap["close"])
    cats = _catalysts(con, symbol)
    # pick the fired strategy with the best out-of-sample evidence
    fired.sort(key=lambda x: -((x[3].get("test") or {}).get("expectancy", -99) if x[3].get("verdict") == "pass" else -99))
    name, label, sig, wf = fired[0]
    test = wf.get("test") or {}
    a = snap["atr"]
    entry_lo, entry_hi = snap["close"] - 0.25 * a, snap["close"] + 0.25 * a
    stop = snap["close"] - sig["stop_atr"] * a
    if sig.get("target_atr"):
        targets = [snap["close"] + sig["target_atr"] * a]
    else:                                               # this strategy exits on a close above the 5-day average
        targets = [max(f["sma5"][last], snap["close"] * 1.005)]
    rr = (targets[0] - snap["close"]) / (snap["close"] - stop) if snap["close"] > stop else None
    score, parts = 0.0, {}
    parts["setup"] = 35
    parts["evidence"] = 0 if wf.get("verdict") != "pass" else min(20, max(0, test.get("expectancy", 0) * 8 + test.get("profit_factor", 1) * 3))
    tr = (8 if snap["trend"] == "up" else 3 if snap["trend"] == "mixed" else 0) + (4 if snap["sma50_rising"] else 0)
    rs = snap["rel_strength_3m"]
    tr += 8 if rs is not None and rs > 0.05 else 4 if rs is not None and rs > 0 else 0
    parts["trend"] = tr
    fu = 0
    if fm:
        fu += 5 if (fm["revenue_growth_pct"] or 0) > 10 else 3 if (fm["revenue_growth_pct"] or 0) > 0 else 0
        fu += 5 if (fm["op_margin_pct"] or 0) > 20 else 3 if (fm["op_margin_pct"] or 0) > 10 else 0
        fu += 5 if (fm["free_cash_flow"] or 0) > 0 else 0
    parts["fundamentals"] = fu
    parts["catalyst"] = min(10, 6 * any(c.get("buy") for c in cats) + 4 * any(c["kind"] == "filing" and not c.get("earnings") for c in cats))
    pen, risks = 3 * age, []
    if age:
        risks.append("The signal fired %d trading day%s ago; price has moved since" % (age, "s" if age > 1 else ""))
    if any(c.get("earnings") and c["date"] >= time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 86400)) for c in cats):
        pen += 10
        risks.append("Earnings were just reported: the reaction may not be finished")
    if snap["rsi"] > 75:
        pen += 5
        risks.append("RSI %.0f: stretched, prone to a pullback" % snap["rsi"])
    if snap["sma50"] and snap["close"] > snap["sma50"] * 1.15:
        pen += 5
        risks.append("%.0f%% above its 50-day average: extended" % ((snap["close"] / snap["sma50"] - 1) * 100))
    score = max(0, min(100, sum(parts.values()) - pen))
    if wf.get("verdict") != "pass":
        risks.insert(0, "This setup did NOT pass out-of-sample testing; treat as unproven")
    risks.append("Stop at %.2f can gap: an overnight drop fills below it" % stop)
    if snap["trend"] != "up":
        risks.append("Not in a clean uptrend (trend is %s)" % snap["trend"])
    bull = "%s works as it did in testing: price reaches %.2f%s." % (label, targets[0],
            " (tested win rate %.0f%%, average result %+.2f%% per trade)" % (test.get("win_rate", 0), test.get("expectancy", 0)) if test else "")
    bear = "The setup fails and price hits the stop at %.2f (%.1f%% below), or keeps falling with the market." % (stop, (1 - stop / snap["close"]) * 100)
    card = {"symbol": symbol, "score": round(score), "score_parts": parts, "penalty": pen,
            "price": q["price"] if q else snap["close"], "quote_time": q["quote_time"] if q else snap["ts"],
            "quote_source": q["source"] if q else "last daily close", "bar_time": snap["ts"],
            "volume": snap["volume"], "rel_volume": snap["rel_volume"], "strategy": name, "strategy_label": label, "signal_age_days": age,
            "technical": technical.describe(snap), "fundamental": fundamental.describe(fm), "fundamentals_as_of": as_of,
            "news": cats[:6], "bull": bull, "bear": bear, "entry_zone": [round(entry_lo, 2), round(entry_hi, 2)],
            "stop": round(stop, 2), "targets": [round(t, 2) for t in targets], "risk_reward": round(rr, 2) if rr else None,
            "confidence": _confidence(wf), "fail_reasons": risks, "evidence": {"walk_forward_test": test, "params": wf.get("params")}}
    return card


def _confidence(wf):
    t = wf.get("test") or {}
    if wf.get("verdict") != "pass":
        return "low: no out-of-sample proof for this setup"
    if t.get("trades", 0) < 50:
        return "medium-low: passed, but on only %d test trades" % t.get("trades", 0)
    return "medium: passed out-of-sample on %d trades (profit factor %.2f)" % (t["trades"], t.get("profit_factor", 0))


def scan(con, symbols, wf_results, record=True):
    spy = prices.bars(con, "SPY")
    cards = [c for c in (evaluate(con, s, wf_results, spy) for s in symbols if s != "SPY") if c]
    cards.sort(key=lambda c: -c["score"])
    if record:
        now = db.now_ms()
        for c in cards:
            con.execute("INSERT INTO predictions(created_at, symbol, strategy, score, direction, entry, stop, target, horizon_days, "
                        "data_as_of, evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (now, c["symbol"], c["strategy"], c["score"], "long", c["price"], c["stop"], c["targets"][0], 20,
                         c["bar_time"] * 1000, json.dumps(c["evidence"], default=str)))
    return cards
