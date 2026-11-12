"""Paper autopilot: turns qualifying scanner cards into SIMULATED orders so the paper account builds a real track record.

Rules (all must hold):
  - the strategy passed walk-forward testing
  - the signal fired on the latest finished bar (an older signal is a late entry the backtest never took)
  - score >= PWS_AUTO_MIN_SCORE (default 0, i.e. off). Replaying the test window showed the score does not predict
    results within a strategy, so filtering on it would only drop trades the backtest counted. Ranking still uses it.
  - no earnings due within 4 days (gap risk the stop can't control)
  - the risk department approves the size
Orders go only to the paper broker. Each setup has a fixed client id, so re-running never doubles an order.

Modes (PWS_AUTOPILOT_MODE, or /mode in Telegram, which wins):
  approve  (default) each qualifying setup becomes a proposal; nothing is ordered until you tap Approve
  auto     orders are placed straight away (paper only)
  off      setups are reported, nothing else
"""
from . import approvals, config, db, risk

MIN_SCORE = config.env("PWS_AUTO_MIN_SCORE", 0, float)
MODES = ("approve", "auto", "off")


def mode(con):
    m = db.kv_get(con, "autopilot_mode") or config.env("PWS_AUTOPILOT_MODE", "approve")
    if config.env("PWS_AUTOTRADE", 1, int) == 0:
        m = "off"
    return m if m in MODES else "approve"


def set_mode(con, m):
    if m not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    db.kv_set(con, "autopilot_mode", m)
    db.audit(con, "autopilot", "mode", {"mode": m})


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


def run(con, broker, cards, wf_results, mode_=None):
    """Returns (results, skipped). results are orders in 'auto' mode and proposals in 'approve' mode (each has 'kind').
    skipped is [(symbol, reason)]."""
    m = mode_ or mode(con)
    if m == "off":
        return [], [(c["symbol"], "autopilot off") for c in cards]
    go, skipped = candidates(cards, wf_results)
    out = []
    for c, _ in go:
        st = broker.state()
        qty = risk.size_for_risk(st["equity"], c["price"], c["stop"], st["cash"])
        if qty <= 0:
            skipped.append((c, "position size rounds to zero"))
            continue
        if m == "approve":
            p = approvals.propose(con, c, qty)
            if p:
                out.append(p | {"kind": "proposal"})
            continue
        o = broker.submit(c["symbol"], "buy", qty, stop=c["stop"], target=None if c["exit_rule"] == "sma5" else c["targets"][0],
                          client_id="auto-%s-%s-%s" % (c["symbol"], c["strategy"], c["signal_time"]), strategy=c["strategy"],
                          evidence={"score": c["score"], "confidence": c["confidence"], "params": c["params"]})
        out.append(o | {"kind": "order"})
    return out, [(c["symbol"], why) for c, why in skipped]
