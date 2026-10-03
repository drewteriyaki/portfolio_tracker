"""Activity (transaction history) exports from any brokerage - ROADMAP "Real
transactions", phases 1-2. Pure logic, no Streamlit.

A Positions export says what you hold today; an activity export says what
happened: buys, sells, dividends, deposits. csv_import.find_header() spots
one ("transactions") and the import dialog brings it here instead of turning
it away. This reader:

1. finds the activity table (a date column and an action or amount column);
2. matches its columns by name (FIELDS, best name first), a remembered
   layout, or - only on a button - the AI, which sees column names and cell
   kinds, never values;
3. reads each row into the app's own kinds (TYPES) from the broker's wording
   ("YOU BOUGHT", "Qualified Dividend", "CDIV", "Reinvest Shares", ...);
   money-market sweeps and moves between your own accounts aren't deposits;
4. saves into `transactions` with origin 'imported' and a row_key, so a
   second, overlapping export adds only what's new. For each account, the
   imported history replaces rows the app worked out from holdings updates
   up to its last date (and later updates don't add worked-out rows inside
   it - see covered() / drop_covered());
5. works out each imported sale's realized gain by average cost, replaying
   the history in date order (replay_gains) - unknown where the history
   doesn't reach back to when the shares were bought. income.received()
   reads the dividends and interest paid.

Account numbers are cut to their last 3 digits; long digit runs in
descriptions (bank account numbers on transfers) are masked; the file
itself is never kept (portfolio.temp_upload).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime

from accounts import mask_number
from csv_import import _is_ticker, _norm, _num, read_rows, shape, signature  # noqa: F401

ORIGIN = "imported"

# field -> header names (normalized by csv_import._norm), best first: a file
# with both "Trade Date" and "Settlement Date" uses the trade date
FIELDS = {
    "date": ("trade date", "run date", "activity date", "transaction date", "date",
             "process date", "settlement date", "settle date"),
    "action": ("action", "transaction type", "trans code", "activity type", "activity",
               "transaction", "type"),
    "symbol": ("symbol", "ticker", "instrument", "symbol/cusip", "security symbol",
               "symbol / cusip"),
    "quantity": ("quantity", "qty", "shares", "share quantity", "units", "quantity/shares"),
    "price": ("price", "share price", "price per share", "execution price", "unit price"),
    "amount": ("amount", "net amount", "total amount", "net cash", "cash amount",
               "principal amount", "value"),
    "fees": ("fees & comm", "fees and commissions", "commissions and fees", "fees", "fee",
             "fees & commissions"),
    "commission": ("commission", "commissions"),
    "account": ("account", "account name", "acct", "account nickname"),
    "account_number": ("account number", "account #", "account no", "acct number"),
    # the security's name first; a "transaction description" ("Dividend
    # Received") only when that's all there is - the action says the same
    "description": ("description", "investment name", "security description", "security name",
                    "name", "details", "transaction description"),
}
LABELS = {"date": "Date", "action": "Action / type", "symbol": "Symbol",
          "quantity": "Shares", "price": "Price", "amount": "Amount", "fees": "Fees",
          "commission": "Commission", "account": "Account", "account_number": "Account number",
          "description": "Description"}

# the app's own kinds, and how they read
TYPES = {"BUY": "Buy", "SELL": "Sell", "DIV": "Dividend", "REINVEST": "Reinvest",
         "INTEREST": "Interest", "DEPOSIT": "Deposit", "WITHDRAWAL": "Withdrawal",
         "FEE": "Fee or tax", "TRANSFER": "Transfer", "SPLIT": "Split", "OTHER": "Other"}

# Broker wording -> kind, first match wins. Checked against the action and
# description together, lower-case, as whole words where it matters.
_RULES = [
    ("OTHER", r"\bsweep|\bcore (?:fund|position|account)|money ?market (?:purchase|redemption)"),
    ("FEE", r"margin int"),
    ("REINVEST", r"reinvest(?:ment)? shares|\breinvestment\b|\breinvest(?:ed)? (?:share|buy)"),
    ("DIV", r"\bdiv(?:idend)?s?\b|\bcdiv\b|\bqual(?:ified)? div|cap(?:ital)? gain|"
            r"\blt cap|\bst cap|\breinvest(?:ed)? div"),
    ("INTEREST", r"\binterest\b|\bint\b|\bcredit interest|\bbank int"),
    ("SPLIT", r"\bsplit\b|\bspl\b|stock dividend"),
    ("BUY", r"\byou bought\b|\bbought\b|\bbuy\b|\bpurchase\b|\bbto\b"),
    ("SELL", r"\byou sold\b|\bsold\b|\bsell\b|\bredemption\b|\bsto\b|\bstc\b"),
    ("FEE", r"\bfee\b|\bfees\b|\btax\b|withholding|\badr\b|\bgold\b"),
    ("TRANSFER", r"\bjournal\b|\bjnl\b|\bacats?\b|transfer of (?:security|assets)|"
                 r"\binternal transfer\b|\bto your account\b|\bfrom your account\b"),
    ("DEPOSIT", r"\bdeposit|\bach\b|\bmoneylink\b|\bwire\b|\bcontribution|\beft\b|"
                r"electronic funds|funds received|check received|\brtp\b|\bdcf\b"),
    ("WITHDRAWAL", r"withdraw|disbursement|\bdistribution\b|\bcheck paid\b|funds sent"),
]
_RULES = [(k, re.compile(rx, re.I)) for k, rx in _RULES]
# cash moving in or out: the sign of the amount decides which
_CASH_KINDS = {"DEPOSIT", "WITHDRAWAL"}

_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y",
                 "%B %d, %Y", "%Y/%m/%d")
# 7+ digits (spaces allowed): an account number, not a date like 080126
_DIGITS_RE = re.compile(r"(?<![\d.])\d[\d ]{5,}\d(?![\d.])")


# ---- finding and mapping the columns --------------------------------------- #
def _field(cell) -> str | None:
    c = _norm(cell)
    for field, names in FIELDS.items():
        if c in names:
            return field
    return None


def auto_mapping(header: list[str]) -> dict:
    """{field: column index}, taking each field's best-named column."""
    normed = [_norm(c) for c in header]
    out = {}
    for field, names in FIELDS.items():
        for name in names:
            if name in normed and normed.index(name) not in out.values():
                out[field] = normed.index(name)
                break
    return out


