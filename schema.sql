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
    client_name TEXT,          -- what the advisor calls them ("Chen household"); auth.set_client_name
    PRIMARY KEY (advisor_id, client_id)
);

-- One row per (positions export file, as-of date) that has been imported.
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT    NOT NULL,              -- ISO date parsed from the export header, e.g. 2026-08-28
    as_of_text    TEXT,                          -- raw "as of ..." string from the file
    source_file   TEXT    NOT NULL,              -- the CSV's path, 'upload: <name>', 'manual entry', 'percentages' or 'sample portfolio'
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
    origin        TEXT,                             -- 'imported' (txn_import.py); NULL = worked out from updates
    row_key       TEXT,                             -- an imported row's fingerprint, so re-imports add only what's new
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
-- (the primary key already indexes (ticker, date); an old copy is dropped in portfolio._ensure_schema)

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
-- (the primary key already indexes (ticker, interval, ts))

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

-- A fund's top holdings from Yahoo (fund_holdings.py, the Fund overlap window):
-- shared market data like security_info, no user_id. Fetched on demand and
-- asked again at most once a week. Slot 0 records when the fund was asked
-- (no holding: Yahoo may list none); slots 1.. are its largest holdings.
CREATE TABLE IF NOT EXISTS fund_top_holdings (
    fund        TEXT    NOT NULL,                   -- the fund's ticker, as held
    slot        INTEGER NOT NULL,                   -- 1 = its largest; 0 = when it was asked
    symbol      TEXT,                               -- the holding's ticker ('' if none)
    name        TEXT,
    weight      REAL,                               -- a fraction of the fund (0.064 = 6.4%)
    source      TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at  TEXT    NOT NULL,                   -- ISO 'YYYY-MM-DDTHH:MM:SSZ' UTC
    PRIMARY KEY (fund, slot)
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
    drawdown_reaction  TEXT,
    age_range          TEXT,
    income_stability   TEXT,
    emergency_fund     TEXT,
    high_interest_debt TEXT,
    employer_match     TEXT,
    contributions      TEXT,
    withdrawal_needs   TEXT,
    preferences        TEXT,
    ai_memory          TEXT,                      -- the assistant's own notes, never shown in the app
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

-- A user's own display name for a broker account ("Roth IRA" instead of
-- "Individual ...111"). Display only: positions keep the broker's name.
CREATE TABLE IF NOT EXISTS account_labels (
    user_id    INTEGER NOT NULL,
    account    TEXT    NOT NULL,
    nickname   TEXT    NOT NULL,
    PRIMARY KEY (user_id, account)
);

-- "Stay signed in": one row per signed-in browser. Only a SHA-256 hash of
-- the cookie's random token is stored, so a copy of this table can't be used
-- to sign in. Rows are deleted on logout and on a password change.
-- two_step_until: "remember this device" (two_step.remember_device) - until
-- then this browser isn't asked for a two-step code (NULL: it is).
CREATE TABLE IF NOT EXISTS login_sessions (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT    NOT NULL,                    -- 'YYYY-MM-DD HH:MM:SS' UTC
    two_step_until TEXT                              -- 'YYYY-MM-DD HH:MM:SS' UTC
);

-- Two-step sign-in (two_step.py, ROADMAP R2): one row per login that has it
-- on. The authenticator app's secret key has to be readable to check codes,
-- so it is never shown again after setup, never exported and never shown to
-- an admin. Backup codes are kept only as SHA-256 hashes (space-separated,
-- unused ones only). last_token_step stops one code being used twice.
CREATE TABLE IF NOT EXISTS two_step (
    user_id           INTEGER PRIMARY KEY,
    totp_secret       TEXT    NOT NULL,              -- base32
    backup_codes_hash TEXT    NOT NULL DEFAULT '',
    enabled_at        TEXT    NOT NULL,              -- 'YYYY-MM-DD HH:MM:SS' UTC
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

-- Per-account dashboard settings (chosen columns, alert limits, hide
-- amounts, ...) as one JSON object. Was a .dashboard_prefs.<id>.json file,
-- which a hosted app loses on every restart.
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id   INTEGER PRIMARY KEY,
    data      TEXT    NOT NULL
);

