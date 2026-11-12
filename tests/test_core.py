"""Unit and integration tests. Offline: no network needed (live checks are in tests/test_live.py)."""
import os
import random
import tempfile
import time
import unittest
from pathlib import Path

os.environ["PWS_DATA_DIR"] = tempfile.mkdtemp()

from wallstreet import backtest, config, db, risk  # noqa: E402
from wallstreet.analysis import indicators as ind  # noqa: E402
from wallstreet.analysis import technical  # noqa: E402
from wallstreet.broker import paper, schwab  # noqa: E402
from wallstreet.data import prices, sec  # noqa: E402
from wallstreet.strategies import STRATEGIES, features  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
DAY = 86400


def walk(n, seed, drift=0.0, vol=0.015, start=100.0, t0=1_500_000_000):
    """Random daily bars. drift/vol are per-day log-return mean and stdev."""
    rnd = random.Random(seed)
    bars, c = [], start
    for i in range(n):
        o = c
        c = o * pow(2.718281828, rnd.gauss(drift, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 3)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 3)))
        bars.append((t0 + i * DAY, o, h, lo, c, rnd.uniform(5e5, 2e6)))
    return bars


def mem():
    return db.connect(":memory:")


def put_bars(con, sym, bars):
    con.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(sym, "1d", *b, "test", 0) for b in bars])


def put_quote(con, sym, price, t=None):
    con.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?,?)", (sym, price, int(t or time.time()), db.now_ms(), "test", 1, "USD"))


class Indicators(unittest.TestCase):
    def test_sma_ema(self):
        x = [1, 2, 3, 4, 5, 6]
        self.assertEqual(ind.sma(x, 3), [None, None, 2, 3, 4, 5])
        e = ind.ema(x, 3)
        self.assertEqual(e[2], 2)                       # seeded with the simple average
        self.assertAlmostEqual(e[3], 4 * 0.5 + 2 * 0.5)

    def test_rsi_extremes_and_reference(self):
        self.assertEqual(ind.rsi(list(range(1, 30)), 14)[-1], 100.0)
        # Wilder's classic worked example (close prices from "New Concepts in Technical Trading Systems" style data)
        c = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
        self.assertAlmostEqual(ind.rsi(c, 14)[14], 70.46, places=1)

    def test_no_lookahead(self):
        b = walk(400, 1)
        c = [x[4] for x in b]
        for fn in (lambda v: ind.sma(v, 20), lambda v: ind.rsi(v, 14), lambda v: ind.macd(v)[2], lambda v: ind.rel_volume(v)):
            full, cut = fn(c), fn(c[:300])
            self.assertEqual(full[:300], cut, "an indicator changed its past values when future data was added")

    def test_rel_volume_excludes_today(self):
        v = [100] * 20 + [300]
        self.assertEqual(ind.rel_volume(v, 20)[20], 3.0)

    def test_pivots_confirmed_only(self):
        h = [1, 2, 3, 9, 3, 2, 1, 1, 1, 1, 1, 1]
        lo = [0] * 12
        hs, _ = ind.pivots(h, lo, 2, 2, upto=4)        # the peak at 3 needs 2 bars after it: not confirmed at 4
        self.assertEqual(hs, [])
        hs, _ = ind.pivots(h, lo, 2, 2, upto=5)
        self.assertIn((3, 9), hs)

    def test_snapshot_fields(self):
        s = technical.snapshot(walk(300, 2))
        for k in ("trend", "rsi", "macd_hist", "atr", "support", "resistance", "breakout20", "rel_volume"):
            self.assertIn(k, s)
        self.assertIsNone(technical.snapshot(walk(30, 2)))


