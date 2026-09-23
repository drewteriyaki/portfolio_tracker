#!/usr/bin/env python3
"""Live price updater - Finnhub quotes.

  1. reads the distinct tickers from the positions table
  2. calls the Finnhub /quote endpoint for each
  3. appends every result to the price_history table (ticker, price, timestamp, ...)
  4. rewrites each position's unrealized gain/loss from the live price
     (live_market_value = live_price * quantity) instead of the CSV market value

Standard library only. The API key comes from .env (FINNHUB_API_KEY=), the
environment, or --key. Run:  python update_prices.py

`refresh_prices()` is the reusable entry point - the CLI and the Streamlit
dashboard both call it.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:  # Windows consoles default to cp1252; keep our own output ASCII regardless.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from portfolio import DEFAULT_DB, connect, money, pct  # noqa: E402  (local module)

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
ENV_PATH = os.path.join(HERE, ".env")


# --------------------------------------------------------------------------- #
# config / small helpers
# --------------------------------------------------------------------------- #
def load_env(path: str) -> dict:
    env = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def resolve_key(cli_key: str | None, env_path: str = ENV_PATH) -> str:
    if cli_key:
        return cli_key.strip()
    return (load_env(env_path).get("FINNHUB_API_KEY")
            or os.environ.get("FINNHUB_API_KEY")
            or "").strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_to_iso(epoch) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Finnhub
# --------------------------------------------------------------------------- #
def fetch_quote(symbol: str, token: str, timeout: float):
    """Return (data, error). Exactly one is truthy.

    Finnhub /quote payload: c=current, d=change, dp=%change, pc=prev close, t=epoch.
    An unknown symbol returns HTTP 200 with c=0, t=0.
    """
    url = f"{FINNHUB_QUOTE_URL}?" + urllib.parse.urlencode({"symbol": symbol, "token": token})
    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-tracker/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return json.loads(body), ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
        except Exception:
            pass
        return {}, f"HTTP {exc.code}" + (f": {detail}" if detail else "")
    except urllib.error.URLError as exc:
        return {}, f"network error: {exc.reason}"
    except TimeoutError:
        return {}, "timeout"
    except json.JSONDecodeError:
        return {}, "unparseable response"


# --------------------------------------------------------------------------- #
# core: fetch every ticker, log it, rewrite the live_* columns
# --------------------------------------------------------------------------- #
def _record_quote(conn: sqlite3.Connection, ticker: str, data: dict, error: str, ok: bool) -> None:
    conn.execute(
        "INSERT INTO price_history "
        "(ticker, price, prev_close, change, pct_change, day_open, day_high, day_low, "
        " quote_time, fetched_at, source, ok, error, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'finnhub', ?, ?, ?)",
        (ticker,
         data.get("c") if ok else None,
         data.get("pc") if data else None,
         data.get("d") if data else None,
         data.get("dp") if data else None,
         data.get("o") if data else None,
         data.get("h") if data else None,
         data.get("l") if data else None,
         epoch_to_iso(data.get("t")) if data else None,
         utc_now_iso(),
         1 if ok else 0,
         error or None,
         json.dumps(data) if data else None),
    )
    conn.commit()


def apply_live_prices(conn: sqlite3.Connection, snapshot: str,
                      fresh: dict, applied_at: str | None = None) -> int:
    """Write live_price / live_market_value / live_unrealized_gain[_pct] / live_price_at
    onto every position in `snapshot` whose ticker has a fresh price. Returns the count."""
    applied_at = applied_at or utc_now_iso()
    rows = conn.execute(
        "SELECT id, symbol, quantity, cost_basis FROM positions WHERE snapshot_date = ?",
        (snapshot,)).fetchall()
    updated = 0
    for r in rows:
        price = fresh.get(r["symbol"])
        if price is None or r["quantity"] is None or r["cost_basis"] is None:
            continue
        live_mv = round(price * r["quantity"], 2)
        live_gl = round(live_mv - r["cost_basis"], 2)
        live_glp = (live_gl / r["cost_basis"] * 100) if r["cost_basis"] else None
        conn.execute(
            "UPDATE positions SET live_price = ?, live_market_value = ?, live_unrealized_gain = ?, "
            "live_unrealized_gain_pct = ?, live_price_at = ? WHERE id = ?",
            (price, live_mv, live_gl, live_glp, applied_at, r["id"]),
        )
        updated += 1
    conn.commit()
    return updated


def refresh_prices(conn: sqlite3.Connection, snapshot: str, key: str, *,
                   delay: float = 0.25, timeout: float = 10.0, on_quote=None) -> dict:
    """Fetch a quote for every distinct ticker, append to price_history, and
    rewrite the live_* columns for `snapshot`.

    on_quote(i, total, ticker, ok, price, error) is called after each fetch.
    Returns {snapshot, tickers, ok, failed, results, updated, applied_at}.
    """
    tickers = [r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM positions ORDER BY symbol")]

    fresh: dict[str, float] = {}
    results = []  # (ticker, price|None, error|None, pct_change|None)
    for i, ticker in enumerate(tickers):
        data, error = fetch_quote(ticker, key, timeout)
        price = data.get("c") if data else None
        if data and not error and price in (None, 0):
            error = "no data for symbol (c=0)"
        ok = bool(data) and not error

        _record_quote(conn, ticker, data, error, ok)
        if ok:
            fresh[ticker] = price
        results.append((ticker, price if ok else None, None if ok else error,
                        data.get("dp") if ok else None))
        if on_quote:
            on_quote(i + 1, len(tickers), ticker, ok, price if ok else None, error or None)
        if delay and i < len(tickers) - 1:
            time.sleep(delay)

    applied_at = utc_now_iso()
    updated = apply_live_prices(conn, snapshot, fresh, applied_at) if fresh else 0
    return {
        "snapshot": snapshot,
        "tickers": len(tickers),
        "ok": len(fresh),
        "failed": len(tickers) - len(fresh),
        "results": results,
        "updated": updated,
        "applied_at": applied_at,
    }


def latest_snapshot(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(snapshot_date) AS d FROM positions").fetchone()
    return row["d"] if row else None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch live Finnhub prices and update unrealized G/L.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default: {DEFAULT_DB})")
    ap.add_argument("--env", default=ENV_PATH, help="path to .env (default: alongside this script)")
    ap.add_argument("--key", help="API key override (otherwise .env, then $FINNHUB_API_KEY)")
    ap.add_argument("--snapshot", help="snapshot date to update (default: latest)")
    ap.add_argument("--delay", type=float, default=0.25,
                    help="seconds between API calls; free tier allows 60/min (default 0.25)")
    ap.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        raise SystemExit(f"No database at {os.path.abspath(args.db)} - run `python portfolio.py import <csv>` first.")

    key = resolve_key(args.key, args.env)
    if not key:
        print("FINNHUB_API_KEY is not set - nothing was fetched.")
        print(f"  1. open {args.env}")
        print("  2. put your key right after 'FINNHUB_API_KEY='  (no quotes, no spaces)")
        print("  3. re-run:  python update_prices.py")
        print("  (or pass it directly:  python update_prices.py --key YOUR_KEY)")
        return 2

    conn = connect(args.db)
    snapshot = args.snapshot or latest_snapshot(conn)
    if not snapshot:
        raise SystemExit("No positions in the database yet.")

    print(f"Fetching Finnhub quotes (key ...{key[-4:]}, {args.delay}s apart):\n")

    def show(i, total, ticker, ok, price, error):
        if ok:
            print(f"  {ticker:<8} {money(price):>12}")
        else:
            print(f"  {ticker:<8} {'--':>12}   {error}")

    summary = refresh_prices(conn, snapshot, key, delay=args.delay,
                             timeout=args.timeout, on_quote=show)
    print(f"\n{summary['ok']} quote(s) stored in price_history, {summary['failed']} failed.")
    if summary["ok"] == 0:
        print("No live prices were obtained, so positions were left unchanged.")
        return 1

    # --- comparison table, read back from the DB --------------------------- #
    rows = conn.execute(
        "SELECT account, symbol, quantity, cost_basis, market_value, "
        "       live_price, live_market_value, live_unrealized_gain "
        "FROM positions WHERE snapshot_date = ? ORDER BY account, symbol", (snapshot,)).fetchall()

    tbl = []
    tot = {"cost": 0.0, "csv_mv": 0.0, "live_mv": 0.0, "csv_gl": 0.0, "live_gl": 0.0}
    for r in rows:
        csv_gl = (None if r["market_value"] is None or r["cost_basis"] is None
                  else r["market_value"] - r["cost_basis"])
        qty_str = "-" if r["quantity"] is None else f"{r['quantity']:,.4f}".rstrip("0").rstrip(".")
        if r["live_price"] is None:
            tbl.append([r["account"], r["symbol"], qty_str, "-",
                        money(r["market_value"]), "-", money(csv_gl), "-", "no live price"])
            continue
        tot["cost"] += r["cost_basis"] or 0.0
        tot["csv_mv"] += r["market_value"] or 0.0
        tot["live_mv"] += r["live_market_value"] or 0.0
        tot["csv_gl"] += csv_gl or 0.0
        tot["live_gl"] += r["live_unrealized_gain"] or 0.0
        tbl.append([r["account"], r["symbol"], qty_str, money(r["live_price"]),
                    money(r["market_value"]), money(r["live_market_value"]),
                    money(csv_gl), money(r["live_unrealized_gain"]),
                    f"{(r['live_unrealized_gain'] or 0.0) - (csv_gl or 0.0):+,.2f}"])

    headers = ["Account", "Ticker", "Qty", "Live Px", "CSV MktVal",
               "Live MktVal", "CSV G/L", "Live G/L", "G/L chg"]
    widths = [max(len(headers[i]), max(len(str(row[i])) for row in tbl)) for i in range(len(headers))]

    def fmt(cells):
        return "  ".join(str(c).ljust(widths[i]) if i < 2 else str(c).rjust(widths[i])
                         for i, c in enumerate(cells))

    rule = "-" * (sum(widths) + 2 * (len(widths) - 1))
    print("\n" + fmt(headers))
    print(rule)
    for row in tbl:
        print(fmt(row))
    print(rule)

    csv_glp = (tot["csv_gl"] / tot["cost"] * 100) if tot["cost"] else None
    live_glp = (tot["live_gl"] / tot["cost"] * 100) if tot["cost"] else None
    print(f"\nSnapshot {snapshot}: {summary['updated']} of {len(rows)} positions updated with live prices.")
    print(f"  Cost basis (priced holdings)   {money(tot['cost'], 16)}")
    print(f"  CSV  market value / G/L        {money(tot['csv_mv'], 16)} / {money(tot['csv_gl'])}  ({pct(csv_glp)})")
    print(f"  Live market value / G/L        {money(tot['live_mv'], 16)} / {money(tot['live_gl'])}  ({pct(live_glp)})")
    print(f"  Move since the CSV             {money(tot['live_gl'] - tot['csv_gl'], 16)}")
    print("\nSee it in the dashboard:  streamlit run dashboard.py")
    print("Or the text summary:      python portfolio.py report")

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
