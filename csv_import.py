"""Positions CSVs from any brokerage.

Schwab's own export keeps its dedicated reader (portfolio.parse_csv); every
other file comes here. Brokerages lay their exports out differently -
Fidelity puts the account on every row, Vanguard follows the holdings with a
transactions section, E*TRADE opens with an account summary - so this reader:

1. finds the holdings table anywhere in the file (skipping titles, notes,
   footers, totals and pending-activity rows; following one section per
   account, and stopping at a transactions section);
2. matches its columns by name (FIELDS), then - for anything still missing -
   by a remembered layout for the same column names, or by asking the AI,
   which sees only the column names and the *shape* of a few rows ("text",
   "number", "money"), never the values;
3. reads each holding's symbol, shares, cost (total, or per share x shares),
   value and account, and money-market / cash lines as cash;
4. recognizes a transactions export and says so instead of guessing.

The result fills the same review step as hand entry (manual_entry.py), so
every row is checked, and account numbers are cut to their last 3 digits on
save. The file itself is never kept.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timezone


# ticker-shaped: 1-5 capitals, optional class suffix (BRK.B, BF-B) or a coin's
# Yahoo name (BTC-USD)
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z]{1,2}|-USD)?$")
# capitalised words a table shows that aren't tickers
_NOT_TICKERS = {
    "CASH", "TOTAL", "TOTALS", "USD", "ETF", "ETFS", "NA", "ACCT", "QTY", "PRICE", "VALUE",
    "COST", "GAIN", "LOSS", "DAY", "TODAY", "SHARE", "SHARES", "BUY", "SELL", "HOLD", "NEW",
    "ALL", "YTD", "MTD", "AVG", "MKT", "IRA", "ROTH", "SEP", "FUND", "FUNDS", "INC", "CORP",
    "LLC", "LTD", "TRUST", "CLASS", "AND", "THE", "OF", "FOR", "MY", "DIV", "EST", "APR", "APY",
    "USA", "US", "NAME", "TYPE", "VIEW", "MORE", "LESS", "SORT", "EDIT", "TRADE", "OPEN",
    "CLOSE", "HIGH", "LOW", "LAST", "CHANGE", "CHG", "PCT", "YIELD", "ACCOUNT", "SYMBOL",
    "PENDING", "MARGIN", "CORE", "SWEEP", "OTHER", "STOCK", "STOCKS", "BOND", "BONDS", "OPTION",
    "OPTIONS", "SPDR", "PLC", "NV", "SA", "AG", "CO", "LP", "ADR", "ETN", "CEF", "REIT", "NYSE",
    "AMEX", "OTC", "IPO", "EPS", "PE", "N", "A", "I", "Y",
}

def _is_ticker(tok: str) -> bool:
    return bool(_TICKER_RE.match(tok)) and tok not in _NOT_TICKERS


# field -> header names, normalized by _norm() (lowercase, "%" -> "percent",
# no $ ( ) # * . , and single spaces)
FIELDS = {
    "symbol": ("symbol", "ticker", "symbol/cusip", "sym", "security symbol", "ticker symbol",
               "symbol / cusip"),
    "quantity": ("quantity", "qty", "shares", "units", "shares owned", "share count", "position",
                 "quantity/shares", "shares/units", "quantity held", "qty shares", "share qty"),
    "cost": ("cost basis", "cost basis total", "total cost", "cost", "total cost basis", "book value",
             "cost basis total dollar", "total cost basis dollar", "adjusted cost basis",
             "cost basis dollar"),
    "avg_cost": ("average cost", "avg cost", "average cost basis", "price paid", "cost per share",
                 "unit cost", "average price", "avg price", "avg cost basis", "cost/share",
                 "purchase price", "average unit cost"),
    "value": ("market value", "current value", "value", "total value", "equity", "marketvalue",
              "market value dollar", "current market value", "total market value", "mkt value",
              "position value"),
    "percent": ("percent of account", "percent of portfolio", "weight", "allocation",
                "portfolio percent", "percent of holdings", "percent of total", "percent of acct",
                "portfolio weight"),
    "account": ("account name", "account", "acct", "account name/number", "account type",
                "account nickname"),
    # kept apart so a name and a number can both be used ("Individual Z12345678",
    # which is saved as "Individual ...678")
    "account_number": ("account number", "account #", "account no", "acct number",
                       "account num"),
    "description": ("description", "name", "investment name", "security description", "security",
                    "security name", "fund name"),
    # optional extras, kept when a file has them (the Income page, alerts and
    # holdings columns use them) - whichever brokerage it's from
    "asset_type": ("asset type", "security type", "asset class", "investment type",
                   "product type"),
    "day_change_pct": ("day change percent", "today's gain/loss percent", "day's gain percent",
                       "change percent", "day change", "today's change percent",
                       "day's gain unrealized percent"),
    "price_change_pct": ("price change percent", "price change"),
    "reported_gain": ("gain", "gain/loss", "total gain", "gain dollar", "total gain/loss dollar",
                      "unrealized gain/loss", "gain/loss dollar", "total gain/loss",
                      "unrealized gain"),
    "reported_gain_pct": ("gain percent", "total gain percent", "total gain/loss percent",
                          "gain/loss percent", "unrealized gain/loss percent"),
    "div_yield_pct": ("dividend yield percent", "dividend yield", "yield", "yield percent",
                      "div yield", "div yield percent", "sec yield"),
    "div_pay_date": ("dividend pay date", "pay date", "next pay date", "dividend date",
                     "last dividend date"),
    "next_earnings_date": ("next earnings date", "earnings date", "next earnings"),
    "reinvest": ("reinvest?", "reinvest", "reinvest dividends", "drip", "reinvest dividends?"),
    "reinvest_cap_gains": ("reinvest capital gains?", "reinvest capital gains",
                           "reinvest cap gains"),
}
# rows that total a section or the file (their figures go to account totals)
_TOTAL_ROWS = ("positions total", "account total", "total", "totals", "grand total",
               "account totals")
LABELS = {"symbol": "Symbol", "quantity": "Shares", "cost": "Total cost",
          "avg_cost": "Cost per share", "value": "Value", "percent": "% of portfolio",
          "account": "Account", "description": "Name"}
# a header with any of these is a transactions export, not holdings
_TXN_WORDS = ("trade date", "transaction type", "trans code", "activity date", "process date",
              "settle date", "settlement date", "transaction date", "action", "run date",
              "transaction")
_CASH_WORDS = ("money market", "cash", "sweep", "core position", "fdic")
_DATE_RES = [
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), lambda m: (int(m[3]), int(m[1]), int(m[2]))),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ .-](\d{1,2}),?[ -](\d{4})\b",
                re.I),
     lambda m: (int(m[3]), datetime.strptime(m[1][:3].title(), "%b").month, int(m[2]))),
]


def _norm(cell) -> str:
    c = str(cell or "").strip().lower().replace(" ", " ").replace("%", " percent ")
    c = re.sub(r"[$()#*.,:]", " ", c)
    return re.sub(r"\s+", " ", c).strip()


def _field(cell) -> str | None:
    c = _norm(cell)
    for field, names in FIELDS.items():
        if c in names:
            return field
    return None


def _num(cell):
    """A number from a cell ("$1,234.50", "(12.30)", "5.00%", "--") or None."""
    t = str(cell or "").strip().replace(" ", "")
    if not t or t in ("--", "-", "n/a", "N/A"):
        return None
    neg = t.startswith("(") or t.startswith("-")
    # a decimal comma ("1.234,56" or "12,5") - a comma followed by 3 digits is
    # a thousands separator instead ("1,234")
    if re.search(r",\d{1,2}\)?%?$", t) and not re.search(r"\.\d+$", t):
        t = t.replace(".", "").replace(",", ".")
    t = re.sub(r"[^\d.]", "", t)
    if not re.search(r"\d", t) or t.count(".") > 1:
        return None
    return -float(t) if neg else float(t)


def shape(cell) -> str:
    """What a cell looks like, for the AI - never the value itself."""
    t = str(cell or "").strip()
    if not t or t in ("--", "-"):
        return "EMPTY"
    if any(r.search(t) for r, _ in _DATE_RES):
        return "DATE"
    if t.endswith("%") and _num(t) is not None:
        return "PERCENT"
    if "$" in t and _num(t) is not None:
        return "MONEY"
    if _num(t) is not None and re.fullmatch(r"[\s\d,.()+-]+", t):
        return "NUMBER"
    if _is_ticker(t.upper().rstrip("*")) and len(t) <= 6:
        return "SHORT_CODE"
    return "TEXT"


def read_rows(data: bytes) -> list[list[str]]:
    """The file's rows, whatever its encoding and delimiter."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = "\n".join(text.splitlines()[:40])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]


