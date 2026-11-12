"""Opportunity scanner. Ranks the universe 0-100 and writes every pick to the predictions table so it can be graded later.

Score parts (100 total), every one traceable to data:
  Setup (35): a strategy's entry rule fired on a recent finished bar.
  Strategy evidence (20): that strategy's OUT-OF-SAMPLE walk-forward result (0 if it failed the test).
  Trend and relative strength (20): price vs 50/200-day averages, 3-month return vs the S&P 500.
  Fundamentals (15): revenue growth, operating margin, free cash flow from SEC filings.
  Catalyst (10): the catalyst score (see catalysts.py) scaled to 10.
Penalties: signal age (3 per day), earnings just reported (10) or due within 3 trading days (10), RSI over 75 (5),
more than 15% above the 50-day average (5).

The same code runs live and in replay. Replay (performance.replay) evaluates past dates using only data available on
that date: the evidence part uses the VALIDATION window (which precedes the test window being replayed), and the
fundamentals and catalyst parts are left out because point-in-time history for them is not stored.
"""
import json

from . import catalysts, db
from .analysis import fundamental, regime, technical
from .backtest import trades_for
from .data import prices
from .strategies import STRATEGIES, features

HORIZON_DAYS = 20


def find_signals(f, last, wf_results, lookback=5):
    """Strategies whose entry fired within `lookback` bars before (and including) index `last`.
    An older signal only counts if the trade it started would still be open at `last` (knowable at `last`)."""
    for back in range(lookback):
        i = last - back
        fired = []
        for name, (fn, label, grid) in STRATEGIES.items():
            wf = wf_results.get(name) or {}
            p = wf.get("params") or grid[0]
            if not f["atr"][i]:
                continue
            sig = fn(f, i, p)
            if sig and back:
                tr = trades_for({k: v[:last + 1] for k, v in f.items()}, name, p, i, i + 1)
                if not tr or tr[0]["why"] != "open at end of data":
                    continue
            if sig:
                fired.append((name, label, sig, wf, p))
        if fired:
            return fired, back, i
    return [], None, None


def build_card(symbol, b, f, last, fired, age, sig_i, spy_bars, cat=None, fm=None, fm_as_of=None, quote=None, replay=False):
    window = "validation" if replay else "test"
    fired = sorted(fired, key=lambda x: -((x[3].get(window) or {}).get("expectancy", -99) if x[3].get("verdict") == "pass" else -99))
    name, label, sig, wf, params = fired[0]
    ev = wf.get(window) or {}
    snap = technical.snapshot(b, i=last, bench=spy_bars)
    a = snap["atr"]
    px = snap["close"]
    entry_lo, entry_hi = px - 0.25 * a, px + 0.25 * a
    stop = px - sig["stop_atr"] * a
    targets = [px + sig["target_atr"] * a] if sig.get("target_atr") else [max(f["sma5"][last], px * 1.005)]
    rr = (targets[0] - px) / (px - stop) if px > stop else None
    parts = {"setup": 35}
    parts["evidence"] = 0 if wf.get("verdict") != "pass" else round(min(20, max(0, ev.get("expectancy", 0) * 8 + ev.get("profit_factor", 1) * 3)), 1)
    tr = (8 if snap["trend"] == "up" else 3 if snap["trend"] == "mixed" else 0) + (4 if snap["sma50_rising"] else 0)
    rs = snap["rel_strength_3m"]
    tr += 8 if rs is not None and rs > 0.05 else 4 if rs is not None and rs > 0 else 0
    parts["trend"] = tr
    fu = None
    if not replay:
        fu = 0
        if fm:
            fu += 5 if (fm["revenue_growth_pct"] or 0) > 10 else 3 if (fm["revenue_growth_pct"] or 0) > 0 else 0
            fu += 5 if (fm["op_margin_pct"] or 0) > 20 else 3 if (fm["op_margin_pct"] or 0) > 10 else 0
            fu += 5 if (fm["free_cash_flow"] or 0) > 0 else 0
    parts["fundamentals"] = fu
    parts["catalyst"] = None if replay else round((cat or {}).get("score", 0) / 10, 1)
    pen, risks = 3 * age, []
    if age:
        risks.append("The signal fired %d trading day%s ago; price has moved since" % (age, "s" if age > 1 else ""))
    if cat and cat.get("earnings_recent"):
        pen += 10
        risks.append("Earnings were just reported: the reaction may not be finished")
    if cat and cat.get("earnings_soon"):
        pen += 10
        risks.append("Earnings due %s: the stock can gap through the stop" % cat["earnings_next"]["date"])
    if snap["rsi"] > 75:
        pen += 5
        risks.append("RSI %.0f: stretched, prone to a pullback" % snap["rsi"])
    if snap["sma50"] and px > snap["sma50"] * 1.15:
        pen += 5
        risks.append("%.0f%% above its 50-day average: extended" % ((px / snap["sma50"] - 1) * 100))
    score = max(0, min(100, sum(v for v in parts.values() if v) - pen))
    if wf.get("verdict") != "pass":
        risks.insert(0, "This setup did NOT pass out-of-sample testing; treat as unproven")
    risks.append("Stop at %.2f can gap: an overnight drop fills below it" % stop)
    if snap["trend"] != "up":
        risks.append("Not in a clean uptrend (trend is %s)" % snap["trend"])
    bull = "%s works as it did in testing: price reaches %.2f%s." % (label, targets[0],
            " (tested win rate %.0f%%, average result %+.2f%% per trade)" % (ev.get("win_rate", 0), ev.get("expectancy", 0)) if ev else "")
    bear = "The setup fails and price hits the stop at %.2f (%.1f%% below), or keeps falling with the market." % (stop, (1 - stop / px) * 100)
    rg = regime.at(spy_bars, snap["ts"]) if spy_bars else None
    return {"symbol": symbol, "score": round(score), "score_parts": parts, "penalty": pen,
            "price": quote["price"] if quote else px, "quote_time": quote["quote_time"] if quote else snap["ts"],
            "quote_source": quote["source"] if quote else "daily close", "bar_time": snap["ts"], "signal_time": f["ts"][sig_i],
            "volume": snap["volume"], "rel_volume": snap["rel_volume"], "strategy": name, "strategy_label": label, "signal_age_days": age,
            "params": params, "max_hold": sig["max_hold"], "exit_rule": "sma5" if sig.get("exit_above_sma5") else "target",
            "technical": technical.describe(snap), "fundamental": fundamental.describe(fm) if fm else [], "fundamentals_as_of": fm_as_of,
            "news": (cat or {}).get("items", [])[:6], "catalyst": cat, "bull": bull, "bear": bear,
            "entry_zone": [round(entry_lo, 2), round(entry_hi, 2)], "stop": round(stop, 2), "targets": [round(t, 2) for t in targets],
            "risk_reward": round(rr, 2) if rr else None, "confidence": _confidence(wf, window), "fail_reasons": risks,
            "regime": rg["trend"] if rg else None,
            "evidence": {"walk_forward_" + window: ev, "params": params, "replay": replay}}


