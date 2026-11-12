"""Prices from Yahoo Finance's public chart endpoint.

Honest labels: this is an unofficial endpoint (no contract, can change or rate-limit without notice). Quotes are
delayed during market hours (about 15 minutes for US stocks on free feeds) and every bar and quote is stored with the
exchange timestamp it describes plus the time we fetched it. If Schwab market data is connected later, it replaces this
source behind the same functions.
"""
import logging
import time
from urllib.parse import quote as q

from .. import db
from .http import DataError, get

log = logging.getLogger("pws.prices")
SOURCE = "yahoo-chart (unofficial, delayed)"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false"
DAY = 86400


def fetch_chart(symbol, rng="2y", interval="1d"):
    """Returns {"meta": {...}, "bars": [(ts, o, h, l, c, v), ...]} with incomplete rows dropped."""
    data = get(CHART % (q(symbol), rng, interval))
    res = ((data or {}).get("chart") or {}).get("result") or []
    if not res:
        err = ((data or {}).get("chart") or {}).get("error")
        raise DataError("no chart data for %s: %s" % (symbol, err))
    r = res[0]
    ts = r.get("timestamp") or []
    qt = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        row = [qt.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close", "volume")]
        if None in row[:4] or row[3] <= 0:
            continue                                    # holidays, halts and half-built candles come back as nulls
        bars.append((int(t), float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4] or 0)))
    m = r.get("meta") or {}
    meta = {"symbol": m.get("symbol", symbol), "price": m.get("regularMarketPrice"), "quote_time": m.get("regularMarketTime"),
            "currency": m.get("currency"), "tz": m.get("exchangeTimezoneName"), "prev_close": m.get("chartPreviousClose"),
            "session": (m.get("currentTradingPeriod") or {}).get("regular") or {}}
    return {"meta": meta, "bars": bars}


def final_daily(bars, meta, now=None):
    """Drop today's daily bar while the session is still open: it isn't a finished candle and would leak into signals."""
    if not bars:
        return bars
    now = now or time.time()
    sess = meta.get("session") or {}
    start, end = sess.get("start"), sess.get("end")
    if start and end and start <= now < end and bars[-1][0] >= start - 6 * 3600:
        return bars[:-1]
    return bars


def refresh(con, symbol, rng="2y", interval="1d"):
    """Download and store bars plus the latest quote. Returns the number of bars stored."""
    got = fetch_chart(symbol, rng, interval)
    bars = got["bars"]
    if interval == "1d":
        bars = final_daily(bars, got["meta"])
    t = db.now_ms()
    con.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(symbol, interval, b[0], b[1], b[2], b[3], b[4], b[5], SOURCE, t) for b in bars])
    m = got["meta"]
    if m.get("price") and m.get("quote_time"):
        con.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?,?)",
                    (symbol, float(m["price"]), int(m["quote_time"]), t, SOURCE, 1, m.get("currency")))
    return len(bars)


def bars(con, symbol, interval="1d", since=None, until=None):
    sql = "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? AND interval=?"
    args = [symbol, interval]
    if since is not None:
        sql += " AND ts>=?"
        args.append(since)
    if until is not None:
        sql += " AND ts<?"
        args.append(until)
    return [tuple(r) for r in con.execute(sql + " ORDER BY ts", args)]


def last_quote(con, symbol):
    r = con.execute("SELECT * FROM quotes WHERE symbol=? ORDER BY quote_time DESC LIMIT 1", (symbol,)).fetchone()
    return dict(r) if r else None


def refresh_many(con, symbols, rng="2y", interval="1d"):
    ok, failed = [], {}
    for s in symbols:
        try:
            refresh(con, s, rng, interval)
            ok.append(s)
        except DataError as e:
            failed[s] = str(e)
    return ok, failed
