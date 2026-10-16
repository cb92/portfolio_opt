"""
data_collection.py  (general-purpose collector)
 
Processes the requested_assets queue:
    pending -> processing -> done | failed
For each pending ticker it detects whether it is a stock or a fund, fetches data from
yfinance (+ Morningstar for funds), upserts it into the asset tables, and updates the
request row (status, error_message, processed_at).
 
Run order:
    1. Create the database and run setup.sql
    2. Copy .env.example -> .env and fill in credentials
    3. python seed_requests.py          (one-off: queue the starting tickers)
    4. python data_collection.py        (process everything pending)
 
Useful flags:
    --limit N             process at most N pending requests this run
    --retry-failed        move failed requests back to pending first
    --reset-stuck         move requests stuck in 'processing' (crashed run) back to pending
    --requeue-stale DAYS  re-queue done assets whose data is older than DAYS days (refresh)
 
Re-running is safe: upserts keep created_at and bump updated_at.

NOTE: this is standalone code that is run on a local machine to populate the database, 
and is NOT part of the production code.
"""
import argparse
import math
import re
import sys
 
import mstarpy as ms
import pandas as pd
import yfinance as yf
 
from db import get_connection
import config


_MSTAR_SESSION = None

"""
Set up helper functions
"""
def to_num(x, places=4, limit=None):
    """Convert numpy/str/etc. to a plain rounded float, or None if unusable / out of range."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if limit is not None and abs(v) >= limit:
        return None
    return round(v, places)
 
def parse_rating(x):
    """yfinance's averageAnalystRating looks like '1.8 - Buy'; keep the number."""
    if x is None:
        return None
    m = re.match(r'\s*(\d+(?:\.\d+)?)', str(x))
    return to_num(m.group(1), 2, 10) if m else None

def normalize_weightings(weights: dict) -> dict:
    """Rescale a dict so its values sum to 1.0.
    Returns {} unchanged if weights is empty or sums to 0."""
    if not weights:
        return {}
    total = sum(v for v in weights.values() if v is not None)
    if not total:
        return {}
    return {k: (v / total if v is not None else None) for k, v in weights.items()}

def get_morningstar_session():
    """Lazily create one shared mstarpy session (no network call at import time)."""
    global _MSTAR_SESSION
    if _MSTAR_SESSION is None:
        _MSTAR_SESSION = ms.MorningstarSession()
    return _MSTAR_SESSION
 
# ---------------------------------------------------------------------------
# Price history + trailing returns
# ---------------------------------------------------------------------------

def fetch_weekly_history(t: yf.Ticker, parent_ticker = None):
    """Weekly closes for the last HISTORY_PERIOD as a DataFrame[Date, Close], or None."""   
    try:
        if parent_ticker:
            hist = yf.Ticker(parent_ticker).history(period=config.HISTORY_PERIOD, interval=config.HISTORY_INTERVAL).reset_index()
        else:
            hist = t.history(period=config.HISTORY_PERIOD, interval=config.HISTORY_INTERVAL).reset_index()
    except Exception as e:
        print(f'  history fetch failed: {e}')
        return None
    if hist is None or hist.empty or 'Date' not in hist or 'Close' not in hist:
        return None
    hist['Date'] = pd.to_datetime(hist['Date']).dt.tz_localize(None).dt.normalize()
    hist = hist.dropna(subset=['Close'])
    return (hist[['Date', 'Close']]
            .drop_duplicates('Date')
            .sort_values('Date')
            .reset_index(drop=True))


