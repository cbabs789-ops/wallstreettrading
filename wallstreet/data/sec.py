"""SEC EDGAR: the official, free source for filings, insider transactions and reported financials.

What each piece means (and doesn't):
  - Filings list: every form a company filed, with the SEC's acceptance timestamp. 8-Ks are confirmed material events.
  - Form 4 (insiders): parsed per transaction. Only code P (open-market purchase) and S (open-market sale) are real
    buying or selling decisions. M (option exercise), F (shares withheld for taxes), A (grant), G (gift) and others are
    routine. A 10b5-1 flag means the trade was scheduled in advance under a plan, not a fresh decision.
  - 13F / 13G: institutional holdings as of a past quarter-end (13F is filed up to 45 days later). Never current positions.
  - Company facts (XBRL): reported financials from 10-K/10-Q, used for fundamentals.
"""
import logging
import re
import xml.etree.ElementTree as ET

from .. import db
from .http import DataError, get

log = logging.getLogger("pws.sec")
TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK%010d.json"
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s"

CODE = {  # Form 4 transaction codes -> (plain label, is it a real open-market decision?)
    "P": ("open-market purchase", True), "S": ("open-market sale", True),
    "M": ("option or RSU exercise/conversion", False), "F": ("shares withheld to pay taxes", False),
    "A": ("grant or award from the company", False), "G": ("gift", False), "D": ("sold back to the company", False),
    "C": ("conversion of a derivative", False), "X": ("exercise of in-the-money option", False),
    "J": ("other (see filing)", False), "W": ("acquired or disposed by will", False), "I": ("discretionary (plan)", False),
}
FORM_KIND = {"8-K": "material event", "10-Q": "quarterly report", "10-K": "annual report", "4": "insider transaction",
             "3": "new insider", "13F-HR": "institutional holdings (quarter-end)", "SC 13G": "5%+ holder (passive)",
             "SCHEDULE 13G": "5%+ holder (passive)", "SC 13D": "5%+ holder (activist)", "SCHEDULE 13D": "5%+ holder (activist)",
             "S-1": "new share registration", "424B5": "share offering", "DEF 14A": "proxy statement", "6-K": "foreign issuer report"}
ITEMS_8K = {"1.01": "material agreement", "1.02": "agreement terminated", "2.01": "acquisition or disposal completed",
            "2.02": "earnings results", "2.05": "restructuring / layoffs", "2.06": "impairment", "3.01": "delisting notice",
            "4.02": "prior financials can't be relied on", "5.01": "change in control", "5.02": "executive or director change",
            "7.01": "Reg FD disclosure", "8.01": "other events", "9.01": "exhibits"}

_cik_cache = {}
_title_cache = {}


def _load_tickers():
    if not _cik_cache:
        data = get(TICKERS)
        for row in data.values():
            _cik_cache[str(row["ticker"]).upper()] = int(row["cik_str"])
            _title_cache[str(row["ticker"]).upper()] = row["title"]


def title_for(symbol):
    """Company name as the SEC lists it, or None (e.g. ETFs)."""
    try:
        _load_tickers()
    except DataError:
        return None
    return _title_cache.get(symbol.upper().replace(".", "-"))


def cik_for(symbol):
    _load_tickers()
    c = _cik_cache.get(symbol.upper().replace(".", "-"))
    if c is None:
        raise DataError("no SEC CIK for %s (ETFs and funds don't file like companies)" % symbol)
    return c


def recent_filings(cik, limit=40):
    d = get(SUBMISSIONS % cik)
    r = (d.get("filings") or {}).get("recent") or {}
    out = []
    for i in range(min(limit, len(r.get("form", [])))):
        acc = r["accessionNumber"][i]
        out.append({"accession": acc, "form": r["form"][i], "filed_at": r["filingDate"][i],
                    "accepted_at": r["acceptanceDateTime"][i], "items": (r.get("items") or [""] * (i + 1))[i] or "",
                    "doc": r["primaryDocument"][i], "desc": (r.get("primaryDocDescription") or [""] * (i + 1))[i] or "",
                    "url": ARCHIVE % (cik, acc.replace("-", ""), r["primaryDocument"][i])})
    return d.get("tickers") or [], out


def _v(node, path):
    el = node.find(path)
    if el is None:
        return None
    v = el.find("value")
    t = (v.text if v is not None else el.text) or ""
    return t.strip() or None                        # a value given only by footnote comes back as whitespace


def _f(node, path):
    try:
        return float(_v(node, path) or 0)
    except ValueError:
        return 0.0


