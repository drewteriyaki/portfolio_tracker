"""Portfolio allocation breakdowns. Pure functions, standard library only.

Groups holdings by asset type and by account, using the best available market
value per position (applied live value, else the CSV's), and treats each
account's cash as its own slice. Also flags positions that are a large share of
the whole portfolio.
"""

from __future__ import annotations

CONCENTRATION_PCT = 15.0  # a single position above this share of the portfolio is flagged

# Long Schwab asset-type strings -> short labels for the chart.
SHORT_ASSET_TYPE = {
    "ETFs & Closed End Funds": "ETF / CEF",
    "Cash and Money Market": "Cash",
}


def _mv(pos):
    v = pos.get("live_market_value")
    if v is not None:
        return float(v)
    v = pos.get("market_value")
    return float(v) if v is not None else 0.0


def _rows(totals: dict, portfolio_value: float):
    rows = [{"label": k, "value": round(v, 2),
             "pct": round(v / portfolio_value * 100, 2) if portfolio_value else None}
            for k, v in totals.items()]
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


def allocate(positions, cash_by_account: dict | None = None):
    """positions: iterable of dicts with account, asset_type, market_value and/or
    live_market_value. cash_by_account: {account: cash_value}.

    Returns {"portfolio_value", "by_asset_type", "by_account", "concentration"}.
    """
    cash_by_account = {k: float(v or 0.0) for k, v in (cash_by_account or {}).items()}
    positions = list(positions)

    holdings_value = sum(_mv(p) for p in positions)
    cash_total = sum(cash_by_account.values())
    portfolio_value = holdings_value + cash_total

    by_type: dict[str, float] = {}
    by_acct: dict[str, float] = {}
    for p in positions:
        mv = _mv(p)
        at = p.get("asset_type") or "Unknown"
        by_type[SHORT_ASSET_TYPE.get(at, at)] = by_type.get(SHORT_ASSET_TYPE.get(at, at), 0.0) + mv
        acct = p.get("account") or "Unknown"
        by_acct[acct] = by_acct.get(acct, 0.0) + mv

    if cash_total:
        by_type["Cash"] = by_type.get("Cash", 0.0) + cash_total
    for acct, cash in cash_by_account.items():
        by_acct[acct] = by_acct.get(acct, 0.0) + cash

    concentration = []
    for p in positions:
        mv = _mv(p)
        share = (mv / portfolio_value * 100) if portfolio_value else 0.0
        if share > CONCENTRATION_PCT:
            concentration.append({
                "symbol": p.get("symbol"),
                "account": p.get("account"),
                "value": round(mv, 2),
                "pct": round(share, 2),
            })
    concentration.sort(key=lambda r: r["pct"], reverse=True)

    return {
        "portfolio_value": round(portfolio_value, 2),
        "by_asset_type": _rows(by_type, portfolio_value),
        "by_account": _rows(by_acct, portfolio_value),
        "concentration": concentration,
    }
