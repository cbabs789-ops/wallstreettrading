"""Catalyst intelligence: what could move a stock soon, with a link to the source for every item.

Catalyst score (0-100) measures how much is going on, not which way the stock will go:
  Earnings within 10 days ..................... 25   (scheduled, Nasdaq calendar; also raises gap risk)
  Material 8-K in the last 14 days ............ 20   (SEC, confirmed; earnings 8-Ks and exhibit-only 8-Ks excluded)
  Insider open-market buying, last 30 days .... 25   (+10 if two or more insiders; 10b5-1 plan trades excluded)
  Unusual news volume ......................... 15   (last 3 days at 2.5x+ the prior 4-week daily rate, 5+ stories)
  Rating change, deal or guidance headline .... 15   (last 7 days; keyword heuristic on headlines; unconfirmed)
  (news volume needs 3 weeks of our own collection history before it can score)
Insider SELLING and negative headlines don't add to the score; they are listed as risks.
13F holdings are shown as context only (stale by design) and never scored.
"""
import datetime as dt
import json
import time

from . import db
from .data import thirteenf
from .data.news import classify
from .data.sec import describe

DAY_MS = 86400000
MATERIAL_8K = {"1.01", "1.02", "2.01", "2.05", "2.06", "3.01", "4.02", "5.01", "5.02", "8.01"}


