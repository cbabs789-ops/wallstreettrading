"""Performance intelligence: grade every prediction against what actually happened, and report accuracy honestly.

Grading rules (identical to the backtest, via backtest.simulate_trade):
  - Bought at the open of the first bar AFTER the data the prediction was made from, plus slippage.
  - Stop checked before target; gaps fill at the open; the strategy's own exit rule and time limit apply.
  - Only bars after the prediction are ever read, so a grade can't leak future data into the past.
A prediction stays open until it exits or its time limit passes.

Two populations, never mixed:
  live    predictions the scanner made in real time.
  replay  the scanner re-run on past dates in the walk-forward TEST window, seeing only data available on each date.
          Fundamentals and catalysts are excluded from replay scores (no point-in-time history), and the evidence
          part uses the VALIDATION window, which ends before the test window starts.

Changing a strategy: never automatic. With fewer than MIN_SAMPLE graded predictions the report says "too few to judge".
Above that, a strategy is flagged for review only if its win rate is below its backtest at p < 0.05 (binomial test).
Any change must then pass walk-forward testing again before use.
"""
import json
import math
import statistics
import time
from pathlib import Path

from . import config, db
from .backtest import simulate_trade
from .data import prices
from .scanner import build_card, find_signals, record
from .strategies import features

MIN_SAMPLE = 30
BUCKETS = [(0, 40), (40, 55), (55, 70), (70, 101)]


class _Feats:
    def __init__(self, con):
        self.con, self.cache = con, {}

    def get(self, symbol):
        if symbol not in self.cache:
            b = prices.bars(self.con, symbol)
            self.cache[symbol] = features(b) if len(b) > 30 else None
        return self.cache[symbol]


