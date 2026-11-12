"""Scheduled events: company earnings dates, economic releases, Fed meetings, and a macro snapshot.

Sources:
  Earnings: Nasdaq earnings calendar API (unofficial, public). Dates are company-announced or estimated by Nasdaq;
            stored with confirmed=0 because a company can move them.
  Economic releases: BLS official release schedules (CPI, jobs report, PPI, job openings). confirmed=1.
  Fed meetings: federalreserve.gov FOMC calendar. confirmed=1.
  Macro numbers: FRED (St. Louis Fed) public CSV downloads. Each value keeps its own observation date.
"""
import datetime as dt
import json
import re
import time

from .. import db
from .http import DataError, get

NASDAQ = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
BLS = {"CPI (inflation)": "cpi", "Jobs report": "empsit", "PPI (producer prices)": "ppi", "Job openings (JOLTS)": "jolts"}
FOMC = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={s}"
MACRO = {"FEDFUNDS": "Fed funds rate %", "DGS10": "10-year Treasury %", "DGS2": "2-year Treasury %",
         "T10Y2Y": "10y minus 2y spread", "UNRATE": "Unemployment rate %", "CPIAUCSL": "CPI index", "ICSA": "Weekly jobless claims"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _ms(d):
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _upsert(con, eid, kind, symbol, title, url, source, when, confirmed, detail):
    """Scheduled events can move: replace the stored row when the source changes the date or details."""
    old = con.execute("SELECT published_at, detail FROM events WHERE id=?", (eid,)).fetchone()
    d = json.dumps(detail)
    if old and old["published_at"] == when and old["detail"] == d:
        return 0
    con.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                (eid, kind, symbol, title, url, source, when, confirmed, d, db.now_ms()))
    return 1


def business_days(start, n):
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def refresh_earnings(con, symbols, days=15, today=None):
    """Next `days` business days of Nasdaq's calendar, kept only for our symbols. Returns rows written."""
    want = set(symbols)
    n = 0
    for d in business_days(today or dt.date.today(), days):
        try:
            data = get(NASDAQ.format(d=d.isoformat()), headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
        except DataError:
            continue
        for r in ((data or {}).get("data") or {}).get("rows") or []:
            sym = (r.get("symbol") or "").upper()
            if sym not in want:
                continue
            when = {"time-pre-market": "before the open", "time-after-hours": "after the close"}.get(r.get("time"), "time not given")
            n += _upsert(con, "earn:%s:%s" % (sym, d.isoformat()), "earnings", sym, "%s reports earnings (%s)" % (sym, when),
                         "https://www.nasdaq.com/market-activity/stocks/%s/earnings" % sym.lower(), "nasdaq.com", _ms(d), 0,
                         {"date": d.isoformat(), "time": when, "eps_forecast": r.get("epsForecast"), "estimates": r.get("noOfEsts"),
                          "fiscal_quarter": r.get("fiscalQuarterEnding")})
    return n


def parse_bls(html):
    """[(reference month, date)] from a BLS schedule page."""
    t = html[html.find("Schedule of Releases"):]
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) >= 2:
            m = re.match(r"([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})", cells[1])
            if m:
                out.append((cells[0], dt.date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))))
    return out


def parse_fomc(html):
    out = []
    for yr in re.findall(r"(\d{4}) FOMC Meetings", html):
        i = html.find(yr + " FOMC Meetings")
        j = html.find("FOMC Meetings", i + 20)
        seg = html[i:j if j > 0 else len(html)]
        months = [re.sub("<[^>]+>", "", m).strip() for m in re.findall(r"fomc-meeting__month[^>]*>(.*?)</div>", seg, re.S)]
        days = [re.sub("<[^>]+>", "", d).strip() for d in re.findall(r"fomc-meeting__date[^>]*>(.*?)</div>", seg, re.S)]
        for mo, ds in zip(months, days):
            m = re.match(r"(\d+)(?:-(\d+))?(\*)?", ds)
            mon = MONTHS.get(mo[:3].lower())
            if not m or not mon:
                continue
            last = int(m.group(2) or m.group(1))
            try:
                out.append((dt.date(int(yr), mon, last), bool(m.group(3))))
            except ValueError:                           # meetings spanning a month end, e.g. "Apr/May 30-1"
                continue
    return out


def refresh_economic(con):
    n = 0
    for name, slug in BLS.items():
        url = "https://www.bls.gov/schedule/news_release/%s.htm" % slug
        try:
            rows = parse_bls(get(url, kind="text"))
        except DataError:
            continue
        for ref, d in rows:
            n += _upsert(con, "econ:%s:%s" % (slug, d.isoformat()), "econ", None, "%s for %s" % (name, ref), url, "bls.gov",
                         _ms(d), 1, {"date": d.isoformat(), "time": "8:30 AM ET", "release": name})
    try:
        for d, sep in parse_fomc(get(FOMC, kind="text")):
            n += _upsert(con, "econ:fomc:%s" % d.isoformat(), "econ", None,
                         "Fed rate decision (FOMC)%s" % (", with projections" if sep else ""), FOMC, "federalreserve.gov",
                         _ms(d), 1, {"date": d.isoformat(), "time": "2:00 PM ET", "release": "FOMC"})
    except DataError:
        pass
    return n


def parse_fred(csv_text):
    out = []
    for line in csv_text.strip().splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            out.append((d, float(v)))
        except ValueError:                               # FRED writes "." for missing days
            continue
    return out


def refresh_macro(con):
    snap = {}
    for sid, label in MACRO.items():
        try:
            obs = parse_fred(get(FRED.format(s=sid), kind="text", timeout=20, retries=2))
        except DataError:
            continue
        if not obs:
            continue
        x = {"label": label, "value": obs[-1][1], "date": obs[-1][0], "prev": obs[-2][1] if len(obs) > 1 else None,
             "url": "https://fred.stlouisfed.org/series/" + sid}
        if sid == "CPIAUCSL" and len(obs) > 12:
            x["yoy_pct"] = round((obs[-1][1] / obs[-13][1] - 1) * 100, 2)
            x["label"] = "CPI inflation, year over year %"
        snap[sid] = x
    if snap:
        db.kv_set(con, "macro", {"fetched_at": int(time.time()), "series": snap})
    return snap


def upcoming(con, kind, days=14, symbol=None, now_ms=None):
    now_ms = now_ms or db.now_ms()
    start = now_ms - 86400000                            # include today
    q = "SELECT * FROM events WHERE kind=? AND published_at>=? AND published_at<=?"
    args = [kind, start, now_ms + days * 86400000]
    if symbol:
        q += " AND symbol=?"
        args.append(symbol)
    return [dict(r) | {"detail": json.loads(r["detail"] or "{}")} for r in con.execute(q + " ORDER BY published_at", args)]
