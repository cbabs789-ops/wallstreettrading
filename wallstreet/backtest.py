"""Strategy lab: honest backtests.

Rules of the replay (each one closes a common way backtests lie):
  - Signals are read on a finished daily bar; the trade happens at the NEXT bar's open.
  - Inside a bar, the stop is checked before the target, so a bar that touched both counts as a loss.
  - A gap through the stop fills at the open, not at the stop price.
  - Every fill pays slippage (config.SLIPPAGE_BPS) and commission.
  - Portfolio rules match the live risk limits: fixed fraction per position, a cap on open positions, no leverage.
  - Walk-forward: parameters are chosen on the TRAIN years only, checked on VALIDATION, and reported on TEST, which
    nothing was ever fitted to. Results are always shown next to simply buying and holding SPY over the same dates.
"""
import math

from . import config
from .strategies import STRATEGIES, features

DAY = 86400


def simulate_trade(f, e, entry, stop, target, max_hold, exit_above_sma5=False):
    """Walk one long trade forward from bar e (bought at `entry` on e's open, slippage already in `entry`).
    Shared by the backtest and the prediction grader so both use identical rules.
    Returns (exit_index, exit_price_before_slippage, reason) or (None, None, None) if the data ends first."""
    o, h, l, c = f["open"], f["high"], f["low"], f["close"]
    n = len(c)
    for k in range(e, min(n, e + max_hold + 1)):
        if k > e and o[k] <= stop:
            return k, o[k], "stop (gap)"
        if l[k] <= stop:
            return k, min(stop, o[k]) if k == e else stop, "stop"        # entry bar opening under the stop fills at the open
        if target and k > e and o[k] >= target:
            return k, o[k], "target (gap)"
        if target and h[k] >= target:
            return k, max(target, o[k]) if k == e else target, "target"  # entry bar opening above the target fills at the open
        if exit_above_sma5 and f["sma5"][k] and c[k] > f["sma5"][k]:
            return k, c[k], "closed above 5-day average"
        if k - e >= max_hold:
            return k, c[k], "time limit"
    return None, None, None


def trades_for(f, strat, params, i_from, i_to):
    """All trades one symbol would take between bar indexes [i_from, i_to). Exits may run past i_to (to the data end)."""
    fn = STRATEGIES[strat][0]
    slip = config.SLIPPAGE_BPS / 10000
    o, h, l, c, a = f["open"], f["high"], f["low"], f["close"], f["atr"]
    out, i = [], max(i_from, 200)
    n = len(c)
    while i < min(i_to, n - 1):
        sig = fn(f, i, params) if a[i] else None
        if not sig:
            i += 1
            continue
        e = i + 1
        entry = o[e] * (1 + slip)
        stop = entry - sig["stop_atr"] * a[i]
        target = entry + sig["target_atr"] * a[i] if sig.get("target_atr") else None
        j, px, why = simulate_trade(f, e, entry, stop, target, sig["max_hold"], sig.get("exit_above_sma5"))
        if j is None:
            j, px, why = n - 1, c[n - 1], "open at end of data"
        exit_px = px * (1 - slip)
        cost = 2 * config.COMMISSION_PER_ORDER
        out.append({"entry_i": e, "exit_i": j, "t_in": f["ts"][e], "t_out": f["ts"][j], "entry": entry, "exit": exit_px,
                    "ret": exit_px / entry - 1, "why": why, "cost": cost,
                    "mfe": max(h[e:j + 1]) / entry - 1, "mae": min(l[e:j + 1]) / entry - 1})
        i = j + 1                                        # one position per symbol at a time
    return out


def portfolio(feats, strat, params, t_from, t_to, start=10000.0, frac=None, max_open=None):
    """Replay across many symbols with the live position limits. feats: {symbol: features}. Times in seconds."""
    frac = (frac or config.RISK["max_position_pct"]) / 100
    max_open = max_open or config.RISK["max_open_positions"]
    cands = []
    for sym, f in feats.items():
        i0 = next((k for k, t in enumerate(f["ts"]) if t >= t_from), len(f["ts"]))
        i1 = next((k for k, t in enumerate(f["ts"]) if t >= t_to), len(f["ts"]))
        for t in trades_for(f, strat, params, max(i0 - 1, 0), max(i1 - 1, 0)):
            if t_from <= t["t_in"] < t_to:
                cands.append(t | {"sym": sym})
    cands.sort(key=lambda t: (t["t_in"], t["sym"]))
    cash, open_, taken = start, [], []
    closes = {s: dict(zip(f["ts"], f["close"])) for s, f in feats.items()}
    days = sorted({t for f in feats.values() for t in f["ts"] if t_from <= t < t_to})
    curve, ci = [], 0
    for d in days:
        # close trades that ended on or before today
        for t in [t for t in open_ if t["t_out"] <= d]:
            cash += t["shares"] * t["exit"] - t["cost"]
            open_.remove(t)
        while ci < len(cands) and cands[ci]["t_in"] <= d:
            t = cands[ci]
            ci += 1
            if t["t_in"] != d or len(open_) >= max_open or any(o["sym"] == t["sym"] for o in open_):
                continue
            eq = cash + sum(o["shares"] * closes[o["sym"]].get(d, o["entry"]) for o in open_)
            alloc = min(eq * frac, cash)
            if alloc < 1:
                continue
            t = t | {"shares": alloc / t["entry"]}
            cash -= alloc
            open_.append(t)
            taken.append(t)
        eq = cash + sum(o["shares"] * _last_close(closes[o["sym"]], d, o["entry"]) for o in open_)
        curve.append((d, eq))
    end = days[-1] if days else t_to
    for t in open_:                                      # mark what's still open at the window end
        cash += t["shares"] * _last_close(closes[t["sym"]], end, t["entry"])
    return metrics(taken, curve, start)