def compute_trailing_returns(hist) -> dict:
    """Given a price history DataFrame with 'Date' and 'Close' columns,
    compute trailing total returns for each period in config.TRAILING_RETURN_PERIODS.

    For each period (e.g. '5y'), finds the trading date closest to
    (latest_date - N years) and computes latest_close / past_close - 1.
    Leaves a period as None if the history doesn't reach back far enough.
    """
    returns = {p: None for p in config.TRAILING_RETURN_PERIODS}
    if hist is None or hist.empty:
        return returns

    earliest = hist['Date'].iloc[0]
    latest = hist['Date'].iloc[-1]
    latest_close = hist['Close'].iloc[-1]

    for period in config.TRAILING_RETURN_PERIODS:
        years = config.TRAILING_RETURN_YEARS[period]
        target = latest - pd.DateOffset(years=years)

        # weekly bars: allow up to one week of slack at the start of the window
        if earliest > target + pd.Timedelta(days=28):
            continue  # not enough history for this window — leave as None

        idx = (hist['Date'] - target).abs().idxmin()
        past_close = hist.loc[idx, 'Close']
        # get average return using CAGR
        if past_close:
            returns[period] = (latest_close / past_close)**(1/years) - 1
    return returns

def history_rows(hist) -> list:
    if hist is None:
        return []
    out = []
    for d, c in zip(hist['Date'], hist['Close']):
        c = to_num(c, 4, config.CLOSE_LIMIT)
        if c is not None:
            out.append((d.date(), c))
    return out

# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def classify_market_cap(market_cap) -> str | None:
    """Bucket a market cap (USD) into 'large' / 'mid' / 'small'."""
    if market_cap is None:
        return None
    if market_cap >= config.LARGE_CAP_THRESHOLD:
        return 'large'
    elif market_cap >= config.MID_CAP_THRESHOLD:
        return 'mid'
    else:
        return 'small'

def classify_stock_asset_class(info: dict) -> str | None:
    """Determine a single pct_asset__* bucket for an individual stock,
    based on country (domestic vs intl) and market-cap size.
    Returns None if either input is missing."""
    size = classify_market_cap(info.get('marketCap'))
    if size is None:
        return None
    origin = 'domestic' if info.get('country') == 'United States' else 'intl'
    return f'{origin}{size}cap'  # e.g. 'domesticlargecap', 'intlmidcap'

def get_security_identifier(symbol: str) -> str | None:
    """Look up a security's ISIN via yfinance, for use with ms.Funds()
    instead of a raw ticker symbol. Returns None if unavailable."""
    try:
        isin = yf.Ticker(symbol).isin
        if isin and isin != '-':  # yfinance sometimes returns '-' for unavailable ISINs
            return isin
    except Exception as e:
        print(f'{symbol}: ISIN lookup failed ({e})')
    return None

def compute_style_pcts(allocation: dict) -> dict:
    """Given an mstarpy allocationWeighting() dict, sum value/blend/growth
    across all three size tiers and normalize to fractions summing to 1.0."""
    if not allocation:
        return {}

    def f(key):
        return float(allocation.get(key, 0) or 0)

    raw = {
        'value': f('largeValue') + f('middleValue') + f('smallValue'),
        'blend': f('largeBlend') + f('middleBlend') + f('smallBlend'),
        'growth': f('largeGrowth') + f('middleGrowth') + f('smallGrowth'),
    }
    return normalize_weightings(raw)

def compute_asset_class_pcts(allocation: dict, regional: dict) -> dict:
    """Combine allocationWeighting() (size x style) with regionalSector()'s
    fundPortfolio (region) into pct_asset__* fractions.

    domesticLarge = (largeValue + largeBlend + largeGrowth) * northAmerica, etc.
    (both inputs converted from percent (0-100) to fraction (0-1) before multiplying)

    Returns a dict with keys: domesticlargecap, domesticmidcap, domesticsmallcap,
    intllargecap, intlmidcap, intlsmallcap, emerging. 'other'/'cash' are NOT
    computed here (no data source for those yet) — caller leaves them None.
    """
    if not allocation or not regional:
        return empty

    def f(key, d):
        return float(d.get(key, 0) or 0)

    large = (f('largeValue', allocation) + f('largeBlend', allocation) + f('largeGrowth', allocation)) / 100
    mid = (f('middleValue', allocation) + f('middleBlend', allocation) + f('middleGrowth', allocation)) / 100
    small = (f('smallValue', allocation) + f('smallBlend', allocation) + f('smallGrowth', allocation)) / 100

    # NOTE: 'northAmerica' includes Canada, not just the US — treating it as
    # "domestic" is an approximation.
    domestic = f('northAmerica', regional) / 100

    intl_developed = (
        f('unitedKingdom', regional) + f('europeDeveloped', regional) +
        f('japan', regional) + f('australasia', regional) + f('asiaDeveloped', regional)
    ) / 100

    # NOTE: africaMiddleEast is bucketed into "emerging" here as an assumption —
    # Morningstar doesn't cleanly label it developed/emerging in this payload.
    emerging = (
        f('europeEmerging', regional) + f('asiaEmerging', regional) +
        f('latinAmerica', regional) + f('africaMiddleEast', regional)
    ) / 100

    return {
        'domesticlargecap': large * domestic,
        'domesticmidcap': mid * domestic,
        'domesticsmallcap': small * domestic,
        'intllargecap': large * intl_developed,
        'intlmidcap': mid * intl_developed,
        'intlsmallcap': small * intl_developed,
        'emerging': emerging,
    }

