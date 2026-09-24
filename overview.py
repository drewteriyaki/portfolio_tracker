"""Advisor clients overview: one summary row per client account.

Pure logic, no Streamlit. Figures are computed the same way the Dashboard
computes them (metrics.eff_mv for value, alerts.evaluate for alerts), so
a client's row matches what the advisor sees after opening that client.
"""

from __future__ import annotations

import advisor
import alerts
import metrics as M
from update_prices import latest_snapshot


def latest_quotes(conn) -> dict:
    """{ticker: latest successful price_history row} - shared market data."""
    return {r["ticker"]: dict(r) for r in conn.execute(
        "SELECT ph.* FROM price_history ph JOIN ("
        "  SELECT ticker, MAX(fetched_at) AS m FROM price_history WHERE ok = 1 GROUP BY ticker"
        ") latest ON ph.ticker = latest.ticker AND ph.fetched_at = latest.m WHERE ph.ok = 1")}


def account_summary(conn, user_id: int, quotes: dict, rules=None) -> dict:
    """Headline figures for one account. `rules` are that account's alert
    rules (alerts.DEFAULT_RULES if None)."""
    profile = advisor.get_profile(conn, user_id)
    answered = len(advisor.REQUIRED_PROFILE_FIELDS) - len(advisor.missing_fields(profile))
    out = {"user_id": user_id, "has_data": False, "snapshot_date": None, "imported_at": None,
           "portfolio_value": None, "gain_pct": None, "n_positions": 0, "n_alerts": 0,
           "profile_answered": answered, "profile_total": len(advisor.REQUIRED_PROFILE_FIELDS)}

    snap = latest_snapshot(conn, user_id)
    if not snap:
        return out
    positions = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE snapshot_date = ? AND user_id = ?", (snap, user_id))]
    cash = sum(r["cash_value"] or 0.0 for r in conn.execute(
        "SELECT cash_value FROM account_totals WHERE snapshot_date = ? AND user_id = ?",
        (snap, user_id)))
    contexts = [{"pos": p, "quote": quotes.get(p["symbol"], {}), "stats": {}, "info": {},
                 "port_value": None, "acct_value": None} for p in positions]

    mv = cost = gain = 0.0
    for p, ctx in zip(positions, contexts):
        v = M.eff_mv(ctx)
        if v is None:
            continue
        mv += v
        if p["cost_basis"] is not None:
            cost += p["cost_basis"]
            gain += v - p["cost_basis"]
    portfolio_value = mv + cash
    for ctx in contexts:
        ctx["port_value"] = portfolio_value

    imported = conn.execute(
        "SELECT MAX(imported_at) m FROM snapshots WHERE user_id = ? AND snapshot_date = ?",
        (user_id, snap)).fetchone()
    out.update({
        "has_data": True, "snapshot_date": snap, "imported_at": imported["m"] if imported else None,
        "portfolio_value": round(portfolio_value, 2),
        "gain_pct": round(gain / cost * 100, 2) if cost else None,
        "n_positions": len(positions),
        "n_alerts": len(alerts.evaluate(contexts, rules)),
    })
    return out
