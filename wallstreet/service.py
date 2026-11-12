"""The always-on server process (`python -m wallstreet run`, started by systemd).

Threads:
  telegram   long-polls the bot: commands, Approve / Reject buttons
  scheduler  the daily pipeline at 4:35 PM Eastern on weekdays; every 15 minutes while the market is open, refreshes
             prices of held stocks and applies stops and targets; pushes alerts and proposals to Telegram;
             publishes stats to GitHub hourly (if configured)
  dashboard  the key-protected web page
Each thread opens its own database connection.
"""
import datetime as dt
import logging
import threading
import time
from zoneinfo import ZoneInfo

from . import alerts, config, dashboard, db, stats
from . import telegram as tg
from .broker.paper import PaperBroker
from .data import prices
from .data.http import DataError

ET = ZoneInfo("America/New_York")
DAILY_AT = (16, 35)
log = logging.getLogger("pws.service")
_daily_lock = threading.Lock()


def is_weekday(now):
    return now.weekday() < 5


def market_open(now):
    return is_weekday(now) and dt.time(9, 35) <= now.time() <= dt.time(15, 58)


def daily_due(now, last_date):
    """True once per weekday, after 4:35 PM Eastern."""
    return is_weekday(now) and (now.hour, now.minute) >= DAILY_AT and last_date != now.date().isoformat()


def run_daily(bot=None):
    """The daily pipeline plus the Telegram summary. Never runs twice at once."""
    from .cli import cmd_daily
    if not _daily_lock.acquire(blocking=False):
        return
    con = db.connect()
    try:
        cmd_daily(con, quiet=True)
        db.kv_set(con, "daily_last", {"at": int(time.time()), "date": dt.datetime.now(ET).date().isoformat()})
        if bot and bot.ready:
            tg.push(con, bot)
            bot.send(tg.daily_summary(con, []))
    except Exception as e:
        log.exception("daily run failed")
        if bot and bot.ready:
            bot.send("🚨 Daily run failed: %s. Check `wallstreet logs` on the server." % tg.E(str(e)[:300]))
    finally:
        con.close()
        _daily_lock.release()


def intraday(con):
    """Refresh held stocks and apply exits. Returns exits made."""
    br = PaperBroker(con)
    held = [p["symbol"] for p in br.positions()]
    if not held:
        return []
    for s in held:
        try:
            prices.refresh(con, s, rng="5d")
        except DataError as e:
            log.warning("intraday refresh %s: %s", s, e)
    exits = br.check_exits()
    st = br.mark()
    alerts.generate(con, [], exits, st, {})
    return exits


def scheduler(bot, stop):
    last_intraday = last_publish = 0
    while not stop.is_set():
        now = dt.datetime.now(ET)
        con = db.connect()
        try:
            last = (db.kv_get(con, "daily_last") or {}).get("date")
            if daily_due(now, last):
                con.close()
                con = None
                run_daily(bot)
                con = db.connect()
            if market_open(now) and time.time() - last_intraday > 15 * 60:
                last_intraday = time.time()
                intraday(con)
            if bot and bot.ready:
                tg.push(con, bot)
            if stats.configured() and time.time() - last_publish > 3600:
                last_publish = time.time()
                try:
                    stats.publish(con)
                except Exception as e:
                    log.warning("stats publish failed: %s", e)
        except Exception:
            log.exception("scheduler step failed")
        finally:
            if con:
                con.close()
        stop.wait(30)


def run():
    stop = threading.Event()
    bot = tg.Bot()
    threads = []
    if bot.ready:
        threads.append(threading.Thread(target=tg.poll_forever, args=(bot, lambda: run_daily(bot), stop), name="telegram", daemon=True))
        try:
            bot.send("🟢 Wall Street is running (paper only). /help for commands.")
        except RuntimeError as e:
            log.warning("startup message failed: %s", e)
    else:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID); running without it")
    threads.append(threading.Thread(target=scheduler, args=(bot, stop), name="scheduler", daemon=True))
    if config.env("PWS_DASH_ENABLED", 1, int):
        srv = dashboard.make_server()
        threads.append(threading.Thread(target=srv.serve_forever, name="dashboard", daemon=True))
        log.info("dashboard on %s:%s", dashboard.HOST, dashboard.PORT)
    for t in threads:
        t.start()
    try:
        while all(t.is_alive() for t in threads):
            time.sleep(5)
        log.error("a worker thread stopped: %s", [t.name for t in threads if not t.is_alive()])
    except KeyboardInterrupt:
        pass
    stop.set()
    return 1                                             # systemd restarts the service
