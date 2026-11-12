"""Risk department. Every simulated order passes through check_buy(); a failed check rejects the order with the reasons.

Limits come from config.RISK. Rejections are never silently resized: the caller sees exactly which rule failed.
Sells that reduce or close a position are always allowed (risk rules must never trap you in a losing position).
"""
from . import config
from .analysis import indicators as ind
from .data import prices

# Sector map for the default universe (SEC data has SIC codes, not GICS sectors; this keeps exposure checks honest)
SECTOR = {
    "AAPL": "Tech hardware", "MSFT": "Software", "NVDA": "Semiconductors", "AVGO": "Semiconductors", "AMD": "Semiconductors",
    "MU": "Semiconductors", "QCOM": "Semiconductors", "INTC": "Semiconductors", "TXN": "Semiconductors", "ANET": "Networking",
    "CSCO": "Networking", "ORCL": "Software", "CRM": "Software", "ADBE": "Software", "PLTR": "Software", "CRWD": "Cybersecurity",
    "PANW": "Cybersecurity", "GOOGL": "Internet", "META": "Internet", "AMZN": "Internet", "NFLX": "Internet", "TSLA": "Autos",
    "JPM": "Financials", "BAC": "Financials", "V": "Payments", "MA": "Payments", "UNH": "Health care", "LLY": "Health care",
    "JNJ": "Health care", "XOM": "Energy", "COST": "Consumer staples", "WMT": "Consumer staples", "PG": "Consumer staples",
    "KO": "Consumer staples", "PEP": "Consumer staples", "HD": "Consumer discretionary",
    "SPY": "Index", "QQQ": "Index", "IWM": "Index", "DIA": "Index",
}
TECH_GROUP = {"Software", "Semiconductors", "Networking", "Cybersecurity", "Tech hardware", "Internet"}


def sector(symbol):
    return SECTOR.get(symbol, "Unknown")


def check_buy(con, state, symbol, qty, price, stop):
    """state: dict from PaperBroker.state(). Returns (ok, [reasons it failed], {numbers used})."""
    R = config.RISK
    eq = state["equity"]
    why = []
    nums = {"equity": eq, "order_value": qty * price}
    if state["drawdown_pct"] >= R["max_drawdown_pct"]:
        why.append("account drawdown %.1f%% hit the %.0f%% kill switch: no new buys until reset" % (state["drawdown_pct"], R["max_drawdown_pct"]))
    if state["day_pnl_pct"] <= -R["daily_loss_limit_pct"]:
        why.append("down %.1f%% today, past the %.0f%% daily loss limit" % (-state["day_pnl_pct"], R["daily_loss_limit_pct"]))
    held = {p["symbol"]: p for p in state["positions"]}
    if symbol not in held and len(held) >= R["max_open_positions"]:
        why.append("already at the %d open-position limit" % R["max_open_positions"])
    if qty <= 0 or price <= 0:
        why.append("quantity and price must be positive")
    pos_val = qty * price + (held[symbol]["qty"] * price if symbol in held else 0)
    nums["position_pct"] = pos_val / eq * 100 if eq else 0
    if nums["position_pct"] > R["max_position_pct"] + 1e-9:
        why.append("%s would be %.1f%% of the account, over the %.0f%% per-position limit" % (symbol, nums["position_pct"], R["max_position_pct"]))
    if qty * price > state["cash"] + 1e-9:
        why.append("not enough cash ($%.2f needed, $%.2f available)" % (qty * price, state["cash"]))
    if stop is None or stop <= 0 or stop >= price:
        why.append("every buy needs a stop price below the entry")
    else:
        risk = (price - stop) * qty
        nums["risk_pct"] = risk / eq * 100 if eq else 0
        if nums["risk_pct"] > R["max_loss_per_trade_pct"] + 1e-9:
            why.append("a stop-out would lose %.2f%% of the account, over the %.1f%% per-trade limit" % (nums["risk_pct"], R["max_loss_per_trade_pct"]))
    sec = sector(symbol)
    if sec not in ("Index", "Unknown"):
        same = sum(p["qty"] * p["last"] for p in state["positions"] if sector(p["symbol"]) == sec and p["symbol"] != symbol)
        nums["sector_pct"] = (same + pos_val) / eq * 100 if eq else 0
        if nums["sector_pct"] > R["max_sector_pct"] + 1e-9:
            why.append("%s exposure would be %.0f%%, over the %.0f%% sector limit" % (sec, nums["sector_pct"], R["max_sector_pct"]))
    corr = max_correlation(con, symbol, [s for s in held if s != symbol])
    if corr:
        nums["max_correlation"] = corr
        if corr[1] > R["max_correlation"]:
            why.append("moves almost in lockstep with %s (60-day correlation %.2f, limit %.2f)" % (corr[0], corr[1], R["max_correlation"]))
    return not why, why, nums


def max_correlation(con, symbol, others, days=60):
    a = [b[4] for b in prices.bars(con, symbol)][-(days + 1):]
    if len(a) < days // 2 or not others:
        return None
    ra = ind.returns(a)
    best = None
    for o in others:
        b = [x[4] for x in prices.bars(con, o)][-(days + 1):]
        if len(b) < days // 2:
            continue
        c = ind.correlation(ra, ind.returns(b))
        if c is not None and (best is None or c > best[1]):
            best = (o, round(c, 3))
    return best


def size_for_risk(equity, price, stop, cash, fractional=None):
    """Largest quantity that respects both the per-trade loss limit and the position-size limit.
    Fractional shares (to 0.0001) by default, like most US brokers offer; a $1,400 stock in a $10,000 account
    would otherwise be untradeable under a 10% position cap."""
    R = config.RISK
    if stop is None or stop >= price or price <= 0:
        return 0
    fractional = config.FRACTIONAL if fractional is None else fractional
    price = price * (1 + config.SLIPPAGE_BPS / 10000)    # size against the fill price the broker will actually charge
    by_risk = equity * R["max_loss_per_trade_pct"] / 100 / (price - stop)
    by_size = equity * R["max_position_pct"] / 100 / price
    q = max(0.0, min(by_risk, by_size, cash / price))
    return int(q * 10000) / 10000 if fractional else int(q)