# ---------------------------------------------------------------------------
# Fetch layer: returns a plain "record" dict (no DB access here)
# ---------------------------------------------------------------------------

def new_record(symbol: str, security_type: str) -> dict:
    return {
        'ticker': symbol,
        'security_type': security_type,
        'country': None,
        'long_business_summary': None,
        'metadata': {},
        'sectors': {},
        'classes': {},
        'styles': {},
        'holdings': [],   # list of (holding_ticker, rank, pct)
        'prices': [],     # list of (date, close)
    }

def build_stock(symbol: str, t: yf.Ticker, info: dict) -> dict:
    rec = new_record(symbol, 'STOCK')
    rec['country'] = info.get('country')
    rec['long_business_summary'] = info.get('longBusinessSummary')
    rec['longName'] = info.get('longName')[:50]
    rec['website'] = info.get('website')
    rec['parentTicker'] = None
 
    sector = config.SECTOR_KEY_MAP.get(info.get('sectorKey'))
    if sector:
        rec['sectors'][sector] = 1.0
 
    asset_class = classify_stock_asset_class(info)
    if asset_class:
        rec['classes'][asset_class] = 1.0
    # styles intentionally empty for single stocks
 
    hist = fetch_weekly_history(t)
    trailing = compute_trailing_returns(hist)
    rec['prices'] = history_rows(hist)
 
    rec['metadata'] = {
        'expense_ratio': None,
        'trailing_return_1y': to_num(trailing['1y'], 4, config.RETURN_LIMIT),
        'trailing_return_5y': to_num(trailing['5y'], 4, config.RETURN_LIMIT),
        'trailing_return_10y': to_num(trailing['10y'], 4, config.RETURN_LIMIT),
        'price_target_median': to_num(info.get('targetMedianPrice'), 2, config.PRICE_LIMIT),
        'price_target_mean': to_num(info.get('targetMeanPrice'), 2, config.PRICE_LIMIT),
        'price_target_low': to_num(info.get('targetLowPrice'), 2, config.PRICE_LIMIT),
        'price_target_hi': to_num(info.get('targetHighPrice'), 2, config.PRICE_LIMIT),
        'avg_analyst_rating': parse_rating(info.get('averageAnalystRating')),
    }
    return rec


