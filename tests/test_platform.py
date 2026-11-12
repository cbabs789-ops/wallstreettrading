"""Tests for the intelligence, performance, alerts, autopilot, briefing and dashboard layers. Offline."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("PWS_DATA_DIR", tempfile.mkdtemp())

from tests.test_core import DAY, mem, put_bars, put_quote, walk  # noqa: E402
from wallstreet import alerts, autopilot, backtest, briefing, catalysts, config, dashboard, db, performance, scanner  # noqa: E402
from wallstreet.broker import paper  # noqa: E402
from wallstreet.data import calendar, news, thirteenf  # noqa: E402
from wallstreet.strategies import features  # noqa: E402

NOW_MS = 1_800_000_000_000


def news_row(con, sym, title, ms, fetched=None):
    cat, tone = news.classify(title)
    con.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("news:%s:%s" % (sym, abs(hash(title))), "news", sym, title, "https://example.com/" + str(abs(hash(title))), "Test",
                 ms, 0, json.dumps({"category": cat, "tone": tone}), fetched or ms))


class News(unittest.TestCase):
    RSS = """<?xml version="1.0"?><rss><channel>
      <item><title>Cisco beats estimates, raises outlook - Reuters</title><link>https://news.google.com/a</link>
        <pubDate>Thu, 12 Nov 2026 12:00:00 GMT</pubDate><source url="https://reuters.com">Reuters</source></item>
      <item><title>Cisco beats estimates, raises outlook - MarketWatch</title><link>https://news.google.com/b</link>
        <pubDate>Thu, 12 Nov 2026 12:05:00 GMT</pubDate><source url="https://mw.com">MarketWatch</source></item>
      <item><title>Networking Market Report 2026: CAGR 9% with Cisco and Juniper</title><link>https://x/c</link>
        <pubDate>Thu, 12 Nov 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""

    def test_syndicated_story_is_one_event_and_spam_dropped(self):
        items = news.parse(self.RSS, "google")
        self.assertEqual(len(items), 3)
        self.assertEqual(news.norm_title(items[0]["title"]), news.norm_title(items[1]["title"]))
        names = news.names_for("CSCO")
        kept = {news.norm_title(i["title"]) for i in items if news.relevant(i["title"], "CSCO", names)}
        self.assertEqual(len(kept), 1)

    def test_relevance_and_categories(self):
        self.assertFalse(news.relevant("Fintech Acquisition Corp V files 8-K", "V", news.names_for("V")))
        self.assertTrue(news.relevant("Visa raises dividend", "V", news.names_for("V")))
        self.assertFalse(news.relevant("Intelligent automation trends", "INTC", news.names_for("INTC")))
        self.assertEqual(news.classify("Cisco Stock Acquired by Rep. Smith")[0], "general")   # a purchase, not M&A
        self.assertEqual(news.classify("AMD to acquire ZT Systems")[0], "deal")
        self.assertEqual(news.classify("Morgan Stanley downgrades Nvidia")[0], "rating_change")
        self.assertEqual(news.classify("Walmart raises full-year outlook")[0], "guidance")

    def test_prune_removes_irrelevant_rows(self):
        con = mem()
        news_row(con, "CSCO", "Cisco names new CFO", NOW_MS)
        news_row(con, "CSCO", "Jensen Huang resigns as Nvidia CEO", NOW_MS)
        self.assertEqual(news.prune(con, "CSCO"), 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)


class Calendar(unittest.TestCase):
    def test_parsers(self):
        bls = """<h3>Schedule of Releases for the Consumer Price Index</h3><table><tr><th>Reference Month</th><th>Release Date</th></tr>
                 <tr><td>October 2026</td><td>Nov. 10, 2026</td><td>08:30 AM</td></tr>
                 <tr><td>November 2026</td><td>Dec. 10, 2026</td><td>08:30 AM</td></tr></table>"""
        rows = calendar.parse_bls(bls)
        self.assertEqual([d.isoformat() for _, d in rows], ["2026-11-10", "2026-12-10"])
        fomc = ('<h4>2026 FOMC Meetings</h4><div class="fomc-meeting__month"><strong>December</strong></div>'
                '<div class="fomc-meeting__date">8-9*</div>')
        self.assertEqual(calendar.parse_fomc(fomc)[0][0].isoformat(), "2026-12-09")
        self.assertTrue(calendar.parse_fomc(fomc)[0][1])
        self.assertEqual(calendar.parse_fred("observation_date,DGS10\n2026-11-06,4.80\n2026-11-09,.\n2026-11-10,4.88\n")[-1], ("2026-11-10", 4.88))

    def test_moved_date_replaces_row(self):
        con = mem()
        self.assertEqual(calendar._upsert(con, "earn:X:1", "earnings", "X", "t", "u", "s", 100, 0, {"a": 1}), 1)
        self.assertEqual(calendar._upsert(con, "earn:X:1", "earnings", "X", "t", "u", "s", 100, 0, {"a": 1}), 0)
        self.assertEqual(calendar._upsert(con, "earn:X:1", "earnings", "X", "t", "u", "s", 200, 0, {"a": 1}), 1)
        self.assertEqual(con.execute("SELECT published_at FROM events").fetchone()[0], 200)


