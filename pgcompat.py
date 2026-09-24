"""Postgres compatibility shim so the existing SQLite-flavored query code
(portfolio.py, perf.py, update_prices.py, sync_history.py, watchlist.py,
news.py - all written against sqlite3's placeholder styles and sqlite3.Row)
works unchanged against Postgres too.

Only imported when portfolio.connect() is given a Postgres DSN instead of a
local file path - a plain local `streamlit run` never touches this module
or its `psycopg` dependency at all.

Three SQLite-specific things get translated in every query string before
it reaches psycopg (verified by grepping the whole codebase for each -
these are the only SQLite-specific constructs the query strings use):
  - `?` positional placeholders           -> psycopg's `%s`
  - `:name` named placeholders            -> psycopg's `%(name)s`
  - the literal fragment `datetime('now')` -> Postgres's `now()`
"""

from __future__ import annotations

import re

_DATETIME_NOW_RE = re.compile(r"datetime\(\s*'now'\s*\)")
_NAMED_RE = re.compile(r":(\w+)")
_QMARK_RE = re.compile(r"\?")

# SQLite's datetime('now') returns 'YYYY-MM-DD HH:MM:SS' - a fixed-width,
# space-separated, always-UTC string with no fractional seconds or offset.
# Every consumer of a fetched_at/imported_at/etc. column (e.g. news.py's
# needs_refresh, which does `strptime(row["m"], "%Y-%m-%d %H:%M:%S")`)
# depends on that *exact* shape. Postgres's now()::text instead produces
# something like '2026-09-23 21:15:03.123456+00' (fractional seconds, a
# timezone offset, and in the SESSION timezone, not necessarily UTC) - that
# would silently break every one of those parsers. to_char(... , format)
# with an explicit UTC conversion reproduces SQLite's exact string shape.
PG_NOW_EXPR = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"


def is_postgres_dsn(db_path: str) -> bool:
    return db_path.startswith("postgres://") or db_path.startswith("postgresql://")


def translate_sql(sql: str) -> str:
    """SQLite -> Postgres query text translation. Safe because the
    codebase never uses a literal `?` or `:word` inside a SQL string
    *value* - verified by grep; every occurrence is a real placeholder.

    Placeholder substitution runs BEFORE the datetime('now') substitution,
    not after: PG_NOW_EXPR itself contains literal `:MI:SS` (a to_char
    format string), which the named-placeholder regex would otherwise
    misparse as `:MI`/`:SS` placeholders on a second pass."""
    sql = _NAMED_RE.sub(r"%(\1)s", sql)
    sql = _QMARK_RE.sub("%s", sql)
    sql = _DATETIME_NOW_RE.sub(PG_NOW_EXPR, sql)
    return sql


class Row(tuple):
    """Mimics sqlite3.Row: a tuple (positional access AND unpacking both
    work, e.g. `for a, b, c in cursor:`) that ALSO supports string-key
    access (`row["col"]`) and `.keys()` (so `dict(row)` works) - the
    codebase uses all three patterns in different places."""

    # No __slots__: tuple's own fixed layout doesn't support non-empty
    # __slots__ on a subclass, so this instance just gets a plain __dict__
    # for the one extra attribute below.

    def __new__(cls, values, colnames):
        obj = super().__new__(cls, values)
        obj._colnames = tuple(colnames)
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._colnames.index(key))
        return tuple.__getitem__(self, key)

    def keys(self):
        return self._colnames


