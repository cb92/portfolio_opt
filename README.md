
## Application Architecture
```mermaid
---
config:
  layout: elk
  theme: base
---
flowchart LR
    fe["Asset Optimization (Python)"] ---> test["Front End"]
    n1["Asset DB (local or Railway)"] --> fe
    n3["Requested Asset Table"] --> n2["Data Collection (Python/MySQL, run manually)"]
    n2 --> n1
    test --> n3
    test -. user constraints .-> fe
    n3 -. request status .-> test
    yf["yfinance"] --> n2
    ms["Morningstar (mstarpy + headless browser)"] --> n2
    manual(["Manual Run (local machine)"]) -.-> n2

    fe@{ shape: rounded}
    n1@{ shape: cyl}
    n3@{ shape: card}
    n2@{ shape: rounded}
    yf@{ shape: rounded}
    ms@{ shape: rounded}
```

## Database Architecture
```mermaid
erDiagram
    requested_assets |o..o| assets : "becomes (no FK)"
    assets ||--o| asset_metadata : has
    assets ||--o{ asset_sector_allocations : has
    assets ||--o{ asset_class_allocations : has
    assets ||--o{ asset_style_allocations : has
    assets ||--o{ asset_top_holdings : "parent of"
    assets ||--o{ asset_price_history : has

    requested_assets {
        varchar ticker PK
        enum request_status "pending, processing, done, failed"
        text error_message
        timestamp requested_at
        timestamp processed_at "NULL until processed"
    }
    assets {
        varchar ticker PK
        enum security_type "STOCK, FUND"
        varchar country
        text long_business_summary
        timestamp created_at
        timestamp updated_at
    }
    asset_metadata {
        varchar ticker PK, FK
        decimal expense_ratio
        decimal trailing_return_1y
        decimal trailing_return_5y
        decimal trailing_return_10y
        decimal price_target_median
        decimal price_target_mean
        decimal price_target_low
        decimal price_target_hi
        decimal avg_analyst_rating
        timestamp created_at
        timestamp updated_at
    }
    asset_sector_allocations {
        varchar ticker PK, FK
        varchar sector PK
        decimal pct
        timestamp created_at
        timestamp updated_at
    }
    asset_class_allocations {
        varchar ticker PK, FK
        varchar asset_class PK
        decimal pct
        timestamp created_at
        timestamp updated_at
    }
    asset_style_allocations {
        varchar ticker PK, FK
        varchar style PK
        decimal pct
        timestamp created_at
        timestamp updated_at
    }
    asset_top_holdings {
        varchar parent_ticker PK, FK
        varchar holding_ticker PK
        tinyint holding_rank
        decimal pct
        timestamp created_at
        timestamp updated_at
    }
    asset_price_history {
        varchar ticker PK, FK
        date price_date PK
        decimal close_price
    }
```

## Database Updates
Because Morningstar no longer allows automated pulling and we need to acquire Morningstar data via a headless browser, it has to be done manually on my local machine and added to a database for posterity. 

Generally speaking, we want all of the stocks and funds to be roughly the same level of freshness, otherwise the optimization may favor those with more recent history in the case of a recent stock market increase (or disfavor those with recent history in the case of a fall). Given this, the whole set of assets that live in the database should be refreshed somewhere between weekly and monthly, or everything should be refreshed if we're clearing a large request queue.

## Optimization Strategy
I'm using Mean-Variance Optimization to develop an efficient portfolio from the desired set of assets, allowing me to take into account expected return, correlation between assets, and accounting for personal risk coefficient.

The quadratic optimization implemented here is as follows: 
$min w^{T}\Sigma w - q R^{T}w$ over $w$ where: 
- $w$ = vector of portfolio weights (which will be optimized)
- $\Sigma$ is the covariance matrix of the potential assets where the annualized variance is calculated by using week over week changes in historical closing prices, calculating the variance of those returns and multiplying by 52.
- $q$ is the risk aversion coefficient
- $R$ is the vector of expected (adjusted) returns (expressed as an annualized arithmetic mean)

The optimization carries the following linear constraints: 
- No single stock accounting for more than 10% of entire portfolio (this will take into account both single stocks and top holdings of ETFs and funds)
- Sum of proportions invested = constant, where constant = 1 - % invested into risk free asset
- Asset-style (cap, geography, etc) constraints (within 2% of provided or age-based targets)
- Sector constraints (no more than 30% in any given sector of the economy)
- Weights all need to be between 0 and 1


### Run Log
A starting set of 35 assets (a mix of stocks, ETFs and mutual funds) has been successfully pulled as of 10/1/26 around 11am.