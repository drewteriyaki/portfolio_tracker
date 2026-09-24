"""AI-assisted fallback for CSV imports whose headers don't match the
strict Schwab export shape `portfolio.parse_csv()` expects.

Deliberately narrow scope, chosen for cost and privacy, not just
simplicity: the AI's ONLY job is mapping column names to field indices
from the header row alone (no real portfolio data - amounts, symbols,
account numbers - ever leaves the machine in the common case). Every
actual value is still parsed by this codebase's own tested
`parse_num`/`parse_text`/`parse_yesno` helpers, in `parse_with_mapping()`
below, not by the AI. Row-type detection (which row is a holding vs. the
account's cash line vs. its totals line) stays as ordinary Python
pattern-matching on generic substrings ("total", "cash") - also not
something that needs AI, and one less thing to send over the network.

Uses the official anthropic SDK (same client as the AI Assistant). This
module is only imported when the strict parser has already failed and an
ANTHROPIC_API_KEY is configured, so every normal import never touches it.
"""

from __future__ import annotations

import csv
import json
import re

from portfolio import COL, extract_snapshot_date, parse_num, parse_text, parse_yesno

MODEL = "claude-haiku-4-5-20251001"  # fastest/cheapest - this is a small structured-output task
MAX_TOKENS = 500  # response is just a compact JSON mapping, nothing else

# Every field parse_csv() can populate. symbol/quantity/cost_basis/market_value
# are the only ones a file must actually have to be usable at all - the rest
# are nice-to-have and null-safe throughout the rest of the codebase already.
REQUIRED_FIELDS = ("symbol", "quantity", "cost_basis", "market_value")
ALL_FIELDS = tuple(COL.keys())

_SYSTEM_PROMPT = (
    "You map CSV column headers from a brokerage positions export to a fixed "
    "set of field names. Given a header row (a JSON array of strings), return "
    "ONLY a JSON object - no prose, no markdown fences - with exactly these "
    "keys: " + ", ".join(ALL_FIELDS) + ". Each value is the 0-based index of "
    "the column in the header row that holds that data, or null if no column "
    "in this file corresponds to it. symbol/quantity/cost_basis/market_value "
    "are the most important to get right - if you're not confident a column "
    "maps to one of them, use null rather than guessing."
)


def _extract_json(text: str) -> str:
    """Strip a ```json ... ``` fence if the model added one despite being
    told not to - cheap defensive parsing, not a real markdown parser."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


def guess_header_row(path: str) -> list[str] | None:
    """First row with more than one non-blank cell - the header, under the
    same title/blank/section-name/header/data structural assumption the
    Schwab format uses (this fallback targets a relabeled or reordered
    version of that same shape, not an arbitrarily different layout).
    None if the file has no such row at all."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if not row or all(cell.strip() == "" for cell in row):
                continue
            if sum(1 for c in row if c.strip()) > 1:
                return row
    return None


def map_columns(header_row: list[str], api_key: str, *, timeout: float = 15.0, client=None):
    """Ask Claude which column index holds each field. Returns (mapping, error)
    - exactly one is truthy. `mapping` is {field_name: int | None}, validated:
    every key present, every value either None or a real index into
    `header_row`, and every REQUIRED_FIELDS entry non-None. `client` is for
    tests; normally one is built from `api_key`."""
    import anthropic

    client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(header_row)}],
        )
    except anthropic.APIStatusError as exc:
        return None, f"HTTP {exc.status_code}: {exc.message}"
    except anthropic.APITimeoutError:
        return None, "timeout"
    except anthropic.APIConnectionError:
        return None, "network error"

    raw_text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        mapping = json.loads(_extract_json(raw_text))
    except json.JSONDecodeError:
        return None, "unparseable response from the model"

    if not isinstance(mapping, dict):
        return None, "model response wasn't a JSON object"

    n = len(header_row)
    validated: dict[str, int | None] = {}
    for field in ALL_FIELDS:
        v = mapping.get(field)
        if v is None:
            validated[field] = None
        elif isinstance(v, int) and 0 <= v < n:
            validated[field] = v
        else:
            return None, f"invalid column index for '{field}': {v!r}"

    missing_required = [f for f in REQUIRED_FIELDS if validated[f] is None]
    if missing_required:
        return None, f"couldn't confidently identify required column(s): {', '.join(missing_required)}"

    return validated, ""


