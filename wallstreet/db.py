"""SQLite storage. One file, WAL mode, every table carries the time the data was observed and where it came from."""
import json
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL,
  source TEXT, fetched_at INTEGER, PRIMARY KEY (symbol, interval, ts)
);
CREATE TABLE IF NOT EXISTS quotes (
  symbol TEXT, price REAL, quote_time INTEGER, fetched_at INTEGER, source TEXT, delayed INTEGER, currency TEXT,
  PRIMARY KEY (symbol, quote_time)
);
CREATE TABLE IF NOT EXISTS filings (
  accession TEXT PRIMARY KEY, cik TEXT, symbol TEXT, form TEXT, filed_at TEXT, accepted_at TEXT, items TEXT,
  url TEXT, description TEXT, fetched_at INTEGER
);
CREATE TABLE IF NOT EXISTS insider_txns (
  accession TEXT, line INTEGER, symbol TEXT, owner TEXT, role TEXT, txn_date TEXT, code TEXT, kind TEXT,
  shares REAL, price REAL, value REAL, acquired INTEGER, owned_after REAL, plan_10b5_1 INTEGER, derivative INTEGER,
  url TEXT, PRIMARY KEY (accession, line)
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, kind TEXT, symbol TEXT, title TEXT, url TEXT, source TEXT, published_at INTEGER,
  confirmed INTEGER, detail TEXT, fetched_at INTEGER
);
CREATE TABLE IF NOT EXISTS fundamentals (
  symbol TEXT PRIMARY KEY, data TEXT, as_of TEXT, fetched_at INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT UNIQUE, symbol TEXT, side TEXT, qty REAL, order_type TEXT,
  limit_price REAL, stop_price REAL, target_price REAL, status TEXT, reason TEXT, created_at INTEGER,
  filled_at INTEGER, fill_price REAL, commission REAL, quote_time INTEGER, strategy TEXT, evidence TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  symbol TEXT PRIMARY KEY, qty REAL, avg_price REAL, stop_price REAL, target_price REAL, opened_at INTEGER, strategy TEXT
);
CREATE TABLE IF NOT EXISTS account (
  k TEXT PRIMARY KEY, v TEXT
);
CREATE TABLE IF NOT EXISTS equity (
  ts INTEGER PRIMARY KEY, value REAL, cash REAL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, actor TEXT, action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, symbol TEXT, strategy TEXT, score REAL, direction TEXT,
  entry REAL, stop REAL, target REAL, horizon_days INTEGER, data_as_of INTEGER, evidence TEXT,
  outcome TEXT, resolved_at INTEGER, ret REAL, mfe REAL, mae REAL
);
CREATE TABLE IF NOT EXISTS holdings13f (
  filer_cik INTEGER, filer TEXT, period TEXT, filed TEXT, symbol TEXT, issuer TEXT, value REAL, shares REAL,
  prev_shares REAL, url TEXT, fetched_at INTEGER, PRIMARY KEY (filer_cik, period, symbol)
);
CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY, created_at INTEGER, severity TEXT, kind TEXT, symbol TEXT, title TEXT, detail TEXT, url TEXT,
  delivered INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_symbol ON events(symbol, published_at);
CREATE INDEX IF NOT EXISTS filings_symbol ON filings(symbol, filed_at);
"""


def now_ms():
    return int(time.time() * 1000)


def connect(path=None):
    p = path or config.DB_PATH
    if str(p) != ":memory:":
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            config.DATA_DIR.chmod(0o700)                 # the account history is nobody else's business
        except OSError:
            pass
    con = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


# Columns added after the first release. Added in place so an existing database keeps its history.
_ADDED = {"predictions": [("signal_ts", "INTEGER"), ("source", "TEXT DEFAULT 'live'"), ("regime", "TEXT"),
                          ("score_parts", "TEXT"), ("max_hold", "INTEGER"), ("exit_rule", "TEXT"), ("entry_fill", "REAL"),
                          ("entry_ts", "INTEGER"), ("exit_ts", "INTEGER"), ("exit_reason", "TEXT"), ("r_mult", "REAL"),
                          ("bench_ret", "REAL"), ("days_held", "INTEGER")]}


def _migrate(con):
    for table, cols in _ADDED.items():
        have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        for name, decl in cols:
            if name not in have:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
    # one prediction per setup: re-running the scanner the same day (or while a signal stays live) must not double count
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS predictions_once ON predictions(source, symbol, strategy, signal_ts)")


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM account WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO account(k, v) VALUES(?,?)", (k, json.dumps(v)))


def audit(con, actor, action, detail):
    con.execute("INSERT INTO audit(ts, actor, action, detail) VALUES(?,?,?,?)",
                (now_ms(), actor, action, json.dumps(detail, default=str)))
