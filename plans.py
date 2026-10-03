"""Plans: one goal per account, progress toward it, and money in vs growth.

A plan is a goal (a target amount by a target date), how much is added each
month, and a target mix by asset type. It is written by the account owner or
their advisor (`set_by`). Everything else here is arithmetic on that plus
the account's imported snapshots and logged contributions - no network, no
Streamlit.

Projections are compound growth at an assumed yearly return, shown as a
range (the assumption plus and minus `SPREAD_PCT`), in today's dollars
without inflation. They illustrate what a plan implies; they don't predict.
"""

from __future__ import annotations

import json
from datetime import date

GOAL_TYPES = ("Retirement", "Buy a home", "Pay for education", "Build long-term wealth",
              "Big purchase", "Other")
DEFAULT_RETURN_PCT = 6.0
SPREAD_PCT = 2.0

_FIELDS = ("goal_type", "goal_name", "target_amount", "target_date", "monthly_contribution",
           "target_alloc", "notes")


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def get_plan(conn, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM plans WHERE user_id = ?", (user_id,)).fetchone()
    return _plan_from(row)


def get_plans(conn, user_ids) -> dict:
    """get_plan() for several accounts in one query: {user_id: plan or None}."""
    ids = tuple(dict.fromkeys(user_ids))
    if not ids:
        return {}
    rows = {r["user_id"]: r for r in conn.execute(
        f"SELECT * FROM plans WHERE user_id IN ({', '.join('?' for _ in ids)})", ids)}
    return {i: _plan_from(rows.get(i)) for i in ids}


def _plan_from(row) -> dict | None:
    if row is None:
        return None
    plan = dict(row)
    try:
        alloc = json.loads(plan.get("target_alloc") or "{}")
    except ValueError:
        alloc = {}
    plan["target_alloc"] = {k: float(v) for k, v in alloc.items() if v} if isinstance(alloc, dict) else {}
    return plan


def save_plan(conn, user_id: int, fields: dict, set_by: int) -> dict:
    """Merge `fields` into the account's plan (creating it) and return it.
    A key that's present with None clears that field."""
    plan = get_plan(conn, user_id) or {f: None for f in _FIELDS}
    plan.update({k: v for k, v in fields.items() if k in _FIELDS})
    alloc = {k: float(v) for k, v in (plan.get("target_alloc") or {}).items() if v}
    conn.execute(
        "INSERT INTO plans (user_id, goal_type, goal_name, target_amount, target_date, "
        "monthly_contribution, target_alloc, notes, set_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id) DO UPDATE SET goal_type = excluded.goal_type, "
        "goal_name = excluded.goal_name, target_amount = excluded.target_amount, "
        "target_date = excluded.target_date, monthly_contribution = excluded.monthly_contribution, "
        "target_alloc = excluded.target_alloc, notes = excluded.notes, set_by = excluded.set_by, "
        "updated_at = datetime('now')",
        (user_id, plan.get("goal_type"), plan.get("goal_name"), plan.get("target_amount"),
         plan.get("target_date"), plan.get("monthly_contribution"), json.dumps(alloc),
         plan.get("notes"), set_by))
    if "target_alloc" in fields:  # new targets replace any cleared by the class change
        conn.execute("UPDATE plans SET targets_cleared = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    return get_plan(conn, user_id)


def has_goal(plan: dict | None) -> bool:
    return bool(plan and plan.get("target_amount") and plan.get("target_date"))


def add_contribution(conn, user_id: int, on: str, amount: float, note: str | None = None) -> None:
    """Log money added (positive) or taken out (negative) on date `on`."""
    conn.execute("INSERT INTO contributions (user_id, date, amount, note) VALUES (?, ?, ?, ?)",
                 (user_id, on, float(amount), (note or "").strip() or None))
    conn.commit()


def delete_contribution(conn, user_id: int, contribution_id: int) -> None:
    conn.execute("DELETE FROM contributions WHERE id = ? AND user_id = ?", (contribution_id, user_id))
    conn.commit()


def list_contributions(conn, user_id: int, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, date, amount, note FROM contributions WHERE user_id = ? "
        "ORDER BY date DESC, id DESC LIMIT ?", (user_id, limit))]


# ---- money added: logged by hand, or from an imported activity export ------ #
def imported_window(conn, user_id: int) -> tuple[str, str] | None:
    """(first, last) date of the imported activity history (txn_import.py)."""
    row = conn.execute("SELECT MIN(trade_date) AS a, MAX(trade_date) AS b FROM transactions "
                       "WHERE user_id = ? AND origin = 'imported'", (user_id,)).fetchone()
    return (row["a"], row["b"]) if row and row["a"] else None


def money_moves(conn, user_id: int, start: str | None = None, end: str | None = None,
                *, window=False) -> list[dict]:
    """Money added (+) or taken out (-), newest first: hand-logged entries
    and the deposits and withdrawals in imported activity history.
    [{"id", "date", "amount", "note", "source": "hand" | "brokerage",
    "counted"}]. A hand entry dated inside the imported history isn't
    counted - the history already has the real figure (moves between your
    own accounts and money-market sweeps are never money added).
    `window`: imported_window() if the caller already read it."""
    start, end = start or "0000-00-00", end or "9999-99-99"
    if window is False:
        window = imported_window(conn, user_id)
    out = [{"id": r["id"], "date": r["date"], "amount": float(r["amount"]), "note": r["note"],
            "source": "hand",
            "counted": not (window and window[0] <= r["date"] <= window[1])}
           for r in conn.execute("SELECT id, date, amount, note FROM contributions WHERE "
                                 "user_id = ? AND date >= ? AND date <= ?", (user_id, start, end))]
    out += [{"id": r["id"], "date": r["trade_date"], "amount": float(r["amount"]),
             "note": r["description"], "source": "brokerage", "counted": True}
            for r in conn.execute(
                "SELECT id, trade_date, amount, description FROM transactions WHERE user_id = ? "
                "AND origin = 'imported' AND action IN ('DEPOSIT', 'WITHDRAWAL') AND amount IS "
                "NOT NULL AND trade_date >= ? AND trade_date <= ?", (user_id, start, end))]
    out.sort(key=lambda m: (m["date"], m["source"] == "brokerage", m["id"]), reverse=True)
    return out


def money_added(conn, user_id: int, start: str, end: str) -> float:
    """Net money added between two dates (YYYY-MM-DD, inclusive)."""
    return round(sum(m["amount"] for m in money_moves(conn, user_id, start, end)
                     if m["counted"]), 2)


def month_total(conn, user_id: int, year: int, month: int, moves=None) -> float:
    """Net money added in that month. `moves`: every money_moves() already
    read - that month's are picked from them, nothing is read again."""
    start, end = f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-31"
    if moves is not None:
        return round(sum(m["amount"] for m in moves
                         if m["counted"] and start <= m["date"] <= end), 2)
    return money_added(conn, user_id, start, end)


def money_in_history(conn, user_id: int) -> list[dict]:
    """Per imported snapshot, from the statement's own figures: total value,
    growth (market value minus cost, over positions with a known cost), and
    money in (the rest: cost basis plus cash). Oldest first."""
    rows = {r["snapshot_date"]: {"date": r["snapshot_date"], "holdings": float(r["mv"] or 0.0),
                                 "growth": float(r["gl"] or 0.0)}
            for r in conn.execute(
                "SELECT snapshot_date, SUM(market_value) AS mv, "
                "SUM(CASE WHEN cost_basis IS NOT NULL AND market_value IS NOT NULL "
                "THEN market_value - cost_basis ELSE 0 END) AS gl "
                "FROM positions WHERE user_id = ? GROUP BY snapshot_date", (user_id,))}
    for r in conn.execute("SELECT snapshot_date, SUM(cash_value) AS cash FROM account_totals "
                          "WHERE user_id = ? GROUP BY snapshot_date", (user_id,)):
        rows.setdefault(r["snapshot_date"], {"date": r["snapshot_date"], "holdings": 0.0,
                                             "growth": 0.0})["cash"] = float(r["cash"] or 0.0)
    out = []
    for d in sorted(rows):
        r = rows[d]
        value = r["holdings"] + r.get("cash", 0.0)
        out.append({"date": d, "value": value, "growth": r["growth"], "money_in": value - r["growth"]})
    return out


# --------------------------------------------------------------------------- #
# arithmetic
# --------------------------------------------------------------------------- #
def months_until(target_date: str, today: date) -> int:
    """Whole months from `today` to `target_date` (YYYY-MM-DD); negative if past."""
    t = date.fromisoformat(str(target_date)[:10])
    return (t.year - today.year) * 12 + (t.month - today.month) - (1 if t.day < today.day else 0)


def _monthly_rate(annual_pct: float) -> float:
    return (1 + annual_pct / 100) ** (1 / 12) - 1


def future_value(present: float, monthly: float, annual_pct: float, months: int) -> float:
    """`present` grown for `months` at `annual_pct` a year, plus `monthly`
    added at the end of each month."""
    months = max(0, months)
    r = _monthly_rate(annual_pct)
    if r == 0:
        return present + monthly * months
    g = (1 + r) ** months
    return present * g + monthly * (g - 1) / r


def required_monthly(present: float, target: float, annual_pct: float, months: int) -> float | None:
    """Monthly amount that reaches `target` in `months` at `annual_pct`;
    0 if growth alone gets there, None when there's no time left."""
    if months <= 0:
        return None
    r = _monthly_rate(annual_pct)
    g = (1 + r) ** months
    gap = target - present * g
    if gap <= 0:
        return 0.0
    return gap / months if r == 0 else gap * r / (g - 1)


def months_to_reach(present: float, monthly: float, annual_pct: float, target: float, *,
                    cap: int = 600) -> int | None:
    """Whole months until `present` plus `monthly` reaches `target` at
    `annual_pct`; 0 if already there, None if not within `cap` months."""
    if present >= target:
        return 0
    r = _monthly_rate(annual_pct)
    value = present
    for n in range(1, cap + 1):
        value = value * (1 + r) + monthly
        if value >= target:
            return n
    return None


# The "what if" playground's return for a mix (ROADMAP G5): the same rounded
# long-run assumptions as advisor proposals (proposals.ASSUMED_RETURN).
STOCK_RETURN_PCT, BOND_RETURN_PCT = 7.0, 4.0


def mix_return(stocks_pct: float) -> float:
    """Assumed yearly return for a mix of `stocks_pct` stocks, the rest bonds."""
    s = max(0.0, min(100.0, stocks_pct)) / 100
    return STOCK_RETURN_PCT * s + BOND_RETURN_PCT * (1 - s)


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


def progress(plan: dict, current_value: float, *, today: date,
             return_pct: float = DEFAULT_RETURN_PCT, spread: float = SPREAD_PCT) -> dict:
    """Where the account stands against its goal.

    status: 'reached' (already at or past the target), 'starting' (nothing
    invested yet - day one isn't "behind"; needed_monthly is what gets
    there), 'on_track' (the assumed return gets there), 'within_reach' (only
    the optimistic end does), 'behind', or 'past_date' (the date has passed
    short of it)."""
    target = float(plan["target_amount"])
    monthly = float(plan.get("monthly_contribution") or 0.0)
    months = months_until(plan["target_date"], today)
    lo, mid, hi = (future_value(current_value, monthly, p, months)
                   for p in (return_pct - spread, return_pct, return_pct + spread))
    if current_value >= target:
        status = "reached"
    elif months <= 0:
        status = "past_date"
    elif current_value <= 0:
        status = "starting"
    elif mid >= target:
        status = "on_track"
    elif hi >= target:
        status = "within_reach"
    else:
        status = "behind"
    return {
        "target": target, "current": current_value, "monthly": monthly, "months": months,
        "pct_of_target": (current_value / target * 100) if target else None,
        "projected_low": lo, "projected": mid, "projected_high": hi,
        "needed_monthly": required_monthly(current_value, target, return_pct, months),
        "status": status,
    }


def projection_series(current_value: float, monthly: float, months: int, *, today: date,
                      return_pct: float = DEFAULT_RETURN_PCT, spread: float = SPREAD_PCT) -> list[dict]:
    """One row per month from today to the goal date: the low / assumed / high
    projected value. At most ~240 rows (long horizons are sampled)."""
    months = max(0, months)
    step = max(1, -(-months // 240))  # ceiling, so at most ~240 points
    points = list(range(0, months + 1, step))
    if points[-1] != months:
        points.append(months)
    return [{"date": add_months(today, n).isoformat(),
             "low": future_value(current_value, monthly, return_pct - spread, n),
             "mid": future_value(current_value, monthly, return_pct, n),
             "high": future_value(current_value, monthly, return_pct + spread, n)}
            for n in points]


# --------------------------------------------------------------------------- #
# what the portfolio could pay each year (the Plan's retirement view)
# --------------------------------------------------------------------------- #
# Rules of thumb for a yearly withdrawal, as a share of today's value. 4% is
# the best-known one, from studies of past US markets over 30-year
# retirements; 3-5% is the range planners usually talk about. Illustrations,
# never advice: nothing here says what anyone should take out.
WITHDRAWAL_RATES = (3.0, 4.0, 5.0)
# The one stated growth rate for "how long it lasts": modest on purpose,
# before inflation, fees and taxes.
LASTS_GROWTH_PCT = 4.0
LASTS_CAP_YEARS = 60
# A goal date this close (or a profile that says so) puts the view first.
NEAR_YEARS = 10
_RETIRED_ANSWERS = {"age_range": ("65 or older",),
                    "income_stability": ("Not working or retired",),
                    "contributions": ("Withdrawing regularly",)}
_INCOME_GOALS = ("Retirement", "Generate income")


def withdrawals(value: float, rates=WITHDRAWAL_RATES) -> list[dict]:
    """[{"rate", "yearly", "monthly"}]: `rate`% of `value` a year."""
    value = max(0.0, float(value or 0.0))
    return [{"rate": float(r), "yearly": value * r / 100, "monthly": value * r / 100 / 12}
            for r in rates]


def months_lasting(present: float, yearly: float, growth_pct: float = LASTS_GROWTH_PCT, *,
                   cap_years: int = LASTS_CAP_YEARS) -> int | None:
    """Whole months `present` keeps paying `yearly` (a twelfth at the start
    of each month), the rest growing at `growth_pct` a year. None when it's
    still paying after `cap_years` (or nothing is taken out); 0 when there
    isn't a first month's amount."""
    present, yearly = float(present or 0.0), float(yearly or 0.0)
    if yearly <= 0:
        return None
    take, r, value = yearly / 12, _monthly_rate(growth_pct), present
    for n in range(cap_years * 12):
        if value + 1e-9 < take:
            return n
        value = (value - take) * (1 + r)
    return None


def retirement_first(plan: dict | None, profile: dict | None, today: date) -> bool:
    """Whether the Plan leads with what the portfolio could pay each year:
    for someone retired or about to be - an answer that says so (65 or
    older, not working or retired, withdrawing regularly), or a retirement
    or income goal that isn't known to be more than NEAR_YEARS away (the
    plan's goal date, else the profile's time horizon)."""
    p = profile or {}
    if any(p.get(k) in v for k, v in _RETIRED_ANSWERS.items()):
        return True
    goals = {g.strip() for g in str(p.get("goal") or "").split(";")}
    if (plan or {}).get("goal_type"):
        goals.add(plan["goal_type"])
    if not goals & set(_INCOME_GOALS):
        return False
    if has_goal(plan) and plan.get("goal_type") in _INCOME_GOALS:
        return months_until(plan["target_date"], today) <= NEAR_YEARS * 12
    try:
        years = float(p.get("time_horizon_years"))
    except (TypeError, ValueError):
        return True     # a retirement goal, with no timeline to say it's far off
    return years <= NEAR_YEARS