_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")


def parse_with_mapping(path: str, mapping: dict, header_row: list[str]):
    """Same return shape as portfolio.parse_csv() - (meta, positions,
    account_totals) - but reading columns by AI-supplied index instead of
    the hardcoded Schwab COL layout. Row-type detection (section header /
    cash row / totals row) uses generic substring matching rather than the
    exact Schwab marker text, since a different broker's wording is
    unknown ahead of time. `header_row` is the exact row map_columns() was
    given - a multi-account export typically repeats it once per account
    section, and those repeats must be skipped rather than parsed as
    holdings (a header cell like "Quantity" is non-blank, so without this
    check it would otherwise look like a real, if garbled, position)."""
    meta = {"snapshot_date": None, "as_of_text": None}
    positions: list[dict] = []
    account_totals: dict[str, dict] = {}
    current_account = None

    def totals_for(acct):
        return account_totals.setdefault(acct, {
            "cash_value": None, "reported_cost_basis": None,
            "reported_market_value": None, "reported_gain": None, "reported_gain_pct": None,
        })

    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))

    for row in rows:
        if not row or all(cell.strip() == "" for cell in row):
            continue
        if row == header_row:
            continue

        first = row[0].strip()
        first_lower = first.lower()
        rest_blank = all(cell.strip() == "" for cell in row[1:])

        # Title/section-header lines only - same structural gate the
        # strict parser uses, so a data row whose OWN cell happens to
        # contain a MM/DD/YYYY-shaped value (e.g. a dividend pay date)
        # is never mistaken for the file's title line.
        is_title_shaped = len(row) == 1 or rest_blank
        if is_title_shaped and meta["snapshot_date"] is None and _DATE_RE.search(first):
            meta["as_of_text"] = first
            meta["snapshot_date"] = extract_snapshot_date(first)
            continue
        if is_title_shaped and "total" not in first_lower:
            if first:
                current_account = first
            continue

        def cell(name: str) -> str:
            idx = mapping[name]
            return row[idx] if idx is not None and idx < len(row) else ""

        if "total" in first_lower:
            if current_account is not None:
                t = totals_for(current_account)
                t["reported_cost_basis"] = parse_num(cell("cost_basis"))
                t["reported_market_value"] = parse_num(cell("market_value"))
                t["reported_gain"] = parse_num(cell("reported_gain"))
                t["reported_gain_pct"] = parse_num(cell("reported_gain_pct"))
            continue

        asset_type = parse_text(cell("asset_type"))
        is_cash = "cash" in first_lower or "cash" in (asset_type or "").lower()
        if is_cash:
            if current_account is not None:
                totals_for(current_account)["cash_value"] = parse_num(cell("market_value"))
            continue

        symbol = parse_text(cell("symbol"))
        if not symbol:
            continue  # a header row or other non-data line the mapping doesn't recognize

        positions.append({
            "snapshot_date": meta["snapshot_date"],
            "account": current_account,
            "symbol": symbol,
            "description": parse_text(cell("description")),
            "asset_type": asset_type,
            "quantity": parse_num(cell("quantity")),
            "cost_basis": parse_num(cell("cost_basis")),
            "market_value": parse_num(cell("market_value")),
            "price_change_pct": parse_num(cell("price_change_pct")),
            "day_change_pct": parse_num(cell("day_change_pct")),
            "reported_gain": parse_num(cell("reported_gain")),
            "reported_gain_pct": parse_num(cell("reported_gain_pct")),
            "reinvest": parse_yesno(cell("reinvest")),
            "reinvest_cap_gains": parse_yesno(cell("reinvest_cap_gains")),
            "div_pay_date": parse_text(cell("div_pay_date")),
            "div_yield_pct": parse_num(cell("div_yield_pct")),
            "next_earnings_date": parse_text(cell("next_earnings_date")),
            "pct_of_account": parse_num(cell("pct_of_account")),
        })

    if meta["snapshot_date"] is None:
        raise SystemExit("AI-assisted parse couldn't find a date anywhere in the file.")
    if not positions:
        raise SystemExit("AI-assisted parse found no holding rows.")
    return meta, positions, account_totals