def signature(header: list[str]) -> str:
    """A layout's fingerprint: its column names, normalized."""
    return hashlib.sha1(json.dumps([_norm(c) for c in header]).encode()).hexdigest()


def _is_txn_header(cells) -> bool:
    names = {_norm(c) for c in cells}
    return any(w in names for w in _TXN_WORDS)


def find_header(rows) -> tuple[int | None, str | None]:
    """(index of the holdings header row, problem). The header is the first row
    naming a symbol column and a shares, value or percent column."""
    for i, row in enumerate(rows):
        fields = {_field(c) for c in row}
        if "symbol" in fields and fields & {"quantity", "value", "percent"}:
            return (None, "transactions") if _is_txn_header(row) else (i, None)
    for row in rows:
        if _is_txn_header(row) and len([c for c in row if c]) >= 3:
            return None, "transactions"
    return None, "no header"


def guess_header(rows) -> int | None:
    """For a layout FIELDS doesn't know: the first row of plain text cells that
    sits above a row with a ticker-like code and a number - the column check
    (or the AI) then says which column is which."""
    for i, row in enumerate(rows[:-1]):
        cells = [c for c in row if c]
        if len(cells) < 2 or any(shape(c) not in ("TEXT", "SHORT_CODE") for c in cells):
            continue
        nxt = next((r for r in rows[i + 1:] if any(r)), [])
        kinds = [shape(c) for c in nxt]
        if "SHORT_CODE" in kinds and ({"NUMBER", "MONEY"} & set(kinds)):
            return i
    return None


