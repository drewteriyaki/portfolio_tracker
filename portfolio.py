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
import csv
import os
import re
import sqlite3
import sys
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

# Column order in the Schwab Positions export (0-based). Same for every account
# section's header row and for the "Positions Total" row.
COL = {
    "symbol": 0,
    "description": 1,
    "price_change_pct": 2,
    "day_change_pct": 3,
    "reported_gain": 4,
    "reported_gain_pct": 5,
    "cost_basis": 6,
    "reinvest": 7,
    "reinvest_cap_gains": 8,
    "div_pay_date": 9,
    "div_yield_pct": 10,
    "next_earnings_date": 11,
    "pct_of_account": 12,
    "market_value": 13,
    "quantity": 14,
    "asset_type": 15,
}

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


def parse_text(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    return None if s.lower() in NULLISH else s


def parse_yesno(raw):
    s = (raw or "").strip().lower()
    if s in ("yes", "y", "true"):
        return 1
    if s in ("no", "n", "false"):
        return 0
    return None


def extract_snapshot_date(header_line: str) -> str:
    """'Positions for All-Accounts as of 06:10 PM ET, 08/28/2026' -> '2026-08-28'."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", header_line)
    if not m:
        return date.today().isoformat()
    month, day, year = (int(x) for x in m.groups())
    return date(year, month, day).isoformat()


# --------------------------------------------------------------------------- #
# CSV parser - rebuilt for the real multi-account export
# --------------------------------------------------------------------------- #
def parse_csv(path: str):
    """Parse a Schwab Positions export.

    Returns (meta, positions, account_totals):
      meta           -> {"snapshot_date": "YYYY-MM-DD", "as_of_text": str|None}
      positions      -> list of holding dicts (real holdings only)
      account_totals -> {account: {"cash_value", "reported_cost_basis",
                                   "reported_market_value", "reported_gain",
                                   "reported_gain_pct"}}

    File shape (verified against All-Accounts-Positions-2026-08-28-181002.csv):

        "Positions for All-Accounts as of 06:10 PM ET, 08/28/2026"   <- title, has the date
        <blank>
        Individual ...641                                            <- account section header (skip, but remember)
        "Symbol","Description",...,"Asset Type",                     <- column header (skip)
        "ARM",...                                                    <- holding
        ... more holdings ...
        "Cash & Cash Investments","--",...                          <- cash row (skip, capture market value)
        "Positions Total","",...                                    <- totals row (skip, capture figures)
        <blank>
        Individual ...363                                            <- next account section
        ...
    """
    meta = {"snapshot_date": None, "as_of_text": None}
    positions: list[dict] = []
    account_totals: dict[str, dict] = {}
    current_account = None

    def totals_for(acct):
        return account_totals.setdefault(acct, {
            "cash_value": None,
            "reported_cost_basis": None,
            "reported_market_value": None,
            "reported_gain": None,
            "reported_gain_pct": None,
        })

    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if not row or all(cell.strip() == "" for cell in row):
                continue

            first = row[0].strip()
            first_lower = first.lower()
            rest_blank = all(cell.strip() == "" for cell in row[1:])

            # --- single-column lines: title or account section header ---------
            if len(row) == 1 or (rest_blank and first_lower not in ("positions total",)):
                if first_lower.startswith("positions for"):
                    meta["as_of_text"] = first
                    meta["snapshot_date"] = extract_snapshot_date(first)
                elif first:
                    current_account = first          # section header - skip the row, keep the name
                continue

            # --- column header row: skip -------------------------------------
            if first == "Symbol":
                continue

            def cell(name: str) -> str:
                idx = COL[name]
                return row[idx] if idx < len(row) else ""

            # --- "Positions Total" row: skip, capture figures ---------------
            if first_lower.startswith("positions total"):
                if current_account is not None:
                    t = totals_for(current_account)
                    t["reported_cost_basis"] = parse_num(cell("cost_basis"))
                    t["reported_market_value"] = parse_num(cell("market_value"))
                    t["reported_gain"] = parse_num(cell("reported_gain"))
                    t["reported_gain_pct"] = parse_num(cell("reported_gain_pct"))
                continue

            asset_type = parse_text(cell("asset_type"))

            # --- cash row: skip, capture its market value ------------------
            is_cash = first_lower.startswith("cash & cash") or \
                first_lower.startswith("cash and cash") or \
                (asset_type or "").lower().startswith("cash")
            if is_cash:
                if current_account is not None:
                    totals_for(current_account)["cash_value"] = parse_num(cell("market_value"))
                continue

            # --- real holding ---------------------------------------------
            positions.append({
                "snapshot_date": meta["snapshot_date"],
                "account": current_account,
                "symbol": first,
                "description": parse_text(cell("description")),
                "asset_type": asset_type,
                "quantity": parse_num(cell("quantity")),          # fractional-safe
                "cost_basis": parse_num(cell("cost_basis")),
                "market_value": parse_num(cell("market_value")),
                "price_change_pct": parse_num(cell("price_change_pct")),
                "day_change_pct": parse_num(cell("day_change_pct")),
                "reported_gain": parse_num(cell("reported_gain")),
                "reported_gain_pct": parse_num(cell("reported_gain_pct")),
                "reinvest": parse_yesno(cell("reinvest")),
                "reinvest_cap_gains": parse_yesno(cell("reinvest_cap_gains")),
                "div_pay_date": parse_text(cell("div_pay_date")),
                "div_yield_pct": parse_num(cell("div_yield_pct")),
                "next_earnings_date": parse_text(cell("next_earnings_date")),
                "pct_of_account": parse_num(cell("pct_of_account")),
            })

    if meta["snapshot_date"] is None:
        raise SystemExit("Could not find the 'Positions for ...' header line; is this a Schwab Positions export?")
    if not positions:
        raise SystemExit("No holding rows found in the file.")
    return meta, positions, account_totals


def parse_csv_smart(path: str, api_key: str | None = None, info: dict | None = None):
    """Like parse_csv(), but on failure - and only if `api_key` is given -
    retries via ai_parse's AI-assisted column-mapping fallback (see
    ai_parse.py's module docstring for why that's scoped to just the
    header row, not full-row AI parsing - cost and privacy, not just
    simplicity). Any failure in the fallback re-raises the ORIGINAL
    strict-parser error rather than a confusing error about the fallback
    itself, so a genuinely unparseable file still gets today's message.
    `ai_parse` is imported lazily so local/no-key use never even imports
    its urllib-based client.

    Returns the exact same (meta, positions, account_totals) shape
    parse_csv() does either way, so a caller that doesn't care which path
    ran needs no changes. A caller that DOES want to know (e.g. to show
    "Claude helped interpret this file" in the UI) passes a dict via
    `info` - it gets `info["ai_assisted"] = True/False` set as a side
    effect, chosen over widening the return tuple so this stays a drop-in
    replacement for parse_csv() everywhere else."""
    try:
        result = parse_csv(path)
        if info is not None:
            info["ai_assisted"] = False
        return result
    except SystemExit as strict_error:
        if not api_key:
            raise
        import ai_parse
        header_row = ai_parse.guess_header_row(path)
        if header_row is None:
            raise
        mapping, error = ai_parse.map_columns(header_row, api_key)
        if mapping is None:
            raise
        try:
            result = ai_parse.parse_with_mapping(path, mapping, header_row)
        except SystemExit:
            raise strict_error from None
        if info is not None:
            info["ai_assisted"] = True
        return result


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
]

# Multi-user data isolation, added after the app already had real data in
# these 5 tables (unlike watchlist, which was empty everywhere and could
# just get user_id baked into its CREATE TABLE directly - see schema.sql).
USER_ID_COL = [("user_id", "INTEGER")]


# Schema creation + column back-fill is idempotent but not free; once a given
# database file has been set up in this process, later connect() calls skip it.
_SCHEMA_READY: set[str] = set()


def _ensure_schema(conn) -> None:
    is_pg = isinstance(conn, pgcompat.ConnWrapper)
    schema_path = SCHEMA_PG_PATH if is_pg else SCHEMA_PATH
    with open(schema_path, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    for table, cols in (("positions", LIVE_POSITION_COLS),
                        ("price_history", PRICE_HISTORY_EXTRA_COLS),
                        ("transactions", TRANSACTIONS_EXTRA_COLS),
                        ("snapshots", USER_ID_COL),
                        ("positions", USER_ID_COL),
                        ("account_totals", USER_ID_COL),
                        ("transactions", USER_ID_COL),
                        ("value_log", USER_ID_COL),
                        ("users", [("is_advisor", "INTEGER")])):
        if is_pg:
            have = {r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,))}
        else:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


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
        _ensure_schema(conn)
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


def import_csv(conn: sqlite3.Connection, csv_path: str, user_id: int,
                api_key: str | None = None) -> dict:
    """Parse a Schwab Positions export and write it into an open connection,
    scoped to `user_id`.

    This is the shared entry point: `cmd_import` (CLI) and the Streamlit dashboard
    both call it. It upserts the `snapshots` row, then replaces `positions` and
    `account_totals` for the file's snapshot date. Re-importing *any* file for a
    date that is already loaded replaces that date wholesale for THIS user only
    (keyed on `snapshot_date` + `user_id`), so a re-downloaded export with a new
    filename still works, and two different users importing a CSV for the same
    calendar date never touch each other's rows. Returns a summary dict; it does
    not print or verify.

    `api_key` (an Anthropic key) is optional and enables parse_csv_smart()'s
    AI-assisted fallback for a file whose headers don't match the strict
    Schwab shape - see ai_parse.py. None (the default) means exactly
    today's behavior: strict parsing only.
    """
    src = os.path.abspath(csv_path)
    if not os.path.isfile(src):
        raise FileNotFoundError(src)

    parse_info: dict = {}
    meta, rows, totals = parse_csv_smart(src, api_key, parse_info)
    snapshot_date = meta["snapshot_date"]

    with conn:
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

    accounts = sorted({r["account"] for r in rows})
    return {
        "snapshot_date": snapshot_date,
        "source_file": src,
        "as_of_text": meta["as_of_text"],
        "n_positions": len(rows),
        "accounts": accounts,
        "per_account": {a: sum(1 for r in rows if r["account"] == a) for a in accounts},
        "ai_assisted": parse_info.get("ai_assisted", False),
    }


def cmd_import(args: argparse.Namespace) -> int:
    import auth
    conn = connect(args.db)
    user_id = auth.get_user_id(conn, args.user)
    if user_id is None:
        raise SystemExit(f"No such user '{args.user}' - create one first: "
                          f"python manage_users.py create {args.user}")
    # Same optional AI-assisted-parsing fallback the dashboard offers (see
    # ai_parse.py) - unset (the common case for local CLI use) means
    # exactly today's strict-parser-only behavior.
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    try:
        info = import_csv(conn, args.csv, user_id, api_key)
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
