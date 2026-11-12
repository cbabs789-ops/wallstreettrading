# Architecture

## Flow

```
                     ┌─ prices (Yahoo chart) ─────────┐
                     ├─ SEC EDGAR: filings, Form 4,   │
                     │  XBRL fundamentals, 13F        │
 data sources ───────┼─ Fed RSS, FOMC calendar        ├──> SQLite (var/wallstreet.db)
                     ├─ BLS release calendars, FRED   │
                     ├─ Nasdaq earnings calendar      │
                     └─ Yahoo + Google News RSS ──────┘
                                    │
          ┌─────────────────────────┼──────────────────────────────┐
          v                         v                              v
   strategy lab              catalysts.py                   analysis/ (technical,
   (walk-forward)            (score + linked items)          fundamental, regime)
          │                         │                              │
          └──────────────> scanner.py (cards, 0-100) <─────────────┘
                                    │
                    ┌───────────────┼────────────────┐
                    v               v                v
             predictions      autopilot.py      alerts.py
             (graded by       risk.py  ──> broker/paper.py (the ONLY order writer)
             performance.py)                         │
                    └───────────────┬────────────────┘
                                    v
                    briefing.py ──> Markdown briefing
                                └─> dashboard.py (served read-only, or a static snapshot)
```

One SQLite file in WAL mode. Tables: `bars`, `quotes`, `filings`, `insider_txns`, `events` (news, earnings, economic
releases, Fed), `fundamentals`, `holdings13f`, `orders`, `positions`, `account`, `equity`, `audit`, `predictions`,
`alerts`. Columns added after release are migrated in place (`db._migrate`).

## Data sources

| Source | What | Confirmed? | Caveats |
|---|---|---|---|
| Yahoo Finance chart endpoint | Daily bars, last quote, VIX, 10-year yield | Market data | Unofficial, undocumented, delayed. Can change or block without notice. Labelled on every screen. |
| SEC EDGAR | Filings, Form 4 insider trades, XBRL fundamentals, 13F | Yes (primary) | Needs a contact User-Agent. About 8 requests/second. |
| Federal Reserve | Press releases RSS, FOMC meeting calendar | Yes (primary) | Briefing shows policy items only. |
| BLS | Release dates for CPI, jobs report, PPI, JOLTS | Yes (primary) | Schedule page format could change. |
| FRED | Fed funds, 2y and 10y yields, curve, unemployment, CPI, jobless claims | Yes (official data) | Each value carries its own observation date (monthly series lag). |
| Nasdaq earnings calendar | Upcoming earnings dates, time, EPS estimate | No | Unofficial public API. Companies move dates; stored as unconfirmed. |
| Yahoo Finance and Google News RSS | Headlines | No | Never traded on. Filtered for relevance, deduplicated, keyword-labelled (heuristic). |
| Schwab Trader API | Quotes, history, movers, accounts | n/a | Code written, not tested against a real account. Read-only. |

## What each layer means

- **Insider trades:** only open-market purchases (P) and sales (S) count as decisions. Option exercises, tax
  withholding, grants and gifts are listed as routine. 10b5-1 plan trades are labelled pre-scheduled and don't score.
- **13F:** a quarter-end snapshot filed up to 45 days later. Shown as context with its period and filing date, marked
  stale after 150 days, never scored, never called a current position.
- **Catalyst score (0-100):** how much is happening (earnings ahead, material 8-K, real insider buying, unusual news
  volume, rating changes, deals or guidance), not which direction. Negative items are listed as risks.
- **Opportunity score (0-100):** setup 35, strategy evidence 20, trend 20, fundamentals 15, catalyst 10, minus
  penalties. Replay testing showed it does not predict results within a strategy (see FINAL_REPORT.md), so it is a
  checklist, and the autopilot does not filter on it.

## Strategy lab rules

- Signals read on a finished bar; trades at the next bar's open.
- Stop checked before target inside a bar; gaps fill at the open. One shared function (`backtest.simulate_trade`) runs
  both the backtest and the grading of live calls, so the two can't drift apart.
- 5 bps slippage per side; portfolio limits match the live risk limits.
- Walk-forward: train 50%, validation 25%, test 25%. Parameters chosen on train only.
- A pass needs positive results in all three windows, profit factor 1.2+ on validation and 1.3+ on test, and 30+ test
  trades. On random-walk prices this falsely passed 1 run in 75.

## Performance intelligence

- Every scanner card is a prediction (one per setup, however many times it is scanned).
- Grading uses only bars after the prediction (a test poisons all earlier bars and checks the grade doesn't change).
- `live` and `replay` populations are never mixed. Replay scores use the validation window's evidence and leave out
  fundamentals and catalysts, which have no point-in-time history.
- Drift check: nothing is judged under 30 graded calls. Above that, a strategy is flagged for review only if its win
  rate differs from the backtest at p < 0.05 (exact binomial). Changes are never automatic.

## Safety design

1. No real order path. `broker/paper.py` is the only module that inserts orders; the Schwab client has no order
   methods. Tests enforce both.
2. Every buy passes the risk department: 15% drawdown kill switch, 3% daily loss limit, 10 positions, 10% per
   position, 1% loss at the stop, 35% per sector, 0.85 max correlation, stop required. Sizing uses the slipped fill price.
3. Idempotent orders by client id; the autopilot's ids are fixed per setup.
4. Bad data is refused: quotes older than 4 days, quotes 30%+ from the last close, inconsistent bars.
5. Audit trail of every order, rejection, exit and daily run.
6. Secrets only in `.env` or the 0600 token file, both git-ignored. Schwab login uses OAuth with a one-time `state`.
   Account numbers are masked to the last 4 digits inside the connector.

## Schwab notes

From public developer documentation (verify when you create the app): register at developer.schwab.com, request the
Market Data and/or Accounts and Trading products (approval has taken days), log in on Schwab's page (the app never
sees your password). Access tokens last 30 minutes and refresh automatically; the refresh token expires after 7 days.
Schwab offers no paper-trading environment, which is why the paper broker here is our own simulator.