def build_fund(symbol: str, t: yf.Ticker, info: dict) -> dict:
    rec = new_record(symbol, 'FUND')
    rec['country'] = info.get('country')
    rec['long_business_summary'] = info.get('longBusinessSummary')
    rec['longName'] = info.get('longName')[:50]
    rec['website'] = info.get('website')
    if symbol in list(config.MINIMAP):
        rec['parentTicker'] = config.MINIMAP.get(symbol)
    else: 
        rec['parentTicker'] = None

 
    fd = t.funds_data
 
    # sectors
    raw_weights = getattr(fd, 'sector_weightings', None) or {}
    for sector, pct in normalize_weightings(raw_weights).items():
        if sector in config.SECTORS:
            rec['sectors'][sector] = pct
        else:
            print(f'  unrecognized sector key "{sector}"')
 
    # expense ratio
    expense_ratio = None
    try:
        expense_ratio = fd.fund_operations[symbol]['Annual Report Expense Ratio']
    except (KeyError, TypeError, AttributeError):
        pass
 
    # top holdings
    try:
        holdings_df = fd.top_holdings
        if holdings_df is not None and not holdings_df.empty:
            ranked = holdings_df.sort_values('Holding Percent', ascending=False)
            for i, (sym, pct) in enumerate(ranked['Holding Percent'].items(), start=1):
                if i > config.N_TOP_HOLDINGS:
                    break
                pct = to_num(pct, 4, config.PCT_LIMIT)
                if pct is not None:
                    rec['holdings'].append((str(sym)[:10], i, pct))
    except (KeyError, TypeError, AttributeError):
        pass
 
    # prices + trailing returns
    hist = fetch_weekly_history(t, rec['parentTicker'])
    trailing = compute_trailing_returns(hist)
    rec['prices'] = history_rows(hist)
 
    # Morningstar: style + asset class
    lookup_key = get_security_identifier(symbol) or symbol
    try:
        ms_fund = ms.Funds(lookup_key, session=get_morningstar_session())
        allocation = ms_fund.allocationWeighting() or {}
        regional_data = ms_fund.regionalSector() or {}
        regional = regional_data.get('fundPortfolio', {}) or {}
 
        for style, pct in compute_style_pcts(allocation).items():
            pct = to_num(pct, 4, config.PCT_LIMIT)
            if pct is not None:
                rec['styles'][style] = pct
        for asset_class, pct in compute_asset_class_pcts(allocation, regional).items():
            pct = to_num(pct, 4, config.PCT_LIMIT)
            if pct is not None:
                rec['classes'][asset_class] = pct
    except Exception as e:
        print(f'  failed to fetch mstarpy allocation/regional data: {e}')
 
    rec['metadata'] = {
        'expense_ratio': to_num(expense_ratio, 4, config.PCT_LIMIT),
        'trailing_return_1y': to_num(trailing['1y'], 4, config.RETURN_LIMIT),
        'trailing_return_5y': to_num(trailing['5y'], 4, config.RETURN_LIMIT),
        'trailing_return_10y': to_num(trailing['10y'], 4, config.RETURN_LIMIT),
        'price_target_median': None,
        'price_target_mean': None,
        'price_target_low': None,
        'price_target_hi': None,
        'avg_analyst_rating': None,
    }
    return rec


def fetch_asset(symbol: str) -> dict:
    """Detect stock vs fund from Yahoo's quoteType, then build the full record.
    Raises (and the caller marks the request failed) if the ticker is unknown,
    unsupported, or has no price history."""
    t = yf.Ticker(symbol)
    info = t.info or {}
 
    quote_type = info.get('quoteType')
    if not quote_type:
        raise LookupError('ticker not found on Yahoo Finance')
    kind = config.QUOTE_TYPE_TO_KIND.get(quote_type)
    if kind is None:
        raise ValueError(f'unsupported security type "{quote_type}"')
 
    rec = build_stock(symbol, t, info) if kind == 'STOCK' else build_fund(symbol, t, info)
 
    if not rec['prices']:
        raise ValueError('no price history available')
    return rec

# ---------------------------------------------------------------------------
# Write layer: functions to update the appropriate tables based on aggregated info using above fxns
# ---------------------------------------------------------------------------
def sync_children(cur, table, parent_col, ticker, key_col, records, value_cols):
    """Upsert `records` (list of tuples: key, *values) for one parent, then delete rows
    for that parent whose key is no longer present. Does nothing if `records` is empty,
    so a failed fetch never wipes previously good data.
 
    created_at is preserved on update; updated_at is always bumped."""
    if not records:
        return
    cols = [parent_col, key_col] + value_cols
    placeholders = ', '.join(['%s'] * len(cols))
    updates = ', '.join(f'{c} = VALUES({c})' for c in value_cols)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON DUPLICATE KEY UPDATE {updates}, updated_at = CURRENT_TIMESTAMP")
    cur.executemany(sql, [(ticker, *r) for r in records])
 
    keys = [r[0] for r in records]
    in_list = ', '.join(['%s'] * len(keys))
    cur.execute(f"DELETE FROM {table} WHERE {parent_col} = %s AND {key_col} NOT IN ({in_list})",
                (ticker, *keys))
 
 
