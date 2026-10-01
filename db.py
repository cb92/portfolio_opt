"""
db.py

Shared database helpers used by data_collection.py, seed_requests.py (and later the
Streamlit app). All connection settings come from the environment, so moving between
a local MySQL and Railway is a configuration change only.
"""
import os
import re
from urllib.parse import unquote, urlparse

import mysql.connector
from dotenv import load_dotenv

# letters/digits/dot/hyphen, 1-10 chars (matches VARCHAR(10) in setup.sql)
TICKER_RE = re.compile(r'^[A-Z0-9][A-Z0-9.\-]{0,9}$')


# Pulls out the first value associated with any one of the names provided
def _env(*names, default=None):
    for n in names:
        v = os.getenv(n)
        if v not in (None, ''):
            return v
    return default


def get_db_config() -> dict:
    """Build mysql-connector kwargs from the environment.

    Priority:
      1. DATABASE_URL / MYSQL_URL   e.g. mysql://user:pass@host:3306/dbname
         (Railway provides MYSQL_URL and MYSQL_PUBLIC_URL)
      2. DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME
         (falls back to Railway's MYSQLHOST / MYSQLPORT / ... names)
    """
    load_dotenv()  # no-op if there is no .env file

    url = _env('DATABASE_URL', 'MYSQL_URL')
    if url:
        p = urlparse(url)
        cfg = {
            'host': p.hostname,
            'port': p.port or 3306,
            'user': unquote(p.username or ''),
            'password': unquote(p.password or ''),
            'database': (p.path or '').lstrip('/'),
        }
    else:
        cfg = {
            'host': _env('DB_HOST', 'MYSQLHOST', default='localhost'),
            'port': int(_env('DB_PORT', 'MYSQLPORT', default='3306')),
            'user': _env('DB_USER', 'MYSQLUSER', default='root'),
            'password': _env('DB_PASSWORD', 'MYSQLPASSWORD', default=''),
            'database': _env('DB_NAME', 'MYSQLDATABASE', default='portfolio_db'),
        }
    cfg['connection_timeout'] = 30
    return cfg


def get_connection():
    cnx = mysql.connector.connect(**get_db_config())
    cur = cnx.cursor()
    cur.execute("SET time_zone = '+00:00'")  # store/compare timestamps in UTC everywhere
    cur.close()
    return cnx


def normalize_ticker(raw):
    """Uppercase/strip a ticker; return None if it isn't a plausible symbol."""
    t = str(raw or '').strip().upper()
    return t if TICKER_RE.match(t) else None


def queue_tickers(cur, tickers):
    """Add tickers to requested_assets (INSERT IGNORE, so existing requests are left alone).

    Needs only INSERT on requested_assets, so the limited app user can use it too.
    Returns (newly_queued, already_queued, invalid).
    """
    valid, invalid = [], []
    for raw in tickers:
        t = normalize_ticker(raw)
        if t is None:
            invalid.append(raw)
        elif t not in valid:
            valid.append(t)

    newly_queued = 0
    for t in valid:
        cur.execute("INSERT IGNORE INTO requested_assets (ticker) VALUES (%s)", (t,))
        newly_queued += cur.rowcount
    return newly_queued, len(valid) - newly_queued, invalid