class ThirteenF(unittest.TestCase):
    XML = """<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
      <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>1000</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt></shrsOrPrnAmt></infoTable>
      <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>500</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt></shrsOrPrnAmt></infoTable>
    </informationTable>"""

    def test_lines_for_one_issuer_are_summed(self):
        rows = thirteenf.parse_infotable(self.XML)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["value"], rows[0]["shares"]), (1500, 15))
        self.assertEqual(thirteenf.norm("Apple Inc."), thirteenf.norm("APPLE INC"))

    def test_holding_is_labelled_as_past_snapshot(self):
        con = mem()
        filed = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 40 * DAY))
        con.execute("INSERT INTO holdings13f VALUES(?,?,?,?,?,?,?,?,?,?,?)", (1, "Fund A", "2026-06-30", filed, "AAPL", "APPLE INC", 2e6, 200, 100, "u", 0))
        con.execute("INSERT INTO holdings13f VALUES(?,?,?,?,?,?,?,?,?,?,?)", (2, "Fund B", "2025-06-30", "2025-08-14", "AAPL", "APPLE INC", 1e6, 100, None, "u", 0))
        h = thirteenf.for_symbol(con, "AAPL")
        self.assertEqual(h[0]["change"], "added 100%")
        self.assertIn("not a current position", h[0]["text"])
        self.assertTrue(h[1]["stale"])
        self.assertIn("STALE", h[1]["text"])


class Catalysts(unittest.TestCase):
    def test_score_parts_and_exclusions(self):
        con = mem()
        day = lambda k: time.strftime("%Y-%m-%d", time.gmtime(NOW_MS / 1000 - k * DAY))   # noqa: E731
        con.execute("INSERT INTO filings VALUES(?,?,?,?,?,?,?,?,?,?)", ("a1", "1", "XYZ", "8-K", day(2), day(2), "1.01,9.01", "https://sec.gov/1", "", 0))
        con.execute("INSERT INTO filings VALUES(?,?,?,?,?,?,?,?,?,?)", ("a2", "1", "XYZ", "8-K", day(1), day(1), "2.02,9.01", "https://sec.gov/2", "", 0))
        ins = "INSERT INTO insider_txns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        con.execute(ins, ("f1", 0, "XYZ", "CEO A", "CEO", day(5), "P", "buy", 1000, 10, 10000, 1, 0, 0, 0, "https://sec.gov/f1"))
        con.execute(ins, ("f2", 0, "XYZ", "CFO B", "CFO", day(5), "P", "buy", 1000, 10, 10000, 1, 0, 1, 0, "https://sec.gov/f2"))   # 10b5-1 plan
        con.execute(ins, ("f3", 0, "XYZ", "Dir C", "Director", day(5), "M", "exercise", 1000, 1, 1000, 1, 0, 0, 1, "https://sec.gov/f3"))
        calendar._upsert(con, "earn:XYZ:x", "earnings", "XYZ", "t", "https://nasdaq.com", "nasdaq", NOW_MS + 3 * DAY * 1000, 0,
                         {"date": day(-3), "time": "after the close"})
        for k in range(8):                               # a burst of stories, but no collection history yet
            news_row(con, "XYZ", "XYZ Corp (XYZ) headline number %d about things" % k, NOW_MS - 3600000 * k, fetched=NOW_MS)
        c = catalysts.for_symbol(con, "XYZ", NOW_MS)
        self.assertEqual(c["parts"]["material_8k"], 20)
        self.assertEqual(c["parts"]["insider_buying"], 25)          # one real buyer; the plan trade and the exercise don't count
        self.assertEqual(c["parts"]["earnings_ahead"], 25)
        self.assertTrue(c["earnings_soon"] and c["earnings_recent"])
        self.assertEqual(c["parts"]["news_volume"], 0)              # no baseline yet, so no "spike"
        self.assertTrue(all(i["url"] for i in c["items"]))
        new = alerts.generate(con, catalyst_by_symbol={"XYZ": c}, now_ms=NOW_MS)
        kinds = sorted(a["kind"] for a in new)
        self.assertEqual(kinds, ["8k", "insider_buy"])           # the earnings 8-K and the plan trade don't alert
        self.assertEqual(alerts.generate(con, catalyst_by_symbol={"XYZ": c}, now_ms=NOW_MS), [])