def save_record(cur, rec: dict, price_chunk: int = 1000):
    ticker = rec['ticker']
 
    # 1. parent row (must exist before any child row because of the foreign keys)
    cur.execute(
        """INSERT INTO assets (ticker, parent_ticker, security_type, website, longname, country, long_business_summary)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON DUPLICATE KEY UPDATE
               parent_ticker = COALESCE(VALUES(parent_ticker), parent_ticker),
               security_type = VALUES(security_type),
               website = COALESCE(VALUES(website), website),
               longname = COALESCE(VALUES(longname), longname),
               country = COALESCE(VALUES(country), country),
               long_business_summary = COALESCE(VALUES(long_business_summary), long_business_summary),
               updated_at = CURRENT_TIMESTAMP""",
        (ticker, rec['parentTicker'], rec['security_type'], rec['website'], rec['longName'], rec['country'], rec['long_business_summary']),
    )
 
    # 2. metadata (a NULL from a flaky fetch keeps the previous value)
    m = rec['metadata']
    meta_cols = ['expense_ratio', 'trailing_return_1y', 'trailing_return_5y', 'trailing_return_10y',
                 'price_target_median', 'price_target_mean', 'price_target_low', 'price_target_hi',
                 'avg_analyst_rating']
    updates = ', '.join(f'{c} = COALESCE(VALUES({c}), {c})' for c in meta_cols)
    cur.execute(
        f"""INSERT INTO asset_metadata (ticker, {', '.join(meta_cols)})
            VALUES (%s, {', '.join(['%s'] * len(meta_cols))})
            ON DUPLICATE KEY UPDATE {updates}, updated_at = CURRENT_TIMESTAMP""",
        (ticker, *[m.get(c) for c in meta_cols]),
    )
 
    # 3. allocations
    sync_children(cur, 'asset_sector_allocations', 'ticker', ticker, 'sector',
                  [(k, to_num(v, 4, config.PCT_LIMIT)) for k, v in rec['sectors'].items() if v is not None], ['pct'])
    sync_children(cur, 'asset_class_allocations', 'ticker', ticker, 'asset_class',
                  [(k, to_num(v, 4, config.PCT_LIMIT)) for k, v in rec['classes'].items() if v is not None], ['pct'])
    sync_children(cur, 'asset_style_allocations', 'ticker', ticker, 'style',
                  [(k, to_num(v, 4, config.PCT_LIMIT)) for k, v in rec['styles'].items() if v is not None], ['pct'])
 
    # 4. top holdings: records are (holding_ticker, rank, pct)
    sync_children(cur, 'asset_top_holdings', 'parent_ticker', ticker, 'holding_ticker',
                  rec['holdings'], ['holding_rank', 'pct'])
 
    # 5. weekly prices 
    prices = rec['prices']
    for i in range(0, len(prices), price_chunk):
        chunk = prices[i:i + price_chunk]
        cur.executemany(
            """INSERT INTO asset_price_history (ticker, price_date, close_price)
               VALUES (%s, %s, %s)
               ON DUPLICATE KEY UPDATE close_price = VALUES(close_price)""",
            [(ticker, d, c) for d, c in chunk],
        )
 
# ---------------------------------------------------------------------------
# Queue layer: requested_assets
# ---------------------------------------------------------------------------
def requeue_failed(cur) -> int:
    cur.execute("""UPDATE requested_assets
                   SET request_status = 'pending', error_message = NULL, processed_at = NULL
                   WHERE request_status = 'failed'""")
    return cur.rowcount
 
 
