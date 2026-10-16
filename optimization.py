"""
optimization.py

Pulls data out of the database, aligns it, and runs the max-Sharpe optimization.
Importable from app.py: every function either returns data or raises an exception.
Nothing in here prints (except the CLI at the bottom), calls plt.show(), or exits.

Typical use from an app:

    frames = fetch_frames()                        # one DB read (wrap in st.cache_data)
    data   = build_inputs(frames, tickers=picked)  # cheap; re-run when the selection changes
    result = optimize(data, Constraints(sector_max=0.30))
    result.positions, result.sectors, result.sharpe, ...
    frontier = efficient_frontier_points(data, Constraints(sector_max=0.30))

Conventions:
  * `data.tickers` is the single ordered asset list; every matrix has its columns in
    exactly this order, and weights are aligned to it.
  * `types`, `sectors`, `stocks`, `styles` have rows = categories and columns = assets,
    so `matrix @ w` gives the portfolio's exposure to each category.
  * If Constraints.pct_risk_free > 0, a fixed-weight 'CASH' asset is added to the problem,
    so all bounds stay percentages of the TOTAL portfolio.

Constraints (all expressed on the weights vector, rows of each matrix @ w):
  * single stock <= MAX_STOCK_PCT of the whole portfolio (direct holdings + fund top holdings)
  * every sector between SECTOR_MIN and SECTOR_MAX
  * every asset class inside ASSET_CLASS_BOUNDS
  * weights between MIN_WEIGHT and MAX_WEIGHT, summing to 1 (pypfopt default)

Run:  python optimization.py
"""
import sys
from dataclasses import dataclass, field

import cvxpy as cp
import numpy as np
import pandas as pd
from pypfopt import expected_returns, risk_models, efficient_frontier
from pypfopt.exceptions import OptimizationError

from db import get_connection
import config

CASH = 'CASH'

# ---------------------------------------------------------------------------
# Errors and parameter objects
# ---------------------------------------------------------------------------
class NoDataError(ValueError):
    """The database has no usable assets/prices for the requested selection."""


class OptimizationFailed(RuntimeError):
    """The solver could not find a portfolio (usually: constraints can't all be met)."""

    def __init__(self, message, warnings=()):
        super().__init__(message)
        self.warnings = list(warnings)


@dataclass
class Constraints:
    """Everything the user can tweak. Defaults come from config.py; override per run with
    e.g. dataclasses.replace(Constraints(), sector_max=0.30) or Constraints(sector_max=0.30)."""
    min_weight: float = config.MIN_WEIGHT
    max_weight: float = config.MAX_WEIGHT
    max_stock_pct: float = config.MAX_STOCK_PCT
    sector_min: float = config.SECTOR_MIN
    sector_max: float = config.SECTOR_MAX
    class_bounds: dict = field(default_factory=lambda: dict(config.ASSET_CLASS_BOUNDS))
    risk_free_rate: float = config.RISK_FREE_RATE   # annual rate used by max_sharpe
    pct_risk_free: float = config.PCT_RISK_FREE     # share of the portfolio held in cash


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
            'assets': query_df(cur, "SELECT a.ticker, a.security_type, r.top_asset_class \
                                    FROM (\
                                        SELECT ticker, asset_class as top_asset_class \
                                        FROM (SELECT ticker, asset_class, pct, \
                                            rank() over(partition by ticker order by pct desc) as rnk \
                                            FROM asset_class_allocations) b WHERE rnk = 1) r \
                                    JOIN assets a USING(ticker);\
                                    "),
            'expense': query_df(cur, "SELECT ticker, expense_ratio FROM asset_metadata"),
            'metadata': query_df(cur, "SELECT ticker, security_type, longname, top_asset_class, expense_ratio, price_target_median, price_target_mean, \
                                        price_target_low, price_target_hi, avg_analyst_rating \
                                        FROM asset_metadata \
                                        join assets using(ticker)\
                                        join (\
                                        SELECT ticker, asset_class as top_asset_class \
                                        FROM (SELECT ticker, asset_class, pct, \
                                            rank() over(partition by ticker order by pct desc) as rnk \
                                            FROM asset_class_allocations) b WHERE rnk = 1) r using(ticker)"),
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
    metadata: pd.DataFrame     # interesting asset level information to be printed/examined
    history_start: pd.Series   # ticker -> first available price date (before trimming)
    dropped: list              # assets excluded (no price history)
    types: pd.DataFrame        # asset classes  x assets
    sectors: pd.DataFrame      # sectors        x assets
    stocks: pd.DataFrame       # underlying stocks (fund top holdings + direct stocks) x assets
    styles: pd.DataFrame       # styles (value, growth, blend) x assets

