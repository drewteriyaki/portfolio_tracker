"""Holdings from screenshots of a brokerage app or website - opt-in, AI-read.

For brokerages where copying or exporting is hard (phone apps). Unlike the
paste and CSV paths, this sends the images themselves to Anthropic's AI, so
the dashboard asks first, suggests cropping to just the holdings list, and
never keeps the images (they're read from memory and dropped).

The model is asked for symbols, share counts, cost (only when a column
clearly says so) or percentages, and cash - nothing else. Its answer is then
re-checked here: only ticker-shaped symbols and sensible numbers survive,
so a name, balance or account number the model returns anyway is dropped.
Results fill the hand-entry review form, where every row is checked before
anything is saved.
"""

from __future__ import annotations

import base64
import json
import re

from csv_import import _is_ticker

MAX_IMAGES = 5
MAX_BYTES = 5 * 1024 * 1024        # per image, the API's limit
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "webp": "image/webp", "gif": "image/gif"}
MAX_TOKENS = 4000

PROMPT = """These are screenshots of someone's brokerage holdings (positions) screen.
List every holding you can see. Return ONLY a JSON object, no other text:
{"holdings": [{"symbol": "VTI", "shares": 10.5, "cost_basis": null, "average_cost": null,
               "percent": null, "crypto": false}],
 "cash": null}

Rules:
- symbol: the ticker symbol exactly as shown (e.g. VTI, BRK.B, BTC). If a row shows only
  a name and no ticker, skip it.
- shares: the number of shares, units or coins held - never a dollar amount or a price.
- cost_basis: the TOTAL cost of the position, only if a column is clearly labeled cost
  basis or total cost; otherwise null. Never use market value, price or gain.
- average_cost: the cost PER SHARE, only if a column is labeled average cost, avg cost
  or cost per share; otherwise null.
- crypto: true for a cryptocurrency (e.g. listed under a Crypto section, or Bitcoin,
  Ethereum), otherwise false.
- percent: the holding's % of the portfolio or account, only if shown; otherwise null.
  Never use a day-change or gain percentage.
- cash: the cash / money market / sweep balance if clearly shown, else null.
- The same holding may appear in two overlapping screenshots: list it once.
- Do not include names, account numbers, balances, gains or anything else.
- If you can't read something with confidence, leave it out.
If there are no holdings in the images, return {"holdings": [], "cash": null}."""


def check_images(files) -> tuple[list[tuple[bytes, str]], list[str]]:
    """[(bytes, media type)] for uploaded files, and problems with them.
    `files` are (filename, bytes) pairs."""
    images, errors = [], []
    if len(files) > MAX_IMAGES:
        errors.append(f"Up to {MAX_IMAGES} screenshots at a time.")
    for name, data in files[:MAX_IMAGES]:
        ext = (name or "").rsplit(".", 1)[-1].lower()
        if ext not in MEDIA_TYPES:
            errors.append(f"{name}: use a PNG, JPG or WEBP image.")
        elif len(data) > MAX_BYTES:
            errors.append(f"{name}: the image is over 5 MB - crop it or take a smaller one.")
        else:
            images.append((data, MEDIA_TYPES[ext]))
    return images, errors


def _num(v, *, allow_zero=False):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except ValueError:
        return None
    return f if (f > 0 or (allow_zero and f == 0)) and f == f else None


def clean(answer) -> dict:
    """The model's answer, re-checked: the same shape paste_parse.parse()
    returns, keeping only ticker-shaped symbols and positive numbers."""
    holdings: dict = {}
    for h in (answer or {}).get("holdings") or []:
        if not isinstance(h, dict):
            continue
        sym = str(h.get("symbol") or "").strip().upper().rstrip("*")
        shares, cost, pct = _num(h.get("shares")), _num(h.get("cost_basis")), _num(h.get("percent"))
        avg = _num(h.get("average_cost"))
        if cost is None and avg is not None and shares:
            cost = round(avg * shares, 2)  # average (per share) cost x shares
        crypto = h.get("crypto") is True
        if crypto and not sym.endswith("-USD"):
            sym += "-USD"  # Yahoo's name for the coin; plain "BTC" is a stock fund's ticker
        if not _is_ticker(sym) or (shares is None and pct is None) or (pct is not None and pct > 100):
            continue
        prev = holdings.get(sym)
        if prev and (prev["Shares"], prev["Percent"]) == (shares, pct):
            continue  # the same row seen in two overlapping screenshots
        if prev:  # the same symbol in two accounts
            prev["Shares"] = (prev["Shares"] or 0) + (shares or 0) or None
            prev["Total cost"] = (prev["Total cost"] or 0) + (cost or 0) or None
            prev["Percent"] = (prev["Percent"] or 0) + (pct or 0) or None
            continue
        holdings[sym] = {"Symbol": sym, "Shares": shares, "Total cost": cost, "Percent": pct,
                         "Type": "Crypto" if crypto else None}
    rows = list(holdings.values())
    with_shares = [r for r in rows if r["Shares"]]
    mode = "Shares" if with_shares or not rows else "Percentages"
    return {"holdings": with_shares if mode == "Shares" else rows,
            "cash": _num((answer or {}).get("cash")), "mode": mode}


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else None


def read(images: list[tuple[bytes, str]], api_key: str, *, client=None, model=None) -> dict:
    """Ask the AI to read `images`; returns clean()'s shape plus "error"
    (a message, or None) and "answered" (the AI replied, so the read counts
    against the month's allowance; False when the request failed - then
    "failure" holds the exception, for the server log). `client` is for
    tests."""
    import anthropic
    if model is None:
        from advisor import MODEL as model
    client = client or anthropic.Anthropic(api_key=api_key, timeout=90.0)
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt,
                                            "data": base64.standard_b64encode(data).decode()}}
               for data, mt in images]
    content.append({"type": "text", "text": PROMPT})
    empty = {"holdings": [], "cash": None, "mode": "Shares", "answered": False}
    try:
        resp = client.messages.create(model=model, max_tokens=MAX_TOKENS,
                                      messages=[{"role": "user", "content": content}])
    except anthropic.AnthropicError as exc:
        # one calm sentence per kind of failure, never the error's own text;
        # the caller logs `failure` (dashboard._ai_failed)
        import ai_usage
        return {**empty, "failure": exc,
                "error": ai_usage.failure_text(exc, feature="Reading screenshots")}
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    try:
        answer = _extract_json(text)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return {**empty, "answered": True,
                "error": "The AI's answer couldn't be understood - try a clearer screenshot."}
    return {**clean(answer), "error": None, "answered": True}
