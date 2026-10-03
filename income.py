"""Estimated dividend income by month, for the next 12 months.

Each holding is assumed to pay in the same months as it did over the past
year, the same amount per share, times the shares held now. The past
payments come from Yahoo's daily history (daily_bars.dividend - the amount
per share on each ex-dividend date, saved by sync_history.py). A holding with
a dividend yield but no payment history yet is spread evenly over the year,
and said so.

Months are ex-dividend months; the money usually arrives a few weeks later.
An estimate from the past, not a promise: companies change dividends.

Yield on cost (yield_on_cost) puts a year's dividends against what was paid
for the shares rather than what they are worth today.
"""

from __future__ import annotations

from datetime import date, timedelta


def _month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def next_months(today: date, n: int = 12) -> list[str]:
    """'YYYY-MM' for this month and the n-1 after it."""
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def payments(conn, tickers, today: date) -> dict:
    """{ticker: [(ex_date, amount_per_share)]} over the last 12 months."""
    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    since = (today - timedelta(days=365)).isoformat()
    ph = ", ".join("?" for _ in tickers)
    out: dict = {t: [] for t in tickers}
    for r in conn.execute(
            f"SELECT ticker, date, dividend FROM daily_bars WHERE dividend > 0 AND date > ? "
            f"AND ticker IN ({ph}) ORDER BY date", (since, *tickers)):
        out[r["ticker"]].append((date.fromisoformat(r["date"][:10]), float(r["dividend"])))
    return out


def has_history(conn, tickers) -> set:
    """The tickers whose daily bars were synced with dividends (any value,
    even 0) - so "no payments" can be told apart from "not synced yet"."""
    tickers = sorted(set(tickers))
    if not tickers:
        return set()
    ph = ", ".join("?" for _ in tickers)
    return {r["ticker"] for r in conn.execute(
        f"SELECT DISTINCT ticker FROM daily_bars WHERE dividend IS NOT NULL AND ticker IN ({ph})",
        tuple(tickers))}


def schedule(holdings, paid: dict, synced: set, today: date) -> dict:
    """The next 12 months of estimated income.

    holdings: [{"symbol", "quantity", "annual"}] - `annual` is the yield-based
    yearly estimate, used only for a holding with no payment history.
    Returns {"months": [{"month": "YYYY-MM", "total", "by_symbol": {sym: amount}}],
    "total", "spread": [symbols spread evenly], "none": [synced, no payments]}."""
    months = next_months(today)
    by_month = {m: {} for m in months}
    spread, none = [], []
    for h in holdings:
        sym, qty = h["symbol"], h.get("quantity") or 0.0
        past = paid.get(sym) or []
        if past and qty:
            for ex, per_share in past:
                # the same month next time round
                nxt = _month(date(ex.year + 1, ex.month, 1))
                key = nxt if nxt in by_month else _month(date(ex.year, ex.month, 1))
                if key in by_month:
                    by_month[key][sym] = by_month[key].get(sym, 0.0) + per_share * qty
        elif h.get("annual"):
            spread.append(sym)
            for m in months:
                by_month[m][sym] = by_month[m].get(sym, 0.0) + h["annual"] / 12
        elif sym in synced:
            none.append(sym)
    rows = [{"month": m, "by_symbol": by_month[m], "total": round(sum(by_month[m].values()), 2)}
            for m in months]
    return {"months": rows, "total": round(sum(r["total"] for r in rows), 2),
            "spread": sorted(spread), "none": sorted(none)}


def past_months(today: date, n: int = 12) -> list[str]:
    """'YYYY-MM' for the n months ending with this one, oldest first."""
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m < 1:
            y, m = y - 1, 12
    return out[::-1]


def received(conn, user_id: int, today: date) -> dict | None:
    """Dividends and interest actually paid in the last 12 months, from an
    imported activity export (txn_import.py) - None when none was imported.
    {"months": [{"month", "total", "by_symbol"}], "dividends", "interest",
    "cash_interest" (the interest with no symbol: on cash, not a holding),
    "total", "since": the first imported date, if inside the 12 months}."""
    first = conn.execute("SELECT MIN(trade_date) AS d FROM transactions WHERE user_id = ? AND "
                         "origin = 'imported'", (user_id,)).fetchone()["d"]
    if not first:
        return None
    months = past_months(today)
    by_month = {m: {} for m in months}
    divs = interest = cash_interest = 0.0
    for r in conn.execute(
            "SELECT trade_date, action, symbol, amount FROM transactions WHERE user_id = ? AND "
            "origin = 'imported' AND action IN ('DIV', 'INTEREST') AND amount > 0 AND "
            "trade_date >= ?", (user_id, months[0] + "-01")):
        m = r["trade_date"][:7]
        if m not in by_month:
            continue
        key = r["symbol"] or ("Interest" if r["action"] == "INTEREST" else "Other")
        by_month[m][key] = by_month[m].get(key, 0.0) + r["amount"]
        if r["action"] == "DIV":
            divs += r["amount"]
        else:
            interest += r["amount"]
            if not r["symbol"]:
                cash_interest += r["amount"]
    rows = [{"month": m, "by_symbol": by_month[m], "total": round(sum(by_month[m].values()), 2)}
            for m in months]
    return {"months": rows, "dividends": round(divs, 2), "interest": round(interest, 2),
            "cash_interest": round(cash_interest, 2),
            "total": round(divs + interest, 2),
            "since": first if first > months[0] + "-01" else None}


