"""Registry of per-position stats the dashboard can show as columns.

Each `Metric` knows its label, a group (for the column picker), how to compute a
raw value from a context dict, and which formatter the UI should use. The holdings
table's column picker and (later) the performance chart's "plot any stat" selector
both read from this one list, so adding a stat is a single entry here.

A metric's `fn` receives a context dict:
    pos         - dict of the position's row from `positions` (latest snapshot)
    quote       - dict of the latest OK `price_history` row for the ticker, or {}
    stats       - {last_close, volume, ma_20, ma_50, ma_200} from daily_bars, or {}
    info        - security_info row (52wk, beta, PE, sector, ...) for the ticker, or {}
    port_value  - total portfolio value (effective MV of all holdings + all cash)
    acct_value  - value of this position's account (its holdings' MV + its cash)

`fn` must never raise; return None when a value can't be computed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    group: str
    fn: Callable[[dict], Any]
    fmt: str = "text"          # text | money | pct | price | qty | int | date
    available: bool = True
    note: str = ""
    color_sign: bool = False   # UI: green/red by sign


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _q(ctx, name):
    return _f((ctx.get("quote") or {}).get(name))


def _p(ctx, name):
    return _f((ctx.get("pos") or {}).get(name))


def _s(ctx, name):
    return _f((ctx.get("stats") or {}).get(name))


def _i(ctx, name):
    return _f((ctx.get("info") or {}).get(name))


def eff_price(ctx):
    """Best available per-share price: applied live price, then latest quote,
    then the CSV's implied price (market value / quantity), then the most
    recent Yahoo daily close - the only one available for a watchlist ticker
    that isn't an owned position (no live_price/quote/market_value at all)."""
    v = _p(ctx, "live_price") or _q(ctx, "price")
    if v:
        return v
    mv, qty = _p(ctx, "market_value"), _p(ctx, "quantity")
    if mv is not None and qty:
        return mv / qty
    return _s(ctx, "last_close")


def eff_mv(ctx):
    """Best available position market value."""
    v = _p(ctx, "live_market_value")
    if v is not None:
        return v
    px, qty = eff_price(ctx), _p(ctx, "quantity")
    if px is not None and qty is not None:
        return px * qty
    return _p(ctx, "market_value")


def _ratio(a, b):
    return (a / b * 100) if (a is not None and b) else None


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def _reinvest(ctx):
    v = (ctx.get("pos") or {}).get("reinvest")
    return {1: "Yes", 0: "No"}.get(v)


def _day_change_pct(ctx):
    v = _q(ctx, "pct_change")
    return v if v is not None else _p(ctx, "day_change_pct")


def _day_change_usd(ctx):
    d, qty = _q(ctx, "change"), _p(ctx, "quantity")
    if d is not None and qty is not None:
        return d * qty
    pctv, mv = _day_change_pct(ctx), eff_mv(ctx)
    return (pctv / 100 * mv) if (pctv is not None and mv is not None) else None


def _unreal_usd(ctx):
    mv, cost = eff_mv(ctx), _p(ctx, "cost_basis")
    return (mv - cost) if (mv is not None and cost is not None) else None


def _unreal_csv_usd(ctx):
    mv, cost = _p(ctx, "market_value"), _p(ctx, "cost_basis")
    return (mv - cost) if (mv is not None and cost is not None) else None


