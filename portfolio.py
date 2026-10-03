#!/usr/bin/env python3
"""Personal portfolio tracker - phase 1.

Subcommands:
  import <positions.csv> [--db portfolio.db]    parse a Schwab Positions export into SQLite
  verify [--db portfolio.db] [--snapshot DATE]  check parsed holdings against the file's totals
  report [--db portfolio.db] [--snapshot DATE]  print an unrealized gain/loss summary

Standard library only (sqlite3, csv, argparse). No network calls.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import date

import pgcompat

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
SCHEMA_PG_PATH = os.path.join(HERE, "schema_pg.sql")
DEFAULT_DB = os.path.join(os.getcwd(), "portfolio.db")

# A single except clause that catches the right database error regardless
# of backend - sqlite3.Error for local SQLite, psycopg.Error too once a
# Postgres DSN is in use. psycopg is only imported lazily by pgcompat.connect()
# itself, so this stays import-safe (and this tuple just collapses to plain
# sqlite3.Error) on a machine that never installed psycopg for local use.
try:
    import psycopg
    DBError: tuple[type[BaseException], ...] | type[BaseException] = (sqlite3.Error, psycopg.Error)
except ImportError:
    DBError = sqlite3.Error

NULLISH = {"", "--", "-", "n/a", "na"}
TOLERANCE = 0.01  # dollars; sums of 2-dp figures should reconcile exactly


# --------------------------------------------------------------------------- #
# value parsing
# --------------------------------------------------------------------------- #
def parse_num(raw):
    """Currency / percent strings -> float.

    '$1,007.19' -> 1007.19   '-$290.04' -> -290.04   '-28.8%' -> -28.8
    '($290.04)' -> -290.04    '--' / 'N/A' / '' -> None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s.lower() in NULLISH:
        return None
    negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = (s.replace("(", "").replace(")", "")
          .replace("$", "").replace(",", "")
          .replace("%", "").replace("-", "").strip())
    if s.lower() in NULLISH:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


# --------------------------------------------------------------------------- #
# reading a positions export (any brokerage - csv_import.py)
# --------------------------------------------------------------------------- #
def parse_csv(path: str):
    """Read a positions export from any brokerage (csv_import.py, which finds
    the table and matches its columns by name - Schwab's layout is just one of
    the layouts it knows). Returns (meta, positions, account_totals) in the
    shapes write_snapshot() takes:

      meta           -> {"snapshot_date": "YYYY-MM-DD", "as_of_text": str|None}
      positions      -> list of holding dicts (real holdings only)
      account_totals -> {account: {"cash_value", "reported_cost_basis",
                                   "reported_market_value", "reported_gain",
                                   "reported_gain_pct"}}

    Raises SystemExit with a plain message for a file with no holdings table,
    or a transactions export. A layout whose columns can't be matched by name
    also raises; the dashboard asks which column is which instead."""
    import csv_import
    with open(path, "rb") as fh:
        rows = csv_import.read_rows(fh.read())
    header_i, problem = csv_import.find_header(rows)
    if problem == "transactions":
        raise SystemExit("This looks like transaction history, not current holdings.")
    if header_i is None:
        raise SystemExit("Couldn't find a table of holdings (a Symbol column and a Quantity "
                         "or Value column) in this file.")
    mapping = csv_import.auto_mapping(rows[header_i])
    found = csv_import.parse(rows, mapping, filename=os.path.basename(path))
    if not found["holdings"]:
        raise SystemExit("No holding rows found in the file.")
    return csv_import.to_snapshot(found)


# --------------------------------------------------------------------------- #
# database helpers
# --------------------------------------------------------------------------- #
# Columns added by later features. SQLite has no "ADD COLUMN IF NOT EXISTS", so
# connect() adds any that are missing (keeps old databases working).
LIVE_POSITION_COLS = [
    ("live_price", "REAL"),                 # last live per-share price
    ("live_market_value", "REAL"),          # live_price * quantity
    ("live_unrealized_gain", "REAL"),       # live_market_value - cost_basis
    ("live_unrealized_gain_pct", "REAL"),
    ("live_price_at", "TEXT"),              # when the live price was applied, ISO-8601 UTC
]

# Intraday fields the Finnhub /quote response already carries; stored so the
# dashboard can offer "day open / high / low" columns.
PRICE_HISTORY_EXTRA_COLS = [
    ("day_open", "REAL"),
    ("day_high", "REAL"),
    ("day_low", "REAL"),
]

TRANSACTIONS_EXTRA_COLS = [
    ("realized_gain", "REAL"),
    # imported activity exports (txn_import.py)
    ("origin", "TEXT"), ("row_key", "TEXT"),
]

# Multi-user data isolation, added after the app already had real data in
# these 5 tables (unlike watchlist, which was empty everywhere and could
# just get user_id baked into its CREATE TABLE directly - see schema.sql).
USER_ID_COL = [("user_id", "INTEGER")]
USER_INDEXES = [
    ("idx_snapshots_user", "snapshots", "user_id, snapshot_date"),
    ("idx_positions_user", "positions", "user_id, snapshot_date"),
    ("idx_account_totals_user", "account_totals", "user_id, snapshot_date"),
    ("idx_transactions_user", "transactions", "user_id, trade_date"),
    ("idx_value_log_user", "value_log", "user_id, logged_at"),
]