def yield_on_cost(annual_income, cost_basis) -> float | None:
    """A year's dividends as a share of what was paid for the shares, in
    percent ("yield on cost"): 41 a year on 1,000 paid is 4.1. None when
    the cost isn't known (or isn't above 0) - nothing is shown then."""
    try:
        income_, cost = float(annual_income), float(cost_basis)
    except (TypeError, ValueError):
        return None
    if cost != cost or income_ != income_ or cost <= 0 or income_ < 0:
        return None
    return income_ / cost * 100


def yield_on_cost_total(rows) -> float | None:
    """yield_on_cost() across holdings [{"est_income", "cost"}], counting only
    those whose cost is known; None if none is."""
    known = [r for r in rows or [] if yield_on_cost(r.get("est_income"), r.get("cost")) is not None]
    if not known:
        return None
    return yield_on_cost(sum(r["est_income"] for r in known), sum(r["cost"] for r in known))


# ---- total return: price change plus the dividends received --------------- #
# A holding's gain on screen is its price change (market value minus cost).
# Its total return adds the dividends it paid while held. They come from:
#   "brokerage" - the imported activity history (txn_import.py): what was
#                 actually paid, DIV and INTEREST rows with the holding's
#                 symbol (reinvested ones included); a symbol with only
#                 REINVEST rows counts what was reinvested;
#   "estimated" - otherwise, Yahoo's dividends per share (daily_bars, saved
#                 by sync_history.py) times the shares held on each ex-date,
#                 from the holdings updates saved here (positions): only
#                 since the holding first shows in them, so never before
#                 the app knew it was held.
# A holding with neither (or nothing paid) has no figure: price only.
def imported_payouts(conn, user_id: int, symbols) -> dict:
    """{symbol: {"paid": DIV + INTEREST, "reinvested": REINVEST (positive),
    "first": the earliest such date}} from the imported activity history."""
    symbols = sorted(set(symbols))
    if not symbols:
        return {}
    ph = ", ".join("?" for _ in symbols)
    out: dict = {}
    for r in conn.execute(
            f"SELECT symbol, action, trade_date, amount FROM transactions WHERE user_id = ? AND "
            f"origin = 'imported' AND action IN ('DIV', 'INTEREST', 'REINVEST') AND "
            f"amount IS NOT NULL AND symbol IN ({ph})", (user_id, *symbols)):
        o = out.setdefault(r["symbol"], {"paid": 0.0, "reinvested": 0.0, "first": None})
        amount = float(r["amount"])
        if r["action"] == "REINVEST":
            o["reinvested"] += abs(amount)
        elif amount > 0:
            o["paid"] += amount
        d = str(r["trade_date"] or "")[:10]
        if d and (o["first"] is None or d < o["first"]):
            o["first"] = d
    return out


def holding_history(conn, user_id: int, skip_sources=()) -> dict:
    """{snapshot_date: {symbol: shares}} for every holdings update saved,
    oldest first - all symbols, so a date without one shows it wasn't held.
    `skip_sources`: positions.source_file values left out (the example
    portfolio, a percentages portfolio's pretend shares)."""
    skip = tuple(skip_sources)
    sql = ("SELECT snapshot_date, symbol, SUM(quantity) AS q FROM positions WHERE user_id = ?"
           + (f" AND (source_file IS NULL OR source_file NOT IN ({', '.join('?' for _ in skip)}))"
              if skip else "")
           + " GROUP BY snapshot_date, symbol")
    out: dict = {}
    for r in conn.execute(sql, (user_id, *skip)):
        out.setdefault(str(r["snapshot_date"])[:10], {})[r["symbol"]] = float(r["q"] or 0.0)
    return dict(sorted(out.items()))


def payments_since(conn, tickers, since: str) -> dict:
    """{ticker: [(ex_date, amount_per_share)]} on ex-dates after `since`."""
    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    ph = ", ".join("?" for _ in tickers)
    out: dict = {t: [] for t in tickers}
    for r in conn.execute(
            f"SELECT ticker, date, dividend FROM daily_bars WHERE dividend > 0 AND date > ? "
            f"AND ticker IN ({ph}) ORDER BY date", (since, *tickers)):
        out[r["ticker"]].append((str(r["date"])[:10], float(r["dividend"])))
    return out


