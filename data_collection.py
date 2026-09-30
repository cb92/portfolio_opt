import yfinance as yf
import mstarpy as ms
import pandas as pd
import numpy as np
import streamlit as st


"""
Set up data structure and common variables
"""
SECTORS = [
    'realestate', 'consumer_cyclical', 'basic_materials', 'consumer_defensive',
    'technology', 'communication_services', 'financial_services', 'utilities',
    'industrials', 'energy', 'healthcare',
]

ASSET_TYPES = [
    'domesticlargecap', 'domesticmidcap', 'domesticsmallcap',
    'intllargecap', 'intlmidcap', 'intlsmallcap',
    'emerging', 'other', 'cash',
]

STYLES = ['value', 'growth', 'blend']

TRAILING_RETURN_PERIODS = ['1y', '5y', '10y']
TRAILING_RETURN_YEARS = {'1y': 1, '5y': 5, '10y': 10}

PRICE_TARGET_STATS = ['median', 'mean', 'low', 'hi']

N_TOP_HOLDINGS = 10

# market-cap size thresholds (USD), used to bucket individual stocks
LARGE_CAP_THRESHOLD = 10e9
MID_CAP_THRESHOLD = 2e9

# --- build column list ---
data_cols = [
    # general info
    'ticker',
    'country',
    'expense_ratio',
    'long_business_summary',

    # sector percentages
    *[f'pct_sector__{s}' for s in SECTORS],

    # top holdings (ticker + pct for each of N_TOP_HOLDINGS positions)
    *[f'top_holding_{i}__ticker' for i in range(1, N_TOP_HOLDINGS + 1)],
    *[f'top_holding_{i}__pct' for i in range(1, N_TOP_HOLDINGS + 1)],

    # asset type percentages
    *[f'pct_asset__{a}' for a in ASSET_TYPES],

    # trailing returns
    *[f'trailing_return__{p}' for p in TRAILING_RETURN_PERIODS],

    # style percentages
    *[f'pct_style__{s}' for s in STYLES],

    # analyst info
    *[f'price_target__{s}' for s in PRICE_TARGET_STATS],
    'avg_analyst_rating',
]

# yfinance's individual-stock sectorKey uses hyphens; our columns use underscores,
# and yfinance spells "real-estate" with a hyphen where our bucket is "realestate" (no separator).
SECTOR_KEY_MAP = {
    'realestate': 'realestate',
    'real-estate': 'realestate',
    'consumer-cyclical': 'consumer_cyclical',
    'basic-materials': 'basic_materials',
    'consumer-defensive': 'consumer_defensive',
    'technology': 'technology',
    'communication-services': 'communication_services',
    'financial-services': 'financial_services',
    'utilities': 'utilities',
    'industrials': 'industrials',
    'energy': 'energy',
    'healthcare': 'healthcare',
}

# single shared mstarpy session, reused across all fund lookups rather than
# re-authenticating per ticker
MORNINGSTAR_SESSION = ms.MorningstarSession()


"""
Set up helper functions
"""
def normalize_weightings(weights: dict) -> dict:
    """Rescale a dict so its values sum to 1.0.
    Returns {} unchanged if weights is empty or sums to 0."""
    if not weights:
        return {}
    total = sum(v for v in weights.values() if v is not None)
    if not total:
        return {}
    return {k: (v / total if v is not None else None) for k, v in weights.items()}


def empty_row(ticker_symbol: str) -> dict:
    row = {col: None for col in data_cols}
    row['ticker'] = ticker_symbol
    return row

def compute_trailing_returns(hist: pd.DataFrame) -> dict:
    """Given a price history DataFrame with 'Date' and 'Close' columns,
    compute trailing total returns for each period in TRAILING_RETURN_PERIODS.

    For each period (e.g. '5y'), finds the trading date closest to
    (latest_date - N years) and computes latest_close / past_close - 1.
    Leaves a period as None if the history doesn't reach back far enough.
    """
    returns = {p: None for p in TRAILING_RETURN_PERIODS}
    if hist is None or hist.empty or 'Date' not in hist or 'Close' not in hist:
        return returns

    hist = hist.sort_values('Date').reset_index(drop=True)
    earliest_date = hist['Date'].iloc[0]
    latest_date = hist['Date'].iloc[-1]
    latest_close = hist['Close'].iloc[-1]

    for period in TRAILING_RETURN_PERIODS:
        years = TRAILING_RETURN_YEARS[period]
        target_date = latest_date - pd.DateOffset(years=years)

        if earliest_date > target_date:
            continue  # not enough history for this window — leave as None

        idx = (hist['Date'] - target_date).abs().idxmin()
        past_close = hist.loc[idx, 'Close']
        # get average return using CAGR
        if past_close:
            returns[period] = (latest_close / past_close)**(1/years) - 1

    return returns

