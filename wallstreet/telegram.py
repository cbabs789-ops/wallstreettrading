"""Telegram bot: commands, alerts, and Approve / Reject buttons for every proposed trade.

Only messages from TELEGRAM_CHAT_ID are obeyed; anything else is ignored and logged. The token lives only in the
server's env file. Plain HTTPS calls to the Bot API (long polling), no third-party library.
"""
import html
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from . import alerts, approvals, autopilot, briefing, catalysts, config, db, performance, risk
from .broker.paper import OrderError, PaperBroker
from .data import calendar

API = config.env("PWS_TELEGRAM_API", "https://api.telegram.org")
log = logging.getLogger("pws.telegram")
E = html.escape


def money(x):
    return "%s$%s" % ("-" if x < 0 else "", format(abs(x), ",.2f"))


def pct(x):
    return "%+.2f%%" % x


def qty(q):
    return ("%.4f" % q).rstrip("0").rstrip(".")


class Bot:
    def __init__(self, token=None, chat_id=None, api=None):
        self.token = token or config.env("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(chat_id or config.env("TELEGRAM_CHAT_ID") or "")
        self.api = api or API
        self.offset = None

    @property
    def ready(self):
        return bool(self.token and self.chat_id)

    def call(self, method, http_timeout=35, **params):
        req = urllib.request.Request("%s/bot%s/%s" % (self.api, self.token, method), data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:              # never log the URL: it contains the token
            body = e.read()[:300].decode("utf-8", "replace")
            raise RuntimeError("Telegram %s failed: HTTP %s %s" % (method, e.code, body)) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise RuntimeError("Telegram %s failed: %s" % (method, type(e).__name__)) from None
        if not out.get("ok"):
            raise RuntimeError("Telegram %s: %s" % (method, out.get("description")))
        return out.get("result")

    def send(self, text, buttons=None, chat_id=None):
        """HTML message, split under Telegram's 4096-character cap. Returns the last message id."""
        mid = None
        parts = _chunks(text)
        for i, part in enumerate(parts):
            p = {"chat_id": chat_id or self.chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True}
            if buttons and i == len(parts) - 1:
                p["reply_markup"] = {"inline_keyboard": buttons}
            mid = self.call("sendMessage", **p)["message_id"]
        return mid

    def edit(self, message_id, text):
        try:
            self.call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text[:4000], parse_mode="HTML",
                      disable_web_page_preview=True)
        except RuntimeError as e:
            log.info("edit failed: %s", e)

    def answer(self, callback_id, text):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except RuntimeError as e:
            log.info("answer failed: %s", e)

    def updates(self, timeout=25):
        p = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if self.offset is not None:
            p["offset"] = self.offset
        res = self.call("getUpdates", http_timeout=timeout + 10, **p)
        if res:
            self.offset = res[-1]["update_id"] + 1
        return res


def _chunks(text, limit=3900):
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        out.append(cur)
    return out or [""]


# ---------- message formats ----------

def fmt_proposal(p):
    c = p["card"]
    risk_pct = (p["price"] - p["stop"]) / p["price"] * 100
    lines = ["🟦 <b>PROPOSED PAPER BUY: %s</b>" % E(p["symbol"]),
             "%s · score %s/100" % (E(c.get("strategy_label") or p["strategy"]), c.get("score")),
             "Price %s · stop %s (%.1f%% below)%s" % (money(p["price"]), money(p["stop"]), risk_pct,
                                                      " · target %s" % money(p["target"]) if p["target"] else " · exits on a close above its 5-day average"),
             "Size about %s shares (re-sized at approval, max 1%% of the account at risk)" % qty(p["qty"]),
             "Confidence: %s" % E(c.get("confidence") or "n/a")]
    for t in (c.get("technical") or [])[:3]:
        lines.append("· " + E(t))
    risks = c.get("fail_reasons") or []
    if risks:
        lines.append("<b>Could fail because:</b> " + E("; ".join(risks[:3])))
    lines.append("Expires %s. Approving re-checks the price (refused if it moved %.0f%%+) and every risk limit." % (
        time.strftime("%a %b %d, 4:00 PM ET", time.localtime(p["expires_at"] / 1000)), approvals.MAX_DRIFT * 100))
    return "\n".join(lines)


def proposal_buttons(p):
    return [[{"text": "✅ Approve", "callback_data": "ap:%d:%s" % (p["id"], p["token"])},
             {"text": "❌ Reject", "callback_data": "rj:%d:%s" % (p["id"], p["token"])}]]


def fmt_decided(p):
    icon = {"filled": "✅", "rejected": "❌", "expired": "⌛", "stale": "⚠️", "failed": "⚠️", "refused": "🛑"}.get(p["status"], "•")
    return "%s <b>%s %s</b>: %s" % (icon, E(p["symbol"]), E(p["status"].upper()), E(p["note"] or ""))


def fmt_alert(a):
    icon = {"critical": "🚨", "action": "🟠", "watch": "👀", "info": "ℹ️"}.get(a["severity"], "•")
    url = a.get("url")
    link = ' <a href="%s">source</a>' % E(url, quote=True) if url and url.startswith(("http://", "https://")) else ""
    return "%s %s%s" % (icon, E(a["title"]), link)


# ---------- commands ----------

HELP = """<b>Wall Street bot</b> (paper trading only)
/status: account, mode, data freshness
/positions: open positions with P&amp;L and stops
/orders: last 10 orders
/pending: trades waiting for your approval
/scan: latest setups
/brief: today's market briefing
/calendar: earnings and economic events ahead
/catalysts: what's moving and why
/calls: how the platform's calls have done
/strategies: backtest verdicts
/alerts: recent alerts
/mode [approve|auto|off]: how the autopilot trades
/buy SYM QTY STOP [TARGET]: manual paper buy (risk-checked)
/sell SYM [QTY]: manual paper sell (all if no QTY)
/run: run the daily pipeline now
/site: dashboard link"""


class Commands:
    def __init__(self, con, bot, run_daily=None):
        self.con, self.bot, self.run_daily = con, bot, run_daily

    def handle(self, text):
        parts = text.strip().split()
        if not parts:
            return None
        cmd = parts[0].split("@")[0].lower()
        fn = getattr(self, "c_" + cmd.lstrip("/"), None)
        if not cmd.startswith("/") or not fn:
            return "Unknown command. /help lists them."
        try:
            return fn(parts[1:])
        except (OrderError, ValueError) as e:
            return "⚠️ " + E(str(e))

    def c_start(self, a):
        return HELP

    c_help = c_start

    def c_status(self, a):
        st = PaperBroker(self.con).state()
        g_last = db.kv_get(self.con, "daily_last") or {}
        q = self.con.execute("SELECT MAX(quote_time) FROM quotes WHERE symbol='SPY'").fetchone()[0]
        pend = len(approvals.pending(self.con))
        return "\n".join([
            "<b>Paper account</b> %s · total %s · today %s · drawdown %.2f%% (kill switch %.0f%%)" % (
                money(st["equity"]), pct(st["total_return_pct"]), pct(st["day_pnl_pct"]), st["drawdown_pct"], config.RISK["max_drawdown_pct"]),
            "Cash %s · %d/%d positions · %d waiting for approval" % (money(st["cash"]), len(st["positions"]), config.RISK["max_open_positions"], pend),
            "Autopilot mode: <b>%s</b>" % autopilot.mode(self.con),
            "Last daily run: %s" % (time.strftime("%a %b %d %H:%M", time.localtime(g_last["at"])) if g_last.get("at") else "not yet"),
            "Latest price data: %s (Yahoo, delayed)" % (time.strftime("%a %b %d %H:%M", time.localtime(q)) if q else "none")])

    def c_positions(self, a):
        st = PaperBroker(self.con).state()
        if not st["positions"]:
            return "No open positions."
        out = ["<b>Positions</b>"]
        for p in st["positions"]:
            en = catalysts.for_symbol(self.con, p["symbol"]).get("earnings_next")
            out.append("%s %s sh @ %s · last %s · %s (%s) · stop %s%s%s" % (
                E(p["symbol"]), qty(p["qty"]), money(p["avg_price"]), money(p["last"]), money(p["pnl"]), pct(p["pnl_pct"]),
                money(p["stop_price"]) if p["stop_price"] else "none",
                " · target %s" % money(p["target_price"]) if p["target_price"] else "",
                " · ⚠️ earnings %s" % en["date"] if en and en["days"] <= 7 else ""))
        return "\n".join(out)

    def c_orders(self, a):
        rows = self.con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            return "No orders yet."
        return "<b>Last orders</b>\n" + "\n".join("%s %s %s %s %s%s" % (
            time.strftime("%b %d %H:%M", time.localtime(r["created_at"] / 1000)), r["side"], E(r["symbol"]), qty(r["qty"]),
            r["status"], " @ %s" % money(r["fill_price"]) if r["fill_price"] else " (%s)" % E((r["reason"] or "")[:80])) for r in rows)

    def c_pending(self, a):
        ps = approvals.pending(self.con)
        if not ps:
            return "Nothing waiting for approval."
        for p in ps:
            mid = self.bot.send(fmt_proposal(p), proposal_buttons(p))
            approvals.set_message(self.con, p["id"], mid)
        return None

    def c_scan(self, a):
        cards = db.kv_get(self.con, "last_scan") or []
        if not cards:
            return "No setups on the last scan. Nothing to buy is a valid answer."
        return "<b>Latest setups</b>\n" + "\n".join("%s · %d/100 · %s · stop %s · %s" % (
            E(c["symbol"]), c["score"], E(c["strategy_label"]), money(c["stop"]), E(c["confidence"])) for c in cards[:8])

    def c_brief(self, a):
        g = briefing.gather(self.con)
        rg = g["regime"] or {}
        lines = ["<b>Briefing</b> (Yahoo prices, delayed)"]
        if rg:
            lines.append("Market: <b>%s</b> · SPY %.2f vs 50-day %.2f / 200-day %.2f · VIX %s" % (
                rg["trend"], rg["spy"], rg["sma50"], rg["sma200"], "%.1f" % rg["vix"] if rg.get("vix") else "n/a"))
        for x in g["indexes"]:
            lines.append("%s %.2f (%s)" % (x["symbol"], x["price"], pct(x["change_pct"])))
        ms = (g["macro"] or {}).get("series", {})
        bits = ["%s %s" % (k, "%.2f" % (v.get("yoy_pct", v["value"]))) for k, v in ms.items() if k in ("FEDFUNDS", "DGS10", "UNRATE")]
        if "CPIAUCSL" in ms:
            bits.append("CPI %.2f%% y/y" % ms["CPIAUCSL"]["yoy_pct"])
        if bits:
            lines.append("Economy (FRED): " + " · ".join(bits))
        lines.append("")
        lines.append(self.c_calendar([]))
        lines.append("")
        lines.append(self.c_scan([]))
        return "\n".join(lines)

    def c_calendar(self, a):
        ev = sorted([(e["detail"].get("date"), "%s (%s)" % (e["title"], e["detail"].get("time", ""))) for e in calendar.upcoming(self.con, "econ", 10)] +
                    [(e["detail"].get("date"), "%s earnings, %s" % (e["symbol"], e["detail"].get("time"))) for e in calendar.upcoming(self.con, "earnings", 14)])
        return "<b>Coming up</b>\n" + ("\n".join("%s %s" % (d, E(t)) for d, t in ev[:14]) or "Nothing scheduled that we track.")

    def c_catalysts(self, a):
        syms = [s for s in config.UNIVERSE if risk.sector(s) != "Index"]
        out = ["<b>Catalysts</b> (activity, not direction; headlines unconfirmed)"]
        for c in catalysts.rank(self.con, syms)[:8]:
            if not c["score"]:
                continue
            top = c["items"][0] if c["items"] else None
            out.append("%s %d/100%s" % (E(c["symbol"]), c["score"], ": " + E(top["text"][:100]) if top else ""))
        return "\n".join(out)

    def c_calls(self, a):
        wf = briefing.wf_results(self.con)
        out = []
        for src, label in (("live", "Live calls"), ("replay", "Replay of the test window")):
            r = performance.report(self.con, src, wf)
            o = r["overall"]
            if not o.get("n"):
                out.append("<b>%s</b>: none graded yet · %d open" % (label, r["open"]))
                continue
            out.append("<b>%s</b>: %d graded · win %.0f%% · avg %+.2f%% · SPY same days %s · %d open" % (
                label, o["n"], o["win_rate"], o["avg_ret"], "%+.2f%%" % o["avg_bench"] if o["avg_bench"] is not None else "n/a", r["open"]))
            for s, d in r["by_strategy"].items():
                out.append("· %s: n=%d, win %.0f%%, %s" % (E(s), d["stats"]["n"], d["stats"]["win_rate"], E(d["drift"]["verdict"])))
        r = performance.report(self.con, "replay", wf)
        out.append(E(r["score_vs_result"]["reading"]))
        return "\n".join(out)

    def c_strategies(self, a):
        out = ["<b>Walk-forward test window</b> (never used for fitting)"]
        for s in briefing.gather(self.con, cards=[], symbols=["SPY"])["strategies"]:
            t = s.get("test") or {}
            b = s.get("bench_test") or {}
            out.append("%s: <b>%s</b> · %d trades · win %.0f%% · return %+.0f%% · worst %.0f%% · SPY %+.0f%%" % (
                E(s["label"]), (s["verdict"] or "untested").upper(), t.get("trades", 0), t.get("win_rate", 0), t.get("total_return", 0),
                t.get("max_drawdown", 0), b.get("total_return", 0)))
        return "\n".join(out)

    def c_alerts(self, a):
        rs = alerts.recent(self.con, 7, 10)
        return "<b>Alerts, 7 days</b>\n" + ("\n".join(fmt_alert(x) for x in rs) or "None.")

    def c_mode(self, a):
        if a:
            autopilot.set_mode(self.con, a[0].lower())
        m = autopilot.mode(self.con)
        expl = {"approve": "every setup waits for your Approve tap", "auto": "paper orders are placed automatically",
                "off": "setups are reported, nothing is traded"}[m]
        return "Autopilot mode: <b>%s</b> (%s). Paper trading only either way." % (m, expl)

    def c_buy(self, a):
        if len(a) < 3:
            return "Usage: /buy SYM QTY STOP [TARGET], e.g. /buy CSCO 5 95"
        o = PaperBroker(self.con).submit(a[0].upper(), "buy", float(a[1]), stop=float(a[2]), target=float(a[3]) if len(a) > 3 else None,
                                         strategy="manual")
        return ("✅ Paper bought %s %s at %s" % (qty(o["qty"]), E(o["symbol"]), money(o["fill_price"])) if o["status"] == "filled"
                else "🛑 Refused: " + E(o["reason"]))

    def c_sell(self, a):
        if not a:
            return "Usage: /sell SYM [QTY]"
        br = PaperBroker(self.con)
        held = next((p for p in br.positions() if p["symbol"] == a[0].upper()), None)
        if not held:
            return "You don't hold %s." % E(a[0].upper())
        o = br.submit(held["symbol"], "sell", float(a[1]) if len(a) > 1 else held["qty"], strategy=held["strategy"])
        return ("✅ Paper sold %s %s at %s" % (qty(o["qty"]), E(o["symbol"]), money(o["fill_price"])) if o["status"] == "filled"
                else "🛑 Refused: " + E(o["reason"]))

    def c_run(self, a):
        if not self.run_daily:
            return "Not available here."
        threading.Thread(target=self.run_daily, daemon=True).start()
        return "Running the daily pipeline now (about 2 minutes). You'll get the summary when it's done."

    def c_site(self, a):
        url = config.env("PWS_PUBLIC_URL")
        key = config.env("PWS_DASH_KEY")
        if not url:
            return "Dashboard address isn't set (PWS_PUBLIC_URL)."
        return "Dashboard: %s/?k=%s" % (E(url.rstrip("/")), E(key or ""))


# ---------- the loop ----------

def handle_update(con, bot, cmds, u, broker_factory=PaperBroker, refresh=None):
    """Process one Telegram update. Anything not from the owner's chat is ignored."""
    if "callback_query" in u:
        cq = u["callback_query"]
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
        if chat != bot.chat_id:
            log.warning("ignored button press from chat %s", chat)
            return
        try:
            kind, pid, token = cq.get("data", "").split(":")
            pid = int(pid)
        except ValueError:
            return bot.answer(cq["id"], "Unknown button")
        if kind == "ap":
            p, changed = approvals.approve(con, broker_factory(con), pid, token, refresh=refresh)
        elif kind == "rj":
            p, changed = approvals.reject(con, pid, token)
        else:
            return bot.answer(cq["id"], "Unknown button")
        if not p:
            return bot.answer(cq["id"], "Not found")
        bot.answer(cq["id"], p["status"] if changed else "Already %s" % p["status"])
        if changed and cq.get("message"):
            bot.edit(cq["message"]["message_id"], fmt_proposal(p) + "\n\n" + fmt_decided(p))
        return
    m = u.get("message") or {}
    chat = str((m.get("chat") or {}).get("id"))
    if chat != bot.chat_id:
        if m:
            log.warning("ignored message from chat %s", chat)
        return
    reply = cmds.handle(m.get("text") or "")
    if reply:
        bot.send(reply)


def push(con, bot):
    """Send new alerts and new proposals, and update messages of proposals that expired. Safe to call often."""
    for a in con.execute("SELECT * FROM alerts WHERE delivered=0 ORDER BY created_at").fetchall():
        bot.send(fmt_alert(dict(a)))
        con.execute("UPDATE alerts SET delivered=1 WHERE id=?", (a["id"],))
    for p in approvals.unsent(con):
        mid = bot.send(fmt_proposal(p), proposal_buttons(p))
        approvals.set_message(con, p["id"], mid)
    for p in approvals.expire_old(con):
        if p.get("message_id"):
            bot.edit(p["message_id"], fmt_proposal(p) + "\n\n" + fmt_decided(p))


def daily_summary(con, lines):
    st = PaperBroker(con).state()
    pend = approvals.pending(con)
    cards = db.kv_get(con, "last_scan") or []
    rg = briefing.gather(con, cards=cards, symbols=["SPY"])["regime"] or {}
    return "\n".join([
        "📊 <b>Daily run done</b> · market %s" % E(rg.get("trend", "n/a")),
        "Paper account %s (%s total, %s today)" % (money(st["equity"]), pct(st["total_return_pct"]), pct(st["day_pnl_pct"])),
        "Setups: %d · waiting for your approval: %d · open positions: %d" % (len(cards), len(pend), len(st["positions"])),
        "No setups today. Nothing to buy is a valid answer." if not cards else "",
        "/brief for the full briefing · /site for the dashboard"]).replace("\n\n", "\n")


def poll_forever(bot, run_daily=None, stop=None):
    """Long-poll loop. Each iteration uses a fresh DB connection so it never holds a stale transaction."""
    backoff = 1
    while not (stop and stop.is_set()):
        try:
            ups = bot.updates()
            if ups:
                con = db.connect()
                try:
                    cmds = Commands(con, bot, run_daily)
                    for u in ups:
                        try:
                            handle_update(con, bot, cmds, u)
                        except Exception:
                            log.exception("update failed")
                finally:
                    con.close()
            backoff = 1
        except RuntimeError as e:
            log.warning("%s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception:                                # a bug must not kill the bot: log it and keep polling
            log.exception("telegram loop error")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def setup_chat(token, api=None):
    """Find the chat id of whoever last messaged the bot (used once by the installer). Returns it or None."""
    bot = Bot(token, "x", api)
    ups = bot.call("getUpdates", http_timeout=20)
    for u in reversed(ups or []):
        m = u.get("message") or {}
        if (m.get("chat") or {}).get("type") == "private":
            return str(m["chat"]["id"])
    return None