# Investing-profile questions and the assistant's memory, added after
# investor_profiles already existed.
PROFILE_EXTRA_COLS = [(c, "TEXT") for c in (
    "drawdown_reaction", "age_range", "income_stability", "emergency_fund",
    "high_interest_debt", "contributions", "withdrawal_needs", "preferences", "ai_memory",
    "employer_match")]


# Schema creation + column back-fill is idempotent but not free; once a given
# database file has been set up in this process, later connect() calls skip it.
_SCHEMA_READY: set[str] = set()
# Only one thread sets a database up at a time. Right after a deploy several
# sessions connect at once, and concurrent CREATE TABLE IF NOT EXISTS on
# Postgres fails with a UniqueViolation instead of waiting.
_SCHEMA_LOCK = threading.Lock()
# Same thing across processes (the app and a scheduled job starting together):
# a Postgres advisory lock held for the setup transaction. Any fixed number.
SCHEMA_ADVISORY_LOCK_ID = 7215346


def _ensure_schema(conn) -> None:
    is_pg = isinstance(conn, pgcompat.ConnWrapper)
    schema_path = SCHEMA_PG_PATH if is_pg else SCHEMA_PATH
    if is_pg:
        conn.execute("SELECT pg_advisory_xact_lock(CAST(? AS BIGINT))", (SCHEMA_ADVISORY_LOCK_ID,))
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema_text = fh.read()
    conn.executescript(schema_text)  # Postgres: one round trip (pgcompat.executescript)
    backfill = (("positions", LIVE_POSITION_COLS),
                ("price_history", PRICE_HISTORY_EXTRA_COLS),
                # dividend per share paid on that day's ex-date, 0 if none
                # (income.py) - None until the bar is re-synced
                ("daily_bars", [("dividend", "REAL")]),
                ("transactions", TRANSACTIONS_EXTRA_COLS),
                ("snapshots", USER_ID_COL),
                ("positions", USER_ID_COL),
                ("account_totals", USER_ID_COL),
                ("transactions", USER_ID_COL),
                ("value_log", USER_ID_COL),
                ("users", [("is_advisor", "INTEGER"),
                           ("ai_unlimited", "INTEGER"),  # ai_usage.py
                           # self-serve sign-up (auth.sign_up)
                           ("email", "TEXT"), ("email_verified_at", "TEXT"),
                           ("terms_version", "TEXT"), ("terms_accepted_at", "TEXT"),
                           # the admin portal (admin.py)
                           ("is_admin", "INTEGER"), ("last_login_at", "TEXT"),
                           # the Account page (auth.set_display_name)
                           ("display_name", "TEXT")]),
                ("advisor_clients", [("client_can_import", "INTEGER"),
                                     # the advisor's name for them (auth.set_client_name)
                                     ("client_name", "TEXT")]),
                # what a fund holds, from Yahoo (asset_classes.py)
                ("security_info", [("quote_type", "TEXT"), ("category", "TEXT"),
                                   ("stock_pct", "REAL"), ("bond_pct", "REAL"),
                                   ("cash_pct", "REAL"), ("other_pct", "REAL"),
                                   # a fund's yearly fee, a fraction (fees.py)
                                   ("expense_ratio", "REAL")]),
                ("plans", [("targets_cleared", "INTEGER")]),
                # "remember this device" for two-step sign-in (two_step.py)
                ("login_sessions", [("two_step_until", "TEXT")]),
                ("investor_profiles", PROFILE_EXTRA_COLS))
    tables = list(dict.fromkeys(t for t, _ in backfill))
    have: dict[str, set] = {t: set() for t in tables}
    if is_pg:
        # every table's columns in one query: on Postgres each query is a
        # round trip, and this runs at every process start
        for r in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                f"WHERE table_name IN ({', '.join('?' for _ in tables)})", tables):
            have[r["table_name"]].add(r["column_name"])
    else:
        for table in tables:
            have[table] = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for table, cols in backfill:
        for name, decl in cols:
            if name not in have[table]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} "
                             f"{'DOUBLE PRECISION' if is_pg and decl == 'REAL' else decl}")
                have[table].add(name)
    # Indexes led by user_id - nearly every read is "this account's ...". Made
    # here, after the back-fill above, since older databases only now have
    # the column. The two dropped ones duplicated their table's primary key.
    indexes = [f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"
               for name, table, cols in USER_INDEXES]
    indexes += [
        # one account per email (NULL for accounts made by an admin or advisor)
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users (email)",
        "CREATE INDEX IF NOT EXISTS idx_signups_time ON signups (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_email_sends_email ON email_sends (email_key, sent_at)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_client ON proposals (client_id)",
        "CREATE INDEX IF NOT EXISTS idx_reports_client ON progress_reports (client_id)",
        *(f"DROP INDEX IF EXISTS {name}"
          for name in ("idx_daily_bars_ticker", "idx_intraday_bars_lookup"))]
    if is_pg:
        conn.executescript(";\n".join(indexes))  # one round trip
    else:
        for stmt in indexes:
            conn.execute(stmt)
    masked = _mask_saved_accounts(conn)
    if masked:
        print(f"schema: account numbers cut to their last 3 digits: {masked}", file=sys.stderr)
    # saved targets from before Stocks / Bonds / Cash / Other; a no-op once done
    import asset_classes
    moved = asset_classes.migrate_targets(conn)
    if moved["plans"] or moved["models"]:
        print(f"schema: targets moved to asset classes: {moved}", file=sys.stderr)
    if is_pg:
        widened = _widen_real_columns(conn, schema_text)
        if widened:
            print(f"schema: {len(widened)} REAL column(s) changed to DOUBLE PRECISION: "
                  + ", ".join(widened), file=sys.stderr)
    conn.commit()