def held_since(history: dict, symbol: str) -> str | None:
    """The first holdings update of the symbol's current stretch: walking
    back from the latest update while it's still held. None if the latest
    update doesn't hold it."""
    since = None
    for d in sorted(history, reverse=True):
        if (history[d].get(symbol) or 0.0) > 0:
            since = d
        else:
            break
    return since


def estimate_received(history: dict, symbol: str, paid: list,
                      today: date) -> tuple[float, str | None]:
    """(dividends, since): Yahoo's per-share payments (`paid`, [(ex_date,
    per_share)]) times the shares held the day before each ex-date - the
    latest update before it - for ex-dates after the holding first shows
    (held_since) up to `today`. A payment on the first update's own date
    isn't counted: the shares could have been bought that day."""
    since = held_since(history, symbol)
    if since is None:
        return 0.0, None
    dates = sorted(d for d in history if d >= since)
    total = 0.0
    for ex, per_share in paid:
        ex = str(ex)[:10]
        if not since < ex <= today.isoformat():
            continue
        before = [d for d in dates if d < ex]
        if before:
            total += per_share * (history[before[-1]].get(symbol) or 0.0)
    return round(total, 2), since


def received_while_held(conn, user_id: int, symbols, today: date, *, skip_sources=()) -> dict:
    """The dividends each held symbol paid: {symbol: {"amount", "source":
    "brokerage" | "estimated", "since"}} - only symbols with something paid.
    Brokerage figures win; the rest are estimated (see above). At most three
    reads, one when the imported history covers every symbol."""
    symbols = sorted(set(symbols))
    if not symbols:
        return {}
    out = {}
    for sym, o in imported_payouts(conn, user_id, symbols).items():
        amount = o["paid"] or o["reinvested"]
        if amount > 0:
            out[sym] = {"amount": round(amount, 2), "source": "brokerage", "since": o["first"]}
    rest = [s for s in symbols if s not in out]
    if not rest:
        return out
    history = holding_history(conn, user_id, skip_sources)
    starts = {s: held_since(history, s) for s in rest}
    starts = {s: d for s, d in starts.items() if d}
    if not starts:
        return out
    paid = payments_since(conn, starts, min(starts.values()))
    for sym in sorted(starts):
        amount, since = estimate_received(history, sym, paid.get(sym) or [], today)
        if amount > 0:
            out[sym] = {"amount": amount, "source": "estimated", "since": since}
    return out


def split_by_holding(positions, by_symbol: dict) -> list:
    """Each position's share of its symbol's dividends (by shares, when one
    symbol is held in several accounts): a list matching `positions`, None
    for a holding with none."""
    shares: dict = {}
    for p in positions:
        shares[p["symbol"]] = shares.get(p["symbol"], 0.0) + (p.get("quantity") or 0.0)
    out = []
    for p in positions:
        d = by_symbol.get(p["symbol"])
        total = shares.get(p["symbol"]) or 0.0
        part = (p.get("quantity") or 0.0) / total if (d and total) else 0.0
        out.append(round(d["amount"] * part, 2) if part else None)
    return out


def total_return(gain, cost, dividends) -> dict | None:
    """Price change plus dividends: {"usd", "pct" (of cost; None without
    one), "dividends"} - None when there are no dividends to add (or no gain
    to add them to), so the price change shows alone, as before."""
    if gain is None or not dividends:
        return None
    usd = gain + dividends
    return {"usd": usd, "pct": (usd / cost * 100) if cost else None, "dividends": dividends}


def source_words(sources) -> str:
    """Where the dividend figure comes from, for a caption."""
    sources = set(sources)
    if sources == {"brokerage"}:
        return "Dividends are from your brokerage's activity history that you imported."
    if sources == {"estimated"}:
        return ("Dividends are estimated from what each fund paid per share while you held it "
                "here - since it first shows in your holdings.")
    return ("Dividends are from your brokerage's activity history where you've imported it, "
            "otherwise estimated from what each fund paid per share while you held it here.")


def yearly_income(schedule_total: float, got: dict | None) -> dict:
    """What the portfolio pays in a year, for the Plan's retirement view:
    {"dividends": the next-12-months estimate (schedule()), "interest": cash
    interest your brokerage paid in the last 12 months (received(); 0 without
    an imported history), "total"}."""
    interest = float((got or {}).get("cash_interest") or 0.0)
    dividends = float(schedule_total or 0.0)
    return {"dividends": round(dividends, 2), "interest": round(interest, 2),
            "total": round(dividends + interest, 2)}