-- One plan per account: a goal, how much is added toward it, and a target
-- mix. Written by the account owner or their advisor (set_by).
CREATE TABLE IF NOT EXISTS plans (
    user_id              INTEGER PRIMARY KEY,
    goal_type            TEXT,
    goal_name            TEXT,
    target_amount        REAL,
    target_date          TEXT,                  -- YYYY-MM-DD
    monthly_contribution REAL,
    target_alloc         TEXT,                  -- JSON {asset class: target %} (asset_classes.py)
    notes                TEXT,
    set_by               INTEGER,               -- users.id of whoever last saved it
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Money added to (or, negative, taken out of) an account, logged by hand.
CREATE TABLE IF NOT EXISTS contributions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    date        TEXT    NOT NULL,               -- YYYY-MM-DD
    amount      REAL    NOT NULL,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contributions_user ON contributions (user_id, date);

-- Advisor notes on a client's account (advising.py): a Review (meeting), a
-- Note, or a Next step (done when the advisor ticks it). Private notes are
-- never shown to the client.
CREATE TABLE IF NOT EXISTS advisor_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL,
    advisor_id  INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    note_date   TEXT    NOT NULL,                -- YYYY-MM-DD
    private     INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_advisor_notes_client ON advisor_notes (client_id, note_date);

-- An advisor's saved target mixes by asset type, applied to clients' plans.
CREATE TABLE IF NOT EXISTS model_portfolios (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    advisor_id    INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    target_alloc  TEXT    NOT NULL,              -- JSON {asset class: target %} (asset_classes.py)
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
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

-- Sign-up tries, for the limits on new accounts (auth.sign_up). Keyed by a
-- SHA-256 of the internet address ('' if unknown) - never the address or the
-- email - and kept for a day.
CREATE TABLE IF NOT EXISTS signups (
    address_key  TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,               -- 'YYYY-MM-DD HH:MM:SS' UTC
    ok           INTEGER NOT NULL                -- 1 = an account was made
);

-- One-time links emailed to people (auth.start_confirmation /
-- request_password_reset): confirm an email, or reset a password. Only the
-- token's hash is stored; `email` is the address it was sent to, so a link
-- stops working if the account's email changes.
CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    purpose     TEXT    NOT NULL,                -- 'confirm', 'reset' or 'change' (email = the new one)
    email       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,                -- 'YYYY-MM-DD HH:MM:SS' UTC
    expires_at  TEXT    NOT NULL
);

-- An advisor's proposed mix for a client (proposals.py): a draft only the
-- advisor sees, then shared, then the client's answer.
CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- Progress reports an advisor sends a client (reports.py): the figures as
-- they were when sent, and the advisor's message.
CREATE TABLE IF NOT EXISTS progress_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- Asking for advisor access (auth.request_advisor): the firm and CRD or
-- licence number the admin checks before making the account an advisor
-- (manage_users.py make-advisor / decline-advisor). One row per account.
CREATE TABLE IF NOT EXISTS advisor_requests (
    user_id       INTEGER PRIMARY KEY,
    firm          TEXT    NOT NULL,
    licence       TEXT    NOT NULL,
    requested_at  TEXT    NOT NULL,              -- 'YYYY-MM-DD HH:MM:SS' UTC
    decision      TEXT,                          -- NULL while waiting, 'approved', 'declined'
    decided_at    TEXT
);

-- Emails asked for, for the limits on them (auth._email_limit). Hashes of the
-- email typed and the internet address only; kept for a day.
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

-- Unexpected errors and failed scheduled jobs, one row per kind (error_alerts.py):
-- the error's type and the file/function where it happened - never its message,
-- anyone's data or a user_id. Limits the admin's alert emails to one an hour.
CREATE TABLE IF NOT EXISTS error_events (
    kind        TEXT PRIMARY KEY,                -- 'KeyError in views/plan.py, _render_tab'
    source      TEXT    NOT NULL,                -- 'app' or 'job'
    error_type  TEXT    NOT NULL,
    place       TEXT    NOT NULL,                -- 'views/plan.py, _render_tab' or the job
    line        INTEGER,                         -- line of the latest one
    first_seen  TEXT    NOT NULL,                -- ISO 'YYYY-MM-DDTHH:MM:SSZ' UTC
    last_seen   TEXT    NOT NULL,
    times       INTEGER NOT NULL DEFAULT 1,
    emailed_at  TEXT                             -- last alert email, NULL if none
);

CREATE INDEX IF NOT EXISTS idx_positions_snapshot   ON positions (snapshot_date);
CREATE INDEX IF NOT EXISTS idx_positions_symbol     ON positions (symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_symbol  ON transactions (symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions (account);