# Every table with an account name in it, and the columns that, with the
# account, must stay unique (a name masked to one already there is left).
ACCOUNT_COLUMNS = {"transactions": None,
                   "positions": ("snapshot_date", "symbol", "user_id"),
                   "account_totals": ("snapshot_date", "user_id"),
                   "account_labels": ("user_id",)}


def _mask_saved_accounts(conn) -> dict:
    """A clean-up of rows saved before every path masked account names (hand
    entry and imports once stored worked-out trades with the file's full
    account number): any account name with a full number is cut to its last
    3 digits (accounts.mask_number). Reads only the distinct names (one query)
    and updates only those that differ, so once done it changes nothing.
    {table: rows changed}."""
    from accounts import mask_number
    found = conn.execute(" UNION ALL ".join(
        f"SELECT '{t}' AS t, account FROM (SELECT DISTINCT account FROM {t}) d{i}"
        for i, t in enumerate(ACCOUNT_COLUMNS))).fetchall()
    changed: dict = {}
    for r in found:
        table, old = r["t"], r["account"]
        new = mask_number(old)
        if new == old:
            continue
        keys = ACCOUNT_COLUMNS[table]
        clash = "" if not keys else (
            f" AND NOT EXISTS (SELECT 1 FROM {table} o WHERE o.account = ? AND "
            + " AND ".join(f"o.{k} = {table}.{k}" for k in keys) + ")")
        # Two accounts ending in the same 3 digits, both holding the same thing
        # that day, can't share a name: the second becomes "... (2)".
        for name in (new, *(f"{new} ({n})" for n in range(2, 6))):
            cur = conn.execute(f"UPDATE {table} SET account = ? WHERE account = ?{clash}",
                               (name, old, name) if keys else (name, old))
            if cur.rowcount and cur.rowcount > 0:
                changed[table] = changed.get(table, 0) + cur.rowcount
            if not keys or not conn.execute(f"SELECT 1 FROM {table} WHERE account = ? LIMIT 1",
                                            (old,)).fetchone():
                break
    return changed


def _widen_real_columns(conn, schema_text: str) -> list[str]:
    """Postgres REAL is a 4-byte float - about 7 significant digits, so money
    over roughly $1M loses cents (SQLite's REAL is 8-byte and fine). Change
    every REAL column in this app's own tables (those schema_pg.sql creates)
    to DOUBLE PRECISION, one ALTER per table so each is rewritten once.
    Existing values keep what float4 stored; everything written afterwards is
    exact to the cent. Once converted there's nothing left to do. Returns the
    "table.column" names it changed."""
    ours = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema_text))
    by_table: dict[str, list[str]] = {}
    for r in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND data_type = 'real' "
            "ORDER BY table_name, ordinal_position").fetchall():
        if r["table_name"] in ours:
            by_table.setdefault(r["table_name"], []).append(r["column_name"])
    for table, cols in by_table.items():
        conn.execute(f'ALTER TABLE "{table}" ' + ", ".join(
            f'ALTER COLUMN "{c}" TYPE DOUBLE PRECISION' for c in cols))
    return [f"{t}.{c}" for t, cols in by_table.items() for c in cols]


def connect(db_path: str):
    """Open a Row-factory connection, creating/upgrading the schema on first
    use (once per db_path per process; both schema.sql and schema_pg.sql are
    all CREATE IF NOT EXISTS, so a new table added in an update is picked up
    on the next process start).

    `db_path` is either a local SQLite file path (every local `streamlit
    run` / CLI invocation - the default, and the only path that has ever
    been used before this) or a Postgres connection string
    (postgres://... / postgresql://...), detected automatically via
    pgcompat.is_postgres_dsn() - no separate flag needed anywhere upstream."""
    if pgcompat.is_postgres_dsn(db_path):
        conn = pgcompat.connect(db_path)
        key = db_path
    else:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        key = os.path.abspath(db_path)
    if key not in _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if key not in _SCHEMA_READY:
                try:
                    _ensure_schema(conn)
                except Exception:
                    conn.close()  # don't hand a failed transaction back to the pool
                    raise
                _SCHEMA_READY.add(key)
    return conn


def money(value, width: int = 0) -> str:
    if value is None:
        return "-".rjust(width)
    text = f"{'-' if value < 0 else ''}${abs(value):,.2f}"
    return text.rjust(width) if width else text


def pct(value, width: int = 0) -> str:
    if value is None:
        return "-".rjust(width)
    text = f"{value:+.2f}%"
    return text.rjust(width) if width else text


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #
POSITION_COLS = [
    "snapshot_date", "account", "symbol", "description", "asset_type",
    "quantity", "cost_basis", "market_value", "price_change_pct", "day_change_pct",
    "reported_gain", "reported_gain_pct", "reinvest", "reinvest_cap_gains",
    "div_pay_date", "div_yield_pct", "next_earnings_date", "pct_of_account", "source_file",
]


