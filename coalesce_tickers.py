import re
import yfinance as yf
import pandas as pd

def get_underlying_benchmark(ticker_symbol: str) -> str:
    """
    Identifies the underlying index or benchmark for ETFs to group equivalent products.
    """
    info = yf.Ticker(ticker_symbol).info
    
    # Check explicit Yahoo underlying fields
    if index_sym := (info.get('underlyingSymbol') or info.get('underlyingIndexSymbol')):
        return index_sym.upper()

    # NLP fallback on business summary for popular indexes
    summary = info.get('longBusinessSummary', '')
    if re.search(r'Nasdaq-?100', summary, re.IGNORECASE):
        return 'BENCHMARK_NASDAQ100'
    elif re.search(r'S&P 500|Standard & Poor\'s 500', summary, re.IGNORECASE):
        return 'BENCHMARK_SP500'
    elif re.search(r'Total Stock Market|CRSP US Total', summary, re.IGNORECASE):
        return 'BENCHMARK_US_TOTAL_MARKET'

    return f"TICKER_{ticker_symbol}"

def get_max_coalesced_history(primary_ticker: str, history_donor_ticker: str) -> pd.DataFrame:
    """
    Pull full available history for a ticker (e.g., QQQM) by stitching earlier 
    data from an older sibling ticker (e.g., QQQ) prior to primary ticker's launch.
    """
    df_primary = yf.Ticker(primary_ticker).history(period="max")
    df_donor = yf.Ticker(history_donor_ticker).history(period="max")

    if df_primary.empty:
        return df_donor

    # Identify the earliest date of the primary ticker
    earliest_primary_date = df_primary.index.min()

    # Trim donor history to strictly before the primary ticker's inception
    df_donor_trimmed = df_donor[df_donor.index < earliest_primary_date].copy()

    # Combine historical donor data with primary actual data
    combined_history = pd.concat([df_donor_trimmed, df_primary])
    return combined_history

# Usage Example:
# QQQM launched in 2020; QQQ launched in 1999
entity_map = {t: get_underlying_benchmark(t) for t in ['QQQ','QQQM']}
print(entity_map)
qqqm_full_history = get_max_coalesced_history(primary_ticker="QQQM", history_donor_ticker="QQQ")
print(qqqm_full_history)

def get_company_entity_key(ticker_symbol: str) -> str:
    """
    Returns a unique entity key for a company to group related share classes.
    Primary identifier: SEC CIK number or first 6 chars of CUSIP.
    Fallback identifier: Normalized base name.
    """
    ticker = yf.Ticker(ticker_symbol)
    info = ticker.info

    # 1. First-class lookup: SEC CIK (unique per company)
    if cik := info.get('cik'):
        return f"CIK_{str(cik).zfill(10)}"

    # 2. Second-class lookup: Base CUSIP (first 6 characters)
    if cusip := info.get('cusip'):
        if len(cusip) >= 6:
            return f"CUSIP6_{cusip[:6].upper()}"

    # 3. Fallback: Standardized Company Name
    raw_name = info.get('longName') or info.get('shortName') or ""
    # Strip share class qualifiers (Class A, Class B, Common Stock, Inc, Corp, etc.)
    clean_name = re.sub(
        r'(?i)\b(class\s+[a-z]|cl\s+[a-z]|common\s+stock|inc\.?|corp\.?|corporation|ltd\.?|co\.?)\b', 
        '', 
        raw_name
    )
    clean_name = re.sub(r'[^\w\s]', '', clean_name).strip().upper()
    
    return f"NAME_{clean_name}" if clean_name else f"TICKER_{ticker_symbol}"

# Usage Example
tickers = ['GOOG', 'GOOGL', 'BRK-A', 'BRK-B']
entity_map = {t: get_company_entity_key(t) for t in tickers}
print(entity_map)
# Output:
# {'GOOG': 'CIK_0001652044', 'GOOGL': 'CIK_0001652044', ...}