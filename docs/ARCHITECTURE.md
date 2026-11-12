# Architecture

## Flow

```
data sources ──> SQLite (var/wallstreet.db) ──> analysis ──> scanner ──> risk ──> paper broker ──> audit / equity
     │                                              │
     └── Yahoo chart, SEC EDGAR, Fed RSS            └── strategy lab (walk-forward) supplies the evidence and confidence
```

One SQLite file in WAL mode. Tables: `bars`, `quotes`, `filings`, `insider_txns`, `events`, `fundamentals`,
`orders`, `positions`, `account`, `equity`, `audit`, `predictions`.

## Data sources

| Source | What | Status | Caveats |
|---|---|---|---|
| Yahoo Finance chart endpoint | Daily bars (10 years), last quote, VIX | Working, tested live | Unofficial and undocumented. It can change or block without notice. Quotes are delayed. Every quote is stored with its own timestamp and labelled "unofficial, delayed". |
| SEC EDGAR | Ticker to CIK, filings list, Form 4 XML, XBRL company facts | Working, tested live | Needs `PWS_SEC_USER_AGENT` with a contact email. Throttled to about 8 requests per second. |
| Federal Reserve RSS | Policy statements and releases | Working, tested live | Headlines and links only. |
| Schwab Trader API | Quotes, price history, movers, accounts | Code written, **not tested** | Needs an approved developer app. Read-only here. |
| News, earnings calendars, 13F, economic data | | **Not built** | Phase for later. |

Today's unfinished daily bar is dropped during market hours so no indicator reads a half-day as a full day.

## Schwab: what we know

From public developer documentation and guides (the official portal page could not be fetched from the build
environment, so verify these when you create the app):

- You register an app at developer.schwab.com and request the Market Data and/or Accounts and Trading products.
  Approval has taken a few days.
- OAuth 2: you log in on Schwab's own page; the app never sees your password. Access tokens last 30 minutes and are
  refreshed automatically. Refresh tokens expire after 7 days, after which you must log in again.
- Schwab offers no paper-trading environment. That is why the paper broker here is our own simulator.
- Tokens are stored in `var/schwab_tokens.json` with owner-only permissions (0600) and are git-ignored.

## Safety design

1. **No real order path exists.** `broker/paper.py` is the only module that inserts into `orders`. The Schwab client
   has no order methods. `tests/test_core.py::Safety` fails if either changes.
2. **Every buy passes the risk department**: kill switch at 15% drawdown, 3% daily loss limit, 10 positions max,
   10% per position, 1% loss per trade at the stop, 35% per sector, 0.85 max correlation, and a stop is required.
   Failed checks reject with reasons; nothing is silently resized.
3. **Idempotent orders**: a repeated `client_id` returns the original order instead of filling twice.
4. **Stale data is refused**: no fill on a quote older than 4 days.
5. **Audit trail**: every order, rejection and exit is written to `audit`.
6. **Secrets**: only in `.env` or the token file, both git-ignored. No password is ever requested.

## Strategy lab rules

- Signals read on a finished bar, trades at the next bar's open.
- Stop checked before target inside a bar; a gap through the stop fills at the open.
- 5 bps slippage per side; portfolio limits match the live risk limits.
- Walk-forward: train 50%, validation 25%, test 25%. Parameters chosen on train only (by Sharpe, 20+ trades).
- A strategy passes only with positive expectancy in all three windows, profit factor 1.2+ on validation, 1.3+ on
  test and 30+ test trades. On random-walk prices this verdict falsely passed 1 run in 75.
