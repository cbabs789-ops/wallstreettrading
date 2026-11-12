# Project Wall Street

A research and paper-trading platform for US stocks. It pulls prices, SEC filings, insider trades, news, earnings and
economic calendars, Fed releases, macro data and 13F holdings; tests strategies with an honest walk-forward lab; scans
for setups; runs every simulated order through a risk department; grades every call it makes against what actually
happened; and shows it all in a daily briefing and a dashboard.

**Paper only.** No code path can place a real order. The Schwab connector is read-only by design (it has no order
methods, and a test fails if one is added). **Not financial advice.** See `docs/FINAL_REPORT.md` for what the evidence
does and doesn't support.

## Setup

Python 3.10+. No third-party packages.

```bash
cp .env.example .env            # set PWS_SEC_USER_AGENT to your name and email (the SEC and FRED require it)
python3 -m unittest tests.test_core tests.test_platform tests.test_server   # 65 offline tests
PWS_LIVE=1 python3 -m unittest tests.test_live                 # 6 tests against the real data sources
python3 -m wallstreet demo                                     # the full 10-step session (first run ~10 minutes)
```

## Run it on a server with Telegram (like Gatekeeper)

On an Ubuntu server (a fresh $4-6 one, or the same one Gatekeeper runs on: it uses about 100 MB of memory), as root:

```bash
curl -fsSL https://raw.githubusercontent.com/cbabs789-ops/wallstreet/main/install.sh | sudo bash
```

It asks for four things, typed on the server and never in a chat:
1. Your name and email (the SEC and FRED require a contact on every request).
2. A Telegram bot token: in Telegram, message @BotFather, send `/newbot`, copy the token.
3. Then you message your new bot "hi" so it learns your chat.
4. Optional: a GitHub token for publishing stats (fine-grained, Contents read/write on one private repo).

Then it loads 10 years of data (about 10 minutes, once) and starts the service. From then on:

- **4:35 PM Eastern, weekdays:** the daily pipeline runs and Telegram gets a summary.
- **Every trade the autopilot wants arrives as a message with ✅ Approve and ❌ Reject buttons.** Nothing is bought until
  you tap Approve. At that moment it re-checks the price (refused if it moved 2%+), earnings (refused within 4 days),
  re-sizes the trade, and runs every risk limit again. Proposals expire at 4:00 PM the next weekday. A double tap can't
  buy twice. `/mode auto` lets it trade paper on its own; `/mode approve` puts you back in control.
- **While the market is open:** every 15 minutes, held stocks are re-priced and stops and targets are applied.
- **Alerts** (setups, exits, earnings on stocks you hold, insider buys, 8-Ks, stale data) arrive as they happen.
- **Dashboard** at `http://<server-ip>:8765/?k=<key>` (send `/site` to the bot). It needs the key.
- **Stats** publish hourly to the GitHub repo, if set, so results can be reviewed from anywhere.
- **Auto-update:** new versions install within 10 minutes, never during the daily run.

Telegram commands: `/status /positions /orders /pending /scan /brief /calendar /catalysts /calls /strategies /alerts
/mode /buy /sell /run /site /help`. Only your chat is obeyed; anyone else is ignored and logged.

Server commands: `wallstreet status | brief | pending | approve ID | reject ID | mode | logs | config | update | restart`.

Still paper only. Real-money execution through Schwab is deliberately not built; when it is, it will use this same
approval flow.

## Daily use

```bash
python3 -m wallstreet daily        # everything: refresh, grade, scan, paper-trade, alerts, briefing, dashboard snapshot
python3 -m wallstreet dashboard    # live dashboard at http://127.0.0.1:8765 (read-only, this machine only)
```

Run it on a schedule once the market has closed (cron, US Eastern time), Monday to Friday:

```
CRON_TZ=America/New_York
35 16 * * 1-5  cd /path/to/wallstreet && python3 -m wallstreet daily >> var/daily.log 2>&1
```

Outputs land in `var/reports/`: `briefing_YYYY-MM-DD.md`, `dashboard.html` (a self-contained snapshot you can open
anywhere), and `performance_YYYY-MM.md`. Alerts are also appended to `var/alerts.log`.

## All commands

| Command | What it does |
|---|---|
| `daily` | The whole pipeline. Safe to re-run; a lock stops overlapping runs |
| `refresh` | Prices (10 years first time, then 3 months), SEC filings, insider trades, fundamentals, Fed |
| `intel` | News, earnings calendar, economic calendar, FRED macro, 13F holdings |
| `backtest` | Walk-forward test of every strategy, vs buying SPY (re-run weekly by `daily`) |
| `scan` | Current setups, scored 0-100, recorded as predictions |
| `grade` | Grade open predictions against what happened since |
| `replay` | Re-run the scanner on every day of the test window (historical calls, kept separate from live) |
| `report [YYYY-MM]` | Monthly performance report |
| `briefing` | Today's briefing |
| `dashboard [--static]` | Serve the dashboard, or write the snapshot file |
| `catalysts` | Rank the universe by catalyst score |
| `status` | Paper account |
| `buy SYM QTY STOP [TARGET]` | Simulated buy through the risk engine |
| `sell SYM QTY` | Simulated sell |
| `exits` | Apply stops, targets and strategy exit rules |
| `demo` | The 10-step integrated session |

## Docs

- `docs/FINAL_REPORT.md`: what works, what doesn't, what's incomplete, and what the numbers mean
- `docs/AUDIT.md`: security and production audit, ranked, with what was fixed
- `docs/ARCHITECTURE.md`: data sources, modules, safety design, Schwab notes
- `docs/integrated_session_2026-11-12.txt`: output of the full session on a fresh database
