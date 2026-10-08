""" 
Configuration file to store python variables that can be used across multiple other scripts
"""

# --------- CLASS DEFINITIONS ---------

ASSET_CLASSES = [
    'domesticlargecap', 'domesticmidcap', 'domesticsmallcap',
    'intllargecap', 'intlmidcap', 'intlsmallcap', 'emerging',
]

STYLES = ['value','growth','blend']


SECTORS = [
    'realestate', 'consumer_cyclical', 'basic_materials', 'consumer_defensive',
    'technology', 'communication_services', 'financial_services', 'utilities',
    'industrials', 'energy', 'healthcare',
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

# --------- OPTIMIZATION PARAMETERS ---------

RISK_AVERSION = 3.0          # q
MIN_WEIGHT = 0.0
MAX_WEIGHT = 1.0
MAX_ASSET_TYPE_PCT = 0.25
ASSET_CLASS_BOUNDS = {          # (min, max) share of the whole portfolio
    'domesticlargecap': (0.45, 0.55),
    'domesticmidcap':   (0.10, 0.15),
    'domesticsmallcap': (0.08, 0.12),
    'emerging':         (0.02, 0.05),
    'intllargecap':     (0.14, 0.16),
    'intlmidcap':       (0.01, 0.03),
    'intlsmallcap':     (0.00, 0.02),
}
MAX_STOCK_PCT = 0.03         
SECTOR_MIN = 0.01
SECTOR_MAX = 0.25
PCT_RISK_FREE = 0.0
RISK_FREE_RATE = 0.035   # annual rate used inside max_sharpe; placeholder, set to a T-bill yield when decided

# --------- HISTORICAL PRICING PARAMETERS ---------

N_TOP_HOLDINGS = 10
HISTORY_PERIOD = '12y'
HISTORY_INTERVAL = '1wk'   # weekly bars; 'Close' = last close of the week
PERIODS_PER_YEAR = 52        # weekly data
MIN_WEEKS_WARNING = 104      # warn if the common price window is shorter than ~2 years
COVERAGE_WARNING = 0.98      # warn if an asset's allocation rows sum to less than this
TRAILING_RETURN_PERIODS = ['1y', '5y', '10y']
TRAILING_RETURN_YEARS = {'1y': 1, '5y': 5, '10y': 10}

# --------- TOLERANCE PARAMETERS ---------

TOP_N = 15          # rows to show in the top-holdings tables
MIN_SHOWN = 0.001   # hide asset weights below 0.1% in the positions table
TOL = 1e-4          # tolerance when checking constraints on the solved weights

# Column limits (must match setup.sql) so out-of-range values become NULL
# instead of crashing the insert.
PCT_LIMIT = 10          # DECIMAL(5,4)
RETURN_LIMIT = 10_000   # DECIMAL(8,4)
PRICE_LIMIT = 1e8       # DECIMAL(10,2)
CLOSE_LIMIT = 1e8       # DECIMAL(12,4)

# --------- GENERAL PARAMETERS ---------

# market-cap size thresholds (USD), used to bucket individual stocks
LARGE_CAP_THRESHOLD = 10e9
MID_CAP_THRESHOLD = 2e9

# yfinance quoteType -> how we fetch it
QUOTE_TYPE_TO_KIND = {'EQUITY': 'STOCK', 'ETF': 'FUND', 'MUTUALFUND': 'FUND'}