def window_summary(data: PortfolioInputs, k=3) -> dict:
    """Price-window facts for display: dates, length, and which assets limit it."""
    return {
        'start': data.prices.index[0].date(),
        'end': data.prices.index[-1].date(),
        'weeks': len(data.prices),
        'limiting_assets': data.history_start.sort_values(ascending=False).head(k),
    }


def data_quality_warnings(data: PortfolioInputs) -> list:
    """Human-readable data problems worth showing next to any result."""
    out = []
    if data.dropped:
        out.append(f'Excluded (no price history): {", ".join(data.dropped)}')
    if len(data.prices) < config.MIN_WEEKS_WARNING:
        out.append(f'Only {len(data.prices)} weekly observations; estimates will be noisy.')
    for title, matrix in (('Asset class', data.types), ('Sector', data.sectors), ('Style', data.styles)):
        coverage = matrix.sum()
        thin = coverage[coverage < config.COVERAGE_WARNING]
        if len(thin):
            out.append(f'{title} data covers < {config.COVERAGE_WARNING:.0%} of: {", ".join(thin.index)}')
    return out


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

def sharpe_ratio(prices: pd.Series, risk_free_rate: float = config.RISK_FREE_RATE) -> float:
    """Annualized Sharpe ratio of one asset from its weekly price history:
    (mean weekly return * 52 - risk_free_rate) / (weekly std * sqrt(52)).
    NaN if there are fewer than two returns or the volatility is zero."""
    returns = prices.dropna().pct_change().dropna()
    if len(returns) < 2:
        return np.nan
    periods = config.PERIODS_PER_YEAR
    vol = returns.std() * np.sqrt(periods)
    return (returns.mean() * periods - risk_free_rate) / vol if vol > 0 else np.nan


def build_inputs(frames: dict, tickers=None, include_stocks=False) -> PortfolioInputs:
    """Align everything for a chosen universe.

    tickers=None  -> every fund in the database (plus stocks if include_stocks=True)
    tickers=[...] -> exactly those tickers, whatever their type
    The common price window is recomputed for the selection, so picking older assets
    gives a longer history.
    """
    assets = frames['assets']
    if tickers is not None:
        assets = assets[assets['ticker'].isin(tickers)]
    elif not include_stocks:
        assets = assets[assets['security_type'] != 'STOCK']

    prices_long = frames['prices']
    prices_long = prices_long[prices_long['ticker'].isin(assets['ticker'])].copy()
    if assets.empty or prices_long.empty:
        raise NoDataError('No assets with price history for this selection. '
                          'Run data_collection.py or pick different tickers.')

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
    if prices.empty:
        raise NoDataError('The selected assets have no overlapping price dates.')
    tickers = list(prices.columns)
    security_types = assets.set_index('ticker')['security_type'].reindex(tickers)

    metadata = frames['metadata'].assign(sharpe_ratio=lambda df: df['ticker'].map(prices.apply(sharpe_ratio)))

    def with_float_pct(name):
        df = frames[name].copy()
        df['pct'] = df['pct'].astype(float)
        return df

    return PortfolioInputs(
        tickers=tickers,
        expense_ratios=frames['expense'].set_index('ticker')['expense_ratio'].astype(float).reindex(tickers),
        metadata=metadata,
        security_types=security_types,
        prices=prices,
        history_start=history_start,
        dropped=dropped,
        types=build_category_matrix(with_float_pct('classes'), 'asset_class', config.ASSET_CLASSES, tickers),
        sectors=build_category_matrix(with_float_pct('sectors'), 'sector', config.SECTORS, tickers),
        styles=build_category_matrix(with_float_pct('styles'), 'style', config.STYLES, tickers),
        stocks=build_stock_matrix(with_float_pct('holdings'), security_types, tickers),
    )