def usable(mapping: dict) -> bool:
    return "date" in mapping and bool({"action", "amount"} & set(mapping))


def find_header(rows) -> int | None:
    """The activity table's header row: names a date column and an action
    or amount column."""
    for i, row in enumerate(rows):
        if usable(auto_mapping(row)):
            return i
    return None


def remembered(conn, header) -> dict | None:
    row = conn.execute("SELECT mapping FROM csv_layouts WHERE signature = ?",
                       (signature(header),)).fetchone()
    if not row:
        return None
    try:
        m = json.loads(row["mapping"])
    except ValueError:
        return None
    m = {f: v for f, v in m.items() if f in FIELDS and isinstance(v, int)
         and 0 <= v < len(header)}
    return m if usable(m) else None


AI_PROMPT = """A brokerage activity (transaction history) export has these column names
(JSON array): {header}
and the first rows look like this (each cell replaced by its kind - the values are
not shown): {shapes}
Which column index (0-based) holds each field? Fields: date (trade date), action (what
happened: bought, sold, dividend...), symbol (ticker), quantity (shares), price (per
share), amount (the cash amount of the row), fees, commission, account (account name),
account_number, description. Use null when no column holds a field.
Return ONLY a JSON object like {{"date": 0, "action": 1, "symbol": 2, ...}}."""


