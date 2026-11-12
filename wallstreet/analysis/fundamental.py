"""Fundamental read from SEC-reported numbers. Every figure carries the period it comes from; nothing is estimated."""
import json


def load(con, symbol):
    r = con.execute("SELECT data, as_of FROM fundamentals WHERE symbol=?", (symbol,)).fetchone()
    return (json.loads(r["data"]), r["as_of"]) if r else (None, None)


def metrics(f, price=None, shares_hint=None):
    """Derived numbers from the latest annual figures. Returns a dict; missing inputs give None, never a guess."""
    if not f:
        return None

    def last(k, n=1):
        s = f.get(k) or []
        return s[-n] if len(s) >= n else None

    rev, rev_prev = last("revenue"), last("revenue", 2)
    ni, op = last("net_income"), last("operating_income")
    cfo, capex = last("op_cash_flow"), last("capex")
    eps = last("eps_diluted")
    debt, eq, cash = last("long_term_debt"), last("equity"), last("cash")
    m = {"fiscal_year_end": rev[0] if rev else None,
         "revenue": rev[1] if rev else None,
         "revenue_growth_pct": (rev[1] / rev_prev[1] - 1) * 100 if rev and rev_prev and rev_prev[1] else None,
         "net_margin_pct": ni[1] / rev[1] * 100 if ni and rev and rev[1] and ni[0] == rev[0] else None,
         "op_margin_pct": op[1] / rev[1] * 100 if op and rev and rev[1] and op[0] == rev[0] else None,
         "free_cash_flow": (cfo[1] - capex[1]) if cfo and capex and cfo[0] == capex[0] else (cfo[1] if cfo else None),
         "debt_to_equity": debt[1] / eq[1] if debt and eq and eq[1] > 0 else None,
         "net_cash": (cash[1] - debt[1]) if cash and debt else None,
         "eps_annual": eps[1] if eps else None,
         "pe_trailing_fy": price / eps[1] if price and eps and eps[1] > 0 else None}
    return m


def describe(m):
    if not m:
        return ["No SEC financials (ETFs and some foreign companies don't file them)."]
    L = []
    if m["revenue"]:
        L.append("Revenue %s (fiscal year ending %s)%s" % (_b(m["revenue"]), m["fiscal_year_end"],
                 ", %+.1f%% year over year" % m["revenue_growth_pct"] if m["revenue_growth_pct"] is not None else ""))
    if m["op_margin_pct"] is not None:
        L.append("Operating margin %.1f%%, net margin %s" % (m["op_margin_pct"], "%.1f%%" % m["net_margin_pct"] if m["net_margin_pct"] is not None else "n/a"))
    if m["free_cash_flow"] is not None:
        L.append("Free cash flow %s" % _b(m["free_cash_flow"]))
    if m["debt_to_equity"] is not None:
        L.append("Long-term debt / equity %.2f" % m["debt_to_equity"])
    if m["pe_trailing_fy"] is not None:
        L.append("P/E %.1f on last fiscal year's EPS of $%.2f (not trailing-12-month)" % (m["pe_trailing_fy"], m["eps_annual"]))
    return L


def _b(x):
    a = abs(x)
    s = "$%.1fB" % (a / 1e9) if a >= 1e9 else "$%.0fM" % (a / 1e6)
    return ("-" if x < 0 else "") + s