def load_inputs(tickers=None, include_stocks=False) -> PortfolioInputs:
    return build_inputs(fetch_frames(), tickers, include_stocks)


def compute_estimates(prices: pd.DataFrame, cov_method='ledoit_wolf'):
    """Expected returns (annualized arithmetic mean of weekly returns) and covariance
    (annualized). cov_method: 'ledoit_wolf' (shrunk, default) or 'sample'."""
    freq = config.PERIODS_PER_YEAR
    mu = expected_returns.mean_historical_return(prices, compounding=False, frequency=freq)
    if cov_method == 'sample':
        cov = risk_models.sample_cov(prices, frequency=freq)
    elif cov_method == 'ledoit_wolf':
        cov = risk_models.CovarianceShrinkage(prices, frequency=freq).ledoit_wolf()
    else:
        raise ValueError(f"cov_method must be 'ledoit_wolf' or 'sample', got {cov_method!r}")
    return mu, cov

# ---------------------------------------------------------------------------
# Constraint helpers
# ---------------------------------------------------------------------------
def bounds_vectors(index, bounds_by_name, default=(0.0, 1.0)):
    """(lo, hi) arrays aligned to a matrix's row order."""
    lo = np.array([bounds_by_name.get(name, default)[0] for name in index], dtype=float)
    hi = np.array([bounds_by_name.get(name, default)[1] for name in index], dtype=float)
    return lo, hi

def category_bounds(data: PortfolioInputs, c: Constraints) -> dict:
    """{'sectors': (lo, hi), 'types': (lo, hi)} aligned to the matrix rows."""
    sector_by_name = {s: (c.sector_min, c.sector_max) for s in data.sectors.index}
    return {
        'sectors': bounds_vectors(data.sectors.index, sector_by_name),
        'types': bounds_vectors(data.types.index, c.class_bounds),
    }

def add_exposure_bounds(ef, matrix, lo, hi):
    """Constrain lo <= matrix @ w <= hi, one row per category.
    Two separate constraints so pypfopt's max_sharpe can rescale each one."""
    A = matrix.values
    ef.add_constraint(lambda w: A @ w >= lo)
    ef.add_constraint(lambda w: A @ w <= hi)


def preflight(title, matrix, lo, hi):
    """Cheap necessary-condition check: if even a 100% position in the best single asset
    can't reach a category's minimum, the problem is infeasible no matter what."""
    best = matrix.max(axis=1).values
    worst = matrix.min(axis=1).values
    problems = []
    for name, l, h, mx, mn in zip(matrix.index, lo, hi, best, worst):
        if l > mx + config.TOL:
            problems.append(f'{title} "{name}" needs >= {l:.1%}, but the best single asset has only {mx:.1%}')
        if h < mn - config.TOL:
            problems.append(f'{title} "{name}" must be <= {h:.1%}, but every asset has at least {mn:.1%}')
    return problems

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def breakdown(matrix, w: pd.Series, lo=None, hi=None) -> pd.DataFrame:
    """Exposure of the portfolio to each row-category (matrix @ w), with an optional
    min/max/status check, plus an '(unclassified)' row for weight the data doesn't cover."""
    aligned = w.reindex(matrix.columns).fillna(0.0)
    exposure = matrix.dot(aligned)
    table = pd.DataFrame({'exposure': exposure})
    if lo is not None:
        table['min'] = lo
        table['max'] = hi
        table['status'] = np.where((exposure >= lo - config.TOL) & (exposure <= hi + config.TOL),
                                   'ok', 'VIOLATED')
    table.loc['(unclassified)', 'exposure'] = w.sum() - exposure.sum()
    return table


