"""Paper broker: the ONLY place orders are created. Simulated fills, no connection to any real account.

- Market orders fill at the latest stored quote plus slippage (against you), only if the quote is recent enough.
- client_id makes every order idempotent: sending the same client_id twice returns the first order, never a second fill.
- Long only (no shorting, no margin, no options). Sells can only reduce or close a position.
- Every order, rejection and fill is written to the audit table.
"""
import json
import logging
import time
import uuid

from .. import config, db, risk
from ..data import prices

log = logging.getLogger("pws.paper")
MAX_QUOTE_AGE_S = 4 * 86400          # a quote older than this (e.g. over a long weekend + holiday) is refused


class OrderError(Exception):
    pass


class PaperBroker:
    def __init__(self, con, start_cash=None):
        self.con = con
        if db.kv_get(con, "cash") is None:
            cash = float(start_cash if start_cash is not None else config.PAPER_START_CASH)
            db.kv_set(con, "cash", cash)
            db.kv_set(con, "start_cash", cash)
            db.kv_set(con, "peak_equity", cash)
            db.audit(con, "paper", "account_opened", {"cash": cash})

    # ---- state
    def positions(self):
        out = []
        for r in self.con.execute("SELECT * FROM positions WHERE qty>0 ORDER BY symbol"):
            q = prices.last_quote(self.con, r["symbol"])
            last = q["price"] if q else r["avg_price"]
            out.append(dict(r) | {"last": last, "quote_time": q["quote_time"] if q else None,
                                  "value": r["qty"] * last, "pnl": (last - r["avg_price"]) * r["qty"],
                                  "pnl_pct": (last / r["avg_price"] - 1) * 100})
        return out

    def state(self):
        cash = db.kv_get(self.con, "cash")
        pos = self.positions()
        eq = cash + sum(p["value"] for p in pos)
        peak = max(db.kv_get(self.con, "peak_equity", eq), eq)
        day_key = time.strftime("day_start_%Y-%m-%d")
        day_start = db.kv_get(self.con, day_key)
        if day_start is None:
            db.kv_set(self.con, day_key, eq)
            day_start = eq
        start = db.kv_get(self.con, "start_cash")
        return {"cash": cash, "equity": eq, "positions": pos, "peak": peak, "start": start,
                "drawdown_pct": (1 - eq / peak) * 100 if peak else 0, "total_return_pct": (eq / start - 1) * 100,
                "day_pnl_pct": (eq / day_start - 1) * 100 if day_start else 0}

    def mark(self):
        """Record equity (call after prices refresh). Also moves the peak used by the drawdown kill switch."""
        s = self.state()
        db.kv_set(self.con, "peak_equity", s["peak"])
        self.con.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)", (db.now_ms(), s["equity"], s["cash"]))
        return s

    # ---- orders
    def _quote(self, symbol):
        q = prices.last_quote(self.con, symbol)
        if not q:
            raise OrderError("no quote for %s: refresh prices first" % symbol)
        age = time.time() - q["quote_time"]
        if age > MAX_QUOTE_AGE_S:
            raise OrderError("latest %s quote is %.1f days old: refresh prices first" % (symbol, age / 86400))
        return q

    def submit(self, symbol, side, qty, stop=None, target=None, client_id=None, strategy="manual", evidence=None):
        """Market order. Returns the order row (dict). Rejections are returned with status 'rejected', not raised."""
        symbol, side = symbol.upper(), side.lower()
        client_id = client_id or uuid.uuid4().hex
        old = self.con.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        if old:
            db.audit(self.con, "paper", "duplicate_ignored", {"client_id": client_id})
            return dict(old)
        if side not in ("buy", "sell"):
            raise OrderError("side must be buy or sell")
        q = self._quote(symbol)
        slip = config.SLIPPAGE_BPS / 10000
        px = q["price"] * (1 + slip if side == "buy" else 1 - slip)
        status, reason = "filled", ""
        st = self.state()
        held = next((p for p in st["positions"] if p["symbol"] == symbol), None)
        if side == "buy":
            ok, why, nums = risk.check_buy(self.con, st, symbol, qty, px, stop)
            if not ok:
                status, reason = "rejected", "; ".join(why)
        else:
            if not held or qty > held["qty"] + 1e-9:
                status, reason = "rejected", "can only sell shares you hold (no shorting)"
            nums = {}
        comm = config.COMMISSION_PER_ORDER if status == "filled" else 0
        now = db.now_ms()
        self.con.execute("BEGIN")
        try:
            self.con.execute(
                "INSERT INTO orders(client_id, symbol, side, qty, order_type, stop_price, target_price, status, reason, created_at, "
                "filled_at, fill_price, commission, quote_time, strategy, evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_id, symbol, side, qty, "market", stop, target, status, reason, now, now if status == "filled" else None,
                 px if status == "filled" else None, comm, q["quote_time"], strategy, json.dumps(evidence or {}, default=str)))
            if status == "filled":
                cash = db.kv_get(self.con, "cash")
                if side == "buy":
                    db.kv_set(self.con, "cash", cash - qty * px - comm)
                    if held:
                        nq = held["qty"] + qty
                        self.con.execute("UPDATE positions SET qty=?, avg_price=?, stop_price=COALESCE(?, stop_price), target_price=COALESCE(?, target_price) WHERE symbol=?",
                                         (nq, (held["qty"] * held["avg_price"] + qty * px) / nq, stop, target, symbol))
                    else:
                        self.con.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?,?)", (symbol, qty, px, stop, target, now, strategy))
                else:
                    db.kv_set(self.con, "cash", cash + qty * px - comm)
                    left = held["qty"] - qty
                    if left <= 1e-9:
                        self.con.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
                    else:
                        self.con.execute("UPDATE positions SET qty=? WHERE symbol=?", (left, symbol))
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        row = dict(self.con.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone())
        db.audit(self.con, "paper", "order_" + status, {"order": row, "risk": nums})
        log.info("paper %s %s %s x%s @ %.2f %s", status, side, symbol, qty, px, reason)
        return row

    def check_exits(self):
        """Sell any position whose latest quote is at/through its stop or target. Returns the orders placed."""
        done = []
        for p in self.positions():
            why = None
            if p["stop_price"] and p["last"] <= p["stop_price"]:
                why = "stop hit (%.2f <= %.2f)" % (p["last"], p["stop_price"])
            elif p["target_price"] and p["last"] >= p["target_price"]:
                why = "target hit (%.2f >= %.2f)" % (p["last"], p["target_price"])
            if why:
                cid = "exit-%s-%s" % (p["symbol"], p["quote_time"])        # one exit per quote, even if called twice
                done.append(self.submit(p["symbol"], "sell", p["qty"], client_id=cid, strategy=p["strategy"], evidence={"exit": why}))
        return done
