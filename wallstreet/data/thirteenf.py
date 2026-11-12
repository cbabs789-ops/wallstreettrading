"""13F institutional holdings for a short list of well-known filers (official SEC EDGAR data).

What a 13F is NOT: a current position. It shows long US stock holdings as of a past quarter-end and is filed up to 45
days later. A fund may have sold everything since. Every row keeps its period date and filing date, and anything
shown to the user says so. 13F holdings are context only: they never add to a score.
"""
import datetime as dt
import re
import xml.etree.ElementTree as ET

from .. import db
from .http import DataError, get
from .sec import ARCHIVE, SUBMISSIONS, TICKERS

FILERS = {1067983: "Berkshire Hathaway", 1336528: "Pershing Square", 1649339: "Scion Asset Management",
          1536411: "Duquesne Family Office", 1656456: "Appaloosa"}
_SUFFIX = r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|PLC|LLC|HOLDINGS?|GROUP|CL(ASS)? [A-C]|COM|NEW|DEL|THE|SHS|ORD|N V|S A)\b"


def norm(name):
    n = re.sub(r"[^A-Z0-9 ]+", " ", name.upper().replace("&", " AND "))
    n = re.sub(_SUFFIX, " ", n)
    return re.sub(r"\s+", " ", n).strip()


def parse_infotable(xml_text):
    root = ET.fromstring(xml_text.encode("utf-8"))
    out = {}
    for it in root.iter():
        if not it.tag.endswith("infoTable"):
            continue
        vals = {c.tag.split("}")[-1]: c for c in it.iter()}
        name = (vals.get("nameOfIssuer").text or "").strip() if vals.get("nameOfIssuer") is not None else ""
        try:
            value = float(vals["value"].text)
            shares = float(vals["sshPrnamt"].text)
        except (KeyError, TypeError, ValueError):
            continue
        k = (name, (vals["cusip"].text or "").strip() if "cusip" in vals else "")
        a = out.setdefault(k, [0.0, 0.0])                # one issuer can appear on many lines (managers, share classes)
        a[0] += value
        a[1] += shares
    return [{"issuer": k[0], "cusip": k[1], "value": v[0], "shares": v[1]} for k, v in out.items()]


def _filings(cik, n=2):
    d = get(SUBMISSIONS % cik)
    r = d["filings"]["recent"]
    rows = [(a, f, p, rp) for a, f, p, rp in zip(r["accessionNumber"], r["form"], r["filingDate"], r["reportDate"]) if f == "13F-HR"]
    return d.get("name") or FILERS.get(cik), rows[:n]


def _infotable(cik, acc):
    idx = get(ARCHIVE % (cik, acc.replace("-", ""), "index.json"))
    names = [i["name"] for i in idx["directory"]["item"] if i["name"].lower().endswith(".xml") and i["name"] != "primary_doc.xml"]
    if not names:
        raise DataError("no information table in %s" % acc)
    url = ARCHIVE % (cik, acc.replace("-", ""), names[0])
    return url, parse_infotable(get(url, kind="text"))


def refresh(con, symbols):
    """Latest two 13F-HR filings for each filer, matched to our symbols by issuer name. Returns rows written."""
    titles = {}
    for row in get(TICKERS).values():
        if row["ticker"].upper() in symbols:
            titles[norm(row["title"])] = row["ticker"].upper()
    n = 0
    for cik in FILERS:
        try:
            filer, fl = _filings(cik)
        except (DataError, KeyError):
            continue
        prev_shares = {}
        for acc, _, filed, period in reversed(fl):       # older first, so the newer one can show the change
            try:
                url, rows = _infotable(cik, acc)
            except (DataError, KeyError, ET.ParseError):
                continue
            for r in rows:
                sym = titles.get(norm(r["issuer"]))
                if not sym:
                    continue
                before = prev_shares.get(sym)
                con.execute("INSERT OR REPLACE INTO holdings13f VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (cik, filer, period, filed, sym, r["issuer"], r["value"], r["shares"], before, url, db.now_ms()))
                prev_shares[sym] = r["shares"]
                n += 1
            # symbols held last quarter but not this one were sold out; record that too
            if prev_shares and fl and acc == fl[0][0]:
                held_now = {titles.get(norm(r["issuer"])) for r in rows}
                for sym, sh in list(prev_shares.items()):
                    if sym not in held_now:
                        con.execute("INSERT OR REPLACE INTO holdings13f VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (cik, filer, period, filed, sym, sym, 0, 0, sh, url, db.now_ms()))
    return n


def for_symbol(con, symbol):
    """Latest reported holding per filer, with the quarter-on-quarter change. Context only."""
    out = []
    for r in con.execute("SELECT * FROM holdings13f h WHERE symbol=? AND period=(SELECT MAX(period) FROM holdings13f WHERE filer_cik=h.filer_cik) "
                         "ORDER BY value DESC", (symbol,)):
        prev = r["prev_shares"]
        if r["shares"] == 0:
            change = "sold the whole position"
        elif prev is None:
            change = "new position"
        elif prev == 0:
            change = "new position"
        else:
            pct = (r["shares"] / prev - 1) * 100
            change = "no change" if abs(pct) < 0.5 else "%s %.0f%%" % ("added" if pct > 0 else "cut", abs(pct))
        age = (dt.date.today() - dt.date.fromisoformat(r["filed"])).days
        stale = age > 150                                # a newer 13F should exist by now; the filer may have stopped filing
        out.append({"filer": r["filer"], "period": r["period"], "filed": r["filed"], "shares": r["shares"], "value": r["value"],
                    "change": change, "url": r["url"], "stale": stale,
                    "text": "%s: %s shares as of %s (filed %s; %s). %s" % (
                        r["filer"], format(int(r["shares"]), ","), r["period"], r["filed"], change,
                        "STALE: filed %d days ago, no newer 13F found." % age if stale else "Quarter-end snapshot, not a current position.")})
    out.sort(key=lambda h: (h["stale"], -h["value"]))
    return out
