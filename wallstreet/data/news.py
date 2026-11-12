"""Company news headlines from public RSS feeds (Yahoo Finance per-ticker feed, Google News search).

News is NOT a primary source: rows are stored with confirmed=0, and the platform never trades on a headline alone.
Duplicates across feeds are collapsed by a normalized title, so one story syndicated five times is one event.
Headline classification is a keyword heuristic, labelled as such wherever it is shown.
"""
import email.utils
import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .. import db
from .http import get

FEEDS = {
    "yahoo": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US",
    "google": "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
}

# Order matters: the first matching category wins.
CATEGORIES = [
    ("earnings", r"\b(earnings|quarterly results|q[1-4] results|eps|revenue (beat|miss)|beats estimates|misses estimates)\b"),
    ("guidance", r"\b(guidance|outlook|forecast)s?\b.*\b(raise[sd]?|lift[sed]*|cut[s]?|lower[sed]*|boost[sed]*|reaffirm[sed]*)\b|\b(raise[sd]?|cut[s]?|lower[sed]*)\b.*\b(guidance|outlook|forecast)"),
    ("rating_change", r"\b(upgrade[sd]?|downgrade[sd]?|initiate[sd]? (coverage|at)|starts? coverage)\b"),
    ("analyst", r"\b(price target|overweight|underweight|outperform|underperform|analysts?)\b"),
    ("deal", r"\b(to acquire|acquires|acquisition of|agree[sd]? to (buy|acquire)|merger|buyout|takeover|to be acquired|deal to buy)\b"),
    ("legal", r"\b(lawsuit|sued|probe|investigation|antitrust|sec charges|settle[sd]?|fine[sd]?|recall)\b"),
    ("capital", r"\b(buyback|repurchase|dividend|stock split|share offering|secondary offering|convertible|dilution)\b"),
    ("management", r"\b(ceo|cfo|chief executive|steps down|resign[sed]*|appoint[sed]*|names new)\b"),
    ("product", r"\b(launch(es|ed)?|unveil[sed]*|announce[sd]? new|approval|fda|contract (win|award)|partnership)\b"),
]
POSITIVE = r"\b(beat[s]?|raise[sd]?|upgrade[sd]?|record|surge[sd]?|soar[sed]*|jump[sed]*|rall(y|ies|ied)|strong|tops|approval|wins?|buyback)\b"
NEGATIVE = r"\b(miss(es|ed)?|cut[s]?|downgrade[sd]?|plunge[sd]?|slump[sed]*|fall[s]?|fell|drop[sed]*|weak|probe|lawsuit|recall|layoffs?|warns?|offering)\b"


# Names people actually use in headlines, for the default universe. Others fall back to the SEC company name.
NAMES = {"AAPL": ["Apple"], "MSFT": ["Microsoft"], "NVDA": ["Nvidia"], "AMZN": ["Amazon"], "GOOGL": ["Alphabet", "Google"],
         "META": ["Meta", "Facebook", "Instagram"], "AVGO": ["Broadcom"], "TSLA": ["Tesla"], "JPM": ["JPMorgan", "JP Morgan"],
         "V": ["Visa"], "MA": ["Mastercard"], "UNH": ["UnitedHealth"], "LLY": ["Eli Lilly", "Lilly"], "XOM": ["Exxon"],
         "COST": ["Costco"], "WMT": ["Walmart"], "HD": ["Home Depot"], "PG": ["Procter", "P&G"], "JNJ": ["Johnson & Johnson", "J&J"],
         "ORCL": ["Oracle"], "CRM": ["Salesforce"], "AMD": ["AMD", "Advanced Micro"], "ADBE": ["Adobe"], "CSCO": ["Cisco"],
         "NFLX": ["Netflix"], "PEP": ["PepsiCo", "Pepsi"], "KO": ["Coca-Cola", "Coke"], "BAC": ["Bank of America", "BofA"],
         "CRWD": ["CrowdStrike"], "PANW": ["Palo Alto Networks"], "ANET": ["Arista"], "PLTR": ["Palantir"], "MU": ["Micron"],
         "QCOM": ["Qualcomm"], "INTC": ["Intel"], "TXN": ["Texas Instruments"]}