# snapshots.source_file of the example portfolio (sample_data.py)
SAMPLE_SOURCE = "sample portfolio"
# ...and of a percentages-only portfolio (manual_entry.PCT_SOURCE): a pretend
# total, so neither is ever merged with real holdings, nor compared with them
PCT_SOURCE = "percentages"
PRETEND_SOURCES = (SAMPLE_SOURCE, PCT_SOURCE)


def clear_sample(conn, user_id: int) -> None:
    """Delete this account's example-portfolio snapshots, and the visits
    logged while it was showing (so "since your last visit" never compares
    real holdings with made-up ones). No commit - it runs inside the
    caller's transaction."""
    since = conn.execute("SELECT MIN(imported_at) m FROM snapshots WHERE source_file = ? "
                         "AND user_id = ?", (SAMPLE_SOURCE, user_id)).fetchone()["m"]
    if since:  # imported_at is 'YYYY-MM-DD HH:MM:SS', logged_at ISO with a T
        conn.execute("DELETE FROM value_log WHERE user_id = ? AND logged_at >= ?",
                     (user_id, str(since).replace(" ", "T")))
    for table in ("positions", "account_totals", "snapshots"):
        conn.execute(f"DELETE FROM {table} WHERE source_file = ? AND user_id = ?",
                     (SAMPLE_SOURCE, user_id))


# what "Delete all my holdings" removes: everything an import or hand entry
# wrote for the account, plus the visit history built from it
HOLDINGS_TABLES = ("positions", "account_totals", "transactions", "value_log",
                   "account_labels", "snapshots")


def delete_holdings(conn, user_id: int) -> None:
    """Delete every holding, snapshot, cash balance, transaction and value
    history row of one account, in one transaction. Plans, goals, settings,
    the watchlist and the login itself are kept."""
    with conn:
        for table in HOLDINGS_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))


def snapshot_source(conn, user_id: int, snapshot_date: str | None) -> str | None:
    """Where a snapshot came from (its source_file), or None."""
    if not snapshot_date:
        return None
    row = conn.execute("SELECT source_file FROM snapshots WHERE snapshot_date = ? AND user_id = ? "
                       "ORDER BY imported_at DESC LIMIT 1", (snapshot_date, user_id)).fetchone()
    return row["source_file"] if row else None


def previous_snapshot(conn, user_id: int, before: str) -> tuple[str | None, list[dict], set]:
    """What a save dated `before` is compared with: (the latest earlier
    snapshot's date, its holdings, every account it names - cash-only ones
    too). Nothing (None, [], set()) when there is none, or when it's the
    example portfolio or a percentages one, which a real save replaces:
    made-up holdings were never bought or sold."""
    base_date = conn.execute(
        "SELECT MAX(snapshot_date) d FROM snapshots WHERE snapshot_date < ? AND user_id = ?",
        (before, user_id)).fetchone()["d"]   # (one with only cash counts too)
    if not base_date or snapshot_source(conn, user_id, base_date) in PRETEND_SOURCES:
        return None, [], set()
    rows = [dict(r) for r in conn.execute(
        "SELECT account, symbol, description, quantity, cost_basis, market_value "
        "FROM positions WHERE snapshot_date = ? AND user_id = ?", (base_date, user_id))]
    accounts = {r["account"] for r in rows} | {r["account"] for r in conn.execute(
        "SELECT account FROM account_totals WHERE snapshot_date = ? AND user_id = ?",
        (base_date, user_id))}
    return base_date, rows, accounts


# an account's cash and the brokerage's own totals (account_totals)
TOTALS_COLS = ("cash_value", "reported_cost_basis", "reported_market_value", "reported_gain",
               "reported_gain_pct")


def current_holdings(conn, user_id: int) -> dict:
    """The latest holdings - what Home shows (update_prices.latest_snapshot)
    and what a save is merged into: {"date", "source", "as_of_text", "rows"
    (every column of each position), "totals" ({account: cash and totals})}.
    "date" is None when there are none."""
    snap = conn.execute("SELECT MAX(snapshot_date) d FROM positions WHERE user_id = ?",
                        (user_id,)).fetchone()["d"]
    if not snap:
        return {"date": None, "source": None, "as_of_text": None, "rows": [], "totals": {}}
    head = conn.execute("SELECT source_file, as_of_text FROM snapshots WHERE snapshot_date = ? "
                        "AND user_id = ? ORDER BY imported_at DESC LIMIT 1",
                        (snap, user_id)).fetchone()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE snapshot_date = ? AND user_id = ? ORDER BY account, symbol",
        (snap, user_id))]
    totals = {r["account"]: {k: r[k] for k in TOTALS_COLS} for r in conn.execute(
        f"SELECT account, {', '.join(TOTALS_COLS)} FROM account_totals "
        "WHERE snapshot_date = ? AND user_id = ?", (snap, user_id))}
    return {"date": snap, "source": head["source_file"] if head else None,
            "as_of_text": head["as_of_text"] if head else None, "rows": rows, "totals": totals}


