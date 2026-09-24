"""Portfolio Tracker - single-page Streamlit dashboard.

Run it:  streamlit run dashboard.py   (or double-click dashboard.cmd)
"""

import json
import os
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st

import alerts
import auth
import charts
import metrics as M
import news
import perf
import pgcompat
import watchlist
from allocation import CONCENTRATION_PCT, allocate
from portfolio import DBError, connect, import_csv, parse_csv
from update_prices import ENV_PATH, latest_snapshot, refresh_prices, resolve_key
from changes import diff_positions, synthesize_transactions

HERE = os.path.dirname(os.path.abspath(__file__))
# Defaults to ./portfolio.db; set PORTFOLIO_DB to point at another file (handy for
# trying the importer against a throwaway copy).
DB = os.environ.get("PORTFOLIO_DB") or os.path.join(HERE, "portfolio.db")

GREEN = "#16a34a"
RED = "#dc2626"

st.set_page_config(page_title="Portfolio Tracker", layout="wide")


def _login() -> bool:
    """Per-account login - every account is admin-provisioned (see
    manage_users.py); there is no signup anywhere in this app. Sets
    st.session_state["user_id"]/["username"] on success, same pattern the
    old single shared-password gate used for "authed". Generic error
    message on any failure (unknown username OR wrong password) so the
    login screen never reveals which username exists."""
    if st.session_state.get("user_id"):
        return True
    st.title("Portfolio Tracker")
    user = st.text_input("Username", key="login_user")
    pw = st.text_input("Password", type="password", key="login_pw")
    if st.button("Log in") or pw:
        conn = connect(DB)
        try:
            user_id = auth.verify_login(conn, user, pw) if user and pw else None
        finally:
            conn.close()
        if user_id is not None:
            st.session_state["user_id"] = user_id
            st.session_state["username"] = user
            st.rerun()
        elif pw:
            st.error("Invalid username or password.")
    return False


if not _login():
    st.stop()

USER_ID = st.session_state["user_id"]
PREFS_PATH = os.path.join(HERE, f".dashboard_prefs.{USER_ID}.json")

MASK = "•••"


def _hidden() -> bool:
    return bool(st.session_state.get("hide_amounts", False))


def mask_or(s):
    """MASK when 'hide amounts' is on, else `s` unchanged."""
    return MASK if _hidden() else s


def _blank(v):
    return v is None or (isinstance(v, float) and pd.isna(v))


# --------------------------------------------------------------------------- #
def fmt_money(v):
    if _hidden():
        return MASK
    if _blank(v):
        return "—"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def fmt_pct(v):
    if _hidden():
        return MASK
    return "—" if _blank(v) else f"{v:+.2f}%"


def color_sign(v):
    if _hidden() or v is None or pd.isna(v) or v == 0:
        return ""
    return f"color: {GREEN}; font-weight: 600" if v > 0 else f"color: {RED}; font-weight: 600"


def fmt_price(v):
    return MASK if _hidden() else ("—" if _blank(v) else f"${v:,.2f}")


def fmt_qty(v):
    return "—" if _blank(v) else f"{v:,.4f}".rstrip("0").rstrip(".")


def fmt_num(v):
    return MASK if _hidden() else ("—" if _blank(v) else f"{v:,.2f}")


def fmt_int(v):
    return MASK if _hidden() else ("—" if _blank(v) else f"{v:,.0f}")


FORMATTERS = {"money": fmt_money, "pct": fmt_pct, "price": fmt_price,
              "qty": fmt_qty, "num": fmt_num, "int": fmt_int}

# axis / tooltip number format string per metric-format name
AXIS_FORMAT = {"money": "$,.2s", "price": "$,.2f", "pct": ".2f", "int": "d", "num": ",.2f"}
TOOLTIP_FORMAT = {"money": "$,.2f", "price": "$,.2f", "pct": ".2f", "int": "d", "num": ",.2f"}


def _stat_tiles(ctx, keys, ncols=4):
    """A compact label/value grid for a list of metrics.py keys — the
    Robinhood-style "stats" block under a ticker's chart. Skips keys with no
    registered metric; renders '—' for a None value like the Holdings table."""
    keys = [k for k in keys if k in M.BY_KEY]
    if not keys:
        return
    cols = st.columns(ncols)
    for i, k in enumerate(keys):
        m = M.BY_KEY[k]
        v = M.value(k, ctx)
        text = FORMATTERS[m.fmt](v) if m.fmt in FORMATTERS else ("—" if _blank(v) else str(v))
        with cols[i % ncols]:
            st.caption(m.label)
            if m.color_sign and not _hidden() and not _blank(v) and v != 0:
                st.markdown(f"<span style='font-weight:600;color:{GREEN if v > 0 else RED}'>"
                            f"{text}</span>", unsafe_allow_html=True)
            else:
                st.markdown(f"**{text}**")


