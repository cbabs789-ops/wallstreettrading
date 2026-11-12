"""Market regime from SPY (and the VIX when known), at any point in time. Facts only, no forecasts."""
from . import indicators as ind


def at(spy_bars, ts=None, vix=None):
    """spy_bars: [(ts, o, h, l, c, v)]. Uses only bars with time <= ts (default: all). Returns a dict or None."""
    c = [b[4] for b in spy_bars if ts is None or b[0] <= ts]
    if len(c) < 200:
        return None
    s50, s200 = ind.sma(c, 50)[-1], ind.sma(c, 200)[-1]
    trend = "uptrend" if c[-1] > s50 > s200 else "downtrend" if c[-1] < s50 < s200 else "mixed"
    vol = "calm" if vix and vix < 15 else "normal" if vix and vix < 22 else "stressed" if vix else "unknown"
    return {"spy": c[-1], "sma50": s50, "sma200": s200, "trend": trend, "vix": vix, "volatility": vol}