def prepare_save(conn, user_id: int, meta: dict, rows: list[dict], totals: dict, src: str, *,
                 whole: bool = False, today: date | None = None) -> dict:
    """Everything a save of new holdings (an import, a paste, screenshots, a
    hand entry) does, worked out for the review before it - nothing is
    written. `meta` / `rows` / `totals` are write_snapshot()'s shapes.

    - The holdings saved are the accounts in `rows` / `totals` as given, and
      every other account of the current holdings carried forward unchanged
      (changes.carry_forward): an import replaces only its own accounts.
    - `whole` (a percentages-only portfolio: one pretend total) replaces
      everything instead, and so does any real save over the example or a
      percentages portfolio - made-up accounts are never carried forward.
    - A file dated before the current holdings is folded into them: saved as
      of today (or the current holdings' date, if later), so no past
      snapshot is rewritten and the new holdings are the latest. No buys or
      sells are worked out from it - it can't say when they happened.
    - What changed, and the buys and sells worked out from it, cover only
      the accounts in the save (the others are unchanged by definition),
      compared with the snapshot before the save's date.

    Returns {"meta", "rows", "totals": what's saved; "updating": the save's
    accounts; "new": those of them not in the current holdings; "kept":
    accounts carried forward; "replaces": None, "example", "percentages" or
    "everything"; "older": the current holdings' date when the file was
    folded into them, else None; "file_date"; "base_date", "diff", "txns"
    (changes.compare, for the save's accounts); "trade_accounts": whose
    worked-out rows the save redoes (None: all); "total": the saved value}."""
    import changes
    from accounts import mask_number as mask
    today = (today or date.today()).isoformat()
    rows, totals = changes.masked(rows), {mask(a): t for a, t in totals.items()}
    cur = current_holdings(conn, user_id)
    file_date = meta["snapshot_date"]
    pretend = cur["date"] is not None and cur["source"] in PRETEND_SOURCES
    replaces = None
    if whole and cur["date"] and cur["source"] != PCT_SOURCE:
        replaces = "example" if cur["source"] == SAMPLE_SOURCE else "everything"
    elif not whole and pretend:
        replaces = "example" if cur["source"] == SAMPLE_SOURCE else "percentages"
    carry = cur["date"] is not None and not whole and not pretend
    older = cur["date"] if carry and file_date < cur["date"] else None
    snap = max(today, cur["date"]) if older else file_date
    meta = {**meta, "snapshot_date": snap}
    rows = [{**r, "snapshot_date": snap} for r in rows]
    updating = changes.accounts_in(rows, totals)
    if carry:
        all_rows, all_totals, kept = changes.carry_forward(cur["rows"], cur["totals"], rows,
                                                           totals, snap)
    else:
        all_rows, all_totals, kept = rows, totals, []
    base_date, base_rows, base_accounts = previous_snapshot(conn, user_id, snap)
    if carry:   # only the accounts in this save are compared
        base_rows = [r for r in base_rows if mask(r["account"]) in updating]
        base_accounts = {a for a in base_accounts if mask(a) in updating}
    d, txns = changes.compare(base_rows, rows, snap, src, old_accounts=base_accounts,
                              new_accounts=totals)
    if whole or older:
        txns = []   # a pretend portfolio, or an older file: no real buys and sells to record
    current = changes.accounts_in(cur["rows"], cur["totals"]) if carry else set()
    return {"meta": meta, "rows": all_rows, "totals": all_totals,
            "updating": sorted(updating), "new": sorted(updating - current), "kept": kept,
            "replaces": replaces, "older": older, "file_date": file_date,
            "base_date": base_date, "diff": d, "txns": txns,
            "trade_accounts": sorted(updating) if carry else None,
            "total": round(sum(r.get("market_value") or 0.0 for r in all_rows)
                           + sum(t.get("cash_value") or 0.0 for t in all_totals.values()), 2)}


def save_prepared(conn, user_id: int, prepared: dict, src: str) -> None:
    """Write what prepare_save() worked out: the snapshot, then the day's
    worked-out buys and sells for the accounts in the save (imported history
    stays; other accounts' rows that day too)."""
    import txn_import
    write_snapshot(conn, user_id, prepared["meta"], prepared["rows"], prepared["totals"], src)
    txn_import.save_worked_out(conn, user_id, prepared["meta"]["snapshot_date"],
                               prepared["txns"], accounts=prepared["trade_accounts"])
    conn.commit()


def remove_account(conn, user_id: int, account: str, *, today: date | None = None) -> str | None:
    """Take one account (its saved name) out of the holdings: a new snapshot,
    dated today (or the current holdings' date, if later), of every other
    account as it is. Earlier snapshots - its history - stay, and no sells
    are recorded: closing or moving an account isn't selling. Returns the
    date saved, or None when the current holdings have no such account."""
    from accounts import mask_number as mask
    cur = current_holdings(conn, user_id)
    account = mask(account)
    if not cur["date"] or account not in {mask(r["account"]) for r in cur["rows"]} | \
            {mask(a) for a in cur["totals"]}:
        return None
    snap = max((today or date.today()).isoformat(), cur["date"])
    rows = [{**r, "snapshot_date": snap} for r in cur["rows"] if mask(r["account"]) != account]
    totals = {a: t for a, t in cur["totals"].items() if mask(a) != account}
    write_snapshot(conn, user_id, {"snapshot_date": snap, "as_of_text": cur["as_of_text"]},
                   rows, totals, cur["source"] or "")
    return snap