def lookthrough_exposure(data: PortfolioInputs, w: pd.Series) -> pd.Series:
    """Portfolio % in each underlying stock (direct + fund top holdings), largest first."""
    aligned = w.reindex(data.stocks.columns).fillna(0.0)
    return data.stocks.dot(aligned).sort_values(ascending=False)


def weighted_expense_ratio(data: PortfolioInputs, w: pd.Series) -> float:
    """Missing expense ratios (e.g. stocks) count as 0."""
    return float(np.nansum(data.expense_ratios.values * w.reindex(data.tickers).fillna(0.0).values))


def positions_table(data: PortfolioInputs, w: pd.Series, min_shown=None) -> pd.DataFrame:
    min_shown = config.MIN_SHOWN if min_shown is None else min_shown
    types = data.security_types.reindex(w.index).fillna(CASH)
    table = pd.DataFrame({'type': types, 'weight': w})
    return table[table['weight'] >= min_shown].sort_values('weight', ascending=False)

# ---------------------------------------------------------------------------
# Optimization support functions
# ---------------------------------------------------------------------------
def asset_risk_return(data: PortfolioInputs, cov_method='ledoit_wolf') -> pd.DataFrame:
    """Per-asset annualized volatility and expected return, for scatter plots."""
    mu, cov = compute_estimates(data.prices, cov_method)
    return pd.DataFrame({'volatility': np.sqrt(np.diag(cov)), 'expected_return': mu.values},
                        index=data.tickers)

def build_problem(data: PortfolioInputs, mu, cov, c: Constraints):
    """A fresh, UNSOLVED EfficientFrontier with every constraint applied.
    Solving mutates the object, so call this again whenever you need another solve
    (e.g. one for max_sharpe, one for the frontier)."""
    n = len(data.tickers)
    cash = c.pct_risk_free > 0
    k = 1 - c.pct_risk_free   # user bounds are shares of INVESTED assets; the solver uses total-portfolio weights
    if cash:
        # Cash = an asset at the risk-free rate with ~zero variance and a FIXED weight.
        mu = pd.concat([mu, pd.Series({CASH: c.risk_free_rate})])
        cov = cov.copy()
        cov.loc[CASH, :] = 0.0
        cov.loc[:, CASH] = 0.0
        cov.loc[CASH, CASH] = 1e-8
        weight_bounds = [(c.min_weight * k, c.max_weight * k)] * n + [(c.pct_risk_free, c.pct_risk_free)]
    else:
        weight_bounds = (c.min_weight * k, c.max_weight * k)

    def with_cash(m):          # cash has no sector / class / style / stock exposure
        return m.assign(**{CASH: 0.0}) if cash else m

    ef = efficient_frontier.EfficientFrontier(mu, cov, weight_bounds=weight_bounds)

    stock_matrix = with_cash(data.stocks).values
    if stock_matrix.shape[0]:
        ef.add_constraint(lambda w: stock_matrix @ w <= c.max_stock_pct * k)

    bounds = category_bounds(data, c)
    add_exposure_bounds(ef, with_cash(data.sectors), bounds['sectors'][0] * k, bounds['sectors'][1] * k)
    add_exposure_bounds(ef, with_cash(data.types), bounds['types'][0] * k, bounds['types'][1] * k)
    return ef

@dataclass
class OptimizationResult:
    weights: pd.Series          # all assets (+ CASH if used); sums to 1
    expected_return: float      # annualized, model-implied (w . mu)
    volatility: float           # annualized, sqrt(w' cov w)
    sharpe: float               # (return - risk_free_rate) / volatility; in-sample
    positions: pd.DataFrame     # weights >= config.MIN_SHOWN, with security type
    sectors: pd.DataFrame       # breakdown with bounds + status
    asset_classes: pd.DataFrame
    styles: pd.DataFrame        # breakdown, no bounds
    top_holdings: pd.Series     # look-through underlying exposure, sorted desc (full list)
    expense_ratio: float        # weighted
    warnings: list              # data-quality + infeasibility warnings
    backtest_results: pd.DataFrame         

