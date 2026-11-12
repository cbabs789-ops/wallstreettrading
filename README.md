# Project Wall Street

A research and paper-trading platform for US stocks. It pulls market data, SEC filings and Fed releases, scores
setups with strategies that passed an honest walk-forward test, runs every simulated order through a risk department,
and keeps an audit trail of everything.

**Paper only.** There is no code path that can place a real order. The Schwab connector is read-only by design
(it has no order methods, and a test fails if one is added).

**Not financial advice.** Backtests are simulations with known biases (see `docs/CHECKPOINT_1.md`).

## Setup

Python 3.10+ and no third-party packages.

```bash
cp .env.example .env          # then set PWS_SEC_USER_AGENT to your name and email
python3 -m unittest tests.test_core -v              # 26 offline tests
PWS_LIVE=1 python3 -m unittest tests.test_live -v   # 3 tests that hit real data sources
```

## Commands

```bash
python3 -m wallstreet refresh            # 10 years of daily prices, SEC filings, insider trades, Fed releases
python3 -m wallstreet backtest           # walk-forward test of every strategy, vs buying SPY
python3 -m wallstreet scan               # current setups, scored 0-100, saved to the predictions table
python3 -m wallstreet status             # paper account: cash, positions, P&L, drawdown
python3 -m wallstreet buy SYMBOL --stop PRICE [--qty N] [--target PRICE]
python3 -m wallstreet sell SYMBOL [--qty N]
python3 -m wallstreet exits              # apply stops and targets to open paper positions
python3 -m wallstreet demo               # full simulated session, steps 1-9
```

A first `refresh` takes about 5 minutes (the SEC limits requests to about 8 per second).

## Where things are

| Path | What |
|---|---|
| `wallstreet/data/` | Prices (Yahoo chart endpoint), SEC EDGAR (filings, Form 4, XBRL fundamentals), Fed RSS |
| `wallstreet/analysis/` | Indicators, technical snapshot, fundamental metrics |
| `wallstreet/strategies.py`, `backtest.py` | Strategy rules and the walk-forward lab |
| `wallstreet/scanner.py` | Opportunity cards with scores |
| `wallstreet/risk.py` | Risk limits and position sizing |
| `wallstreet/broker/paper.py` | The only code that writes orders |
| `wallstreet/broker/schwab.py` | Read-only Schwab OAuth client (untested against a real account) |
| `docs/` | Architecture, checkpoint report, demo session output |
