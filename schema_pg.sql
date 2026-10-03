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
--   REAL                              -> DOUBLE PRECISION
--     (SQLite's REAL is 8-byte; Postgres REAL is 4-byte and would lose
--     cents on large amounts). TEXT and INTEGER are the same in both.
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

CREATE TABLE IF NOT EXISTS advisor_clients (
    advisor_id INTEGER NOT NULL,
    client_id  INTEGER NOT NULL,
    client_name TEXT,          -- what the advisor calls them ("Chen household"); auth.set_client_name
    PRIMARY KEY (advisor_id, client_id)
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
    quantity           DOUBLE PRECISION,
    cost_basis         DOUBLE PRECISION,
    market_value       DOUBLE PRECISION,
    price_change_pct   DOUBLE PRECISION,
    day_change_pct     DOUBLE PRECISION,
    reported_gain      DOUBLE PRECISION,
    reported_gain_pct  DOUBLE PRECISION,
    reinvest           INTEGER,
    reinvest_cap_gains INTEGER,
    div_pay_date       TEXT,
    div_yield_pct      DOUBLE PRECISION,
    next_earnings_date TEXT,
    pct_of_account     DOUBLE PRECISION,
    source_file        TEXT,
    user_id            INTEGER NOT NULL,
    imported_at        TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    UNIQUE (snapshot_date, account, symbol, user_id)
);