def optimize(data: PortfolioInputs, constraints: Constraints = None, cov_method='ledoit_wolf') -> OptimizationResult:
    """Max-Sharpe portfolio under the constraints. Raises OptimizationFailed if no
    feasible portfolio exists (the exception carries the diagnostic warnings)."""
    c = constraints or Constraints()
    mu, cov = compute_estimates(data.prices, cov_method)

    bounds = category_bounds(data, c)
    warnings = data_quality_warnings(data)
    warnings += preflight('Sector', data.sectors, *bounds['sectors'])
    warnings += preflight('Asset class', data.types, *bounds['types'])

    ef = build_problem(data, mu, cov, c)
    try:
        raw = ef.max_sharpe(risk_free_rate=c.risk_free_rate)
        ret, vol, sharpe = ef.portfolio_performance(risk_free_rate=c.risk_free_rate)
    except (OptimizationError, cp.error.SolverError, ValueError) as e:
        raise OptimizationFailed(
            f'Optimization failed: {e}. Most likely the constraints cannot all be met by the '
            f'selected assets; loosen the bounds or add assets.', warnings) from e

    names = list(data.tickers) + ([CASH] if c.pct_risk_free > 0 else [])
    w = pd.Series(raw).reindex(names).fillna(0.0).clip(lower=0.0)
    w_inv = w.drop(CASH, errors='ignore') / (1 - c.pct_risk_free)   # shares of invested assets, sums to 1

    return OptimizationResult(
        weights=w,
        expected_return=ret,
        volatility=vol,
        sharpe=sharpe,
        positions=positions_table(data, w),
        sectors=breakdown(data.sectors, w_inv, *bounds['sectors']),
        asset_classes=breakdown(data.types, w_inv, *bounds['types']),
        styles=breakdown(data.styles, w_inv),
        top_holdings=lookthrough_exposure(data, w_inv),
        expense_ratio=weighted_expense_ratio(data, w),
        warnings=warnings,
        backtest_results = backtest_portfolio(data, w),
    )


def efficient_frontier_points(data: PortfolioInputs, constraints: Constraints = None,
                              cov_method='ledoit_wolf', n_points=100) -> pd.DataFrame:
    """Efficient frontier under the same constraints, as a DataFrame[volatility, expected_return]
    ready to plot. Uses fresh copies of the problem, so it never touches a solved optimizer.
    Target returns the constraints make unreachable are skipped."""
    c = constraints or Constraints()
    mu, cov = compute_estimates(data.prices, cov_method)
    template = build_problem(data, mu, cov, c)
    try:
        lo = template.deepcopy()
        lo.min_volatility()
        min_ret = lo.portfolio_performance()[0]
    except (OptimizationError, cp.error.SolverError, ValueError) as e:
        raise OptimizationFailed(f'Could not build the frontier: {e}') from e

    sweep = template.deepcopy()
    rows = []
    for target in np.linspace(min_ret, float(mu.max()), n_points):
        try:
            sweep.efficient_return(float(target))
        except (ValueError, OptimizationError, cp.error.SolverError):
            continue
        r, v, _ = sweep.portfolio_performance()
        rows.append((v, r))
    return pd.DataFrame(rows, columns=['volatility', 'expected_return'])


