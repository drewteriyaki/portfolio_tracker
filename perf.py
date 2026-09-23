"""Portfolio value over time.

`log_open()` appends one row of portfolio-level aggregates to `value_log` per app
session (throttled). `history()` returns those rows merged with a value computed
for every historical CSV snapshot, ready to chart.

The chartable series are exactly the columns we log - you can only plot what was
recorded - so `SERIES` doubles as the "pick a stat" menu.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from portfolio import connect

MIN_LOG_GAP_SEC = 300  # don't append another app_open row within this many seconds

# (column, label, format) - the y-axis options offered in the dashboard.
SERIES = [
    ("portfolio_value", "Portfolio value", "money"),
    ("holdings_value", "Holdings value", "money"),
    ("cash", "Cash", "money"),
    ("unrealized_gain", "Unrealized gain/loss $", "money"),
    ("unrealized_gain_pct", "Unrealized gain/loss %", "pct"),
    ("day_change_usd", "Day change $", "money"),
    ("cost_basis", "Cost basis", "money"),
    ("n_positions", "Position count", "int"),
]
SERIES_LABEL = {c: lbl for c, lbl, _ in SERIES}
SERIES_FMT = {c: f for c, _, f in SERIES}

# How each point on the chart was obtained.
SOURCE_LABEL = {
    "snapshot": "Broker statement (CSV)",
    "app_open": "Live check (dashboard open)",
    "reconstructed": "Reconstructed (holdings x daily close)",
}
SOURCE_COLOR = {
    "snapshot": "#f59e0b",
    "app_open": "#3b82f6",
    "reconstructed": "#22c55e",
}

# Per-ticker series from the sparse Finnhub refreshes (price_history).
TICKER_SERIES = [
    ("price", "Price", "price"),
    ("pct_change", "Day change %", "pct"),
    ("day_high", "Day high", "price"),
    ("day_low", "Day low", "price"),
    ("prev_close", "Prev close", "price"),
]
TICKER_SERIES_LABEL = {c: lbl for c, lbl, _ in TICKER_SERIES}
TICKER_SERIES_FMT = {c: f for c, _, f in TICKER_SERIES}

# Per-ticker series from the deep Yahoo history (daily_bars).
BAR_SERIES = [
    ("close", "Close", "price"),
    ("open", "Open", "price"),
    ("high", "High", "price"),
    ("low", "Low", "price"),
    ("adj_close", "Adj. close", "price"),
    ("volume", "Volume", "int"),
]
BAR_SERIES_LABEL = {c: lbl for c, lbl, _ in BAR_SERIES}
BAR_SERIES_FMT = {c: f for c, _, f in BAR_SERIES}
MA_WINDOWS = (20, 50, 200)

# Uniform OHLCV series offered by the per-ticker chart, at whatever resolution
# ticker_series() picks (daily_bars has adj_close too, but intraday_bars
# doesn't, so it's left off this shared list).
PRICE_SERIES = [
    ("close", "Close", "price"),
    ("open", "Open", "price"),
    ("high", "High", "price"),
    ("low", "Low", "price"),
    ("volume", "Volume", "int"),
]
PRICE_SERIES_LABEL = {c: lbl for c, lbl, _ in PRICE_SERIES}
PRICE_SERIES_FMT = {c: f for c, _, f in PRICE_SERIES}

# Intraday resolutions, finest first, with the days of look-back Yahoo serves
# for each (mirrors sync_history.INTRADAY_SPECS). "1d" (unlimited look-back) is
# always the final fallback and isn't listed here.
INTRADAY_INTERVALS = [
    ("1m", "1-minute", 7),
    ("5m", "5-minute", 60),
    ("15m", "15-minute", 60),
    ("60m", "1-hour", 730),
]
INTERVAL_LABEL = {c: lbl for c, lbl, _ in INTRADAY_INTERVALS}
INTERVAL_LABEL["1d"] = "daily"

_LOG_COLS = ["logged_at", "snapshot_date", "source", "portfolio_value", "holdings_value",
             "cash", "cost_basis", "unrealized_gain", "unrealized_gain_pct",
             "day_change_usd", "n_positions", "n_priced", "priced_at"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return datetime.now(timezone.utc)


def last_open(db_path: str) -> dict | None:
    """The most recent app_open row, or None if this is the first visit ever.
    Call this BEFORE log_open() logs the current session's row, or it'll just
    return what you're about to log."""
    conn = connect(db_path)
    try:
        # id DESC breaks ties between rows logged within the same second -
        # logged_at only has second resolution, so two opens close together
        # (as in a test with min_gap_sec=0) can otherwise tie and SQLite's
        # pick among tied rows isn't guaranteed to be the newest insert.
        row = conn.execute(
            "SELECT * FROM value_log WHERE source = 'app_open' "
            "ORDER BY logged_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def log_open(db_path: str, agg: dict, *, min_gap_sec: int = MIN_LOG_GAP_SEC) -> bool:
    """Append one app_open row of `agg`. Returns False (writes nothing) if the
    most recent app_open row is younger than `min_gap_sec`."""
    conn = connect(db_path)
    try:
        last = conn.execute(
            "SELECT logged_at FROM value_log WHERE source = 'app_open' "
            "ORDER BY logged_at DESC LIMIT 1"
        ).fetchone()
        if last and (datetime.now(timezone.utc) - _parse(last["logged_at"])).total_seconds() < min_gap_sec:
            return False
        row = {c: agg.get(c) for c in _LOG_COLS}
        row["logged_at"] = _utc_now_iso()
        row["source"] = "app_open"
        conn.execute(
            f"INSERT INTO value_log ({', '.join(_LOG_COLS)}) "
            f"VALUES ({', '.join(':' + c for c in _LOG_COLS)})", row)
        conn.commit()
        return True
    finally:
        conn.close()


def _snapshot_aggregates(conn, snapshot_date: str) -> dict:
    """Portfolio value etc. for a historical CSV snapshot, from its own figures."""
    p = conn.execute(
        "SELECT COALESCE(SUM(market_value), 0) mv, COALESCE(SUM(cost_basis), 0) cost, "
        "COUNT(*) n FROM positions WHERE snapshot_date = ?", (snapshot_date,)).fetchone()
    cash = conn.execute(
        "SELECT COALESCE(SUM(cash_value), 0) c FROM account_totals WHERE snapshot_date = ?",
        (snapshot_date,)).fetchone()["c"]
    mv, cost = p["mv"], p["cost"]
    gain = mv - cost
    return {
        "portfolio_value": round(mv + cash, 2),
        "holdings_value": round(mv, 2),
        "cash": round(cash, 2),
        "cost_basis": round(cost, 2),
        "unrealized_gain": round(gain, 2),
        "unrealized_gain_pct": round(gain / cost * 100, 4) if cost else None,
        "day_change_usd": None,
        "n_positions": p["n"],
    }


def history(db_path: str, *, days: int | None = None, reconstruct: bool = True,
            include_snapshots: bool = False, include_app_open: bool = True) -> list[dict]:
    """Merged time series for the performance chart: every `value_log` row
    (source 'app_open') and a reconstructed portfolio-value line (source
    'reconstructed': current holdings x that bar's close + cash, one point per
    bar). With `include_snapshots=True`, also one point per imported CSV
    snapshot valued from the CSV itself (source 'snapshot'). With
    `include_app_open=False`, omit the 'app_open' rows — the reconstructed
    line alone is the accurate, continuously-priced portfolio value; app_open
    rows only capture whatever prices happened to be last fetched when the
    dashboard was opened.

    `days` (None = all history) picks the reconstruction's resolution exactly
    like `ticker_series()` does: the finest interval covering `days` that
    Yahoo's intraday bars actually have data for, falling back to daily, and
    finally to the last 2 points overall so the chart is never empty. Sorted
    by time.
    """
    conn = connect(db_path)
    try:
        rows: list[dict] = []
        if include_snapshots:
            for s in conn.execute(
                    "SELECT DISTINCT snapshot_date FROM positions ORDER BY snapshot_date"):
                d = s["snapshot_date"]
                rows.append({"t": f"{d}T00:00:00Z", "source": "snapshot",
                             "source_label": SOURCE_LABEL["snapshot"], "snapshot_date": d,
                             "priced_at": None, "n_priced": None,
                             **_snapshot_aggregates(conn, d)})
        if include_app_open:
            for r in conn.execute("SELECT * FROM value_log ORDER BY logged_at"):
                rec = {"t": r["logged_at"], "source": r["source"],
                       "source_label": SOURCE_LABEL.get(r["source"], r["source"]),
                       "snapshot_date": r["snapshot_date"],
                       "priced_at": r["priced_at"], "n_priced": r["n_priced"]}
                for c, _, _ in SERIES:
                    rec[c] = r[c]
                rows.append(rec)
        if reconstruct:
            rows.extend(_reconstructed_daily_rows(conn) if days is None
                        else _reconstruct_best(conn, days))
        rows.sort(key=lambda x: x["t"])
        if days is None:
            return rows
        cutoff = _cutoff_ts(days)
        clipped = [r for r in rows if r["t"] >= cutoff]
        return clipped if len(clipped) >= 2 else (rows[-2:] if len(rows) >= 2 else rows)
    finally:
        conn.close()


def holdings_coverage(db_path: str):
    """(covered_tickers, missing_tickers) for the latest snapshot vs daily_bars."""
    conn = connect(db_path)
    try:
        return _coverage(conn)
    finally:
        conn.close()


def _coverage(conn):
    snap = conn.execute("SELECT MAX(snapshot_date) d FROM positions").fetchone()["d"]
    held = [r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM positions WHERE snapshot_date = ?", (snap,))]
    have = {r["ticker"] for r in conn.execute(
        "SELECT DISTINCT ticker FROM daily_bars WHERE ticker IN (%s)"
        % ",".join("?" * len(held)), held)} if held else set()
    return sorted(t for t in held if t in have), sorted(t for t in held if t not in have)


def _reconstruct_from(conn, sql: str, params) -> list[dict]:
    """Shared aggregation for the reconstructed line: `sql` must yield
    (t, ticker, close) rows. At every timestamp any held ticker has a bar,
    values the whole portfolio using each ticker's most recent known close as
    of that instant (forward-filled) - not just the tickers that happen to
    have a bar at that *exact* timestamp. Intraday bars across 20+ tickers are
    rarely all stamped in lockstep (a quiet minute for one ticker is a normal
    gap, not a missing trade), so requiring an exact match would swing the
    total based on which subset of holdings happened to report that instant,
    not on what the market actually did. A newly-listed holding joins the sum
    once its first bar arrives and never drops back out."""
    snap = conn.execute("SELECT MAX(snapshot_date) d FROM positions").fetchone()["d"]
    if not snap:
        return []
    holdings = {r["symbol"]: (r["quantity"] or 0.0, r["cost_basis"] or 0.0)
                for r in conn.execute(
                    "SELECT symbol, quantity, cost_basis FROM positions WHERE snapshot_date = ?",
                    (snap,))}
    if not holdings:
        return []
    cash = conn.execute(
        "SELECT COALESCE(SUM(cash_value), 0) c FROM account_totals WHERE snapshot_date = ?",
        (snap,)).fetchone()["c"]

    series: dict[str, list[tuple[str, float]]] = {tk: [] for tk in holdings}
    for t, ticker, close in conn.execute(sql, params):
        if close is not None and ticker in holdings:
            series[ticker].append((t, close))
    for rows in series.values():
        rows.sort()

    all_ts = sorted({t for rows in series.values() for t, _ in rows})
    if not all_ts:
        return []

    idx = {tk: -1 for tk in holdings}
    last_close: dict[str, float | None] = {tk: None for tk in holdings}
    out = []
    for t in all_ts:
        for tk, rows in series.items():
            i = idx[tk]
            while i + 1 < len(rows) and rows[i + 1][0] <= t:
                i += 1
                last_close[tk] = rows[i][1]
            idx[tk] = i

        hv = cost_sum = 0.0
        n = 0
        for tk, (qty, lot_cost) in holdings.items():
            c = last_close[tk]
            if c is not None:
                hv += c * qty
                cost_sum += lot_cost
                n += 1

        gain = hv - cost_sum
        out.append({
            "t": t, "source": "reconstructed",
            "source_label": SOURCE_LABEL["reconstructed"], "snapshot_date": snap,
            "priced_at": None, "n_priced": n,
            "portfolio_value": round(hv + cash, 2), "holdings_value": round(hv, 2),
            "cash": round(cash, 2), "cost_basis": round(cost_sum, 2),
            "unrealized_gain": round(gain, 2),
            "unrealized_gain_pct": round(gain / cost_sum * 100, 4) if cost_sum else None,
            "day_change_usd": None, "n_positions": n,
        })
    return out


def _reconstructed_daily_rows(conn) -> list[dict]:
    """One point per trading day, from daily_bars."""
    return _reconstruct_from(
        conn, "SELECT date, ticker, close FROM daily_bars WHERE close IS NOT NULL", ())


def _reconstructed_intraday_rows(conn, interval: str) -> list[dict]:
    """One point per bar at the given intraday resolution, from intraday_bars."""
    return _reconstruct_from(
        conn, "SELECT ts, ticker, close FROM intraday_bars WHERE interval = ? AND close IS NOT NULL",
        (interval,))


def _reconstruct_best(conn, days: int) -> list[dict]:
    """Reconstructed portfolio-value rows at the finest resolution whose
    Yahoo look-back covers `days` and that actually has >=2 points within the
    window; [] if nothing qualifies (caller falls back further)."""
    for interval in _intervals_for(days):
        rows = (_reconstructed_daily_rows(conn) if interval == "1d"
                else _reconstructed_intraday_rows(conn, interval))
        cutoff = f"{_cutoff_daily(days)}T00:00:00Z" if interval == "1d" else _cutoff_ts(days)
        clipped = [r for r in rows if r["t"] >= cutoff]
        if len(clipped) >= 2:
            return clipped
    return []


def ticker_history(db_path: str, ticker: str) -> list[dict]:
    """Every successful price_history row for one ticker (sparse Finnhub), oldest first."""
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT fetched_at, price, prev_close, pct_change, day_open, day_high, day_low "
            "FROM price_history WHERE ticker = ? AND ok = 1 ORDER BY fetched_at", (ticker,))]
    finally:
        conn.close()


