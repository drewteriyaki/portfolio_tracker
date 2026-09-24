-- Portfolio tracker schema (phase 1)
-- SQLite. Safe to run repeatedly; every object uses IF NOT EXISTS.

-- Individual login accounts. Admin-provisioned only (see manage_users.py) -
-- there is no self-service signup anywhere in the app.
--
-- `user_id` on snapshots/positions/account_totals is declared directly
-- below (unlike live_price/day_open/realized_gain, which are ALTER-only)
-- because their UNIQUE constraints need to include it - two different
-- users legitimately CAN share a snapshot_date+account+symbol (e.g. the
-- same broker account-naming convention, or two people testing with the
-- same sample data), and the pre-multi-user constraints would otherwise
-- block the second user's import. transactions/value_log have no UNIQUE
-- constraint to widen, so they still just get user_id via the
-- ALTER-if-missing loop in portfolio.py's _ensure_schema, same as before.
-- Deployed databases that predate this (this app has exactly one: the
-- live one) needed a one-time explicit table-rebuild migration to widen
-- their already-existing constraints - CREATE TABLE IF NOT EXISTS alone
-- can't retrofit that onto a table that already exists.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,          -- hex pbkdf2_hmac('sha256', ...) digest
    password_salt TEXT    NOT NULL,          -- hex random salt (os.urandom(16))
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Advisor mode: which accounts each advisor manages. Who is an advisor is
-- users.is_advisor (added by portfolio.py's _ensure_schema), set only via
-- manage_users.py.
CREATE TABLE IF NOT EXISTS advisor_clients (
    advisor_id INTEGER NOT NULL,
    client_id  INTEGER NOT NULL,
    PRIMARY KEY (advisor_id, client_id)
);

-- One row per (positions export file, as-of date) that has been imported.
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT    NOT NULL,              -- ISO date parsed from the export header, e.g. 2026-08-28
    as_of_text    TEXT,                          -- raw "as of ..." string from the file
    source_file   TEXT    NOT NULL,              -- absolute path of the CSV that was imported
    user_id       INTEGER NOT NULL,
    imported_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (snapshot_date, source_file, user_id)
);

-- One row per real holding, per account, per snapshot.
-- Cash lines and "Positions Total" lines are NOT stored here.
CREATE TABLE IF NOT EXISTS positions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date      TEXT    NOT NULL,
    account            TEXT    NOT NULL,          -- e.g. "Individual ...641"
    symbol             TEXT    NOT NULL,
    description        TEXT,
    asset_type         TEXT,                      -- Equity / ETFs & Closed End Funds / ...
    quantity           REAL,                      -- may be fractional (e.g. 252.7033)
    cost_basis         REAL,                      -- total cost basis for the lot, in dollars
    market_value       REAL,                      -- total market value, in dollars
    price_change_pct   REAL,
    day_change_pct     REAL,
    reported_gain      REAL,                      -- "Gain $" straight from the file (for reconciliation)
    reported_gain_pct  REAL,                      -- "Gain %" straight from the file
    reinvest           INTEGER,                   -- 1 = Yes, 0 = No, NULL = n/a
    reinvest_cap_gains INTEGER,
    div_pay_date       TEXT,
    div_yield_pct      REAL,
    next_earnings_date TEXT,
    pct_of_account     REAL,
    source_file        TEXT,
    user_id            INTEGER NOT NULL,
    imported_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (snapshot_date, account, symbol, user_id)
);

-- The skipped-but-useful rows: per account, the cash line's market value and the
-- "Positions Total" figures. Kept so the importer can prove the parsed holdings
-- reconcile to the totals the broker printed.
CREATE TABLE IF NOT EXISTS account_totals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date         TEXT    NOT NULL,
    account               TEXT    NOT NULL,
    cash_value            REAL,                   -- "Cash & Cash Investments" market value
    reported_cost_basis   REAL,                   -- "Positions Total" cost basis (holdings only)
    reported_market_value REAL,                   -- "Positions Total" market value (holdings + cash)
    reported_gain         REAL,                   -- "Positions Total" gain $
    reported_gain_pct     REAL,                   -- "Positions Total" gain %
    source_file           TEXT,
    user_id               INTEGER NOT NULL,
    imported_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (snapshot_date, account, user_id)
);

-- Live quotes fetched by update_prices.py. Append-only: one row per ticker per run.
CREATE TABLE IF NOT EXISTS price_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT    NOT NULL,
    price      REAL,                             -- current price ("c" from Finnhub); NULL if the fetch failed
    prev_close REAL,                             -- "pc"
    change     REAL,                             -- "d"
    pct_change REAL,                             -- "dp"
    day_open   REAL,                             -- "o"
    day_high   REAL,                             -- "h"
    day_low    REAL,                             -- "l"
    quote_time TEXT,                             -- exchange timestamp, ISO-8601 UTC ("t" from Finnhub)
    fetched_at TEXT    NOT NULL DEFAULT (datetime('now')),   -- when this row was written, ISO-8601 UTC
    source     TEXT    NOT NULL DEFAULT 'finnhub',
    ok         INTEGER NOT NULL DEFAULT 1,       -- 0 = fetch failed or no data for the symbol
    error      TEXT,
    raw        TEXT                              -- raw JSON body, for debugging
);
CREATE INDEX IF NOT EXISTS idx_price_history_ticker ON price_history (ticker, fetched_at);

