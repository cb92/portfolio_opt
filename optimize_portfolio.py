"""
optimize_portfolio.py

Max-Sharpe optimization with pypfopt on top of the matrices from portfolio_inputs.py.

Constraints (all expressed on the weights vector, rows of each matrix @ w):
  * single stock <= MAX_STOCK_PCT of the whole portfolio (direct holdings + fund top holdings)
  * every sector between SECTOR_MIN and SECTOR_MAX
  * every asset class inside ASSET_CLASS_BOUNDS
  * weights between MIN_WEIGHT and MAX_WEIGHT, summing to 1 (pypfopt default)

Run:  python optimize_portfolio.py
"""
import sys

import cvxpy as cp
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from pypfopt import efficient_frontier, plotting
from pypfopt.exceptions import OptimizationError

from portfolio_inputs import (MAX_STOCK_PCT, MAX_WEIGHT, MIN_WEIGHT,
                              compute_estimates, load_inputs)

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
RISK_FREE_RATE = 0.035   # annual rate used inside max_sharpe; placeholder, set to a T-bill yield when decided
SECTOR_MIN = 0.01
SECTOR_MAX = 0.25
ASSET_CLASS_BOUNDS = {          # (min, max) share of the whole portfolio
    'domesticlargecap': (0.45, 0.55),
    'domesticmidcap':   (0.10, 0.15),
    'domesticsmallcap': (0.08, 0.12),
    'emerging':         (0.02, 0.05),
    'intllargecap':     (0.14, 0.16),
    'intlmidcap':       (0.01, 0.03),
    'intlsmallcap':     (0.00, 0.02),
}
TOP_N = 15          # rows to show in the top-holdings tables
MIN_SHOWN = 0.001   # hide asset weights below 0.1% in the positions table
TOL = 1e-4          # tolerance when checking constraints on the solved weights


# ---------------------------------------------------------------------------
# Constraint helpers
# ---------------------------------------------------------------------------
def bounds_vectors(index, bounds_by_name, default=(0.0, 1.0)):
    """(lo, hi) arrays aligned to a matrix's row order."""
    lo = np.array([bounds_by_name.get(name, default)[0] for name in index], dtype=float)
    hi = np.array([bounds_by_name.get(name, default)[1] for name in index], dtype=float)
    return lo, hi


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
        if l > mx + TOL:
            problems.append(f'{title} "{name}" needs >= {l:.1%}, but the best single asset has only {mx:.1%}')
        if h < mn - TOL:
            problems.append(f'{title} "{name}" must be <= {h:.1%}, but every asset has at least {mn:.1%}')
    return problems


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def report(title, matrix, w, lo=None, hi=None):
    exposure = matrix.dot(w)
    table = pd.DataFrame({'exposure': exposure})
    if lo is not None:
        table['min'] = lo
        table['max'] = hi
        table['ok'] = np.where((exposure >= lo - TOL) & (exposure <= hi + TOL), 'ok', 'VIOLATED')
    print(f'\n=== {title} breakdown of the optimized portfolio ===')
    print(table)
    print(f'Not covered by {title.lower()} data: {w.sum() - exposure.sum():.4f}')

def main():
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    pd.set_option('display.max_rows', 120)
    pd.set_option('display.float_format', lambda x: f'{x:.4f}')

    data = load_inputs()
    print("History Start: " + str(data.history_start))
    print(data.prices.head(10))
    tickers = data.tickers
    mu, _, shrunk_cov = compute_estimates(data.prices)

    print(f'{len(tickers)} assets, {len(data.prices)} weekly closes '
          f'({data.prices.index[0].date()} to {data.prices.index[-1].date()})')

    # --- constraint bounds aligned to matrix rows ---
    sector_lo = np.full(len(data.sectors.index), SECTOR_MIN)
    sector_hi = np.full(len(data.sectors.index), SECTOR_MAX)
    class_lo, class_hi = bounds_vectors(data.types.index, ASSET_CLASS_BOUNDS)

    problems = (preflight('Sector', data.sectors, sector_lo, sector_hi)
                + preflight('Asset class', data.types, class_lo, class_hi))
    if problems:
        print('\nWARNING: constraints look infeasible with the current data:')
        for p in problems:
            print(f'  - {p}')

    # --- build the EF object ---
    stock_matrix = data.stocks.values

    ef = efficient_frontier.EfficientFrontier(mu, shrunk_cov, weight_bounds=(MIN_WEIGHT, MAX_WEIGHT))
    ef.add_constraint(lambda w: stock_matrix @ w <= MAX_STOCK_PCT)
    add_exposure_bounds(ef, data.sectors, sector_lo, sector_hi)
    add_exposure_bounds(ef, data.types, class_lo, class_hi)

    # --- Plotting code ---
    # ------- Plot efficient frontier -------
    fig, ax = plt.subplots()
    plotting.plot_efficient_frontier(ef, ax=ax, show_assets=True)
    plt.show()

    # --- solve for max sharpe ---
    # need to rebuild EF object
    ef = efficient_frontier.EfficientFrontier(mu, shrunk_cov, weight_bounds=(MIN_WEIGHT, MAX_WEIGHT))
    ef.add_constraint(lambda w: stock_matrix @ w <= MAX_STOCK_PCT)
    add_exposure_bounds(ef, data.sectors, sector_lo, sector_hi)
    add_exposure_bounds(ef, data.types, class_lo, class_hi)

    try:
        weights = ef.max_sharpe(risk_free_rate=RISK_FREE_RATE)
        #weights = ef.max_quadratic_utility(3)
    except (OptimizationError, cp.error.SolverError, ValueError) as e:
        print(f'\nOptimization failed: {e}')
        print('Most likely the constraints cannot all be met by the available assets. '
              'Check the warnings above, or loosen the bounds.')
        sys.exit(1)

    print('\n=== Optimized portfolio performance (annualized) ===')
    ef.portfolio_performance(verbose=True, risk_free_rate=RISK_FREE_RATE)

    w = pd.Series(weights).reindex(tickers).fillna(0.0).clip(lower=0.0)
    wv = w.values
    fig, ax = plt.subplots()
    plotting.plot_weights(weights, ax=ax, show_assets=True)
    plt.show()

    # --- positions and breakdowns ---
    positions = pd.DataFrame({'type': data.security_types, 'weight': w})
    positions = positions[positions['weight'] >= MIN_SHOWN].sort_values('weight', ascending=False)
    print(f'\n=== Positions (weight >= {MIN_SHOWN:.1%}; {len(positions)} of {len(tickers)} assets) ===')
    print(positions)
    print(f'Total weight: {wv.sum():.4f}')

    report('Sector', data.sectors, wv, sector_lo, sector_hi)
    report('Asset class', data.types, wv, class_lo, class_hi)
    report('Style', data.styles, wv)
    print(f'Weighted expense ratio: {np.nansum(data.expense_ratios.values * wv):.4%}')

    # --- look-through top holdings ---
    exposure = data.stocks.dot(wv).sort_values(ascending=False)
    print(f'\n=== Top {TOP_N} underlying holdings (direct stock + fund top-10 look-through) ===')
    print(exposure.head(TOP_N).to_frame('pct_of_portfolio'))
    print(f'Largest single-stock exposure: {exposure.iloc[0]:.4f} (cap {MAX_STOCK_PCT:.2f})')
    print('Note: only each fund\'s top 10 holdings are stored, so these exposures are a lower bound.')


if __name__ == '__main__':
    main()