def _split_statements(sql_text: str) -> list[str]:
    """Split a schema script into individual statements. Strips `--` line
    comments before splitting - NOT just an edge-case precaution: the real
    schema_pg.sql header comment turned out to contain a literal semicolon
    in prose ("needs_refresh();"), which a naive whole-text split on ';'
    treated as a statement boundary and produced a garbage fragment
    starting mid-comment (caught by the smoke test against a real Neon
    instance, not by the unit tests, since it needed the actual multi-line
    file content). Otherwise still just a DDL splitter, not a general SQL
    parser - safe for our schema file (no semicolons inside string values,
    no block /* */ comments), not for arbitrary user SQL."""
    cleaned_lines = []
    for line in sql_text.split("\n"):
        idx = line.find("--")
        cleaned_lines.append(line[:idx] if idx != -1 else line)
    cleaned = "\n".join(cleaned_lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


class _CursorWrapper:
    def __init__(self, cur):
        self._cur = cur

    def _colnames(self):
        return [d.name for d in (self._cur.description or [])]

    def fetchone(self):
        row = self._cur.fetchone()
        return Row(row, self._colnames()) if row is not None else None

    def fetchall(self):
        cols = self._colnames()
        return [Row(r, cols) for r in self._cur.fetchall()]

    def __iter__(self):
        cols = self._colnames()
        for r in self._cur:
            yield Row(r, cols)

    @property
    def rowcount(self):
        return self._cur.rowcount


class ConnWrapper:
    """Stands in for a sqlite3.Connection. `.execute()`/`.executemany()`
    return something iterable/fetchable just like sqlite3's cursor does,
    since call sites do both `conn.execute(...).fetchone()` and
    `for row in conn.execute(...):` in different places.

    `pool` is set when this wraps a pooled connection (the normal case from
    `connect()` below) - `.close()` then returns it to the pool instead of
    tearing down the TCP/TLS connection, since dashboard.py's call sites all
    follow an open/use/close pattern per Streamlit script rerun, and a fresh
    handshake to a remote host (Neon) on every single one of those is where
    the deployed app's real per-click latency came from - confirmed live
    (every widget interaction reruns the whole script from the top, opening
    and closing up to 8 separate connections each time)."""

    def __init__(self, raw_conn, pool=None):
        self._conn = raw_conn
        self._pool = pool

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(translate_sql(sql), params)
        return _CursorWrapper(cur)

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(translate_sql(sql), list(seq_of_params))
        return _CursorWrapper(cur)

    def executescript(self, sql_text):
        cur = self._conn.cursor()
        for stmt in _split_statements(sql_text):
            cur.execute(stmt)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._pool is not None:
            # Roll back explicitly first: a read-only call site that never
            # called commit()/rollback() leaves the connection mid-
            # transaction (psycopg defaults to autocommit=False), and
            # without this the pool's own reset-on-return logs a "rolling
            # back returned connection" warning on every single putconn.
            #
            # But a pooled connection can go stale between checkouts - Neon
            # (or any server) can close an idle connection server-side, and
            # rollback() over that dead socket raises psycopg.OperationalError
            # (reproduced live: crashed the whole page with a traceback
            # through psycopg_binary's PGconn.socket getter). Swallow that
            # here rather than let a stale connection crash the request -
            # pool.putconn() below already checks the connection's own
            # broken/closed state and discards+replaces it instead of
            # reusing it, which is exactly what should happen here too.
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._pool.putconn(self._conn)
        else:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


# One pool per DSN per process, created lazily on first use. Streamlit
# Community Cloud runs this app as a single long-lived process (even across
# separate user sessions), so a module-level dict here persists for the
# process lifetime - exactly what a connection pool needs to actually
# amortize handshake cost across script reruns.
_POOLS: dict = {}


def connect(dsn: str) -> ConnWrapper:
    from psycopg_pool import ConnectionPool  # imported lazily - local SQLite usage never needs this installed
    pool = _POOLS.get(dsn)
    if pool is None:
        pool = ConnectionPool(dsn, min_size=1, max_size=5, kwargs={"autocommit": False}, open=True)
        _POOLS[dsn] = pool
    raw = pool.getconn()
    return ConnWrapper(raw, pool=pool)


def close_all_pools() -> None:
    """Shut down every pool opened by connect() in this process. The
    dashboard (a long-running server) never needs this - its pool lives for
    the process lifetime, checked out/returned per script rerun. But a
    one-shot CLI invocation (update_prices.py / sync_history.py, including
    every GitHub Actions scheduled-sync run) opens a pool, uses it once,
    and exits - without an explicit close() here, psycopg_pool's background
    worker threads don't stop before the process's normal exit, which hangs
    for ~5s and logs a "couldn't stop thread" warning on every single
    scheduled run. No-op if no Postgres DSN was ever connected (e.g. every
    local SQLite run)."""
    for pool in _POOLS.values():
        pool.close()
    _POOLS.clear()