def ai_mapping(header, shapes, api_key, *, client=None, model=None) -> dict | None:
    """Ask the AI to map columns from their names and cell kinds only. None
    when its answer doesn't give a usable mapping; a failed request raises
    (anthropic's errors), so the caller can say so and not count it."""
    import anthropic
    from csv_import import AI_MODEL
    client = client or anthropic.Anthropic(api_key=api_key, timeout=30.0)
    prompt = AI_PROMPT.format(header=json.dumps(header), shapes=json.dumps(shapes))
    resp = client.messages.create(model=model or AI_MODEL, max_tokens=400,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        raw = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    out = {f: v for f, v in raw.items() if f in FIELDS and isinstance(v, int)
           and not isinstance(v, bool) and 0 <= v < len(header)}
    return out if usable(out) else None


# ---- reading the rows ----------------------------------------------------- #
def parse_date(cell) -> str | None:
    """ISO date from "09/15/2026", "09/15/2026 as of 09/14/2026", "2026-09-15",
    "Sep 15, 2026"... - the first date in the cell."""
    t = str(cell or "").strip()
    if not t:
        return None
    candidates = [t, t.split(" as of ")[0], t.split()[0]]
    m = re.search(r"\d{1,4}[/-]\d{1,2}[/-]\d{2,4}", t)
    if m:
        candidates.insert(0, m.group())
    for c in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(c.strip(), fmt).date().isoformat()
            except ValueError:
                continue
    return None


def classify(action: str, description: str = "", *, quantity=None, amount=None) -> str:
    """The app's kind (a TYPES key) for one row of broker wording."""
    text = f"{action or ''} {description or ''}"
    kind = None
    for k, rx in _RULES:
        # the action column decides when it says something; the description
        # only fills in when the action is blank or unhelpful
        if rx.search(action or ""):
            kind = k
            break
    if kind is None:
        for k, rx in _RULES:
            if rx.search(text):
                kind = k
                break
    if kind in _CASH_KINDS and amount is not None:
        kind = "DEPOSIT" if amount > 0 else "WITHDRAWAL"
    if kind is None and quantity and amount:
        kind = "BUY" if amount < 0 else "SELL"
    return kind or "OTHER"


def _symbol(cell) -> str | None:
    t = str(cell or "").strip().upper().rstrip("*").strip()
    return t if t and _is_ticker(t) else None


def mask_digits(text: str | None) -> str | None:
    """Long digit runs (bank or account numbers on transfers) cut to their last 3."""
    if not text:
        return text
    return _DIGITS_RE.sub(lambda m: "..." + re.sub(r"\D", "", m.group())[-3:], text)


def _account(row, mapping, default: str) -> str:
    name = str(row[mapping["account"]]).strip() if "account" in mapping and \
        mapping["account"] < len(row) else ""
    number = str(row[mapping["account_number"]]).strip() if "account_number" in mapping and \
        mapping["account_number"] < len(row) else ""
    joined = " ".join(x for x in (name, number) if x) or default
    return mask_number(joined) or default


def parse(rows, mapping: dict, *, account_default: str = "My account",
          header_i: int | None = None) -> dict:
    """{"rows": [{account, trade_date, action, raw_action, symbol, description,
    quantity, price, amount, fees}], "skipped": count of lines that weren't
    activity (blank, notes, totals), "accounts": the accounts named}."""
    header_i = find_header(rows) if header_i is None else header_i
    start = (header_i + 1) if header_i is not None else 0

    def cell(row, f):
        i = mapping.get(f)
        return row[i] if i is not None and i < len(row) else ""

    out, skipped = [], 0
    for row in rows[start:]:
        if not any(c.strip() for c in row):
            continue
        d = parse_date(cell(row, "date"))
        if d is None:
            skipped += 1
            continue
        action = str(cell(row, "action")).strip()
        desc = str(cell(row, "description")).strip()
        qty = _num(cell(row, "quantity"))
        price = _num(cell(row, "price"))
        amount = _num(cell(row, "amount"))
        fees = sum(v for v in (_num(cell(row, "fees")), _num(cell(row, "commission")))
                   if v is not None) or None
        kind = classify(action, desc, quantity=qty, amount=amount)
        if amount is None and qty and price:
            amount = round(abs(qty) * price * (-1 if kind in ("BUY", "REINVEST") else 1), 2)
        out.append({
            "account": _account(row, mapping, account_default),
            "trade_date": d, "action": kind, "raw_action": mask_digits(action)[:80],
            "symbol": _symbol(cell(row, "symbol")),
            "description": mask_digits(desc)[:200] or None,
            "quantity": abs(qty) if qty else None,   # 0 shares on a dividend row: none
            "price": abs(price) if price else None,
            "amount": round(amount, 2) if amount is not None else None,
            "fees": abs(fees) if fees else None,
        })
    return {"rows": out, "skipped": skipped,
            "accounts": sorted({r["account"] for r in out})}


def match_account(name: str, existing: list[str]) -> str | None:
    """The holdings account a file's account is - the same name, or the same
    last 3 digits - or None."""
    if name in existing:
        return name
    digits = re.sub(r"\D", "", name or "")
    if len(digits) >= 3:
        same = [e for e in existing if re.sub(r"\D", "", e or "").endswith(digits[-3:])]
        if len(same) == 1:
            return same[0]
    return None


def summary(rows: list[dict]) -> dict:
    """{"first", "last", "by_type": Counter, "other": rows the app couldn't name}."""
    dates = sorted(r["trade_date"] for r in rows)
    return {"first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "by_type": Counter(r["action"] for r in rows),
            "other": [r for r in rows if r["action"] == "OTHER"]}


# ---- saving ------------------------------------------------------------------ #
def row_keys(rows: list[dict]) -> list[str]:
    """A fingerprint per row; identical rows on the same day are told apart by
    their order, so two equal buys stay two, and the same export twice adds
    nothing the second time."""
    seen = Counter()
    keys = []
    for r in rows:
        base = "|".join(str(r.get(k) if r.get(k) is not None else "") for k in
                        ("account", "trade_date", "action", "symbol", "quantity", "amount"))
        seen[base] += 1
        keys.append(hashlib.sha1(f"{base}|{seen[base]}".encode()).hexdigest()[:20])
    return keys


def covered(conn, user_id: int) -> dict[str, str]:
    """{account: the last date its imported history covers}."""
    return {r["account"]: r["d"] for r in conn.execute(
        "SELECT account, MAX(trade_date) AS d FROM transactions WHERE user_id = ? AND "
        "origin = ? GROUP BY account", (user_id, ORIGIN))}


def drop_covered(txns: list[dict], cover: dict[str, str]) -> list[dict]:
    """Worked-out rows (changes.synthesize_transactions) the imported history
    already covers are left out."""
    return [t for t in txns if not (t["account"] in cover
                                    and (t["trade_date"] or "") <= cover[t["account"]])]


def save_worked_out(conn, user_id: int, trade_date: str, txns: list[dict],
                    accounts=None) -> int:
    """Replace the day's worked-out rows (changes.compare) with `txns`, less
    those the imported history already covers; imported rows stay. With
    `accounts`, only those accounts' rows that day are replaced (a save of
    one brokerage's accounts leaves the trades worked out for the others).
    Account names are saved masked. No commit - the caller's transaction.
    Returns how many were written."""
    if accounts is None:
        conn.execute("DELETE FROM transactions WHERE trade_date = ? AND user_id = ? AND "
                     "origin IS NULL", (trade_date, user_id))
    else:
        for acct in sorted({mask_number(a) for a in accounts}):
            conn.execute("DELETE FROM transactions WHERE trade_date = ? AND user_id = ? AND "
                         "origin IS NULL AND account = ?", (trade_date, user_id, acct))
    txns = [{**t, "account": mask_number(t["account"]), "user_id": user_id} for t in txns]
    txns = drop_covered(txns, covered(conn, user_id))
    if txns:
        conn.executemany(
            "INSERT INTO transactions (account, trade_date, action, symbol, "
            "description, quantity, price, amount, fees, realized_gain, "
            "source_file, user_id) VALUES "
            "(:account, :trade_date, :action, :symbol, :description, :quantity, "
            ":price, :amount, :fees, :realized_gain, :source_file, :user_id)", txns)
    return len(txns)


def existing_keys(conn, user_id: int) -> set[str]:
    return {r["row_key"] for r in conn.execute(
        "SELECT row_key FROM transactions WHERE user_id = ? AND origin = ?", (user_id, ORIGIN))}


def save(conn, user_id: int, rows: list[dict], source: str) -> dict:
    """Save imported rows, in one transaction: rows already imported are
    skipped; each account's worked-out rows up to its imported history's last
    date are removed. {"added", "duplicates", "replaced"}."""
    rows = [{**r, "account": mask_number(r["account"])} for r in rows]  # parse() did; be sure
    have = existing_keys(conn, user_id)
    keys = row_keys(rows)
    new = [(r, k) for r, k in zip(rows, keys) if k not in have]
    last = {}
    for r in rows:
        last[r["account"]] = max(last.get(r["account"], ""), r["trade_date"])
    replaced = 0
    with conn:
        for account, d in last.items():
            cur = conn.execute("DELETE FROM transactions WHERE user_id = ? AND account = ? AND "
                               "origin IS NULL AND trade_date <= ?", (user_id, account, d))
            replaced += max(cur.rowcount, 0)
        for r, k in new:
            conn.execute(
                "INSERT INTO transactions (account, trade_date, action, symbol, description, "
                "quantity, price, amount, fees, realized_gain, source_file, user_id, origin, "
                "row_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (r["account"], r["trade_date"], r["action"], r["symbol"],
                 r["description"] or r["raw_action"], r["quantity"], r["price"], r["amount"],
                 r["fees"], source, user_id, ORIGIN, k))
    refresh_gains(conn, user_id)
    return {"added": len(new), "duplicates": len(rows) - len(new), "replaced": replaced}