def auto_mapping(header: list[str]) -> dict:
    """{field: column index} for the columns named in FIELDS."""
    out = {}
    for i, cell in enumerate(header):
        f = _field(cell)
        if f and f not in out:
            out[f] = i
    return out


def usable(mapping: dict) -> bool:
    return "symbol" in mapping and bool({"quantity", "value", "percent"} & set(mapping))


def _snapshot_date(rows, header_i, data_end, filename, today) -> str:
    """The export's own date, from lines outside the table or the file name,
    else today. Never a date after today."""
    texts = [" ".join(r) for r in rows[:header_i]] + [" ".join(r) for r in rows[data_end:]]
    texts.append(filename or "")
    found = []
    for t in texts:
        for rx, parts in _DATE_RES:
            for m in rx.finditer(t):
                try:
                    d = date(*parts(m))
                except ValueError:
                    continue
                if d <= today:
                    found.append(d)
    return (max(found) if found else today).isoformat()


def parse(rows, mapping: dict, *, filename: str = "", today: date | None = None) -> dict:
    """Holdings from `rows` with `mapping`: {"holdings": [{Account, Symbol,
    Shares, Total cost, Value, Percent}], "cash": {account: amount},
    "snapshot_date", "mode", "skipped": rows that weren't holdings,
    "left_out": those of them that look like a holding - [{Name, Value, why}] -
    so the review can say what isn't imported}."""
    today = today or date.today()
    header_i, _ = find_header(rows)
    if header_i is None:
        header_i = guess_header(rows)
    if header_i is None:
        header_i = -1
    header = rows[header_i] if header_i >= 0 else []
    width = max(mapping.values(), default=0) + 1

    def cell(row, field):
        i = mapping.get(field)
        return row[i] if i is not None and i < len(row) else ""

    section_account, holdings, cash, totals, skipped, left_out = None, [], {}, {}, 0, []
    # the first section's account name can sit just above the header
    # ("Individual ...111", then the column names)
    for above in reversed(rows[:max(header_i, 0)]):
        filled = [c for c in above if c]
        if filled:
            if len(filled) == 1 and _looks_like_account(filled[0]):
                section_account = filled[0]
            break
    data_end, txn_start = header_i + 1, len(rows)
    for j in range(header_i + 1, len(rows)):
        row = rows[j]
        filled = [c for c in row if c]
        if not filled:
            continue
        if _is_txn_header(row):
            txn_start = j
            break  # a transactions section follows the holdings (Vanguard)
        if [_norm(c) for c in row[:len(header)]] == [_norm(c) for c in header]:
            continue  # the header repeated for the next account
        if len(filled) == 1 and not _num(filled[0]):
            if _looks_like_account(filled[0]):
                section_account = filled[0]  # an account heading line
            continue  # a heading, note or footer line
        acct = cell(row, "account") or section_account or ""
        number = cell(row, "account_number")
        if number and number not in acct:
            acct = f"{acct} {number}".strip()
        raw_sym = cell(row, "symbol").strip()
        sym = raw_sym.upper().rstrip("*").strip()
        desc = cell(row, "description")
        value = _num(cell(row, "value"))
        if _norm(raw_sym) in _TOTAL_ROWS or _norm(filled[0]) in _TOTAL_ROWS:
            totals[acct] = {"reported_cost_basis": _num(cell(row, "cost")),
                            "reported_market_value": value,
                            "reported_gain": _num(cell(row, "reported_gain")),
                            "reported_gain_pct": _num(cell(row, "reported_gain_pct"))}
            continue
        is_cash = raw_sym.endswith("**") or sym in ("CASH", "CASH & CASH INVESTMENTS") or \
            any(w in (desc or raw_sym).lower() for w in _CASH_WORDS) and not _num(cell(row, "quantity"))
        if is_cash:
            if value is None:  # a short cash line ("CASH   $512.33"): its last amount
                value = next((_num(c) for c in reversed(filled) if "$" in c and _num(c)), None)
            if value:
                cash[acct] = round(cash.get(acct, 0.0) + value, 2)
            data_end = j + 1
            continue
        if not _is_ticker(sym):
            skipped += 1
            if (desc or raw_sym) and (value or _num(cell(row, "quantity"))):
                left_out.append({"Name": _text(desc) or raw_sym, "Value": value,
                                 "why": "it has no ticker"})
            continue
        qty = _num(cell(row, "quantity"))
        cost = _num(cell(row, "cost"))
        avg = _num(cell(row, "avg_cost"))
        if cost is None and avg is not None and qty:
            cost = round(avg * qty, 2)
        pct = _num(cell(row, "percent"))
        if qty is None and value is None and pct is None:
            skipped += 1
            left_out.append({"Name": _text(desc) or sym, "Value": None,
                             "why": "it has no shares or value"})
            continue
        extras = {f: _text(cell(row, f)) for f in _TEXT_EXTRAS}
        extras.update({f: _num(cell(row, f)) for f in _NUM_EXTRAS})
        extras.update({f: _yesno(cell(row, f)) for f in _YESNO_EXTRAS})
        holdings.append({"Account": acct, "Symbol": sym, "Shares": qty, "Total cost": cost,
                         "Value": value, "Percent": pct, "Name": _text(desc), "extras": extras})
        data_end = j + 1
    with_shares = [h for h in holdings if h["Shares"]]
    mode = "Shares" if with_shares or not holdings else "Percentages"
    return {"holdings": with_shares if mode == "Shares" else holdings, "cash": cash,
            "totals": totals,
            "snapshot_date": _snapshot_date(rows[:txn_start], max(header_i, 0), data_end,
                                            filename, today),
            "as_of_text": _as_of(rows[:max(header_i, 0)]),
            "mode": mode, "skipped": skipped, "left_out": left_out}