def write_snapshot(conn, user_id: int, meta: dict, rows: list[dict], totals: dict,
                   src: str) -> None:
    """Save one snapshot for `user_id`, in one transaction: replace any
    snapshot already saved for meta["snapshot_date"] (whatever its source),
    then write its positions and account totals. `meta`, `rows` and `totals`
    are parse_csv()'s shapes; `src` names where it came from (the CSV's path,
    or manual_entry.SOURCE). Shared by import_csv() and hand entry
    (manual_entry.py). It saves exactly `rows`: the app's saves come through
    prepare_save(), which adds every account not in them as it was. Full account numbers in account names are cut to
    their last 3 digits first (accounts.mask_number), whatever the source."""
    from accounts import mask_number
    rows = [{**r, "account": mask_number(r.get("account"))} for r in rows]
    totals = {mask_number(a): t for a, t in totals.items()}
    snapshot_date = meta["snapshot_date"]
    with conn:
        if src != SAMPLE_SOURCE:
            clear_sample(conn, user_id)  # real holdings replace the example portfolio
        # Replace-by-date: drop any prior import of this date (whatever its file),
        # then the rows below re-establish it from `src`. Scoped to user_id so
        # this never touches another user's snapshot for the same date.
        conn.execute(
            "DELETE FROM snapshots WHERE snapshot_date = ? AND source_file <> ? AND user_id = ?",
            (snapshot_date, src, user_id))
        conn.execute(
            "INSERT INTO snapshots (snapshot_date, as_of_text, source_file, user_id, imported_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(snapshot_date, source_file, user_id) DO UPDATE SET "
            "as_of_text = excluded.as_of_text, imported_at = datetime('now')",
            (snapshot_date, meta["as_of_text"], src, user_id),
        )
        conn.execute("DELETE FROM positions WHERE snapshot_date = ? AND user_id = ?",
                     (snapshot_date, user_id))
        conn.execute("DELETE FROM account_totals WHERE snapshot_date = ? AND user_id = ?",
                     (snapshot_date, user_id))

        # imported_at is set explicitly here (not left to the column's own
        # DEFAULT) on all three INSERTs in this function - the multi-user
        # migration's table rebuild silently dropped that DEFAULT on the
        # live database (reproduced live: NOT NULL violation on every
        # import), so never depend on it existing.
        pos_cols = POSITION_COLS + ["user_id"]
        ph = ", ".join("?" for _ in pos_cols)
        conn.executemany(
            f"INSERT INTO positions ({', '.join(pos_cols)}, imported_at) VALUES ({ph}, datetime('now'))",
            [tuple((src if c == "source_file" else user_id if c == "user_id" else r.get(c))
                   for c in pos_cols) for r in rows],
        )
        conn.executemany(
            "INSERT INTO account_totals (snapshot_date, account, cash_value, reported_cost_basis, "
            "reported_market_value, reported_gain, reported_gain_pct, source_file, user_id, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            [(snapshot_date, acct, t["cash_value"], t["reported_cost_basis"],
              t["reported_market_value"], t["reported_gain"], t["reported_gain_pct"], src, user_id)
             for acct, t in totals.items()],
        )


@contextlib.contextmanager
def temp_upload(filename: str, data):
    """An uploaded file as a path, for the length of the `with` block only.
    Uploads are never kept: the bytes go to a private temporary folder that
    is removed afterwards, even if the block fails or the page reruns."""
    folder = tempfile.mkdtemp(prefix="pt_upload_")
    path = os.path.join(folder, os.path.basename(filename or "") or "upload.csv")
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        yield path
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def upload_label(filename: str) -> str:
    """What snapshots.source_file records for an uploaded file. Uploads are
    read from a temporary copy that is deleted afterwards, so the source is
    named, not a path on the server."""
    return f"upload: {os.path.basename(filename or '') or 'file.csv'}"