def _last_close(series, d, default):
    v = series.get(d)
    return v if v is not None else default


def metrics(trades, curve, start):
    rets = [t["ret"] for t in trades]
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    eqs = [e for _, e in curve] or [start]
    peak, dd = eqs[0], 0.0
    for e in eqs:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    daily = [eqs[k] / eqs[k - 1] - 1 for k in range(1, len(eqs)) if eqs[k - 1] > 0]
    mu = sum(daily) / len(daily) if daily else 0
    sd = (sum((x - mu) ** 2 for x in daily) / (len(daily) - 1)) ** 0.5 if len(daily) > 1 else 0
    years = (curve[-1][0] - curve[0][0]) / (365.25 * DAY) if len(curve) > 1 else 0
    total = eqs[-1] / start - 1
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {"trades": len(rets), "win_rate": len(wins) / len(rets) * 100 if rets else 0,
            "avg_win": sum(wins) / len(wins) * 100 if wins else 0, "avg_loss": sum(losses) / len(losses) * 100 if losses else 0,
            "expectancy": sum(rets) / len(rets) * 100 if rets else 0,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0),
            "total_return": total * 100, "cagr": ((1 + total) ** (1 / years) - 1) * 100 if years > 0.25 and total > -1 else None,
            "max_drawdown": dd * 100, "sharpe": mu / sd * math.sqrt(252) if sd else 0,
            "avg_mfe": sum(t["mfe"] for t in trades) / len(trades) * 100 if trades else 0,
            "avg_mae": sum(t["mae"] for t in trades) / len(trades) * 100 if trades else 0,
            "exits": _count(t["why"] for t in trades)}


def _count(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def buy_hold(f, t_from, t_to):
    c = [(t, x) for t, x in zip(f["ts"], f["close"]) if t_from <= t < t_to]
    if len(c) < 2:
        return None
    peak, dd = c[0][1], 0.0
    for _, x in c:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    return {"total_return": (c[-1][1] / c[0][1] - 1) * 100, "max_drawdown": dd * 100}


def walk_forward(bars_by_symbol, strat, bench="SPY", split=(0.5, 0.25, 0.25)):
    """Pick parameters on TRAIN by Sharpe (min 20 trades), confirm on VALIDATION, report TEST. Returns a dict."""
    feats = {s: features(b) for s, b in bars_by_symbol.items() if len(b) > 260}
    if not feats:
        return {"error": "not enough history (needs 260+ daily bars per symbol)"}
    t0 = min(f["ts"][200] for f in feats.values())       # first bar with a 200-day average
    t3 = max(f["ts"][-1] for f in feats.values()) + 1
    t1 = t0 + int((t3 - t0) * split[0])
    t2 = t1 + int((t3 - t0) * split[1])
    grid = STRATEGIES[strat][2]
    scored = []
    for p in grid:
        m = portfolio(feats, strat, p, t0, t1)
        scored.append((m["sharpe"] if m["trades"] >= 20 else -99, p, m))
    scored.sort(key=lambda x: -x[0])
    best_p, train = scored[0][1], scored[0][2]
    val = portfolio(feats, strat, best_p, t1, t2)
    test = portfolio(feats, strat, best_p, t2, t3)
    bf = feats.get(bench)
    # On random-walk prices the looser rule (just "positive in all three windows") passed about 1 run in 6 for the
    # reversion strategy, so a pass also needs a real margin: profit factor 1.2+ on validation and 1.3+ on test.
    verdict = "pass" if (train["expectancy"] > 0 and val["expectancy"] > 0 and test["expectancy"] > 0
                         and val["profit_factor"] >= 1.2 and test["profit_factor"] >= 1.3 and test["trades"] >= 30) else "fail"
    return {"strategy": strat, "name": STRATEGIES[strat][1], "params": best_p, "windows": [t0, t1, t2, t3],
            "train": train, "validation": val, "test": test, "grid_size": len(grid),
            "bench_train": buy_hold(bf, t0, t1) if bf else None, "bench_val": buy_hold(bf, t1, t2) if bf else None,
            "bench_test": buy_hold(bf, t2, t3) if bf else None, "verdict": verdict,
            "symbols": sorted(feats)}
