"""Fund overlap: do my funds hold the same companies?

Two funds can own many of the same companies - a total-market fund and an
S&P 500 fund share nearly all of their largest holdings - and a fund can own
a company you also hold directly. This looks through the funds:

- pair_overlap(): how much two funds' top holdings have in common - how many
  of them they share, and the weight they share (the smaller of the two
  weights of each shared holding, added up).
- look_through(): the companies you own most of once funds are looked
  through - Apple directly, plus Apple's share of each fund that holds it -
  as dollars and as a share of the whole portfolio.

The data is each fund's TOP holdings from Yahoo Finance (yfinance's
funds_data.top_holdings: usually the 10 largest, as fractions of the fund).
It covers a fund's biggest positions only, so every figure here is "based on
each fund's top 10 holdings" and is a floor, not the whole picture.

Fetched on demand (the Fund overlap window on Home, views/fund_overlap.py),
never by the nightly job: a short time limit for all funds together, a
failure remembered for a couple of minutes so a redraw never waits on Yahoo
again, and the answer kept in fund_top_holdings - shared market data like
security_info, no user_id - and asked again at most once a week. Without a
network the kept answer is used, however old; with none kept the fund is
"not available". Description only - nothing here says to buy or sell.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import datetime, timedelta, timezone

from fees import holding_type

TOP_N = 10                  # what Yahoo lists; said in the window
REFRESH_DAYS = 7            # ask Yahoo again after a week
TIMEOUT = 6.0               # seconds to wait for all the funds together
DOWN_SECONDS = 120          # after a failure, don't ask again for this long
_ASKED = 0                  # the row that records when a fund was asked (no holding)

# how many of their top holdings two funds share -> a plain word
MOST, SOME = 0.6, 0.3

_LOCK = threading.Lock()
_DOWN_UNTIL = [0.0]


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(ts)[:19].replace(" ", "T"),
                                 "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def norm(symbol) -> str:
    """One spelling per ticker: brokers write BRK.B or BRK/B, Yahoo BRK-B."""
    return str(symbol or "").strip().upper().replace(".", "-").replace("/", "-")


def _key(symbol, name) -> str:
    """What identifies a holding across funds: its ticker, else its name
    (some holdings - a bond, a foreign share - have no ticker here)."""
    s = norm(symbol)
    if s and s not in ("NAN", "NONE"):
        return s
    return "name:" + " ".join(str(name or "").lower().split())


def funds_in(positions, sec_info) -> list[str]:
    """The funds among `positions` (fees.holding_type), one per ticker."""
    sec_info = sec_info or {}
    return sorted({p["symbol"] for p in positions or [] if p.get("symbol")
                   and holding_type((sec_info.get(p["symbol"]) or {}).get("quote_type"),
                                    p.get("asset_type")) == "fund"})


# ---- the kept answers ------------------------------------------------------ #
def cached(conn, funds) -> dict:
    """{fund: {"holdings": [{symbol, name, weight}], "fetched_at": ISO}} for
    the funds Yahoo was asked about (holdings [] when it listed none). One
    query; none at all for no funds."""
    funds = sorted(set(funds or []))
    if not funds:
        return {}
    out: dict = {}
    for r in conn.execute(
            f"SELECT fund, slot, symbol, name, weight, fetched_at FROM fund_top_holdings "
            f"WHERE fund IN ({', '.join('?' for _ in funds)}) ORDER BY fund, slot", tuple(funds)):
        entry = out.setdefault(r["fund"], {"holdings": [], "fetched_at": r["fetched_at"]})
        if r["slot"] != _ASKED:
            entry["holdings"].append({"symbol": r["symbol"] or "", "name": r["name"],
                                      "weight": float(r["weight"] or 0.0)})
    return out


def store(conn, fund: str, holdings, now: datetime | None = None) -> None:
    """Keep `holdings` ([{symbol, name, weight}], [] = Yahoo lists none) as
    `fund`'s answer, replacing the one before. The caller commits."""
    at = _iso(now or datetime.now(timezone.utc))
    conn.execute("DELETE FROM fund_top_holdings WHERE fund = ?", (fund,))
    rows = [(fund, _ASKED, None, None, None, at)]
    rows += [(fund, i, h.get("symbol") or "", h.get("name"), float(h["weight"]), at)
             for i, h in enumerate(holdings or [], start=1)]
    # (two sessions storing the same fund at once: the later one wins)
    conn.executemany("INSERT INTO fund_top_holdings (fund, slot, symbol, name, weight, "
                     "fetched_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(fund, slot) DO UPDATE "
                     "SET symbol=excluded.symbol, name=excluded.name, weight=excluded.weight, "
                     "fetched_at=excluded.fetched_at", rows)