def today_iso() -> str:
    return date.today().isoformat()


# ---- realized gains (phase 2) ------------------------------------------------ #
# within a day: shares arrive (splits, transfers, buys) before they're sold
_DAY_ORDER = {"SPLIT": 0, "TRANSFER": 1, "BUY": 2, "REINVEST": 2, "SELL": 3}
QTY_EPS = 1e-6


def replay_gains(rows: list[dict], held_before: dict | None = None) -> dict:
    """{row id: realized gain or None} for every SELL in `rows` (imported
    rows of one person, any order), by average cost, replayed in date order
    per account and symbol.

    A sale's gain is None (unknown) when its cost isn't fully known: shares
    held before the history starts (`held_before`: {(account, symbol):
    shares}), shares transferred in, a buy or split without the figures, or a
    sale of more shares than the history shows bought."""
    state = {}
    for (acct, sym), q in (held_before or {}).items():
        if q and q > QTY_EPS:
            state[(acct, sym)] = {"shares": q, "cost": 0.0, "known": False}
    out = {}
    for r in sorted(rows, key=lambda r: (r["trade_date"] or "", _DAY_ORDER.get(r["action"], 4),
                                         r.get("id") or 0)):
        kind, sym, q = r["action"], r.get("symbol"), r.get("quantity") or 0.0
        if not sym or kind not in _DAY_ORDER:
            continue
        s = state.setdefault((r["account"], sym), {"shares": 0.0, "cost": 0.0, "known": True})
        amount, price = r.get("amount"), r.get("price")
        if kind in ("BUY", "REINVEST"):
            cash = abs(amount) if amount is not None else (q * price if q and price else None)
            if not q:
                continue
            if cash is None:
                s["known"] = False
            s["shares"] += q
            s["cost"] += cash or 0.0
        elif kind in ("SPLIT", "TRANSFER"):
            if kind == "SPLIT" and not q:
                s["known"] = False       # a split without the new shares: can't follow it
            elif q:
                s["shares"] += q
                if kind == "TRANSFER":
                    s["known"] = False   # shares moved in: their cost isn't in this file
        else:  # SELL
            proceeds = amount if amount is not None else (
                q * price - (r.get("fees") or 0.0) if q and price else None)
            enough = s["shares"] >= q - QTY_EPS
            if q and enough and s["known"] and proceeds is not None and s["shares"] > QTY_EPS:
                basis = s["cost"] * q / s["shares"]
                out[r["id"]] = round(proceeds - basis, 2)
            else:
                out[r["id"]] = None
            if q and s["shares"] > q + QTY_EPS:
                s["cost"] -= s["cost"] * q / s["shares"]
                s["shares"] -= q
            else:   # sold out: whatever comes next starts fresh
                s.update(shares=0.0, cost=0.0, known=True)
    return out