def evaluate(con, symbol, wf_results, spy_bars=None, lookback=5):
    """One symbol's full opportunity card, or None if no strategy setup fired in the last `lookback` trading days."""
    b = prices.bars(con, symbol)
    if len(b) < 260:
        return None
    f = features(b)
    last = len(b) - 1
    fired, age, sig_i = find_signals(f, last, wf_results, lookback)
    if not fired:
        return None
    fdat, as_of = fundamental.load(con, symbol)
    fm = fundamental.metrics(fdat, price=b[-1][4])
    return build_card(symbol, b, f, last, fired, age, sig_i, spy_bars, cat=catalysts.for_symbol(con, symbol),
                      fm=fm, fm_as_of=as_of, quote=prices.last_quote(con, symbol))


def _confidence(wf, window="test"):
    t = wf.get(window) or {}
    if wf.get("verdict") != "pass":
        return "low: no out-of-sample proof for this setup"
    if t.get("trades", 0) < 50:
        return "medium-low: passed, but on only %d %s trades" % (t.get("trades", 0), window)
    return "medium: passed out-of-sample on %d %s trades (profit factor %.2f)" % (t["trades"], window, t.get("profit_factor", 0))


def record(con, c, source="live", created_at=None):
    """Write a card to predictions. Returns True if new (one row per setup, however many times it is scanned)."""
    cur = con.execute(
        "INSERT OR IGNORE INTO predictions(created_at, symbol, strategy, score, direction, entry, stop, target, horizon_days, "
        "data_as_of, evidence, signal_ts, source, regime, score_parts, max_hold, exit_rule) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (created_at or db.now_ms(), c["symbol"], c["strategy"], c["score"], "long", c["price"], c["stop"], c["targets"][0],
         HORIZON_DAYS, c["bar_time"] * 1000, json.dumps(c["evidence"], default=str), c["signal_time"], source, c["regime"],
         json.dumps(c["score_parts"]), c["max_hold"], c["exit_rule"]))
    return cur.rowcount == 1


def scan(con, symbols, wf_results, record_predictions=True):
    spy = prices.bars(con, "SPY")
    cards = [c for c in (evaluate(con, s, wf_results, spy) for s in symbols if s != "SPY") if c]
    cards.sort(key=lambda c: -c["score"])
    if record_predictions:
        for c in cards:
            c["new"] = record(con, c)
    return cards