def _index_after(ts_list, t):
    lo, hi = 0, len(ts_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts_list[mid] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def grade_one(row, f, spy):
    """Returns a dict of grade fields, or None if the trade hasn't finished yet."""
    slip = config.SLIPPAGE_BPS / 10000
    e = _index_after(f["ts"], row["data_as_of"] // 1000)
    if e >= len(f["ts"]):
        return None                                      # no bar after the prediction yet
    entry = f["open"][e] * (1 + slip)
    sma5_rule = row["exit_rule"] == "sma5"
    max_hold = row["max_hold"] or row["horizon_days"] or 20
    j, px, why = simulate_trade(f, e, entry, row["stop"], None if sma5_rule else row["target"], max_hold, sma5_rule)
    if j is None:
        return None
    exit_px = px * (1 - slip)
    ret = exit_px / entry - 1
    planned_risk = (row["entry"] - row["stop"]) / row["entry"] if row["entry"] > row["stop"] else None
    bench = None
    if spy:
        a = _index_after(spy["ts"], f["ts"][e] - 1)
        b = _index_after(spy["ts"], f["ts"][j]) - 1
        if a < len(spy["ts"]) and 0 <= b and b >= a:
            bench = spy["close"][b] / spy["open"][a] - 1
    return {"outcome": "win" if ret > 0 else "loss", "ret": ret, "entry_fill": entry, "entry_ts": f["ts"][e], "exit_ts": f["ts"][j],
            "exit_reason": why, "mfe": max(f["high"][e:j + 1]) / entry - 1, "mae": min(f["low"][e:j + 1]) / entry - 1,
            "r_mult": ret / planned_risk if planned_risk else None, "bench_ret": bench, "days_held": j - e,
            "resolved_at": db.now_ms()}


def grade_all(con, source=None):
    """Grade every open prediction that can be graded. Returns (graded now, still open)."""
    fs = _Feats(con)
    spy = fs.get("SPY")
    q = "SELECT * FROM predictions WHERE outcome IS NULL"
    rows = con.execute(q + (" AND source=?" if source else ""), (source,) if source else ()).fetchall()
    done = 0
    con.execute("BEGIN")
    for r in rows:
        f = fs.get(r["symbol"])
        g = grade_one(r, f, spy) if f else None
        if g:
            con.execute("UPDATE predictions SET %s WHERE id=?" % ", ".join("%s=?" % k for k in g), (*g.values(), r["id"]))
            done += 1
    con.execute("COMMIT")
    return done, len(rows) - done


def replay(con, wf_results, symbols, window=2):
    """Re-run the scanner on every day of the walk-forward TEST window (window index 2 -> 3). Returns predictions added."""
    any_wf = next((w for w in wf_results.values() if w.get("windows")), None)
    if not any_wf:
        return 0
    t_from, t_to = any_wf["windows"][window], any_wf["windows"][window + 1]
    spy_b = prices.bars(con, "SPY")
    n = 0
    con.execute("BEGIN")
    for s in symbols:
        if s == "SPY":
            continue
        b = prices.bars(con, s)
        if len(b) < 260:
            continue
        f = features(b)
        for i in range(260, len(b) - 1):
            if not (t_from <= f["ts"][i] < t_to):
                continue
            fired, age, sig_i = find_signals(f, i, wf_results, lookback=1)
            if not fired:
                continue
            card = build_card(s, b, f, i, fired, 0, sig_i, spy_b, replay=True)
            n += record(con, card, source="replay", created_at=f["ts"][i] * 1000)
    con.execute("COMMIT")
    return n


# ---------- statistics ----------

def stats(rows):
    rets = [r["ret"] for r in rows]
    if not rets:
        return {"n": 0}
    wins = [x for x in rets if x > 0]
    losses = [-x for x in rets if x <= 0]
    rs = [r["r_mult"] for r in rows if r["r_mult"] is not None]
    bench = [r["bench_ret"] for r in rows if r["bench_ret"] is not None]
    excess = [r["ret"] - r["bench_ret"] for r in rows if r["bench_ret"] is not None]
    return {"n": len(rets), "win_rate": len(wins) / len(rets) * 100, "avg_ret": statistics.mean(rets) * 100,
            "median_ret": statistics.median(rets) * 100, "avg_r": statistics.mean(rs) if rs else None,
            "profit_factor": sum(wins) / sum(losses) if sum(losses) > 0 else None,
            "avg_bench": statistics.mean(bench) * 100 if bench else None, "avg_excess": statistics.mean(excess) * 100 if excess else None,
            "avg_days": statistics.mean(r["days_held"] for r in rows), "best": max(rets) * 100, "worst": min(rets) * 100}


def _lbinom(n, k, p):
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * math.log(p) + (n - k) * math.log(1 - p)


def binom_two_sided(k, n, p):
    """Exact two-sided binomial p-value: chance of a result at least this unlikely if the true rate were p."""
    if n == 0 or not 0 < p < 1:
        return 1.0
    obs = _lbinom(n, k, p)
    return min(1.0, sum(math.exp(_lbinom(n, i, p)) for i in range(n + 1) if _lbinom(n, i, p) <= obs + 1e-9))


def spearman(xs, ys):
    """Rank correlation and an approximate two-sided p-value. None if fewer than 10 points."""
    n = len(xs)
    if n < 10:
        return None, None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    rho = cov / den if den else 0.0
    t = rho * math.sqrt((n - 2) / max(1e-12, 1 - rho ** 2))
    p = math.erfc(abs(t) / math.sqrt(2))                 # normal approximation, fine for n >= 30
    return rho, p


def drift(rows, backtest_win_rate):
    """Plain-English verdict on whether live results differ from the backtest beyond chance."""
    n = len(rows)
    if n < MIN_SAMPLE:
        return {"verdict": "too few to judge", "n": n,
                "action": "No change. %d graded so far; at least %d are needed before results mean anything." % (n, MIN_SAMPLE)}
    if backtest_win_rate is None:
        return {"verdict": "no backtest to compare", "n": n, "action": "No change."}
    k = sum(1 for r in rows if r["ret"] > 0)
    p0 = backtest_win_rate / 100
    pv = binom_two_sided(k, n, p0)
    live = k / n * 100
    if pv < 0.05 and live < backtest_win_rate:
        return {"verdict": "worse than backtest beyond chance", "n": n, "p_value": pv, "live_win_rate": live,
                "backtest_win_rate": backtest_win_rate,
                "action": "Review this strategy. Do not change parameters until a new walk-forward test passes."}
    if pv < 0.05:
        return {"verdict": "better than backtest beyond chance", "n": n, "p_value": pv, "live_win_rate": live,
                "backtest_win_rate": backtest_win_rate, "action": "No change. Better-than-expected streaks also fade."}
    return {"verdict": "consistent with backtest", "n": n, "p_value": pv, "live_win_rate": live,
            "backtest_win_rate": backtest_win_rate, "action": "No change."}


def report(con, source="live", wf_results=None, since_ms=None, until_ms=None):
    q = "SELECT * FROM predictions WHERE source=? AND outcome IS NOT NULL"
    args = [source]
    if since_ms:
        q += " AND entry_ts*1000>=?"
        args.append(since_ms)
    if until_ms:
        q += " AND entry_ts*1000<?"
        args.append(until_ms)
    rows = [dict(r) for r in con.execute(q, args)]
    open_n = con.execute("SELECT COUNT(*) FROM predictions WHERE source=? AND outcome IS NULL", (source,)).fetchone()[0]
    out = {"source": source, "overall": stats(rows), "open": open_n, "by_strategy": {}, "by_score": [], "by_regime": {}}
    window = "validation" if source == "replay" else "test"
    for s in sorted({r["strategy"] for r in rows}):
        sub = [r for r in rows if r["strategy"] == s]
        bt = ((wf_results or {}).get(s) or {}).get("test") or {}
        out["by_strategy"][s] = {"stats": stats(sub), "backtest_test": {"win_rate": bt.get("win_rate"), "expectancy": bt.get("expectancy")},
                                 "drift": drift(sub, bt.get("win_rate"))}
    for lo, hi in BUCKETS:
        sub = [r for r in rows if lo <= r["score"] < hi]
        out["by_score"].append({"range": "%d-%d" % (lo, min(hi, 100)), **stats(sub)})
    for g in sorted({r["regime"] or "unknown" for r in rows}):
        out["by_regime"][g] = stats([r for r in rows if (r["regime"] or "unknown") == g])
    rho, p = spearman([r["score"] for r in rows], [r["ret"] for r in rows])
    within = {}
    for s in out["by_strategy"]:
        sub = [r for r in rows if r["strategy"] == s]
        wr, wp = spearman([r["score"] for r in sub], [r["ret"] for r in sub])
        within[s] = {"spearman": wr, "p_value": wp, "n": len(sub)}
        out["by_strategy"][s]["score_vs_result"] = within[s]
    out["score_vs_result"] = {"spearman": rho, "p_value": p, "n": len(rows), "within_strategy": within,
                              "reading": _score_reading(within, len(rows))}
    out["window_note"] = "Replay evidence scores use the %s window." % window if source == "replay" else ""
    return out


def _score_reading(within, n):
    """Judged WITHIN each strategy: pooled numbers mostly reflect which strategy fired (each has its own payoff shape)."""
    tested = {s: w for s, w in within.items() if w["n"] >= MIN_SAMPLE and w["spearman"] is not None}
    if not tested:
        return "Too few graded predictions per strategy to tell whether higher scores do better."
    better = [s for s, w in tested.items() if w["p_value"] < 0.05 and w["spearman"] > 0]
    worse = [s for s, w in tested.items() if w["p_value"] < 0.05 and w["spearman"] < 0]
    detail = ", ".join("%s %.2f (p=%.2f, n=%d)" % (s, w["spearman"], w["p_value"], w["n"]) for s, w in tested.items())
    if worse:
        return "Within %s, higher scores did WORSE. The score weights need review on validation data, not this test data. [%s]" % (", ".join(worse), detail)
    if better:
        return "Within %s, higher scores did better; elsewhere no measurable effect. Small effects are typical. [%s]" % (", ".join(better), detail)
    return ("Within each strategy, the score did NOT predict results [rank correlation %s]. Treat it as a checklist of the "
            "evidence, not a forecast: a 75 is not more likely to work than a 60 from the same strategy." % detail)


# ---------- paper account ----------

def account_period(con, since_ms, until_ms):
    eq = con.execute("SELECT ts, value FROM equity WHERE ts>=? AND ts<? ORDER BY ts", (since_ms, until_ms)).fetchall()
    sells = con.execute("SELECT o.symbol, o.qty, o.fill_price, o.filled_at, o.reason, o.strategy FROM orders o WHERE side='sell' AND status='filled' "
                        "AND filled_at>=? AND filled_at<?", (since_ms, until_ms)).fetchall()
    buys = con.execute("SELECT COUNT(*) FROM orders WHERE side='buy' AND status='filled' AND filled_at>=? AND filled_at<?", (since_ms, until_ms)).fetchone()[0]
    rejected = con.execute("SELECT COUNT(*) FROM orders WHERE status='rejected' AND created_at>=? AND created_at<?", (since_ms, until_ms)).fetchone()[0]
    out = {"buys": buys, "sells": len(sells), "rejected": rejected}
    if len(eq) >= 2:
        out.update({"start": eq[0]["value"], "end": eq[-1]["value"], "return_pct": (eq[-1]["value"] / eq[0]["value"] - 1) * 100})
        spy = [b for b in prices.bars(con, "SPY") if since_ms / 1000 - 86400 * 4 <= b[0] <= until_ms / 1000]
        if len(spy) >= 2:
            out["spy_return_pct"] = (spy[-1][4] / spy[0][4] - 1) * 100
    return out


# ---------- monthly report ----------

def _fmt(s):
    if not s or not s.get("n"):
        return "none graded"
    return "%d graded · win %.0f%% · avg %+.2f%% · median %+.2f%% · avg R %s · SPY same days %s · avg held %.1f days" % (
        s["n"], s["win_rate"], s["avg_ret"], s["median_ret"], "%+.2f" % s["avg_r"] if s["avg_r"] is not None else "n/a",
        "%+.2f%%" % s["avg_bench"] if s["avg_bench"] is not None else "n/a", s["avg_days"])


def monthly_report(con, wf_results, year=None, month=None, out_dir=None):
    """Writes var/reports/performance_YYYY-MM.md and returns (path, text)."""
    t = time.gmtime()
    year, month = year or t.tm_year, month or t.tm_mon
    since = int(time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1)) * 1000)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    until = int(time.mktime((ny, nm, 1, 0, 0, 0, 0, 0, -1)) * 1000)
    L = ["# Performance report, %04d-%02d" % (year, month), "",
         "Generated %s. Paper trading only. Not financial advice." % time.strftime("%Y-%m-%d %H:%M %Z"), ""]
    acct = account_period(con, since, until)
    L += ["## Paper account this month", ""]
    if "return_pct" in acct:
        L.append("- Equity $%.2f to $%.2f: %+.2f%%%s" % (acct["start"], acct["end"], acct["return_pct"],
                 " (SPY %+.2f%%)" % acct["spy_return_pct"] if "spy_return_pct" in acct else ""))
    else:
        L.append("- Not enough equity history this month to measure a return.")
    L += ["- Orders: %d buys filled, %d sells filled, %d rejected by risk or data checks" % (acct["buys"], acct["sells"], acct["rejected"]), ""]
    for src, title in (("live", "Live predictions"), ("replay", "Replay of the test window (historical, see notes)")):
        allr = report(con, src, wf_results)
        mon = report(con, src, wf_results, since, until) if src == "live" else None
        L += ["## " + title, ""]
        if mon:
            L.append("- This month: " + _fmt(mon["overall"]))
        L.append("- All time: " + _fmt(allr["overall"]) + " · %d still open" % allr["open"])
        if allr["overall"].get("n"):
            L += ["", "| Strategy | Graded | Win rate | Avg result | Backtest win rate | Verdict | Action |", "|---|---|---|---|---|---|---|"]
            for s, d in allr["by_strategy"].items():
                st, bt, dr = d["stats"], d["backtest_test"], d["drift"]
                L.append("| %s | %d | %.0f%% | %+.2f%% | %s | %s%s | %s |" % (
                    s, st["n"], st["win_rate"], st["avg_ret"], "%.0f%%" % bt["win_rate"] if bt.get("win_rate") is not None else "n/a",
                    dr["verdict"], " (p=%.3f)" % dr["p_value"] if "p_value" in dr else "", dr["action"]))
            L += ["", "| Score | Graded | Win rate | Avg result |", "|---|---|---|---|"]
            for b in allr["by_score"]:
                L.append("| %s | %d | %s | %s |" % (b["range"], b["n"], "%.0f%%" % b["win_rate"] if b["n"] else "-",
                                                    "%+.2f%%" % b["avg_ret"] if b["n"] else "-"))
            L += ["", "**Does the score predict results?** " + allr["score_vs_result"]["reading"], ""]
            if allr["by_regime"]:
                L.append("By market regime when the call was made:")
                for g, st in allr["by_regime"].items():
                    L.append("- %s: %s" % (g, _fmt(st)))
        L.append("")
    L += ["## Notes", "",
          "- Replay re-runs the scanner on past dates using only data available on each date. It leaves out fundamentals and",
          "  catalysts (no point-in-time history) and is drawn from today's large caps, so it carries survivorship bias.",
          "- Overlapping calls on the same stock are not independent, so p-values are approximate and lean optimistic.",
          "- Rule: no strategy is changed on fewer than %d graded calls, and never automatically. A change must pass a new" % MIN_SAMPLE,
          "  walk-forward test first.", ""]
    text = "\n".join(L)
    out_dir = Path(out_dir or config.DATA_DIR / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / ("performance_%04d-%02d.md" % (year, month))
    p.write_text(text)
    return p, text


def summary(con, wf_results):
    """Small dict for the briefing and dashboard."""
    return {"live": report(con, "live", wf_results), "replay": report(con, "replay", wf_results)}


def _json(o):
    return json.dumps(o, default=str)