def left_out_text(left_out: list[dict], money=lambda v: f"${v:,.2f}") -> str:
    """'1 row skipped: FID CONTRAFUND POOL CL 2 ($5,112.00) - it has no ticker.'"""
    n = len(left_out)
    parts = [f"{r['Name']}" + (f" ({money(r['Value'])})" if r.get("Value") else "")
             + f" - {r['why']}" for r in left_out[:5]]
    more = f"; and {n - 5} more" if n > 5 else ""
    return (f"{n} row{'s' if n != 1 else ''} skipped, not imported: " + "; ".join(parts)
            + more + ".")


_TEXT_EXTRAS = ("asset_type", "div_pay_date", "next_earnings_date")
_NUM_EXTRAS = ("day_change_pct", "price_change_pct", "reported_gain", "reported_gain_pct",
               "div_yield_pct")
_YESNO_EXTRAS = ("reinvest", "reinvest_cap_gains")


_ACCOUNT_WORDS = re.compile(
    r"\b(individual|joint|ira|roth|brokerage|401 ?k|403 ?b|457|trust|custodial|hsa|sep|simple|"
    r"rollover|traditional|taxable|retirement|margin|cash account|529|utma|ugma|account)\b", re.I)


def _looks_like_account(line: str) -> bool:
    """A single-cell line naming an account ("Individual ...111", "Roth IRA"),
    not a title or a note ("View Summary - All Positions")."""
    if re.search(r"\bas of\b", line, re.I) or len(line) > 60:
        return False
    return bool(re.search(r"\d{3}", line) or _ACCOUNT_WORDS.search(line))


def _text(cell):
    t = str(cell or "").strip()
    return None if t.lower() in ("", "--", "-", "n/a", "na") else t