def _read_prefs():
    try:
        with open(PREFS_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_prefs(d):
    try:
        with open(PREFS_PATH, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
    except OSError:
        pass


def load_columns():
    saved = _read_prefs().get("columns")
    keys = [k for k in (saved or M.DEFAULT_KEYS) if k in M.BY_KEY and M.BY_KEY[k].available]
    return keys or list(M.DEFAULT_KEYS)


def save_columns(keys):
    p = _read_prefs()
    p["columns"] = list(keys)
    _write_prefs(p)


def load_rules():
    saved = _read_prefs().get("rules") or {}
    return [{**r, "abs_gt": float(saved.get(r["key"], r["abs_gt"]))} for r in alerts.DEFAULT_RULES]


def save_rules(rules):
    p = _read_prefs()
    p["rules"] = {r["key"]: r["abs_gt"] for r in rules}
    _write_prefs(p)


def load_perf_series():
    s = _read_prefs().get("perf_series")
    return s if s in perf.SERIES_LABEL else perf.SERIES[0][0]


DEFAULT_DRIFT_THRESHOLD = 5.0  # percentage points off target before flagging


def load_alloc_targets():
    """{asset-type label: target %}. Only labels the user has explicitly set
    a nonzero target for are included — an unset label has no target and is
    never flagged, rather than implicitly meaning "target 0%"."""
    saved = _read_prefs().get("alloc_targets") or {}
    return {k: float(v) for k, v in saved.items() if v}


def save_alloc_targets(targets: dict):
    p = _read_prefs()
    p["alloc_targets"] = {k: v for k, v in targets.items() if v}
    _write_prefs(p)


def load_drift_threshold():
    v = _read_prefs().get("drift_threshold")
    return float(v) if v else DEFAULT_DRIFT_THRESHOLD


def save_drift_threshold(v):
    p = _read_prefs()
    p["drift_threshold"] = float(v)
    _write_prefs(p)


def save_hide(on):
    p = _read_prefs()
    p["hide_amounts"] = bool(on)
    _write_prefs(p)


def save_perf_series(col):
    p = _read_prefs()
    p["perf_series"] = col
    _write_prefs(p)


def load():
    """Return (snapshot_date, positions, cash_by_account, quotes)."""
    conn = connect(DB)
    try:
        snap = latest_snapshot(conn, USER_ID)
        if not snap:
            return None, [], {}, {}
        rows = conn.execute(
            "SELECT * FROM positions WHERE snapshot_date = ? AND user_id = ? ORDER BY account, symbol",
            (snap, USER_ID)
        ).fetchall()
        cash_by_account = {
            r["account"]: r["cash_value"] or 0.0
            for r in conn.execute(
                "SELECT account, cash_value FROM account_totals WHERE snapshot_date = ? AND user_id = ?",
                (snap, USER_ID))
        }
        quotes = {
            r["ticker"]: dict(r)
            for r in conn.execute(
                "SELECT ph.* FROM price_history ph JOIN ("
                "  SELECT ticker, MAX(fetched_at) AS m FROM price_history WHERE ok = 1 GROUP BY ticker"
                ") latest ON ph.ticker = latest.ticker AND ph.fetched_at = latest.m WHERE ph.ok = 1")
        }
    finally:
        conn.close()
    return snap, [dict(r) for r in rows], cash_by_account, quotes


# --------------------------------------------------------------------------- #
if not pgcompat.is_postgres_dsn(DB) and not os.path.isfile(DB):
    st.error("No `portfolio.db` yet. Build it first:")
    st.code("python portfolio.py import \"path\\to\\All-Accounts-Positions-....csv\"", language="bash")
    st.stop()

if "hide_amounts" not in st.session_state:
    st.session_state["hide_amounts"] = bool(_read_prefs().get("hide_amounts", False))

# surface the result of a refresh that happened just before the rerun
_msg = st.session_state.pop("refresh_msg", None)
if _msg:
    getattr(st, _msg[0])(_msg[1])

_flash = st.session_state.pop("import_flash", None)
if _flash:
    st.success(_flash)

snapshot, positions, cash_by_account, quotes = load()
if not positions:
    # Blank-account onboarding: a brand-new admin-provisioned account has no
    # data at all yet. Skip straight to a CSV upload prompt instead of the
    # rest of the page (which would otherwise render a wall of "no data"
    # empty states across every section) - reuses the same import_csv()
    # entry point as the full "Import a new positions CSV" expander further
    # down, just without that flow's diff-preview step (there's nothing to
    # diff a first import against).
    st.title("Portfolio Tracker")
    st.info(f"Welcome, **{st.session_state['username']}** — your account has no data yet. "
            "Upload a Schwab Positions export CSV to get started.")
    up = st.file_uploader("Positions export (.csv)", type=["csv"], key="onboard_csv_upload")
    if up is not None:
        imports_dir = os.path.join(HERE, "imports", str(USER_ID))
        os.makedirs(imports_dir, exist_ok=True)
        src_path = os.path.join(imports_dir, up.name)
        with open(src_path, "wb") as fh:
            fh.write(up.getbuffer())
        _conn = connect(DB)
        try:
            info = import_csv(_conn, src_path, USER_ID)
        except DBError as exc:
            st.error(f"Import failed: {exc}")
        else:
            st.success(f"Imported snapshot {info['snapshot_date']} — {info['n_positions']} positions.")
            st.rerun()
        finally:
            _conn.close()
    st.stop()

cash = sum(cash_by_account.values())

# Deep Yahoo history (moving averages, volume, 52-wk, beta, P/E, sector).
bar_stats = perf.bar_stats(DB)
sec_info = perf.security_info(DB)

_wl_conn = connect(DB)
try:
    watch_tickers = watchlist.list_tickers(_wl_conn, USER_ID)
finally:
    _wl_conn.close()
_held_symbols = {p["symbol"] for p in positions}
watch_only = [t for t in watch_tickers if t not in _held_symbols]

# One metric context per position (same order as `positions`). Reused everywhere
# below: totals, alerts, the holdings table. port_value / acct_value are filled
# in once the totals are known.
contexts = [{"pos": p, "quote": quotes.get(p["symbol"], {}),
             "stats": bar_stats.get(p["symbol"], {}), "info": sec_info.get(p["symbol"], {}),
             "port_value": None, "acct_value": None} for p in positions]

tot_mv = tot_gl = tot_cost = 0.0
acct_value = {}
for p, ctx in zip(positions, contexts):
    mv, cost = M.eff_mv(ctx), p["cost_basis"]
    if mv is not None:
        tot_mv += mv
        acct_value[p["account"]] = acct_value.get(p["account"], 0.0) + mv
        if cost is not None:
            tot_gl += mv - cost
            tot_cost += cost

for acct, csh in cash_by_account.items():
    acct_value[acct] = acct_value.get(acct, 0.0) + (csh or 0.0)

portfolio_value = tot_mv + cash
tot_glp = (tot_gl / tot_cost * 100) if tot_cost else None
for p, ctx in zip(positions, contexts):
    ctx["port_value"] = portfolio_value
    ctx["acct_value"] = acct_value.get(p["account"])

n_live = sum(1 for p in positions if p["live_price"] is not None)
last_live = max((p["live_price_at"] for p in positions if p["live_price_at"]), default=None)
day_change_total = sum(
    v for v in (M.value("day_change_usd", ctx) for ctx in contexts) if v is not None
)

# ---- log this session's portfolio value (once, throttled) ---------- #
# last_open() must run BEFORE log_open() writes this session's own row, or
# "since you last opened" would just be comparing the portfolio to itself.
if "last_open_snapshot" not in st.session_state:
    st.session_state["last_open_snapshot"] = perf.last_open(DB, USER_ID)
if "value_logged" not in st.session_state:
    st.session_state["value_logged"] = perf.log_open(DB, USER_ID, {
        "snapshot_date": snapshot,
        "portfolio_value": portfolio_value,
        "holdings_value": tot_mv,
        "cash": cash,
        "cost_basis": tot_cost,
        "unrealized_gain": tot_gl,
        "unrealized_gain_pct": tot_glp,
        "day_change_usd": day_change_total,
        "n_positions": len(positions),
        "n_priced": n_live,
        "priced_at": last_live,
    })

# ---- header -------------------------------------------------------------- #
left, right = st.columns([0.7, 0.3])
with left:
    st.title("Portfolio Tracker")
    st.caption(
        f"CSV snapshot **{snapshot}**  ·  "
        + (f"live prices as of **{last_live} UTC** ({n_live}/{len(positions)} priced)"
           if last_live else "**no live prices yet — click Refresh**")
    )
    st.toggle("Hide amounts", key="hide_amounts",
              help="Mask every dollar / percent on the page with " + MASK)
hide_amounts = st.session_state["hide_amounts"]
if hide_amounts != _read_prefs().get("hide_amounts", False):
    save_hide(hide_amounts)
with right:
    st.write("")
    if st.button("Refresh prices", type="primary", use_container_width=True):
        key = resolve_key(None, ENV_PATH)
        if not key:
            st.session_state["refresh_msg"] = ("error", "No FINNHUB_API_KEY in .env — add it and retry.")
            st.rerun()
        bar = st.progress(0.0, text="Contacting Finnhub…")
        conn = connect(DB)
        try:
            summary = refresh_prices(
                conn, latest_snapshot(conn, USER_ID), USER_ID, key, delay=0.0,
                on_quote=lambda i, n, tk, ok, px, err: bar.progress(i / n, text=f"{tk} ({i}/{n})"),
            )
        finally:
            conn.close()
        bar.empty()
        if summary["failed"]:
            bad = ", ".join(tk for tk, _, err, _ in summary["results"] if err)
            st.session_state["refresh_msg"] = (
                "warning",
                f"Updated {summary['updated']} positions · {summary['ok']}/{summary['tickers']} quotes OK · "
                f"no data for: {bad}",
            )
        else:
            st.session_state["refresh_msg"] = (
                "success", f"Updated {summary['updated']} positions from {summary['ok']} live quotes.")
        st.rerun()

    if st.button("Sync history (Yahoo)", use_container_width=True,
                 help="Pull the maximum history Yahoo allows at every resolution it offers - "
                      "~2 years daily, plus 1-minute (~7d), 5- and 15-minute (~60d), and "
                      "hourly (~2y) bars - plus fundamentals. Takes a minute or two. Powers "
                      "the 1D-1Y charts, moving averages, volume, 52-wk figures, and the "
                      "reconstructed line. Tip: run setup-scheduled-tasks.ps1 once to have "
                      "this (and price refresh) happen on their own — see the README."):
        try:
            import sync_history
        except ImportError:
            st.session_state["refresh_msg"] = ("error", "yfinance not installed — run: pip install yfinance")
            st.rerun()
        prog = st.progress(0.0, text="Contacting Yahoo…")
        summary = sync_history.sync(
            DB, period=sync_history.DEFAULT_PERIOD,
            on_progress=lambda i, n, tk, nr, ok, err: prog.progress(
                i / n, text=f"{tk} ({i}/{n}) — {nr:,} rows"),
        )
        prog.empty()
        msg = (f"Synced {summary['bars_written']:,} daily + "
               f"{summary['intraday_written']:,} intraday bars for "
               f"{summary['ok']}/{summary['tickers']} tickers.")
        if summary["failed"]:
            st.session_state["refresh_msg"] = ("warning", msg + " No data for: " + ", ".join(summary["failed"]))
        else:
            st.session_state["refresh_msg"] = ("success", msg)
        st.rerun()

# ---- "since you last opened" ---------------------------------------- #
_last_open = st.session_state["last_open_snapshot"]
if _last_open and _last_open.get("portfolio_value"):
    _prev_val = _last_open["portfolio_value"]
    _since_delta = portfolio_value - _prev_val
    _since_pct = (_since_delta / _prev_val * 100) if _prev_val else None
    _last_ts = pd.to_datetime(_last_open["logged_at"], utc=True).to_pydatetime()
    _secs = (datetime.now(timezone.utc) - _last_ts).total_seconds()
    if _secs < 3600:
        _ago = f"{max(1, int(_secs // 60))} min ago"
    elif _secs < 86400:
        _ago = f"{int(_secs // 3600)}h ago"
    else:
        _ago = f"{int(_secs // 86400)}d ago"
    _arrow = "↑" if _since_delta >= 0 else "↓"
    st.info(
        f"**Since you last opened** ({_ago}): {_arrow} "
        + (MASK if hide_amounts else f"{fmt_money(abs(_since_delta))} "
           f"({_since_pct:+.2f}%)" if _since_pct is not None else fmt_money(abs(_since_delta)))
    )

# ---- alerts (recomputed on every page load) ------------------------ #
_rules = load_rules()
_fired = alerts.evaluate(contexts, _rules)

al, ar = st.columns([0.8, 0.2])
al.subheader(f"Alerts ({len(_fired)})")
with ar.popover("Rules", use_container_width=True):
    _new = []
    for _r in alerts.DEFAULT_RULES:
        cur = next((x["abs_gt"] for x in _rules if x["key"] == _r["key"]), _r["abs_gt"])
        val = st.number_input(f"{_r['label']} — flag beyond ±%", min_value=0.0, max_value=1000.0,
                              value=float(cur), step=0.5, key=f"rule_{_r['key']}")
        _new.append({**_r, "abs_gt": val})
    if _new != _rules:
        save_rules(_new)
        st.rerun()
    st.caption("Thresholds recompute live against the latest prices — no scheduler.")

if _fired:
    with st.container(border=True):
        for _a in _fired:
            st.markdown(_a.masked_message if hide_amounts else _a.message)
else:
    st.caption("No position is past its day-move or gain/loss limit.")

# ---- import a new positions CSV -------------------------------------- #
with st.expander("Import a new positions CSV", expanded=False):
    st.caption(
        "Upload a fresh Schwab **Positions** export (or point at one already on "
        "this machine). You'll see exactly what changed before anything is saved."
    )
    up = st.file_uploader("Positions export (.csv)", type=["csv"], key="csv_upload")
    path_in = st.text_input(
        "…or a path to a CSV on this machine",
        key="csv_path",
        placeholder="C:\\Users\\you\\Downloads\\All-Accounts-Positions-....csv",
    ).strip().strip('"')

    src_path = None
    if up is not None:
        imports_dir = os.path.join(HERE, "imports", str(USER_ID))
        os.makedirs(imports_dir, exist_ok=True)
        src_path = os.path.join(imports_dir, up.name)
        with open(src_path, "wb") as fh:
            fh.write(up.getbuffer())
    elif path_in:
        src_path = path_in

    if src_path and not os.path.isfile(src_path):
        st.error(f"No file at: {src_path}")
    elif src_path:
        try:
            _meta, new_rows, _ = parse_csv(src_path)
        except SystemExit as exc:
            st.error(f"Couldn't parse this file: {exc}")
        else:
            file_date = _meta["snapshot_date"]
            _conn = connect(DB)
            try:
                # Diff against the snapshot *before* this file's date, so the change
                # set (and the transactions derived from it) is the same however many
                # times this file is imported.
                base_date = _conn.execute(
                    "SELECT MAX(snapshot_date) d FROM positions WHERE snapshot_date < ? AND user_id = ?",
                    (file_date, USER_ID),
                ).fetchone()["d"]
                base_rows = [dict(r) for r in _conn.execute(
                    "SELECT account, symbol, description, quantity, cost_basis, market_value "
                    "FROM positions WHERE snapshot_date = ? AND user_id = ?",
                    (base_date, USER_ID))] if base_date else []
                replacing = _conn.execute(
                    "SELECT 1 FROM positions WHERE snapshot_date = ? AND user_id = ? LIMIT 1",
                    (file_date, USER_ID)
                ).fetchone() is not None

                d = diff_positions(base_rows, new_rows)
                n_changed = len(d["increased"]) + len(d["decreased"])

                st.markdown(
                    f"Changes vs snapshot **{base_date or '— none (first import)'}**"
                )
                c = st.columns(5)
                c[0].metric("New", len(d["new"]))
                c[1].metric("Qty changed", n_changed)
                c[2].metric("Closed", len(d["closed"]))
                c[3].metric("Unchanged", len(d["unchanged"]))
                c[4].metric("File date", file_date)

                if replacing:
                    st.warning(
                        f"A snapshot for {file_date} already exists — importing replaces "
                        "its positions, account totals, and inferred transactions."
                    )

                def _tbl(entries, cols):
                    return pd.DataFrame([{k: e[k] for k in cols} for e in entries])

                if d["new"]:
                    st.markdown("**New positions**")
                    st.dataframe(_tbl(d["new"], ["account", "symbol", "description",
                                                "new_qty", "new_cost", "new_mv"]),
                                 hide_index=True, use_container_width=True)
                if n_changed:
                    st.markdown("**Quantity changes**")
                    st.dataframe(_tbl(d["increased"] + d["decreased"],
                                      ["account", "symbol", "old_qty", "new_qty", "dqty",
                                       "old_mv", "new_mv"]),
                                 hide_index=True, use_container_width=True)
                if d["closed"]:
                    st.markdown("**Closed positions**")
                    st.dataframe(_tbl(d["closed"], ["account", "symbol", "description",
                                                    "old_qty", "old_mv"]),
                                 hide_index=True, use_container_width=True)

                txns = synthesize_transactions(d, file_date, os.path.abspath(src_path))
                st.caption(
                    f"On confirm: positions + account totals for **{file_date}** are written, "
                    f"and **{len(txns)}** transaction row(s) inferred from the quantity deltas "
                    f"(BUY / SELL) are recorded."
                )

                if st.button("Confirm import", type="primary", key="csv_confirm"):
                    try:
                        info = import_csv(_conn, src_path, USER_ID)
                        # Replace-by-date: this date's inferred transactions are
                        # rewritten from the new file's diff.
                        _conn.execute(
                            "DELETE FROM transactions WHERE trade_date = ? AND user_id = ?",
                            (info["snapshot_date"], USER_ID),
                        )
                        if txns:
                            for _t in txns:
                                _t["user_id"] = USER_ID
                            _conn.executemany(
                                "INSERT INTO transactions (account, trade_date, action, symbol, "
                                "description, quantity, price, amount, fees, realized_gain, "
                                "source_file, user_id) VALUES "
                                "(:account, :trade_date, :action, :symbol, :description, :quantity, "
                                ":price, :amount, :fees, :realized_gain, :source_file, :user_id)",
                                txns,
                            )
                        _conn.commit()
                    except DBError as exc:
                        _conn.rollback()
                        st.error(f"Import failed, nothing was saved: {exc}")
                    else:
                        st.session_state["import_flash"] = (
                            f"Imported snapshot {info['snapshot_date']} — {info['n_positions']} "
                            f"positions, {len(txns)} transaction row(s) recorded."
                        )
                        st.rerun()
            finally:
                _conn.close()


# ---- totals ------------------------------------------------------------- #
m1, m2, m3, m4 = st.columns(4)
m1.metric("Portfolio value", fmt_money(portfolio_value))
m2.metric("Total gain/loss", fmt_money(tot_gl),
          delta=(None if hide_amounts else fmt_pct(tot_glp)))
m3.metric("Holdings value", fmt_money(tot_mv))
m4.metric("Cash", fmt_money(cash))

st.divider()

# ---- performance over time ---------------------------------------- #
p1, p2 = st.columns([0.65, 0.35])
p1.subheader("Performance over time")
series_col = p2.selectbox(
    "Series", [c for c, _, _ in perf.SERIES],
    index=[c for c, _, _ in perf.SERIES].index(load_perf_series()),
    format_func=lambda c: perf.SERIES_LABEL[c], key="perf_series_sel",
    label_visibility="collapsed",
)
if series_col != load_perf_series():
    save_perf_series(series_col)

prng = st.segmented_control("Range", charts.RANGE_LABELS, default="1D",
                            key="perf_range", label_visibility="collapsed") or "1D"

# history() picks the reconstruction's resolution to match `prng`, exactly like
# ticker_series() does for a single ticker (finest Yahoo interval that covers
# the window), and already clips + falls back so this is never < 2 rows once
# there's any data at all.
_hist = perf.history(DB, USER_ID, days=charts.RANGE_DAYS[prng], include_app_open=False)
if len(_hist) < 2:
    st.caption("Not enough data yet — **Sync history** backfills a reconstructed "
               "line from Yahoo, and a point is logged each time you open the app.")
else:
    _fmtname = perf.SERIES_FMT[series_col]
    y_title = perf.SERIES_LABEL[series_col]
    hist_df = pd.DataFrame(_hist)
    hist_df["t"] = pd.to_datetime(hist_df["t"], utc=True, format="mixed")
    pwin = hist_df.dropna(subset=[series_col]).sort_values("t")

    if len(pwin) < 2:
        st.caption(f"No **{y_title}** recorded in this window yet.")
    else:
        # Intraday-resolution data (minutes/hours apart) gets the gaps-compressed
        # axis; daily-resolution data (~1 day apart, weekends aside) doesn't need it.
        _pcompress = pwin["t"].diff().median() < pd.Timedelta(hours=20)

        _pf, _pl, ppct = charts.window_change(pwin, "t", series_col)
        pmcol, _ = st.columns([0.4, 0.6])
        pmcol.metric(y_title, mask_or(FORMATTERS[_fmtname](_pl)),
                     delta=(None if hide_amounts or ppct is None else f"{ppct:+.2f}% over {prng}"))

        _ptips = [alt.Tooltip("t:T", title="When", format="%b %d, %Y  %H:%M")]
        if not hide_amounts:
            _ptips.append(alt.Tooltip(f"{series_col}:Q", title=y_title, format=TOOLTIP_FORMAT[_fmtname]))
        st.altair_chart(
            charts.line(
                pwin, x="t", y=series_col, y_title=y_title, y_format=AXIS_FORMAT[_fmtname],
                mask=hide_amounts, compress_gaps=bool(_pcompress),
                line_color=perf.SOURCE_COLOR["reconstructed"],
                tooltip=_ptips),
            use_container_width=True,
        )
        _covered, _missing = perf.holdings_coverage(DB, USER_ID)
        st.caption(
            f"{len(pwin)} points · reconstructed from current holdings × each bar's close. "
            + (f"Sync {len(_missing)} more ticker(s) to extend the line: {', '.join(_missing)}."
               if _missing else "")
        )

st.divider()

# ---- allocation ----------------------------------------------------- #
alloc = allocate(positions, cash_by_account)


def _alloc_chart(rows, title):
    data = pd.DataFrame(rows)
    if data.empty:
        return None
    data["row"] = data.apply(
        lambda r: r["label"] if (hide_amounts or r["pct"] is None)
        else f"{r['label']}  ·  {r['pct']:.1f}%", axis=1)
    tips = ["label"] if hide_amounts else [
        "label", alt.Tooltip("value:Q", title="Value", format="$,.2f"),
        alt.Tooltip("pct:Q", title="% of portfolio", format=".2f")]
    return (
        alt.Chart(data)
        .mark_bar()
        .encode(
            y=alt.Y("row:N", sort="-x", title=None),
            x=alt.X("value:Q", title=None, axis=alt.Axis(format="$,.2s", labels=not hide_amounts)),
            tooltip=tips,
        )
        .properties(height=44 * len(data) + 40, title=title)
    )


def _short_acct(rows):
    return [{**r, "label": r["label"].replace("Individual ", "").strip()} for r in rows]


al1, al2 = st.columns([0.75, 0.25])
al1.subheader("Allocation")
_asset_labels = [r["label"] for r in alloc["by_asset_type"]]
with al2.popover("Targets", use_container_width=True):
    st.caption("Set a target % of portfolio for any asset type — leave at 0 for no target.")
    _saved_targets = load_alloc_targets()
    _new_targets = {}
    for _lbl in _asset_labels:
        _new_targets[_lbl] = st.number_input(
            _lbl, min_value=0.0, max_value=100.0, step=1.0,
            value=float(_saved_targets.get(_lbl, 0.0)), key=f"target_{_lbl}")
    _new_thresh = st.number_input(
        "Flag drift beyond ± this many percentage points", min_value=0.5, max_value=50.0,
        step=0.5, value=load_drift_threshold(), key="drift_threshold_input")
    if _new_targets != _saved_targets:
        save_alloc_targets(_new_targets)
    if _new_thresh != load_drift_threshold():
        save_drift_threshold(_new_thresh)

a1, a2 = st.columns(2)
c1 = _alloc_chart(alloc["by_asset_type"], "By asset type")
c2 = _alloc_chart(_short_acct(alloc["by_account"]), "By account")
if c1 is not None:
    a1.altair_chart(c1, use_container_width=True)
if c2 is not None:
    a2.altair_chart(c2, use_container_width=True)

if alloc["concentration"]:
    lines = "  \n".join(
        f"- **{r['symbol']}** ({r['account']}) — {fmt_money(r['value'])}, "
        + (MASK if hide_amounts else f"**{r['pct']:.1f}%**") + " of portfolio"
        for r in alloc["concentration"]
    )
    st.warning(f"Positions over {CONCENTRATION_PCT:.0f}% of portfolio value:  \n{lines}")
else:
    st.caption(f"No single position exceeds {CONCENTRATION_PCT:.0f}% of portfolio value.")

_targets = load_alloc_targets()
if _targets:
    _thresh = load_drift_threshold()
    _pct_by_label = {r["label"]: r["pct"] for r in alloc["by_asset_type"]}
    _drift = []
    for _lbl, _target in _targets.items():
        _actual = _pct_by_label.get(_lbl, 0.0) or 0.0
        _delta = _actual - _target
        if abs(_delta) > _thresh:
            _drift.append((_lbl, _actual, _target, _delta))
    _drift.sort(key=lambda r: abs(r[3]), reverse=True)
    if _drift:
        lines = "  \n".join(
            f"- {'▲' if d > 0 else '▼'} **{lbl}** — "
            + (MASK if hide_amounts else f"{actual:.1f}% vs {target:.1f}% target ({d:+.1f} pts)")
            for lbl, actual, target, d in _drift
        )
        st.warning(f"Drifted beyond ±{_thresh:g} pts from target:  \n{lines}")
    else:
        st.caption(f"All targeted asset types are within ±{_thresh:g} pts of target.")

st.divider()

# ---- accounts: side-by-side comparison -------------------------------- #
st.subheader("Accounts")

_acct_stats = {}
for _p, _ctx in zip(positions, contexts):
    _a = _p["account"]
    _s = _acct_stats.setdefault(_a, {"mv": 0.0, "cost": 0.0, "gain": 0.0, "day_change": 0.0, "n": 0})
    _mv = M.eff_mv(_ctx)
    if _mv is not None:
        _s["mv"] += _mv
        _s["n"] += 1
        _cost = _p["cost_basis"]
        if _cost is not None:
            _s["cost"] += _cost
            _s["gain"] += _mv - _cost
        _dchg = M.value("day_change_usd", _ctx)
        if _dchg is not None:
            _s["day_change"] += _dchg

_all_accounts = sorted(set(list(_acct_stats) + list(cash_by_account)))
_acct_rows = []
for _a in _all_accounts:
    _s = _acct_stats.get(_a, {"mv": 0.0, "cost": 0.0, "gain": 0.0, "day_change": 0.0, "n": 0})
    _csh = cash_by_account.get(_a, 0.0) or 0.0
    _total = _s["mv"] + _csh
    _acct_rows.append({
        "account": _a, "total": _total, "holdings": _s["mv"], "cash": _csh,
        "gain_usd": _s["gain"], "gain_pct": (_s["gain"] / _s["cost"] * 100) if _s["cost"] else None,
        "day_change": _s["day_change"], "n_positions": _s["n"],
        "pct_of_portfolio": (_total / portfolio_value * 100) if portfolio_value else None,
    })
_acct_rows.sort(key=lambda r: r["total"], reverse=True)

if len(_acct_rows) < 2:
    st.caption("Only one account in this portfolio — nothing to compare yet.")
else:
    _adf_raw = pd.DataFrame(_acct_rows)
    _adf = pd.DataFrame([{
        "Account": r["account"], "Total Value": fmt_money(r["total"]),
        "% of Portfolio": fmt_pct(r["pct_of_portfolio"]), "Holdings": fmt_money(r["holdings"]),
        "Cash": fmt_money(r["cash"]), "Gain/Loss": fmt_money(r["gain_usd"]),
        "Gain/Loss %": fmt_pct(r["gain_pct"]), "Today": fmt_money(r["day_change"]),
        "Positions": r["n_positions"],
    } for r in _acct_rows])
    _gain_raw = [r["gain_usd"] for r in _acct_rows]
    _today_raw = [r["day_change"] for r in _acct_rows]
    _acct_styler = (
        _adf.style
        .apply(lambda col: [color_sign(v) for v in _gain_raw], subset=["Gain/Loss"])
        .apply(lambda col: [color_sign(v) for v in _today_raw], subset=["Today"])
    )
    st.dataframe(_acct_styler, use_container_width=True, hide_index=True)
    st.download_button(
        "Download CSV", _adf_raw.to_csv(index=False).encode("utf-8"),
        file_name="accounts.csv", mime="text/csv", key="accounts_dl",
        disabled=hide_amounts, help=(
            "Disabled while amounts are hidden — turn off Hide amounts to export real figures."
            if hide_amounts else None),
    )

    st.caption("Asset mix by account:")
    _acct_cols = st.columns(len(_all_accounts))
    for _col, _a in zip(_acct_cols, _all_accounts):
        _acct_positions = [p for p in positions if p["account"] == _a]
        _acct_cash = {_a: cash_by_account.get(_a, 0.0)}
        _acct_alloc = allocate(_acct_positions, _acct_cash)
        _c = _alloc_chart(_acct_alloc["by_asset_type"], _a.replace("Individual ", "").strip())
        if _c is not None:
            _col.altair_chart(_c, use_container_width=True)

st.divider()

# ---- holdings (configurable columns) -------------------------------- #
if "col_keys" not in st.session_state:
    st.session_state["col_keys"] = load_columns()

h1, h2 = st.columns([0.75, 0.25])
h1.subheader("Holdings")
with h2.popover("Columns", use_container_width=True):
    labels = st.multiselect(
        "Columns — add or remove as many as you want",
        [m.label for m in M.AVAILABLE],
        default=[M.BY_KEY[k].label for k in st.session_state["col_keys"] if k in M.BY_KEY],
        key="col_labels",
    )
    new_keys = [M.BY_LABEL[lbl].key for lbl in labels]
    if new_keys and new_keys != st.session_state["col_keys"]:
        st.session_state["col_keys"] = new_keys
        save_columns(new_keys)
    if not perf.has_bars(DB):
        st.caption("The **Yahoo history** columns (MA, Volume, 52-wk, Beta, P/E, Sector) "
                   "stay blank until you click **Sync history** up top.")

# A tappable strip of ticker symbols — the Robinhood-style "click the name"
# entry point into the detail view below. Deliberately separate from the
# data grid's own row/cell interactions (Streamlit's dataframe treats a plain
# cell click as spreadsheet-style cell focus, not row selection — only its
# checkbox actually selects a row, which isn't the one-tap feel we want here).
# The search box keeps this usable as the holdings list grows past a couple
# dozen tickers, where a flat pill strip alone starts taking real scrolling.
# Only one of the Holdings / Watchlist pill strips can be "the" open ticker
# at a time — picking one clears the other via on_change (see _pick_holdings
# / _pick_watchlist below), so there's a single unambiguous selection.
st.caption("Tap a ticker for its chart and full details:")
_desc_by_sym = {p["symbol"]: (p.get("description") or "") for p in positions}
_symbols_held = sorted(_desc_by_sym)
_search = st.text_input("Search tickers", key="ticker_search",
                        placeholder="Filter by symbol or name…", label_visibility="collapsed")
if _search.strip():
    _q = _search.strip().upper()
    _pill_options = [s for s in _symbols_held if _q in s.upper() or _q in _desc_by_sym[s].upper()]
else:
    _pill_options = _symbols_held
# Never let a search term hide the ticker you already have open.
_cur_pill = st.session_state.get("holdings_pill")
if _cur_pill and _cur_pill not in _pill_options:
    _pill_options = sorted(_pill_options + [_cur_pill])


def _pick_holdings():
    st.session_state["watchlist_pill"] = None


def _pick_watchlist():
    st.session_state["holdings_pill"] = None


if not _pill_options:
    st.caption("No ticker matches your search.")
else:
    st.pills("Tickers", _pill_options, key="holdings_pill", label_visibility="collapsed",
            on_change=_pick_holdings)

chosen = [M.BY_KEY[k] for k in st.session_state["col_keys"] if k in M.BY_KEY] \
    or [M.BY_KEY[k] for k in M.DEFAULT_KEYS]

records = [{m.label: M.value(m.key, ctx) for m in chosen} for ctx in contexts]

df = pd.DataFrame(records, columns=[m.label for m in chosen])
fmt_map = {m.label: FORMATTERS[m.fmt] for m in chosen if m.fmt in FORMATTERS}
color_cols = [m.label for m in chosen if m.color_sign]
styler = df.style.format(fmt_map, na_rep="—")
if color_cols:
    styler = styler.map(color_sign, subset=color_cols)
st.dataframe(styler, use_container_width=True, hide_index=True)
st.download_button(
    "Download CSV", df.to_csv(index=False).encode("utf-8"),
    file_name="holdings.csv", mime="text/csv", key="holdings_dl",
    disabled=hide_amounts, help=(
        "Disabled while amounts are hidden — turn off Hide amounts to export real figures."
        if hide_amounts else None),
)
st.caption("Green = gain, red = loss. Price / Market Value / Gain-Loss use the live price where "
           "available, otherwise the CSV's figures. Edit the column set with **Columns**.")

st.divider()

# ---- watchlist: tickers tracked for their chart/stats, not owned ------- #
st.subheader("Watchlist")
wc1, wc2 = st.columns([0.75, 0.25])
_wl_raw = wc1.text_input("Add a ticker", key="wl_add_input", placeholder="Add a ticker, e.g. NVDA",
                         label_visibility="collapsed")
wc2.write("")
if wc2.button("+ Add to watchlist", use_container_width=True) and _wl_raw.strip():
    _wl_conn = connect(DB)
    try:
        added = watchlist.add(_wl_conn, USER_ID, _wl_raw)
    finally:
        _wl_conn.close()
    if added:
        st.session_state["refresh_msg"] = (
            "success", f"Added **{added}** to your watchlist. "
                       f"Click **Sync history** up top to pull its chart data.")
    else:
        st.session_state["refresh_msg"] = ("error", f"'{_wl_raw}' doesn't look like a valid ticker.")
    st.rerun()

if not watch_only:
    st.caption("Nothing on your watchlist yet — add a ticker above to track its chart and stats "
               "without owning it.")
else:
    st.caption("Tap a ticker for its chart and stats (no position, so no cost/return figures):")
    st.pills("Watchlist", sorted(watch_only), key="watchlist_pill", label_visibility="collapsed",
            on_change=_pick_watchlist)

st.divider()

_pill_sym = st.session_state.get("holdings_pill") or st.session_state.get("watchlist_pill")

# ---- ticker detail: chart + everything about the position/security ----- #
_idx = next((i for i, p in enumerate(positions) if p["symbol"] == _pill_sym), None) \
    if _pill_sym else None
_is_held = _idx is not None
if _pill_sym and not _is_held:
    # Watchlist-only ticker: synthesize a position-less context. eff_price /
    # eff_mv / etc. all read via ctx.get(...).get(...) so a missing "pos"
    # field just resolves to None instead of raising.
    _pos = {"symbol": _pill_sym, "description": sec_info.get(_pill_sym, {}).get("name")}
    _ctx = {"pos": _pos, "quote": quotes.get(_pill_sym, {}), "stats": bar_stats.get(_pill_sym, {}),
            "info": sec_info.get(_pill_sym, {}), "port_value": portfolio_value, "acct_value": None}
if _pill_sym:
    if _is_held:
        _pos = positions[_idx]
        _ctx = contexts[_idx]
    _sym = _pos["symbol"]
    _has_yahoo = perf.ticker_has_bars(DB, _sym)

    with st.container(border=True):
        # ---- header: symbol, description, live price, today's move ---- #
        hc1, hc2 = st.columns([0.65, 0.35])
        with hc1:
            st.markdown(f"## {_sym}")
            if _pos.get("description"):
                st.caption(_pos["description"])
        _price = M.eff_price(_ctx)
        _dchg_pct = M.value("day_change_pct", _ctx)
        _dchg_usd = M.value("day_change_usd", _ctx)
        with hc2:
            st.metric("Price", fmt_price(_price),
                      delta=(None if hide_amounts or _dchg_pct is None
                             else f"{fmt_money(_dchg_usd)} ({_dchg_pct:+.2f}%) today"))
        _price_at = M.value("price_at", _ctx)
        if _price_at:
            st.caption(f"As of {_price_at}")

        t1, t2 = st.columns([0.6, 0.4])
        t1.markdown("#### Price history")
        _series = perf.PRICE_SERIES if _has_yahoo else perf.TICKER_SERIES
        _series_label = perf.PRICE_SERIES_LABEL if _has_yahoo else perf.TICKER_SERIES_LABEL
        _series_fmt = perf.PRICE_SERIES_FMT if _has_yahoo else perf.TICKER_SERIES_FMT
        tk_col = t2.selectbox("Ticker series", [c for c, _, _ in _series],
                              format_func=lambda c: _series_label[c],
                              key="tk_series_sel", label_visibility="collapsed")

        rng = st.segmented_control("Range", charts.RANGE_LABELS, default="1D",
                                   key="tk_range", label_visibility="collapsed") or "1D"

        if _has_yahoo:
            # ticker_series() already picks the finest resolution Yahoo has for this
            # window (1-minute up through daily) and clips to it at the SQL level.
            _rows, _interval = perf.ticker_series(DB, _sym, charts.RANGE_DAYS[rng])
            _short = False
        else:
            _th = perf.ticker_history(DB, _sym)
            _rows = [{**r, "t": r["fetched_at"]} for r in _th]
            _interval = "sparse"

        if len(_rows) < 2:
            st.info(f"Not enough history for **{_sym}** in this range yet. Click "
                    "**Sync history** up top for real intraday + daily bars, or keep "
                    "hitting **Refresh prices**.")
        else:
            tdf = pd.DataFrame(_rows)
            tdf["t"] = pd.to_datetime(tdf["t"], utc=True, format="mixed")
            full = tdf.dropna(subset=[tk_col]) if tk_col in tdf.columns else tdf.iloc[0:0]

            if full.empty:
                st.info(f"No **{_series_label[tk_col]}** recorded for {_sym} at this resolution.")
            else:
                _fname = _series_fmt[tk_col]
                _title = _series_label[tk_col]
                if _has_yahoo:
                    win = full  # already clipped to the range by ticker_series()
                else:
                    win, _short = charts.window(full, "t", charts.RANGE_DAYS[rng])

                mas = []
                if _interval == "1d" and tk_col == "close":
                    picked = st.segmented_control(
                        "Moving averages", list(charts.MA_STYLE), format_func=lambda w: f"{w}-day",
                        selection_mode="multi", key="tk_ma", label_visibility="collapsed") or []
                    mas = [(f"ma_{w}", *charts.MA_STYLE[w]) for w in picked if f"ma_{w}" in win.columns]

                first, last, pct = charts.window_change(win, "t", tk_col)
                mcol, _sp = st.columns([0.4, 0.6])
                mcol.metric(
                    _title, FORMATTERS[_fname](last) if _fname in FORMATTERS else mask_or(f"{last:,.2f}"),
                    delta=(None if hide_amounts or pct is None else f"{pct:+.2f}% over {rng}"))

                _date_fmt = "%b %d, %Y" if _interval == "1d" else "%b %d, %Y  %H:%M"
                _tips = [alt.Tooltip("t:T", title="Date", format=_date_fmt)]
                if not hide_amounts:
                    _tips.append(alt.Tooltip(f"{tk_col}:Q", title=_title, format=TOOLTIP_FORMAT[_fname]))
                    for _mc, _, _ in mas:
                        _tips.append(alt.Tooltip(f"{_mc}:Q", title=_mc.replace("ma_", "") + "-day MA",
                                                 format="$,.2f"))
                st.altair_chart(
                    charts.line(win, x="t", y=tk_col, y_title=_title, y_format=AXIS_FORMAT[_fname],
                                overlays=mas, tooltip=_tips, mask=hide_amounts,
                                compress_gaps=(_interval in ("1m", "5m", "15m", "60m"))),
                    use_container_width=True,
                )
                _res_label = perf.INTERVAL_LABEL.get(_interval, _interval)
                st.caption(
                    f"{len(win)}" + (f" of {len(full)}" if len(win) != len(full) else "") + " points · "
                    + (f"**{_res_label}** Yahoo bars." if _has_yahoo
                       else "sparse Refresh-prices history — **Sync history** for real bars.")
                    + (f"  ·  *{rng} is shorter than the data interval — showing the last {len(win)}.*"
                       if _short else "")
                )

        # ---- your position (held) or watchlist note ------------------- #
        st.divider()
        if _is_held:
            st.markdown("#### Your position")
            _qty = M.value("quantity", _ctx)
            _cost_basis = M.value("cost_basis", _ctx)
            _avg_cost = (_cost_basis / _qty) if (_qty and _cost_basis is not None) else None
            _mv = M.eff_mv(_ctx)
            _unreal_usd = M.value("unrealized_usd", _ctx)
            _unreal_pct = M.value("unrealized_pct", _ctx)
            _pct_port = M.value("pct_of_portfolio", _ctx)

            pc1, pc2, pc3, pc4 = st.columns(4)
            pc1.metric("Shares", fmt_qty(_qty))
            pc2.metric("Avg Cost", fmt_price(_avg_cost))
            pc3.metric("Market Value", fmt_money(_mv))
            pc4.metric("Total Return", fmt_money(_unreal_usd),
                      delta=(None if hide_amounts or _unreal_pct is None else f"{_unreal_pct:+.2f}%"))

            pc5, pc6, pc7, pc8 = st.columns(4)
            pc5.metric("Cost Basis", fmt_money(_cost_basis))
            pc6.metric("Today's Return", fmt_money(_dchg_usd),
                      delta=(None if hide_amounts or _dchg_pct is None else f"{_dchg_pct:+.2f}%"))
            pc7.metric("% of Portfolio", fmt_pct(_pct_port))
            pc8.metric("Account", _pos.get("account") or "—")
        else:
            st.markdown("#### On your watchlist")
            st.caption("Not a position you own — tracking it for the chart and stats only.")

            def _remove_from_watchlist(sym=_sym):
                # Must clear the "watchlist_pill" widget's state from a
                # callback, not the main script body below where it renders -
                # Streamlit forbids writing to a widget's key after that
                # widget has already been instantiated in the same run.
                _wl_conn = connect(DB)
                try:
                    watchlist.remove(_wl_conn, USER_ID, sym)
                finally:
                    _wl_conn.close()
                st.session_state["watchlist_pill"] = None

            st.button("Remove from watchlist", key="wl_remove_from_detail",
                     on_click=_remove_from_watchlist)

        # ---- stats: day range, fundamentals, income -------------------- #
        st.markdown("#### Stats")
        _stat_tiles(_ctx, [
            "prev_close", "day_open", "day_high", "day_low",
            "week52_high", "week52_low", "pct_off_52wk_high",
            "volume", "avg_volume", "beta", "pe_ttm", "pb_ratio", "market_cap", "sector",
            "ma_20", "ma_50", "ma_200", "price_vs_ma50",
            "div_yield_pct", "div_pay_date", "reinvest", "next_earnings",
        ])
        if not perf.has_bars(DB):
            st.caption("Fundamentals (52-wk range, beta, P/E, market cap, sector, moving averages) "
                       "fill in after you click **Sync history** up top.")

        # ---- news: cached Finnhub headlines, fetched when stale --------- #
        st.markdown("#### Recent News")
        _news_key = resolve_key(None, ENV_PATH)
        if not _news_key:
            st.caption("No `FINNHUB_API_KEY` in `.env` — news uses the same key as "
                       "**Refresh prices**.")
        else:
            _news_conn = connect(DB)
            try:
                _n_new, _news_err = news.sync_ticker(_news_conn, _sym, _news_key)
                _articles = news.latest_news(_news_conn, _sym)
            finally:
                _news_conn.close()
            if _news_err and not _articles:
                st.caption(f"Couldn't load news for {_sym}: {_news_err}")
            elif not _articles:
                st.caption(f"No recent news for {_sym} in the last {news.LOOKBACK_DAYS} days.")
            else:
                for _a in _articles:
                    _pub = (pd.to_datetime(_a["published_at"], utc=True).strftime("%b %d, %Y %H:%M UTC")
                           if _a["published_at"] else "")
                    st.markdown(f"**[{_a['headline']}]({_a['url']})**  \n*{_a['source']} · {_pub}*")
                    if _a.get("summary"):
                        _sumtext = _a["summary"]
                        st.caption(_sumtext[:220] + ("…" if len(_sumtext) > 220 else ""))
    st.divider()
else:
    st.caption("↑ Tap a ticker above to see its chart and full details here.")
    st.divider()

# ---- activity: inferred transaction history --------------------------- #
st.subheader("Activity")

_txn_conn = connect(DB)
try:
    _all_txns = [dict(r) for r in _txn_conn.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY trade_date DESC, id DESC", (USER_ID,))]
finally:
    _txn_conn.close()

if not _all_txns:
    st.caption("No transactions yet — they're inferred automatically the next time you import "
               "a CSV whose quantities differ from your last snapshot.")
else:
    fc1, fc2, fc3 = st.columns(3)
    _f_accounts = fc1.multiselect(
        "Account", sorted({t["account"] for t in _all_txns if t["account"]}), key="txn_f_account")
    _f_actions = fc2.multiselect(
        "Action", sorted({t["action"] for t in _all_txns if t["action"]}), key="txn_f_action")
    _f_symbol = fc3.text_input("Symbol contains", key="txn_f_symbol", placeholder="e.g. AAPL")

    _filtered = _all_txns
    if _f_accounts:
        _filtered = [t for t in _filtered if t["account"] in _f_accounts]
    if _f_actions:
        _filtered = [t for t in _filtered if t["action"] in _f_actions]
    if _f_symbol.strip():
        _sq = _f_symbol.strip().upper()
        _filtered = [t for t in _filtered if _sq in (t["symbol"] or "").upper()]

    if not _filtered:
        st.caption("No transactions match these filters.")
    else:
        _realized = [t["realized_gain"] for t in _filtered if t["realized_gain"] is not None]
        rc1, rc2 = st.columns(2)
        rc1.metric("Transactions shown", len(_filtered))
        if _realized:
            rc2.metric("Realized gain/loss", fmt_money(sum(_realized)))

        # Pre-formatted to display strings (not left as raw floats for the
        # Styler to format at render time) - Streamlit's dataframe grid
        # doesn't reliably pick up a Styler's na_rep/format for a NaN cell,
        # rendering the raw missing value as the literal text "None" instead.
        # Color still needs the ORIGINAL numbers, so it's computed from a
        # closure over the raw list, independent of the now-string columns.
        _amount_raw = [t["amount"] for t in _filtered]
        _gain_raw = [t["realized_gain"] for t in _filtered]
        _tdf = pd.DataFrame([{
            "Date": t["trade_date"], "Action": t["action"], "Symbol": t["symbol"],
            "Description": t["description"], "Qty": fmt_qty(t["quantity"]),
            "Price": fmt_price(t["price"]), "Amount": fmt_money(t["amount"]),
            "Realized G/L": fmt_money(t["realized_gain"]), "Account": t["account"],
        } for t in _filtered])
        _txn_styler = (
            _tdf.style
            .apply(lambda col: [color_sign(v) for v in _amount_raw], subset=["Amount"])
            .apply(lambda col: [color_sign(v) for v in _gain_raw], subset=["Realized G/L"])
        )
        st.dataframe(_txn_styler, use_container_width=True, hide_index=True)
        _tdf_raw = pd.DataFrame([{
            "Date": t["trade_date"], "Action": t["action"], "Symbol": t["symbol"],
            "Description": t["description"], "Qty": t["quantity"], "Price": t["price"],
            "Amount": t["amount"], "Realized G/L": t["realized_gain"], "Account": t["account"],
        } for t in _filtered])
        st.download_button(
            "Download CSV", _tdf_raw.to_csv(index=False).encode("utf-8"),
            file_name="activity.csv", mime="text/csv", key="activity_dl",
            disabled=hide_amounts, help=(
                "Disabled while amounts are hidden — turn off Hide amounts to export real figures."
                if hide_amounts else None),
        )
        st.caption("Inferred from the quantity change between imported snapshots, not broker "
                   "trade confirmations — Price/Amount are estimates, and Realized G/L uses the "
                   "average-cost method (a Positions export has no per-lot detail for FIFO).")

st.divider()

# ---- income: dividend yield summary + per-position breakdown ---------- #
st.subheader("Income")

_income_rows = []
for p, ctx in zip(positions, contexts):
    _yld = M.value("div_yield_pct", ctx)
    _mv = M.eff_mv(ctx)
    if _yld is None or _mv is None:
        continue
    _income_rows.append({
        "symbol": p["symbol"], "description": p.get("description"), "market_value": _mv,
        "yield_pct": _yld, "est_income": _mv * _yld / 100,
        "last_pay_date": p.get("div_pay_date"),
        "reinvest": {1: "Yes", 0: "No"}.get(p.get("reinvest")), "account": p["account"],
    })

if not _income_rows:
    st.caption("No dividend-yield data on any position yet — it comes straight from the Schwab "
               "CSV export's **Dividend Yield** / **Div Pay Date** columns, not Yahoo, so it's "
               "only there if your broker reported it at import time.")
else:
    _total_income = sum(r["est_income"] for r in _income_rows)
    _yield_on_holdings = (_total_income / tot_mv * 100) if tot_mv else None

    ic1, ic2, ic3 = st.columns(3)
    ic1.metric("Est. annual dividend income", fmt_money(_total_income))
    ic2.metric("Yield on holdings", fmt_pct(_yield_on_holdings))
    ic3.metric("Income-producing positions", f"{len(_income_rows)} / {len(positions)}")

    _income_rows.sort(key=lambda r: r["est_income"], reverse=True)
    _idf_raw = pd.DataFrame([{
        "Symbol": r["symbol"], "Description": r["description"],
        "Market Value": r["market_value"], "Div Yield %": r["yield_pct"],
        "Est. Annual Income": r["est_income"], "Last Pay Date": r["last_pay_date"],
        "Reinvest": r["reinvest"], "Account": r["account"],
    } for r in _income_rows])
    _idf = pd.DataFrame([{
        "Symbol": r["symbol"], "Description": r["description"],
        "Market Value": fmt_money(r["market_value"]), "Div Yield %": fmt_pct(r["yield_pct"]),
        "Est. Annual Income": fmt_money(r["est_income"]),
        "Last Pay Date": r["last_pay_date"] or "—", "Reinvest": r["reinvest"] or "—",
        "Account": r["account"],
    } for r in _income_rows])
    st.dataframe(_idf, use_container_width=True, hide_index=True)
    st.download_button(
        "Download CSV", _idf_raw.to_csv(index=False).encode("utf-8"),
        file_name="income.csv", mime="text/csv", key="income_dl",
        disabled=hide_amounts, help=(
            "Disabled while amounts are hidden — turn off Hide amounts to export real figures."
            if hide_amounts else None),
    )
    st.caption("Est. Annual Income = market value × dividend yield, both as reported in the CSV "
               "— a simple estimate, not a payment schedule. **Last Pay Date** is the most "
               "recently known payment from Schwab, not a prediction of the next one.")