def is_stale(entry: dict | None, now: datetime | None = None) -> bool:
    """Never asked, or asked more than REFRESH_DAYS ago."""
    if not entry:
        return True
    at = _parse(entry.get("fetched_at"))
    return at is None or (now or datetime.now(timezone.utc)) - at > timedelta(days=REFRESH_DAYS)


# ---- Yahoo ------------------------------------------------------------------ #
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None   # not NaN, not 0 or below


def yahoo_top(fund: str) -> list[dict]:
    """[{symbol, name, weight (a fraction)}] - the fund's top holdings, [] when
    Yahoo lists none. Raises when Yahoo can't be reached."""
    import yfinance as yf
    try:
        df = yf.Ticker(fund).funds_data.top_holdings
    except Exception as e:
        # Yahoo answered, with no fund data for it: nothing listed (kept a
        # week, like an empty list). Anything else - offline - is raised.
        if type(e).__name__ == "YFDataException":
            return []
        raise
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    names = df["Name"] if "Name" in df.columns else [None] * len(df)
    for sym, name, w in zip(df.index, names, df["Holding Percent"]):
        weight = _num(w)
        if weight is None:
            continue
        sym = "" if sym is None or str(sym).lower() in ("nan", "none") else str(sym).strip()
        out.append({"symbol": sym, "name": (str(name).strip() if name is not None else "")
                    or sym or None, "weight": weight})
    if sum(h["weight"] for h in out) > 1.5:   # percents, not fractions
        for h in out:
            h["weight"] /= 100
    return [h for h in out if h["weight"] <= 1][:TOP_N]