def held_before(conn, user_id: int, rows: list[dict]) -> dict:
    """{(account, symbol): shares} from each account's last holdings update
    before its imported history starts - shares whose cost the history can't
    know."""
    first = {}
    for r in rows:
        first[r["account"]] = min(first.get(r["account"], r["trade_date"]), r["trade_date"])
    out = {}
    for acct, d in first.items():
        snap = conn.execute("SELECT MAX(snapshot_date) AS d FROM positions WHERE user_id = ? "
                            "AND account = ? AND snapshot_date < ?",
                            (user_id, acct, d)).fetchone()["d"]
        if snap:
            for p in conn.execute("SELECT symbol, quantity FROM positions WHERE user_id = ? "
                                  "AND account = ? AND snapshot_date = ?", (user_id, acct, snap)):
                out[(acct, p["symbol"])] = (out.get((acct, p["symbol"]), 0.0)
                                            + (p["quantity"] or 0.0))
    return out


def refresh_gains(conn, user_id: int) -> int:
    """Work out every imported sale's realized gain again (after an import).
    Returns how many sales have a known gain."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, account, trade_date, action, symbol, quantity, price, amount, fees "
        "FROM transactions WHERE user_id = ? AND origin = ?", (user_id, ORIGIN))]
    gains = replay_gains(rows, held_before(conn, user_id, rows))
    with conn:
        for rid, g in gains.items():
            conn.execute("UPDATE transactions SET realized_gain = ? WHERE id = ? AND user_id = ?",
                         (g, rid, user_id))
    return sum(g is not None for g in gains.values())
