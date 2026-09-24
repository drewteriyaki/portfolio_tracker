"""Tickers tracked for their chart/stats without being an owned position.

A thin CRUD layer over the `watchlist` table. Chart data for a watchlisted
ticker comes from the same place as everything else - `sync_history.py` -
the dashboard just needs to pass watchlist tickers into that sync alongside
the held ones (see `all_sync_tickers` below).
"""

from __future__ import annotations

import re
import sqlite3

# Tickers are 1-10 chars: letters/digits plus '.' or '-' for share classes
# and exchange suffixes (e.g. BRK.B, RDS-A). Not exhaustive - Yahoo will
# simply fail to find bad ones, which sync/chart code already handles.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def normalize(raw: str) -> str | None:
    """Upper-cased, whitespace-trimmed ticker, or None if it doesn't look
    like one at all."""
    t = (raw or "").strip().upper()
    return t if _TICKER_RE.match(t) else None


def list_tickers(conn: sqlite3.Connection, user_id: int) -> list[str]:
    return [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY ticker", (user_id,))]


def add(conn: sqlite3.Connection, user_id: int, raw_ticker: str) -> str | None:
    """Normalize and insert `raw_ticker`. Returns the normalized ticker on
    success, None if it didn't look like a valid ticker."""
    t = normalize(raw_ticker)
    if not t:
        return None
    conn.execute(
        "INSERT INTO watchlist (user_id, ticker) VALUES (?, ?) ON CONFLICT (user_id, ticker) DO NOTHING",
        (user_id, t))
    conn.commit()
    return t


def remove(conn: sqlite3.Connection, user_id: int, ticker: str) -> None:
    conn.execute("DELETE FROM watchlist WHERE user_id = ? AND ticker = ?", (user_id, ticker))
    conn.commit()


def all_sync_tickers(conn: sqlite3.Connection) -> list[str]:
    """Held position tickers + watchlist tickers, across EVERY user, deduped
    - what `sync_history.sync()` should pull history for. Deliberately
    global, not scoped to one user: market data (daily_bars/intraday_bars/
    security_info) is shared across every account, so one sync should cover
    everyone's tickers at once rather than re-fetching the same ticker's
    history once per user."""
    held = {r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM positions")}
    watched = {r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM watchlist")}
    return sorted(held | watched)