METRICS: list[Metric] = [
    # -- Identity ----------------------------------------------------------- #
    Metric("symbol", "Symbol", "Identity", lambda c: (c["pos"] or {}).get("symbol")),
    Metric("description", "Description", "Identity", lambda c: (c["pos"] or {}).get("description")),
    Metric("account", "Account", "Identity", lambda c: (c["pos"] or {}).get("account")),
    Metric("asset_type", "Asset Type", "Identity", lambda c: (c["pos"] or {}).get("asset_type")),

    # -- Position --------------------------------------------------------- #
    Metric("quantity", "Qty", "Position", lambda c: _p(c, "quantity"), "qty"),
    Metric("cost_basis", "Cost Basis", "Position", lambda c: _p(c, "cost_basis"), "money"),
    Metric("market_value", "Market Value", "Position", eff_mv, "money"),
    Metric("market_value_csv", "Market Value (CSV)", "Position", lambda c: _p(c, "market_value"), "money"),

    # -- Price ---------------------------------------------------------- #
    Metric("price", "Price", "Price", eff_price, "price"),
    Metric("prev_close", "Prev Close", "Price", lambda c: _q(c, "prev_close"), "price"),
    Metric("day_open", "Day Open", "Price", lambda c: _q(c, "day_open"), "price"),
    Metric("day_high", "Day High", "Price", lambda c: _q(c, "day_high"), "price"),
    Metric("day_low", "Day Low", "Price", lambda c: _q(c, "day_low"), "price"),
    Metric("price_at", "Price as of", "Price",
           lambda c: _p(c, "live_price_at") or (c.get("quote") or {}).get("fetched_at"), "text"),

    # -- Today -------------------------------------------------------- #
    Metric("day_change_pct", "Day Change %", "Today", _day_change_pct, "pct", color_sign=True),
    Metric("day_change_usd", "Gain/Loss Today $", "Today", _day_change_usd, "money", color_sign=True),
    Metric("pct_off_high", "% Off Day High", "Today",
           lambda c: _ratio((eff_price(c) - _q(c, "day_high")) if (eff_price(c) is not None and _q(c, "day_high")) else None,
                            _q(c, "day_high")), "pct"),
    Metric("price_change_pct", "Price Change % (CSV)", "Today", lambda c: _p(c, "price_change_pct"), "pct", color_sign=True),

    # -- Gain / Loss ------------------------------------------------ #
    Metric("unrealized_usd", "Unrealized G/L $", "Gain/Loss", _unreal_usd, "money", color_sign=True),
    Metric("unrealized_pct", "Unrealized G/L %", "Gain/Loss",
           lambda c: _ratio(_unreal_usd(c), _p(c, "cost_basis")), "pct", color_sign=True),
    Metric("unrealized_csv_usd", "Unrealized G/L $ (CSV)", "Gain/Loss", _unreal_csv_usd, "money", color_sign=True),
    Metric("unrealized_csv_pct", "Unrealized G/L % (CSV)", "Gain/Loss",
           lambda c: _ratio(_unreal_csv_usd(c), _p(c, "cost_basis")), "pct", color_sign=True),

    # -- Allocation ---------------------------------------------- #
    Metric("pct_of_portfolio", "% of Portfolio", "Allocation",
           lambda c: _ratio(eff_mv(c), c.get("port_value")), "pct"),
    Metric("pct_of_account", "% of Account", "Allocation",
           lambda c: _ratio(eff_mv(c), c.get("acct_value")), "pct"),
    Metric("pct_of_account_csv", "% of Account (CSV)", "Allocation", lambda c: _p(c, "pct_of_account"), "pct"),

    # -- Income -------------------------------------------- #
    Metric("div_yield_pct", "Dividend Yield %", "Income", lambda c: _p(c, "div_yield_pct"), "pct"),
    Metric("div_pay_date", "Dividend Pay Date", "Income", lambda c: (c["pos"] or {}).get("div_pay_date"), "text"),
    Metric("reinvest", "Reinvest", "Income", _reinvest, "text"),
    Metric("next_earnings", "Next Earnings", "Income", lambda c: (c["pos"] or {}).get("next_earnings_date"), "text"),

    # -- From Yahoo daily bars (run sync_history.py / the Sync button) ----- #
    Metric("ma_20", "20-day MA", "Yahoo history", lambda c: _s(c, "ma_20"), "price"),
    Metric("ma_50", "50-day MA", "Yahoo history", lambda c: _s(c, "ma_50"), "price"),
    Metric("ma_200", "200-day MA", "Yahoo history", lambda c: _s(c, "ma_200"), "price"),
    Metric("price_vs_ma50", "Price vs 50-day MA %", "Yahoo history",
           lambda c: _ratio((eff_price(c) - _s(c, "ma_50"))
                            if (eff_price(c) is not None and _s(c, "ma_50")) else None,
                            _s(c, "ma_50")), "pct", color_sign=True),
    Metric("volume", "Volume (latest day)", "Yahoo history", lambda c: _s(c, "volume"), "int"),

    # -- From Yahoo fundamentals (security_info) -------------------------- #
    Metric("week52_high", "52-wk High", "Yahoo history", lambda c: _i(c, "week52_high"), "price"),
    Metric("week52_low", "52-wk Low", "Yahoo history", lambda c: _i(c, "week52_low"), "price"),
    Metric("pct_off_52wk_high", "% Off 52-wk High", "Yahoo history",
           lambda c: _ratio((eff_price(c) - _i(c, "week52_high"))
                            if (eff_price(c) is not None and _i(c, "week52_high")) else None,
                            _i(c, "week52_high")), "pct"),
    Metric("beta", "Beta", "Yahoo history", lambda c: _i(c, "beta"), "num"),
    Metric("pe_ttm", "P/E (TTM)", "Yahoo history", lambda c: _i(c, "trailing_pe"), "num"),
    Metric("pb_ratio", "P/B", "Yahoo history", lambda c: _i(c, "price_to_book"), "num"),
    Metric("market_cap", "Market Cap", "Yahoo history", lambda c: _i(c, "market_cap"), "money"),
    Metric("avg_volume", "Avg Volume", "Yahoo history", lambda c: _i(c, "avg_volume"), "int"),
    Metric("sector", "Sector", "Yahoo history", lambda c: (c.get("info") or {}).get("sector"), "text"),
]

BY_KEY = {m.key: m for m in METRICS}
BY_LABEL = {m.label: m for m in METRICS}
AVAILABLE = [m for m in METRICS if m.available]

DEFAULT_KEYS = [
    "account", "symbol", "description", "quantity", "price",
    "cost_basis", "market_value", "unrealized_usd", "unrealized_pct",
]


def value(key: str, ctx: dict):
    m = BY_KEY.get(key)
    if m is None:
        return None
    try:
        return m.fn(ctx)
    except Exception:
        return None


def groups(include_unavailable: bool = True):
    """Ordered {group: [Metric, ...]} for building the picker."""
    out: dict[str, list[Metric]] = {}
    for m in METRICS:
        if not include_unavailable and not m.available:
            continue
        out.setdefault(m.group, []).append(m)
    return out