def fetch(funds, *, timeout: float = TIMEOUT, now: float | None = None, raw=None) -> dict:
    """{fund: holdings, or None when it couldn't be fetched} - asked side by
    side, waiting at most `timeout` seconds for all of them. A failure pauses
    asking for DOWN_SECONDS. `raw` stands in for yahoo_top (tests)."""
    funds = list(dict.fromkeys(funds or []))
    t = time.time() if now is None else now
    if not funds:
        return {}
    with _LOCK:
        if t < _DOWN_UNTIL[0]:
            return dict.fromkeys(funds)
    ask = raw or yahoo_top
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(funds)))
    try:
        jobs = {f: pool.submit(ask, f) for f in funds}
        concurrent.futures.wait(jobs.values(), timeout=timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # a slow answer is left behind
    out, failed = {}, False
    for f, job in jobs.items():
        if job.done() and not job.cancelled() and job.exception() is None:
            out[f] = job.result()
        else:
            out[f], failed = None, True
    if failed:
        with _LOCK:
            _DOWN_UNTIL[0] = t + DOWN_SECONDS
    return out


def ensure(conn, funds, *, now: datetime | None = None, timeout: float = TIMEOUT,
           raw=None) -> tuple[dict, list]:
    """The top holdings of `funds`: kept answers, with any missing or older
    than a week asked again. Returns (cached()-style dict, funds that could
    not be fetched just now). A failed refresh keeps the older answer."""
    now = now or datetime.now(timezone.utc)
    have = cached(conn, funds)
    want = [f for f in sorted(set(funds or [])) if is_stale(have.get(f), now)]
    failed = []
    if want:
        got = fetch(want, timeout=timeout, raw=raw)
        fresh = False
        for f in want:
            if got.get(f) is None:
                failed.append(f)
                continue
            store(conn, f, got[f], now)
            have[f] = {"holdings": got[f], "fetched_at": _iso(now)}
            fresh = True
        if fresh:
            conn.commit()
    return have, [f for f in failed if f not in have]


def clear_pause():
    with _LOCK:
        _DOWN_UNTIL[0] = 0.0


# ---- the maths -------------------------------------------------------------- #
def _weights(holdings) -> dict:
    """{key: (weight, name)}, the same holding listed twice added up."""
    out: dict = {}
    for h in holdings or []:
        k = _key(h.get("symbol"), h.get("name"))
        w, _ = out.get(k, (0.0, None))
        out[k] = (w + float(h.get("weight") or 0.0), h.get("name") or h.get("symbol"))
    return out


def pair_overlap(a, b) -> dict:
    """How much two funds' top holdings overlap.
    {shared: [names, biggest shared weight first], n_shared, of (the shorter
    list's length), weight: the shared weight - for each shared holding the
    smaller of its two weights, added up (a fraction of each fund), level:
    'most' / 'some' / 'few' / 'none'}."""
    wa, wb = _weights(a), _weights(b)
    common = sorted(set(wa) & set(wb), key=lambda k: -min(wa[k][0], wb[k][0]))
    of = min(len(wa), len(wb))
    share = len(common) / of if of else 0.0
    level = ("most" if share >= MOST else "some" if share >= SOME
             else "few" if common else "none")
    return {"shared": [wa[k][1] or k for k in common], "n_shared": len(common), "of": of,
            "weight": sum(min(wa[k][0], wb[k][0]) for k in common), "level": level}


def overlaps(tops: dict, funds) -> list[dict]:
    """pair_overlap() for every two of `funds` with top holdings in `tops`,
    most shared first: [{a, b, ...pair_overlap()}]."""
    have = [f for f in sorted(set(funds or [])) if (tops.get(f) or {}).get("holdings")]
    pairs = []
    for i, a in enumerate(have):
        for b in have[i + 1:]:
            pairs.append({"a": a, "b": b, **pair_overlap(tops[a]["holdings"],
                                                         tops[b]["holdings"])})
    pairs.sort(key=lambda p: (-(p["n_shared"] / p["of"] if p["of"] else 0), -p["weight"],
                              p["a"], p["b"]))
    return pairs


def describe(pair: dict) -> str:
    """'VTI and VOO share most of their largest holdings (9 of their top 10).'"""
    names = f"{pair['a']} and {pair['b']}"
    if pair["level"] == "none":
        return f"{names} have none of their largest holdings in common."
    words = {"most": "most", "some": "some", "few": "a few"}[pair["level"]]
    return (f"{names} share {words} of their largest holdings ({pair['n_shared']} of "
            f"their top {pair['of']}).")


def look_through(holdings, tops: dict, total: float | None = None) -> dict:
    """What you own of each company once funds are looked through.

    holdings: [{symbol, name, kind ('fund'/'stock'/...), value}] - one per
    position; the same ticker in two accounts is added up. tops: cached().
    total: the whole portfolio's value (cash too) for the shares; defaults
    to the holdings' total.
    Returns {companies: [{key, symbol, name, value, pct, direct, via:
    [(fund, value)] biggest first}] biggest first, only those held directly
    or in a fund's top holdings; funds_seen: funds with top holdings;
    funds_missing: funds without; total}. A fund's holdings beyond its top
    ones aren't counted - so these are floors."""
    merged: dict = {}
    for h in holdings or []:
        sym = (h.get("symbol") or "").strip()
        if not sym:
            continue
        row = merged.setdefault(sym, {"symbol": sym, "name": h.get("name"),
                                      "kind": h.get("kind"), "value": 0.0})
        row["value"] += float(h.get("value") or 0.0)
        row["name"] = row["name"] or h.get("name")
    if total is None:
        total = sum(r["value"] for r in merged.values())
    companies: dict = {}

    def add(key, symbol, name):
        return companies.setdefault(key, {"key": key, "symbol": symbol, "name": name,
                                          "value": 0.0, "direct": 0.0, "via": {}})

    seen, missing = [], []
    for sym, h in sorted(merged.items()):
        if h["kind"] == "stock":
            c = add(_key(sym, h["name"]), sym, h["name"])
            c["direct"] += h["value"]
            c["value"] += h["value"]
        elif h["kind"] == "fund":
            listed = (tops.get(sym) or {}).get("holdings")
            if not listed:
                missing.append(sym)
                continue
            seen.append(sym)
            for k, (w, name) in _weights(listed).items():
                c = add(k, None if k.startswith("name:") else k, name)
                part = h["value"] * w
                c["via"][sym] = c["via"].get(sym, 0.0) + part
                c["value"] += part
    for c in companies.values():
        c["via"] = sorted(c["via"].items(), key=lambda kv: (-kv[1], kv[0]))
        c["pct"] = (c["value"] / total * 100) if total else None
    out = sorted(companies.values(), key=lambda c: (-c["value"], c["key"]))
    return {"companies": out, "funds_seen": seen, "funds_missing": missing, "total": total}


def through(company: dict) -> list[str]:
    """Where a company comes from: ['VTI', 'VOO', 'directly']."""
    return [f for f, _ in company["via"]] + (["directly"] if company["direct"] else [])