class SEC(unittest.TestCase):
    def test_real_form4_tax_withholding_is_not_a_sale(self):
        owner, role, plan, tx = sec.parse_form4((FIX / "form4_csco_tax.xml").read_text())
        self.assertEqual(tx[0]["code"], "F")
        self.assertFalse(tx[0]["decision"])
        self.assertIn("taxes", tx[0]["kind"])
        self.assertIn("EVP", role)

    def test_purchase_plan_and_derivative(self):
        owner, role, plan, tx = sec.parse_form4((FIX / "form4_buy_plan.xml").read_text())
        self.assertEqual(owner, "Doe Jane")
        self.assertTrue(plan, "a 10b5-1 footnote must flag the filing as plan trades")
        p, s_, m = tx
        self.assertTrue(p["decision"] and p["acquired"] and p["value"] == 50000)
        self.assertEqual(s_["price"], 0.0)               # footnote-only price must not crash the parser
        self.assertTrue(m["derivative"] and not m["decision"])

    def test_8k_items(self):
        self.assertEqual(sec.describe("8-K", "2.02,9.01"), "8-K: earnings results")
        self.assertIn("quarter-end", sec.describe("13F-HR"))


class Prices(unittest.TestCase):
    def test_partial_daily_bar_dropped_during_session(self):
        now = 1_000_000
        bars = [(now - DAY, 1, 1, 1, 1, 1), (now - 3600, 1, 1, 1, 1, 1)]
        meta = {"session": {"start": now - 3600, "end": now + 3600}}
        self.assertEqual(len(prices.final_daily(bars, meta, now=now)), 1)
        self.assertEqual(len(prices.final_daily(bars, meta, now=now + 7200)), 2)


class Backtest(unittest.TestCase):
    def _f(self, bars):
        return features(bars)

    def test_next_open_entry_and_slippage(self):
        bars = walk(400, 3, drift=0.001)
        f = self._f(bars)
        tr = backtest.trades_for(f, "rsi2_reversion", {"rsi2": 15, "stop_atr": 3.0}, 0, len(bars))
        self.assertTrue(tr)
        t = tr[0]
        self.assertAlmostEqual(t["entry"], f["open"][t["entry_i"]] * (1 + config.SLIPPAGE_BPS / 10000))
        sig_i = t["entry_i"] - 1
        self.assertTrue(STRATEGIES["rsi2_reversion"][0](f, sig_i, {"rsi2": 15, "stop_atr": 3.0}))

    def test_stop_checked_before_target_and_gap_fill(self):
        # flat history, then a signal bar, then one bar that touches both stop and target -> must be a stop
        base = [(i * DAY, 100, 100.5, 99.5, 100, 1e6) for i in range(260)]
        f = self._f(base)
        f["atr"] = [1.0] * len(base)
        orig = STRATEGIES["trend_pullback"][0]
        try:
            STRATEGIES["trend_pullback"] = (lambda f_, i, p: {"stop_atr": 2, "target_atr": 2, "max_hold": 5} if i == 255 else None,) + STRATEGIES["trend_pullback"][1:]
            f["open"][256], f["high"][256], f["low"][256], f["close"][256] = 100, 105, 95, 100
            t = backtest.trades_for(f, "trend_pullback", {}, 0, len(base))[0]
            self.assertEqual(t["why"], "stop")
            f["open"][256], f["high"][256], f["low"][256] = 100, 100.5, 99.5
            f["open"][257], f["high"][257], f["low"][257], f["close"][257] = 90, 91, 89, 90
            t = backtest.trades_for(f, "trend_pullback", {}, 0, len(base))[0]
            self.assertEqual(t["why"], "stop (gap)")
            self.assertAlmostEqual(t["exit"], 90 * (1 - config.SLIPPAGE_BPS / 10000))
        finally:
            STRATEGIES["trend_pullback"] = (orig,) + STRATEGIES["trend_pullback"][1:]

    def test_portfolio_limits(self):
        feats = {"S%d" % k: self._f(walk(500, 10 + k, drift=0.0005)) for k in range(15)}
        for f in feats.values():
            f["ts"] = [1_500_000_000 + i * DAY for i in range(len(f["ts"]))]
        t0, t1 = feats["S0"]["ts"][200], feats["S0"]["ts"][-1] + 1
        m = backtest.portfolio(feats, "rsi2_reversion", {"rsi2": 30, "stop_atr": 3.0}, t0, t1, max_open=3)
        self.assertGreater(m["trades"], 0)
        self.assertLessEqual(m["max_drawdown"], 0)

    def test_random_walks_rarely_pass(self):
        """On prices with no edge, walk-forward should almost never pass every window."""
        passes = 0
        for seed in range(6):
            bb = {"S%d" % k: walk(900, seed * 50 + k, drift=0.0) for k in range(8)}
            bb["SPY"] = walk(900, seed * 50 + 99)
            for s, b in bb.items():
                bb[s] = [(1_400_000_000 + i * DAY,) + x[1:] for i, x in enumerate(b)]
            for strat in STRATEGIES:
                r = backtest.walk_forward(bb, strat)
                passes += r.get("verdict") == "pass"
        self.assertLessEqual(passes, 3, "too many random walks passed; the test is too loose")


