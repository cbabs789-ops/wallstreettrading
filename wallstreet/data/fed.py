"""Federal Reserve press releases (official RSS). Stored as confirmed events with the Fed's own publish time."""
import email.utils
import hashlib
import xml.etree.ElementTree as ET

from .. import db
from .http import get

FEEDS = {"fed_monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
         "fed_all": "https://www.federalreserve.gov/feeds/press_all.xml"}


def parse_rss(text):
    root = ET.fromstring(text.encode("utf-8"))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        try:
            ts = int(email.utils.parsedate_to_datetime(pub).timestamp() * 1000) if pub else None
        except (TypeError, ValueError):
            ts = None
        if title:
            out.append({"title": title, "url": link, "published_at": ts, "desc": (it.findtext("description") or "").strip()[:500]})
    return out


def refresh(con, feed="fed_all"):
    items = parse_rss(get(FEEDS[feed], kind="text"))
    new = 0
    for x in items:
        eid = "fed:" + hashlib.sha1((x["url"] or x["title"]).encode()).hexdigest()[:16]
        kind = "fed_policy" if any(w in x["title"].lower() for w in ("fomc", "federal funds", "monetary policy", "minutes")) else "fed_release"
        cur = con.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (eid, kind, None, x["title"], x["url"], "federalreserve.gov", x["published_at"], 1, x["desc"], db.now_ms()))
        new += cur.rowcount
    return new
