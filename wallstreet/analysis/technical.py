"""The technical read of one stock at one point in time. Uses only bars up to index i, so it can be replayed in backtests."""
from . import indicators as ind


def snapshot(bars, i=None, bench=None):
    """bars: [(ts, o, h, l, c, v)] daily. bench: the same for SPY (for relative strength). Returns a dict of facts."""
    i = len(bars) - 1 if i is None else i
    if i < 60:
        return None
    b = bars[:i + 1]
    o, h, l, c, v = ([x[k] for x in b] for k in range(1, 6))
    close = c[-1]
    s9, s20, s50, s100, s200 = (ind.sma(c, n)[-1] for n in (9, 20, 50, 100, 200))
    s50_prev = ind.sma(c, 50)[-11] if len(c) > 60 else None
    r = ind.rsi(c)[-1]
    ml, ms, mh = ind.macd(c)
    mid, up, lo = ind.bollinger(c)
    a = ind.atr(h, l, c)[-1]
    rv = ind.rel_volume(v)[-1]
    hi20 = max(h[-21:-1])
    lo10 = min(l[-11:-1])
    hi52 = max(h[-252:])
    piv_h, piv_l = ind.pivots(h, l, upto=len(h) - 1)
    res = min((p for _, p in piv_h if p > close), default=None)
    sup = max((p for _, p in piv_l if p < close), default=None)
    ret63 = close / c[-64] - 1 if len(c) > 64 else None
    rs = None
    if bench:
        bc = [x[4] for x in bench if x[0] <= b[-1][0]]
        if len(bc) > 64 and ret63 is not None:
            rs = ret63 - (bc[-1] / bc[-64] - 1)
    trend = "up" if s50 and s200 and close > s50 > s200 else "down" if s50 and s200 and close < s50 < s200 else "mixed"
    snap = {
        "ts": b[-1][0], "close": close, "sma9": s9, "sma20": s20, "sma50": s50, "sma100": s100, "sma200": s200,
        "sma50_rising": bool(s50 and s50_prev and s50 > s50_prev), "trend": trend, "rsi": r,
        "macd": ml[-1], "macd_signal": ms[-1], "macd_hist": mh[-1],
        "macd_cross_up": bool(mh[-1] is not None and mh[-2] is not None and mh[-1] > 0 >= mh[-2]),
        "bb_upper": up[-1], "bb_lower": lo[-1], "atr": a, "atr_pct": a / close * 100 if a else None,
        "rel_volume": rv, "high20": hi20, "low10": lo10, "high52": hi52, "off_high52_pct": (close / hi52 - 1) * 100,
        "breakout20": close > hi20, "breakdown10": close < lo10, "resistance": res, "support": sup,
        "ret_3m": ret63, "rel_strength_3m": rs, "volume": v[-1], "avg_volume20": sum(v[-21:-1]) / 20,
    }
    return snap


def describe(s):
    """Plain-English lines for a snapshot. Facts only, no forecasts."""
    if not s:
        return ["Not enough history (needs 60+ daily bars)."]
    L = ["Trend: %s (price %.2f, 50-day %.2f, 200-day %s)" % (s["trend"], s["close"], s["sma50"], "%.2f" % s["sma200"] if s["sma200"] else "n/a")]
    L.append("RSI %.0f%s" % (s["rsi"], " (overbought)" if s["rsi"] > 70 else " (oversold)" if s["rsi"] < 30 else ""))
    L.append("MACD %s%s" % ("above" if (s["macd_hist"] or 0) > 0 else "below", " signal, just crossed up" if s["macd_cross_up"] else " signal"))
    if s["rel_volume"]:
        L.append("Volume %.1fx its 20-day average" % s["rel_volume"])
    if s["breakout20"]:
        L.append("Closed above its 20-day high (%.2f)" % s["high20"])
    if s["breakdown10"]:
        L.append("Closed below its 10-day low (%.2f)" % s["low10"])
    L.append("%.1f%% from its 52-week high" % s["off_high52_pct"])
    if s["support"] or s["resistance"]:
        L.append("Nearest support %s, resistance %s" % ("%.2f" % s["support"] if s["support"] else "none found",
                                                         "%.2f" % s["resistance"] if s["resistance"] else "none (at highs)"))
    if s["rel_strength_3m"] is not None:
        L.append("3-month return %+.1f%%, %+.1f points vs the S&P 500" % (s["ret_3m"] * 100, s["rel_strength_3m"] * 100))
    return L