class Risk(unittest.TestCase):
    def state(self, eq=10000, cash=10000, dd=0, day=0, positions=()):
        return {"equity": eq, "cash": cash, "drawdown_pct": dd, "day_pnl_pct": day, "positions": list(positions)}

    def test_limits(self):
        con = mem()
        ok, why, _ = risk.check_buy(con, self.state(), "AAPL", 5, 100, 98)
        self.assertTrue(ok, why)
        self.assertFalse(risk.check_buy(con, self.state(), "AAPL", 20, 100, 98)[0])        # 20% of account
        self.assertFalse(risk.check_buy(con, self.state(), "AAPL", 9, 100, 80)[0])         # $180 risk = 1.8%
        self.assertFalse(risk.check_buy(con, self.state(), "AAPL", 5, 100, None)[0])       # no stop
        self.assertFalse(risk.check_buy(con, self.state(dd=20), "AAPL", 1, 100, 98)[0])    # kill switch
        self.assertFalse(risk.check_buy(con, self.state(day=-4), "AAPL", 1, 100, 98)[0])   # daily limit
        many = [{"symbol": "X%d" % i, "qty": 1, "last": 10} for i in range(config.RISK["max_open_positions"])]
        self.assertFalse(risk.check_buy(con, self.state(positions=many), "AAPL", 1, 100, 98)[0])

    def test_sector_and_correlation(self):
        con = mem()
        pos = [{"symbol": "NVDA", "qty": 30, "last": 100}, {"symbol": "AMD", "qty": 30, "last": 100}]
        ok, why, _ = risk.check_buy(con, self.state(positions=pos), "AVGO", 9, 100, 98)
        self.assertFalse(ok)
        self.assertTrue(any("sector" in w for w in why))
        b = walk(100, 7)
        put_bars(con, "AAA", b)
        put_bars(con, "BBB", [(t, o * 2, h * 2, lo * 2, c * 2, v) for t, o, h, lo, c, v in b])   # identical moves
        ok, why, _ = risk.check_buy(con, self.state(positions=[{"symbol": "AAA", "qty": 1, "last": 100}]), "BBB", 1, 100, 98)
        self.assertTrue(any("lockstep" in w for w in why))

    def test_size_for_risk(self):
        q = risk.size_for_risk(10000, 100, 95, 10000)
        self.assertEqual(q, 9.995)                       # position cap (10% = $1,000) binds before the 1% risk cap (20 shares)
        self.assertEqual(risk.size_for_risk(10000, 100, 100, 10000), 0)
        self.assertAlmostEqual(risk.size_for_risk(10000, 1437.29, 1344.49, 10000), 0.6954, places=4)   # fractional, after slippage
        self.assertEqual(risk.size_for_risk(10000, 1437.29, 1344.49, 10000, fractional=False), 0)
        con = mem()
        ok, why, n = risk.check_buy(con, {"equity": 10000, "cash": 10000, "drawdown_pct": 0, "day_pnl_pct": 0, "positions": []},
                                    "LLY", 0.6954, 1437.29 * 1.0005, 1344.49)
        self.assertTrue(ok, why)