def import_csv(conn: sqlite3.Connection, csv_path: str, user_id: int,
                api_key: str | None = None, *, source_name: str | None = None) -> dict:
    """Read a positions export from any brokerage (parse_csv) and write it into
    an open connection, scoped to `user_id`.

    The command line (`cmd_import`) and tests call it; the dashboard uses the
    same reader through its column check and review. It upserts the `snapshots` row, then replaces `positions` and
    `account_totals` for the file's snapshot date. Re-importing *any* file for a
    date that is already loaded replaces that date wholesale for THIS user only
    (keyed on `snapshot_date` + `user_id`), so a re-downloaded export with a new
    filename still works, and two different users importing a CSV for the same
    calendar date never touch each other's rows. Returns a summary dict; it does
    not print or verify.

    `api_key` is accepted for older callers and ignored: columns are matched by
    name (csv_import.py); the AI is only used from the dashboard, when asked.

    `source_name` is what's recorded as the snapshot's source (default: the
    file's absolute path); an upload passes upload_label(), since its
    temporary copy is deleted right after.
    """
    path = os.path.abspath(csv_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    meta, rows, totals = parse_csv(path)
    snapshot_date = meta["snapshot_date"]
    src = source_name or path

    write_snapshot(conn, user_id, meta, rows, totals, src)

    from accounts import mask_number  # the names as saved
    saved = [mask_number(r["account"]) for r in rows]
    accounts = sorted(set(saved))
    return {
        "snapshot_date": snapshot_date,
        "source_file": src,
        "as_of_text": meta["as_of_text"],
        "n_positions": len(rows),
        "accounts": accounts,
        "per_account": {a: saved.count(a) for a in accounts},
        "ai_assisted": False,
    }


def cmd_import(args: argparse.Namespace) -> int:
    import auth
    conn = connect(args.db)
    user_id = auth.get_user_id(conn, args.user)
    if user_id is None:
        raise SystemExit(f"No such user '{args.user}' - create one first: "
                          f"python manage_users.py create {args.user}")
    try:
        info = import_csv(conn, args.csv, user_id)
    except FileNotFoundError as exc:
        raise SystemExit(f"File not found: {exc}")

    print(f"Imported {info['n_positions']} real positions across {len(info['accounts'])} account(s) "
          f"for snapshot {info['snapshot_date']}")
    for acct in info["accounts"]:
        print(f"  {acct}: {info['per_account'][acct]} positions")
    print(f"Database: {os.path.abspath(args.db)}")
    print("\nNote: transactions table created but left empty - the Positions export has no trade history.\n")

    ok = verify_snapshot(conn, info["snapshot_date"], user_id)
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# verify - parsed holdings vs the file's "Positions Total" rows
# --------------------------------------------------------------------------- #
def verify_snapshot(conn: sqlite3.Connection, snapshot: str, user_id: int | None = None) -> bool:
    """`user_id=None` checks across every account sharing this snapshot_date
    (fine for a single-tenant local DB; on a multi-user DB, pass the user's
    id or accounts sharing a calendar date get summed together)."""
    uid_clause = " AND user_id = ?" if user_id is not None else ""
    uid_params = (user_id,) if user_id is not None else ()
    accounts = [r["account"] for r in conn.execute(
        f"SELECT DISTINCT account FROM positions WHERE snapshot_date = ?{uid_clause} ORDER BY account",
        (snapshot, *uid_params))]
    if not accounts:
        raise SystemExit(f"No positions for snapshot {snapshot}.")

    print(f"VERIFICATION  -  snapshot {snapshot}")
    print("-" * 78)
    all_ok = True
    grand_n = 0

    for acct in accounts:
        holdings = conn.execute(
            f"SELECT cost_basis, market_value FROM positions WHERE snapshot_date = ? AND account = ?{uid_clause}",
            (snapshot, acct, *uid_params)).fetchall()
        t = conn.execute(
            f"SELECT * FROM account_totals WHERE snapshot_date = ? AND account = ?{uid_clause}",
            (snapshot, acct, *uid_params)).fetchone()

        n = len(holdings)
        grand_n += n
        sum_cost = sum(h["cost_basis"] for h in holdings if h["cost_basis"] is not None)
        sum_mv = sum(h["market_value"] for h in holdings if h["market_value"] is not None)
        sum_gain = sum((h["market_value"] - h["cost_basis"]) for h in holdings
                       if h["market_value"] is not None and h["cost_basis"] is not None)
        cash = (t["cash_value"] if t else None) or 0.0

        checks = [
            ("cost basis   sum(holdings)            vs Positions Total",
             sum_cost, t["reported_cost_basis"] if t else None),
            ("market value sum(holdings) + cash     vs Positions Total",
             sum_mv + cash, t["reported_market_value"] if t else None),
            ("unrealized   sum(mkt - cost)          vs Positions Total gain $",
             sum_gain, t["reported_gain"] if t else None),
        ]

        print(f"\n{acct}   ({n} positions, cash {money(cash)})")
        for label, got, expected in checks:
            if expected is None:
                status, detail = "SKIP", "no total in file"
            else:
                diff = got - expected
                ok = abs(diff) <= TOLERANCE
                all_ok &= ok
                status = "PASS" if ok else "FAIL"
                detail = f"parsed {money(got)}  file {money(expected)}  diff {money(diff)}"
            print(f"  [{status}] {label}")
            print(f"         {detail}")

    print("\n" + "-" * 78)
    print(f"{'ALL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED'}   -   "
          f"{grand_n} real positions across {len(accounts)} accounts")
    print("-" * 78)
    return all_ok


def cmd_verify(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.db):
        raise SystemExit(f"No database at {os.path.abspath(args.db)} - run `import` first.")
    import auth
    conn = connect(args.db)
    user_id = auth.get_user_id(conn, args.user) if getattr(args, "user", None) else None
    snapshot = args.snapshot or (conn.execute(
        "SELECT MAX(snapshot_date) AS d FROM positions").fetchone()["d"])
    if snapshot is None:
        raise SystemExit("No positions in the database yet.")
    return 0 if verify_snapshot(conn, snapshot, user_id) else 1


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def cmd_report(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.db):
        raise SystemExit(f"No database at {os.path.abspath(args.db)} - run `import` first.")

    conn = connect(args.db)
    snapshot = args.snapshot or (conn.execute(
        "SELECT MAX(snapshot_date) AS d FROM positions").fetchone()["d"])
    if snapshot is None:
        raise SystemExit("No positions in the database yet.")

    all_dates = [r["snapshot_date"] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM positions ORDER BY snapshot_date")]

    rows = conn.execute(
        "SELECT * FROM positions WHERE snapshot_date = ? ORDER BY account, symbol", (snapshot,)).fetchall()

    header = f" PORTFOLIO SUMMARY - snapshot {snapshot} "
    print("=" * len(header))
    print(header)
    print("=" * len(header))
    if len(all_dates) > 1:
        print(f"(database holds {len(all_dates)} snapshots: {', '.join(all_dates)}; use --snapshot to pick one)")
    print()

    grand = {"cost": 0.0, "mv": 0.0, "gl": 0.0, "cash": 0.0}
    accounts = []
    for r in rows:
        if r["account"] not in accounts:
            accounts.append(r["account"])

    for acct in accounts:
        acct_rows = [r for r in rows if r["account"] == acct]
        t = conn.execute("SELECT * FROM account_totals WHERE snapshot_date = ? AND account = ?",
                         (snapshot, acct)).fetchone()
        cash_total = (t["cash_value"] if t else None) or 0.0

        table, sub = [], {"cost": 0.0, "mv": 0.0, "gl": 0.0}
        for r in acct_rows:
            cost, mv = r["cost_basis"], r["market_value"]
            gl = (mv - cost) if (mv is not None and cost is not None) else None
            glp = (gl / cost * 100) if (gl is not None and cost) else None
            if cost is not None:
                sub["cost"] += cost
            if mv is not None:
                sub["mv"] += mv
            if gl is not None:
                sub["gl"] += gl
            qty = r["quantity"]
            qty_str = "" if qty is None else f"{qty:,.4f}".rstrip("0").rstrip(".")
            table.append([r["symbol"], (r["description"] or "")[:34], qty_str,
                          money(cost), money(mv), money(gl), pct(glp)])

        headers = ["Symbol", "Description", "Qty", "Cost Basis", "Market Value", "Unrealized G/L", "G/L %"]
        widths = [max(len(headers[i]), *(len(row[i]) for row in table)) if table else len(headers[i])
                  for i in range(len(headers))]
        rule = "-" * (sum(widths) + 3 * (len(widths) - 1))

        def fmt(cells):
            return "  ".join(c.ljust(widths[i]) if i < 2 else c.rjust(widths[i])
                             for i, c in enumerate(cells))

        print(f"Account: {acct}")
        print(rule)
        print(fmt(headers))
        print(rule)
        for row in table:
            print(fmt(row))
        print(rule)
        sub_glp = (sub["gl"] / sub["cost"] * 100) if sub["cost"] else None
        print(fmt(["", "SUBTOTAL (holdings)", "", money(sub["cost"]), money(sub["mv"]),
                   money(sub["gl"]), pct(sub_glp)]))
        print(f"  Cash & equivalents: {money(cash_total)}")
        print(f"  Account value (holdings + cash): {money(sub['mv'] + cash_total)}")
        print()

        grand["cost"] += sub["cost"]
        grand["mv"] += sub["mv"]
        grand["gl"] += sub["gl"]
        grand["cash"] += cash_total

    grand_glp = (grand["gl"] / grand["cost"] * 100) if grand["cost"] else None
    print("=" * len(header))
    print("ALL ACCOUNTS")
    print(f"  Total cost basis           {money(grand['cost'], 16)}")
    print(f"  Total market value         {money(grand['mv'], 16)}")
    print(f"  Total unrealized G/L       {money(grand['gl'], 16)}   ({pct(grand_glp)})")
    print(f"  Cash & equivalents         {money(grand['cash'], 16)}")
    print(f"  Portfolio value            {money(grand['mv'] + grand['cash'], 16)}")
    print("=" * len(header))

    live = conn.execute(
        "SELECT MAX(live_price_at) AS t, COUNT(live_price) AS n, "
        "       SUM(live_market_value) AS mv, SUM(live_unrealized_gain) AS gl, "
        "       SUM(CASE WHEN live_price IS NOT NULL THEN cost_basis END) AS cost "
        "FROM positions WHERE snapshot_date = ?", (snapshot,)).fetchone()
    if live and live["n"]:
        lglp = (live["gl"] / live["cost"] * 100) if live["cost"] else None
        print()
        print("=" * len(header))
        print(f"LIVE PRICES  ({live['n']} of {len(rows)} positions priced, as of {live['t']} UTC)")
        print(f"  Live market value          {money(live['mv'], 16)}")
        print(f"  Live unrealized G/L        {money(live['gl'], 16)}   ({pct(lglp)})")
        print(f"  vs CSV unrealized G/L      {money(grand['gl'], 16)}")
        print("=" * len(header))
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Personal portfolio tracker (phase 1).")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("import", help="parse a Schwab Positions CSV into SQLite")
    pi.add_argument("csv", help="path to the Positions export CSV")
    pi.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default: {DEFAULT_DB})")
    pi.add_argument("--user", required=True, help="account username to import this CSV into "
                                                    "(see manage_users.py)")
    pi.set_defaults(func=cmd_import)

    pv = sub.add_parser("verify", help="check parsed holdings against the file's Positions Total rows")
    pv.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default: {DEFAULT_DB})")
    pv.add_argument("--snapshot", help="snapshot date YYYY-MM-DD (default: latest)")
    pv.add_argument("--user", help="scope to one account's data (default: everyone sharing that date)")
    pv.set_defaults(func=cmd_verify)

    pr = sub.add_parser("report", help="print an unrealized gain/loss summary")
    pr.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default: {DEFAULT_DB})")
    pr.add_argument("--snapshot", help="snapshot date YYYY-MM-DD (default: latest)")
    pr.set_defaults(func=cmd_report)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