def has_bars(db_path: str) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM daily_bars LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def _daily_with_ma(conn, ticker: str) -> list[dict]:
    bars = [dict(r) for r in conn.execute(
        "SELECT date, open, high, low, close, adj_close, volume "
        "FROM daily_bars WHERE ticker = ? ORDER BY date", (ticker,))]
    closes = [b["close"] for b in bars]
    for i, b in enumerate(bars):
        for w in MA_WINDOWS:
            window = [c for c in closes[max(0, i - w + 1): i + 1] if c is not None]
            b[f"ma_{w}"] = round(sum(window) / w, 4) if len(window) == w else None
    return bars


def ticker_bars(db_path: str, ticker: str) -> list[dict]:
    """Daily OHLCV bars for one ticker (deep Yahoo history), oldest first, with
    the moving-average columns pre-computed. `ticker_series()` (below) is what
    the dashboard actually charts - this stays for direct daily-only use."""
    conn = connect(db_path)
    try:
        return _daily_with_ma(conn, ticker)
    finally:
        conn.close()


def _intervals_for(days: int | None) -> list[str]:
    """Interval try-order (finest first) for a window spanning the last `days`
    (None = all history). Only intraday resolutions whose Yahoo look-back
    covers `days` are tried; daily is always the final fallback."""
    if days is None:
        return ["1d"]
    return [c for c, _, lookback in INTRADAY_INTERVALS if days <= lookback] + ["1d"]


