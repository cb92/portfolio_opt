"""
portfolio_inputs.py

Pulls everything the optimizer needs out of MySQL and reshapes it into aligned
matrices/vectors. NO optimization happens here.

Conventions (so later cvxpy code is simple):
  * `tickers` is the single, ordered list of assets; every matrix has its columns in
    exactly this order, and the weights vector `w` has one entry per ticker.
  * `types`, `sectors`, `stocks`, 'styles', have rows = categories and columns = assets, so
    `matrix @ w` gives the portfolio's exposure to each category.

Run:  python portfolio_inputs.py
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd
from pypfopt import expected_returns, risk_models, plotting, efficient_frontier

from db import get_connection

# ---------------------------------------------------------------------------
# Parameters (starting values; we expect to tweak these)
# ---------------------------------------------------------------------------
RISK_AVERSION = 3.0          # q
MIN_WEIGHT = 0.0
MAX_WEIGHT = 1.0
MAX_SECTOR_PCT = 0.30
MAX_ASSET_TYPE_PCT = 0.25
MAX_STOCK_PCT = 0.03         
PCT_RISK_FREE = 0.0

PERIODS_PER_YEAR = 52        # weekly data
MIN_WEEKS_WARNING = 104      # warn if the common price window is shorter than ~2 years
COVERAGE_WARNING = 0.98      # warn if an asset's allocation rows sum to less than this

# Canonical categories (rows always appear, even if every asset is 0 there)
SECTORS = [
    'realestate', 'consumer_cyclical', 'basic_materials', 'consumer_defensive',
    'technology', 'communication_services', 'financial_services', 'utilities',
    'industrials', 'energy', 'healthcare',
]
ASSET_CLASSES = [
    'domesticlargecap', 'domesticmidcap', 'domesticsmallcap',
    'intllargecap', 'intlmidcap', 'intlsmallcap', 'emerging',
]

STYLES = ['value','growth','blend']


# ---------------------------------------------------------------------------
# Database -> raw DataFrames
# ---------------------------------------------------------------------------
def query_df(cur, sql, params=()):
    cur.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=cur.column_names)


def fetch_frames() -> dict:
    """One read-only pass over the database; returns plain DataFrames."""
    cnx = get_connection()
    cur = cnx.cursor()
    try:
        frames = {
            'assets': query_df(cur, "SELECT ticker, security_type FROM assets where security_type <> 'STOCK' ORDER BY ticker"),
            'expense': query_df(cur, "SELECT ticker, expense_ratio FROM asset_metadata"),
            'prices': query_df(cur, "SELECT ticker, price_date, close_price FROM asset_price_history"),
            'classes': query_df(cur, "SELECT ticker, asset_class, pct FROM asset_class_allocations"),
            'sectors': query_df(cur, "SELECT ticker, sector, pct FROM asset_sector_allocations"),
            'holdings': query_df(cur, "SELECT parent_ticker, holding_ticker, pct FROM asset_top_holdings"),
            'styles': query_df(cur, "SELECT ticker, style, pct FROM asset_style_allocations")
        }
    finally:
        cur.close()
        cnx.close()
    return frames


# ---------------------------------------------------------------------------
# Raw DataFrames -> aligned optimizer inputs
# ---------------------------------------------------------------------------
@dataclass
class PortfolioInputs:
    tickers: list              # ordered asset list (column order everywhere)
    expense_ratios: pd.Series  # ticker -> expense ratio (NaN if none)
    security_types: pd.Series  # ticker -> 'STOCK' / 'FUND'
    prices: pd.DataFrame       # weekly closes over the COMMON window, columns = tickers
    history_start: pd.Series   # ticker -> first available price date (before trimming)
    dropped: list              # assets excluded (no price history)
    types: pd.DataFrame        # asset classes  x assets
    sectors: pd.DataFrame      # sectors        x assets
    stocks: pd.DataFrame       # underlying stocks (fund top holdings + direct stocks) x assets
    styles: pd.DataFrame       # styles (value, growth, blend) x assets


def build_category_matrix(df, category_col, categories, tickers) -> pd.DataFrame:
    """Pivot (ticker, category, pct) rows into a categories x assets matrix of fractions."""
    if df.empty:
        m = pd.DataFrame(0.0, index=list(categories), columns=tickers)
    else:
        m = df.pivot_table(index=category_col, columns='ticker', values='pct',
                           aggfunc='sum', fill_value=0.0)
        m = m.reindex(columns=tickers, fill_value=0.0)
        extras = [c for c in m.index if c not in categories]
        m = m.reindex(index=list(categories) + extras, fill_value=0.0)
    m = m.astype(float)
    m.index.name = category_col
    return m


def build_stock_matrix(holdings, security_types, tickers) -> pd.DataFrame:
    """Look-through exposure matrix: rows = underlying tickers, columns = assets.

    Entry [h, a] = fraction of asset a that sits in underlying ticker h.
      * funds: from asset_top_holdings (top 10 only, so exposures are a LOWER bound)
      * direct stocks: 1.0 on their own row
    """
    h = holdings[holdings['parent_ticker'].isin(tickers)]
    if h.empty:
        m = pd.DataFrame(0.0, index=[], columns=tickers)
    else:
        m = h.pivot_table(index='holding_ticker', columns='parent_ticker', values='pct',
                          aggfunc='sum', fill_value=0.0)
        m = m.reindex(columns=tickers, fill_value=0.0)

    m = m.astype(float)
    for t in tickers:
        if security_types.get(t) == 'STOCK':
            if t not in m.index:
                m.loc[t] = 0.0
            m.loc[t, t] += 1.0
    m.index.name = 'underlying_ticker'
    m.columns.name = 'ticker'
    return m.sort_index()


def build_inputs(frames: dict) -> PortfolioInputs:
    assets = frames['assets']
    prices_long = frames['prices'].copy()

    if assets.empty or prices_long.empty:
        raise SystemExit('No assets/prices in the database yet. Run data_collection.py first.')

    prices_long['price_date'] = pd.to_datetime(prices_long['price_date'])
    prices_long['close_price'] = prices_long['close_price'].astype(float)
    wide = (prices_long.pivot(index='price_date', columns='ticker', values='close_price')
            .sort_index())

    all_assets = list(assets['ticker'])
    wide = wide.reindex(columns=all_assets)
    dropped = [t for t in all_assets if wide[t].notna().sum() == 0]
    wide = wide.drop(columns=dropped)

    history_start = wide.apply(lambda s: s.first_valid_index())

    # Common window: keep only dates where EVERY asset has a price. This avoids NaN
    # handling inside the estimators, at the cost of being limited by the youngest asset.
    prices = wide.dropna(how='any')
    tickers = list(prices.columns)
    security_types = assets.set_index('ticker')['security_type'].reindex(tickers)

    classes = frames['classes'].copy()
    sectors = frames['sectors'].copy()
    styles = frames['styles'].copy()
    holdings = frames['holdings'].copy()
    for df in (classes, sectors, styles, holdings):
        df['pct'] = df['pct'].astype(float)

    return PortfolioInputs(
        tickers=tickers,
        security_types=security_types,
        prices=prices,
        history_start=history_start,
        dropped=dropped,
        expense_ratios=frames['expense'].set_index('ticker')['expense_ratio'].astype(float).reindex(tickers),
        types=build_category_matrix(classes, 'asset_class', ASSET_CLASSES, tickers),
        sectors=build_category_matrix(sectors, 'sector', SECTORS, tickers),
        styles=build_category_matrix(styles, 'style', STYLES, tickers),
        stocks=build_stock_matrix(holdings, security_types, tickers),
    )


def load_inputs() -> PortfolioInputs:
    return build_inputs(fetch_frames())


def compute_estimates(prices: pd.DataFrame):
    """Expected returns (arithmetic mean, annualized) and two covariance estimates."""
    mu = expected_returns.mean_historical_return(
        prices, compounding=False, frequency=PERIODS_PER_YEAR)
    sample_cov = risk_models.sample_cov(prices, frequency=PERIODS_PER_YEAR)
    shrunk_cov = risk_models.CovarianceShrinkage(
        prices, frequency=PERIODS_PER_YEAR).ledoit_wolf()
    return mu, sample_cov, shrunk_cov


# ---------------------------------------------------------------------------
# Demo / smoke test
# ---------------------------------------------------------------------------
def show_breakdown(title, matrix, w):
    exposure = matrix.dot(w)
    print(f'\n=== {title} matrix (rows = categories, columns = assets) ===')
    print(matrix)
    print(f'\n{title} breakdown of portfolio (matrix @ weights):')
    print(exposure)
    print(f'Unallocated (weights not covered by {title} data): {w.sum() - exposure.sum():.4f}')

    coverage = matrix.sum()
    thin = coverage[coverage < COVERAGE_WARNING]
    if len(thin):
        print(f'WARNING: {title} data covers < {COVERAGE_WARNING:.0%} of these assets:')
        print(thin.to_string())
    return exposure


def main():
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    pd.set_option('display.max_rows', 120)
    pd.set_option('display.float_format', lambda x: f'{x:.4f}')

    data = load_inputs()
    tickers = data.tickers
    n = len(tickers)

    print(f'{n} assets: {tickers}')
    if data.dropped:
        print(f'Excluded (no price history): {data.dropped}')
    print(f'Common price window: {data.prices.index[0].date()} to {data.prices.index[-1].date()} '
          f'({len(data.prices)} weekly closes)')
    latest = data.history_start.sort_values(ascending=False).head(3)
    print('Assets limiting the window (latest history start):')
    print(latest.to_string())
    if len(data.prices) < MIN_WEEKS_WARNING:
        print(f'WARNING: fewer than {MIN_WEEKS_WARNING} weekly observations; estimates will be noisy.')

    # --- starting point ---
    w = np.full(n, (1 - PCT_RISK_FREE) / n)   # equal weights; sums to 1 - pct_risk_free
    print(f'\nParameters: risk_aversion={RISK_AVERSION}, min_weight={MIN_WEIGHT}, max_weight={MAX_WEIGHT}, '
          f'max_sector_pct={MAX_SECTOR_PCT}, max_asset_type={MAX_ASSET_TYPE_PCT}, '
          f'max_stock_pct={MAX_STOCK_PCT}, pct_risk_free={PCT_RISK_FREE}')
    print(f'Initial weights: 1/n = {w[0]:.4f} each (sum = {w.sum():.4f})')

    # --- asset types, sectors, stock look-through ---
    show_breakdown('Asset type', data.types, w)
    show_breakdown('Sector', data.sectors, w)
    show_breakdown('Style', data.styles, w)

    stocks = data.stocks
    print(f'\n=== Stock look-through matrix: {stocks.shape[0]} underlying tickers x {stocks.shape[1]} assets ===')
    print('(top holdings only, so exposures are a lower bound; showing the 15 largest)')
    exposure = stocks.dot(w).sort_values(ascending=False)
    print(stocks.loc[exposure.index].head(15))
    print('\nStock exposure of portfolio (matrix @ weights), top 15:')
    print(exposure.head(15))
    print(f'Underlying tickers above the {MAX_STOCK_PCT:.0%} cap: {(exposure > MAX_STOCK_PCT).sum()}')

    # --- expected returns and covariance ---
    mu, sample_cov, shrunk_cov = compute_estimates(data.prices)
    print('\n=== Expected returns (annualized arithmetic mean of weekly returns) ===')
    print(mu)
    print('\n=== Sample covariance (annualized) ===')
    print(sample_cov)
    print('\n=== Ledoit-Wolf shrunk covariance (annualized) ===')
    print(shrunk_cov)
    for name, cov in (('sample', sample_cov), ('shrunk', shrunk_cov)):
        print(f'Smallest eigenvalue, {name} covariance: {np.linalg.eigvalsh(cov.values).min():.2e}')

    ef = efficient_frontier.EfficientFrontier(mu, shrunk_cov)
    ef.add_constraint(lambda x: data.sectors.values @ x <= 0.2)
    weights = ef.max_sharpe()
    print(weights)
    ef.portfolio_performance(verbose=True)

if __name__ == '__main__':
    main()