def reset_stuck(cur) -> int:
    cur.execute("""UPDATE requested_assets
                   SET request_status = 'pending', error_message = NULL, processed_at = NULL
                   WHERE request_status = 'processing'""")
    return cur.rowcount
 
 
def requeue_stale(cur, days: int) -> int:
    """Re-queue 'done' requests whose asset data hasn't been refreshed in `days` days."""
    cur.execute("""UPDATE requested_assets r
                   JOIN asset_metadata m ON m.ticker = r.ticker
                   SET r.request_status = 'pending', r.error_message = NULL, r.processed_at = NULL
                   WHERE r.request_status = 'done'
                     AND m.updated_at < NOW() - INTERVAL %s DAY""", (days,))
    return cur.rowcount
 
 
def get_pending(cur, limit=None) -> list:
    sql = "SELECT ticker FROM requested_assets WHERE request_status = 'pending' ORDER BY requested_at, ticker"
    params = ()
    if limit:
        sql += " LIMIT %s"
        params = (limit,)
    cur.execute(sql, params)
    return [row[0] for row in cur.fetchall()]
 
 
def claim_request(cnx, cur, ticker: str) -> bool:
    """Atomically move pending -> processing. False if someone else already took it."""
    cur.execute("""UPDATE requested_assets
                   SET request_status = 'processing', error_message = NULL
                   WHERE ticker = %s AND request_status = 'pending'""", (ticker,))
    claimed = cur.rowcount == 1
    cnx.commit()
    return claimed
 
 
def finish_request(cur, ticker: str, status: str, error=None):
    cur.execute("""UPDATE requested_assets
                   SET request_status = %s, error_message = %s, processed_at = CURRENT_TIMESTAMP
                   WHERE ticker = %s""", (status, error, ticker))
 
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Process the requested_assets queue.')
    parser.add_argument('--limit', type=int, help='process at most N pending requests')
    parser.add_argument('--retry-failed', action='store_true', help='re-queue failed requests first')
    parser.add_argument('--reset-stuck', action='store_true',
                        help="re-queue requests stuck in 'processing' (e.g. after a crashed run)")
    parser.add_argument('--requeue-stale', type=int, metavar='DAYS',
                        help='re-queue done assets whose data is older than DAYS days')
    args = parser.parse_args()
 
    cnx = get_connection('ADMIN_')
    cur = cnx.cursor()
 
    if args.reset_stuck:
        print(f'Reset {reset_stuck(cur)} stuck request(s)')
    if args.retry_failed:
        print(f'Re-queued {requeue_failed(cur)} failed request(s)')
    if args.requeue_stale is not None:
        print(f'Re-queued {requeue_stale(cur, args.requeue_stale)} stale request(s)')
    cnx.commit()
 
    pending = get_pending(cur, args.limit)
    print(f'{len(pending)} pending request(s)')
 
    done, failed = [], []
    for ticker in pending:
        if not claim_request(cnx, cur, ticker):
            print(f'{ticker}: already claimed, skipping')
            continue
        try:
            print(f'{ticker}: fetching...')
            rec = fetch_asset(ticker)
            save_record(cur, rec)
            finish_request(cur, ticker, 'done')
            cnx.commit()  # data and 'done' status land together
            done.append(ticker)
            print(f'{ticker}: done ({rec["security_type"]}, {len(rec["prices"])} weekly prices)')
        except Exception as e:
            cnx.rollback()
            message = f'{type(e).__name__}: {e}'[:500]
            finish_request(cur, ticker, 'failed', message)
            cnx.commit()
            failed.append(ticker)
            print(f'{ticker}: FAILED ({message})')
 
    cur.close()
    cnx.close()
 
    print(f'\nFinished: {len(done)} done, {len(failed)} failed')
    if failed:
        print(f'Failed tickers: {", ".join(failed)}')
        sys.exit(1)  # non-zero exit so any scheduler flags the run
 
 
if __name__ == '__main__':
    main()