class Grading(unittest.TestCase):
    def test_entry_bar_opening_beyond_target_or_stop_fills_at_open(self):
        f = {"open": [100, 120, 80], "high": [101, 121, 81], "low": [99, 119, 79], "close": [100, 120, 80], "sma5": [None] * 3}
        j, px, why = backtest.simulate_trade(f, 1, 120, 90, 110, 5)
        self.assertEqual((j, px, why), (1, 120, "target"))         # not 110: that would book a fake loss
        j, px, why = backtest.simulate_trade(f, 2, 80, 90, 130, 5)
        self.assertEqual((px, why), (80, "stop"))

    def test_grade_reads_only_bars_after_the_prediction(self):
        bars = walk(400, 3, drift=0.001)
        f_full = features(bars)
        row = {"data_as_of": bars[300][0] * 1000, "entry": bars[300][4], "stop": bars[300][4] * 0.9, "target": bars[300][4] * 1.1,
               "exit_rule": "target", "max_hold": 20, "horizon_days": 20}
        g = performance.grade_one(row, f_full, None)
        self.assertIsNotNone(g)
        self.assertGreater(g["entry_ts"], bars[300][0])
        # changing everything up to the prediction bar must not change the grade
        poisoned = [(b[0], b[1] * 3, b[2] * 3, b[3] * 3, b[4] * 3, b[5]) if i <= 300 else b for i, b in enumerate(bars)]
        g2 = performance.grade_one(row, features(poisoned), None)
        self.assertEqual((g["ret"], g["exit_reason"]), (g2["ret"], g2["exit_reason"]))
        # a prediction on the last bar can't be graded yet
        self.assertIsNone(performance.grade_one(dict(row, data_as_of=bars[-1][0] * 1000), f_full, None))

    def test_statistics(self):
        self.assertGreater(performance.binom_two_sided(50, 100, 0.5), 0.9)
        self.assertLess(performance.binom_two_sided(30, 100, 0.5), 0.001)
        rho, p = performance.spearman(list(range(50)), [x * 2 for x in range(50)])
        self.assertAlmostEqual(rho, 1.0)
        few = [{"ret": -0.01}] * 10
        self.assertEqual(performance.drift(few, 60)["verdict"], "too few to judge")
        bad = [{"ret": -0.01}] * 40 + [{"ret": 0.01}] * 10
        d = performance.drift(bad, 66)
        self.assertEqual(d["verdict"], "worse than backtest beyond chance")
        self.assertIn("Do not change parameters", d["action"])

    def test_replay_matches_backtest_rules_and_never_duplicates(self):
        con = mem()
        rows = walk(900, 11, drift=0.0006, vol=0.02)
        put_bars(con, "AAA", rows)
        put_bars(con, "SPY", walk(900, 12, drift=0.0004))
        bb = {"AAA": rows, "SPY": [tuple(r) for r in con.execute("SELECT ts,open,high,low,close,volume FROM bars WHERE symbol='SPY' ORDER BY ts")]}
        wf = {n: backtest.walk_forward(bb, n) for n in ("rsi2_reversion", "breakout", "trend_pullback")}
        n = performance.replay(con, wf, ["AAA"])
        self.assertGreater(n, 0)
        self.assertEqual(performance.replay(con, wf, ["AAA"]), 0)  # second run adds nothing
        performance.grade_all(con, "replay")
        # every graded replay call of a strategy equals that strategy's backtest trade from the same signal
        f = features(rows)
        r = con.execute("SELECT * FROM predictions WHERE source='replay' AND strategy='rsi2_reversion' AND outcome IS NOT NULL LIMIT 1").fetchone()
        if r:
            i = f["ts"].index(r["data_as_of"] // 1000)
            t = backtest.trades_for(f, "rsi2_reversion", wf["rsi2_reversion"]["params"], i, i + 1)[0]
            self.assertAlmostEqual(r["ret"], t["ret"], places=9)
        evidence = json.loads(con.execute("SELECT evidence FROM predictions WHERE source='replay' LIMIT 1").fetchone()[0])
        self.assertIn("walk_forward_validation", evidence)         # replay never sees test-window results
        self.assertIsNone(json.loads(con.execute("SELECT score_parts FROM predictions WHERE source='replay' LIMIT 1").fetchone()[0])["fundamentals"])


class AlertsAndAutopilot(unittest.TestCase):
    def card(self, **kw):
        c = {"symbol": "AAPL", "strategy": "rsi2_reversion", "strategy_label": "x", "score": 60, "signal_time": 1, "signal_age_days": 0,
             "price": 100.0, "stop": 95.0, "targets": [101.0], "confidence": "medium", "exit_rule": "sma5", "params": {}, "catalyst": {}}
        c.update(kw)
        return c

    def test_alerts_deduplicate(self):
        con = mem()
        c = self.card()
        self.assertEqual(len(alerts.generate(con, [c])), 1)
        self.assertEqual(len(alerts.generate(con, [c])), 0)
        self.assertEqual(len(alerts.generate(con, [self.card(signal_time=2)])), 1)

    def test_autopilot_rules_and_idempotence(self):
        con = mem()
        put_quote(con, "AAPL", 100.0)
        put_bars(con, "AAPL", walk(100, 1))
        br = paper.PaperBroker(con, start_cash=10000)
        wf = {"rsi2_reversion": {"verdict": "pass"}, "trend_pullback": {"verdict": "fail"}}
        cards = [self.card(), self.card(symbol="MSFT", strategy="trend_pullback"), self.card(symbol="NVDA", signal_age_days=2),
                 self.card(symbol="CSCO", catalyst={"earnings_soon": True})]
        orders, skipped = autopilot.run(con, br, cards, wf)
        self.assertEqual([o["symbol"] for o in orders], ["AAPL"])
        self.assertEqual(orders[0]["status"], "filled")
        self.assertEqual({s for s, _ in skipped}, {"MSFT", "NVDA", "CSCO"})
        self.assertIsNone(orders[0]["target_price"])                # sma5-exit strategy: no fixed target, like the backtest
        again, _ = autopilot.run(con, br, cards, wf)
        self.assertEqual(again[0]["id"], orders[0]["id"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM orders WHERE status='filled'").fetchone()[0], 1)

    def test_paper_time_limit_exit(self):
        con = mem()
        bars = walk(60, 5, drift=0.0, vol=0.001)
        put_bars(con, "AAPL", bars)
        put_quote(con, "AAPL", bars[-1][4])
        br = paper.PaperBroker(con, start_cash=10000)
        o = br.submit("AAPL", "buy", 1, stop=bars[-1][4] * 0.5, client_id="t", strategy="breakout")
        con.execute("UPDATE positions SET opened_at=? WHERE symbol='AAPL'", (bars[20][0] * 1000,))
        self.assertEqual(o["status"], "filled")
        ex = br.check_exits()
        self.assertEqual(len(ex), 1)
        self.assertIn("time limit", ex[0]["why"])


class Storage(unittest.TestCase):
    def test_old_database_is_migrated(self):
        import sqlite3
        p = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, symbol TEXT, strategy TEXT, score REAL, "
                  "direction TEXT, entry REAL, stop REAL, target REAL, horizon_days INTEGER, data_as_of INTEGER, evidence TEXT, outcome TEXT, "
                  "resolved_at INTEGER, ret REAL, mfe REAL, mae REAL)")
        c.execute("INSERT INTO predictions(symbol) VALUES('OLD')")
        c.commit()
        c.close()
        con = db.connect(p)
        cols = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
        self.assertTrue({"signal_ts", "source", "r_mult", "bench_ret"} <= cols)
        self.assertEqual(con.execute("SELECT symbol, source FROM predictions").fetchone()[:], ("OLD", "live"))

    def test_scanner_records_each_setup_once(self):
        con = mem()
        c = {"symbol": "A", "strategy": "s", "score": 50, "price": 10, "stop": 9, "targets": [11], "bar_time": 1, "evidence": {},
             "signal_time": 1, "regime": "mixed", "score_parts": {}, "max_hold": 10, "exit_rule": "target"}
        self.assertTrue(scanner.record(con, c))
        self.assertFalse(scanner.record(con, dict(c, bar_time=2)))  # same setup seen again the next day


class BriefingAndDashboard(unittest.TestCase):
    def test_briefing_on_empty_database(self):
        con = mem()
        md = briefing.render_md(briefing.gather(con, cards=[], symbols=["AAPL"]))
        self.assertIn("Nothing to buy is a valid answer", md)
        self.assertIn("Paper trading only", md)

    def test_state_cannot_break_out_of_script_tag(self):
        html, nonce = dashboard.page({"x": "</script><script>alert(1)</script><!--"})
        body = html.split('<script id="state" type="application/json">')[1].split("</script>")[0]
        self.assertNotIn("<", body)
        self.assertIn("\\u003c/script", body)
        self.assertIn("nonce-" + nonce, dashboard.csp(nonce))
        self.assertNotIn("innerHTML", html)                          # all values go in as text

    def test_server_is_read_only_and_local(self):
        self.assertEqual(dashboard.HOST, "127.0.0.1")
        os.environ["PWS_DATA_DIR"] = config.DATA_DIR.as_posix()
        srv = dashboard.http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            r = urllib.request.urlopen(base + "/", timeout=10)
            self.assertIn("script-src 'nonce-", r.headers["Content-Security-Policy"])
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            st = json.loads(urllib.request.urlopen(base + "/api/state", timeout=30).read())
            self.assertTrue(st["paper_only"])
            for method in ("POST", "PUT", "DELETE"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(urllib.request.Request(base + "/api/state", data=b"x", method=method), timeout=10)
                self.assertEqual(e.exception.code, 405)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/../wallstreet.db", timeout=10)
            self.assertEqual(e.exception.code, 404)
        finally:
            srv.shutdown()


class AuditFixes(unittest.TestCase):
    def test_schwab_login_requires_matching_state_and_masks_accounts(self):
        from wallstreet.broker import schwab
        os.environ.update(SCHWAB_APP_KEY="k", SCHWAB_APP_SECRET="s")
        try:
            url = schwab.login_url()
            self.assertIn("state=", url)
            with self.assertRaises(schwab.SchwabDisabled):
                schwab.finish_login("https://127.0.0.1/?code=abc&state=forged")
            self.assertEqual(schwab.mask("12345678"), "****5678")
            self.assertEqual(oct(schwab.STATE_FILE.stat().st_mode & 0o777), "0o600")
        finally:
            os.environ.pop("SCHWAB_APP_KEY")
            os.environ.pop("SCHWAB_APP_SECRET")

    def test_bad_quote_is_refused(self):
        con = mem()
        put_bars(con, "AAPL", walk(30, 2, start=100))
        last = con.execute("SELECT close FROM bars ORDER BY ts DESC LIMIT 1").fetchone()[0]
        put_quote(con, "AAPL", last * 0.4)                # a 60% "drop" that is really a bad tick
        with self.assertRaises(paper.OrderError):
            paper.PaperBroker(con, start_cash=10000).submit("AAPL", "buy", 1, stop=last * 0.3)

    def test_inconsistent_bars_are_dropped(self):
        from unittest import mock
        from wallstreet.data import prices
        fake = {"chart": {"result": [{"timestamp": [1, 2], "meta": {}, "indicators": {"quote": [{
            "open": [10, 10], "high": [11, 9], "low": [9, 9], "close": [10.5, 10], "volume": [1, 1]}]}}]}}
        with mock.patch.object(prices, "get", return_value=fake):
            self.assertEqual(len(prices.fetch_chart("X")["bars"]), 1)   # second bar's high is below its open

    def test_daily_runs_cannot_overlap(self):
        import fcntl
        from wallstreet import cli
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.DATA_DIR / "daily.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(cli.cmd_daily(mem(), quiet=True), [])
            fcntl.flock(held, fcntl.LOCK_UN)


if __name__ == "__main__":
    unittest.main()