class Paper(unittest.TestCase):
    def setUp(self):
        self.con = mem()
        self.b = paper.PaperBroker(self.con, start_cash=10000)
        put_quote(self.con, "AAPL", 100)

    def test_buy_sell_and_cash(self):
        o = self.b.submit("AAPL", "buy", 5, stop=96, target=110, client_id="a1")
        self.assertEqual(o["status"], "filled")
        px = 100 * (1 + config.SLIPPAGE_BPS / 10000)
        self.assertAlmostEqual(o["fill_price"], px)
        self.assertAlmostEqual(db.kv_get(self.con, "cash"), 10000 - 5 * px)
        s = self.b.submit("AAPL", "sell", 5, client_id="a2")
        self.assertEqual(s["status"], "filled")
        self.assertEqual(self.b.positions(), [])

    def test_risk_sized_order_always_fills(self):
        # Regression: sizing at the quote price let slippage push the fill 0.0004% over the 10% cap, so the risk
        # engine approved orders the broker then rejected. Sizing must use the fill price.
        for price, stop in ((1437.29, 1344.49), (100, 99.5), (100, 60), (3.17, 2.9)):
            con = mem()
            b = paper.PaperBroker(con, start_cash=10000)
            put_quote(con, "LLY", price)
            st = b.state()
            q = risk.size_for_risk(st["equity"], price, stop, st["cash"])
            o = b.submit("LLY", "buy", q, stop=stop)
            self.assertEqual(o["status"], "filled", (price, stop, q, o.get("reason")))

    def test_duplicate_client_id(self):
        a = self.b.submit("AAPL", "buy", 5, stop=96, client_id="dup")
        b = self.b.submit("AAPL", "buy", 5, stop=96, client_id="dup")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.b.positions()[0]["qty"], 5)

    def test_rejections(self):
        self.assertEqual(self.b.submit("AAPL", "sell", 1)["status"], "rejected")          # nothing to sell
        r = self.b.submit("AAPL", "buy", 50, stop=96)
        self.assertEqual(r["status"], "rejected")
        self.assertIn("per-position limit", r["reason"])
        put_quote(self.con, "OLD", 10, t=time.time() - 10 * DAY)
        with self.assertRaises(paper.OrderError):
            self.b.submit("OLD", "buy", 1, stop=9)

    def test_exits_once(self):
        self.b.submit("AAPL", "buy", 5, stop=96, target=110)
        put_quote(self.con, "AAPL", 95, t=time.time() + 1)
        a = self.b.check_exits()
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["status"], "filled")
        self.assertEqual(self.b.check_exits(), [])

    def test_audit_trail(self):
        self.b.submit("AAPL", "buy", 5, stop=96)
        self.b.submit("AAPL", "buy", 500, stop=96)
        acts = [r["action"] for r in self.con.execute("SELECT action FROM audit")]
        self.assertIn("order_filled", acts)
        self.assertIn("order_rejected", acts)


class Safety(unittest.TestCase):
    def test_schwab_has_no_order_methods(self):
        names = [n.lower() for n in dir(schwab)]
        for bad in ("order", "place", "submit", "cancel", "replace"):
            self.assertFalse(any(bad in n for n in names), "the Schwab connector must not be able to place orders: %s" % bad)

    def test_schwab_off_without_keys(self):
        os.environ.pop("SCHWAB_APP_KEY", None)
        with self.assertRaises(schwab.SchwabDisabled):
            schwab.login_url()

    def test_only_paper_broker_writes_orders(self):
        root = Path(__file__).resolve().parent.parent / "wallstreet"
        writers = [p.name for p in root.rglob("*.py") if "INSERT INTO orders" in p.read_text()]
        self.assertEqual(writers, ["paper.py"])


if __name__ == "__main__":
    unittest.main()
