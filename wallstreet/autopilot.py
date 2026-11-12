"""Paper autopilot: turns qualifying scanner cards into SIMULATED orders so the paper account builds a real track record.

Rules (all must hold):
  - the strategy passed walk-forward testing
  - the signal fired on the latest finished bar (an older signal is a late entry the backtest never took)
  - score >= PWS_AUTO_MIN_SCORE (default 0, i.e. off). Replaying the test window showed the score does not predict
    results within a strategy, so filtering on it would only drop trades the backtest counted. Ranking still uses it.
  - no earnings due within 4 days (gap risk the stop can't control)
  - the risk department approves the size
Orders go only to the paper broker. Each setup has a fixed client id, so re-running never doubles an order.
Turn it off with PWS_AUTOTRADE=0.
"""
from . import config, risk

MIN_SCORE = config.env("PWS_AUTO_MIN_SCORE", 0, float)
ENABLED = config.env("PWS_AUTOTRADE", 1, int) == 1


def candidates(cards, wf_results):
    out, skipped = [], []
    for c in sorted(cards, key=lambda c: -c["score"]):
        why = None
        if (wf_results.get(c["strategy"]) or {}).get("verdict") != "pass":
            why = "strategy failed out-of-sample testing"
        elif c["signal_age_days"]:
            why = "signal is %d day(s) old" % c["signal_age_days"]
        elif c["score"] < MIN_SCORE:
            why = "score %d under %d" % (c["score"], MIN_SCORE)
        elif (c.get("catalyst") or {}).get("earnings_soon"):
            why = "earnings within 4 days"
        (skipped if why else out).append((c, why))
    return out, skipped


def run(con, broker, cards, wf_results):
    """Returns (orders, skipped) where skipped is [(symbol, reason)]."""
    if not ENABLED:
        return [], [(c["symbol"], "autopilot off") for c in cards]
    go, skipped = candidates(cards, wf_results)
    orders = []
    for c, _ in go:
        st = broker.state()
        qty = risk.size_for_risk(st["equity"], c["price"], c["stop"], st["cash"])
        if qty <= 0:
            skipped.append((c, "position size rounds to zero"))
            continue
        o = broker.submit(c["symbol"], "buy", qty, stop=c["stop"], target=None if c["exit_rule"] == "sma5" else c["targets"][0],
                          client_id="auto-%s-%s-%s" % (c["symbol"], c["strategy"], c["signal_time"]), strategy=c["strategy"],
                          evidence={"score": c["score"], "confidence": c["confidence"], "params": c["params"]})
        orders.append(o)
    return orders, [(c["symbol"], why) for c, why in skipped]
