"""Technical indicators as plain functions over lists. Each returns a list the same length as its input, with None
where there isn't enough history yet. Value at index i uses only data up to and including i (no look-ahead)."""


def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    out, k, prev = [None] * len(x), 2 / (n + 1), None
    for i, v in enumerate(x):
        if i == n - 1:
            prev = sum(x[:n]) / n                       # seed with the simple average, the standard convention
        elif i >= n:
            prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(close, n=14):
    """Wilder's RSI."""
    out = [None] * len(close)
    if len(close) <= n:
        return out
    gains = [max(close[i] - close[i - 1], 0) for i in range(1, n + 1)]
    losses = [max(close[i - 1] - close[i], 0) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(close)):
        d = close[i] - close[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(close, fast=12, slow=26, signal=9):
    ef, es = ema(close, fast), ema(close, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig = [None] * len(close)
    if first is not None and len(close) - first >= signal:
        s = ema(line[first:], signal)
        sig[first:] = s
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(close, n=20, k=2.0):
    mid = sma(close, n)
    up, lo = [None] * len(close), [None] * len(close)
    for i in range(n - 1, len(close)):
        w = close[i - n + 1:i + 1]
        m = mid[i]
        sd = (sum((v - m) ** 2 for v in w) / n) ** 0.5
        up[i], lo[i] = m + k * sd, m - k * sd
    return mid, up, lo


def atr(high, low, close, n=14):
    tr = [high[0] - low[0]] + [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(1, len(close))]
    out = [None] * len(close)
    if len(close) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(close)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def vwap(high, low, close, volume, session_starts):
    """Intraday VWAP that resets at each session start index (session_starts: set of indexes)."""
    out, pv, vv = [None] * len(close), 0.0, 0.0
    for i in range(len(close)):
        if i in session_starts:
            pv, vv = 0.0, 0.0
        tp = (high[i] + low[i] + close[i]) / 3
        pv += tp * volume[i]
        vv += volume[i]
        out[i] = pv / vv if vv else None
    return out


def rel_volume(volume, n=20):
    """Today's volume / average of the previous n days (excludes today, so a spike isn't diluted by itself)."""
    out = [None] * len(volume)
    for i in range(n, len(volume)):
        avg = sum(volume[i - n:i]) / n
        out[i] = volume[i] / avg if avg else None
    return out


def pivots(high, low, left=5, right=5, upto=None):
    """Swing highs and lows confirmed by `right` bars after them. Only pivots confirmed by index `upto` are returned."""
    upto = len(high) - 1 if upto is None else upto
    hs, ls = [], []
    for i in range(left, upto - right + 1):
        if high[i] == max(high[i - left:i + right + 1]):
            hs.append((i, high[i]))
        if low[i] == min(low[i - left:i + right + 1]):
            ls.append((i, low[i]))
    return hs, ls


def returns(close):
    return [close[i] / close[i - 1] - 1 for i in range(1, len(close))]


def correlation(a, b):
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return cov / (va * vb) if va and vb else None