CREATE TABLE IF NOT EXISTS account_totals (
    id                    SERIAL  PRIMARY KEY,
    snapshot_date         TEXT    NOT NULL,
    account               TEXT    NOT NULL,
    cash_value            DOUBLE PRECISION,
    reported_cost_basis   DOUBLE PRECISION,
    reported_market_value DOUBLE PRECISION,
    reported_gain         DOUBLE PRECISION,
    reported_gain_pct     DOUBLE PRECISION,
    source_file           TEXT,
    user_id               INTEGER NOT NULL,
    imported_at           TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    UNIQUE (snapshot_date, account, user_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id         SERIAL  PRIMARY KEY,
    ticker     TEXT    NOT NULL,
    price      DOUBLE PRECISION,
    prev_close DOUBLE PRECISION,
    change     DOUBLE PRECISION,
    pct_change DOUBLE PRECISION,
    day_open   DOUBLE PRECISION,
    day_high   DOUBLE PRECISION,
    day_low    DOUBLE PRECISION,
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
    quantity      DOUBLE PRECISION,
    price         DOUBLE PRECISION,
    amount        DOUBLE PRECISION,
    fees          DOUBLE PRECISION,
    realized_gain DOUBLE PRECISION,
    source_file   TEXT,
    origin        TEXT,                             -- 'imported' (txn_import.py); NULL = worked out from updates
    row_key       TEXT,                             -- an imported row's fingerprint, so re-imports add only what's new
    imported_at   TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS value_log (
    id                  SERIAL  PRIMARY KEY,
    logged_at           TEXT    NOT NULL,
    snapshot_date       TEXT,
    source              TEXT    NOT NULL DEFAULT 'app_open',
    portfolio_value     DOUBLE PRECISION,
    holdings_value      DOUBLE PRECISION,
    cash                DOUBLE PRECISION,
    cost_basis          DOUBLE PRECISION,
    unrealized_gain     DOUBLE PRECISION,
    unrealized_gain_pct DOUBLE PRECISION,
    day_change_usd      DOUBLE PRECISION,
    n_positions         INTEGER,
    n_priced            INTEGER,
    priced_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_value_log_time ON value_log (logged_at);

CREATE TABLE IF NOT EXISTS daily_bars (
    ticker     TEXT    NOT NULL,
    date       TEXT    NOT NULL,
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION,
    adj_close  DOUBLE PRECISION,
    volume     INTEGER,
    source     TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    PRIMARY KEY (ticker, date)
);
-- (the primary key already indexes (ticker, date); an old copy is dropped in portfolio._ensure_schema)

CREATE TABLE IF NOT EXISTS intraday_bars (
    ticker     TEXT    NOT NULL,
    interval   TEXT    NOT NULL,
    ts         TEXT    NOT NULL,
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION,
    volume     INTEGER,
    source     TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    PRIMARY KEY (ticker, interval, ts)
);
-- (the primary key already indexes (ticker, interval, ts))

CREATE TABLE IF NOT EXISTS security_info (
    ticker         TEXT    PRIMARY KEY,
    name           TEXT,
    sector         TEXT,
    industry       TEXT,
    market_cap     DOUBLE PRECISION,
    beta           DOUBLE PRECISION,
    trailing_pe    DOUBLE PRECISION,
    forward_pe     DOUBLE PRECISION,
    price_to_book  DOUBLE PRECISION,
    dividend_yield DOUBLE PRECISION,
    week52_high    DOUBLE PRECISION,
    week52_low     DOUBLE PRECISION,
    avg_volume     INTEGER,
    avg_volume_10d INTEGER,
    source         TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at     TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);

-- A fund's top holdings from Yahoo (fund_holdings.py, the Fund overlap window):
-- shared market data like security_info, no user_id. Fetched on demand and
-- asked again at most once a week. Slot 0 records when the fund was asked
-- (no holding: Yahoo may list none); slots 1.. are its largest holdings.
CREATE TABLE IF NOT EXISTS fund_top_holdings (
    fund        TEXT    NOT NULL,                   -- the fund's ticker, as held
    slot        INTEGER NOT NULL,                   -- 1 = its largest; 0 = when it was asked
    symbol      TEXT,                               -- the holding's ticker ('' if none)
    name        TEXT,
    weight      DOUBLE PRECISION,                   -- a fraction of the fund (0.064 = 6.4%)
    source      TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at  TEXT    NOT NULL,                   -- ISO 'YYYY-MM-DDTHH:MM:SSZ' UTC
    PRIMARY KEY (fund, slot)
);

CREATE TABLE IF NOT EXISTS investor_profiles (
    user_id            INTEGER PRIMARY KEY,
    goal               TEXT,
    time_horizon_years INTEGER,
    target_return_pct  DOUBLE PRECISION,
    risk_tolerance     TEXT,
    experience         TEXT,
    drawdown_reaction  TEXT,
    age_range          TEXT,
    income_stability   TEXT,
    emergency_fund     TEXT,
    high_interest_debt TEXT,
    employer_match     TEXT,
    contributions      TEXT,
    withdrawal_needs   TEXT,
    preferences        TEXT,
    ai_memory          TEXT,
    notes              TEXT,
    updated_at         TEXT
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

CREATE TABLE IF NOT EXISTS account_labels (
    user_id    INTEGER NOT NULL,
    account    TEXT    NOT NULL,
    nickname   TEXT    NOT NULL,
    PRIMARY KEY (user_id, account)
);

CREATE TABLE IF NOT EXISTS login_sessions (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')),
    expires_at  TEXT    NOT NULL,
    two_step_until TEXT
);

-- Two-step sign-in - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS two_step (
    user_id           INTEGER PRIMARY KEY,
    totp_secret       TEXT    NOT NULL,
    backup_codes_hash TEXT    NOT NULL DEFAULT '',
    enabled_at        TEXT    NOT NULL,
    last_token_step   INTEGER NOT NULL DEFAULT 0
);

-- One-time setup links an advisor sends a client (auth.create_invite): the
-- client opens it and chooses their own password, so none is ever shared.
-- Only the token's hash is kept; a link works once, until expires_at.
CREATE TABLE IF NOT EXISTS invites (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,                    -- the client account it sets up
    created_by  INTEGER NOT NULL,                    -- the advisor
    created_at  TEXT    NOT NULL,                    -- 'YYYY-MM-DD HH:MM:SS' UTC
    expires_at  TEXT    NOT NULL
);

-- How many AI requests each account made per month, per feature (ai_usage.py
-- enforces the monthly allowances). Counts only - never what was asked.
CREATE TABLE IF NOT EXISTS ai_usage (
    user_id  INTEGER NOT NULL,
    month    TEXT    NOT NULL,                       -- 'YYYY-MM' (UTC)
    kind     TEXT    NOT NULL,                       -- chat / screenshot / csv / plan
    used     INTEGER NOT NULL,
    PRIMARY KEY (user_id, month, kind)
);

-- Every decimal column in this file is DOUBLE PRECISION: Postgres REAL is
-- 4-byte and loses cents on large amounts. portfolio._widen_real_columns()
-- converts any REAL column an older database still has.
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id   INTEGER PRIMARY KEY,
    data      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    user_id              INTEGER PRIMARY KEY,
    goal_type            TEXT,
    goal_name            TEXT,
    target_amount        DOUBLE PRECISION,
    target_date          TEXT,
    monthly_contribution DOUBLE PRECISION,
    target_alloc         TEXT,
    notes                TEXT,
    set_by               INTEGER,
    updated_at           TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS contributions (
    id          SERIAL  PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    date        TEXT    NOT NULL,
    amount      DOUBLE PRECISION NOT NULL,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_contributions_user ON contributions (user_id, date);

-- Advisor notes on a client's account (advising.py): a Review (meeting), a
-- Note, or a Next step (done when the advisor ticks it). Private notes are
-- never shown to the client.
CREATE TABLE IF NOT EXISTS advisor_notes (
    id          SERIAL  PRIMARY KEY,
    client_id   INTEGER NOT NULL,
    advisor_id  INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    note_date   TEXT    NOT NULL,                -- YYYY-MM-DD
    private     INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_advisor_notes_client ON advisor_notes (client_id, note_date);

-- An advisor's saved target mixes by asset type, applied to clients' plans.
CREATE TABLE IF NOT EXISTS model_portfolios (
    id            SERIAL  PRIMARY KEY,
    advisor_id    INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    target_alloc  TEXT    NOT NULL,              -- JSON {asset class: target %} (asset_classes.py)
    updated_at    TEXT    NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
);

-- Failed logins per username, for the lockout (auth.attempt_login): after
-- MAX_FAILED_LOGINS wrong passwords in LOCKOUT_MINUTES, that username is
-- locked for LOCKOUT_MINUTES. Keyed by a SHA-256 of the username as typed
-- (case-folded), known or not - so the lock reveals nothing about which
-- usernames exist, and a password typed into the username box isn't stored.
CREATE TABLE IF NOT EXISTS login_failures (
    username_key  TEXT    PRIMARY KEY,
    failures      INTEGER NOT NULL,
    window_start  TEXT    NOT NULL,              -- 'YYYY-MM-DD HH:MM:SS' UTC
    locked_until  TEXT
);

-- Sign-up tries - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS signups (
    address_key  TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,               -- 'YYYY-MM-DD HH:MM:SS' UTC
    ok           INTEGER NOT NULL                -- 1 = an account was made
);

-- Emailed one-time links - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    purpose     TEXT    NOT NULL,                -- 'confirm', 'reset' or 'change' (email = the new one)
    email       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,                -- 'YYYY-MM-DD HH:MM:SS' UTC
    expires_at  TEXT    NOT NULL
);

-- An advisor's proposed mix for a client - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS proposals (
    id            SERIAL  PRIMARY KEY,
    advisor_id    INTEGER NOT NULL,
    client_id     INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    mix_json      TEXT    NOT NULL,              -- JSON {asset class: %} (asset_classes.py)
    note          TEXT,                          -- the advisor's reasoning, for the client
    status        TEXT    NOT NULL,              -- draft / shared / accepted / declined
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    shared_at     TEXT,
    responded_at  TEXT
);

-- Progress reports - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS progress_reports (
    id            SERIAL  PRIMARY KEY,
    advisor_id    INTEGER NOT NULL,
    client_id     INTEGER NOT NULL,
    period_label  TEXT    NOT NULL,              -- e.g. "Q3 2026"
    period_start  TEXT    NOT NULL,              -- YYYY-MM-DD
    period_end    TEXT    NOT NULL,
    facts_json    TEXT    NOT NULL,              -- reports.build()
    message       TEXT,
    created_at    TEXT    NOT NULL,
    read_at       TEXT
);

-- Asking for advisor access - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS advisor_requests (
    user_id       INTEGER PRIMARY KEY,
    firm          TEXT    NOT NULL,
    licence       TEXT    NOT NULL,
    requested_at  TEXT    NOT NULL,              -- 'YYYY-MM-DD HH:MM:SS' UTC
    decision      TEXT,                          -- NULL while waiting, 'approved', 'declined'
    decided_at    TEXT
);

-- Emails asked for - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS email_sends (
    email_key    TEXT NOT NULL,
    address_key  TEXT NOT NULL,                  -- '' if unknown
    purpose      TEXT NOT NULL,
    sent_at      TEXT NOT NULL                   -- 'YYYY-MM-DD HH:MM:SS' UTC
);

-- Column layouts of brokerage CSVs seen before (csv_import.py): a fingerprint
-- of the column NAMES -> which column holds which field. No holdings or
-- personal data - so the next file with the same columns needs no questions.
CREATE TABLE IF NOT EXISTS csv_layouts (
    signature   TEXT PRIMARY KEY,
    mapping     TEXT NOT NULL,                   -- JSON {field: column index}
    updated_at  TEXT NOT NULL
);

-- Errors and failed jobs, one row per kind - see the matching comment in schema.sql.
CREATE TABLE IF NOT EXISTS error_events (
    kind        TEXT PRIMARY KEY,
    source      TEXT    NOT NULL,                -- 'app' or 'job'
    error_type  TEXT    NOT NULL,
    place       TEXT    NOT NULL,
    line        INTEGER,
    first_seen  TEXT    NOT NULL,                -- ISO 'YYYY-MM-DDTHH:MM:SSZ' UTC
    last_seen   TEXT    NOT NULL,
    times       INTEGER NOT NULL DEFAULT 1,
    emailed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_snapshot   ON positions (snapshot_date);
CREATE INDEX IF NOT EXISTS idx_positions_symbol     ON positions (symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_symbol  ON transactions (symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions (account);