def backtest_portfolio(data: PortfolioInputs, weights: pd.Series, initial_principal: float = 100000.0, 
                       rebalance_months: int = 12, tax_rate: float = 0.25) -> pd.DataFrame:
    """Backtests portfolio weights over available historical prices with periodic tax-aware rebalancing."""
    w = weights.drop('CASH', errors='ignore')
    w = w[w > 0] / w[w > 0].sum()
    
    # Resample price history to monthly ends over the historical window
    prices_m = data.prices[w.index].resample('ME').last().dropna()
    
    portfolio_values, taxes_paid, cost_basis_values = [], [], []
    total_val = initial_principal
    cost_basis = total_val * w
    units = (total_val * w) / prices_m.iloc[0] # allow partial units for simplicity

    for i, (date, prices) in enumerate(prices_m.iterrows()):
        curr_vals = units * prices
        total_val = curr_vals.sum()
        tax = 0.0

        # Rebalance according to schedule
        if i > 0 and i % rebalance_months == 0:
            sold_mask = curr_vals > (total_val * w)
            #print(sold_mask)
            
            # Tax capital gains on sold portions
            if sold_mask.any():
                gain_frac = np.maximum(0.0, (curr_vals[sold_mask] - cost_basis[sold_mask]) / curr_vals[sold_mask])
                #print(gain_frac)
                pre_tax_targets = total_val * w
                sold_amt = curr_vals[sold_mask] - pre_tax_targets[sold_mask]
                realized_gains = sold_amt * gain_frac
                #print(realized_gains)
                tax = float(realized_gains.sum() * tax_rate)
                #print(tax)

            # 2. Subtract tax from total portfolio value to get post-tax pool
            post_tax_total = total_val - tax
            post_tax_targets = post_tax_total * w

            # 3. Update cost basis correctly per asset
            new_cost_basis = cost_basis.copy()
            for ticker in w.index:
                if curr_vals[ticker] > post_tax_targets[ticker]:
                    # Sold/Trimmed: reduce cost basis proportionally to remaining value
                    frac_retained = post_tax_targets[ticker] / curr_vals[ticker]
                    new_cost_basis[ticker] *= frac_retained
                else:
                    # Bought/Added: add exact dollar amount of new purchases
                    added_dollars = post_tax_targets[ticker] - curr_vals[ticker]
                    new_cost_basis[ticker] += added_dollars

            # 4. Update state variables
            cost_basis = new_cost_basis
            total_val = post_tax_total
            units = post_tax_targets / prices

        portfolio_values.append(total_val)
        taxes_paid.append(tax)
        cost_basis_values.append(cost_basis.sum())

    return pd.DataFrame({'portfolio_value': portfolio_values, 'taxes_paid': taxes_paid, 'cost_basis': cost_basis_values}, index=prices_m.index)

# ---------------------------------------------------------------------------
# Command-line report (the only place that prints)
# ---------------------------------------------------------------------------
def print_report(data: PortfolioInputs, result: OptimizationResult):
    win = window_summary(data)
    print(f'{len(data.tickers)} assets: {data.tickers}')
    print(f'Common price window: {win["start"]} to {win["end"]} ({win["weeks"]} weekly closes)')
    print('Assets limiting the window (latest history start):')
    print(win['limiting_assets'].to_string())
    for w in result.warnings:
        print(f'WARNING: {w}')

    print('\n=== Optimized portfolio performance (annualized, in-sample) ===')
    print(f'Expected return: {result.expected_return:.1%}   Volatility: {result.volatility:.1%}   '
          f'Sharpe: {result.sharpe:.2f}')
    print(f'Weighted expense ratio: {result.expense_ratio:.4%}')

    print(f'\n=== Positions (weight >= {config.MIN_SHOWN:.1%}; {len(result.positions)} of {len(result.weights)}) ===')
    print(result.positions)
    print(f'Total weight: {result.weights.sum():.4f}')

    for title, table in (('Sector', result.sectors), ('Asset class', result.asset_classes),
                         ('Style', result.styles)):
        print(f'\n=== {title} breakdown of the optimized portfolio ===')
        print(table)

    print(f'\n=== Top {config.TOP_N} underlying holdings (direct stock + fund top-10 look-through) ===')
    print(result.top_holdings.head(config.TOP_N).to_frame('pct_of_portfolio'))
    print(f'Largest single-stock exposure: {result.top_holdings.iloc[0]:.4f} (cap {config.MAX_STOCK_PCT:.2f})')
    print("Note: only each fund's top 10 holdings are stored, so these exposures are a lower bound.")


def main():
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    pd.set_option('display.max_rows', 120)
    pd.set_option('display.float_format', lambda x: f'{x:.4f}')

    try:
        data = load_inputs()
        result = optimize(data)
    except NoDataError as e:
        sys.exit(str(e))
    except OptimizationFailed as e:
        for w in e.warnings:
            print(f'WARNING: {w}')
        sys.exit(str(e))
    print_report(data, result)


if __name__ == '__main__':
    main()