def parse_form4(xml_text):
    """Every transaction line in a Form 4, labelled. Returns (owner, role, plan_flag, [txn...])."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    owner = _v(root, "reportingOwner/reportingOwnerId/rptOwnerName") or "?"
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    role = []
    if rel is not None:
        if (_v(rel, "isDirector") or "0") in ("1", "true"):
            role.append("director")
        if (_v(rel, "isOfficer") or "0") in ("1", "true"):
            role.append(_v(rel, "officerTitle") or "officer")
        if (_v(rel, "isTenPercentOwner") or "0") in ("1", "true"):
            role.append("10% owner")
    plan_doc = (_v(root, "aff10b5One") or "0") in ("1", "true")
    foot = " ".join((f.text or "") for f in root.iter("footnote")).lower()
    plan = plan_doc or "10b5-1" in foot
    txns = []
    for table, deriv in (("nonDerivativeTable/nonDerivativeTransaction", 0), ("derivativeTable/derivativeTransaction", 1)):
        for t in root.findall(table):
            code = _v(t, "transactionCoding/transactionCode") or "?"
            shares = _f(t, "transactionAmounts/transactionShares")
            price = _f(t, "transactionAmounts/transactionPricePerShare")
            label, real = CODE.get(code, ("other code " + code, False))
            txns.append({"date": _v(t, "transactionDate"), "code": code, "kind": label, "decision": real and not deriv,
                         "shares": shares, "price": price, "value": round(shares * price, 2),
                         "acquired": (_v(t, "transactionAmounts/transactionAcquiredDisposedCode") or "") == "A",
                         "owned_after": _f(t, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
                         "derivative": deriv})
    return owner, ", ".join(role) or "insider", plan, txns


def refresh(con, symbol, limit=40, form4_max=10):
    """Store the company's recent filings and parse its recent Form 4s. Returns (new filings, insider lines)."""
    cik = cik_for(symbol)
    _, fl = recent_filings(cik, limit)
    t = db.now_ms()
    new = 0
    for f in fl:
        cur = con.execute("INSERT OR IGNORE INTO filings VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (f["accession"], str(cik), symbol, f["form"], f["filed_at"], f["accepted_at"], f["items"],
                           f["url"], f["desc"], t))
        new += cur.rowcount
    lines = 0
    for f in [f for f in fl if f["form"] == "4"][:form4_max]:
        if con.execute("SELECT 1 FROM insider_txns WHERE accession=? LIMIT 1", (f["accession"],)).fetchone():
            continue                                    # parsed already
        raw_doc = re.sub(r"^xsl[^/]+/", "", f["doc"])   # the xsl path is the HTML view; the bare name is the XML
        url = ARCHIVE % (cik, f["accession"].replace("-", ""), raw_doc)
        try:
            owner, role, plan, txns = parse_form4(get(url, kind="text"))
        except (DataError, ET.ParseError, ValueError) as e:
            log.info("form 4 %s unreadable: %s", f["accession"], e)
            continue
        for i, x in enumerate(txns):
            con.execute("INSERT OR REPLACE INTO insider_txns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f["accession"], i, symbol, owner, role, x["date"], x["code"], x["kind"], x["shares"], x["price"],
                         x["value"], int(x["acquired"]), x["owned_after"], int(plan), x["derivative"], url))
            lines += 1
    return new, lines


def fundamentals(con, symbol):
    """Latest reported annual and quarterly numbers from XBRL company facts. Stored with the period they describe."""
    facts = get(FACTS % cik_for(symbol)).get("facts", {}).get("us-gaap", {})

    def series(*names, unit="USD", form=("10-K",), dur=True):
        # companies switch XBRL tags over the years (Cisco stopped using "Revenues" in 2018), so use the tag
        # whose data is most recent, not the first one that exists
        best = []
        for n in names:
            u = (facts.get(n) or {}).get("units", {}).get(unit)
            if not u:
                continue
            rows = [x for x in u if x.get("form") in form and ("start" in x) == dur and x.get("fp") in (("FY",) if "10-K" in form else ("Q1", "Q2", "Q3", "FY"))]
            if dur:
                rows = [x for x in rows if 330 <= _days(x["start"], x["end"]) <= 380] if "10-K" in form else rows
            seen = {}
            for x in sorted(rows, key=lambda x: (x["end"], x.get("filed", ""))):
                seen[x["end"]] = x                      # last filed value for each period end wins (restatements)
            got = [(k, v["val"]) for k, v in sorted(seen.items())]
            if got and (not best or got[-1][0] > best[-1][0]):
                best = got
        return best

    rev = series("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")
    ni = series("NetIncomeLoss")
    op = series("OperatingIncomeLoss")
    cfo = series("NetCashProvidedByUsedInOperatingActivities")
    capex = series("PaymentsToAcquirePropertyPlantAndEquipment")
    debt = series("LongTermDebtNoncurrent", "LongTermDebt", dur=False, form=("10-K", "10-Q"))
    eq = series("StockholdersEquity", dur=False, form=("10-K", "10-Q"))
    cash = series("CashAndCashEquivalentsAtCarryingValue", dur=False, form=("10-K", "10-Q"))
    eps = series("EarningsPerShareDiluted", unit="USD/shares")
    out = {"revenue": rev[-4:], "net_income": ni[-4:], "operating_income": op[-4:], "op_cash_flow": cfo[-4:],
           "capex": capex[-4:], "long_term_debt": debt[-1:], "equity": eq[-1:], "cash": cash[-1:], "eps_diluted": eps[-4:]}
    as_of = max((v[-1][0] for v in out.values() if v), default=None)
    con.execute("INSERT OR REPLACE INTO fundamentals VALUES(?,?,?,?)", (symbol, __import__("json").dumps(out), as_of, db.now_ms()))
    return out


def _days(a, b):
    from datetime import date
    y1, m1, d1 = map(int, a.split("-"))
    y2, m2, d2 = map(int, b.split("-"))
    return (date(y2, m2, d2) - date(y1, m1, d1)).days


def describe(form, items=""):
    base = FORM_KIND.get(form, form)
    if form == "8-K" and items:
        named = [ITEMS_8K.get(i.strip(), "item " + i.strip()) for i in items.split(",") if i.strip() and i.strip() != "9.01"]
        if named:
            return "8-K: " + ", ".join(named)
    return base