def _yesno(cell):
    t = str(cell or "").strip().lower()
    return 1 if t in ("yes", "y", "true") else 0 if t in ("no", "n", "false") else None


def _as_of(rows_above) -> str | None:
    """The export's own "as of" title line, when it has one."""
    for row in rows_above:
        line = " ".join(c for c in row if c).strip()
        if re.search(r"\bas of\b", line, re.I):
            return line
    return None


def to_snapshot(found: dict, *, account_default: str = "My account") -> tuple[dict, list, dict]:
    """parse()'s result in portfolio.write_snapshot()'s shapes (meta, positions,
    account totals). Values are the file's own; a holding without one gets
    market_value None (the caller prices it)."""
    snap = found["snapshot_date"]
    rows = []
    for h in found["holdings"]:
        cost, value = h["Total cost"], h["Value"]
        row = {"snapshot_date": snap, "account": h["Account"] or account_default,
               "symbol": h["Symbol"], "description": h["Name"], "quantity": h["Shares"],
               "cost_basis": cost, "market_value": value, "pct_of_account": h["Percent"],
               **h.get("extras", {})}
        if row.get("reported_gain") is None and value is not None and cost is not None:
            row["reported_gain"] = round(value - cost, 2)
        rows.append(row)
    blank = {"cash_value": None, "reported_cost_basis": None, "reported_market_value": None,
             "reported_gain": None, "reported_gain_pct": None}
    totals = {}
    for acct in sorted({r["account"] for r in rows} | {a or account_default for a in found["cash"]}
                       | {a or account_default for a in found.get("totals", {})}):
        totals[acct] = dict(blank)
    for a, v in found["cash"].items():
        totals[a or account_default]["cash_value"] = v
    for a, t in found.get("totals", {}).items():
        totals[a or account_default].update(t)
    return {"snapshot_date": snap, "as_of_text": found.get("as_of_text")}, rows, totals


def sample_shapes(rows, header_i: int, n: int = 3) -> list[list[str]]:
    """The shape of the first few rows under the header - what the AI may see."""
    out = []
    for row in rows[header_i + 1:]:
        if len([c for c in row if c]) >= 2:
            out.append([shape(c) for c in row])
        if len(out) >= n:
            break
    return out


# Only when the person asks ("Let AI guess the columns"), and only column names
# and cell kinds go out - a small, cheap request; the fastest model is plenty.
AI_MODEL = "claude-haiku-4-5-20251001"

AI_PROMPT = """A brokerage positions export has these column names (JSON array):
{header}
and the first rows look like this (each cell replaced by its kind - the values are
not shown): {shapes}
Which column index (0-based) holds each field? Fields: symbol (ticker), quantity
(shares held), cost (TOTAL cost basis), avg_cost (cost PER SHARE), value (current market
value), percent (% of the account/portfolio), account (account name or number),
description (security name). Use null when no column holds a field.
Return ONLY a JSON object like {{"symbol": 0, "quantity": 3, "cost": null, ...}}."""


def ai_mapping(header, shapes, api_key, *, client=None, model=None) -> dict | None:
    """Ask the AI to map columns from their names and cell kinds only. None
    when its answer doesn't give a usable mapping; a failed request raises
    (anthropic's errors), so the caller can say so and not count it."""
    import anthropic
    client = client or anthropic.Anthropic(api_key=api_key, timeout=30.0)
    model = model or AI_MODEL
    prompt = AI_PROMPT.format(header=json.dumps(header), shapes=json.dumps(shapes))
    resp = client.messages.create(model=model, max_tokens=400,
                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        raw = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    out = {f: v for f, v in raw.items()
           if f in FIELDS and isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(header)}
    return out if usable(out) else None


# ---- remembered layouts ----------------------------------------------------- #
def remembered(conn, header) -> dict | None:
    row = conn.execute("SELECT mapping FROM csv_layouts WHERE signature = ?",
                       (signature(header),)).fetchone()
    if not row:
        return None
    try:
        m = json.loads(row["mapping"])
    except ValueError:
        return None
    m = {f: v for f, v in m.items() if isinstance(v, int) and 0 <= v < len(header)}
    return m if usable(m) else None


def remember(conn, header, mapping: dict) -> None:
    """Keep a layout's column mapping (column names only - no data) so the
    next file with the same columns needs no questions."""
    conn.execute("INSERT INTO csv_layouts (signature, mapping, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT (signature) DO UPDATE SET mapping = excluded.mapping, "
                 "updated_at = excluded.updated_at",
                 (signature(header), json.dumps(mapping),
                  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()