-- Rows are *inferred* from the quantity delta between two imported snapshots
-- (the Positions export has no real trade history) - see changes.py.
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account       TEXT    NOT NULL,
    trade_date    TEXT,
    settle_date   TEXT,
    action        TEXT,                             -- BUY / SELL / DIV / REINVEST / ...
    symbol        TEXT,
    description   TEXT,
    quantity      REAL,
    price         REAL,
    amount        REAL,                             -- signed cash impact
    fees          REAL,
    realized_gain REAL,                              -- SELL only; average-cost method, NULL for BUY
    source_file   TEXT,
    imported_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Portfolio-level aggregates over time. One row is appended per app session
-- open (see perf.py); historical CSV snapshots are folded in when charting.
CREATE TABLE IF NOT EXISTS value_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at           TEXT    NOT NULL,            -- ISO-8601 UTC, when this row was written
    snapshot_date       TEXT,                        -- the CSV snapshot in effect at the time
    source              TEXT    NOT NULL DEFAULT 'app_open',   -- app_open | snapshot | manual
    portfolio_value     REAL,
    holdings_value      REAL,
    cash                REAL,
    cost_basis          REAL,
    unrealized_gain     REAL,
    unrealized_gain_pct REAL,
    day_change_usd      REAL,                        -- sum of per-position day $ change
    n_positions         INTEGER,
    n_priced            INTEGER,                     -- how many had live prices
    priced_at           TEXT                         -- max(live_price_at) at the time
);
CREATE INDEX IF NOT EXISTS idx_value_log_time ON value_log (logged_at);

-- Real daily OHLCV history, fetched from Yahoo via sync_history.py (yfinance).
-- One row per (ticker, trading day). Upserted, so a re-sync refreshes.
CREATE TABLE IF NOT EXISTS daily_bars (
    ticker     TEXT    NOT NULL,
    date       TEXT    NOT NULL,                    -- trading day, YYYY-MM-DD
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    adj_close  REAL,
    volume     INTEGER,
    source     TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_bars_ticker ON daily_bars (ticker, date);

-- Intraday OHLCV, several resolutions, fetched from Yahoo via sync_history.py.
-- Yahoo only keeps a limited look-back per resolution (roughly: 1m ~ 8 days,
-- 5m/15m ~ 60 days, 60m ~ 2 years) - sync_history.py requests the maximum each
-- allows. One row per (ticker, interval, bar timestamp). Upserted.
CREATE TABLE IF NOT EXISTS intraday_bars (
    ticker     TEXT    NOT NULL,
    interval   TEXT    NOT NULL,                    -- '1m' | '5m' | '15m' | '60m'
    ts         TEXT    NOT NULL,                    -- bar start, ISO-8601 UTC
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     INTEGER,
    source     TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_intraday_bars_lookup ON intraday_bars (ticker, interval, ts);

-- Point-in-time fundamentals / reference data, fetched alongside the bars.
-- One row per ticker, replaced on each sync.
CREATE TABLE IF NOT EXISTS security_info (
    ticker         TEXT    PRIMARY KEY,
    name           TEXT,
    sector         TEXT,
    industry       TEXT,
    market_cap     REAL,
    beta           REAL,
    trailing_pe    REAL,
    forward_pe     REAL,
    price_to_book  REAL,
    dividend_yield REAL,                            -- as returned by Yahoo (may be % or fraction)
    week52_high    REAL,
    week52_low     REAL,
    avg_volume     INTEGER,
    avg_volume_10d INTEGER,
    source         TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- AI Assistant investing profile, one row per account (see advisor.py).
-- updated_at is always written explicitly, never left to a DEFAULT.
CREATE TABLE IF NOT EXISTS investor_profiles (
    user_id            INTEGER PRIMARY KEY,
    goal               TEXT,
    time_horizon_years INTEGER,
    target_return_pct  REAL,
    risk_tolerance     TEXT,                      -- conservative | moderate | aggressive
    experience         TEXT,                      -- new | some | experienced
    notes              TEXT,
    updated_at         TEXT
);

-- Tickers tracked for their chart/stats without being an owned position.
-- Per-user: two different accounts can each watch the same ticker.
CREATE TABLE IF NOT EXISTS watchlist (
    user_id    INTEGER NOT NULL,
    ticker     TEXT    NOT NULL,
    added_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, ticker)
);

-- Company news headlines from Finnhub's /company-news endpoint, cached per
-- ticker so opening a ticker's detail view doesn't re-fetch on every page
-- load. `id` is Finnhub's own article id - a natural key for INSERT OR
-- IGNORE dedup across repeated syncs.
CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY,
    ticker       TEXT    NOT NULL,
    headline     TEXT,
    summary      TEXT,
    source       TEXT,
    url          TEXT,
    published_at TEXT,                              -- ISO-8601 UTC, from Finnhub's epoch
    fetched_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_news_ticker ON news (ticker, published_at);

CREATE INDEX IF NOT EXISTS idx_positions_snapshot   ON positions (snapshot_date);
CREATE INDEX IF NOT EXISTS idx_positions_symbol     ON positions (symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_symbol  ON transactions (symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions (account);
