"""Advisor clients overview: one summary row per client account.

Pure logic, no Streamlit. Figures are computed the same way the Dashboard
computes them (metrics.eff_mv for value, alerts.evaluate for alerts), so
a client's row matches what the advisor sees after opening that client.
"""

from __future__ import annotations

import advisor
import alerts
import asset_classes
import metrics as M
from allocation import allocate
from update_prices import latest_snapshot


def attention_alerts(fired) -> int:
    """How many holdings make an advisor's client "need a look": a move past
    the day-move limit (either way), or a loss past the gain/loss limit. A
    holding UP past that limit - most long-held ones, +20% by default - is
    good news, not a reason to look, so it isn't counted; each holding counts
    once however many limits it passes."""
    return len({(a.symbol, a.account) for a in fired
                if a.rule_key != "total_gl" or a.direction == "down"})


def latest_quotes(conn, tickers=None) -> dict:
    """{ticker: latest successful price_history row} - shared market data.
    Pass `tickers` to read only those (price_history grows every minute, so
    reading every ticker's latest row gets slower over time)."""
    if tickers is not None:
        tickers = sorted(set(tickers))
        if not tickers:
            return {}
        ph = ", ".join("?" for _ in tickers)
        return {r["ticker"]: dict(r) for r in conn.execute(
            "SELECT ph.* FROM price_history ph JOIN ("
            f"  SELECT ticker, MAX(fetched_at) AS m FROM price_history WHERE ok = 1 AND ticker IN ({ph})"
            "  GROUP BY ticker"
            ") latest ON ph.ticker = latest.ticker AND ph.fetched_at = latest.m WHERE ph.ok = 1",
            tuple(tickers))}
    return {r["ticker"]: dict(r) for r in conn.execute(
        "SELECT ph.* FROM price_history ph JOIN ("
        "  SELECT ticker, MAX(fetched_at) AS m FROM price_history WHERE ok = 1 GROUP BY ticker"
        ") latest ON ph.ticker = latest.ticker AND ph.fetched_at = latest.m WHERE ph.ok = 1")}


def account_summary(conn, user_id: int, quotes: dict, rules=None, *, overrides=None) -> dict:
    """Headline figures for one account. `rules` are that account's alert
    rules (alerts.DEFAULT_RULES if None); `overrides` its asset-class choices
    (asset_classes.load_overrides - read here if None)."""
    profile = advisor.get_profile(conn, user_id)
    snap = latest_snapshot(conn, user_id)
    if not snap:
        return _summary(user_id, profile)
    positions = [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE snapshot_date = ? AND user_id = ? ORDER BY id",
        (snap, user_id))]
    cash_by_account = {r["account"]: r["cash_value"] or 0.0 for r in conn.execute(
        "SELECT account, cash_value FROM account_totals WHERE snapshot_date = ? AND user_id = ? "
        "ORDER BY id", (snap, user_id))}
    imported = conn.execute(
        "SELECT MAX(imported_at) m FROM snapshots WHERE user_id = ? AND snapshot_date = ?",
        (user_id, snap)).fetchone()
    if overrides is None:
        overrides = asset_classes.load_overrides(conn, user_id)
    info = asset_classes.load_info(conn, [p["symbol"] for p in positions])
    return _summary(user_id, profile, snap, positions, cash_by_account,
                    imported["m"] if imported else None, quotes, rules, overrides, info)


def account_summaries(conn, user_ids, quotes: dict, rules=None, *, overrides=None) -> dict:
    """account_summary() for a whole book of accounts in a fixed number of
    queries, however many there are: {user_id: summary}. `rules` and
    `overrides` are by account ({user_id: ...}): one missing from `rules`
    gets alerts.DEFAULT_RULES, one missing from `overrides` has its
    asset-class choices read from its settings (one query for all of them)."""
    ids = tuple(dict.fromkeys(user_ids))
    if not ids:
        return {}
    rules = rules or {}
    profiles = advisor.get_profiles(conn, ids)
    # each account's latest snapshot, as in update_prices.latest_snapshot
    latest = ("(SELECT user_id, MAX(snapshot_date) AS d FROM positions WHERE user_id IN "
              f"({', '.join('?' for _ in ids)}) GROUP BY user_id) l")
    positions, snaps = {}, {}
    for r in conn.execute(f"SELECT p.* FROM positions p JOIN {latest} ON p.user_id = l.user_id "
                          "AND p.snapshot_date = l.d ORDER BY p.user_id, p.id", ids):
        positions.setdefault(r["user_id"], []).append(dict(r))
        snaps[r["user_id"]] = r["snapshot_date"]
    cash = {}
    for r in conn.execute(f"SELECT t.user_id, t.account, t.cash_value FROM account_totals t "
                          f"JOIN {latest} ON t.user_id = l.user_id AND t.snapshot_date = l.d "
                          "ORDER BY t.user_id, t.id", ids):
        cash.setdefault(r["user_id"], {})[r["account"]] = r["cash_value"] or 0.0
    imported = {r["user_id"]: r["m"] for r in conn.execute(
        f"SELECT s.user_id, MAX(s.imported_at) AS m FROM snapshots s JOIN {latest} "
        "ON s.user_id = l.user_id AND s.snapshot_date = l.d GROUP BY s.user_id", ids)}
    overrides = dict(overrides or {})
    unread = [i for i in snaps if i not in overrides]
    if unread:
        import prefs
        overrides.update({i: asset_classes.overrides_in(saved)
                          for i, saved in prefs.load_many(conn, unread).items()})
    info = asset_classes.load_info(conn, [p["symbol"] for ps in positions.values() for p in ps])
    return {i: (_summary(i, profiles[i], snaps[i], positions[i], cash.get(i, {}),
                         imported.get(i), quotes, rules.get(i), overrides[i], info)
                if i in snaps else _summary(i, profiles[i]))
            for i in ids}


def _summary(user_id, profile, snap=None, positions=(), cash_by_account=None, imported_at=None,
             quotes=None, rules=None, overrides=None, info=None) -> dict:
    """The summary dict from what was read: account_summary and
    account_summaries share it, so one account comes out the same either way."""
    answered = len(advisor.REQUIRED_PROFILE_FIELDS) - len(advisor.missing_fields(profile))
    out = {"user_id": user_id, "has_data": False, "snapshot_date": None, "imported_at": None,
           "portfolio_value": None, "gain_pct": None, "n_positions": 0, "n_alerts": 0,
           "n_alerts_attention": 0,
           "alloc_pct": {},
           "profile_answered": answered, "profile_total": len(advisor.REQUIRED_PROFILE_FIELDS)}
    if not snap:
        return out
    cash = sum(cash_by_account.values())
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
    fired = alerts.evaluate(contexts, rules)

    out.update({
        "has_data": True, "snapshot_date": snap, "imported_at": imported_at,
        "portfolio_value": round(portfolio_value, 2),
        "gain_pct": round(gain / cost * 100, 2) if cost else None,
        "n_positions": len(positions),
        "n_alerts": len(fired),
        "n_alerts_attention": attention_alerts(fired),
        # % of portfolio by asset class, for comparing against a target mix
        "alloc_pct": {r["label"]: r["pct"] or 0.0 for r in allocate(
            [{**p, "live_market_value": M.eff_mv(c)} for p, c in zip(positions, contexts)],
            cash_by_account,
            asset_classes.splits_from(positions, info, overrides)
        )["by_asset_class"]},
    })
    return out