def _cutoff_daily(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _cutoff_ts(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_intraday(conn, ticker: str, interval: str, days: int | None) -> list[dict]:
    sql = ("SELECT ts AS t, open, high, low, close, volume FROM intraday_bars "
           "WHERE ticker = ? AND interval = ?")
    params = [ticker, interval]
    if days is not None:
        sql += " AND ts >= ?"
        params.append(_cutoff_ts(days))
    return [dict(r) for r in conn.execute(sql + " ORDER BY ts", params)]


def ticker_series(db_path: str, ticker: str, days: int | None):
    """Best-resolution OHLCV rows for `ticker` covering the last `days` (None =
    all recorded history). Tries progressively finer intraday resolutions first
    (bounded by how far back Yahoo actually allows each - see
    INTRADAY_INTERVALS / sync_history.INTRADAY_SPECS), falls back to daily
    bars, and finally to just the last 2 daily points so a chart is never
    empty. Returns (rows, interval_used); each row has a "t" timestamp key.
    """
    conn = connect(db_path)
    try:
        daily = None

        def _daily_rows(cutoff):
            nonlocal daily
            if daily is None:
                daily = _daily_with_ma(conn, ticker)
            rows = daily if cutoff is None else [b for b in daily if b["date"] >= cutoff]
            return [{**b, "t": b["date"]} for b in rows]

        for interval in _intervals_for(days):
            if interval == "1d":
                rows = _daily_rows(None if days is None else _cutoff_daily(days))
            else:
                rows = _read_intraday(conn, ticker, interval, days)
            if len(rows) >= 2:
                return rows, interval

        rows = _daily_rows(None)
        return (rows[-2:] if len(rows) >= 2 else rows), "1d"
    finally:
        conn.close()


def has_intraday(db_path: str) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM intraday_bars LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def ticker_has_bars(db_path: str, ticker: str) -> bool:
    """Whether `ticker` has any Yahoo history at all (daily or intraday) -
    the dashboard's signal to use ticker_series() instead of the sparse
    Finnhub ticker_history()."""
    conn = connect(db_path)
    try:
        return (conn.execute("SELECT 1 FROM daily_bars WHERE ticker = ? LIMIT 1",
                             (ticker,)).fetchone() is not None
                or conn.execute("SELECT 1 FROM intraday_bars WHERE ticker = ? LIMIT 1",
                                (ticker,)).fetchone() is not None)
    finally:
        conn.close()


def bar_stats(db_path: str, tickers=None) -> dict:
    """{ticker: {last_close, volume, ma_20, ma_50, ma_200}} from daily_bars."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, date, close, volume FROM daily_bars ORDER BY ticker, date").fetchall()
    finally:
        conn.close()
    want = set(tickers) if tickers else None
    series: dict[str, list] = {}
    for r in rows:
        if want is not None and r["ticker"] not in want:
            continue
        series.setdefault(r["ticker"], []).append((r["close"], r["volume"]))

    stats = {}
    for tk, pairs in series.items():
        closes = [c for c, _ in pairs if c is not None]
        s = {"last_close": closes[-1] if closes else None,
             "volume": next((v for _, v in reversed(pairs) if v is not None), None)}
        for w in MA_WINDOWS:
            s[f"ma_{w}"] = round(sum(closes[-w:]) / w, 4) if len(closes) >= w else None
        stats[tk] = s
    return stats


def security_info(db_path: str) -> dict:
    """{ticker: row dict} from security_info."""
    conn = connect(db_path)
    try:
        return {r["ticker"]: dict(r) for r in conn.execute("SELECT * FROM security_info")}
    finally:
        conn.close()
