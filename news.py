"""Company news via Finnhub's free /company-news endpoint.

Standard library only, same request pattern as update_prices.py's Finnhub
client (urllib, a User-Agent header - Finnhub 401s without one). Cached in
the `news` table so opening a ticker's detail view doesn't re-fetch on every
rerun - only when that ticker's cache is stale or empty (see needs_refresh).
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
MAX_AGE_HOURS = 4    # how long a ticker's cached news is considered fresh
LOOKBACK_DAYS = 7    # how far back to pull headlines each sync


def fetch_company_news(symbol: str, token: str, *, days_back: int = LOOKBACK_DAYS,
                        timeout: float = 10.0):
    """Return (articles, error). Exactly one is truthy."""
    today = datetime.now(timezone.utc).date()
    frm = (today - timedelta(days=days_back)).isoformat()
    url = f"{FINNHUB_NEWS_URL}?" + urllib.parse.urlencode(
        {"symbol": symbol, "from": frm, "to": today.isoformat(), "token": token})
    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-tracker/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
        return (data if isinstance(data, list) else []), ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
        except Exception:
            pass
        return [], f"HTTP {exc.code}" + (f": {detail}" if detail else "")
    except urllib.error.URLError as exc:
        return [], f"network error: {exc.reason}"
    except TimeoutError:
        return [], "timeout"
    except json.JSONDecodeError:
        return [], "unparseable response"


def upsert_news(conn: sqlite3.Connection, ticker: str, articles: list[dict]) -> int:
    """Insert articles not already cached (by Finnhub's own id). Returns the
    number of rows actually inserted (re-synced duplicates don't count)."""
    rows = []
    for a in articles:
        aid = a.get("id")
        if aid is None or not a.get("headline"):
            continue
        dt = a.get("datetime")
        published = (datetime.fromtimestamp(dt, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if dt else None)
        rows.append({
            "id": aid, "ticker": ticker, "headline": a.get("headline"),
            "summary": a.get("summary"), "source": a.get("source"), "url": a.get("url"),
            "published_at": published,
        })
    if not rows:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM news WHERE ticker = ?", (ticker,)).fetchone()[0]
    conn.executemany(
        "INSERT INTO news (id, ticker, headline, summary, source, url, published_at) "
        "VALUES (:id, :ticker, :headline, :summary, :source, :url, :published_at) "
        "ON CONFLICT (id) DO NOTHING", rows)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM news WHERE ticker = ?", (ticker,)).fetchone()[0]
    return after - before


def needs_refresh(conn: sqlite3.Connection, ticker: str, *,
                   max_age_hours: float = MAX_AGE_HOURS) -> bool:
    row = conn.execute("SELECT MAX(fetched_at) m FROM news WHERE ticker = ?", (ticker,)).fetchone()
    if not row or not row["m"]:
        return True
    fetched = datetime.strptime(row["m"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched).total_seconds() > max_age_hours * 3600


def latest_news(conn: sqlite3.Connection, ticker: str, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM news WHERE ticker = ? ORDER BY published_at DESC LIMIT ?",
        (ticker, limit)).fetchall()
    return [dict(r) for r in rows]


def sync_ticker(conn: sqlite3.Connection, ticker: str, token: str, *,
                 force: bool = False) -> tuple[int, str]:
    """Fetch + cache news for `ticker` if its cache is stale (or `force`).
    Returns (n_new_articles, error) - skips the network call entirely
    (returns (0, "")) when the cache is already fresh."""
    if not force and not needs_refresh(conn, ticker):
        return 0, ""
    articles, err = fetch_company_news(ticker, token)
    if err:
        return 0, err
    return upsert_news(conn, ticker, articles), ""
