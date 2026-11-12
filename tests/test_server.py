"""Server-side tests: approvals, the Telegram bot (against a fake Telegram API), the schedule, dashboard keys, stats
publishing (against a fake GitHub API). Offline."""
import base64
import datetime as dt
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("PWS_DATA_DIR", tempfile.mkdtemp())

from tests.test_core import mem, put_bars, put_quote, walk  # noqa: E402
from wallstreet import alerts, approvals, autopilot, dashboard, db, service, stats  # noqa: E402
from wallstreet import telegram as tg  # noqa: E402
from wallstreet.broker.paper import PaperBroker  # noqa: E402

CHAT = "4242"


class FakeAPI(http.server.BaseHTTPRequestHandler):
    """Records every call; replies like Telegram or GitHub would."""
    calls = []
    store = {}

    def log_message(self, *a):
        pass

    def _reply(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        method = self.path.rsplit("/", 1)[-1]
        FakeAPI.calls.append((method, body))
        if method == "sendMessage":
            return self._reply({"ok": True, "result": {"message_id": len(FakeAPI.calls)}})
        if method == "getUpdates":
            return self._reply({"ok": True, "result": []})
        return self._reply({"ok": True, "result": True})

    def do_GET(self):                                     # GitHub contents API
        f = FakeAPI.store.get(self.path)
        FakeAPI.calls.append(("GET", self.path))
        return self._reply(f) if f else self._reply({"message": "Not Found"}, 404)

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.calls.append(("PUT", self.path, self.headers.get("Authorization")))
        FakeAPI.store[self.path] = {"content": body["content"], "sha": "sha%d" % len(FakeAPI.calls)}
        return self._reply({"content": {"sha": "x"}})


def start_fake():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def card(sym="AAPL", price=100.0, **kw):
    c = {"symbol": sym, "strategy": "rsi2_reversion", "strategy_label": "Oversold bounce", "score": 61, "signal_time": 7,
         "signal_age_days": 0, "price": price, "stop": price * 0.95, "targets": [price * 1.02], "confidence": "medium",
         "exit_rule": "sma5", "params": {}, "catalyst": {}, "technical": ["Trend: up"], "fail_reasons": ["Stop can gap"]}
    c.update(kw)
    return c


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.api = start_fake()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeAPI.calls.clear()
        FakeAPI.store.clear()
        self.con = mem()
        put_bars(self.con, "AAPL", walk(60, 1, start=100, vol=0.001))
        self.last = self.con.execute("SELECT close FROM bars WHERE symbol='AAPL' ORDER BY ts DESC LIMIT 1").fetchone()[0]
        put_quote(self.con, "AAPL", self.last)
        self.bot = tg.Bot("123:TESTTOKEN", CHAT, self.api)
        self.cmds = tg.Commands(self.con, self.bot)

    def sent(self, method="sendMessage"):
        return [c[1] for c in FakeAPI.calls if c[0] == method]

    def update_msg(self, text, chat=CHAT):
        return {"update_id": 1, "message": {"chat": {"id": int(chat), "type": "private"}, "text": text}}

    def update_btn(self, data, chat=CHAT, mid=99):
        return {"update_id": 2, "callback_query": {"id": "cb1", "data": data, "message": {"message_id": mid, "chat": {"id": int(chat)}}}}


class Approvals(Base):
    def propose(self, **kw):
        res, _ = autopilot.run(self.con, PaperBroker(self.con, 10000), [card(price=self.last, **kw)], {"rsi2_reversion": {"verdict": "pass"}}, "approve")
        return res[0]

    def test_approve_mode_places_nothing_until_approved(self):
        p = self.propose()
        self.assertEqual(p["kind"], "proposal")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        tg.push(self.con, self.bot)
        msg = self.sent()[-1]
        self.assertIn("PROPOSED PAPER BUY", msg["text"])
        btn = msg["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in btn], ["✅ Approve", "❌ Reject"])
        tg.handle_update(self.con, self.bot, self.cmds, self.update_btn(btn[0]["callback_data"]), refresh=lambda s: None)
        row = approvals.get(self.con, p["id"])
        self.assertEqual(row["status"], "filled", row["note"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders WHERE status='filled'").fetchone()[0], 1)
        self.assertTrue(self.sent("editMessageText"))
        # double tap: no second order
        tg.handle_update(self.con, self.bot, self.cmds, self.update_btn(btn[0]["callback_data"]), refresh=lambda s: None)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        self.assertIn("Already filled", self.sent("answerCallbackQuery")[-1]["text"])
        # proposing the same setup again does nothing
        res, _ = autopilot.run(self.con, PaperBroker(self.con), [card(price=self.last)], {"rsi2_reversion": {"verdict": "pass"}}, "approve")
        self.assertEqual(res, [])

    def test_buttons_from_other_chats_and_forged_tokens_do_nothing(self):
        p = self.propose()
        tg.handle_update(self.con, self.bot, self.cmds, self.update_btn("ap:%d:%s" % (p["id"], p["token"]), chat="999"), refresh=lambda s: None)
        tg.handle_update(self.con, self.bot, self.cmds, self.update_btn("ap:%d:wrongtok" % p["id"]), refresh=lambda s: None)
        self.assertEqual(approvals.get(self.con, p["id"])["status"], "pending")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_price_moved_or_expired_means_no_order(self):
        p = self.propose()
        r, changed = approvals.approve(self.con, PaperBroker(self.con), p["id"], p["token"], refresh=lambda s: put_quote(self.con, s, self.last * 1.05, time.time() + 60))
        self.assertTrue(changed)
        self.assertEqual(r["status"], "stale")
        self.assertIn("moved", r["note"])
        p2 = self.propose(signal_time=8)
        r2, _ = approvals.approve(self.con, PaperBroker(self.con), p2["id"], p2["token"], refresh=lambda s: None, now_ms=p2["expires_at"] + 1)
        self.assertEqual(r2["status"], "expired")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_earnings_soon_blocks_approval(self):
        from wallstreet.data import calendar
        p = self.propose()
        calendar._upsert(self.con, "earn:AAPL:x", "earnings", "AAPL", "t", "u", "s", db.now_ms() + 86400000, 0,
                         {"date": time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400)), "time": "after the close"})
        r, _ = approvals.approve(self.con, PaperBroker(self.con), p["id"], p["token"], refresh=lambda s: None)
        self.assertEqual(r["status"], "refused")
        self.assertIn("earnings", r["note"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_reject_and_expiry_time(self):
        p = self.propose()
        tg.handle_update(self.con, self.bot, self.cmds, self.update_btn("rj:%d:%s" % (p["id"], p["token"])))
        self.assertEqual(approvals.get(self.con, p["id"])["status"], "rejected")
        fri = dt.datetime(2026, 11, 13, 17, 0, tzinfo=approvals.ET)          # Friday evening -> Monday 4 PM
        exp = dt.datetime.fromtimestamp(approvals.expiry_for(fri) / 1000, approvals.ET)
        self.assertEqual((exp.weekday(), exp.hour), (0, 16))

    def test_auto_mode_and_off_mode(self):
        res, _ = autopilot.run(self.con, PaperBroker(self.con, 10000), [card(price=self.last)], {"rsi2_reversion": {"verdict": "pass"}}, "auto")
        self.assertEqual(res[0]["kind"], "order")
        res, skipped = autopilot.run(self.con, PaperBroker(self.con), [card(price=self.last, signal_time=9)], {"rsi2_reversion": {"verdict": "pass"}}, "off")
        self.assertEqual((res, skipped[0][1]), ([], "autopilot off"))


class Bot(Base):
    def test_only_the_owner_is_answered(self):
        tg.handle_update(self.con, self.bot, self.cmds, self.update_msg("/status", chat="777"))
        self.assertEqual(self.sent(), [])
        tg.handle_update(self.con, self.bot, self.cmds, self.update_msg("/status"))
        self.assertIn("Paper account", self.sent()[-1]["text"])

    def test_commands(self):
        for c in ("/help", "/positions", "/orders", "/scan", "/calendar", "/alerts", "/mode", "/calls", "/strategies", "/brief"):
            out = self.cmds.handle(c)
            self.assertTrue(out and "Unknown" not in out, c)
        self.assertIn("auto", self.cmds.handle("/mode auto"))
        self.assertEqual(autopilot.mode(self.con), "auto")
        self.assertIn("⚠️", self.cmds.handle("/mode yolo"))
        self.assertIn("Paper bought", self.cmds.handle("/buy AAPL 1 %.2f" % (self.last * 0.95)))
        self.assertIn("Paper sold", self.cmds.handle("/sell AAPL"))
        self.assertIn("Refused", self.cmds.handle("/buy AAPL 1000 %.2f" % (self.last * 0.95)))
        self.assertIn("Unknown command", self.cmds.handle("/rm -rf"))

    def test_text_is_escaped_and_long_messages_split(self):
        alerts.add(self.con, "x", 1, "info", "<script>alert(1)</script> & co", url="javascript:alert(1)")
        tg.push(self.con, self.bot)
        t = self.sent()[-1]["text"]
        self.assertIn("&lt;script&gt;", t)
        self.assertNotIn("javascript:", t)
        self.assertEqual(self.con.execute("SELECT delivered FROM alerts").fetchone()[0], 1)
        tg.push(self.con, self.bot)                       # delivered once only
        self.assertEqual(len(self.sent()), 1)
        parts = tg._chunks("line\n" * 2000)
        self.assertTrue(len(parts) > 1 and all(len(p) <= 3900 for p in parts))

    def test_long_poll_and_setup_chat(self):
        self.assertEqual(self.bot.updates(timeout=0), [])
        req = [c[1] for c in FakeAPI.calls if c[0] == "getUpdates"][-1]
        self.assertEqual(req["timeout"], 0)                 # Telegram's own long-poll parameter reaches the API
        self.assertIsNone(tg.setup_chat("123:TESTTOKEN", self.api))

    def test_errors_never_show_the_token(self):
        bad = tg.Bot("123:SECRETTOKEN", CHAT, "http://127.0.0.1:1")
        with self.assertRaises(RuntimeError) as e:
            bad.send("hi")
        self.assertNotIn("SECRETTOKEN", str(e.exception))


class Schedule(unittest.TestCase):
    def test_daily_and_market_hours(self):
        et = service.ET
        thu_afternoon = dt.datetime(2026, 11, 12, 16, 40, tzinfo=et)
        self.assertTrue(service.daily_due(thu_afternoon, "2026-11-11"))
        self.assertFalse(service.daily_due(thu_afternoon, "2026-11-12"))
        self.assertFalse(service.daily_due(dt.datetime(2026, 11, 12, 16, 0, tzinfo=et), None))
        self.assertFalse(service.daily_due(dt.datetime(2026, 11, 14, 17, 0, tzinfo=et), None))     # Saturday
        self.assertTrue(service.market_open(dt.datetime(2026, 11, 12, 11, 0, tzinfo=et)))
        self.assertFalse(service.market_open(dt.datetime(2026, 11, 12, 18, 0, tzinfo=et)))

    def test_intraday_stop_exit(self):
        con = mem()
        put_bars(con, "AAPL", walk(30, 3, start=100, vol=0.001))
        last = con.execute("SELECT close FROM bars ORDER BY ts DESC LIMIT 1").fetchone()[0]
        put_quote(con, "AAPL", last)
        br = PaperBroker(con, 10000)
        br.submit("AAPL", "buy", 1, stop=last * 0.97, strategy="manual")
        put_quote(con, "AAPL", last * 0.96, time.time() + 5)
        orig = service.prices.refresh
        service.prices.refresh = lambda *a, **k: None
        try:
            ex = service.intraday(con)
        finally:
            service.prices.refresh = orig
        self.assertEqual(len(ex), 1)
        self.assertIn("stop hit", ex[0]["why"])


class Dashboard(unittest.TestCase):
    def test_key_required(self):
        with self.assertRaises(SystemExit):
            dashboard.make_server("0.0.0.0", 0, key="")
        srv = dashboard.make_server("127.0.0.1", 0, key="k3y")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            for path in ("/", "/api/state", "/?k=wrong"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + path, timeout=10)
                self.assertEqual(e.exception.code, 403)
            self.assertEqual(urllib.request.urlopen(base + "/?k=k3y", timeout=10).status, 200)
            self.assertTrue(json.loads(urllib.request.urlopen(base + "/api/state?k=k3y", timeout=30).read())["paper_only"])
        finally:
            srv.shutdown()


class Stats(Base):
    def test_publish_writes_once_and_never_leaks(self):
        old = stats.API
        stats.API = self.api
        os.environ["PWS_STATS_TOKEN"] = "ghp_TESTTOKEN123"
        try:
            n = stats.publish(self.con, "me/ws-stats", "ghp_TESTTOKEN123")
            self.assertEqual(n, 4)
            puts = [c for c in FakeAPI.calls if c[0] == "PUT"]
            self.assertEqual(puts[0][2], "Bearer ghp_TESTTOKEN123")
            body = base64.b64decode(FakeAPI.store["/repos/me/ws-stats/contents/latest.md"]["content"]).decode()
            self.assertIn("Daily briefing", body)
            self.assertNotIn("ghp_TESTTOKEN123", "".join(base64.b64decode(v["content"]).decode() for v in FakeAPI.store.values()))
            before = len([c for c in FakeAPI.calls if c[0] == "PUT"])
            stats.publish(self.con, "me/ws-stats", "ghp_TESTTOKEN123")
            again = [c[1] for c in FakeAPI.calls if c[0] == "PUT"][before:]
            self.assertEqual(again, ["/repos/me/ws-stats/contents/latest.json"])   # only the timestamped file; unchanged files skipped
            alerts.add(self.con, "oops", 1, "info", "token ghp_TESTTOKEN123 pasted somewhere")
            with self.assertRaises(RuntimeError):
                stats.files(self.con)
        finally:
            stats.API = old
            os.environ.pop("PWS_STATS_TOKEN")


if __name__ == "__main__":
    unittest.main()
