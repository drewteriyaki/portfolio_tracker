-- Portfolio tracker schema - Postgres.
--
-- Line-for-line the same tables/columns/indexes as schema.sql (SQLite),
-- translated only where the syntax genuinely differs:
--   INTEGER PRIMARY KEY AUTOINCREMENT -> SERIAL PRIMARY KEY
--   datetime('now')                   -> to_char(now() AT TIME ZONE 'UTC',
--                                          'YYYY-MM-DD HH24:MI:SS')
--     (reproduces SQLite's exact 'YYYY-MM-DD HH:MM:SS' string shape - every
--     consumer of these columns parses/compares them as that exact format,
--     e.g. news.py's needs_refresh(); see pgcompat.PG_NOW_EXPR, which this
--     literal expression must stay byte-for-byte identical to)
-- REAL/TEXT/INTEGER are valid Postgres types as-is - no translation needed.
-- Executed statement-by-statement by pgcompat.ConnWrapper.executescript(),
-- not as one script, so every statement needs to already be independently
-- valid (Postgres has no CREATE TABLE IF NOT EXISTS quirks here - it's
-- supported natively, same as SQLite).

-- Individual login accounts - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL  PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    password_salt TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            SERIAL  PRIMARY KEY,
    snapshot_date TEXT    NOT NULL,
    as_of_text    TEXT,
    source_file   TEXT    NOT NULL,
    user_id       INTEGER NOT NULL,
    imported_at   TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    UNIQUE (snapshot_date, source_file, user_id)
);

CREATE TABLE IF NOT EXISTS positions (
    id                 SERIAL  PRIMARY KEY,
    snapshot_date      TEXT    NOT NULL,
    account            TEXT    NOT NULL,
    symbol             TEXT    NOT NULL,
    description        TEXT,
    asset_type         TEXT,
    quantity           REAL,
    cost_basis         REAL,
    market_value       REAL,
    price_change_pct   REAL,
    day_change_pct     REAL,
    reported_gain      REAL,
    reported_gain_pct  REAL,
    reinvest           INTEGER,
    reinvest_cap_gains INTEGER,
    div_pay_date       TEXT,
    div_yield_pct      REAL,
    next_earnings_date TEXT,
    pct_of_account     REAL,
    source_file        TEXT,
    user_id            INTEGER NOT NULL,
    imported_at        TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    UNIQUE (snapshot_date, account, symbol, user_id)
);

CREATE TABLE IF NOT EXISTS account_totals (
    id                    SERIAL  PRIMARY KEY,
    snapshot_date         TEXT    NOT NULL,
    account               TEXT    NOT NULL,
    cash_value            REAL,
    reported_cost_basis   REAL,
    reported_market_value REAL,
    reported_gain         REAL,
    reported_gain_pct     REAL,
    source_file           TEXT,
    user_id               INTEGER NOT NULL,
    imported_at           TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    UNIQUE (snapshot_date, account, user_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id         SERIAL  PRIMARY KEY,
    ticker     TEXT    NOT NULL,
    price      REAL,
    prev_close REAL,
    change     REAL,
    pct_change REAL,
    day_open   REAL,
    day_high   REAL,
    day_low    REAL,
    quote_time TEXT,
    fetched_at TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    source     TEXT    NOT NULL DEFAULT 'finnhub',
    ok         INTEGER NOT NULL DEFAULT 1,
    error      TEXT,
    raw        TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_history_ticker ON price_history (ticker, fetched_at);

CREATE TABLE IF NOT EXISTS transactions (
    id            SERIAL  PRIMARY KEY,
    account       TEXT    NOT NULL,
    trade_date    TEXT,
    settle_date   TEXT,
    action        TEXT,
    symbol        TEXT,
    description   TEXT,
    quantity      REAL,
    price         REAL,
    amount        REAL,
    fees          REAL,
    realized_gain REAL,
    source_file   TEXT,
    imported_at   TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS value_log (
    id                  SERIAL  PRIMARY KEY,
    logged_at           TEXT    NOT NULL,
    snapshot_date       TEXT,
    source              TEXT    NOT NULL DEFAULT 'app_open',
    portfolio_value     REAL,
    holdings_value      REAL,
    cash                REAL,
    cost_basis          REAL,
    unrealized_gain     REAL,
    unrealized_gain_pct REAL,
    day_change_usd      REAL,
    n_positions         INTEGER,
    n_priced            INTEGER,
    priced_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_value_log_time ON value_log (logged_at);

CREATE TABLE IF NOT EXISTS daily_bars (
    ticker     TEXT    NOT NULL,
    date       TEXT    NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    adj_close  REAL,
    volume     INTEGER,
    source     TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_bars_ticker ON daily_bars (ticker, date);

CREATE TABLE IF NOT EXISTS intraday_bars (
    ticker     TEXT    NOT NULL,
    interval   TEXT    NOT NULL,
    ts         TEXT    NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     INTEGER,
    source     TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    PRIMARY KEY (ticker, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_intraday_bars_lookup ON intraday_bars (ticker, interval, ts);

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
    dividend_yield REAL,
    week52_high    REAL,
    week52_low     REAL,
    avg_volume     INTEGER,
    avg_volume_10d INTEGER,
    source         TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at     TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS watchlist (
    user_id    INTEGER NOT NULL,
    ticker     TEXT    NOT NULL,
    added_at   TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    PRIMARY KEY (user_id, ticker)
);

CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY,
    ticker       TEXT    NOT NULL,
    headline     TEXT,
    summary      TEXT,
    source       TEXT,
    url          TEXT,
    published_at TEXT,
    fetched_at   TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_news_ticker ON news (ticker, published_at);

CREATE INDEX IF NOT EXISTS idx_positions_snapshot   ON positions (snapshot_date);
CREATE INDEX IF NOT EXISTS idx_positions_symbol     ON positions (symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_symbol  ON transactions (symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions (account);