def get_trailing_returns_for_ticker(t: yf.Ticker) -> dict:
    """Pull 12y of price history for a yfinance Ticker and compute trailing returns.
    Returns all-None dict if history is unavailable."""
    try:
        hist = t.history(period='12y').reset_index()  # 'Date' comes from the index
        return compute_trailing_returns(hist)
    except Exception:
        return {p: None for p in TRAILING_RETURN_PERIODS}

def classify_market_cap(market_cap) -> str | None:
    """Bucket a market cap (USD) into 'large' / 'mid' / 'small'."""
    if market_cap is None:
        return None
    if market_cap >= LARGE_CAP_THRESHOLD:
        return 'large'
    elif market_cap >= MID_CAP_THRESHOLD:
        return 'mid'
    else:
        return 'small'

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


def classify_stock_asset_type(info: dict) -> str | None:
    """Determine a single pct_asset__* bucket for an individual stock,
    based on country (domestic vs intl) and market-cap size.
    Returns None if either input is missing."""
    size = classify_market_cap(info.get('marketCap'))
    if size is None:
        return None
    origin = 'domestic' if info.get('country') == 'United States' else 'intl'
    return f'{origin}{size}cap'  # e.g. 'domesticlargecap', 'intlmidcap'


def compute_style_pcts(allocation: dict) -> dict:
    """Given an mstarpy allocationWeighting() dict, sum value/blend/growth
    across all three size tiers and normalize to fractions summing to 1.0."""
    if not allocation:
        return {s: None for s in STYLES}

    def f(key):
        return float(allocation.get(key, 0) or 0)

    raw = {
        'value': f('largeValue') + f('middleValue') + f('smallValue'),
        'blend': f('largeBlend') + f('middleBlend') + f('smallBlend'),
        'growth': f('largeGrowth') + f('middleGrowth') + f('smallGrowth'),
    }
    return normalize_weightings(raw)


def compute_asset_type_pcts(allocation: dict, regional: dict) -> dict:
    """Combine allocationWeighting() (size x style) with regionalSector()'s
    fundPortfolio (region) into pct_asset__* fractions.

    domesticLarge = (largeValue + largeBlend + largeGrowth) * northAmerica, etc.
    (both inputs converted from percent (0-100) to fraction (0-1) before multiplying)

    Returns a dict with keys: domesticlargecap, domesticmidcap, domesticsmallcap,
    intllargecap, intlmidcap, intlsmallcap, emerging. 'other'/'cash' are NOT
    computed here (no data source for those yet) — caller leaves them None.
    """
    empty = {
        'domesticlargecap': None, 'domesticmidcap': None, 'domesticsmallcap': None,
        'intllargecap': None, 'intlmidcap': None, 'intlsmallcap': None,
        'emerging': None,
    }
    if not allocation or not regional:
        return empty

    def f(key, d):
        return float(d.get(key, 0) or 0)

    large_frac = (f('largeValue', allocation) + f('largeBlend', allocation) + f('largeGrowth', allocation)) / 100
    mid_frac = (f('middleValue', allocation) + f('middleBlend', allocation) + f('middleGrowth', allocation)) / 100
    small_frac = (f('smallValue', allocation) + f('smallBlend', allocation) + f('smallGrowth', allocation)) / 100

    # NOTE: 'northAmerica' includes Canada, not just the US — treating it as
    # "domestic" is an approximation. Flag if you want US-only precision.
    domestic_frac = f('northAmerica', regional) / 100

    intl_developed_frac = (
        f('unitedKingdom', regional) + f('europeDeveloped', regional) +
        f('japan', regional) + f('australasia', regional) + f('asiaDeveloped', regional)
    ) / 100

    # NOTE: africaMiddleEast is bucketed into "emerging" here as an assumption —
    # Morningstar doesn't cleanly label it developed/emerging in this payload.
    emerging_frac = (
        f('europeEmerging', regional) + f('asiaEmerging', regional) +
        f('latinAmerica', regional) + f('africaMiddleEast', regional)
    ) / 100

    return {
        'domesticlargecap': large_frac * domestic_frac,
        'domesticmidcap': mid_frac * domestic_frac,
        'domesticsmallcap': small_frac * domestic_frac,
        'intllargecap': large_frac * intl_developed_frac,
        'intlmidcap': mid_frac * intl_developed_frac,
        'intlsmallcap': small_frac * intl_developed_frac,
        'emerging': emerging_frac,
    }


