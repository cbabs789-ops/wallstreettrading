"""Live data checks (needs internet). Run: PWS_LIVE=1 python -m unittest tests.test_live -v"""
import os
import tempfile
import time
import unittest

os.environ.setdefault("PWS_DATA_DIR", tempfile.mkdtemp())

from wallstreet import db  # noqa: E402
from wallstreet.data import calendar, fed, news, prices, sec, thirteenf  # noqa: E402


@unittest.skipUnless(os.environ.get("PWS_LIVE"), "set PWS_LIVE=1 to hit real data sources")
class Live(unittest.TestCase):
    con = None

    @classmethod
    def setUpClass(cls):
        cls.con = db.connect(":memory:")

    def test_prices_have_timestamps_and_sane_values(self):
        n = prices.refresh(self.con, "MSFT", rng="1y")
        self.assertGreater(n, 200)
        q = prices.last_quote(self.con, "MSFT")
        self.assertGreater(q["price"], 1)
        self.assertLess(time.time() - q["quote_time"], 5 * 86400, "quote is stale")
        b = prices.bars(self.con, "MSFT")
        self.assertTrue(all(x[2] >= max(x[1], x[4]) - 1e-6 and x[3] <= min(x[1], x[4]) + 1e-6 for x in b), "high/low inconsistent")
        self.assertEqual(len({x[0] for x in b}), len(b), "duplicate bars")

    def test_sec_filings_and_insiders(self):
        new, lines = sec.refresh(self.con, "MSFT", limit=40, form4_max=3)
        self.assertGreater(new, 0)
        again, _ = sec.refresh(self.con, "MSFT", limit=40, form4_max=3)
        self.assertEqual(again, 0, "refreshing twice must not duplicate filings")
        f = sec.fundamentals(self.con, "MSFT")
        self.assertTrue(f["revenue"] and f["revenue"][-1][1] > 1e10)

    def test_fed_feed(self):
        self.assertGreaterEqual(fed.refresh(self.con), 1)
        self.assertEqual(fed.refresh(self.con), 0, "duplicate Fed events")

    def test_news_relevant_and_deduplicated(self):
        items = news.fetch("MSFT", sec.title_for("MSFT"))
        self.assertGreater(news.store(self.con, "MSFT", items, sec.title_for("MSFT")), 0)
        self.assertEqual(news.store(self.con, "MSFT", items, sec.title_for("MSFT")), 0, "duplicate headlines")
        titles = [r[0] for r in self.con.execute("SELECT title FROM events WHERE kind='news' AND symbol='MSFT'")]
        self.assertTrue(all(news.relevant(t, "MSFT", news.names_for("MSFT")) for t in titles))

    def test_calendars_and_macro(self):
        self.assertGreater(calendar.refresh_economic(self.con), 3)
        m = calendar.refresh_macro(self.con)
        self.assertIn("DGS10", m)
        self.assertTrue(0 < m["DGS10"]["value"] < 20)
        calendar.refresh_earnings(self.con, ["AAPL", "MSFT", "NVDA", "WMT", "HD", "CSCO", "CRM"], days=10)

    def test_13f(self):
        self.assertGreater(thirteenf.refresh(self.con, {"AAPL", "KO", "AMZN", "GOOGL"}), 0)
        h = thirteenf.for_symbol(self.con, "AAPL")
        self.assertTrue(h and all(("not a current position" in x["text"]) or x["stale"] for x in h))
