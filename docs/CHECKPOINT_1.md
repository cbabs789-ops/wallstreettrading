# Checkpoint 1 review (Nov 12, 2026)

Scope: data engine, analysis, strategy lab, scanner, risk department, paper broker, and an end-to-end simulated
session. Dashboard, news intelligence, performance tracking, briefings and the production audit are later phases.

## Tests

- 26 offline tests: pass.
- 3 live data tests (prices, SEC, Fed, including no duplicates on a second refresh): pass.
- Full simulated session on a fresh database: completes all 9 steps with no errors
  (`docs/demo_session_2026-11-12.txt`).

## What works (tested)

| Area | Evidence |
|---|---|
| Price data, 40 symbols, 10 years | Live test: timestamps fresh, high/low consistent, no duplicate bars |
| SEC filings, Form 4 insider trades, XBRL fundamentals | 1,440 filings and 1,423 insider lines loaded. Real Cisco Form 4 tax withholding (code F) is correctly not called a sale. 10b5-1 plan trades flagged. Option exercises marked as derivative. |
| Fed releases | Loaded, no duplicates on refresh |
| Indicators | RSI checked against a reference value; no look-ahead test |
| Walk-forward lab | Next-open entry, stop before target, gap fills, slippage and portfolio limits each have a test |
| Risk department | Every limit has a rejection test |
| Paper broker | Fills, cash, duplicates, stale-quote refusal, exits fire once, audit rows |
| Sizing | Fractional shares; a regression test proves a risk-sized order always fills |
| Safety | Tests prove no real order path exists |

## Strategy results (test window Jul 2024 to Nov 2026, never used for fitting)

| Strategy | Verdict | Test trades | Profit factor | Return | Worst drop | SPY buy and hold |
|---|---|---|---|---|---|---|
| 20-day breakout on volume | PASS | 116 | 1.85 | +30% | -9% | +41% (worst -19%) |
| Oversold bounce above 200-day (RSI-2) | PASS | 360 | 1.46 | +20% | -7% | +41% (worst -19%) |
| Dip buy in an uptrend | FAIL | 95 | 1.00 | 0% | -10% | +41% |

**Read this honestly:** both passing strategies made less money than simply holding SPY over the same period. Their
case is lower drawdown (about half) with cash idle much of the time, not higher returns. Neither is proof of an
edge you should put real money behind.

## Known problems and limits, ranked

1. **Survivorship bias.** The universe is today's large caps. Stocks that fell out of the S&P over 10 years are
   missing, so every backtest is flattered. Fix needs historical index membership data.
2. **Price data is unofficial.** The Yahoo chart endpoint is undocumented and delayed. If it changes, prices stop.
   Schwab market data would replace it once the developer app is approved.
3. **Schwab connector is untested.** Written to the documented API, never run against a real account.
4. **False passes happen.** The verdict passed a random walk 1 time in 75. With 3 strategies tested, a lucky pass is
   possible. More history and more symbols would tighten this.
5. **Daily bars only.** Nothing here is suitable for intraday trading.
6. **P/E uses fiscal-year EPS**, not trailing twelve months, so it lags after strong quarters.
7. **Predictions table only fills on live setups.** Today the scanner found 0 setups, so it is empty and the
   performance tracker has nothing to grade yet. Grading (resolving predictions) is not built.
8. **Sector map is hand-written** for the default 40 symbols. New symbols show as "Unknown" and skip the sector check.

## Not built yet

- Dashboard (terminal-style web view)
- News and catalyst intelligence beyond SEC and Fed (news feed, earnings calendar, 13F, economic data, catalyst score)
- Performance intelligence (grading predictions, accuracy metrics, monthly report)
- Daily briefing and alerts
- Security and production audit
- Hosting decision (currently runs only in the build workspace)

## Decision needed before Phase 2

1. Where it runs: a small cloud server (about $5 to $10 a month), your own computer, or keep it here for now.
2. Whether to apply for the Schwab developer app now (approval takes days, so starting early helps).
3. What to build next. Recommended order: performance tracking (so every prediction is graded), then the dashboard,
   then news and catalysts.
