"""Trade approvals. In 'approve' mode the autopilot never trades on its own: it writes a proposal, Telegram shows it with
Approve and Reject buttons, and only an approval from your chat places the order.

At approval time everything is re-checked, because the proposal was made from the previous close:
  - it must not have expired (proposals expire at 4:00 PM Eastern on the next weekday)
  - the price is refreshed; if it moved more than MAX_DRIFT from the proposal, the order is refused as stale
  - the size is recomputed by the risk department at the current price and account value
  - no earnings within 4 days (the same rule the autopilot uses when proposing)
  - every risk limit is checked again by the broker
A double tap can't place two orders: the status changes from 'pending' in one atomic update, and the order's client id
is fixed per proposal.
"""
import datetime as dt
import json
import logging
import secrets
from zoneinfo import ZoneInfo

from . import config, db, risk
from .data import prices
from .data.http import DataError

ET = ZoneInfo("America/New_York")
MAX_DRIFT = config.env("PWS_APPROVAL_MAX_DRIFT_PCT", 2.0, float) / 100
log = logging.getLogger("pws.approvals")

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, created_at INTEGER, expires_at INTEGER, symbol TEXT, side TEXT,
  qty REAL, price REAL, stop REAL, target REAL, strategy TEXT, client_id TEXT UNIQUE, card TEXT, status TEXT,
  decided_at INTEGER, order_id INTEGER, note TEXT, message_id INTEGER
);
"""


def ensure(con):
    con.executescript(SCHEMA)


def expiry_for(now=None):
    """4:00 PM Eastern on the next weekday after `now` (holidays are caught by the stale-price check)."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    d = now.date() + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return int(dt.datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).timestamp() * 1000)


def propose(con, card, qty, now=None):
    """Create a pending proposal from a scanner card. Returns the row (dict), or None if this setup was proposed before."""
    ensure(con)
    cid = "prop-%s-%s-%s" % (card["symbol"], card["strategy"], card["signal_time"])
    cur = con.execute(
        "INSERT OR IGNORE INTO proposals(token, created_at, expires_at, symbol, side, qty, price, stop, target, strategy, client_id, card, status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
        (secrets.token_hex(4), db.now_ms(), expiry_for(now), card["symbol"], "buy", qty, card["price"], card["stop"],
         None if card.get("exit_rule") == "sma5" else card["targets"][0], card["strategy"], cid,
         json.dumps({k: card.get(k) for k in ("score", "strategy_label", "confidence", "fail_reasons", "entry_zone", "targets",
                                               "technical", "risk_reward", "signal_age_days")}, default=str)))
    if cur.rowcount != 1:
        return None
    row = get(con, cur.lastrowid)
    db.audit(con, "approvals", "proposed", row)
    return row


def get(con, pid):
    ensure(con)
    r = con.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return dict(r) | {"card": json.loads(r["card"] or "{}")} if r else None


def pending(con):
    ensure(con)
    return [get(con, r[0]) for r in con.execute("SELECT id FROM proposals WHERE status='pending' ORDER BY id")]


def unsent(con):
    ensure(con)
    return [get(con, r[0]) for r in con.execute("SELECT id FROM proposals WHERE status='pending' AND message_id IS NULL ORDER BY id")]


def set_message(con, pid, message_id):
    con.execute("UPDATE proposals SET message_id=? WHERE id=?", (message_id, pid))


def _claim(con, pid, token, new_status):
    """Atomically move pending -> new_status. False if it was already decided (double tap) or the token is wrong."""
    cur = con.execute("UPDATE proposals SET status=?, decided_at=? WHERE id=? AND status='pending' AND (? IS NULL OR token=?)",
                      (new_status, db.now_ms(), pid, token, token))
    return cur.rowcount == 1


def _finish(con, pid, status, note, order_id=None):
    con.execute("UPDATE proposals SET status=?, note=?, order_id=COALESCE(?, order_id) WHERE id=?", (status, note, order_id, pid))
    db.audit(con, "approvals", status, {"id": pid, "note": note, "order_id": order_id})
    return get(con, pid)


def reject(con, pid, token=None, who="you"):
    ensure(con)
    if not _claim(con, pid, token, "rejected"):
        return get(con, pid), False
    return _finish(con, pid, "rejected", "rejected by %s" % who), True


def approve(con, broker, pid, token=None, refresh=None, now_ms=None):
    """Returns (proposal, changed). `refresh(symbol)` fetches a fresh quote (default: Yahoo, last 5 days)."""
    ensure(con)
    p = get(con, pid)
    if not p:
        return None, False
    if (now_ms or db.now_ms()) > p["expires_at"]:
        if _claim(con, pid, token, "expired"):
            return _finish(con, pid, "expired", "expired before approval"), True
        return get(con, pid), False
    if not _claim(con, pid, token, "approving"):
        return get(con, pid), False
    try:
        (refresh or (lambda s: prices.refresh(con, s, rng="5d")))(p["symbol"])
    except DataError as e:
        return _finish(con, pid, "failed", "couldn't refresh the price (%s); nothing was ordered" % e), True
    q = prices.last_quote(con, p["symbol"])
    drift = q["price"] / p["price"] - 1 if q and p["price"] else 0
    if not q or abs(drift) > MAX_DRIFT:
        return _finish(con, pid, "stale", "price moved %+.1f%% since the proposal (limit %.0f%%); nothing was ordered" % (
            drift * 100, MAX_DRIFT * 100)), True
    if q["price"] <= p["stop"]:
        return _finish(con, pid, "stale", "price is already at or below the stop; nothing was ordered"), True
    from .catalysts import for_symbol
    en = for_symbol(con, p["symbol"]).get("earnings_next")
    if en and en["days"] <= 4:                           # same rule as the autopilot: the stop can't protect an earnings gap
        return _finish(con, pid, "refused", "earnings %s (%s): the stop can't protect against an earnings gap; nothing was ordered" % (
            "today" if en["days"] == 0 else "on " + en["date"], en.get("time"))), True
    st = broker.state()
    qty = risk.size_for_risk(st["equity"], q["price"], p["stop"], st["cash"])
    if qty <= 0:
        return _finish(con, pid, "failed", "size rounds to zero at today's price; nothing was ordered"), True
    try:
        o = broker.submit(p["symbol"], "buy", qty, stop=p["stop"], target=p["target"], client_id=p["client_id"],
                          strategy=p["strategy"], evidence={"proposal": pid, "approved": True})
    except Exception as e:                               # e.g. stale or bad quote refused by the broker
        return _finish(con, pid, "failed", str(e)), True
    if o["status"] == "filled":
        return _finish(con, pid, "filled", "bought %s at %.2f" % (_qty(o["qty"]), o["fill_price"]), o["id"]), True
    return _finish(con, pid, "refused", "risk check refused it: %s" % o["reason"], o["id"]), True


def expire_old(con, now_ms=None):
    """Mark pending proposals past their expiry. Returns the expired rows (so their messages can be updated)."""
    ensure(con)
    now_ms = now_ms or db.now_ms()
    out = []
    for r in con.execute("SELECT id FROM proposals WHERE status='pending' AND expires_at<?", (now_ms,)).fetchall():
        if _claim(con, r[0], None, "expired"):
            out.append(_finish(con, r[0], "expired", "expired without a decision"))
    return out


def _qty(q):
    return ("%.4f" % q).rstrip("0").rstrip(".")