def _date(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def for_symbol(con, symbol, now_ms=None):
    now_ms = now_ms or db.now_ms()
    today = _date(now_ms)
    items, risks, parts = [], [], {}

    # SEC filings (confirmed)
    since14 = _date(now_ms - 14 * DAY_MS)
    material, earnings_8k = [], None
    for r in con.execute("SELECT form, filed_at, items, url FROM filings WHERE symbol=? AND filed_at>=? AND filed_at<=? ORDER BY filed_at DESC",
                         (symbol, since14, today)):
        its = set((r["items"] or "").split(","))
        if r["form"] == "8-K" and "2.02" in its:
            earnings_8k = earnings_8k or r["filed_at"]
        is_material = r["form"] == "8-K" and its & MATERIAL_8K
        if r["form"] in ("8-K", "10-Q", "10-K", "SC 13D", "SCHEDULE 13D", "S-1", "424B5"):
            items.append({"kind": "filing", "text": describe(r["form"], r["items"]), "date": r["filed_at"], "url": r["url"],
                          "source": "SEC EDGAR", "confirmed": True, "earnings": "2.02" in its, "material": bool(is_material)})
        if is_material:
            material.append(r["filed_at"])
        if r["form"] in ("S-1", "424B5"):
            risks.append("%s prospectus filed %s: a share sale would dilute, but large companies often file these for bond deals; check the filing"
                         % (r["form"], r["filed_at"]))
        if r["form"] == "8-K" and "4.02" in its:
            risks.append("Non-reliance on prior financials (8-K item 4.02) filed %s" % r["filed_at"])
    parts["material_8k"] = 20 if material else 0

    # Insiders (confirmed; only open-market P/S are real decisions)
    since30 = _date(now_ms - 30 * DAY_MS)
    buyers, sellers = set(), 0.0
    for r in con.execute("SELECT owner, role, txn_date, code, value, plan_10b5_1, url FROM insider_txns WHERE symbol=? AND txn_date>=? "
                         "AND code IN ('P','S') AND derivative=0 ORDER BY txn_date DESC", (symbol, since30)):
        plan = ", pre-scheduled 10b5-1 plan" if r["plan_10b5_1"] else ""
        items.append({"kind": "insider", "text": "%s (%s): open-market %s of $%s%s" % (r["owner"], r["role"] or "insider",
                      "purchase" if r["code"] == "P" else "sale", format(round(r["value"] or 0), ","), plan),
                      "date": r["txn_date"], "url": r["url"], "source": "SEC Form 4", "confirmed": True,
                      "buy": r["code"] == "P" and not r["plan_10b5_1"]})
        if r["code"] == "P" and not r["plan_10b5_1"]:
            buyers.add(r["owner"])
        elif r["code"] == "S" and not r["plan_10b5_1"]:
            sellers += r["value"] or 0
    parts["insider_buying"] = (25 + (10 if len(buyers) >= 2 else 0)) if buyers else 0
    if sellers > 5e6:
        risks.append("Insiders sold $%.1fM on the open market (not under a plan) in the last 30 days" % (sellers / 1e6))

    # Earnings dates
    nxt = con.execute("SELECT detail, url FROM events WHERE kind='earnings' AND symbol=? AND published_at>=? ORDER BY published_at LIMIT 1",
                      (symbol, now_ms - DAY_MS)).fetchone()
    last_ev = con.execute("SELECT detail FROM events WHERE kind='earnings' AND symbol=? AND published_at<? AND published_at>=? "
                          "ORDER BY published_at DESC LIMIT 1", (symbol, now_ms - DAY_MS, now_ms - 4 * DAY_MS)).fetchone()
    earnings_next = None
    earnings_soon = False
    if nxt:
        d = json.loads(nxt["detail"])
        days = (dt.date.fromisoformat(d["date"]) - dt.date.fromisoformat(today)).days
        if days >= 0:
            earnings_next = {"date": d["date"], "time": d.get("time"), "days": days, "eps_forecast": d.get("eps_forecast"), "url": nxt["url"]}
            earnings_soon = days <= 4
            items.append({"kind": "earnings", "text": "Earnings %s (%s)%s" % (
                "today" if days == 0 else "in %d day%s" % (days, "s" if days > 1 else ""), d.get("time"),
                ", EPS forecast %s" % d["eps_forecast"] if d.get("eps_forecast") else ""),
                "date": d["date"], "url": nxt["url"], "source": "Nasdaq calendar", "confirmed": False})
    parts["earnings_ahead"] = 25 if earnings_next and earnings_next["days"] <= 10 else 0
    earnings_recent = bool((earnings_8k and earnings_8k >= _date(now_ms - 3 * DAY_MS)) or last_ev)

    # News (unconfirmed, deduplicated at ingest)
    rows = con.execute("SELECT title, url, source, published_at, detail FROM events WHERE kind='news' AND symbol=? AND published_at>=? "
                       "AND published_at<=? ORDER BY published_at DESC", (symbol, now_ms - 30 * DAY_MS, now_ms)).fetchall()
    recent = [r for r in rows if r["published_at"] >= now_ms - 3 * DAY_MS]
    base = (len(rows) - len(recent)) / 27.0
    # A baseline needs history we collected ourselves: feeds only return the last week or two, so until we have
    # 3 weeks of our own collection every symbol would look like a "spike".
    first = con.execute("SELECT MIN(fetched_at) FROM events WHERE kind='news' AND symbol=?", (symbol,)).fetchone()[0]
    baseline_ok = bool(first and now_ms - first >= 21 * DAY_MS)
    spike = baseline_ok and len(recent) >= 5 and len(recent) / 3.0 >= 2.5 * max(base, 0.34)
    parts["news_volume"] = 15 if spike else 0
    hot, neg = False, []
    for r in rows:
        if r["published_at"] < now_ms - 7 * DAY_MS:
            continue
        cat_, tone_ = classify(r["title"])               # re-label at read time so old rows follow the current rules
        d = {"category": cat_, "tone": tone_}
        if d.get("category") in ("rating_change", "deal", "guidance"):
            hot = True
        if d.get("tone") == "negative" and d.get("category") in ("legal", "guidance", "rating_change", "capital"):
            neg.append(r["title"])
        items.append({"kind": "news", "text": r["title"], "date": _date(r["published_at"]), "url": r["url"], "source": r["source"],
                      "confirmed": False, "category": d.get("category"), "tone": d.get("tone")})
    parts["headline_events"] = 15 if hot else 0
    if neg:
        risks.append("Negative headline (keyword read, unconfirmed): %s" % neg[0][:140])

    holders = thirteenf.for_symbol(con, symbol)
    score = min(100, sum(parts.values()))
    items.sort(key=lambda x: x["date"] or "", reverse=True)
    return {"symbol": symbol, "score": score, "parts": parts, "items": items[:25], "risks": risks, "holders_13f": holders,
            "earnings_next": earnings_next, "earnings_soon": earnings_soon, "earnings_recent": earnings_recent,
            "news_count_3d": len(recent), "news_rate_baseline": round(base, 2) if baseline_ok else None, "as_of": now_ms}


def rank(con, symbols, now_ms=None):
    out = [for_symbol(con, s, now_ms) for s in symbols]
    return sorted(out, key=lambda c: -c["score"])
