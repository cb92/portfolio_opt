-- setup.sql
-- Creates all tables. Safe to re-run (IF NOT EXISTS).
--
-- This file deliberately does NOT contain CREATE DATABASE / USE, so the exact same
-- file works locally and on Railway (where the database already exists).
--
-- Local:    mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS portfolio_db CHARACTER SET utf8mb4"
--           mysql -u root -p portfolio_db < setup.sql
-- Railway:  mysql -h <host> -P <port> -u root -p railway < setup.sql
--
-- Tables are ordered by foreign-key dependency (assets first).

-- ---------------------------------------------------------------------------
-- Asset request queue
-- The limited app user can only INSERT (ticker), so every other column needs a
-- default. processed_at stays NULL until the collector finishes the request.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS requested_assets (
    ticker              VARCHAR(10) PRIMARY KEY,
    request_status      ENUM('pending','processing','done','failed') NOT NULL DEFAULT 'pending',
    error_message       TEXT,
    requested_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at        TIMESTAMP NULL DEFAULT NULL,
    INDEX idx_request_status (request_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Core asset table (parent of everything else)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    ticker                VARCHAR(10) PRIMARY KEY,
    security_type         ENUM('STOCK', 'FUND') NOT NULL,
    country               VARCHAR(50),
    long_business_summary TEXT,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- One row per asset: returns, expense ratio, analyst data
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_metadata (
    ticker              VARCHAR(10) PRIMARY KEY,
    expense_ratio       DECIMAL(5,4),          -- 0.0015 for 0.15%
    trailing_return_1y  DECIMAL(8,4),          -- annualized (CAGR)
    trailing_return_5y  DECIMAL(8,4),
    trailing_return_10y DECIMAL(8,4),
    price_target_median DECIMAL(10,2),
    price_target_mean   DECIMAL(10,2),
    price_target_low    DECIMAL(10,2),
    price_target_hi     DECIMAL(10,2),
    avg_analyst_rating  DECIMAL(3,2),
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (ticker) REFERENCES assets(ticker) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Allocations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_sector_allocations (
    ticker     VARCHAR(10),
    sector     VARCHAR(50),                    -- 'technology', 'healthcare', etc.
    pct        DECIMAL(5,4) NOT NULL,          -- 0.4500 for 45%
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, sector),
    FOREIGN KEY (ticker) REFERENCES assets(ticker) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS asset_class_allocations (
    ticker      VARCHAR(10),
    asset_class VARCHAR(50),                   -- 'domesticlargecap', 'emerging', etc.
    pct         DECIMAL(5,4) NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, asset_class),
    FOREIGN KEY (ticker) REFERENCES assets(ticker) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS asset_style_allocations (
    ticker     VARCHAR(10),
    style      VARCHAR(20),                    -- 'value', 'growth', 'blend'
    pct        DECIMAL(5,4) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, style),
    FOREIGN KEY (ticker) REFERENCES assets(ticker) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Top holdings (holding_ticker is intentionally NOT a foreign key: a fund's
-- holdings usually aren't themselves rows in `assets`)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_top_holdings (
    parent_ticker  VARCHAR(10),
    holding_ticker VARCHAR(10),
    holding_rank   TINYINT,                    -- 1 through 10
    pct            DECIMAL(5,4) NOT NULL,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (parent_ticker, holding_ticker),
    INDEX idx_holding_ticker (holding_ticker),
    FOREIGN KEY (parent_ticker) REFERENCES assets(ticker) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- Weekly close prices (no created/updated columns, per spec)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_price_history (
    ticker      VARCHAR(10),
    price_date  DATE,                          -- Yahoo's weekly bar date (Monday of that week)
    close_price DECIMAL(12,4) NOT NULL,        -- last close of the week (split/dividend adjusted)
    PRIMARY KEY (ticker, price_date),
    FOREIGN KEY (ticker) REFERENCES assets(ticker) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
