"""Alerts. Each alert has a stable id built from what it is about, so the same event never alerts twice.

Delivery is local: alerts are stored, appended to var/alerts.log, shown on the dashboard and in the briefing.
(No messaging service is wired in; adding one means reading undelivered rows and marking them delivered.)
"""
import hashlib
import json
import time

from . import config, db

SEVERITY = ("info", "watch", "action", "critical")


def _id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def add(con, kind, key, severity, title, symbol=None, detail=None, url=None):
    """Insert unless an alert with the same (kind, key) exists. Returns True if new."""
    cur = con.execute("INSERT OR IGNORE INTO alerts(id, created_at, severity, kind, symbol, title, detail, url) VALUES(?,?,?,?,?,?,?,?)",
                      (_id(kind, key), db.now_ms(), severity, kind, symbol, title, json.dumps(detail or {}, default=str), url))
    return cur.rowcount == 1


def generate(con, cards=None, exits=None, state=None, catalyst_by_symbol=None, now_ms=None):
    """Build alerts from the latest run. Returns the list of NEW alerts (dicts)."""
    now_ms = now_ms or db.now_ms()
    before = {r[0] for r in con.execute("SELECT id FROM alerts")}
    for c in cards or []:
        sev = "action" if c["score"] >= 60 else "watch"
        add(con, "setup", (c["symbol"], c["strategy"], c["signal_time"]), sev,
            "%s: %s setup, score %d (stop %.2f, target %.2f)" % (c["symbol"], c["strategy_label"], c["score"], c["stop"], c["targets"][0]),
            c["symbol"], {"score": c["score"], "confidence": c["confidence"]})
    for e in exits or []:
        add(con, "exit", (e.get("symbol"), e.get("client_id") or e.get("id")), "action",
            "%s %s at %s (%s)" % (e.get("symbol"), "sold", e.get("fill_price"), e.get("reason") or e.get("why")), e.get("symbol"), e)
    if state:
        R = config.RISK
        dd = state.get("drawdown_pct", 0)
        if dd >= R["max_drawdown_pct"]:
            add(con, "kill_switch", round(dd), "critical", "Kill switch: drawdown %.1f%%. New buys are blocked." % dd)
        elif dd >= R["max_drawdown_pct"] / 2:
            add(con, "drawdown", (time.strftime("%Y-%m-%d"), int(dd)), "watch", "Drawdown %.1f%% (kill switch at %.0f%%)" % (dd, R["max_drawdown_pct"]))
        if state.get("day_pnl_pct", 0) <= -R["daily_loss_limit_pct"]:
            add(con, "daily_loss", time.strftime("%Y-%m-%d"), "action", "Daily loss limit hit (%.1f%%). No new buys today." % state["day_pnl_pct"])
        held = {p["symbol"] for p in state.get("positions", [])}
        for sym in held:
            cat = (catalyst_by_symbol or {}).get(sym)
            if not cat:
                continue
            en = cat.get("earnings_next")
            if en and en["days"] <= 3:
                add(con, "earnings_held", (sym, en["date"]), "action",
                    "You hold %s and it reports earnings %s (%s). The stop can be gapped." % (sym, "today" if en["days"] == 0 else en["date"], en["time"]),
                    sym, en, en.get("url"))
    for sym, cat in (catalyst_by_symbol or {}).items():
        for it in cat.get("items", []):
            if it["kind"] == "insider" and it.get("buy"):
                add(con, "insider_buy", (sym, it["url"], it["text"]), "watch", "%s insider buy: %s" % (sym, it["text"]), sym, it, it["url"])
            if it["kind"] == "filing" and it.get("material"):
                add(con, "8k", (sym, it["url"]), "info", "%s filed %s" % (sym, it["text"]), sym, it, it["url"])
    # stale data
    q = con.execute("SELECT MAX(quote_time) FROM quotes WHERE symbol='SPY'").fetchone()[0]
    if q and now_ms / 1000 - q > 4 * 86400:
        add(con, "stale", time.strftime("%Y-%m-%d"), "critical", "Price data is %.0f days old. Orders are refused until it refreshes." % ((now_ms / 1000 - q) / 86400))
    new = [dict(r) for r in con.execute("SELECT * FROM alerts ORDER BY created_at") if r["id"] not in before]
    if new:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.DATA_DIR / "alerts.log", "a") as fh:
            for a in new:
                fh.write("%s [%s] %s%s\n" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(a["created_at"] / 1000)), a["severity"].upper(),
                                             a["title"], " %s" % a["url"] if a["url"] else ""))
    return new


def recent(con, days=7, limit=50):
    return [dict(r) for r in con.execute("SELECT * FROM alerts WHERE created_at>=? ORDER BY created_at DESC LIMIT ?",
                                         (db.now_ms() - days * 86400000, limit))]