def build_stock_row(symbol: str) -> dict:
    row = empty_row(symbol)
    t = yf.Ticker(symbol)
    info = t.info or {}

    row['country'] = info.get('country')
    row['long_business_summary'] = info.get('longBusinessSummary')

    sector_key = info.get('sectorKey')
    mapped = SECTOR_KEY_MAP.get(sector_key)
    if mapped:
        col = f'pct_sector__{mapped}'
        if col in row:
            row[col] = 1.0  # a single stock is 100% "in" its own sector

    # --- asset type (single bucket, full weight) ---
    asset_bucket = classify_stock_asset_type(info)
    if asset_bucket:
        col = f'pct_asset__{asset_bucket}'
        if col in row:
            row[col] = 1.0
    # pct_style__* intentionally left None — value/growth/blend classification
    # isn't meaningful for a single stock the way it is for a fund


    # --- trailing returns ---
    trailing = get_trailing_returns_for_ticker(t)
    for period, val in trailing.items():
        row[f'trailing_return__{period}'] = val

    # --- analyst info ---
    row['price_target__median'] = info.get('targetMedianPrice')
    row['price_target__mean'] = info.get('targetMeanPrice')
    row['price_target__low'] = info.get('targetLowPrice')
    row['price_target__hi'] = info.get('targetHighPrice')
    row['avg_analyst_rating'] = info.get('averageAnalystRating')


    # expense_ratio and top_holding_* stay None — N/A for individual stocks
    return row


def build_fund_row(symbol: str) -> dict:
    row = empty_row(symbol)
    t = yf.Ticker(symbol)
    info = t.info or {}

    row['country'] = info.get('country')
    row['long_business_summary'] = info.get('longBusinessSummary')

    fd = t.funds_data

    # --- sector weightings ---
    raw_weights = getattr(fd, 'sector_weightings', None) or {}
    normalized = normalize_weightings(raw_weights)
    for sector, pct in normalized.items():
        col = f'pct_sector__{sector}'
        if col in row:
            row[col] = pct
        else:
            print(f'{symbol}: unrecognized sector key "{sector}"')

    # --- expense ratio ---
    try:
        row['expense_ratio'] = fd.fund_operations[symbol]['Annual Report Expense Ratio']
    except (KeyError, TypeError, AttributeError):
        pass  # leave as None if not available for this fund

    # --- top holdings ---
    try:
        holdings_df = fd.top_holdings
        if holdings_df is not None and not holdings_df.empty:
            sorted_holdings = holdings_df.sort_values('Holding Percent', ascending=False)
            for i, (sym, pct) in enumerate(sorted_holdings['Holding Percent'].items(), start=1):
                if i > N_TOP_HOLDINGS:
                    break
                row[f'top_holding_{i}__ticker'] = sym
                row[f'top_holding_{i}__pct'] = pct
    except (KeyError, TypeError, AttributeError):
        pass

    # --- trailing returns ---
    trailing = get_trailing_returns_for_ticker(t)
    for period, val in trailing.items():
        row[f'trailing_return__{period}'] = val

    # --- style (value/blend/growth) + asset type (domestic/intl/emerging x size) ---
    identifier = get_security_identifier(symbol)
    print(identifier)
    lookup_key = identifier if identifier else symbol  # fall back to raw symbol if lookup fails

    try:
        ms_fund = ms.Funds(lookup_key, session=MORNINGSTAR_SESSION)
        allocation = ms_fund.allocationWeighting() or {}
        regional_data = ms_fund.regionalSector() or {}
        regional = regional_data.get('fundPortfolio', {}) or {}

        style_pcts = compute_style_pcts(allocation)
        for style, pct in style_pcts.items():
            row[f'pct_style__{style}'] = pct

        asset_pcts = compute_asset_type_pcts(allocation, regional)
        for asset, pct in asset_pcts.items():
            col = f'pct_asset__{asset}'
            if col in row:
                row[col] = pct
        # pct_asset__other and pct_asset__cash stay None — no data source wired
        # up for these yet
    except Exception as e:
        print(f'{symbol}: failed to fetch mstarpy allocation/regional data: {e}')

    return row


rows = []

stocks = ['GOOG', 'MSFT', 'AAPL', 'BAC', 'SBUX', 'AMZN']
for s in stocks:
    try:
        rows.append(build_stock_row(s))
    except Exception as e:
        print(f'Failed to fetch stock {s}: {e}')
        rows.append(empty_row(s))

funds = ['VEIRX', 'TRBCX', 'DIA', 'QQQM', 'SWPPX','NCTWX','VTI','VEA',
        'SCHB','SCHD','VOO','ONEQ','VBR','XMHQ','XMMO','RWK','XSVM',
        'FNDF','IEFA','FEZ','SPEM','VWO','DGS','EEMS','IPAC','IEUR',
        'FXAIX','FSMDX','FSKAX']

for f in funds:
    try:
        rows.append(build_fund_row(f))
    except Exception as e:
        print(f'Failed to fetch fund {f}: {e}')
        rows.append(empty_row(f))

# --- assemble the DataFrame once, in one shot ---
ticker_data = pd.DataFrame(rows, columns=data_cols)
ticker_data.to_csv('test.csv', index = False)


def main():
    # Your core program logic goes here
    #st.text('hello world')  

if __name__ == "__main__":
    main()
