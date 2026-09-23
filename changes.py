"""Position-set diffing and transaction synthesis for the CSV re-upload flow.

Pure functions, standard library only. Given the positions already in the DB
(latest snapshot) and the positions parsed from a freshly uploaded CSV, work out
what changed, and turn those changes into rows for the `transactions` table.

The Schwab Positions export carries no trade history, so the transactions we
write here are *inferred* from the change in each holding between two snapshots:
a bigger quantity is a BUY, a smaller one is a SELL, a vanished holding is a full
SELL. Prices/amounts are estimates from the reported cost basis and market value,
not broker trade confirmations.
"""

from __future__ import annotations

QTY_EPS = 1e-6  # quantities within this are "the same" (guards float noise)


def _num(row, key):
    v = row.get(key)
    return None if v is None else float(v)


def _entry(account, symbol, old, new):
    o = old or {}
    n = new or {}
    return {
        "account": account,
        "symbol": symbol,
        "description": (n.get("description") or (o.get("description") if o else None)),
        "old_qty": _num(o, "quantity"),
        "new_qty": _num(n, "quantity"),
        "dqty": (_num(n, "quantity") or 0.0) - (_num(o, "quantity") or 0.0),
        "old_cost": _num(o, "cost_basis"),
        "new_cost": _num(n, "cost_basis"),
        "old_mv": _num(o, "market_value"),
        "new_mv": _num(n, "market_value"),
    }


def diff_positions(old_rows, new_rows):
    """Compare two position lists, keyed on (account, symbol).

    Returns {"new", "increased", "decreased", "closed", "unchanged"}: lists of
    entry dicts (see `_entry`). "increased"/"decreased" mean the quantity moved.
    """
    old = {(r["account"], r["symbol"]): r for r in old_rows}
    new = {(r["account"], r["symbol"]): r for r in new_rows}

    out = {"new": [], "increased": [], "decreased": [], "closed": [], "unchanged": []}

    for key, n in new.items():
        acct, sym = key
        if key not in old:
            out["new"].append(_entry(acct, sym, None, n))
            continue
        e = _entry(acct, sym, old[key], n)
        if abs(e["dqty"]) <= QTY_EPS:
            out["unchanged"].append(e)
        elif e["dqty"] > 0:
            out["increased"].append(e)
        else:
            out["decreased"].append(e)

    for key, o in old.items():
        if key not in new:
            acct, sym = key
            out["closed"].append(_entry(acct, sym, o, None))

    return out


def _price(mv, qty):
    if mv is None or not qty:
        return None
    return round(mv / qty, 4)


def synthesize_transactions(diff, trade_date, source_file):
    """Turn a `diff_positions` result into `transactions` rows.

    Each row: account, trade_date, action (BUY/SELL), symbol, description,
    quantity (always positive), price (per share, estimated), amount (signed cash
    impact: negative for BUY, positive for SELL), fees (None), source_file.
    """
    txns = []

    for e in diff["new"]:
        qty = e["new_qty"] or 0.0
        price = _price(e["new_mv"], qty)
        amount = -e["new_cost"] if e["new_cost"] is not None else (
            -(price * qty) if price is not None else None)
        txns.append(_txn(e, trade_date, "BUY", qty, price, amount, source_file))

    for e in diff["increased"]:
        dq = e["dqty"]
        dcost = None
        if e["new_cost"] is not None and e["old_cost"] is not None:
            dcost = round(e["new_cost"] - e["old_cost"], 2)
        price = round(dcost / dq, 4) if (dcost and dq) else _price(e["new_mv"], e["new_qty"])
        amount = -dcost if dcost is not None else (
            -(price * dq) if price is not None else None)
        txns.append(_txn(e, trade_date, "BUY", dq, price, amount, source_file))

    for e in diff["decreased"]:
        sold = -e["dqty"]
        price = _price(e["new_mv"], e["new_qty"]) or _price(e["old_mv"], e["old_qty"])
        amount = round(price * sold, 2) if price is not None else None
        # Average-cost method: the cost basis attributed to the sold shares is
        # the same proportion of the pre-sale total cost basis as the sold
        # quantity is of the pre-sale total quantity (no per-lot data in a
        # Schwab Positions export to do FIFO/specific-lot instead).
        realized_gain = None
        if amount is not None and e["old_cost"] is not None and e["old_qty"]:
            cost_removed = e["old_cost"] * (sold / e["old_qty"])
            realized_gain = round(amount - cost_removed, 2)
        txns.append(_txn(e, trade_date, "SELL", sold, price, amount, source_file, realized_gain))

    for e in diff["closed"]:
        qty = e["old_qty"] or 0.0
        price = _price(e["old_mv"], qty)
        amount = e["old_mv"] if e["old_mv"] is not None else (
            round(price * qty, 2) if price is not None else None)
        realized_gain = None
        if amount is not None and e["old_cost"] is not None:
            realized_gain = round(amount - e["old_cost"], 2)
        txns.append(_txn(e, trade_date, "SELL", qty, price, amount, source_file, realized_gain))

    return txns


def _txn(entry, trade_date, action, qty, price, amount, source_file, realized_gain=None):
    return {
        "account": entry["account"],
        "trade_date": trade_date,
        "action": action,
        "symbol": entry["symbol"],
        "description": entry["description"],
        "quantity": round(qty, 6) if qty is not None else None,
        "price": price,
        "amount": round(amount, 2) if amount is not None else None,
        "fees": None,
        "realized_gain": realized_gain,
        "source_file": source_file,
    }
