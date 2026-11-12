"""Strategies, written down before any testing. Each is a well-known, published idea, not something fitted to our data.

A strategy sees precomputed features up to index i (the bar that just closed) and answers: enter tomorrow at the open?
It also defines its stop, target and maximum hold. Parameters live in GRID; walk-forward testing picks one setting on the
training years only, then checks it on years it has never seen.
"""
from .analysis import indicators as ind


def features(bars):
    o, h, l, c, v = ([b[k] for b in bars] for k in range(1, 6))
    return {"ts": [b[0] for b in bars], "open": o, "high": h, "low": l, "close": c, "volume": v,
            "sma5": ind.sma(c, 5), "sma20": ind.sma(c, 20), "sma50": ind.sma(c, 50), "sma200": ind.sma(c, 200),
            "rsi14": ind.rsi(c, 14), "rsi2": ind.rsi(c, 2), "atr": ind.atr(h, l, c), "relvol": ind.rel_volume(v),
            "hi20": [max(h[i - 20:i]) if i >= 20 else None for i in range(len(c))]}


def _up(f, i):
    s50, s200 = f["sma50"][i], f["sma200"][i]
    return s50 is not None and s200 is not None and f["close"][i] > s50 > s200 and s50 > (f["sma50"][i - 10] or s50)


def trend_pullback(f, i, p):
    """Buy a dip in an established uptrend: price above rising 50-day, 50-day above 200-day, RSI(14) under p['rsi']."""
    if not _up(f, i) or f["rsi14"][i] is None or f["rsi14"][i] >= p["rsi"]:
        return None
    return {"stop_atr": p["stop_atr"], "target_atr": p["target_atr"], "max_hold": 20}


def breakout(f, i, p):
    """Close above the prior 20-day high on at least p['relvol'] x normal volume, with the 50-day above the 200-day."""
    hi, rv = f["hi20"][i], f["relvol"][i]
    if hi is None or rv is None or f["sma200"][i] is None or f["sma50"][i] <= f["sma200"][i]:
        return None
    if f["close"][i] > hi and rv >= p["relvol"]:
        return {"stop_atr": p["stop_atr"], "target_atr": p["target_atr"], "max_hold": 30}
    return None


def rsi2_reversion(f, i, p):
    """Connors-style: above the 200-day, RSI(2) under p['rsi2']; exit on a close above the 5-day average or after 10 days."""
    if f["sma200"][i] is None or f["close"][i] <= f["sma200"][i] or f["rsi2"][i] is None or f["rsi2"][i] >= p["rsi2"]:
        return None
    return {"stop_atr": p["stop_atr"], "target_atr": None, "max_hold": 10, "exit_above_sma5": True}


STRATEGIES = {
    "trend_pullback": (trend_pullback, "Dip buy in an uptrend",
                       [{"rsi": r, "stop_atr": s, "target_atr": t} for r in (35, 40, 45) for s in (1.5, 2.0) for t in (2.0, 3.0)]),
    "breakout": (breakout, "20-day breakout on volume",
                 [{"relvol": r, "stop_atr": s, "target_atr": t} for r in (1.3, 1.8) for s in (1.5, 2.5) for t in (3.0, 5.0)]),
    "rsi2_reversion": (rsi2_reversion, "Short-term oversold bounce above the 200-day",
                       [{"rsi2": r, "stop_atr": s} for r in (5, 10, 15) for s in (2.0, 3.0)]),
}

# Exit rules the paper broker enforces on open positions (must match each strategy's signal dict above).
EXIT_RULES = {"trend_pullback": {"max_hold": 20}, "breakout": {"max_hold": 30},
              "rsi2_reversion": {"max_hold": 10, "exit_above_sma5": True}}