SPAM = r"\b(market (report|forecast|size|research|analysis|outlook)|cagr|forecast to 20\d\d|market to reach|industry report)\b"


def names_for(symbol, company=None):
    if symbol in NAMES:
        return NAMES[symbol]
    if company:
        first = re.sub(r"[^A-Za-z0-9&.' -]", "", company).split()
        if first:
            return [first[0].title() if len(first[0]) >= 4 else " ".join(first[:2]).title()]
    return []


def relevant(title, symbol, names):
    """Keep a headline only if it names the company (or its ticker in an unambiguous form) and isn't market-research spam."""
    if re.search(SPAM, title, re.I):
        return False
    if any(re.search(r"(?<![A-Za-z])%s(?![a-z])" % re.escape(n), title, re.I if len(n) > 3 else 0) for n in names):
        return True
    sym = re.escape(symbol)
    forms = r"(\$%s\b|\(%s\)|(NYSE|NASDAQ|Nasdaq)\s*:\s*%s\b)" % (sym, sym, sym)
    return bool(re.search(forms, title)) or (len(symbol) >= 3 and re.search(r"\b%s\b" % sym, title) is not None)


def classify(title):
    t = title.lower()
    cat = next((name for name, rx in CATEGORIES if re.search(rx, t)), "general")
    pos, neg = len(re.findall(POSITIVE, t)), len(re.findall(NEGATIVE, t))
    tone = "positive" if pos > neg else "negative" if neg > pos else "neutral"
    return cat, tone


def norm_title(title):
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)       # Google appends " - Publisher"
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def parse(text, feed):
    root = ET.fromstring(text.encode("utf-8"))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        pub = it.findtext("pubDate")
        try:
            ts = int(email.utils.parsedate_to_datetime(pub).timestamp() * 1000) if pub else None
        except (TypeError, ValueError):
            ts = None
        src = it.find("source")
        publisher = (src.text or "").strip() if src is not None else "Yahoo Finance" if feed == "yahoo" else ""
        out.append({"title": title, "url": (it.findtext("link") or "").strip(), "published_at": ts, "publisher": publisher})
    return out


def refresh(con, symbol, company=None, max_age_days=21):
    """Fetch both feeds for one symbol. Returns the number of new (deduplicated) headlines."""
    return store(con, symbol, fetch(symbol, company), company, max_age_days)


def fetch(symbol, company=None):
    names = names_for(symbol, company)
    q = urllib.parse.quote('%s stock%s when:14d' % (symbol, "".join(' OR "%s"' % n for n in names[:2])))
    items = []
    for feed, url in FEEDS.items():
        try:
            items += [x | {"feed": feed} for x in parse(get(url.format(sym=urllib.parse.quote(symbol), q=q), kind="text"), feed)]
        except Exception:                                # one feed down must not stop the other
            continue
    return items


def store(con, symbol, items, company=None, max_age_days=21):
    names = names_for(symbol, company)
    cutoff = db.now_ms() - max_age_days * 86400000
    new = 0
    for x in items:
        if x["published_at"] and x["published_at"] < cutoff:
            continue
        key = norm_title(x["title"])
        if len(key) < 12 or not relevant(x["title"], symbol, names):
            continue
        eid = "news:%s:%s" % (symbol, hashlib.sha1(key.encode()).hexdigest()[:16])
        cat, tone = classify(x["title"])
        cur = con.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (eid, "news", symbol, x["title"], x["url"], x["publisher"] or x["feed"], x["published_at"], 0,
                           '{"category": "%s", "tone": "%s", "feed": "%s"}' % (cat, tone, x["feed"]), db.now_ms()))
        new += cur.rowcount
    return new


def prune(con, symbol, company=None):
    """Remove stored headlines for a symbol that fail the current relevance rules. Returns rows removed."""
    names = names_for(symbol, company)
    bad = [r["id"] for r in con.execute("SELECT id, title FROM events WHERE kind='news' AND symbol=?", (symbol,))
           if not relevant(r["title"], symbol, names)]
    con.executemany("DELETE FROM events WHERE id=?", [(i,) for i in bad])
    return len(bad)
