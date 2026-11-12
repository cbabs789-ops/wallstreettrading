# Project Wall Street: final report (Nov 12, 2026)

Paper trading only. Not financial advice.

## Bottom line

The platform works end to end on live data and is tested:

- **Tests:** 50 offline tests and 6 live-source tests pass.
- **Integrated session:** the full 10-step session runs clean on an empty database.
- **Daily pipeline:** `daily` runs in about 2 minutes and does nothing twice.

What it has NOT shown is an edge worth real money:

- Both passing strategies made less than buying SPY over the same 2.3 years. Their case is about half the drawdown, not more profit.
- The 0-100 opportunity score does not predict which trades work (details below).
- Live calls have not been graded yet, because live calls need days to play out.

The honest next step is to let it run on paper for a few months and read the monthly reports.

## What works (tested)

| Area | What it does | Evidence |
|---|---|---|
| Market data | 10 years of daily bars for 40 symbols, quotes with timestamps, VIX, 10-year yield | Live test; bad bars and bad ticks refused |
| SEC | Filings, insider trades, XBRL fundamentals, 13F holdings | Real Form 4 fixtures; plan trades and tax withholding not counted as decisions; 13F labelled as past snapshots |
| News | Yahoo and Google headlines, relevance-filtered, deduplicated, keyword-labelled | 13 relevance cases; syndicated story counted once |
| Calendars | Earnings dates (Nasdaq), CPI, jobs, PPI, JOLTS (BLS), Fed meetings | Parser tests; moved dates update in place |
| Macro | Fed funds, 2y, 10y, curve, unemployment, CPI inflation, jobless claims (FRED) | Live test; each value shows its own date |
| Catalysts | 0-100 activity score with a source link on every item | Exclusion rules tested (plan trades, earnings 8-Ks, no fake news spikes) |
| Strategy lab | Walk-forward, out-of-sample, costs, slippage, full metrics, vs SPY | Next-open entry, stop-first, gap fills, false-pass rate 1 in 75 on random data |
| Scanner | Cards with all required fields, recorded as predictions once per setup | Tested |
| Risk department | 9 hard limits, sizing at the fill price | Every limit has a rejection test; risk-sized orders always fill |
| Paper broker | Fills, cash, exits (stop, target, time limit, 5-day-average rule), idempotent orders, audit trail | Tested |
| Paper autopilot | Takes fresh signals from passing strategies through the risk engine | Rules and idempotence tested |
| Performance | Grades every call with the same rules as the backtest; live and replay kept separate; drift test; monthly report | No-look-ahead test poisons all earlier bars; replay matches the backtest |
| Alerts | Setups, exits, kill switch, daily loss, held-stock earnings, insider buys, material 8-Ks, stale data | Dedup tested: a rerun creates none |
| Briefing | Market, economy, calendar, Fed policy, setups, catalysts, portfolio, alerts, accuracy, data freshness | Renders on an empty database |
| Dashboard | Dark terminal page, live or as a snapshot file | Read-only, local-only, CSP, escaping tested; no errors at desktop or phone width |

## What the numbers say

**Walk-forward test window (Jul 2024 to Nov 2026), never used for fitting:**

| Strategy | Verdict | Trades | Win rate | Avg per trade | Return | Worst drop | SPY |
|---|---|---|---|---|---|---|---|
| 20-day breakout on volume | PASS | 116 | 39% | +2.35% | +30% | -9% | +41% (worst -19%) |
| Oversold bounce above the 200-day | PASS | 360 | 66% | +0.52% | +20% | -7% | +41% (worst -19%) |
| Dip buy in an uptrend | FAIL | 95 | 33% | 0.00% | 0% | -10% | +41% |

**Replay:** the scanner was re-run on every day of that window, seeing only what was known on each day. That produced 907 graded calls:

- Overall: win rate 57%, average +0.51% per call, versus +0.31% for SPY over the same days.
- Win rates per strategy match the backtest, with no difference beyond chance. That confirms the grading and the backtest agree.

**The score finding:**

- Within each strategy, higher scores did not do better:
  - breakout: rank correlation 0.02 (p=0.82)
  - oversold bounce: -0.05 (p=0.17)
  - dip buy: -0.09 (p=0.37)
- So a 75 is not more likely to work than a 60 from the same strategy. The score is a checklist of the evidence, not a forecast.
- The autopilot therefore takes every fresh signal from a passing strategy, exactly as the backtest did, instead of filtering on the score.
- The weights were deliberately NOT re-tuned on this data, because that would be fitting to the test window.

## What does not work, or is incomplete

1. **No live track record yet.** The scanner found 0 setups today, so there are no live calls to grade. Grading, drift checks and the monthly report are built and tested, and fill in as live calls resolve.
2. **Schwab connector untested.** It's read-only by design and was hardened in the audit (OAuth `state`, masked account numbers, wrapped errors). It has never touched a real account and needs an approved developer app.
3. **No language-model analysis.** Every fact on a card comes from data, with sources. A model-written narrative was not added because it would need an API key and could state things the data doesn't support.
4. **News sentiment is a keyword heuristic.** It's labelled as such everywhere and never traded on.
5. **Alerts are local only:** dashboard, briefing and a log file. There is no push or email.
6. **Not hosted.** It runs where you put it. The README has the cron line.

## Limitations to keep in mind

- **Survivorship bias.** The universe is today's large caps, so every backtest and replay is flattered.
- **Prices come from an unofficial, delayed Yahoo endpoint.** Nasdaq's earnings API is also unofficial. Either can break without notice. The platform alerts on stale data and refuses orders after 4 days without fresh quotes.
- **The paper autopilot fills at the delayed quote when `daily` runs.** The backtest buys at the next open, so expect small differences.
- **Daily bars only.** Nothing here is for intraday trading.

## Security

See `docs/AUDIT.md`:

- 15 problems were found and fixed, each with a test.
- 11 open issues are ranked.
- No real order path exists: tests enforce that only the paper broker writes orders and that the Schwab client has no order methods.
- The project uses no third-party packages.

## Recommended next steps

1. Pick a host and schedule `daily` after the close. A small cloud server or an always-on computer works.
2. Apply for the Schwab developer app (market data), to replace the unofficial price feed.
3. After 30+ live calls per strategy, read the drift verdicts in the monthly report. Change nothing before then.
4. To improve the score, fit new weights on the validation window, then check them once on the test window.
