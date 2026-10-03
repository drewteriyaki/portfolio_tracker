"""Unit tests for the pure logic modules. Standard library only.

    python -m unittest discover -s tests        (from the repo root)
"""

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
import unittest.mock
from datetime import date, datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import accounts  # noqa: E402
import advising  # noqa: E402
import advisor  # noqa: E402
import alerts  # noqa: E402
import allocation  # noqa: E402
import auth  # noqa: E402
import manage_users  # noqa: E402
import changes  # noqa: E402
import charts  # noqa: E402
import learn  # noqa: E402
import client_plan  # noqa: E402
import metrics as M  # noqa: E402
import pandas as pd  # noqa: E402
import perf  # noqa: E402
import portfolio  # noqa: E402
import sync_history  # noqa: E402
import update_prices  # noqa: E402
import watchlist  # noqa: E402
import news  # noqa: E402
import overview  # noqa: E402
import pgcompat  # noqa: E402
import plans  # noqa: E402
import prefs  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_positions.csv")


class TempDBMixin:
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pt_test_")
        self.db = os.path.join(self.dir, "test.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(self.db))
        self.user_id = auth.create_user(portfolio.connect(self.db), "testuser", "testpass")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class ParseCsvTests(unittest.TestCase):
    def test_shape(self):
        meta, rows, totals = portfolio.parse_csv(FIXTURE)
        self.assertEqual(meta["snapshot_date"], "2026-01-15")
        self.assertEqual(len(rows), 3)  # cash + totals rows excluded
        self.assertEqual({r["account"] for r in rows}, {"Individual ...111", "Individual ...222"})
        aaa = next(r for r in rows if r["symbol"] == "AAA")
        self.assertEqual(aaa["quantity"], 10.0)
        self.assertEqual(aaa["cost_basis"], 1000.0)
        self.assertEqual(aaa["market_value"], 1200.0)
        self.assertEqual(aaa["asset_type"], "Equity")
        self.assertEqual(totals["Individual ...111"]["cash_value"], 100.0)
        self.assertEqual(totals["Individual ...222"]["reported_gain"], -400.0)

    def test_currency_and_percent_parsing(self):
        self.assertEqual(portfolio.parse_num("$1,007.19"), 1007.19)
        self.assertEqual(portfolio.parse_num("-$290.04"), -290.04)
        self.assertEqual(portfolio.parse_num("($290.04)"), -290.04)
        self.assertEqual(portfolio.parse_num("-28.8%"), -28.8)
        self.assertIsNone(portfolio.parse_num("--"))
        self.assertIsNone(portfolio.parse_num("N/A"))


class ImportTests(TempDBMixin, unittest.TestCase):
    def _count(self, conn, table, **where):
        sql = f"SELECT COUNT(*) FROM {table}"
        args = ()
        if where:
            sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
            args = tuple(where.values())
        return conn.execute(sql, args).fetchone()[0]

    def test_import_verifies_and_is_idempotent(self):
        conn = portfolio.connect(self.db)
        info = portfolio.import_csv(conn, FIXTURE, self.user_id)
        self.assertEqual(info["snapshot_date"], "2026-01-15")
        self.assertEqual(info["n_positions"], 3)
        with contextlib.redirect_stdout(io.StringIO()):
            ok = portfolio.verify_snapshot(conn, "2026-01-15")
        self.assertTrue(ok)
        portfolio.import_csv(conn, FIXTURE, self.user_id)  # again
        self.assertEqual(self._count(conn, "positions"), 3)
        self.assertEqual(self._count(conn, "snapshots"), 1)
        conn.close()

    def test_replace_by_date_across_filenames(self):
        other = os.path.join(self.dir, "renamed_export.csv")
        shutil.copyfile(FIXTURE, other)
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        portfolio.import_csv(conn, other, self.user_id)  # same date, different path
        self.assertEqual(self._count(conn, "positions", snapshot_date="2026-01-15"), 3)
        self.assertEqual(self._count(conn, "snapshots"), 1)
        conn.close()


class ConnectTests(TempDBMixin, unittest.TestCase):
    def test_creates_all_tables(self):
        conn = portfolio.connect(self.db)
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(
            {"snapshots", "positions", "account_totals", "price_history", "transactions", "value_log"},
            names,
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(price_history)")}
        self.assertLessEqual({"day_open", "day_high", "day_low"}, cols)
        conn.close()


class SchemaSetupRaceTests(unittest.TestCase):
    """Right after a deploy several sessions connect at once. Setup must run
    once, not concurrently - on Postgres, racing CREATE TABLE IF NOT EXISTS
    raised psycopg.errors.UniqueViolation on the live app's first login."""

    def test_concurrent_connects_set_up_the_schema_once(self):
        import threading
        import time
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = os.path.join(tmp, "race.db")
        real, calls = portfolio._ensure_schema, []

        def slow_ensure(conn):
            calls.append(1)
            time.sleep(0.2)  # wide window for a second thread to slip in
            real(conn)

        errors = []

        def worker():
            try:
                portfolio.connect(db).close()  # SQLite: close in the opening thread
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with unittest.mock.patch.object(portfolio, "_ensure_schema", slow_ensure):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)

    def test_postgres_setup_takes_the_advisory_lock_first(self):
        executed = []

        class FakeCursor:
            description = None

            def execute(self, sql, params=None):
                executed.append((sql, params))

            def __iter__(self):
                return iter([])

            def fetchall(self):
                return []

        class FakeRaw:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                executed.append(("COMMIT", None))

        portfolio._ensure_schema(pgcompat.ConnWrapper(FakeRaw()))
        first_sql, first_params = executed[0]
        self.assertIn("pg_advisory_xact_lock", first_sql)
        self.assertIn("%s", first_sql)                      # placeholder translated for psycopg
        self.assertEqual(first_params, (portfolio.SCHEMA_ADVISORY_LOCK_ID,))
        self.assertEqual(executed[-1][0], "COMMIT")          # the lock is released by this commit
        self.assertTrue(any("CREATE TABLE" in sql for sql, _ in executed[1:]))


class DiffTests(unittest.TestCase):
    OLD = [
        {"account": "A", "symbol": "X", "description": "x", "quantity": 10, "cost_basis": 1000, "market_value": 1200},
        {"account": "A", "symbol": "Y", "description": "y", "quantity": 5, "cost_basis": 500, "market_value": 400},
        {"account": "A", "symbol": "Z", "description": "z", "quantity": 3, "cost_basis": 300, "market_value": 330},
    ]
    NEW = [
        {"account": "A", "symbol": "X", "description": "x", "quantity": 13, "cost_basis": 1300, "market_value": 1600},
        {"account": "A", "symbol": "Y", "description": "y", "quantity": 2, "cost_basis": 200, "market_value": 170},
        {"account": "A", "symbol": "W", "description": "w", "quantity": 7, "cost_basis": 700, "market_value": 720},
    ]

    def test_buckets(self):
        d = changes.diff_positions(self.OLD, self.NEW)
        self.assertEqual([e["symbol"] for e in d["new"]], ["W"])
        self.assertEqual([e["symbol"] for e in d["increased"]], ["X"])
        self.assertEqual([e["symbol"] for e in d["decreased"]], ["Y"])
        self.assertEqual([e["symbol"] for e in d["closed"]], ["Z"])
        self.assertEqual(d["unchanged"], [])

    def test_synthesized_transactions(self):
        d = changes.diff_positions(self.OLD, self.NEW)
        txns = {t["symbol"]: t for t in changes.synthesize_transactions(d, "2026-02-01", "f.csv")}
        self.assertEqual(txns["W"]["action"], "BUY")
        self.assertEqual(txns["W"]["amount"], -700.0)          # cash out
        self.assertEqual(txns["X"]["action"], "BUY")
        self.assertEqual(txns["X"]["quantity"], 3)             # the delta only
        self.assertEqual(txns["X"]["amount"], -300.0)
        self.assertEqual(txns["Y"]["action"], "SELL")
        self.assertEqual(txns["Y"]["quantity"], 3)
        self.assertGreater(txns["Y"]["amount"], 0)             # cash in
        self.assertEqual(txns["Z"]["action"], "SELL")
        self.assertEqual(txns["Z"]["amount"], 330.0)
        # realized_gain: average-cost method, SELL only
        self.assertIsNone(txns["W"]["realized_gain"])          # BUY -> not applicable
        self.assertIsNone(txns["X"]["realized_gain"])          # BUY -> not applicable
        # Y: sold 3 of 5 @ avg cost 100/share ($300 basis removed) for $255 -> -$45
        self.assertAlmostEqual(txns["Y"]["realized_gain"], -45.0)
        # Z: closed entirely, $330 proceeds - $300 cost basis -> +$30
        self.assertAlmostEqual(txns["Z"]["realized_gain"], 30.0)


class AllocationTests(unittest.TestCase):
    POS = [
        {"account": "A", "symbol": "X", "asset_type": "Equity", "market_value": 2000, "live_market_value": None},
        {"account": "A", "symbol": "Y", "asset_type": "ETFs & Closed End Funds", "market_value": 1000, "live_market_value": 1200},
        {"account": "B", "symbol": "Z", "asset_type": "Equity", "market_value": 500, "live_market_value": None},
    ]

    def test_breakdown_and_concentration(self):
        r = allocation.allocate(self.POS, {"A": 300, "B": 0})
        self.assertEqual(r["portfolio_value"], 2000 + 1200 + 500 + 300)
        self.assertEqual(r["by_asset_type"][0]["label"], "Equity")       # 2500, largest
        self.assertTrue(any(x["label"] == "Cash" for x in r["by_asset_type"]))
        self.assertEqual(r["by_asset_type"][1]["label"], "ETF / CEF")    # short label
        self.assertEqual(r["concentration"][0]["symbol"], "X")           # 2000/4000 = 50%
        self.assertTrue(all(c["pct"] > allocation.CONCENTRATION_PCT for c in r["concentration"]))


class AlertTests(unittest.TestCase):
    @staticmethod
    def _ctx(sym, day_pct, gl_pct):
        return {"pos": {"symbol": sym, "account": "A", "day_change_pct": day_pct,
                        "cost_basis": 100, "market_value": 100 + gl_pct},
                "quote": {}, "port_value": 1000, "acct_value": 1000}

    def test_default_rules(self):
        rows = [self._ctx("AAA", -6.0, -25), self._ctx("BBB", 2.0, -5),
                self._ctx("CCC", 7.5, 30), self._ctx("DDD", -1.0, -21)]
        fired = {(a.symbol, a.rule_key) for a in alerts.evaluate(rows)}
        self.assertIn(("AAA", "day_move"), fired)
        self.assertIn(("AAA", "total_gl"), fired)
        self.assertIn(("CCC", "day_move"), fired)
        self.assertIn(("DDD", "total_gl"), fired)
        self.assertNotIn(("DDD", "day_move"), fired)
        self.assertNotIn(("BBB", "day_move"), fired)
        self.assertNotIn(("BBB", "total_gl"), fired)

    def test_sorted_worst_first_and_custom_threshold(self):
        rows = [self._ctx("AAA", -6.0, -25), self._ctx("CCC", 7.5, 30)]
        fired = alerts.evaluate(rows)
        self.assertEqual([abs(a.value) for a in fired], sorted((abs(a.value) for a in fired), reverse=True))
        loose = alerts.evaluate(rows, [{**alerts.DEFAULT_RULES[0], "abs_gt": 1.0}])
        self.assertEqual({a.rule_key for a in loose}, {"day_move"})


class MetricsTests(unittest.TestCase):
    def test_effective_price_and_mv_fallbacks(self):
        live = {"pos": {"live_price": 50.0, "live_market_value": 500.0, "quantity": 10,
                        "market_value": 480, "cost_basis": 400}, "quote": {}}
        self.assertEqual(M.eff_price(live), 50.0)
        self.assertEqual(M.eff_mv(live), 500.0)
        quote_only = {"pos": {"quantity": 10, "market_value": 480, "cost_basis": 400},
                      "quote": {"price": 49.0}}
        self.assertEqual(M.eff_price(quote_only), 49.0)
        self.assertEqual(M.eff_mv(quote_only), 490.0)
        csv_only = {"pos": {"quantity": 10, "market_value": 480, "cost_basis": 400}, "quote": {}}
        self.assertEqual(M.eff_price(csv_only), 48.0)
        self.assertEqual(M.eff_mv(csv_only), 480.0)
        # no position at all (a watchlist ticker) - falls back to the last
        # known Yahoo daily close instead of returning None
        watchlist_only = {"pos": {}, "quote": {}, "stats": {"last_close": 123.45}}
        self.assertEqual(M.eff_price(watchlist_only), 123.45)
        self.assertIsNone(M.eff_price({"pos": {}, "quote": {}, "stats": {}}))

    def test_derived_values_and_safety(self):
        ctx = {"pos": {"symbol": "X", "quantity": 10, "cost_basis": 400, "market_value": 480,
                       "live_price": 50.0, "live_market_value": 500.0},
               "quote": {"pct_change": 1.5, "change": 0.75, "day_high": 51.0},
               "port_value": 2000, "acct_value": 1000}
        self.assertAlmostEqual(M.value("unrealized_usd", ctx), 100.0)
        self.assertAlmostEqual(M.value("unrealized_pct", ctx), 25.0)
        self.assertAlmostEqual(M.value("day_change_usd", ctx), 7.5)
        self.assertAlmostEqual(M.value("pct_of_portfolio", ctx), 25.0)
        self.assertIsNone(M.value("ma_50", ctx))                 # registered but unavailable
        self.assertIsNone(M.value("not_a_metric", {}))           # unknown key -> None, no raise
        self.assertIsNone(M.value("unrealized_usd", {"pos": {}, "quote": {}}))  # missing data -> None

    def test_levels_and_changes(self):
        """A level (a yield, a share of the portfolio) is "pct_level", shown
        without a sign; a change keeps "pct" and its sign. Alerts treat both
        as percents."""
        for key in ("pct_of_portfolio", "pct_of_account", "pct_of_account_csv", "div_yield_pct"):
            self.assertEqual(M.BY_KEY[key].fmt, "pct_level", key)
        for key in ("day_change_pct", "price_change_pct", "unrealized_pct", "unrealized_csv_pct",
                    "price_vs_ma50", "pct_off_high", "pct_off_52wk_high"):
            self.assertEqual(M.BY_KEY[key].fmt, "pct", key)
        ctx = {"pos": {"symbol": "X", "account": "A", "market_value": 900}, "quote": {},
               "port_value": 1000, "acct_value": 1000}
        fired = alerts.evaluate([ctx], [{"key": "big", "metric": "pct_of_portfolio", "abs_gt": 50}])
        self.assertEqual([a.is_pct for a in fired], [True])


class PerfTests(TempDBMixin, unittest.TestCase):
    AGG = {"snapshot_date": "2026-01-15", "portfolio_value": 3400.0, "holdings_value": 3250.0,
           "cash": 150.0, "cost_basis": 3500.0, "unrealized_gain": -250.0,
           "unrealized_gain_pct": -7.14, "day_change_usd": -12.0, "n_positions": 3,
           "n_priced": 3, "priced_at": "2026-01-15T21:00:00Z"}

    def test_log_open_throttle_and_history(self):
        portfolio.connect(self.db).close()  # create schema
        self.assertTrue(perf.log_open(self.db, self.user_id, self.AGG))
        self.assertFalse(perf.log_open(self.db, self.user_id, self.AGG))          # within the gap
        self.assertTrue(perf.log_open(self.db, self.user_id, self.AGG, min_gap_sec=0))
        # snapshot rows come from positions; add one via import
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        conn.close()
        # snapshots are opt-in; the default performance line is app_open (+ reconstructed)
        self.assertEqual({r["source"] for r in perf.history(self.db, self.user_id)}, {"app_open"})
        hist = perf.history(self.db, self.user_id, include_snapshots=True)
        self.assertEqual({r["source"] for r in hist}, {"snapshot", "app_open"})
        snap_row = next(r for r in hist if r["source"] == "snapshot")
        self.assertEqual(snap_row["source_label"], perf.SOURCE_LABEL["snapshot"])
        self.assertAlmostEqual(snap_row["portfolio_value"], 3400.0, places=2)

    def test_last_open_returns_most_recent_row(self):
        # Callers must call last_open() BEFORE log_open() writes the current
        # session's own row, or "since you last opened" just compares the
        # portfolio to itself - last_open() itself has no special-casing for
        # that, it always just returns the newest app_open row.
        portfolio.connect(self.db).close()
        self.assertIsNone(perf.last_open(self.db, self.user_id))          # never opened before
        perf.log_open(self.db, self.user_id, self.AGG, min_gap_sec=0)
        first = perf.last_open(self.db, self.user_id)
        self.assertAlmostEqual(first["portfolio_value"], 3400.0, places=2)
        newer_agg = {**self.AGG, "portfolio_value": 5000.0}
        perf.log_open(self.db, self.user_id, newer_agg, min_gap_sec=0)
        second = perf.last_open(self.db, self.user_id)
        self.assertAlmostEqual(second["portfolio_value"], 5000.0, places=2)


class PerfBarsTests(TempDBMixin, unittest.TestCase):
    def _seed(self):
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)  # AAA qty10 cost1000, BBB qty5 cost500, CCC qty2 cost2000
        bars = []
        # 60 trading days; AAA rises 100->? , CCC flat, BBB only has 30 days (recent listing)
        for i in range(60):
            date = f"2026-01-{i + 1:02d}" if i < 31 else f"2026-02-{i - 30:02d}"
            bars.append(("AAA", date, 100.0 + i))
            bars.append(("CCC", date, 800.0))
            if i >= 30:
                bars.append(("BBB", date, 90.0))
        conn.executemany(
            "INSERT INTO daily_bars (ticker, date, close, volume) VALUES (?, ?, ?, 1000)", bars)
        conn.commit()
        conn.close()

    def test_ticker_bars_moving_averages(self):
        self._seed()
        rows = perf.ticker_bars(self.db, "AAA")
        self.assertEqual(len(rows), 60)
        self.assertIsNone(rows[18]["ma_20"])                 # < 20 bars
        self.assertAlmostEqual(rows[19]["ma_20"], sum(100.0 + j for j in range(20)) / 20)
        self.assertIsNone(rows[59]["ma_200"])                # never enough for 200

    def test_bar_stats(self):
        self._seed()
        s = perf.bar_stats(self.db)["AAA"]
        self.assertEqual(s["last_close"], 159.0)             # 100 + 59
        self.assertEqual(s["volume"], 1000)
        self.assertAlmostEqual(s["ma_20"], sum(100.0 + j for j in range(40, 60)) / 20)

    def test_coverage_and_reconstruction(self):
        self._seed()
        covered, missing = perf.holdings_coverage(self.db, self.user_id)
        self.assertEqual(set(covered), {"AAA", "BBB", "CCC"})
        self.assertEqual(missing, [])
        hist = perf.history(self.db, self.user_id)
        rec = [r for r in hist if r["source"] == "reconstructed"]
        self.assertEqual(len(rec), 60)
        self.assertEqual(rec[0]["n_positions"], 2)           # BBB not listed yet
        self.assertEqual(rec[-1]["n_positions"], 3)
        # last day: AAA 10*159 + BBB 5*90 + CCC 2*800 + cash(150) = 1590+450+1600+150
        self.assertAlmostEqual(rec[-1]["portfolio_value"], 1590 + 450 + 1600 + 150, places=2)

    def test_reconstruction_forward_fills_asynchronous_tickers(self):
        # AAA and CCC don't report a bar at the same instant every minute -
        # a naive "exact timestamp match" sum would make the portfolio value
        # swing based on which ticker happened to report, not on real moves.
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)  # AAA qty10 cost1000, CCC qty2 cost2000
        now = datetime.now(timezone.utc).replace(microsecond=0)
        t0 = (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t1 = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t2 = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.executemany(
            "INSERT INTO intraday_bars (ticker, interval, ts, close, volume) VALUES (?,?,?,?,1000)", [
                ("AAA", "1m", t0, 100.0),
                ("CCC", "1m", t1, 800.0),   # AAA silent this minute
                ("AAA", "1m", t2, 102.0),   # CCC silent this minute
            ])
        conn.commit()
        conn.close()

        hist = perf.history(self.db, self.user_id, days=1)
        rec = [r for r in hist if r["source"] == "reconstructed"]
        self.assertEqual([r["t"] for r in rec], [t0, t1, t2])
        # 14:30: only AAA has reported anything yet -> partial (matches
        # "a newly-listed holding joins once its bars begin")
        self.assertEqual(rec[0]["n_positions"], 1)
        self.assertAlmostEqual(rec[0]["holdings_value"], 10 * 100.0)
        # 14:31: AAA's 14:30 close carries forward (not dropped) + CCC's fresh 800
        self.assertEqual(rec[1]["n_positions"], 2)
        self.assertAlmostEqual(rec[1]["holdings_value"], 10 * 100.0 + 2 * 800.0)
        # 14:32: AAA's fresh 102 + CCC's 14:31 close carries forward (not dropped)
        self.assertEqual(rec[2]["n_positions"], 2)
        self.assertAlmostEqual(rec[2]["holdings_value"], 10 * 102.0 + 2 * 800.0)

    def test_history_empty_without_data(self):
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        conn.close()
        self.assertFalse(perf.has_bars(self.db))
        self.assertEqual(perf.history(self.db, self.user_id), [])                       # nothing to plot yet
        self.assertEqual({r["source"] for r in perf.history(self.db, self.user_id, include_snapshots=True)},
                         {"snapshot"})


class IntradayTests(TempDBMixin, unittest.TestCase):
    def _seed(self):
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)  # AAA, BBB, CCC
        now = datetime.now(timezone.utc)

        def stamp(minutes_ago):
            return (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

        rows_1m = [("AAA", "1m", stamp(m), 100.0 + m) for m in range(10, 0, -1)]     # last 10 min
        rows_5m = [("AAA", "5m", stamp(m), 100.0 + m) for m in range(4000, 0, -5)]   # spans >7d
        rows_60m = [("AAA", "60m", stamp(m), 100.0 + m) for m in range(90000, 0, -60)]  # spans >60d
        conn.executemany(
            "INSERT INTO intraday_bars (ticker, interval, ts, close, volume) VALUES (?,?,?,?,1000)",
            rows_1m + rows_5m + rows_60m)
        conn.executemany(
            "INSERT INTO daily_bars (ticker, date, close, volume) VALUES (?,?,?,1000)",
            [("AAA", f"2020-01-{d:02d}", 50.0 + d) for d in range(1, 29)])
        conn.commit()
        conn.close()

    def test_intervals_for_thresholds(self):
        self.assertEqual(perf._intervals_for(1), ["1m", "5m", "15m", "60m", "1d"])
        self.assertEqual(perf._intervals_for(7), ["1m", "5m", "15m", "60m", "1d"])
        self.assertEqual(perf._intervals_for(8), ["5m", "15m", "60m", "1d"])
        self.assertEqual(perf._intervals_for(60), ["5m", "15m", "60m", "1d"])
        self.assertEqual(perf._intervals_for(61), ["60m", "1d"])
        self.assertEqual(perf._intervals_for(730), ["60m", "1d"])
        self.assertEqual(perf._intervals_for(731), ["1d"])
        self.assertEqual(perf._intervals_for(None), ["1d"])

    def test_ticker_series_picks_finest_available_resolution(self):
        self._seed()
        rows, interval = perf.ticker_series(self.db, "AAA", 1)      # "1D" -> the 1m bars
        self.assertEqual(interval, "1m")
        self.assertEqual(len(rows), 10)

        rows, interval = perf.ticker_series(self.db, "AAA", 30)     # 1m/15m don't cover -> 5m
        self.assertEqual(interval, "5m")

        rows, interval = perf.ticker_series(self.db, "AAA", 365)    # only 60m covers a year
        self.assertEqual(interval, "60m")

        rows, interval = perf.ticker_series(self.db, "AAA", None)   # "All" -> daily
        self.assertEqual(interval, "1d")
        self.assertEqual(len(rows), 28)
        self.assertIn("ma_20", rows[0])                             # daily rows carry MA cols

    def test_ticker_series_falls_back_when_no_intraday(self):
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        conn.executemany(
            "INSERT INTO daily_bars (ticker, date, close, volume) VALUES (?,?,?,1000)",
            [("AAA", f"2020-01-{d:02d}", 50.0 + d) for d in range(1, 5)])
        conn.commit()
        conn.close()
        rows, interval = perf.ticker_series(self.db, "AAA", 1)      # no intraday synced
        self.assertEqual(interval, "1d")
        self.assertEqual(len(rows), 2)                              # last-2 fallback, not empty

    def test_value_chart_picks_finest_interval_with_points(self):
        self._seed()
        rec = lambda days: [r for r in perf.history(self.db, self.user_id, days=days)
                            if r["source"] == "reconstructed"]
        self.assertEqual(len(rec(1)), 10)                           # the 1m bars
        self.assertEqual(len(rec(30)), 800)                         # 1m/15m too short -> 5m
        conn = portfolio.connect(self.db)
        conn.execute("DELETE FROM intraday_bars WHERE interval = '1m' AND ts < ?",
                     ((datetime.now(timezone.utc) - timedelta(minutes=1, seconds=30))
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),))
        conn.commit()
        conn.close()
        day = rec(1)                                                # one 1m point left -> 5m
        # a day of 5-minute bars: 288, or 287 when the clock has moved past the
        # bar seeded exactly 24 hours back (slow runners, e.g. GitHub's)
        self.assertIn(len(day), (287, 288))

    def test_same_fund_in_two_accounts_counts_both(self):
        conn = portfolio.connect(self.db)
        meta = {"snapshot_date": "2026-01-02", "as_of_text": ""}
        rows = [{"snapshot_date": "2026-01-02", "account": acct, "symbol": "AAA",
                 "quantity": q, "cost_basis": c, "market_value": q * 100}
                for acct, q, c in (("IRA", 3.0, 300.0), ("Brokerage", 7.0, 650.0))]
        portfolio.write_snapshot(conn, self.user_id, meta, rows,
                                 {"IRA": {"cash_value": 20.0, "reported_cost_basis": None,
                                          "reported_market_value": None, "reported_gain": None,
                                          "reported_gain_pct": None}}, "test")
        conn.executemany("INSERT INTO daily_bars (ticker, date, close, volume) VALUES (?,?,?,1)",
                         [("AAA", "2026-01-01", 100.0), ("AAA", "2026-01-02", 110.0)])
        conn.commit()
        loaded = [dict(r) for r in conn.execute("SELECT * FROM positions WHERE user_id = ?",
                                                (self.user_id,))]
        self.assertEqual(perf._basis(conn, self.user_id),
                         ("2026-01-02", {"AAA": (10.0, 950.0)}, 20.0))
        conn.close()
        basis = perf.basis_of("2026-01-02", loaded, {"IRA": 20.0})
        self.assertEqual(basis, ("2026-01-02", {"AAA": (10.0, 950.0)}, 20.0))
        last = perf.history(self.db, self.user_id, basis=basis)[-1]
        self.assertAlmostEqual(last["portfolio_value"], 10 * 110.0 + 20.0)
        self.assertEqual(perf.holdings_coverage(self.db, self.user_id, basis), (["AAA"], []))

    def test_ticker_has_bars_and_has_intraday(self):
        self.assertFalse(perf.has_intraday(self.db))
        self._seed()
        self.assertTrue(perf.has_intraday(self.db))
        self.assertTrue(perf.ticker_has_bars(self.db, "AAA"))
        self.assertFalse(perf.ticker_has_bars(self.db, "ZZZ"))


class SyncHistoryUnitTests(TempDBMixin, unittest.TestCase):
    """Pure/storage-layer tests only - no network calls."""

    def test_num_coerces_and_drops_nan(self):
        self.assertEqual(sync_history._num("1.5"), 1.5)
        self.assertIsNone(sync_history._num(None))
        self.assertIsNone(sync_history._num(float("nan")))
        self.assertIsNone(sync_history._num("not a number"))

    def test_upsert_intraday_idempotent(self):
        conn = portfolio.connect(self.db)
        rows = [{"ts": "2026-01-01T14:30:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                "volume": 100}]
        sync_history.upsert_intraday(conn, "AAA", "1m", rows)
        sync_history.upsert_intraday(conn, "AAA", "1m", rows)  # re-sync same bar
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0]
        self.assertEqual(n, 1)
        updated = [{**rows[0], "close": 9.9}]
        sync_history.upsert_intraday(conn, "AAA", "1m", updated)
        conn.commit()
        close = conn.execute("SELECT close FROM intraday_bars").fetchone()[0]
        self.assertEqual(close, 9.9)                                # updates in place
        conn.close()


class _ConnectReached(Exception):
    """Raised by a patched connect() to prove the isfile guard let a
    Postgres DSN through without a network call actually happening."""


class CliPostgresDsnGuardTests(unittest.TestCase):
    """update_prices.py and sync_history.py both gate their --db argument on
    os.path.isfile() before connecting - correct for a local SQLite path,
    but a Postgres DSN (e.g. from the GitHub Actions scheduled-sync
    workflow's DATABASE_URL secret) is never a real file on disk, so a
    naive isfile() check would reject every hosted-deploy run with a
    misleading "No database at ..." error before ever reaching connect().
    Caught while wiring up the scheduled-sync workflow, before it ever ran
    in CI - not by these tests failing first, but these lock the fix in."""

    DSN = "postgresql://user:pw@example.neon.tech/neondb?sslmode=require"

    def test_update_prices_skips_isfile_check_for_postgres_dsn(self):
        with unittest.mock.patch.object(update_prices, "connect",
                                         side_effect=_ConnectReached):
            with self.assertRaises(_ConnectReached):
                update_prices.main(["--db", self.DSN, "--key", "dummy", "--user", "testuser"])

    def test_sync_history_skips_isfile_check_for_postgres_dsn(self):
        with unittest.mock.patch.object(sync_history, "connect",
                                         side_effect=_ConnectReached):
            with self.assertRaises(_ConnectReached):
                sync_history.main(["--db", self.DSN, "--no-info", "--no-intraday"])


class LoginLockoutTests(TempDBMixin, unittest.TestCase):
    T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    def _fail(self, conn, n, username="testuser", start=None):
        t = start or self.T0
        for i in range(n):
            r = auth.attempt_login(conn, username, "wrong", now=t + timedelta(seconds=i))
        return r

    def test_locks_after_the_limit_even_for_the_right_password(self):
        conn = portfolio.connect(self.db)
        r = self._fail(conn, auth.MAX_FAILED_LOGINS - 1)
        self.assertEqual((r["locked_minutes"], r["attempts_left"]), (0, 1))
        r = self._fail(conn, 1, start=self.T0 + timedelta(minutes=1))
        self.assertEqual(r["locked_minutes"], auth.LOCKOUT_MINUTES)
        ok = auth.attempt_login(conn, "testuser", "testpass", now=self.T0 + timedelta(minutes=2))
        self.assertIsNone(ok["user_id"])                               # locked: password not checked
        self.assertGreater(ok["locked_minutes"], 0)
        later = self.T0 + timedelta(minutes=1 + auth.LOCKOUT_MINUTES, seconds=5)
        ok = auth.attempt_login(conn, "testuser", "testpass", now=later)
        self.assertEqual(ok["user_id"], self.user_id)                  # lock ran out
        conn.close()

    def test_success_resets_and_old_failures_expire(self):
        conn = portfolio.connect(self.db)
        self._fail(conn, auth.MAX_FAILED_LOGINS - 1)
        self.assertEqual(auth.attempt_login(conn, "testuser", "testpass", now=self.T0 + timedelta(minutes=1))
                         ["user_id"], self.user_id)
        r = self._fail(conn, 1, start=self.T0 + timedelta(minutes=2))
        self.assertEqual(r["attempts_left"], auth.MAX_FAILED_LOGINS - 1)  # count started over
        # failures spread past the window never add up to a lock
        conn2 = portfolio.connect(self.db)
        for i in range(auth.MAX_FAILED_LOGINS + 2):
            r = auth.attempt_login(conn2, "slowguesser", "x",
                                   now=self.T0 + timedelta(minutes=(auth.LOCKOUT_MINUTES + 1) * i))
        self.assertEqual(r["locked_minutes"], 0)
        conn2.close()
        conn.close()

    def test_unknown_usernames_lock_the_same_and_case_is_ignored(self):
        conn = portfolio.connect(self.db)
        r = self._fail(conn, auth.MAX_FAILED_LOGINS, username="nobody")
        self.assertEqual(r["locked_minutes"], auth.LOCKOUT_MINUTES)   # same as a real account
        self._fail(conn, auth.MAX_FAILED_LOGINS - 1, username="TestUser")
        r = auth.attempt_login(conn, "testuser", "nope", now=self.T0 + timedelta(seconds=30))
        self.assertEqual(r["locked_minutes"], auth.LOCKOUT_MINUTES)   # 'TestUser' counted too
        keys = [row["username_key"] for row in conn.execute("SELECT username_key FROM login_failures")]
        self.assertTrue(all(len(k) == 64 and "user" not in k for k in keys))  # hashed, not stored
        conn.close()

    def test_password_change_and_unlock_clear_a_lock(self):
        conn = portfolio.connect(self.db)
        self._fail(conn, auth.MAX_FAILED_LOGINS)
        auth.set_password(conn, "testuser", "new-password")
        self.assertEqual(auth.attempt_login(conn, "testuser", "new-password",
                                            now=self.T0 + timedelta(minutes=1))["user_id"], self.user_id)
        self._fail(conn, auth.MAX_FAILED_LOGINS, start=self.T0 + timedelta(minutes=2))
        self.assertTrue(auth.unlock_login(conn, "testuser"))
        self.assertFalse(auth.unlock_login(conn, "testuser"))
        self.assertEqual(auth.attempt_login(conn, "testuser", "new-password",
                                            now=self.T0 + timedelta(minutes=3))["user_id"], self.user_id)
        conn.close()


class ChangePasswordTests(TempDBMixin, unittest.TestCase):
    NEW = "brand-new-pass"

    def test_change_ends_other_sessions_and_keeps_this_one(self):
        conn = portfolio.connect(self.db)
        other = auth.create_session(conn, self.user_id)
        before = auth.password_stamp(conn, self.user_id)
        r = auth.change_password(conn, self.user_id, "testpass", self.NEW, keep_session=True)
        self.assertTrue(r["ok"])
        self.assertIsNone(auth.verify_login(conn, "testuser", "testpass"))
        self.assertEqual(auth.verify_login(conn, "testuser", self.NEW), self.user_id)
        self.assertIsNone(auth.session_user(conn, other))                 # other device signed out
        self.assertEqual(auth.session_user(conn, r["token"]), (self.user_id, "testuser"))
        self.assertNotEqual(auth.password_stamp(conn, self.user_id), before)  # open tabs notice
        self.assertIsNone(auth.change_password(conn, self.user_id, self.NEW, "another-pass")["token"])
        conn.close()

    def test_rejections_leave_the_password_alone(self):
        conn = portfolio.connect(self.db)
        for current, new in (("testpass", "short"), ("wrong", self.NEW), ("testpass", "testpass")):
            r = auth.change_password(conn, self.user_id, current, new)
            self.assertFalse(r["ok"])
            self.assertTrue(r["error"])
        self.assertEqual(auth.verify_login(conn, "testuser", "testpass"), self.user_id)
        # "must be different" only once the current password is right, so it
        # can't be used to confirm a guess
        self.assertIn("wrong", auth.change_password(conn, self.user_id, "guess-123", "guess-123")["error"])
        conn.close()

    def test_wrong_current_passwords_hit_the_login_lockout(self):
        conn = portfolio.connect(self.db)
        for _ in range(auth.MAX_FAILED_LOGINS):
            auth.change_password(conn, self.user_id, "wrong", self.NEW)
        r = auth.change_password(conn, self.user_id, "testpass", self.NEW)
        self.assertFalse(r["ok"])
        self.assertIn("Too many", r["error"])
        self.assertGreater(auth.attempt_login(conn, "testuser", "testpass")["locked_minutes"], 0)
        conn.close()

    def test_stamp_is_none_for_a_missing_account(self):
        conn = portfolio.connect(self.db)
        self.assertIsNone(auth.password_stamp(conn, 999999))
        self.assertEqual(len(auth.password_stamp(conn, self.user_id)), 16)
        conn.close()


class StaySignedInTests(TempDBMixin, unittest.TestCase):
    def test_token_signs_in_until_it_expires(self):
        conn = portfolio.connect(self.db)
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        token = auth.create_session(conn, self.user_id, now=t0)
        self.assertEqual(auth.session_user(conn, token, now=t0), (self.user_id, "testuser"))
        late = t0 + timedelta(days=auth.SESSION_DAYS, seconds=1)
        self.assertIsNone(auth.session_user(conn, token, now=late))
        self.assertIsNone(auth.session_user(conn, "not-a-real-token", now=t0))
        self.assertIsNone(auth.session_user(conn, None))
        conn.close()

    def test_only_a_hash_of_the_token_is_stored(self):
        conn = portfolio.connect(self.db)
        token = auth.create_session(conn, self.user_id)
        stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM login_sessions")]
        self.assertEqual(len(stored), 1)
        self.assertNotIn(token, stored[0])
        conn.close()

    def test_logout_ends_only_that_session(self):
        conn = portfolio.connect(self.db)
        phone, laptop = auth.create_session(conn, self.user_id), auth.create_session(conn, self.user_id)
        auth.end_session(conn, phone)
        self.assertIsNone(auth.session_user(conn, phone))
        self.assertEqual(auth.session_user(conn, laptop), (self.user_id, "testuser"))
        conn.close()

    def test_password_change_signs_out_everywhere(self):
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "other", "pw-other")
        mine, theirs = auth.create_session(conn, self.user_id), auth.create_session(conn, other)
        auth.set_password(conn, "testuser", "a-new-password")
        self.assertIsNone(auth.session_user(conn, mine))
        self.assertEqual(auth.session_user(conn, theirs), (other, "other"))  # other users unaffected
        conn.close()

    def test_new_session_clears_expired_ones(self):
        conn = portfolio.connect(self.db)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        auth.create_session(conn, self.user_id, now=t0)
        auth.create_session(conn, self.user_id, now=t0 + timedelta(days=auth.SESSION_DAYS + 1))
        n = conn.execute("SELECT COUNT(*) AS n FROM login_sessions").fetchone()["n"]
        self.assertEqual(n, 1)
        conn.close()


class AuthTests(TempDBMixin, unittest.TestCase):
    """TempDBMixin already created one user ('testuser'/'testpass', id
    self.user_id) via auth.create_user() in setUp - these tests exercise
    the rest of the auth surface directly."""

    def test_verify_login_round_trip(self):
        conn = portfolio.connect(self.db)
        self.assertEqual(auth.verify_login(conn, "testuser", "testpass"), self.user_id)

    def test_verify_login_rejects_wrong_password(self):
        conn = portfolio.connect(self.db)
        self.assertIsNone(auth.verify_login(conn, "testuser", "wrongpass"))

    def test_verify_login_rejects_unknown_username(self):
        conn = portfolio.connect(self.db)
        self.assertIsNone(auth.verify_login(conn, "nosuchuser", "whatever"))

    def test_duplicate_username_rejected(self):
        conn = portfolio.connect(self.db)
        with self.assertRaises(portfolio.DBError):
            auth.create_user(conn, "testuser", "anotherpass")

    def test_set_password_changes_login(self):
        conn = portfolio.connect(self.db)
        self.assertTrue(auth.set_password(conn, "testuser", "newpass"))
        self.assertIsNone(auth.verify_login(conn, "testuser", "testpass"))     # old password now rejected
        self.assertEqual(auth.verify_login(conn, "testuser", "newpass"), self.user_id)

    def test_set_password_unknown_user_returns_false(self):
        conn = portfolio.connect(self.db)
        self.assertFalse(auth.set_password(conn, "nosuchuser", "whatever"))

    def test_two_users_never_see_each_others_data(self):
        """The core multi-tenancy guarantee: create a second account in the
        same database, import a DIFFERENT csv for it, and confirm every
        user-owned read (positions, watchlist, value_log/perf.history) for
        user A never returns user B's rows, and vice versa."""
        conn = portfolio.connect(self.db)
        user_a = self.user_id
        user_b = auth.create_user(conn, "otheruser", "otherpass")

        portfolio.import_csv(conn, FIXTURE, user_a)
        other_fixture = os.path.join(self.dir, "other_positions.csv")
        shutil.copyfile(FIXTURE, other_fixture)
        # re-date the second file's snapshot so both users have distinct,
        # independently-verifiable data even though they share a "date" concept
        portfolio.import_csv(conn, other_fixture, user_b)

        a_positions = conn.execute(
            "SELECT symbol FROM positions WHERE user_id = ?", (user_a,)).fetchall()
        b_positions = conn.execute(
            "SELECT symbol FROM positions WHERE user_id = ?", (user_b,)).fetchall()
        self.assertEqual(len(a_positions), 3)
        self.assertEqual(len(b_positions), 3)

        watchlist.add(conn, user_a, "NVDA")
        watchlist.add(conn, user_b, "AMD")
        self.assertEqual(watchlist.list_tickers(conn, user_a), ["NVDA"])
        self.assertEqual(watchlist.list_tickers(conn, user_b), ["AMD"])

        perf.log_open(self.db, user_a, {"portfolio_value": 111}, min_gap_sec=0)
        perf.log_open(self.db, user_b, {"portfolio_value": 222}, min_gap_sec=0)
        self.assertEqual(perf.last_open(self.db, user_a)["portfolio_value"], 111)
        self.assertEqual(perf.last_open(self.db, user_b)["portfolio_value"], 222)


class _FakeCreateClient:
    """Stands in for anthropic.Anthropic for a single messages.create() call."""

    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _Obj(stop_reason="end_turn", content=[_Obj(type="text", text=self._text)])


def _fake_anthropic_response(mapping_dict_or_text) -> "_FakeCreateClient":
    text = (mapping_dict_or_text if isinstance(mapping_dict_or_text, str)
            else json.dumps(mapping_dict_or_text))
    return _FakeCreateClient(text)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeStream:
    def __init__(self, texts, message):
        self._texts, self._message = texts, message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(_Obj(type="text", text=t) for t in self._texts)

    def get_final_message(self):
        return self._message


class _FakeClient:
    """Stands in for anthropic.Anthropic: each messages.stream() call plays
    the next scripted turn and records what it was sent."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._turns.pop(0)


class AdvisorTests(TempDBMixin, unittest.TestCase):
    def _contexts(self):
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        positions = [dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE user_id = ?", (self.user_id,))]
        cash = {r["account"]: r["cash_value"] for r in conn.execute(
            "SELECT account, cash_value FROM account_totals WHERE user_id = ?", (self.user_id,))}
        conn.close()
        total = sum(p["market_value"] for p in positions) + sum(cash.values())
        ctxs = [{"pos": p, "quote": {}, "stats": {}, "info": {}, "port_value": total,
                 "acct_value": None} for p in positions]
        return ctxs, cash

    def test_portfolio_summary_sends_weights_not_dollars_or_account_names(self):
        ctxs, cash = self._contexts()
        text = advisor.portfolio_summary(ctxs, cash)
        for sym in ("AAA", "BBB", "CCC"):
            self.assertIn(sym, text)
        self.assertNotIn("$", text)
        self.assertNotIn("Individual", text)
        self.assertNotIn("...111", text)
        for dollars in ("1200", "1,200", "1600", "1,600"):   # fixture market values
            self.assertNotIn(dollars, text)
        self.assertIn("% of portfolio", text)

    def test_portfolio_summary_empty_account(self):
        self.assertIn("No holdings yet", advisor.portfolio_summary([], {}))

    def test_profile_round_trip_and_isolation(self):
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "other", "pw")
        advisor.save_profile(conn, self.user_id, {"goal": "retire at 60", "risk_tolerance": "moderate"})
        advisor.save_profile(conn, self.user_id, {"time_horizon_years": 25, "goal": None})
        p = advisor.get_profile(conn, self.user_id)
        self.assertEqual(p["goal"], "retire at 60")        # None left it unchanged
        self.assertEqual(p["time_horizon_years"], 25)
        self.assertIsNone(advisor.get_profile(conn, other)["goal"])
        advisor.save_profile(conn, self.user_id, {"goal": None}, replace=True)
        self.assertIsNone(advisor.get_profile(conn, self.user_id)["risk_tolerance"])
        conn.close()

    def test_validate_profile_input(self):
        ok, err = advisor.validate_profile_input(
            {"goal": ["Buy a home", "Retirement"], "time_horizon_years": 20, "target_return_pct": 7,
             "risk_tolerance": "aggressive", "experience": None, "age_range": "25-34",
             "preferences": []})
        self.assertEqual(err, "")
        # pick-any answers are stored in the options' own order; an empty list is "no change"
        self.assertEqual(ok, {"goal": "Retirement; Buy a home", "time_horizon_years": 20,
                              "target_return_pct": 7.0, "risk_tolerance": "aggressive",
                              "age_range": "25-34"})
        for bad in ({"risk_tolerance": "yolo"}, {"time_horizon_years": 0},
                    {"target_return_pct": 900}, {"surprise": "x"}, "not a dict",
                    {"goal": "retire at 60"}, {"goal": ["Get rich quick"]},
                    {"notes": "the user's own field"}):
            fields, err = advisor.validate_profile_input(bad)
            self.assertIsNone(fields, bad)
            self.assertTrue(err)

    def test_system_prompt_asks_for_missing_profile_fields(self):
        empty = {f: None for f in advisor.PROFILE_FIELDS}
        self.assertIn("Still unknown", advisor.system_prompt(empty, "No holdings yet"))
        full = {f: None for f in advisor.PROFILE_FIELDS}
        full.update({"goal": "Retirement", "time_horizon_years": 30, "risk_tolerance": "moderate",
                     "drawdown_reaction": "Hold and wait", "experience": "new",
                     "age_range": "35-44", "income_stability": "Very stable",
                     "emergency_fund": "3-6 months"})
        prompt = advisor.system_prompt(full, "No holdings yet")
        self.assertNotIn("Still unknown", prompt)
        self.assertIn("Retirement", prompt)
        self.assertIn("None yet", prompt)  # no notes from earlier conversations
        self.assertIn("- saving for a boat",
                      advisor.system_prompt(full, "No holdings yet", "- saving for a boat"))

    def test_stream_reply_saves_profile_then_continues(self):
        tool_block = _Obj(type="tool_use", id="tu_1", name="update_investor_profile",
                          input={**{f: None for f in advisor.TOOL_PROFILE_FIELDS},
                                 "goal": ["Retirement"], "risk_tolerance": "moderate"})
        client = _FakeClient([
            _FakeStream(["Got it. "], _Obj(stop_reason="tool_use", content=[tool_block])),
            _FakeStream(["How long until you retire?"],
                        _Obj(stop_reason="end_turn", content=[_Obj(type="text", text="...")])),
        ])
        saved = []
        history = [{"role": "user", "content": "I want to retire at 60, moderate risk."}]
        text = "".join(advisor.stream_reply(client, history, "sys", saved.append))
        self.assertEqual(text, "Got it. How long until you retire?")
        self.assertEqual(saved, [{"goal": "Retirement", "risk_tolerance": "moderate"}])
        self.assertEqual(len(client.calls), 2)
        # second call carries the assistant tool_use turn and its tool_result
        self.assertEqual(client.calls[1]["messages"][-1]["content"][0]["tool_use_id"], "tu_1")

    def test_stream_reply_saves_memory(self):
        mem_block = _Obj(type="tool_use", id="tu_m", name="save_memory",
                         input={"notes": "- house ~2029\n- avoid crypto"})
        too_long = _Obj(type="tool_use", id="tu_x", name="save_memory",
                        input={"notes": "x" * (advisor.MEMORY_MAX_CHARS + 1)})
        client = _FakeClient([
            _FakeStream(["Noted."], _Obj(stop_reason="tool_use", content=[mem_block, too_long])),
            _FakeStream([], _Obj(stop_reason="end_turn", content=[])),
        ])
        notes, profile = [], []
        text = "".join(advisor.stream_reply(client, [{"role": "user", "content": "x"}], "sys",
                                            profile.append, notes.append))
        self.assertEqual(text, "Noted.")
        self.assertEqual(notes, ["- house ~2029\n- avoid crypto"])  # the too-long one isn't saved
        self.assertEqual(profile, [])
        results = client.calls[1]["messages"][-1]["content"]
        self.assertFalse(results[0].get("is_error"))
        self.assertTrue(results[1]["is_error"])
        self.assertEqual({t["name"] for t in client.calls[0]["tools"]},
                         {"update_investor_profile", "save_memory"})

    def test_memory_round_trip_is_separate_from_profile(self):
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "other", "pw")
        self.assertEqual(advisor.get_memory(conn, self.user_id), "")
        advisor.save_memory(conn, self.user_id, "- house ~2029")      # before any profile row
        advisor.save_profile(conn, self.user_id, {"age_range": "25-34"}, replace=True)
        self.assertEqual(advisor.get_memory(conn, self.user_id), "- house ~2029")
        self.assertEqual(advisor.get_profile(conn, self.user_id)["age_range"], "25-34")
        self.assertNotIn("ai_memory", advisor.get_profile(conn, self.user_id))  # never shown
        self.assertEqual(advisor.get_memory(conn, other), "")
        conn.close()

    def test_profile_columns_added_to_an_existing_database(self):
        import sqlite3
        old = os.path.join(os.path.dirname(self.db), "old.db")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE investor_profiles (user_id INTEGER PRIMARY KEY, goal TEXT, "
                  "time_horizon_years INTEGER, target_return_pct REAL, risk_tolerance TEXT, "
                  "experience TEXT, notes TEXT, updated_at TEXT)")
        c.execute("INSERT INTO investor_profiles (user_id, goal) VALUES (1, 'retire at 60')")
        c.commit()
        c.close()
        conn = portfolio.connect(old)
        self.assertEqual(advisor.get_profile(conn, 1)["goal"], "retire at 60")
        advisor.save_memory(conn, 1, "- note")
        self.assertEqual(advisor.get_memory(conn, 1), "- note")
        conn.close()

    def test_ui_script_survives_the_html_sanitizer(self):
        # Streamlit's st.html sanitizer drops a whole script whose text looks
        # like it contains a tag (e.g. an SVG string), and then nothing runs.
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui_enhancements.js")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(re.findall(r"<[/\w!]", fh.read()), [])

    def test_stream_reply_refusal(self):
        client = _FakeClient([_FakeStream([], _Obj(stop_reason="refusal", content=[]))])
        text = "".join(advisor.stream_reply(client, [{"role": "user", "content": "x"}],
                                            "sys", lambda f: None))
        self.assertEqual(text, advisor.REFUSAL_TEXT)


class AdvisorModeTests(TempDBMixin, unittest.TestCase):
    """self.user_id ("testuser") is the advisor in these tests."""

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)
        auth.set_advisor(self.conn, "testuser", True)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_existing_accounts_are_not_advisors(self):
        other = auth.create_user(self.conn, "plain", "pw")
        self.conn.execute("UPDATE users SET is_advisor = NULL WHERE id = ?", (other,))
        self.assertFalse(auth.is_advisor(self.conn, other))
        self.assertTrue(auth.is_advisor(self.conn, self.user_id))

    def test_client_without_password_cannot_log_in_until_given_one(self):
        cid = auth.create_client(self.conn, self.user_id, "jsmith")
        self.assertEqual(auth.list_clients(self.conn, self.user_id), [(cid, "jsmith")])
        self.assertIsNone(auth.verify_login(self.conn, "jsmith", ""))
        auth.set_password(self.conn, "jsmith", "clientpass1")
        self.assertEqual(auth.verify_login(self.conn, "jsmith", "clientpass1"), cid)

    def test_can_view_rules(self):
        client = auth.create_client(self.conn, self.user_id, "c1", "pw12345678")
        stranger = auth.create_user(self.conn, "stranger", "pw")
        other_adv = auth.create_user(self.conn, "adv2", "pw")
        auth.set_advisor(self.conn, "adv2", True)
        other_client = auth.create_client(self.conn, other_adv, "c2")

        self.assertTrue(auth.can_view(self.conn, self.user_id, self.user_id))
        self.assertTrue(auth.can_view(self.conn, self.user_id, client))
        self.assertFalse(auth.can_view(self.conn, self.user_id, stranger))
        self.assertFalse(auth.can_view(self.conn, self.user_id, other_client))  # someone else's client
        self.assertTrue(auth.can_view(self.conn, client, client))
        self.assertFalse(auth.can_view(self.conn, client, self.user_id))         # client can't see advisor
        self.assertFalse(auth.can_view(self.conn, stranger, client))

    def test_client_import_switch_is_off_by_default_and_only_the_advisor_sets_it(self):
        client = auth.create_client(self.conn, self.user_id, "c1")
        self.assertFalse(advising.client_can_import(self.conn, client))
        self.assertTrue(advising.set_client_can_import(self.conn, self.user_id, client, True))
        self.assertTrue(advising.client_can_import(self.conn, client))
        other_adv = auth.create_user(self.conn, "adv2", "pw")
        self.assertFalse(advising.set_client_can_import(self.conn, other_adv, client, False))
        self.assertTrue(advising.client_can_import(self.conn, client))   # not their client
        advising.set_client_can_import(self.conn, self.user_id, client, False)
        self.assertFalse(advising.client_can_import(self.conn, client))
        self.assertFalse(advising.client_can_import(self.conn, self.user_id))  # not a client

    def test_can_view_stops_when_advisor_rights_are_removed(self):
        client = auth.create_client(self.conn, self.user_id, "c1")
        auth.set_advisor(self.conn, "testuser", False)
        self.assertFalse(auth.can_view(self.conn, self.user_id, client))

    def test_weekly_summary_groups_reviews_and_other_reasons(self):
        row = lambda uid, review, days, reasons: {"user_id": uid, "name": f"c{uid}", "review": review,
                                                   "review_days": days, "reasons": reasons}
        every = advising.REVIEW_EVERY_DAYS
        s = advising.weekly_summary([
            row(1, "due", every + 40, ["Review due"]),
            row(2, "never", None, ["No review yet", "No goal"]),
            row(3, "due", every + 5, ["Review due", "2 alerts"]),
            row(4, "ok", every - 3, []),                       # due in 4 days
            row(5, "ok", every - advising.SOON_DAYS - 1, []),  # not soon yet
            row(6, "ok", 10, ["Drift 12 pts"]),
        ])
        self.assertEqual([r["user_id"] for r in s["due"]], [2, 1, 3])   # never, then most overdue
        self.assertEqual([(r["user_id"], r["in_days"]) for r in s["soon"]], [(4, 4)])
        self.assertEqual([(r["user_id"], r["other"]) for r in s["attention"]],
                         [(2, ["No goal"]), (3, ["2 alerts"]), (6, ["Drift 12 pts"])])
        self.assertTrue(s["any"])
        self.assertFalse(advising.weekly_summary([row(7, "ok", 5, [])])["any"])

    def test_week_of_runs_monday_to_sunday(self):
        self.assertEqual(advising.week_of(date(2026, 9, 28)), "2026-W40")   # Monday
        self.assertEqual(advising.week_of(date(2026, 10, 4)), "2026-W40")   # Sunday
        self.assertEqual(advising.week_of(date(2026, 10, 5)), "2026-W41")
        self.assertEqual(advising.week_of(date(2027, 1, 1)), "2026-W53")    # ISO year

    def test_setup_link_lets_the_client_choose_a_password_once(self):
        cid = auth.create_client(self.conn, self.user_id, "jsmith")
        now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        token = auth.create_invite(self.conn, self.user_id, cid, now=now)
        stored = self.conn.execute("SELECT token_hash FROM invites").fetchone()["token_hash"]
        self.assertNotEqual(stored, token)                          # only the hash is kept
        self.assertEqual(auth.invite_info(self.conn, token, now=now)["username"], "jsmith")
        self.assertEqual(auth.pending_invite(self.conn, cid, now=now), "2026-10-07 12:00:00")

        short = auth.accept_invite(self.conn, token, "short", now=now)
        self.assertFalse(short["ok"])                               # still usable after this
        ok = auth.accept_invite(self.conn, token, "clientpass1", now=now)
        self.assertEqual((ok["ok"], ok["user_id"]), (True, cid))
        self.assertEqual(auth.verify_login(self.conn, "jsmith", "clientpass1"), cid)
        again = auth.accept_invite(self.conn, token, "takeover99", now=now)
        self.assertFalse(again["ok"])                               # used up
        self.assertIsNone(auth.verify_login(self.conn, "jsmith", "takeover99"))
        self.assertIsNone(auth.pending_invite(self.conn, cid, now=now))

    def test_setup_links_expire_are_replaced_and_only_for_own_clients(self):
        cid = auth.create_client(self.conn, self.user_id, "jsmith")
        now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        first = auth.create_invite(self.conn, self.user_id, cid, now=now)
        second = auth.create_invite(self.conn, self.user_id, cid, now=now)
        self.assertIsNone(auth.invite_info(self.conn, first, now=now))   # replaced
        later = now + timedelta(days=auth.INVITE_DAYS, seconds=1)
        self.assertIsNone(auth.invite_info(self.conn, second, now=later))  # expired
        self.assertFalse(auth.accept_invite(self.conn, second, "clientpass1", now=later)["ok"])
        auth.cancel_invite(self.conn, cid)
        self.assertIsNone(auth.invite_info(self.conn, second, now=now))   # cancelled
        self.assertIsNone(auth.invite_info(self.conn, "made-up", now=now))

        other_adv = auth.create_user(self.conn, "adv2", "pw")
        auth.set_advisor(self.conn, "adv2", True)
        with self.assertRaises(ValueError):
            auth.create_invite(self.conn, other_adv, cid)           # not their client
        with self.assertRaises(ValueError):
            auth.create_invite(self.conn, self.user_id, self.user_id)   # not a client

    def test_only_advisors_create_clients_and_names_are_validated(self):
        plain = auth.create_user(self.conn, "plain", "pw")
        with self.assertRaises(ValueError):
            auth.create_client(self.conn, plain, "c1")
        with self.assertRaises(ValueError):
            auth.create_client(self.conn, self.user_id, "bad name; drop")
        with self.assertRaises(portfolio.DBError):
            auth.create_client(self.conn, self.user_id, "testuser")   # taken


class AiUsageTests(TempDBMixin, unittest.TestCase):
    """Monthly AI allowances (ai_usage.py)."""

    def test_counts_per_month_and_feature_until_the_limit(self):
        import ai_usage
        conn = portfolio.connect(self.db)
        sep = datetime(2026, 9, 30, 23, tzinfo=timezone.utc)
        limit = ai_usage.LIMITS["screenshot"]
        for _ in range(limit - 1):
            ai_usage.record(conn, self.user_id, "screenshot", now=sep)
        st_ = ai_usage.status(conn, self.user_id, "screenshot", now=sep)
        self.assertEqual((st_["used"], st_["left"], st_["ok"]), (limit - 1, 1, True))
        self.assertEqual(ai_usage.left_text(st_, "screenshot"),
                         f"1 of {limit} screenshot reads left this month")
        ai_usage.record(conn, self.user_id, "screenshot", now=sep)
        st_ = ai_usage.status(conn, self.user_id, "screenshot", now=sep)
        self.assertFalse(st_["ok"])
        self.assertIn("start again on October 1", ai_usage.used_up_text(st_, "screenshot"))
        self.assertTrue(ai_usage.status(conn, self.user_id, "chat", now=sep)["ok"])  # own count
        octo = datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(ai_usage.status(conn, self.user_id, "screenshot", now=octo)["used"], 0)
        self.assertEqual(ai_usage.resets_on(datetime(2026, 12, 5)), date(2027, 1, 1))
        conn.close()

    def test_advisors_get_more_and_unlimited_accounts_have_no_limit(self):
        import ai_usage
        conn = portfolio.connect(self.db)
        base = ai_usage.LIMITS["chat"]
        self.assertEqual(ai_usage.limit_for(conn, self.user_id, "chat"), base)
        auth.set_advisor(conn, "testuser", True)
        self.assertEqual(ai_usage.limit_for(conn, self.user_id, "chat"),
                         base * ai_usage.ADVISOR_SCALE)
        manage_users.main(["--db", self.db, "ai-unlimited", "testuser"])
        st_ = ai_usage.status(conn, self.user_id, "chat")
        self.assertEqual((st_["limit"], st_["left"], st_["ok"]), (None, None, True))
        self.assertEqual(ai_usage.left_text(st_, "chat"), "")
        manage_users.main(["--db", self.db, "ai-limited", "testuser"])
        self.assertEqual(ai_usage.limit_for(conn, self.user_id, "chat"),
                         base * ai_usage.ADVISOR_SCALE)
        conn.close()


class SignUpTests(TempDBMixin, unittest.TestCase):
    """Self-serve sign-up with an email (auth.sign_up)."""

    NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _sign_up(self, email="New.Person@Example.com", password="goodpass1", **kw):
        args = dict(agreed=True, adult=True, terms_version="October 1, 2026",
                    ip="203.0.113.7", seconds_open=10, now=self.NOW)
        args.update(kw)
        return auth.sign_up(self.conn, email, password, **args)

    def test_new_account_signs_in_by_email_and_gets_normal_ai_limits(self):
        import ai_usage
        result = self._sign_up()
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["username"], "new.person@example.com")
        row = self.conn.execute("SELECT email, email_verified_at, terms_version, "
                                "terms_accepted_at, is_advisor FROM users WHERE id = ?",
                                (result["user_id"],)).fetchone()
        self.assertEqual(row["email"], "new.person@example.com")
        self.assertIsNone(row["email_verified_at"])        # no email service yet
        self.assertEqual(row["terms_version"], "October 1, 2026")
        self.assertEqual(row["terms_accepted_at"], "2026-10-01 12:00:00")
        self.assertFalse(row["is_advisor"])
        for typed in ("new.person@example.com", "NEW.Person@example.COM"):
            self.assertEqual(auth.verify_login(self.conn, typed, "goodpass1"), result["user_id"])
        self.assertIsNone(auth.verify_login(self.conn, "new.person@example.com", "wrong"))
        for kind, base in ai_usage.LIMITS.items():
            self.assertEqual(ai_usage.limit_for(self.conn, result["user_id"], kind), base)
        # only a hash of the address is kept
        keys = [r["address_key"] for r in self.conn.execute("SELECT address_key FROM signups")]
        self.assertTrue(keys and all("203.0.113.7" not in k for k in keys))

    def test_form_checks(self):
        cases = [(dict(email="not an email"), "email address"),
                 (dict(password="short"), "at least"),
                 (dict(adult=False), "18 and over"),
                 (dict(agreed=False), "agree")]
        for kw, msg in cases:
            result = self._sign_up(**kw)
            self.assertFalse(result["ok"])
            self.assertIn(msg, result["error"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"], 1)

    def test_taken_email_in_any_case_is_refused(self):
        self.assertTrue(self._sign_up()["ok"])
        again = self._sign_up(email="new.person@EXAMPLE.com", ip="198.51.100.1")
        self.assertFalse(again["ok"])
        self.assertIn("already an account", again["error"])
        auth.create_user(self.conn, "Admin.Made@example.com", "whatever1")  # by an admin
        self.assertFalse(self._sign_up(email="admin.made@example.com", ip="198.51.100.2")["ok"])

    def test_bot_checks(self):
        trap = self._sign_up(honeypot="http://spam.example")
        self.assertFalse(trap["ok"])
        self.assertNotIn("field", trap["error"].lower())   # says nothing about why
        fast = self._sign_up(seconds_open=1)
        self.assertFalse(fast["ok"])
        self.assertIn("quick", fast["error"])
        self.assertTrue(self._sign_up(seconds_open=4)["ok"])  # the person tries again

    def test_limits_per_address_and_app_wide(self):
        for i in range(auth.SIGNUPS_PER_ADDRESS_PER_DAY):
            self.assertTrue(self._sign_up(email=f"p{i}@example.com")["ok"])
        blocked = self._sign_up(email="one.more@example.com")
        self.assertFalse(blocked["ok"])
        self.assertIn("tomorrow", blocked["error"])
        tomorrow = self.NOW + timedelta(days=1, minutes=1)
        self.assertTrue(self._sign_up(email="one.more@example.com", now=tomorrow)["ok"])
        # app-wide: many addresses within one hour
        later = self.NOW + timedelta(days=3)
        for i in range(auth.SIGNUPS_PER_HOUR):
            self.assertTrue(self._sign_up(email=f"w{i}@example.com", ip=f"10.0.{i}.1",
                                          now=later)["ok"])
        busy = self._sign_up(email="late@example.com", ip="10.9.9.9", now=later)
        self.assertIn("try again in an hour", busy["error"])
        # an unknown address only meets the app-wide limit
        self.assertTrue(self._sign_up(email="anon@example.com", ip=None,
                                      now=later + timedelta(hours=2))["ok"])

    def test_checking_many_emails_from_one_address_is_slowed(self):
        self.assertTrue(self._sign_up(email="taken@example.com")["ok"])
        for _ in range(auth.SIGNUP_TRIES_PER_ADDRESS_PER_HOUR - 1):
            self.assertIn("already", self._sign_up(email="taken@example.com")["error"])
        self.assertIn("tomorrow", self._sign_up(email="taken@example.com")["error"])


class AdminTests(TempDBMixin, unittest.TestCase):
    """The admin portal's data side (admin.py)."""

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_admin_only_from_the_command_line(self):
        import admin
        self.assertFalse(admin.is_admin(self.conn, self.user_id))
        with contextlib.redirect_stdout(io.StringIO()):
            manage_users.main(["--db", self.db, "make-admin", "testuser"])
        self.assertTrue(admin.is_admin(self.conn, self.user_id))
        self.assertEqual(admin.list_accounts(self.conn)[0]["role"], "admin")

    def test_db_option_works_after_the_command_and_says_where(self):
        import admin
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            manage_users.main(["make-admin", "testuser", "--db", self.db])
        self.assertTrue(admin.is_admin(self.conn, self.user_id))
        self.assertIn("changed in the local file", out.getvalue())
        self.assertEqual(manage_users.where("postgresql://u:pw@ep-x.neon.tech/db"),
                         "the Postgres database at ep-x.neon.tech")

    def test_listed_admins_from_the_secrets(self):
        import admin
        with unittest.mock.patch.dict(os.environ, {"NORTHWEND_ADMINS": "someone, TestUser"}):
            self.assertTrue(admin.is_admin(self.conn, self.user_id))
            self.assertEqual(admin.list_accounts(self.conn)[0]["role"], "admin")
            self.assertFalse(admin.delete_account(self.conn, self.user_id, by=-1)["ok"])
            # a self-made account with a listed login only counts once its email is confirmed
            self.conn.execute("UPDATE users SET terms_version = 'v1' WHERE id = ?",
                              (self.user_id,))
            self.conn.commit()
            self.assertFalse(admin.is_admin(self.conn, self.user_id))
            self.conn.execute("UPDATE users SET email_verified_at = '2026-10-01T00:00:00Z' "
                              "WHERE id = ?", (self.user_id,))
            self.conn.commit()
            self.assertTrue(admin.is_admin(self.conn, self.user_id))
        self.assertFalse(admin.is_admin(self.conn, self.user_id))

    def test_every_table_with_account_data_is_cleared_on_delete(self):
        import admin
        covered = {t: set(c) for t, c in admin.ACCOUNT_TABLES.items()}
        for t, cols in admin.ACCOUNT_REFERENCES.items():
            covered.setdefault(t, set()).update(cols)
        for (table,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            account_cols = cols & {"user_id", "advisor_id", "client_id", "created_by", "set_by"}
            if table != "users" and account_cols:
                self.assertEqual(account_cols - covered.get(table, set()), set(),
                                 f"{table} isn't cleared by admin.delete_account")

    def test_create_list_and_delete(self):
        import admin
        import sample_data
        auth.set_advisor(self.conn, "testuser", True)
        made = admin.create_account(self.conn, " New.Person@Example.com ")
        self.assertEqual((made["username"], made["email"], made["temp_password"]),
                         ("new.person@example.com", "new.person@example.com", None))
        named = admin.create_account(self.conn, "jo_client")
        self.assertTrue(named["temp_password"])
        self.assertEqual(auth.verify_login(self.conn, "jo_client", named["temp_password"]),
                         named["user_id"])
        self.assertIn("already", admin.create_account(self.conn, "new.person@EXAMPLE.com")["error"])
        self.assertIn("username", admin.create_account(self.conn, "bad name; drop")["error"])
        auth.link_client(self.conn, self.user_id, named["user_id"])
        rows = {a["username"]: a for a in admin.list_accounts(self.conn)}
        self.assertEqual(rows["jo_client"]["role"], "client")
        self.assertEqual(rows["jo_client"]["advisor"], "testuser")
        self.assertEqual(rows["testuser"]["clients"], 1)
        self.assertIs(rows["new.person@example.com"]["confirmed"], False)
        # a setup link lets them choose a password, and lasts a week
        link = auth.setup_link(self.conn, made["user_id"])
        self.assertEqual(link["to"], "new.person@example.com")
        later = datetime.now(timezone.utc) + timedelta(days=auth.SETUP_DAYS - 1)
        self.assertTrue(auth.reset_password(self.conn, link["token"], "chosen-pass1", now=later)["ok"])
        self.assertFalse(auth.setup_link(self.conn, named["user_id"])["ok"])  # no email

        # deleting the advisor clears its data and leaves its client unmanaged
        sample_data.load(self.conn, self.user_id)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) AS n FROM positions WHERE "
                                             "user_id = ?", (self.user_id,)).fetchone()["n"], 0)
        other_admin = auth.create_user(self.conn, "boss", "bosspass1")
        admin.set_admin(self.conn, "boss", True)
        self.assertFalse(admin.delete_account(self.conn, other_admin, by=self.user_id)["ok"])
        self.assertFalse(admin.delete_account(self.conn, self.user_id, by=self.user_id)["ok"])
        gone = admin.delete_account(self.conn, self.user_id, by=other_admin)
        self.assertEqual((gone["ok"], gone["orphaned_clients"]), (True, 1))
        for table, cols in admin.ACCOUNT_TABLES.items():
            for col in cols:
                n = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?",
                                      (self.user_id,)).fetchone()["n"]
                self.assertEqual(n, 0, f"{table}.{col}")
        self.assertIsNone(auth.get_username(self.conn, self.user_id))
        self.assertEqual(auth.get_username(self.conn, named["user_id"]), "jo_client")

    def test_last_sign_in_is_recorded(self):
        import admin
        self.assertIsNone(admin.list_accounts(self.conn)[0]["last_login_at"])
        auth.attempt_login(self.conn, "testuser", "testpass")
        self.assertIsNotNone(admin.list_accounts(self.conn)[0]["last_login_at"])


class BookOverviewTests(unittest.TestCase):
    """The book overview's new attention signals (advising.attention)."""

    def test_accepted_proposals_and_inactivity(self):
        base = dict(has_data=True, goal_status="on_track", review="ok", n_alerts=0, drift=None,
                    profile_done=True)
        self.assertEqual(advising.attention(**base), [])
        self.assertEqual(advising.attention(**base, proposal_accepted=True),
                         ["Proposal accepted"])                      # first: it's on the advisor
        self.assertEqual(advising.attention(**base, days_since_login=None), [])  # never signs in
        self.assertEqual(advising.attention(**base, days_since_login=advising.INACTIVE_DAYS), [])
        self.assertEqual(advising.attention(**base, days_since_login=75),
                         ["Not signed in for 75 days"])


class ProgressReportTests(TempDBMixin, unittest.TestCase):
    """Client progress reports (reports.py)."""

    def test_periods(self):
        import reports
        today = date(2026, 10, 1)
        self.assertEqual(reports.period_bounds("Last month", today),
                         (date(2026, 9, 1), date(2026, 9, 30), "September 2026"))
        self.assertEqual(reports.period_bounds("Last quarter", today),
                         (date(2026, 7, 1), date(2026, 9, 30), "Q3 2026"))
        self.assertEqual(reports.period_bounds("Last quarter", date(2026, 2, 10))[2], "Q4 2025")
        start, end, _ = reports.period_bounds("Since the last report", today, "2026-08-31")
        self.assertEqual((start, end), (date(2026, 9, 1), today))

    def test_build_save_read_and_pdf(self):
        import reports
        conn = portfolio.connect(self.db)
        auth.set_advisor(conn, "testuser", True)
        cid = auth.create_client(conn, self.user_id, "pat_client")
        conn.execute("INSERT INTO value_log (logged_at, portfolio_value, user_id) VALUES "
                     "('2026-06-30T12:00:00Z', 40000, ?)", (cid,))
        conn.commit()
        plans.add_contribution(conn, cid, "2026-08-15", 1500)
        plans.add_contribution(conn, cid, "2026-10-02", 999)     # after the period
        plans.save_plan(conn, cid, {"goal_type": "Retirement", "target_amount": 100000,
                                    "target_date": "2040-01-01", "monthly_contribution": 500},
                        self.user_id)
        advising.add_note(conn, cid, self.user_id, "Next step", "Open an IRA", "2026-09-01")
        advising.add_note(conn, cid, self.user_id, "Next step", "Private thing", "2026-09-01",
                          private=True)
        facts = reports.build(conn, cid, date(2026, 7, 1), date(2026, 10, 1),
                              value_now=44000.0, today=date(2026, 10, 1))
        self.assertEqual((facts["value_start"], facts["value_end"], facts["money_in"]),
                         (40000, 44000.0, 1500.0))
        self.assertEqual(facts["growth"], 2500.0)
        self.assertEqual(facts["goal"]["status"], "on_track")
        self.assertEqual(facts["next_steps"], ["Open an IRA"])      # shared steps only
        old = reports.build(conn, cid, date(2026, 1, 1), date(2026, 3, 31), value_now=44000.0,
                            today=date(2026, 10, 1))   # no values logged near that period
        self.assertEqual((old["value_start"], old["value_end"], old["growth"]), (None, None, None))
        lines = reports.summary_lines(facts, lambda v: f"${v:,.0f}")
        self.assertIn("from $40,000 to $44,000", lines[0])
        self.assertIn("$1,500 added by you and $2,500 growth", lines[1])
        rid = reports.save(conn, self.user_id, cid, label="Q3 2026", start=date(2026, 7, 1),
                           end=date(2026, 9, 30), facts=facts, message="Nice quarter.")
        rep = reports.for_client(conn, cid)[0]
        self.assertEqual((rep["id"], rep["read_at"], rep["facts"]["money_in"]), (rid, None, 1500.0))
        self.assertEqual(reports.last_end(conn, cid), "2026-09-30")
        reports.mark_read(conn, cid, rid)
        self.assertIsNotNone(reports.for_client(conn, cid)[0]["read_at"])
        self.assertTrue(reports.render_pdf(rep, client_name="pat", advisor_name="sam")
                        .startswith(b"%PDF"))
        conn.close()


class MeetingPrepTests(TempDBMixin, unittest.TestCase):
    """Meeting prep (meeting.py): what changed since the last review."""

    def test_changes_since_the_last_review_and_safe_ai_facts(self):
        import meeting
        import sample_data
        conn = portfolio.connect(self.db)
        auth.set_advisor(conn, "testuser", True)
        cid = auth.create_client(conn, self.user_id, "pat_client")
        today = date(2026, 10, 1)
        first = meeting.prep(conn, cid, today=today, value=None, latest_snapshot=None,
                             actual_pct={}, targets={}, drift_threshold=5)
        self.assertIsNone(first["last_review"])
        self.assertIn("first review", meeting.facts_for_ai(first))

        sample_data.load(conn, cid, today=date(2026, 6, 1))
        old_snap = conn.execute("SELECT MAX(snapshot_date) AS d FROM snapshots WHERE "
                                "user_id = ?", (cid,)).fetchone()["d"]
        conn.execute("INSERT INTO value_log (logged_at, snapshot_date, portfolio_value, user_id) "
                     "VALUES (?, ?, ?, ?)", ("2026-06-02T10:00:00Z", old_snap, 40000.0, cid))
        advising.add_note(conn, cid, self.user_id, "Review", "Met to talk goals", "2026-06-03")
        advising.add_note(conn, cid, self.user_id, "Next step", "Open a Roth IRA $5,000",
                          "2026-06-03", private=True)
        # a later snapshot with one holding sold out and one added
        rows = [dict(r) for r in conn.execute("SELECT * FROM positions WHERE user_id = ? AND "
                                              "snapshot_date = ?", (cid, old_snap))]
        new_snap = "2026-09-30"
        conn.execute("INSERT INTO snapshots (snapshot_date, source_file, user_id) VALUES (?, ?, ?)",
                     (new_snap, "test", cid))
        for r in rows[1:]:
            conn.execute("INSERT INTO positions (snapshot_date, account, symbol, quantity, "
                         "cost_basis, market_value, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (new_snap, r["account"], r["symbol"], r["quantity"], r["cost_basis"],
                          r["market_value"], cid))
        conn.execute("INSERT INTO positions (snapshot_date, account, symbol, quantity, cost_basis, "
                     "market_value, user_id) VALUES (?, 'Brokerage', 'NEWX', 1, 10, 10, ?)",
                     (new_snap, cid))
        conn.commit()
        p = meeting.prep(conn, cid, today=today, value=44000.0, latest_snapshot=new_snap,
                         actual_pct={"Stocks": 85.0, "Bonds": 15.0},
                         targets={"Stocks": 70.0, "Bonds": 30.0}, drift_threshold=5)
        self.assertEqual(p["days_since"], (today - date(2026, 6, 3)).days)
        self.assertAlmostEqual(p["value_change_pct"], 10.0)
        self.assertEqual(p["trades"]["closed"], [f"{rows[0]['symbol']} ({rows[0]['account']})"])
        self.assertEqual(p["trades"]["new"], ["NEWX (Brokerage)"])
        self.assertEqual([d[0] for d in p["drift"]], ["Stocks", "Bonds"])
        self.assertEqual(len(p["next_steps"]), 1)
        facts = meeting.facts_for_ai(p)
        self.assertNotIn("$", facts)                       # no dollar amounts
        self.assertNotIn("Roth", facts)                    # no note text
        self.assertNotIn("Met to talk", facts)
        self.assertIn("+10.0%", facts)
        fake = _FakeCreateClient("- Celebrate the 10% rise\n- Ask about the new holding")
        self.assertEqual(meeting.talking_points(fake, {}, "summary", facts),
                         ["Celebrate the 10% rise", "Ask about the new holding"])
        self.assertIn("Since the last review", fake.kwargs["messages"][0]["content"])
        conn.close()


class ClientOnboardingTests(TempDBMixin, unittest.TestCase):
    """Client onboarding by link (ROADMAP G6): an advisor adds a client by
    email, emails the setup link, the client sets a password."""

    def test_client_by_email_and_emailed_setup_link(self):
        import mailer
        conn = portfolio.connect(self.db)
        auth.set_advisor(conn, "testuser", True)
        cid = auth.create_client(conn, self.user_id, " Pat.Client@Example.com ")
        self.assertEqual(auth.get_username(conn, cid), "pat.client@example.com")
        self.assertEqual(auth.email_status(conn, cid), {"email": "pat.client@example.com",
                                                        "confirmed": False})
        with self.assertRaises(ValueError):
            auth.create_client(conn, self.user_id, "pat.client@EXAMPLE.com")   # taken
        with self.assertRaises(ValueError):
            auth.create_client(conn, self.user_id, "not-an@email")
        token = auth.create_invite(conn, self.user_id, cid)
        sent = {}
        with unittest.mock.patch.object(mailer, "send", lambda to, subject, text, html=None,
                                        from_name=None: sent.update(
                                            to=to, subject=subject, text=text,
                                            from_name=from_name) or True):
            mailer.client_invite("pat.client@example.com", f"https://x/?invite={token}",
                                 "Sam Advisor (Acme)", auth.INVITE_DAYS,
                                 from_name="Sam Advisor, Acme")
        self.assertIn("Sam Advisor (Acme) invited you", sent["subject"])
        self.assertEqual(sent["from_name"], "Sam Advisor, Acme")
        self.assertIn(f"?invite={token}", sent["text"])
        self.assertTrue(auth.accept_invite(conn, token, "clientpass1")["ok"])
        self.assertTrue(auth.email_status(conn, cid)["confirmed"])   # the advisor vouched
        self.assertEqual(auth.verify_login(conn, "Pat.Client@example.com", "clientpass1"), cid)
        conn.close()


class WhatIfTests(unittest.TestCase):
    """The what-if playground's arithmetic (plans.months_to_reach, mix_return)."""

    def test_months_to_reach(self):
        self.assertEqual(plans.months_to_reach(1000, 0, 6, 500), 0)
        self.assertEqual(plans.months_to_reach(0, 100, 0, 1200), 12)
        n = plans.months_to_reach(10000, 500, 6, 50000)
        self.assertGreater(plans.future_value(10000, 500, 6, n), 50000)
        self.assertLess(plans.future_value(10000, 500, 6, n - 1), 50000)
        self.assertLess(plans.months_to_reach(10000, 550, 6, 50000), n)   # more a month: sooner
        self.assertIsNone(plans.months_to_reach(0, 1, 0, 10 ** 9))

    def test_mix_return_matches_the_proposals_assumptions(self):
        import proposals
        self.assertEqual(plans.mix_return(100), proposals.ASSUMED_RETURN["Stocks"])
        self.assertEqual(plans.mix_return(0), proposals.ASSUMED_RETURN["Bonds"])
        self.assertAlmostEqual(plans.mix_return(60), 5.8)
        self.assertEqual(plans.mix_return(150), plans.mix_return(100))


class ProposalTests(TempDBMixin, unittest.TestCase):
    """Advisor proposals (proposals.py)."""

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)
        auth.set_advisor(self.conn, "testuser", True)
        self.client = auth.create_client(self.conn, self.user_id, "pat_client")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_draft_share_answer(self):
        import proposals
        with self.assertRaises(ValueError):
            proposals.save(self.conn, self.user_id, self.client, title="x",
                           mix={"Stocks": 60, "Bonds": 30})          # 90%
        pid = proposals.save(self.conn, self.user_id, self.client, title=" Steadier ",
                             mix={"Stocks": 60, "Bonds": 35, "Cash": 5, "Nonsense": 9},
                             note="Closer to your date.")
        p = proposals.for_client(self.conn, self.client, include_drafts=True)[0]
        self.assertEqual((p["title"], p["status"], p["mix"]),
                         ("Steadier", "draft", {"Stocks": 60.0, "Bonds": 35.0, "Cash": 5.0}))
        self.assertEqual(proposals.for_client(self.conn, self.client, include_drafts=False), [])
        self.assertFalse(proposals.respond(self.conn, self.client, pid, True))   # not shared yet
        self.assertTrue(proposals.share(self.conn, self.user_id, pid))
        self.assertFalse(proposals.share(self.conn, self.client, pid))           # not theirs
        self.assertFalse(proposals.respond(self.conn, self.user_id, pid, True))  # not the client
        self.assertTrue(proposals.respond(self.conn, self.client, pid, True))
        self.assertEqual(proposals.for_client(self.conn, self.client,
                                              include_drafts=False)[0]["status"], "accepted")
        self.assertFalse(proposals.respond(self.conn, self.client, pid, False))  # answered
        # editing an answered proposal sends it back to draft
        proposals.save(self.conn, self.user_id, self.client, title="v2",
                       mix={"Stocks": 50, "Bonds": 50}, proposal_id=pid)
        self.assertEqual(proposals.for_client(self.conn, self.client,
                                              include_drafts=True)[0]["status"], "draft")
        other = auth.create_user(self.conn, "other_adv", "pass12345")
        with self.assertRaises(ValueError):
            proposals.save(self.conn, other, self.client, title="no",
                           mix={"Stocks": 100}, proposal_id=pid)
        proposals.delete(self.conn, self.user_id, pid)
        self.assertEqual(proposals.for_client(self.conn, self.client, include_drafts=True), [])

    def test_compare_and_pdf(self):
        import proposals
        cmp = proposals.compare({"Stocks": 90.0, "Bonds": 10.0}, {"Stocks": 60.0, "Bonds": 40.0},
                                value=40000, monthly=300, months=36)
        self.assertEqual(cmp["rows"][0], ("Stocks", 90.0, 60.0, -30.0))
        self.assertAlmostEqual(cmp["assumed_return"][0], 6.7)
        self.assertAlmostEqual(cmp["assumed_return"][1], 5.8)
        y08 = cmp["hard_years"]["2008"]
        self.assertAlmostEqual(y08[0], -35.5)
        self.assertAlmostEqual(y08[1], -22.0)
        self.assertGreater(cmp["projected"][0], cmp["projected"][1])
        empty = proposals.compare({}, {"Stocks": 100.0})
        self.assertEqual((empty["rows"][0][1], empty["assumed_return"][0], empty["projected"]),
                         (None, None, None))
        pid = proposals.save(self.conn, self.user_id, self.client, title="Steadier",
                             mix={"Stocks": 60, "Bonds": 40}, note="Why: closer to the date.")
        p = proposals.for_client(self.conn, self.client, include_drafts=True)[0]
        pdf = proposals.render_pdf(p, cmp, client_name="pat_client", advisor_name="testuser")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertTrue(pid)


class InvestorTypeTests(unittest.TestCase):
    """Find your direction (learn.investor_type): named from the readiness
    check and the example mix, so it always agrees with them."""

    BASE = {"time_horizon_years": 25, "risk_tolerance": "moderate",
            "drawdown_reaction": "Hold and wait", "emergency_fund": "3-6 months",
            "high_interest_debt": "None"}

    def _type(self, horizon=None, **changes):
        import learn
        p = {**self.BASE, **changes}
        mix = learn.starter_mix(p, horizon)
        kind = learn.investor_type(p, mix, learn.readiness(p))
        return kind and kind["key"]

    def test_types_follow_the_mix(self):
        self.assertEqual(self._type(), "grower")                         # 90% stocks
        self.assertEqual(self._type(time_horizon_years=12), "builder")    # 75%
        self.assertEqual(self._type(time_horizon_years=7), "builder")     # 60%
        self.assertEqual(self._type(time_horizon_years=7, risk_tolerance="conservative"),
                         "balanced")                                      # 45%
        self.assertEqual(self._type(time_horizon_years=4, risk_tolerance="conservative",
                                    drawdown_reaction="Sell everything"), "preserver")
        self.assertEqual(self._type(time_horizon_years=2), "short_term")
        self.assertEqual(self._type(horizon=2.5), "short_term")           # the plan's date wins

    def test_foundation_first_and_nothing_without_a_horizon(self):
        import learn
        self.assertEqual(self._type(emergency_fund="None"), "foundation")
        self.assertEqual(self._type(high_interest_debt="A lot"), "foundation")
        self.assertIsNone(self._type(time_horizon_years=None))
        for kind in learn.INVESTOR_TYPES.values():
            text = " ".join([kind["about"], kind["watch"], *kind["kinds"]]).lower()
            self.assertNotIn("you should buy", text)      # education, not instructions


class ExpeditionTests(unittest.TestCase):
    """T1: the route's regions, the trail drawing and the contour backgrounds."""

    W = [("profile", "About you", True), ("ready", "Ready?", True), ("goal", "Goal", True),
         ("basics", "Basics", False), ("mix", "Mix", False), ("practice", "Practice", False),
         ("brokerage", "Brokerage", False), ("account", "Account", False),
         ("first", "First", False), ("bring", "Bring", False)]

    def test_region_is_where_the_first_open_waypoint_is(self):
        import route
        self.assertEqual(route.region(self.W), ("Learner's ridge", "The practice range"))
        self.assertEqual(route.region([(k, t, True) for k, t, _ in self.W]),
                         ("On the trail", None))
        # someone whose route starts at Start investing
        self.assertEqual(route.region(self.W[6:]), ("The trailhead", "On the trail"))
        every_key = {k for _, keys in route.REGIONS for k in keys}
        self.assertEqual(every_key, {k for k, _, _ in self.W})

    def test_trail_is_two_images_with_a_safe_label(self):
        import base64
        import route
        html = route.trail_html(route.dots(self.W, False), "You're 3 of 7 along")
        self.assertEqual(html.count("<img"), 2)
        self.assertIn("pt-on-light", html)
        self.assertIn("pt-on-dark", html)
        self.assertIn("You&#x27;re", html)          # the apostrophe can't end the attribute
        self.assertNotIn("<svg", html)              # st.html strips inline SVG
        data = html.split("base64,")[1].split("'")[0]
        svg = base64.b64decode(data).decode("utf-8")
        self.assertEqual(svg.count("<circle"), 11)   # 10 waypoints and the goal

    def test_contour_backgrounds_are_built_into_the_styles(self):
        for theme in ("light", "dark"):
            with open(os.path.join(REPO, "static", f"topo-{theme}.svg"), encoding="utf-8") as fh:
                self.assertTrue(fh.read().startswith("<svg"))


class GearTests(unittest.TestCase):
    """T3: milestones and gear - earned from learning and steady habits."""

    def test_earned_in_kit_order(self):
        import gear
        self.assertEqual(gear.earned({"goal_set": True, "profile_done": True, "storm": True}),
                         ["map", "compass", "cloak"])
        self.assertEqual(len(gear.KEYS), 8)

    def test_first_visit_after_the_update_is_quiet(self):
        import gear
        self.assertEqual(gear.new_since(["map", "compass"], None), ([], ["map", "compass"]))
        self.assertEqual(gear.new_since(["map", "compass", "tent"], ["map", "compass"]),
                         (["tent"], ["map", "compass", "tent"]))

    def test_steady_pace_is_three_months_in_a_row(self):
        import gear
        today = date(2026, 10, 15)
        self.assertTrue(gear.steady_months(["2026-08-03", "2026-09-01", "2026-10-02"], today))
        self.assertTrue(gear.steady_months(["2026-07-03", "2026-08-01", "2026-09-30"], today))
        self.assertFalse(gear.steady_months(["2026-06-03", "2026-08-01", "2026-09-30"], today))
        self.assertFalse(gear.steady_months(["2025-12-05", "2026-01-05", "2026-02-05"], today))

    def test_storm_weathered_only_without_selling_in_the_drop(self):
        import gear
        values = [("2026-03-01", 100.0), ("2026-03-10", 110.0), ("2026-03-20", 97.0),
                  ("2026-04-10", 112.0)]
        self.assertTrue(gear.weathered_storm(values, []))
        self.assertTrue(gear.weathered_storm(values, ["2026-05-01"]))     # sold later: fine
        self.assertFalse(gear.weathered_storm(values, ["2026-03-15"]))    # sold in the drop
        self.assertFalse(gear.weathered_storm([("2026-03-01", 100.0), ("2026-03-02", 95.0)], []))

    def test_icons_are_images_per_theme(self):
        import gear
        html = gear.icon_html("cloak", False)
        self.assertEqual(html.count("<img"), 2)
        self.assertNotIn("<svg", html)
        self.assertIn("Storm cloak, not earned yet", html)

    def test_every_piece_says_what_its_for_and_how_its_earned(self):
        import gear
        for k in gear.KEYS:
            for words, start in ((gear.FOR, "For "), (gear.HOW, "Earned when "), (gear.WHY, "")):
                line = words[k]
                self.assertTrue(line.startswith(start), (k, line))
                self.assertTrue(line.endswith("."), (k, line))
                self.assertNotIn("\n", line)
                self.assertLess(len(line), 170, (k, line))     # one short line
            self.assertTrue(gear.BY_KEY[k][3].endswith("."), k)
        # the owner's example: the cloak says plainly what it's for
        self.assertEqual(gear.FOR["cloak"],
                         "For holding steady through a market drop instead of selling.")

    def test_how_its_earned_matches_the_real_rule(self):
        import gear
        # each piece is earned by its own fact, and only that
        self.assertEqual(sorted(gear.NEED), sorted(gear.KEYS))
        for k in gear.KEYS:
            self.assertEqual(gear.earned({gear.NEED[k]: True}), [k])
        self.assertIn(f"{gear.STORM_DROP_PCT:.0f}% or more below its high", gear.HOW["cloak"])
        self.assertIn("don't sell anything between the high and the low", gear.HOW["cloak"])
        self.assertEqual(gear.STREAK_MONTHS, 3)
        self.assertIn("three calendar months in a row, up to this month or last",
                      gear.HOW["lantern"])
        self.assertIn("an amount and a date", gear.HOW["compass"])   # plans.has_goal
        for k in ("boots", "flag"):    # the example portfolio never counts
            self.assertIn("example portfolio doesn't count", gear.HOW[k])
        # the Learn waypoints named are Learn's own titles
        with open(os.path.join(REPO, "views", "get_started.py"), encoding="utf-8") as fh:
            steps = fh.read().split("GET_STARTED_STEPS = (", 1)[1].split("\n)", 1)[0]
        self.assertIn('("profile", "About you")', steps)
        self.assertIn("(About you, on Learn)", gear.HOW["map"])
        self.assertIn('("basics", "Learn the basics")', steps)
        self.assertIn('"Learn the basics"', gear.HOW["tent"])
        self.assertIn('("practice", "Try it with practice money")', steps)
        self.assertIn('"Try it with practice money"', gear.HOW["rope"])

    def test_each_button_goes_where_its_earned(self):
        import gear
        with open(os.path.join(REPO, "views", "get_started.py"), encoding="utf-8") as fh:
            steps = fh.read().split("GET_STARTED_STEPS = (", 1)[1].split("\n)", 1)[0]
        for k, (label, (kind, where)) in gear.GO.items():
            self.assertIn(k, gear.KEYS)
            self.assertTrue(label)
            if kind == "learn":
                self.assertIn(f'("{where}", ', steps)
            elif kind == "page":
                self.assertEqual(where, "Plan")
            else:
                self.assertEqual((kind, where), ("dialog", "manual"))
        self.assertNotIn("cloak", gear.GO)    # nothing to do but stay in

    def test_next_up_skips_the_cloak_until_last(self):
        import gear
        self.assertEqual(gear.next_up([]), "map")
        self.assertEqual(gear.next_up(["map", "compass", "tent", "rope", "boots", "lantern"]),
                         "flag")
        self.assertEqual(gear.next_up([k for k in gear.KEYS if k != "cloak"]), "cloak")
        self.assertIsNone(gear.next_up(list(gear.KEYS)))

    def test_when_each_piece_was_earned(self):
        import gear
        dates = gear.stamp(None, ["map", "compass"], ["compass"], "2026-10-02")
        self.assertEqual(dates, {"map": {"by": "2026-10-02"}, "compass": {"on": "2026-10-02"}})
        # kept once set
        self.assertEqual(gear.stamp(dates, ["map", "compass"], [], "2026-11-05"), dates)
        fmt = lambda d: "Oct 2, 2026"     # noqa: E731
        self.assertEqual(gear.when_text(dates["compass"], fmt), "Earned Oct 2, 2026")
        self.assertEqual(gear.when_text(dates["map"], fmt), "Earned by Oct 2, 2026")
        self.assertEqual(gear.when_text(None, fmt), "Earned")


class RouteTests(unittest.TestCase):
    """The investor home's next step (route.py): one step, in priority order."""

    WAYS = [("profile", "About you", True), ("ready", "Ready?", True), ("goal", "Goal", True),
            ("basics", "Basics", False), ("mix", "Mix", False)]

    def _next(self, **kw):
        import route
        args = dict(has_goal=True, can_manage=True, profile_missing=False, has_holdings=True,
                    monthly=300.0, goal={"status": "on_track", "needed_monthly": 250.0},
                    drift=[], days_since_holdings=5,
                    waypoints=[(k, t, True) for k, t, _ in self.WAYS])
        args.update(kw)
        return route.next_step(**args)

    def test_priority_order(self):
        self.assertEqual(self._next(has_goal=False)["key"], "goal")
        self.assertEqual(self._next(has_goal=False, can_manage=False)["key"], "goal_wait")
        self.assertEqual(self._next(profile_missing=True)["key"], "profile")
        self.assertEqual(self._next(has_holdings=False)["key"], "holdings")
        self.assertEqual(self._next(goal={"status": "reached"}, monthly=0)["key"], "reached")
        self.assertEqual(self._next(monthly=0)["key"], "monthly")
        gap = self._next(goal={"status": "behind", "needed_monthly": 1229.0})
        self.assertEqual((gap["key"], round(gap["extra"])), ("gap", 929))
        drift = self._next(drift=[("Stocks", 80.0, 70.0, 10.0)])
        self.assertEqual((drift["key"], drift["label"]), ("drift", "Stocks"))
        self.assertEqual(self._next(days_since_holdings=60)["key"], "update")
        learn = self._next(waypoints=self.WAYS)
        self.assertEqual((learn["key"], learn["number"], learn["title"]), ("learn", 4, "Basics"))
        self.assertEqual(self._next()["key"], "steady")

    def test_an_advisors_client_is_not_asked_to_do_the_advisors_part(self):
        # the plan and holdings are the advisor's: no money or update steps for the client
        self.assertEqual(self._next(can_manage=False, profile_missing=True, monthly=0,
                                    days_since_holdings=90)["key"], "steady")

    def test_dots_and_drift(self):
        import route
        self.assertEqual(route.dots(self.WAYS, False),
                         ["done", "done", "done", "here", "todo", "goal"])
        self.assertEqual(route.dots([(k, t, True) for k, t, _ in self.WAYS], True)[-1],
                         "goal_reached")
        rows = route.drifted({"Stocks": 82.0, "Bonds": 18.0}, {"Stocks": 70, "Bonds": 25,
                                                               "Cash": 5}, 5.0)
        self.assertEqual([r[0] for r in rows], ["Stocks", "Bonds"])   # biggest first; Cash 5 pts
        self.assertEqual(route.drifted({"Stocks": 72.0}, {"Stocks": 70}, 5.0), [])


class AdvisorRequestTests(TempDBMixin, unittest.TestCase):
    """Asking for advisor access (auth.request_advisor): nobody makes themselves
    an advisor; the admin approves or declines (manage_users.py)."""

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)
        made = auth.sign_up(self.conn, "adv@example.com", "goodpass1", agreed=True, adult=True,
                            terms_version="v", seconds_open=10)
        self.uid = made["user_id"]

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _run(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage_users.main(["--db", self.db, *args])
        return code, out.getvalue()

    def test_details_are_checked(self):
        self.assertIn("firm", auth.advisor_request_error("", "123"))
        self.assertIn("licence", auth.advisor_request_error("Acme Wealth", " "))
        self.assertIn("firm", auth.advisor_request_error("x" * 101, "123"))
        self.assertIsNone(auth.advisor_request_error("Acme Wealth", "CRD 1234567"))
        with self.assertRaises(ValueError):
            auth.request_advisor(self.conn, self.uid, "", "")

    def test_a_request_is_not_advisor_access_until_approved(self):
        auth.request_advisor(self.conn, self.uid, " Acme Wealth ", "1234567")
        self.assertFalse(auth.is_advisor(self.conn, self.uid))
        req = auth.advisor_request(self.conn, self.uid)
        self.assertEqual((req["firm"], req["licence"], req["decision"]),
                         ("Acme Wealth", "1234567", None))
        code, out = self._run("advisor-requests")
        self.assertIn("adv@example.com", out)
        self.assertIn("Acme Wealth", out)
        code, out = self._run("make-advisor", "adv@example.com")
        self.assertEqual(code, 0)
        self.assertTrue(auth.is_advisor(self.conn, self.uid))
        self.assertEqual(auth.advisor_request(self.conn, self.uid)["decision"], "approved")
        self.assertIn("No advisor requests", self._run("advisor-requests")[1])

    def test_decline_keeps_an_investor_account(self):
        auth.request_advisor(self.conn, self.uid, "Acme Wealth", "1234567")
        code, _ = self._run("decline-advisor", "adv@example.com")
        self.assertEqual(code, 0)
        self.assertFalse(auth.is_advisor(self.conn, self.uid))
        self.assertEqual(auth.advisor_request(self.conn, self.uid)["decision"], "declined")
        self.assertEqual(self._run("decline-advisor", "adv@example.com")[0], 1)  # none waiting
        # asking again replaces the old request and waits for a new decision
        auth.request_advisor(self.conn, self.uid, "Acme Wealth", "7654321")
        self.assertIsNone(auth.advisor_request(self.conn, self.uid)["decision"])
        self.assertIsNone(auth.advisor_request(self.conn, self.user_id))   # never asked

    def test_the_admin_is_emailed_the_details(self):
        import mailer
        sent = {}
        with unittest.mock.patch.object(mailer, "send",
                                        lambda to, subject, text, html=None: sent.update(
                                            to=to, subject=subject, text=text) or True):
            mailer.advisor_request("adv@example.com", "Acme Wealth", "1234567")
        self.assertEqual(sent["to"], mailer.REPLY_TO)
        self.assertIn("Acme Wealth", sent["subject"])
        self.assertIn("make-advisor adv@example.com", sent["text"])


class EmailLinkTests(TempDBMixin, unittest.TestCase):
    """Confirming an email and resetting a password from emailed links (auth.py)."""

    NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)
        made = auth.sign_up(self.conn, "Pat@Example.com", "firstpass1", agreed=True, adult=True,
                            terms_version="v", ip="203.0.113.9", seconds_open=10, now=self.NOW)
        self.uid = made["user_id"]

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_confirm_link_works_once_and_unlocks_the_ai(self):
        import ai_usage
        self.assertFalse(auth.email_status(self.conn, self.uid)["confirmed"])
        st_ = ai_usage.status(self.conn, self.uid, "chat", now=self.NOW)
        self.assertFalse(st_["ok"])
        self.assertTrue(st_["unconfirmed"])
        self.assertIn("Confirm your email", ai_usage.used_up_text(st_, "chat"))
        # accounts made by an admin have no email and are never held back
        self.assertTrue(ai_usage.status(self.conn, self.user_id, "chat", now=self.NOW)["ok"])

        link = auth.start_confirmation(self.conn, self.uid, ip="203.0.113.9", now=self.NOW)
        self.assertEqual(link["to"], "pat@example.com")
        stored = [r["token_hash"] for r in self.conn.execute("SELECT token_hash FROM email_tokens")]
        self.assertNotIn(link["token"], stored)                  # only the hash is kept
        done = auth.confirm_email(self.conn, link["token"], now=self.NOW)
        self.assertTrue(done["ok"])
        self.assertTrue(auth.email_status(self.conn, self.uid)["confirmed"])
        self.assertTrue(ai_usage.status(self.conn, self.uid, "chat", now=self.NOW)["ok"])
        self.assertFalse(auth.confirm_email(self.conn, link["token"], now=self.NOW)["ok"])  # used
        self.assertFalse(auth.start_confirmation(self.conn, self.uid, now=self.NOW)["ok"])

    def test_confirm_links_expire_are_replaced_and_limited(self):
        first = auth.start_confirmation(self.conn, self.uid, now=self.NOW)
        soon = auth.start_confirmation(self.conn, self.uid, now=self.NOW + timedelta(minutes=1))
        self.assertFalse(soon["ok"])
        self.assertIn("just sent", soon["error"])
        later = self.NOW + timedelta(minutes=3)
        second = auth.start_confirmation(self.conn, self.uid, now=later)
        self.assertTrue(second["ok"])
        self.assertFalse(auth.confirm_email(self.conn, first["token"], now=later)["ok"])  # replaced
        expired = later + timedelta(days=auth.CONFIRM_DAYS, minutes=1)
        self.assertFalse(auth.confirm_email(self.conn, second["token"], now=expired)["ok"])
        t = self.NOW
        for _ in range(auth.CONFIRMS_PER_DAY - 2):
            t += timedelta(minutes=5)
            self.assertTrue(auth.start_confirmation(self.conn, self.uid, now=t)["ok"])
        self.assertIn("tomorrow", auth.start_confirmation(
            self.conn, self.uid, now=t + timedelta(minutes=5))["error"])

    def test_reset_answer_is_the_same_with_or_without_an_account(self):
        known = auth.request_password_reset(self.conn, "PAT@example.com", ip="1.2.3.4", now=self.NOW)
        unknown = auth.request_password_reset(self.conn, "nobody@example.com", ip="1.2.3.4",
                                              now=self.NOW)
        self.assertEqual((known["ok"], known["error"]), (unknown["ok"], unknown["error"]))
        self.assertEqual(known["to"], "pat@example.com")
        self.assertIsNone(unknown["token"])
        self.assertIn("email address", auth.request_password_reset(
            self.conn, "not-an-email", now=self.NOW)["error"])

    def test_reset_sets_the_password_signs_out_and_confirms(self):
        session = auth.create_session(self.conn, self.uid, now=self.NOW)
        for _ in range(3):  # a lock from wrong guesses
            auth.attempt_login(self.conn, "pat@example.com", "wrong", now=self.NOW)
        req = auth.request_password_reset(self.conn, "pat@example.com", now=self.NOW)
        self.assertEqual(auth.reset_info(self.conn, req["token"], now=self.NOW)["email"],
                         "pat@example.com")
        self.assertIn("at least", auth.reset_password(self.conn, req["token"], "short",
                                                      now=self.NOW)["error"])
        done = auth.reset_password(self.conn, req["token"], "secondpass2", now=self.NOW)
        self.assertTrue(done["ok"])
        self.assertEqual(auth.verify_login(self.conn, "pat@example.com", "secondpass2"), self.uid)
        self.assertIsNone(auth.verify_login(self.conn, "pat@example.com", "firstpass1"))
        self.assertIsNone(auth.session_user(self.conn, session, now=self.NOW))  # signed out
        self.assertTrue(auth.email_status(self.conn, self.uid)["confirmed"])
        self.assertFalse(auth.reset_password(self.conn, req["token"], "thirdpass3",
                                             now=self.NOW)["ok"])                 # used up
        stale = auth.request_password_reset(self.conn, "pat@example.com",
                                            now=self.NOW + timedelta(minutes=1))
        late = self.NOW + timedelta(minutes=auth.RESET_MINUTES + 2)
        self.assertIsNone(auth.reset_info(self.conn, stale["token"], now=late))  # expired

    def test_reset_limits_per_email_and_per_address(self):
        for i in range(auth.RESETS_PER_EMAIL_PER_HOUR):
            self.assertTrue(auth.request_password_reset(
                self.conn, "pat@example.com", ip=f"10.0.0.{i}", now=self.NOW)["ok"])
        self.assertIn("in an hour", auth.request_password_reset(
            self.conn, "pat@example.com", ip="10.0.0.99", now=self.NOW)["error"])
        # one address asking for many different emails
        t = self.NOW + timedelta(hours=2)
        for i in range(auth.EMAILS_PER_ADDRESS_PER_HOUR):
            auth.request_password_reset(self.conn, f"x{i}@example.com", ip="198.51.100.5", now=t)
        self.assertIn("from here", auth.request_password_reset(
            self.conn, "y@example.com", ip="198.51.100.5", now=t)["error"])
        keys = [r["email_key"] for r in self.conn.execute("SELECT email_key FROM email_sends")]
        self.assertTrue(all("@" not in k for k in keys))  # hashes, not addresses


class MailerTests(unittest.TestCase):
    """mailer.py: the request sent to Resend, and the dry run."""

    def _env(self, **values):
        import mailer
        return unittest.mock.patch.object(mailer, "_setting", lambda name: values.get(name, ""))

    def test_sends_to_resend_with_the_key(self):
        import mailer
        seen = {}

        class _Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout):
            seen.update(url=req.full_url, auth=req.get_header("Authorization"),
                        body=json.loads(req.data))
            return _Resp()

        with self._env(RESEND_API_KEY="re_test"), \
                unittest.mock.patch.object(mailer.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(mailer.reset_password("pat@example.com", "https://x/?reset=abc", 60))
        self.assertEqual(seen["url"], mailer.API_URL)
        self.assertEqual(seen["auth"], "Bearer re_test")
        self.assertEqual(seen["body"]["to"], ["pat@example.com"])
        self.assertEqual(seen["body"]["from"], mailer.SENDER)
        self.assertIn("https://x/?reset=abc", seen["body"]["text"])
        self.assertIn("https://x/?reset=abc", seen["body"]["html"])

    def test_dry_run_and_missing_key_never_call_out(self):
        import mailer
        boom = unittest.mock.Mock(side_effect=AssertionError("network used"))
        with unittest.mock.patch.object(mailer.urllib.request, "urlopen", boom), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            with self._env(MAIL_DRY_RUN="1", RESEND_API_KEY="re_test"):
                self.assertTrue(mailer.confirm_email("pat@example.com", "https://x/?confirm=t", 3))
            with self._env():
                self.assertFalse(mailer.confirm_email("pat@example.com", "https://x/?confirm=t", 3))
        self.assertIn("?confirm=t", log.getvalue())
        self.assertIn("RESEND_API_KEY isn't set", log.getvalue())


class BulkCreateTests(TempDBMixin, unittest.TestCase):
    def test_parse_user_list_skips_blanks_and_comments(self):
        text = "alice,pw1\n\n# a comment\nbob\n  carol , pw3  \n"
        self.assertEqual(manage_users.parse_user_list(text), [
            ("alice", "pw1"), ("bob", None), ("carol", "pw3"),
        ])

    def test_bulk_create_generates_password_when_omitted_and_skips_existing(self):
        conn = portfolio.connect(self.db)
        path = os.path.join(self.dir, "users.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("testuser\nnewperson,chosenpw\n")  # testuser already exists (TempDBMixin)

        args = argparse.Namespace(db=self.db, file=path)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = manage_users.cmd_bulk_create(args)
        self.assertEqual(rc, 0)
        self.assertIn("already exists", out.getvalue())

        # newperson created with the chosen password (not regenerated)
        self.assertEqual(auth.verify_login(conn, "newperson", "chosenpw"), auth.get_user_id(conn, "newperson"))
        # testuser's original password is untouched
        self.assertEqual(auth.verify_login(conn, "testuser", "testpass"), self.user_id)


class PlanMathTests(unittest.TestCase):
    TODAY = date(2026, 9, 29)

    def test_future_value(self):
        self.assertAlmostEqual(plans.future_value(1000, 100, 0, 12), 2200)            # no growth
        self.assertAlmostEqual(plans.future_value(10000, 0, 6, 12), 10600, places=6)  # one year at 6%
        self.assertEqual(plans.future_value(500, 50, 6, -3), 500)                     # no negative time

    def test_required_monthly_reaches_the_target(self):
        need = plans.required_monthly(20000, 500000, 6, 360)
        self.assertAlmostEqual(plans.future_value(20000, need, 6, 360), 500000, places=4)
        self.assertEqual(plans.required_monthly(500000, 100000, 6, 12), 0.0)  # already enough
        self.assertIsNone(plans.required_monthly(1000, 5000, 6, 0))          # no time left

    def test_months_until_and_add_months(self):
        self.assertEqual(plans.months_until("2027-09-29", self.TODAY), 12)
        self.assertEqual(plans.months_until("2027-09-28", self.TODAY), 11)    # a day short
        self.assertEqual(plans.months_until("2026-01-01", self.TODAY), -9)
        self.assertEqual(plans.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(plans.add_months(date(2026, 11, 15), 3), date(2027, 2, 15))

    def _plan(self, target, when, monthly=0):
        return {"target_amount": target, "target_date": when, "monthly_contribution": monthly}

    def test_progress_statuses(self):
        today = self.TODAY
        status = lambda plan, value: plans.progress(plan, value, today=today)["status"]  # noqa: E731
        self.assertEqual(status(self._plan(10000, "2030-01-01"), 12000), "reached")
        self.assertEqual(status(self._plan(10000, "2026-01-01"), 5000), "past_date")
        # 10 years at 6% turns 10k into ~17.9k: 15k is on track
        self.assertEqual(status(self._plan(15000, "2036-09-29"), 10000), "on_track")
        # ~20.7k at 8% but ~17.9k at 6%: 20k only at the optimistic end
        self.assertEqual(status(self._plan(20000, "2036-09-29"), 10000), "within_reach")
        self.assertEqual(status(self._plan(50000, "2036-09-29"), 10000), "behind")
        p = plans.progress(self._plan(50000, "2036-09-29"), 10000, today=today)
        self.assertLess(p["projected_low"], p["projected"])
        self.assertLess(p["projected"], p["projected_high"])
        self.assertAlmostEqual(p["pct_of_target"], 20.0)
        self.assertGreater(p["needed_monthly"], 0)

    def test_projection_series(self):
        rows = plans.projection_series(1000, 100, 24, today=self.TODAY)
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[0]["date"], "2026-09-29")
        self.assertEqual(rows[0]["mid"], 1000)
        self.assertEqual(rows[-1]["date"], "2028-09-29")
        long = plans.projection_series(1000, 100, 600, today=self.TODAY)
        self.assertLessEqual(len(long), 242)                                    # sampled
        self.assertEqual(long[-1]["date"], plans.add_months(self.TODAY, 600).isoformat())


class LearnTests(unittest.TestCase):
    def test_readiness_reads_the_profile(self):
        items = {i["key"]: i["state"] for i in learn.readiness({
            "emergency_fund": "None", "high_interest_debt": "Some",
            "employer_match": "Yes, and I get the full match", "withdrawal_needs": "A large amount"})}
        self.assertEqual(items, {"emergency_fund": learn.STOP, "high_interest_debt": learn.CAUTION,
                                 "employer_match": learn.GOOD, "withdrawal_needs": learn.CAUTION})
        self.assertEqual(learn.readiness_summary(learn.readiness({
            "emergency_fund": "None"})), "Start here first")
        ready = learn.readiness({"emergency_fund": "3-6 months", "high_interest_debt": "None",
                                 "employer_match": "No match or no plan"})
        self.assertEqual(learn.readiness_summary(ready), "Ready to start")
        self.assertEqual(learn.readiness_summary(learn.readiness({})), "Answer a few questions")

    def test_starter_mix_follows_horizon_and_risk(self):
        self.assertIsNone(learn.starter_mix({}))
        long = learn.starter_mix({"time_horizon_years": 30, "risk_tolerance": "moderate"})
        short = learn.starter_mix({"time_horizon_years": 2, "risk_tolerance": "moderate"})
        self.assertGreater(long["stocks_pct"], short["stocks_pct"])
        self.assertTrue(short["short_horizon"])
        careful = learn.starter_mix({"time_horizon_years": 30, "risk_tolerance": "conservative",
                                     "drawdown_reaction": "Sell everything"})
        self.assertLess(careful["stocks_pct"], long["stocks_pct"])
        for mix in (long, short, careful):
            self.assertEqual(sum(mix["weights"].values()), 100)
            self.assertEqual(mix["stocks_pct"] % 5, 0)
            self.assertTrue(mix["reasons"])
        self.assertEqual(learn.starter_mix({"time_horizon_years": 2}, horizon_years=25)["horizon_years"], 25)

    def test_target_date_year(self):
        today = date(2026, 9, 29)
        self.assertEqual(learn.target_date_year({"goal_type": "Retirement", "target_date": "2053-06-01"},
                                                None, today), 2055)
        self.assertEqual(learn.target_date_year(None, "25-34", today), 2060)   # 2026 + 35 = 2061
        self.assertIsNone(learn.target_date_year(None, "65 or older", today))

    def test_fee_cost_is_positive_and_grows_with_time(self):
        short, long = (learn.fee_cost(200, y, 6, 0.05, 1.0) for y in (10, 30))
        self.assertGreater(short, 0)
        self.assertGreater(long, short * 3)
        self.assertAlmostEqual(learn.grow_monthly(100, 1, 0), 1200)

    def test_simulate_monthly_buys_and_value(self):
        # one fund that doubles over the period: two buys of 100
        prices = {"AAA": [("2026-01-05", 10.0), ("2026-01-20", 12.0), ("2026-02-02", 20.0)]}
        rows = learn.simulate(prices, {"AAA": 1}, monthly=100)
        self.assertEqual([r["money_in"] for r in rows], [100, 100, 200])   # once per month
        self.assertAlmostEqual(rows[-1]["value"], 10 * 20 + 100)            # 10 units + new 100
        self.assertAlmostEqual(rows[-1]["nav"], 2.0)                        # price doubled
        # two funds, only shared days count
        both = learn.simulate({"A": [("d1", 1.0), ("d2", 1.0)], "B": [("d2", 2.0)]},
                              {"A": 50, "B": 50}, monthly=0, initial=100)
        self.assertEqual([r["date"] for r in both], ["d2"])
        self.assertEqual(learn.simulate(prices, {}, monthly=100), [])

    def test_max_drawdown_ignores_contributions(self):
        prices = {"A": [("2026-01-02", 10.0), ("2026-02-02", 5.0), ("2026-03-02", 10.0)]}
        rows = learn.simulate(prices, {"A": 1}, monthly=1000)
        self.assertAlmostEqual(learn.max_drawdown(rows), -50.0)             # the price halved
        self.assertIsNone(learn.max_drawdown(rows[:1]))


class PlanStorageTests(TempDBMixin, unittest.TestCase):
    def test_save_merges_and_records_who_saved(self):
        conn = portfolio.connect(self.db)
        self.assertIsNone(plans.get_plan(conn, self.user_id))
        plans.save_plan(conn, self.user_id, {"target_alloc": {"Equity": 70, "Cash": 0}}, set_by=self.user_id)
        plan = plans.save_plan(conn, self.user_id, {"goal_type": "Retirement", "target_amount": 900000,
                                                    "target_date": "2055-01-01"}, set_by=99)
        self.assertEqual(plan["target_alloc"], {"Equity": 70.0})   # kept, and zero targets dropped
        self.assertEqual(plan["set_by"], 99)
        self.assertTrue(plans.has_goal(plan))
        plan = plans.save_plan(conn, self.user_id, {"target_amount": None}, set_by=self.user_id)
        self.assertFalse(plans.has_goal(plan))                       # None clears a field
        conn.close()

    def test_contributions_are_per_user(self):
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "other", "pw")
        plans.add_contribution(conn, self.user_id, "2026-09-02", 500, " paycheck ")
        plans.add_contribution(conn, self.user_id, "2026-09-20", -200)
        plans.add_contribution(conn, self.user_id, "2026-08-31", 300)
        plans.add_contribution(conn, other, "2026-09-05", 1000)
        self.assertEqual(plans.month_total(conn, self.user_id, 2026, 9), 300.0)
        rows = plans.list_contributions(conn, self.user_id)
        self.assertEqual([r["date"] for r in rows], ["2026-09-20", "2026-09-02", "2026-08-31"])
        self.assertEqual(rows[1]["note"], "paycheck")
        plans.delete_contribution(conn, other, rows[0]["id"])       # someone else's id: no effect
        self.assertEqual(len(plans.list_contributions(conn, self.user_id)), 3)
        plans.delete_contribution(conn, self.user_id, rows[0]["id"])
        self.assertEqual(plans.month_total(conn, self.user_id, 2026, 9), 500.0)
        conn.close()

    def test_money_in_history_adds_up(self):
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        hist = plans.money_in_history(conn, self.user_id)
        self.assertEqual(len(hist), 1)
        h = hist[0]
        mv = conn.execute("SELECT SUM(market_value) AS s FROM positions WHERE user_id = ?",
                          (self.user_id,)).fetchone()["s"]
        cash = conn.execute("SELECT SUM(cash_value) AS s FROM account_totals WHERE user_id = ?",
                            (self.user_id,)).fetchone()["s"]
        self.assertAlmostEqual(h["value"], mv + cash)
        self.assertAlmostEqual(h["money_in"] + h["growth"], h["value"])
        conn.close()


class PrefsTests(TempDBMixin, unittest.TestCase):
    def test_round_trip_and_legacy_file_carries_over(self):
        conn = portfolio.connect(self.db)
        self.assertEqual(prefs.load(conn, self.user_id), {})
        legacy = os.path.join(os.path.dirname(self.db), "old_prefs.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump({"hide_amounts": True, "rules": {"day_change_pct": 3}}, fh)
        other = auth.create_user(conn, "other", "pw")
        self.assertEqual(prefs.load(conn, other, legacy)["hide_amounts"], True)   # imported once
        os.remove(legacy)
        self.assertEqual(prefs.load(conn, other, legacy)["rules"], {"day_change_pct": 3})  # now in the db
        prefs.save(conn, other, {"columns": ["symbol"]})
        self.assertEqual(prefs.load(conn, other), {"columns": ["symbol"]})
        self.assertEqual(prefs.load(conn, self.user_id), {})                     # per user
        conn.close()


class AccountLabelTests(TempDBMixin, unittest.TestCase):
    def test_set_display_and_clear(self):
        conn = portfolio.connect(self.db)
        accounts.set_label(conn, self.user_id, "Individual ...111", "  Roth IRA ")
        names = accounts.labels(conn, self.user_id)
        self.assertEqual(names, {"Individual ...111": "Roth IRA"})              # trimmed
        self.assertEqual(accounts.display("Individual ...111", names), "Roth IRA")
        self.assertEqual(accounts.display("Individual ...222", names), "Individual ...222")
        accounts.set_label(conn, self.user_id, "Individual ...111", "New name")   # replaces
        self.assertEqual(accounts.labels(conn, self.user_id), {"Individual ...111": "New name"})
        accounts.set_label(conn, self.user_id, "Individual ...111", "  ")         # blank clears
        self.assertEqual(accounts.labels(conn, self.user_id), {})
        conn.close()

    def test_labels_are_per_user(self):
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "someone_else", "testpass")
        accounts.set_label(conn, self.user_id, "Individual ...111", "Mine")
        self.assertEqual(accounts.labels(conn, other), {})
        conn.close()

    def test_clash_catches_a_shared_display_name(self):
        accts = ["Individual ...111", "Individual ...222"]
        names = {"Individual ...111": "Brokerage"}
        self.assertEqual(accounts.clash("Individual ...222", "brokerage", accts, names),
                         "Individual ...111")                                  # case-insensitive
        self.assertIsNone(accounts.clash("Individual ...111", "Brokerage", accts, names))  # itself
        self.assertEqual(accounts.clash("Individual ...111", "Individual ...222", accts, {}),
                         "Individual ...222")                                  # another's broker name
        self.assertIsNone(accounts.clash("Individual ...222", "", accts, names))


class WatchlistTests(TempDBMixin, unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(watchlist.normalize("  nvda "), "NVDA")
        self.assertEqual(watchlist.normalize("brk.b"), "BRK.B")
        self.assertIsNone(watchlist.normalize(""))
        self.assertIsNone(watchlist.normalize("not a ticker"))       # spaces aren't valid
        self.assertIsNone(watchlist.normalize("way-too-long-for-a-ticker"))

    def test_add_list_remove_is_idempotent(self):
        conn = portfolio.connect(self.db)
        self.assertEqual(watchlist.add(conn, self.user_id, " nvda "), "NVDA")
        self.assertEqual(watchlist.add(conn, self.user_id, "NVDA"), "NVDA")        # re-add is a no-op, not a dupe
        self.assertIsNone(watchlist.add(conn, self.user_id, "not valid"))
        self.assertEqual(watchlist.list_tickers(conn, self.user_id), ["NVDA"])
        watchlist.remove(conn, self.user_id, "NVDA")
        self.assertEqual(watchlist.list_tickers(conn, self.user_id), [])
        conn.close()

    def test_all_sync_tickers_merges_held_and_watchlist(self):
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        watchlist.add(conn, self.user_id, "NVDA")
        held = {r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM positions")}
        merged = watchlist.all_sync_tickers(conn)
        self.assertEqual(set(merged), held | {"NVDA"})
        self.assertEqual(merged, sorted(merged))                    # sorted, no dupes
        conn.close()


class NewsTests(TempDBMixin, unittest.TestCase):
    """No network calls - fetch_company_news is never exercised here, only
    the storage/caching layer around it."""

    ARTICLES = [
        {"id": 101, "headline": "Widgets up 5%", "summary": "A summary.", "source": "Reuters",
         "url": "https://example.com/101", "datetime": 1_700_000_000},
        {"id": 102, "headline": "CEO speaks", "summary": "Another summary.", "source": "Bloomberg",
         "url": "https://example.com/102", "datetime": 1_700_003_600},
    ]

    def test_upsert_is_idempotent_and_skips_incomplete_articles(self):
        conn = portfolio.connect(self.db)
        n = news.upsert_news(conn, "AAPL", self.ARTICLES)
        self.assertEqual(n, 2)
        n_again = news.upsert_news(conn, "AAPL", self.ARTICLES)     # re-sync, same articles
        self.assertEqual(n_again, 0)                                # INSERT OR IGNORE, no dupes
        # missing id or headline -> silently skipped, not an error
        n_bad = news.upsert_news(conn, "AAPL", [{"headline": "no id"}, {"id": 999}])
        self.assertEqual(n_bad, 0)
        rows = news.latest_news(conn, "AAPL")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["id"] for r in rows}, {101, 102})
        conn.close()

    def test_published_at_converted_from_epoch(self):
        conn = portfolio.connect(self.db)
        news.upsert_news(conn, "AAPL", self.ARTICLES)
        rows = {r["id"]: r for r in news.latest_news(conn, "AAPL")}
        self.assertEqual(rows[101]["published_at"], "2023-11-14T22:13:20Z")
        conn.close()

    def test_needs_refresh_true_when_empty_false_after_fetch(self):
        conn = portfolio.connect(self.db)
        self.assertTrue(news.needs_refresh(conn, "AAPL"))           # nothing cached yet
        news.upsert_news(conn, "AAPL", self.ARTICLES)
        self.assertFalse(news.needs_refresh(conn, "AAPL"))          # just fetched, still fresh
        self.assertTrue(news.needs_refresh(conn, "AAPL", max_age_hours=0))  # force-stale
        self.assertTrue(news.needs_refresh(conn, "MSFT"))           # different ticker, never cached
        conn.close()

    def test_sync_ticker_skips_network_call_when_fresh(self):
        # sync_ticker's network path isn't exercised (no real fetch_company_news
        # call happens if the cache is already fresh) - this proves the guard
        # itself, i.e. that a fresh cache short-circuits before ever calling
        # out, by checking it returns (0, "") with no key required to work.
        conn = portfolio.connect(self.db)
        news.upsert_news(conn, "AAPL", self.ARTICLES)
        n, err = news.sync_ticker(conn, "AAPL", token="not-a-real-key")
        self.assertEqual((n, err), (0, ""))
        conn.close()


class ClientsOverviewTests(TempDBMixin, unittest.TestCase):
    def test_account_summary_matches_fixture(self):
        conn = portfolio.connect(self.db)
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        advisor.save_profile(conn, self.user_id, {"goal": "retire", "risk_tolerance": "moderate"})
        s = overview.account_summary(conn, self.user_id, quotes={})
        # fixture: AAA 1200 + BBB 450 + CCC 1600 holdings, 100 + 50 cash
        self.assertEqual(s["portfolio_value"], 3400.0)
        self.assertEqual(s["n_positions"], 3)
        self.assertEqual(s["snapshot_date"], "2026-01-15")
        self.assertIsNotNone(s["imported_at"])
        # cost 3500 vs value 3250 -> -7.14%
        self.assertAlmostEqual(s["gain_pct"], -7.14, places=2)
        # default rules: CCC day move -6.5% (>5) and CCC total -20% is not > 20 -> 1 alert
        self.assertEqual(s["n_alerts"], 1)
        self.assertEqual((s["profile_answered"], s["profile_total"]),
                         (2, len(advisor.REQUIRED_PROFILE_FIELDS)))
        conn.close()

    def test_account_summary_empty_account(self):
        conn = portfolio.connect(self.db)
        s = overview.account_summary(conn, self.user_id, quotes={})
        self.assertFalse(s["has_data"])
        self.assertIsNone(s["portfolio_value"])
        self.assertEqual(s["profile_answered"], 0)
        conn.close()


class AdvisingTests(TempDBMixin, unittest.TestCase):
    def _pair(self, conn):
        auth.set_advisor(conn, "testuser", True)
        return auth.create_client(conn, self.user_id, "client1")

    def test_notes_private_and_next_steps(self):
        conn = portfolio.connect(self.db)
        client = self._pair(conn)
        self.assertEqual(advising.advisor_of(conn, client), self.user_id)
        self.assertIsNone(advising.advisor_of(conn, self.user_id))
        advising.add_note(conn, client, self.user_id, "Review", "Went over the plan", "2026-06-01")
        advising.add_note(conn, client, self.user_id, "Next step", "Raise monthly to $600", "2026-06-01")
        advising.add_note(conn, client, self.user_id, "Note", "Nervous about markets", "2026-06-02",
                          private=True)
        with self.assertRaises(ValueError):
            advising.add_note(conn, client, self.user_id, "Note", "   ", "2026-06-02")
        with self.assertRaises(ValueError):
            advising.add_note(conn, client, self.user_id, "Gossip", "x", "2026-06-02")
        seen = advising.list_notes(conn, client, include_private=False)
        self.assertEqual({n["body"] for n in seen}, {"Went over the plan", "Raise monthly to $600"})
        self.assertEqual(len(advising.list_notes(conn, client, include_private=True)), 3)
        step = advising.open_next_steps(seen)[0]
        advising.set_done(conn, self.user_id, step["id"], True)       # wrong client id: no effect
        self.assertEqual(len(advising.open_next_steps(advising.list_notes(conn, client, include_private=False))), 1)
        advising.set_done(conn, client, step["id"], True)
        self.assertEqual(advising.open_next_steps(advising.list_notes(conn, client, include_private=False)), [])
        self.assertEqual(advising.last_review(conn, client), "2026-06-01")
        conn.close()

    def test_review_status(self):
        today = date(2026, 9, 29)
        self.assertEqual(advising.review_status(None, today), ("never", None))
        self.assertEqual(advising.review_status("2026-09-01", today), ("ok", 28))
        self.assertEqual(advising.review_status("2026-05-01", today)[0], "due")

    def test_models_validate_and_replace_by_name(self):
        conn = portfolio.connect(self.db)
        with self.assertRaises(ValueError):
            advising.save_model(conn, self.user_id, "Bad", {"Equity": 50})
        with self.assertRaises(ValueError):
            advising.save_model(conn, self.user_id, " ", {"Equity": 100})
        advising.save_model(conn, self.user_id, "Growth", {"Equity": 80, "Cash": 20, "Fixed Income": 0})
        advising.save_model(conn, self.user_id, "Growth", {"Equity": 90, "Cash": 10})
        models = advising.list_models(conn, self.user_id)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["target_alloc"], {"Equity": 90.0, "Cash": 10.0})
        self.assertEqual(advising.mix_text(models[0]["target_alloc"]), "Equity 90% · Cash 10%")
        other = auth.create_user(conn, "other", "pw")
        advising.delete_model(conn, other, models[0]["id"])            # not theirs
        self.assertEqual(len(advising.list_models(conn, self.user_id)), 1)
        self.assertEqual(advising.list_models(conn, other), [])
        conn.close()

    def test_drift_and_attention(self):
        self.assertIsNone(advising.max_drift({"Equity": 60}, {}))
        self.assertAlmostEqual(advising.max_drift({"Equity": 70, "Cash": 30},
                                                  {"Equity": 60, "Fixed Income": 20}), 20)
        self.assertEqual(advising.attention(has_data=True, goal_status="on_track", review="ok",
                                            n_alerts=0, drift=2.0, profile_done=True), [])
        reasons = advising.attention(has_data=True, goal_status="behind", review="due", n_alerts=2,
                                     drift=12.4, profile_done=False)
        self.assertEqual(reasons, ["Goal behind", "Review due", "2 alerts", "Drift 12 pts",
                                   "Profile incomplete"])
        self.assertIn("No goal", advising.attention(has_data=False, goal_status=None, review="never",
                                                    n_alerts=0, drift=None, profile_done=True))


class PostgresPrecisionTests(unittest.TestCase):
    """Postgres REAL is 4-byte and loses cents on large amounts, so every
    decimal column must be DOUBLE PRECISION there - new databases via
    schema_pg.sql, older ones converted by _widen_real_columns()."""

    @staticmethod
    def _columns(sql_text):
        out = {}
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql_text, re.S):
            for line in m.group(2).splitlines():
                parts = line.split("--")[0].split()
                if len(parts) >= 2 and parts[0].isidentifier() and parts[0].upper() != "PRIMARY":
                    out[(m.group(1), parts[0])] = " ".join(parts[1:3]).rstrip(",").upper()
        return out

    def test_every_sqlite_real_column_is_double_precision_on_postgres(self):
        with open(os.path.join(REPO, "schema.sql"), encoding="utf-8") as fh:
            lite = self._columns(fh.read())
        with open(os.path.join(REPO, "schema_pg.sql"), encoding="utf-8") as fh:
            pg = self._columns(fh.read())
        reals = [k for k, t in lite.items() if t.startswith("REAL")]
        self.assertGreater(len(reals), 40)
        for key in reals:
            self.assertTrue(pg[key].startswith("DOUBLE PRECISION"), key)
        self.assertFalse([k for k, t in pg.items() if t.startswith("REAL")])

    def test_older_database_real_columns_are_widened_once_per_table(self):
        executed = []
        reals = [("positions", "cost_basis"), ("positions", "market_value"),
                 ("daily_bars", "close"), ("someone_elses_table", "x")]

        class Col:
            def __init__(self, name):
                self.name = name

        class FakeCursor:
            def __init__(self):
                self.rows, self.description = [], None

            def execute(self, sql, params=None):
                executed.append(sql)
                if "data_type = 'real'" in sql:
                    self.rows = list(reals)
                    self.description = [Col("table_name"), Col("column_name")]
                elif "information_schema.columns WHERE table_name" in sql:
                    # every back-filled column already there except one REAL
                    # one (all the tables' columns come back in one query)
                    self.rows = [(t, c) for t in params
                                 for c in ("live_price_at", "user_id", "is_advisor")
                                 + tuple(n for n, _ in portfolio.PROFILE_EXTRA_COLS)
                                 + ("live_price", "live_market_value", "live_unrealized_gain_pct",
                                    "day_open", "day_high", "day_low", "realized_gain")]
                    self.description = [Col("table_name"), Col("column_name")]
                else:
                    self.rows, self.description = [], None

            def __iter__(self):
                return iter(self.rows)

            def fetchall(self):
                return list(self.rows)

        class FakeRaw:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                executed.append("COMMIT")

        with contextlib.redirect_stderr(io.StringIO()) as err:
            portfolio._ensure_schema(pgcompat.ConnWrapper(FakeRaw()))
        alters = [q for q in executed if q.startswith('ALTER TABLE "')]
        self.assertEqual(alters, [
            'ALTER TABLE "positions" ALTER COLUMN "cost_basis" TYPE DOUBLE PRECISION, '
            'ALTER COLUMN "market_value" TYPE DOUBLE PRECISION',
            'ALTER TABLE "daily_bars" ALTER COLUMN "close" TYPE DOUBLE PRECISION'])  # not someone else's
        self.assertIn("ALTER TABLE positions ADD COLUMN live_unrealized_gain DOUBLE PRECISION", executed)
        # every table's columns in one query (a round trip each on Postgres)
        self.assertEqual(sum("information_schema.columns WHERE table_name" in q
                             for q in executed), 1)
        self.assertEqual(executed[-1], "COMMIT")
        self.assertIn("3 REAL column(s)", err.getvalue())


class ClientPlanTests(TempDBMixin, unittest.TestCase):
    _contexts = AdvisorTests._contexts

    def _facts(self):
        ctxs, cash = self._contexts()
        conn = portfolio.connect(self.db)
        advisor.save_profile(conn, self.user_id, {"goal": "retire", "risk_tolerance": "moderate"})
        facts = client_plan.build_facts(conn, self.user_id, ctxs, cash)
        summary = overview.account_summary(conn, self.user_id, quotes={})
        conn.close()
        return facts, summary, ctxs, cash

    def test_build_facts_matches_clients_overview(self):
        facts, summary, _, _ = self._facts()
        self.assertEqual(facts["summary"]["portfolio_value"], summary["portfolio_value"])
        self.assertEqual(facts["summary"]["gain_pct"], summary["gain_pct"])
        self.assertEqual(facts["cash"], 150.0)
        self.assertEqual([h["symbol"] for h in facts["holdings"]], ["CCC", "AAA", "BBB"])
        self.assertAlmostEqual(sum(h["weight_pct"] for h in facts["holdings"]), 3250 / 3400 * 100)
        # CCC 1600 / 3400 = 47% and AAA 1200 / 3400 = 35% are both over the 15% limit
        self.assertEqual([c["symbol"] for c in facts["concentration"]], ["CCC", "AAA"])
        self.assertEqual(len(facts["alerts"]), summary["n_alerts"])
        self.assertIn("experience", facts["missing"])

    def test_next_steps_parses_bullets_and_sends_no_dollars(self):
        _, _, ctxs, cash = self._facts()
        client = _FakeCreateClient("Here you go:\n- Add a bond fund\n- Trim CCC\n")
        steps = client_plan.next_steps(client, {"goal": "retire"},
                                       advisor.portfolio_summary(ctxs, cash),
                                       "User: how am I doing?", "- house ~2029")
        self.assertEqual(steps, ["Add a bond fund", "Trim CCC"])
        sent = json.dumps({"system": client.kwargs["system"], "messages": client.kwargs["messages"]})
        self.assertNotIn("$", sent)
        self.assertNotIn("Individual", sent)
        self.assertIn("how am I doing", sent)
        self.assertIn("house ~2029", sent)  # the assistant's notes inform the plan

    def test_next_steps_refusal_and_plain_text(self):
        refusing = _Obj(messages=_Obj(create=lambda **kw: _Obj(stop_reason="refusal", content=[])))
        self.assertIsNone(client_plan.next_steps(refusing, {}, "summary"))
        plain = _Obj(messages=_Obj(create=lambda **kw: _Obj(
            stop_reason="end_turn", content=[_Obj(type="text", text="Just diversify.")])))
        self.assertEqual(client_plan.next_steps(plain, {}, "summary"), ["Just diversify."])

    def test_render_pdf_handles_unicode_empty_account_and_no_steps(self):
        facts, _, _, _ = self._facts()
        pdf = client_plan.render_pdf(facts, ["Diversify \U0001F680 中文 — soon"],
                                     account_name="jsmith", advisor_name="alice")
        self.assertTrue(pdf.startswith(b"%PDF"))
        conn = portfolio.connect(self.db)
        empty_id = auth.create_user(conn, "empty", "pw")
        empty = client_plan.build_facts(conn, empty_id, [], {})
        conn.close()
        self.assertTrue(client_plan.render_pdf(empty, None, account_name="empty").startswith(b"%PDF"))


    def test_plan_pdf_includes_goal_and_advisor_steps_but_not_private_notes(self):
        ctxs, cash = self._contexts()
        conn = portfolio.connect(self.db)
        auth.set_advisor(conn, "testuser", True)
        client = auth.create_client(conn, self.user_id, "client1")
        plans.save_plan(conn, client, {"goal_type": "Retirement", "target_amount": 500000,
                                       "target_date": "2050-01-01", "monthly_contribution": 400},
                        set_by=self.user_id)
        advising.add_note(conn, client, self.user_id, "Next step", "Open a Roth IRA", "2026-09-01")
        advising.add_note(conn, client, self.user_id, "Next step", "SECRET-PRIVATE", "2026-09-01",
                          private=True)
        facts = client_plan.build_facts(conn, client, [], {})
        conn.close()
        self.assertEqual(facts["goal"]["target"], 500000)
        self.assertEqual(facts["advisor_steps"], ["Open a Roth IRA"])
        pdf = client_plan.render_pdf(facts, None, account_name="client1", advisor_name="testuser")
        self.assertTrue(pdf.startswith(b"%PDF"))


class RefreshAllUsersTests(TempDBMixin, unittest.TestCase):
    def test_each_ticker_fetched_once_and_applied_to_every_account(self):
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "other", "pw")
        portfolio.import_csv(conn, FIXTURE, self.user_id)
        shutil.copyfile(FIXTURE, os.path.join(self.dir, "other.csv"))
        portfolio.import_csv(conn, os.path.join(self.dir, "other.csv"), other)

        fetched = []

        def fake_quote(ticker, key, timeout):
            fetched.append(ticker)
            return {"c": 50.0, "pc": 49.0, "d": 1.0, "dp": 2.0, "t": 1700000000}, ""

        with unittest.mock.patch.object(update_prices, "fetch_quote", side_effect=fake_quote):
            summary = update_prices.refresh_all_users(conn, "key", delay=0)

        self.assertEqual(sorted(fetched), ["AAA", "BBB", "CCC"])     # once each, not per account
        self.assertEqual(summary["updated_by_user"], {self.user_id: 3, other: 3})
        prices = {r["live_price"] for r in conn.execute("SELECT live_price FROM positions")}
        self.assertEqual(prices, {50.0})

        # straight after (the open app fetched them a moment ago): nothing fetched again,
        # the recent quotes are still applied
        fetched.clear()
        conn.execute("UPDATE positions SET live_price = NULL")
        conn.commit()
        with unittest.mock.patch.object(update_prices, "fetch_quote", side_effect=fake_quote):
            again = update_prices.refresh_all_users(conn, "key", delay=0)
        self.assertEqual((fetched, again["reused"], again["failed"]), ([], 3, 0))
        self.assertEqual(again["updated_by_user"], {self.user_id: 3, other: 3})
        with unittest.mock.patch.object(update_prices, "fetch_quote", side_effect=fake_quote):
            update_prices.refresh_all_users(conn, "key", delay=0, reuse_within=timedelta(0))
        self.assertEqual(sorted(fetched), ["AAA", "BBB", "CCC"])     # older than allowed: fetched
        conn.close()


class NoSecretsInRepoTests(unittest.TestCase):
    """This repo is public. Fails if anything shaped like a real Anthropic API
    key or a Neon database password sits in a git-tracked file - an API key
    pasted into COMMANDS.txt nearly got pushed once."""

    PATTERNS = {
        "Anthropic API key": re.compile(r"sk-ant-(?:api|admin)\d\d-[A-Za-z0-9_-]{20,}"),
        "Neon database password": re.compile(r"npg_[A-Za-z0-9]{8,}"),
    }

    def test_tracked_files_contain_no_secrets(self):
        try:
            files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                                   text=True, check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git not available")
        found = []
        for rel in files:
            try:
                with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue  # deleted in the working tree, or binary
            found += [f"{rel}: {label}" for label, pat in self.PATTERNS.items() if pat.search(text)]
        self.assertEqual(found, [], "secret-looking values in tracked files - remove them before pushing")


class PgCompatTests(unittest.TestCase):
    """Pure logic only - no live Postgres needed. Every SQLite-specific
    construct the real codebase's SQL strings actually use (verified by
    grepping every .py file), translated exactly as portfolio.connect()
    will need it translated when given a Postgres DSN."""

    def test_is_postgres_dsn(self):
        self.assertTrue(pgcompat.is_postgres_dsn("postgres://u:p@host/db"))
        self.assertTrue(pgcompat.is_postgres_dsn("postgresql://u:p@host/db"))
        self.assertFalse(pgcompat.is_postgres_dsn("portfolio.db"))
        self.assertFalse(pgcompat.is_postgres_dsn(r"C:\scratch\test.db"))

    def test_translate_qmark_placeholders(self):
        self.assertEqual(
            pgcompat.translate_sql("SELECT * FROM t WHERE a = ? AND b = ?"),
            "SELECT * FROM t WHERE a = %s AND b = %s")

    def test_translate_named_placeholders(self):
        self.assertEqual(
            pgcompat.translate_sql("INSERT INTO t (a, b) VALUES (:a, :b)"),
            "INSERT INTO t (a, b) VALUES (%(a)s, %(b)s)")

    def test_translate_datetime_now(self):
        self.assertEqual(
            pgcompat.translate_sql("UPDATE t SET x = datetime('now')"),
            f"UPDATE t SET x = {pgcompat.PG_NOW_EXPR}")
        # exact fragment used in sync_history.py's f-strings
        self.assertEqual(
            pgcompat.translate_sql("VALUES (?, ?, datetime('now'))"),
            f"VALUES (%s, %s, {pgcompat.PG_NOW_EXPR})")

    def test_translate_combines_all_three(self):
        sql = ("INSERT INTO news (id, ticker, fetched_at) "
               "VALUES (:id, :ticker, datetime('now')) "
               "ON CONFLICT DO UPDATE SET x = ?")
        self.assertEqual(
            pgcompat.translate_sql(sql),
            "INSERT INTO news (id, ticker, fetched_at) "
            f"VALUES (%(id)s, %(ticker)s, {pgcompat.PG_NOW_EXPR}) "
            "ON CONFLICT DO UPDATE SET x = %s")

    def test_row_supports_positional_and_string_access(self):
        row = pgcompat.Row((1, "AAPL", 150.0), ("id", "symbol", "price"))
        # positional unpacking (perf.py's `for t, ticker, close in ...`)
        a, b, c = row
        self.assertEqual((a, b, c), (1, "AAPL", 150.0))
        # positional index
        self.assertEqual(row[0], 1)
        # string-key access (used pervasively)
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["price"], 150.0)

    def test_row_supports_dict_conversion(self):
        row = pgcompat.Row((1, "AAPL"), ("id", "symbol"))
        self.assertEqual(dict(row), {"id": 1, "symbol": "AAPL"})

    def test_split_statements_ignores_blank_segments(self):
        script = "CREATE TABLE a (x INT);\n\nCREATE TABLE b (y INT);  "
        stmts = pgcompat._split_statements(script)
        self.assertEqual(len(stmts), 2)
        self.assertTrue(stmts[0].startswith("CREATE TABLE a"))
        self.assertTrue(stmts[1].startswith("CREATE TABLE b"))

    def test_split_statements_ignores_semicolons_inside_comments(self):
        # Caught for real against a live Neon instance: schema_pg.sql's own
        # header comment contains a literal ';' in prose ("needs_refresh();"),
        # which a naive whole-text split on ';' treated as a statement
        # boundary and fed Postgres a garbage fragment starting mid-comment.
        script = ("-- a comment; with a semicolon in it\n"
                  "CREATE TABLE a (x INT);\n"
                  "-- another; comment; with; several\n"
                  "CREATE TABLE b (y INT);")
        stmts = pgcompat._split_statements(script)
        self.assertEqual(len(stmts), 2)
        self.assertTrue(stmts[0].startswith("CREATE TABLE a"))
        self.assertTrue(stmts[1].startswith("CREATE TABLE b"))

    def test_schema_pg_uses_the_same_now_expression_as_the_shim(self):
        # schema_pg.sql's DEFAULT clauses are written out literally (can't
        # reference a Python constant from SQL) - this pins them to
        # pgcompat.PG_NOW_EXPR so the two can't silently drift apart and
        # produce two different timestamp string shapes.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "schema_pg.sql"), encoding="utf-8") as fh:
            sql = fh.read()
        n = sql.count(pgcompat.PG_NOW_EXPR)
        self.assertGreater(n, 0, "schema_pg.sql doesn't contain PG_NOW_EXPR verbatim")
        # every DEFAULT clause should use it - count DEFAULT occurrences too
        self.assertEqual(n, sql.count("DEFAULT ("))

    def test_close_swallows_rollback_failure_on_a_stale_pooled_connection(self):
        # Reproduced live against Neon: a pooled connection that went stale
        # between checkouts (server closed an idle connection) raised
        # psycopg.OperationalError from rollback() itself, which crashed the
        # whole page instead of just being treated as "discard this
        # connection, the pool will make a fresh one." close() must never
        # propagate a rollback failure.
        class _DeadConn:
            def rollback(self):
                raise OSError("the socket is dead, as a stale pooled connection's would be")

        putconn_calls = []

        class _FakePool:
            def putconn(self, conn):
                putconn_calls.append(conn)

        dead = _DeadConn()
        wrapper = pgcompat.ConnWrapper(dead, pool=_FakePool())
        wrapper.close()  # must not raise
        self.assertEqual(putconn_calls, [dead])  # still handed back for the pool to discard


class ChartsTests(unittest.TestCase):
    def _df(self, n=40):
        idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
        return pd.DataFrame({"t": idx, "v": [100.0 + i for i in range(n)]})

    def test_money_axes(self):
        """Dollar axes read "$0", "$500", "$1.5k", "$2M" (d3's "$,.3~s", trailing
        zeros trimmed), with cents under a dollar instead of "$500m"; the old
        "$,.2s" (which drew 0 as "$0.0") is gone everywhere."""
        self.assertEqual(charts.MONEY_AXIS, "$,.3~s")

        def axes(spec):
            if isinstance(spec, dict):
                if "format" in spec and "labels" in spec:
                    yield spec
                for v in spec.values():
                    yield from axes(v)
            elif isinstance(spec, list):
                for v in spec:
                    yield from axes(v)
        idx = pd.date_range("2026-01-01", periods=6, freq="MS")
        built = [
            charts.line(self._df(10), x="t", y="v", y_title="", y_format=charts.MONEY_AXIS),
            charts.projection(pd.DataFrame({"date": idx, "low": range(6), "mid": range(6),
                                            "high": range(6)}), target=10, color="#000"),
            charts.money_in_chart(pd.DataFrame({"date": idx, "money_in": range(6),
                                                "value": range(6)}),
                                  money_color="#000", value_color="#111"),
        ]
        for chart in built:
            found = [a for a in axes(chart.to_dict()) if a["format"].startswith("$")]
            self.assertTrue(found)
            for a in found:
                self.assertEqual((a["format"], a["labelExpr"]),
                                 (charts.MONEY_AXIS, charts.MONEY_LABELS))
        # a price axis keeps its cents and no label rewrite
        price = charts.line(self._df(10), x="t", y="v", y_title="", y_format="$,.2f").to_dict()
        self.assertTrue(all("labelExpr" not in a for a in axes(price)))
        sources = ["dashboard.py", "charts.py"] + [os.path.join("views", f) for f in
                                                   os.listdir(os.path.join(REPO, "views"))
                                                   if f.endswith(".py")]
        for path in sources:
            with open(os.path.join(REPO, path), encoding="utf-8") as fh:
                self.assertNotIn("$,.2s", fh.read(), path)

    def test_clip_range(self):
        df = self._df(40)
        self.assertEqual(len(charts.clip_range(df, "t", 5)), 6)     # last point + 5 days back
        self.assertEqual(len(charts.clip_range(df, "t", 14)), 15)
        self.assertEqual(len(charts.clip_range(df, "t", 365)), 40)  # more than we have
        self.assertEqual(len(charts.clip_range(df, "t", None)), 40)

    def test_window_change(self):
        df = self._df(11)  # values 100..110
        first, last, pct = charts.window_change(df, "t", "v")
        self.assertEqual((first, last), (100.0, 110.0))
        self.assertAlmostEqual(pct, 10.0)
        self.assertEqual(charts.window_change(df.iloc[0:0], "t", "v"), (None, None, None))

    def test_window_never_returns_whole_frame_when_range_too_short(self):
        df = self._df(40)
        # consecutive daily points: "1D" legitimately gives the last two
        win, short = charts.window(df, "t", 1)
        self.assertEqual(len(win), 2)
        self.assertFalse(short)
        # isolated last point (a gap): the clip yields 1 row -> fall back to last 2,
        # NOT the whole 40-row frame
        gappy = pd.concat([df.iloc[:39],
                           df.iloc[[39]].assign(t=df["t"].iloc[38] + pd.Timedelta(days=6))])
        win, short = charts.window(gappy, "t", 1)
        self.assertEqual(len(win), 2)
        self.assertTrue(short)
        # a comfortable range and "All" behave normally
        self.assertFalse(charts.window(df, "t", 30)[1])
        self.assertEqual(len(charts.window(df, "t", None)[0]), 40)

    def test_line_builds_layered_chart(self):
        spec = charts.line(self._df(5), x="t", y="v", y_title="V", y_format="$,.2f",
                           tooltip=[]).to_dict()
        self.assertIn("layer", spec)
        self.assertGreaterEqual(len(spec["layer"]), 3)

    def test_line_has_no_permanent_point_markers(self):
        spec = charts.line(self._df(5), x="t", y="v", y_title="V", y_format="$,.2f",
                           tooltip=[]).to_dict()
        line_layer = spec["layer"][0]
        mark = line_layer["mark"]
        self.assertFalse(mark.get("point", False))

    def test_line_color_sets_fixed_colour_no_legend(self):
        spec = charts.line(self._df(5), x="t", y="v", y_title="V", y_format="$,.2f",
                           tooltip=[], line_color="#22c55e").to_dict()
        line_layer = spec["layer"][0]
        self.assertEqual(line_layer["encoding"]["color"]["value"], "#22c55e")

    def test_compress_gaps_uses_ordinal_axis_in_chronological_order(self):
        # a gap that spans a month boundary - lexical string sort of the axis
        # labels ("Feb..." < "Jan...") would get this backwards if not handled
        idx = pd.to_datetime(["2026-01-30T09:30:00Z", "2026-01-30T09:31:00Z",
                              "2026-02-02T09:30:00Z", "2026-02-02T09:31:00Z"], utc=True)
        df = pd.DataFrame({"t": idx, "v": [10.0, 11.0, 12.0, 13.0]})
        spec = charts.line(df, x="t", y="v", y_title="V", y_format="$,.2f",
                           tooltip=[], compress_gaps=True).to_dict()
        line_layer = spec["layer"][0]
        self.assertEqual(line_layer["encoding"]["x"]["field"], "_x")
        self.assertEqual(line_layer["encoding"]["x"]["type"], "ordinal")
        # explicit chronological sort order, not the field's own alphabetical order
        expected_sort = ["Jan 30 09:30", "Jan 30 09:31", "Feb 02 09:30", "Feb 02 09:31"]
        self.assertEqual(line_layer["encoding"]["x"]["sort"], expected_sort)
        # every layer sharing the ordinal x channel must carry the SAME explicit
        # sort - Vega-Lite merges per-layer sorts for a shared scale, and a layer
        # left without one drags the whole resolved domain back to alphabetical
        # (this is exactly how the axis-order bug slipped through before).
        for layer in spec["layer"]:
            x_enc = layer.get("encoding", {}).get("x")
            if x_enc is not None and x_enc.get("field") == "_x":
                self.assertEqual(x_enc.get("sort"), expected_sort)

    def test_compress_gaps_off_keeps_temporal_axis(self):
        spec = charts.line(self._df(5), x="t", y="v", y_title="V", y_format="$,.2f",
                           tooltip=[], compress_gaps=False).to_dict()
        self.assertEqual(spec["layer"][0]["encoding"]["x"]["field"], "t")
        self.assertEqual(spec["layer"][0]["encoding"]["x"]["type"], "temporal")


class AppFilesCompileTests(unittest.TestCase):
    """Every app file at least compiles - dashboard.py has no unit tests of
    its own, so a syntax slip there would otherwise only show on the live app."""

    def test_every_python_file_compiles(self):
        import py_compile
        views = [os.path.join("views", n) for n in os.listdir(os.path.join(REPO, "views"))]
        for name in sorted(os.listdir(REPO)) + sorted(views):
            if name.endswith(".py"):
                with self.subTest(name):
                    py_compile.compile(os.path.join(REPO, name), doraise=True,
                                       cfile=os.path.join(tempfile.gettempdir(), "pt_compile.pyc"))

    def test_every_view_file_is_run_once_and_exists(self):
        # dashboard.py runs each page's file with _view("name"); a renamed or
        # forgotten file would only show on the live app
        with open(os.path.join(REPO, "dashboard.py"), encoding="utf-8") as fh:
            called = re.findall(r'^_view\("(\w+)"\)$', fh.read(), re.M)
        files = sorted(n[:-3] for n in os.listdir(os.path.join(REPO, "views")) if n.endswith(".py"))
        self.assertEqual(sorted(called), files)

    def test_injected_styles_and_script_have_no_tag_like_text(self):
        # Streamlit's sanitizer drops a whole <style> or <script> whose text
        # looks like it holds an HTML tag - a comment saying "<html>" once
        # left the entire app unstyled. Only the wrapping tags may appear.
        tag = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*[\s>/]")
        with open(os.path.join(REPO, "dashboard.py"), encoding="utf-8") as fh:
            css = re.search(r'st\.html\("""<style>(.*?)</style>"""\)', fh.read(), re.S).group(1)
        with open(os.path.join(REPO, "ui_enhancements.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertEqual(tag.findall(css), [])
        self.assertEqual(tag.findall(js), [])


class PhoneAndDarkStyleTests(unittest.TestCase):
    """Rules from the phone / dark browser pass (390px wide, dark theme) that
    keep text inside its box and readable - checked in the stylesheet, since
    a unit test can't see the page."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "dashboard.py"), encoding="utf-8") as fh:
            cls.css = re.search(r'st\.html\("""<style>(.*?)</style>"""\)', fh.read(), re.S).group(1)
        cls.flat = re.sub(r"\s+", " ", cls.css)

    def _phone_blocks(self):
        return " ".join(m.group(1) for m in re.finditer(
            r"@media \(max-width: 640px\) \{(.*?\}) \}", self.flat))

    def test_stat_box_labels_wrap_on_a_phone(self):
        # three boxes in a row are ~90px wide: "Expected, next 12 months" ran
        # into the next box and off the screen
        phone = self._phone_blocks()
        self.assertRegex(phone, r"\.pt-stat-label, \.pt-stat-sub \{[^}]*white-space: normal")
        self.assertRegex(self.flat, r"\.pt-stat-label \{[^}]*overflow: hidden")

    def test_ticker_stats_two_per_line_on_a_phone(self):
        self.assertIn('.st-key-pt_stat_tiles [data-testid="stColumn"] { min-width: calc(50% - 8px)',
                      self._phone_blocks())

    def test_dark_selected_controls_use_the_link_blue(self):
        # the theme's primary blue as text on the dark page is 3.9:1
        rule = re.search(r'(:root\[data-pt-theme="dark"\] \[data-testid="stButtonGroup"\][^{]*)'
                         r'\{([^}]*)\}', self.flat)
        self.assertIsNotNone(rule)
        self.assertIn('[data-testid="stTab"][aria-selected="true"]', rule.group(1))
        self.assertIn('[data-testid="stSliderThumbValue"]', rule.group(1))
        self.assertIn("color: var(--pt-link)", rule.group(2))

    def test_captions_fade_their_text_not_their_links(self):
        rule = re.search(r'\[data-testid="stCaptionContainer"\] \{([^}]*)\}', self.flat)
        self.assertIsNotNone(rule)
        self.assertIn("opacity: 1", rule.group(1))
        self.assertIn("color-mix(in srgb, currentColor 70%, transparent)", rule.group(1))

    def test_top_bar_is_pinned_and_slims_on_a_phone(self):
        # the menu is a bar along the top (there's no sidebar): pinned, on the
        # page's own background, the page starting below it; on a phone its
        # tabs give way to the tab bar along the bottom
        self.assertRegex(self.flat, r"\.st-key-pt_topbar \{ position: fixed; top: 0;[^}]*"
                                    r"background: var\(--pt-bg")
        self.assertRegex(self.flat, r'\[data-testid="stMainBlockContainer"\] \{ padding-top: 5\.75rem')
        phone = self._phone_blocks()
        self.assertIn('.st-key-pt_topbar [class*="st-key-nav_"] { display: none !important; }',
                      phone)
        self.assertRegex(phone, r"\.st-key-pt_tabbar \{ display: flex !important; position: fixed")

    def test_tabs_wrap_on_a_phone_only(self):
        # Plan's five or six tabs scrolled sideways behind an arrow at 390px
        phone = self._phone_blocks()
        self.assertRegex(phone, r'\[data-testid="stTabs"\] \[role="tablist"\] \{[^}]*flex-wrap: wrap')
        self.assertRegex(phone, r'\[data-testid="stTabsScrollRight"\] \{ display: none')
        desktop = re.sub(r"@media \(max-width: 640px\) \{(.*?\}) \}", "", self.flat)
        self.assertNotIn('[role="tablist"]', desktop)

    def test_grey_text_is_the_muted_ink_and_passes_aa(self):
        # a metric's plain change chip (theme grayTextColor) read 3.4:1 and a
        # slider's min / max labels 4.0:1 on the light theme
        import tomllib
        with open(os.path.join(REPO, ".streamlit", "config.toml"), "rb") as fh:
            theme = tomllib.load(fh)["theme"]
        light = re.search(r":root \{[^}]*--pt-ink-muted: (#[0-9a-f]{6})", self.flat).group(1)
        dark = re.search(r':root\[data-pt-theme="dark"\] \{[^}]*--pt-ink-muted: (#[0-9a-f]{6})',
                         self.flat).group(1)
        self.assertEqual(theme["light"]["grayTextColor"], light)
        self.assertEqual(theme["dark"]["grayTextColor"], dark)

        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
            return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

        for fg, bg in ((light, theme["light"]["backgroundColor"]),
                       (light, theme["light"]["secondaryBackgroundColor"]),
                       (dark, theme["dark"]["backgroundColor"]),
                       (dark, theme["dark"]["secondaryBackgroundColor"])):
            a, b = sorted((lum(fg), lum(bg)))
            self.assertGreaterEqual((b + 0.05) / (a + 0.05), 4.5, (fg, bg))
        rule = re.search(r'\[data-testid="stSliderTickBar"\] \{([^}]*)\}', self.flat)
        self.assertIsNotNone(rule)
        self.assertIn("color: var(--pt-ink-muted)", rule.group(1))


class AnyBrokerCsvTests(TempDBMixin, unittest.TestCase):
    """Roadmap 9b: positions CSVs from any brokerage (csv_import.py)."""
    DIR = os.path.join(os.path.dirname(__file__), "fixtures", "brokers")
    TODAY = date(2026, 9, 30)

    def _read(self, name, mapping=None):
        import csv_import as ci
        with open(os.path.join(self.DIR, name), "rb") as fh:
            rows = ci.read_rows(fh.read())
        hi, problem = ci.find_header(rows)
        if problem:
            return rows, problem, None
        m = mapping or ci.auto_mapping(rows[hi])
        return rows, None, ci.parse(rows, m, filename=name, today=self.TODAY)

    @staticmethod
    def _held(r):
        return [(h["Account"], h["Symbol"], h["Shares"], h["Total cost"]) for h in r["holdings"]]

    def test_fidelity(self):
        _, _, r = self._read("fidelity_positions.csv")
        self.assertEqual(self._held(r), [("Individual Z12345678", "VTI", 10.0, 2500.0),
                                         ("Individual Z12345678", "BND", 25.5, 1900.0),
                                         ("ROTH IRA Z87654321", "FXAIX", 20.123, 4000.0)])
        self.assertEqual(r["cash"], {"Individual Z12345678": 1234.56})   # SPAXX** money market
        self.assertEqual(r["snapshot_date"], "2026-09-28")               # "Date downloaded" footer
        self.assertEqual(accounts.mask_number("Individual Z12345678"), "Individual ...678")

    def test_vanguard_stops_at_its_transactions_section(self):
        _, _, r = self._read("vanguard_download.csv")
        self.assertEqual([(s, q) for _, s, q, _ in self._held(r)],
                         [("VTSAX", 15.321), ("VTIAX", 30.5), ("VMFXX", 410.22)])
        self.assertEqual(r["snapshot_date"], "2026-09-30")   # not the trade date below

    def test_etrade_per_share_cost_and_cash(self):
        _, _, r = self._read("etrade_portfolio.csv")
        self.assertEqual([(s, q, c) for _, s, q, c in self._held(r)],
                         [("AAPL", 12.0, 1899.96), ("SCHD", 120.0, 3120.0), ("VXUS", 33.1, 1986.0)])
        self.assertEqual(r["cash"], {"": 812.4})
        self.assertEqual(r["snapshot_date"], "2026-09-28")

    def test_schwab_is_just_another_layout_with_its_extras_kept(self):
        meta, rows, totals = portfolio.parse_csv(FIXTURE)
        by = {r["symbol"]: r for r in rows}
        self.assertEqual((meta["snapshot_date"], meta["as_of_text"][:13]),
                         ("2026-01-15", "Positions for"))
        self.assertEqual((by["AAA"]["account"], by["CCC"]["account"]),
                         ("Individual ...111", "Individual ...222"))   # per-account sections
        self.assertEqual((by["BBB"]["div_yield_pct"], by["BBB"]["reinvest"],
                          by["BBB"]["asset_type"]), (1.5, 1, "ETFs & Closed End Funds"))
        self.assertEqual((by["CCC"]["next_earnings_date"], by["AAA"]["pct_of_account"],
                          by["AAA"]["day_change_pct"]), ("03/01/2026", 64.86, 1.2))
        self.assertEqual(totals["Individual ...111"]["cash_value"], 100.0)
        self.assertEqual(totals["Individual ...222"]["reported_market_value"], 1650.0)

    def test_transaction_exports_are_recognized(self):
        _, problem, _ = self._read("robinhood_activity.csv")
        self.assertEqual(problem, "transactions")

    def test_an_unknown_layout_with_decimal_commas(self):
        import csv_import as ci
        rows, problem, _ = self._read("odd_layout.csv")
        self.assertEqual(problem, "no header")
        hi = ci.guess_header(rows)
        self.assertEqual(rows[hi][0], "Ticker Code")
        shapes = ci.sample_shapes(rows, hi)
        self.assertEqual(shapes[0], ["SHORT_CODE", "NUMBER", "NUMBER", "NUMBER"])  # never values
        r = ci.parse(rows, {"symbol": 0, "quantity": 1, "cost": 2, "value": 3}, today=self.TODAY)
        self.assertEqual(self._held(r), [("", "VTI", 4.0, 1000.0), ("", "BND", 10.0, 700.0)])

    def test_ai_mapping_is_validated_and_layouts_are_remembered(self):
        import csv_import as ci
        header = ["Ticker Code", "Units Held", "Book Cost", "Cur. Val"]
        client = ScreenshotReadTests._Client(
            '{"symbol": 0, "quantity": 1, "cost": 2, "value": 3, "percent": 9, "bogus": 1}')
        m = ci.ai_mapping(header, [["SHORT_CODE", "NUMBER", "NUMBER", "NUMBER"]], "key",
                          client=client, model="m")
        self.assertEqual(m, {"symbol": 0, "quantity": 1, "cost": 2, "value": 3})  # 9 is out of range
        prompt = client.sent["messages"][0]["content"]
        self.assertIn("SHORT_CODE", prompt)
        conn = portfolio.connect(self.db)
        self.assertIsNone(ci.remembered(conn, header))
        ci.remember(conn, header, m)
        self.assertEqual(ci.remembered(conn, [" ticker code ", "Units Held", "Book Cost", "Cur. Val"]), m)
        stored = conn.execute("SELECT * FROM csv_layouts").fetchone()
        self.assertNotIn("VTI", repr(dict(stored)))                  # names only, no data
        conn.close()

    def test_numbers(self):
        import csv_import as ci
        for raw, want in (("$1,234.56", 1234.56), ("(12.30)", -12.3), ("1000,00", 1000.0),
                          ("1,234", 1234.0), ("12,5%", 12.5), ("--", None), ("Cur. Val", None)):
            self.assertEqual(ci._num(raw), want, raw)


class IncomeByMonthTests(TempDBMixin, unittest.TestCase):
    TODAY = date(2026, 9, 30)

    def test_quarterly_payer_repeats_its_months_at_todays_shares(self):
        import income
        conn = portfolio.connect(self.db)
        bars = [("SCHD", d, 28.0, 1000, div) for d, div in (
            ("2025-10-08", 0.25), ("2025-12-10", 0.26), ("2026-03-25", 0.26), ("2026-06-24", 0.27),
            ("2026-06-25", 0.0), ("2024-12-11", 0.20))]                  # too old to count
        bars += [("BRK.B", "2026-09-01", 400.0, 10, 0.0)]                # synced, never pays
        conn.executemany("INSERT INTO daily_bars (ticker, date, close, volume, dividend) "
                         "VALUES (?,?,?,?,?)", bars)
        conn.commit()
        tickers = ["SCHD", "BRK.B", "NEWCO", "VTI"]
        synced = income.has_history(conn, tickers)
        self.assertEqual(synced, {"SCHD", "BRK.B"})
        paid = income.payments(conn, tickers, self.TODAY)
        plan = income.schedule(
            [{"symbol": "SCHD", "quantity": 100, "annual": 300.0},       # dates win over the yield
             {"symbol": "BRK.B", "quantity": 2, "annual": None},
             {"symbol": "NEWCO", "quantity": 10, "annual": 120.0}],      # yield only: spread
            paid, synced, self.TODAY)
        months = {r["month"]: r for r in plan["months"]}
        self.assertEqual(list(months)[0], "2026-09")
        self.assertEqual(len(months), 12)
        self.assertAlmostEqual(months["2026-12"]["by_symbol"]["SCHD"], 26.0)
        self.assertAlmostEqual(months["2027-03"]["by_symbol"]["SCHD"], 26.0)
        self.assertAlmostEqual(months["2026-10"]["by_symbol"]["SCHD"], 25.0)
        self.assertNotIn("SCHD", months["2026-11"]["by_symbol"])
        self.assertAlmostEqual(months["2026-10"]["by_symbol"]["NEWCO"], 10.0)
        self.assertEqual((plan["spread"], plan["none"]), (["NEWCO"], ["BRK.B"]))
        self.assertAlmostEqual(plan["total"], 25 + 26 + 26 + 27 + 120)
        conn.close()

    def test_the_sync_keeps_each_days_dividend(self):
        import pandas as pd
        df = pd.DataFrame({"Open": [1.0, 1.0], "High": [1.0, 1.0], "Low": [1.0, 1.0],
                           "Close": [1.0, 1.0], "Adj Close": [1.0, 1.0], "Volume": [5, 5],
                           "Dividends": [0.0, 0.31]},
                          index=pd.to_datetime(["2026-09-01", "2026-09-02"]))
        fake = type(sys)("yf")
        fake.Ticker = lambda t: types.SimpleNamespace(history=lambda **kw: df)
        with unittest.mock.patch.object(sync_history, "yf", fake):
            rows, err = sync_history.fetch_bars("SCHD")
        self.assertEqual(([r["dividend"] for r in rows], err), ([0.0, 0.31], ""))
        conn = portfolio.connect(self.db)
        sync_history.upsert_bars(conn, "SCHD", rows)
        conn.commit()
        self.assertEqual(conn.execute("SELECT dividend FROM daily_bars WHERE date = '2026-09-02'"
                                      ).fetchone()["dividend"], 0.31)
        conn.close()


class LivePricesTests(TempDBMixin, unittest.TestCase):
    """Prices keep themselves current (no Refresh button): live_prices.freshen."""
    OPEN = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)      # Tue 11:00 ET
    NIGHT = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)      # Tue 11 PM ET
    SATURDAY = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)

    def _hold(self, conn, user_id, rows):
        import manual_entry as me
        clean, cash, _ = me.validate(rows, [])
        found = {r["Symbol"]: {"price": 10.0, "name": r["Symbol"]} for r in rows}
        meta, prows, totals, _ = me.build(clean, cash, found)
        portfolio.write_snapshot(conn, user_id, meta, prows, totals, me.SOURCE)

    def _fakes(self):
        calls = {"finnhub": [], "yahoo": []}

        def finnhub(sym):
            calls["finnhub"].append(sym)
            return ({"c": 101.0, "pc": 100.0}, "") if sym != "NOFH" else ({"c": 0}, "")

        def yahoo(sym):
            calls["yahoo"].append(sym)
            return ({"c": 55.0, "pc": 50.0}, "") if sym != "DEAD" else ({}, "yahoo: no price")
        return calls, finnhub, yahoo

    def test_market_hours(self):
        import live_prices as lp
        self.assertTrue(lp.market_open(self.OPEN))
        self.assertFalse(lp.market_open(self.NIGHT))
        self.assertFalse(lp.market_open(self.SATURDAY))
        self.assertEqual(lp.last_close(self.SATURDAY).astimezone(lp.NY).strftime("%a %H:%M"),
                         "Fri 16:00")
        self.assertEqual(lp.kind("BTC-USD", "Crypto"), "crypto")
        self.assertEqual(lp.kind("VTSAX", "Mutual Funds"), "fund")
        self.assertEqual(lp.kind("VTI", "ETFs & Closed End Funds"), "stock")

    def test_due_rules(self):
        import live_prices as lp
        m = timedelta(minutes=1)
        self.assertTrue(lp.due("stock", None, self.OPEN))
        self.assertFalse(lp.due("stock", self.OPEN - 30 * timedelta(seconds=1), self.OPEN))
        self.assertTrue(lp.due("stock", self.OPEN - m, self.OPEN))
        self.assertFalse(lp.due("stock", self.NIGHT - m, self.NIGHT))     # after the close fetch
        self.assertTrue(lp.due("stock", self.OPEN, self.NIGHT))            # not yet since the close
        self.assertTrue(lp.due("crypto", self.NIGHT - 6 * m, self.NIGHT))  # crypto never closes
        self.assertFalse(lp.due("fund", self.OPEN - 30 * m, self.OPEN))    # NAV: hourly is plenty

    def test_prices_are_shared_and_routed_to_the_right_source(self):
        import live_prices as lp
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "other", "pw")
        rows = [{"Symbol": "VTI", "Shares": 2, "Total cost": 20}, {"Symbol": "BTC-USD", "Shares": 1,
                "Total cost": 10, "Type": "Crypto"}, {"Symbol": "VTSAX", "Shares": 1, "Total cost": 10,
                "Type": "Mutual fund"}, {"Symbol": "NOFH", "Shares": 1, "Total cost": 10}]
        self._hold(conn, self.user_id, rows)
        self._hold(conn, other, rows[:1])
        calls, fh, yh = self._fakes()
        r = lp.freshen(conn, self.user_id, "key", now=self.OPEN, finnhub=fh, yahoo=yh)
        self.assertEqual(r["fetched"], 4)
        self.assertEqual(sorted(calls["finnhub"]), ["NOFH", "VTI"])            # stocks: Finnhub
        self.assertEqual(sorted(calls["yahoo"]), ["BTC-USD", "NOFH", "VTSAX"])  # + the fallback
        live = {p["symbol"]: p["live_price"] for p in conn.execute(
            "SELECT symbol, live_price FROM positions WHERE user_id = ?", (self.user_id,))}
        self.assertEqual(live, {"VTI": 101.0, "BTC-USD": 55.0, "VTSAX": 55.0, "NOFH": 55.0})
        self.assertTrue(r["live"])
        # another viewer a few seconds later: nothing is fetched again, prices still apply
        calls2, fh2, yh2 = self._fakes()
        r2 = lp.freshen(conn, other, "key", now=self.OPEN + timedelta(seconds=10), finnhub=fh2, yahoo=yh2)
        self.assertEqual((r2["fetched"], calls2), (0, {"finnhub": [], "yahoo": []}))
        conn.close()

    def test_known_holdings_skip_the_reads(self):
        # the dashboard passes what its page run loaded (known=...): the same
        # fetches and prices as reading them here, without the three reads
        import shutil
        import live_prices as lp
        conn = portfolio.connect(self.db)
        self._hold(conn, self.user_id, [
            {"Symbol": "VTI", "Shares": 2, "Total cost": 20},
            {"Symbol": "VTSAX", "Shares": 1, "Total cost": 10, "Type": "Mutual fund"}])
        watchlist.add(conn, self.user_id, "NVDA")
        conn.close()
        copy = self.db + ".copy"
        shutil.copyfile(self.db, copy)

        def run(db, known_from_db):
            c = portfolio.connect(db)
            try:
                known = None
                if known_from_db:
                    snap = lp.latest_snapshot(c, self.user_id)
                    held = {r["symbol"]: r["asset_type"] for r in c.execute(
                        "SELECT symbol, asset_type FROM positions WHERE snapshot_date = ? "
                        "AND user_id = ?", (snap, self.user_id))}
                    known = (snap, held, watchlist.list_tickers(c, self.user_id))
                sql = []
                c.set_trace_callback(sql.append)
                calls, fh, yh = self._fakes()
                r = lp.freshen(c, self.user_id, "key", now=self.OPEN, finnhub=fh, yahoo=yh,
                               known=known)
                c.set_trace_callback(None)
                live = {p["symbol"]: p["live_price"] for p in c.execute(
                    "SELECT symbol, live_price FROM positions WHERE user_id = ?", (self.user_id,))}
                return r, calls, live, sql
            finally:
                c.close()
        r1, calls1, live1, sql1 = run(self.db, False)
        r2, calls2, live2, sql2 = run(copy, True)
        self.assertEqual((r1, calls1, live1), (r2, calls2, live2))
        self.assertEqual(r1["fetched"], 3)   # VTI, VTSAX and the watched NVDA
        read_here = ("MAX(snapshot_date)", "DISTINCT symbol, asset_type", "FROM watchlist")
        self.assertTrue(all(any(s in q for q in sql1) for s in read_here))
        self.assertFalse(any(s in q for q in sql2 for s in read_here))
        self.assertEqual(len(sql1) - len(sql2), 3)

    def test_trim_keeps_a_week_then_one_close_per_day(self):
        import live_prices as lp
        conn = portfolio.connect(self.db)
        rows = []
        for day, hhmm, price, ok in (("2026-09-10", "14:00", 1.0, 1), ("2026-09-10", "19:59", 2.0, 1),
                                     ("2026-09-10", "20:30", None, 0),      # a failed late try
                                     ("2026-09-11", "15:00", 3.0, 1),
                                     ("2026-09-28", "14:00", 4.0, 1), ("2026-09-28", "15:00", 5.0, 1)):
            rows.append(("VTI", price, f"{day}T{hhmm}:00Z", ok))
        rows.append(("BND", 70.0, "2026-09-10T19:00:00Z", 1))
        conn.executemany("INSERT INTO price_history (ticker, price, fetched_at, ok) VALUES (?,?,?,?)", rows)
        conn.commit()
        n = lp.trim_history(conn, now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        left = [(r["ticker"], r["fetched_at"][:10], r["price"]) for r in conn.execute(
            "SELECT ticker, fetched_at, price FROM price_history ORDER BY ticker, fetched_at")]
        self.assertEqual(n, 2)       # VTI's 14:00 quote and the failed try on Sep 10
        self.assertEqual(left, [("BND", "2026-09-10", 70.0),
                                ("VTI", "2026-09-10", 2.0),        # that day's close
                                ("VTI", "2026-09-11", 3.0),
                                ("VTI", "2026-09-28", 4.0), ("VTI", "2026-09-28", 5.0)])  # last week: all
        self.assertEqual(lp.trim_history(conn, now=datetime(2026, 9, 30, tzinfo=timezone.utc)), 0)
        conn.close()

    def test_watchlist_tickers_get_live_quotes_too(self):
        import live_prices as lp
        conn = portfolio.connect(self.db)
        watchlist.add(conn, self.user_id, "NVDA")                    # no holdings at all
        calls, fh, yh = self._fakes()
        r = lp.freshen(conn, self.user_id, "key", now=self.OPEN, finnhub=fh, yahoo=yh)
        self.assertEqual((r["fetched"], r["watch_fetched"], r["updated"]), (1, 1, 0))
        self.assertEqual(calls["finnhub"], ["NVDA"])
        q = lp.quotes(conn, ["NVDA", "NONE"])
        self.assertEqual(set(q), {"NVDA"})
        self.assertEqual((q["NVDA"]["price"], q["NVDA"]["prev_close"]), (101.0, 100.0))
        conn.close()

    def test_a_holding_without_cost_still_gets_its_live_price(self):
        import live_prices as lp
        conn = portfolio.connect(self.db)
        self._hold(conn, self.user_id, [{"Symbol": "VTI", "Shares": 3},              # no cost
                                        {"Symbol": "BND", "Shares": 2, "Total cost": 100}])
        _, fh, yh = self._fakes()
        lp.freshen(conn, self.user_id, "key", now=self.OPEN, finnhub=fh, yahoo=yh)
        got = {r["symbol"]: (r["live_price"], r["live_market_value"], r["live_unrealized_gain"])
               for r in conn.execute("SELECT symbol, live_price, live_market_value, "
                                     "live_unrealized_gain FROM positions WHERE user_id = ?",
                                     (self.user_id,))}
        self.assertEqual(got, {"VTI": (101.0, 303.0, None), "BND": (101.0, 202.0, 102.0)})
        conn.close()

    def test_a_symbol_with_no_price_is_not_asked_every_minute(self):
        import live_prices as lp
        conn = portfolio.connect(self.db)
        self._hold(conn, self.user_id, [{"Symbol": "DEAD", "Shares": 1, "Total cost": 1,
                                         "Type": "Mutual fund"}])
        calls, fh, yh = self._fakes()
        lp.freshen(conn, self.user_id, "key", now=self.OPEN, finnhub=fh, yahoo=yh)
        lp.freshen(conn, self.user_id, "key", now=self.OPEN + timedelta(minutes=2), finnhub=fh, yahoo=yh)
        self.assertEqual(calls["yahoo"], ["DEAD"])                   # tried once, not again yet
        r = lp.freshen(conn, self.user_id, "key", now=self.NIGHT, finnhub=fh, yahoo=yh)
        self.assertFalse(r["live"])                                   # market closed, no crypto
        conn.close()


class ScreenshotReadTests(unittest.TestCase):
    """Roadmap 9d: holdings from screenshots, read by the AI (a fake client here)."""

    class _Client:
        def __init__(self, reply=None, exc=None):
            self.reply, self.exc, self.sent = reply, exc, None
            outer = self

            class _Messages:
                def create(self, **kw):
                    outer.sent = kw
                    if outer.exc:
                        raise outer.exc
                    return types.SimpleNamespace(content=[types.SimpleNamespace(
                        type="text", text=outer.reply)])
            self.messages = _Messages()

    def test_images_are_checked_before_anything_is_sent(self):
        import screenshot_read as sr
        ok, errors = sr.check_images([("a.PNG", b"x"), ("b.jpg", b"y")])
        self.assertEqual(([mt for _, mt in ok], errors), (["image/png", "image/jpeg"], []))
        _, errors = sr.check_images([("doc.pdf", b"x"), ("big.png", b"x" * (sr.MAX_BYTES + 1))])
        self.assertEqual(len(errors), 2)
        _, errors = sr.check_images([(f"{i}.png", b"x") for i in range(sr.MAX_IMAGES + 1)])
        self.assertIn("Up to", errors[0])

    def test_answer_is_rechecked_and_overlaps_counted_once(self):
        import screenshot_read as sr
        reply = json.dumps({"holdings": [
            {"symbol": "vti", "shares": "10", "cost_basis": 2500, "percent": None},
            {"symbol": "VTI", "shares": 10, "cost_basis": 2500},          # overlapping shot
            {"symbol": "Vanguard Total Bond", "shares": 5},              # a name, not a ticker
            {"symbol": "Z12345678", "shares": 1},                         # an account number
            {"symbol": "BND", "shares": "$1,861.50"},                     # still a number...
            {"symbol": "AAPL", "shares": 0}, {"symbol": "MSFT", "shares": True}],
            "cash": "$120.50", "balance": 99999})
        client = self._Client("Here you go:\n" + reply)
        out = sr.read([(b"png-bytes", "image/png")], "key", client=client, model="m")
        self.assertIsNone(out["error"])
        self.assertEqual([(h["Symbol"], h["Shares"], h["Total cost"]) for h in out["holdings"]],
                         [("VTI", 10.0, 2500.0), ("BND", 1861.5, None)])
        self.assertEqual(out["cash"], 120.5)
        self.assertNotIn("99999", repr(out))
        sent = client.sent["messages"][0]["content"]
        self.assertEqual((sent[0]["type"], sent[0]["source"]["media_type"]), ("image", "image/png"))
        self.assertIn("Do not include names, account numbers", sent[-1]["text"])

    def test_average_cost_and_crypto_like_a_robinhood_screen(self):
        import screenshot_read as sr
        out = sr.clean({"holdings": [
            {"symbol": "USO", "shares": 11, "average_cost": 137.16, "crypto": False},
            {"symbol": "BTC", "shares": 0.01523505, "average_cost": "105,026.24", "crypto": True}]})
        self.assertEqual([(h["Symbol"], h["Total cost"], h["Type"]) for h in out["holdings"]],
                         [("USO", 1508.76, None), ("BTC-USD", 1600.08, "Crypto")])
        import asset_classes
        self.assertEqual(asset_classes.from_yahoo({"quote_type": "CRYPTOCURRENCY"}), {"Other": 1.0})

    def test_percentages_and_failures(self):
        import anthropic
        import screenshot_read as sr
        pct = sr.clean({"holdings": [{"symbol": "VTI", "percent": "60%"},
                                     {"symbol": "BND", "percent": 40},
                                     {"symbol": "XX", "percent": 250}]})
        self.assertEqual((pct["mode"], [h["Percent"] for h in pct["holdings"]]),
                         ("Percentages", [60.0, 40.0]))
        out = sr.read([(b"x", "image/png")], "key", client=self._Client("I can't read that"), model="m")
        self.assertIn("couldn't be understood", out["error"])
        err = anthropic.APIConnectionError(request=None)  # a network failure
        out = sr.read([(b"x", "image/png")], "key", client=self._Client(exc=err), model="m")
        self.assertEqual((out["holdings"], bool(out["error"])), ([], True))


class PasteParseTests(unittest.TestCase):
    """Roadmap 9c: holdings from text pasted off a brokerage's website."""

    def _rows(self, text):
        import paste_parse
        r = paste_parse.parse(text)
        return r, [(h["Symbol"], h["Shares"], h["Total cost"], h["Percent"]) for h in r["holdings"]]

    def test_table_with_a_header_keeps_only_symbols_shares_and_cost(self):
        r, rows = self._rows(
            "Individual - TOD Z12345678\n"
            "Symbol\tDescription\tQuantity\tPrice\tMarket Value\tCost Basis\t% of Account\n"
            "VTI\tVANGUARD TOTAL STOCK MARKET ETF\t10\t$300.12\t$3,001.20\t$2,500.00\t60.5%\n"
            "BND\tVANGUARD TOTAL BOND\t25.5\t$73.00\t$1,861.50\t$1,900.00\t37.5%\n"
            "Cash & Cash Investments\t--\t--\t--\t$100.00\t--\t2%\n"
            "Account Total\t\t\t\t$4,962.70")
        self.assertEqual(rows, [("VTI", 10.0, 2500.0, 60.5), ("BND", 25.5, 1900.0, 37.5)])
        self.assertEqual((r["mode"], r["cash"]), ("Shares", 100.0))
        self.assertNotIn("12345678", repr(r))              # the account number is dropped

    def test_one_cell_per_line_like_a_web_app(self):
        _, rows = self._rows("Stocks\nName\nSymbol\nShares\nPrice\nApple\nAAPL\n10\n$230.10\n"
                             "+$801.00\nVanguard S&P 500 ETF\nVOO\n2.5\n$550.00\n$1,375.00")
        self.assertEqual([(s, q) for s, q, _, _ in rows], [("AAPL", 10.0), ("VOO", 2.5)])

    def test_typed_lists_percentages_and_spaced_columns(self):
        _, rows = self._rows("vti 10\nBND 25\nAAPL 3 shares\nbrk.b 2")
        self.assertEqual([(s, q) for s, q, _, _ in rows],
                         [("VTI", 10.0), ("BND", 25.0), ("AAPL", 3.0), ("BRK.B", 2.0)])
        r, rows = self._rows("VTI 60%\nVXUS 25%\nBND 15%")
        self.assertEqual((r["mode"], [p for *_, p in rows]), ("Percentages", [60.0, 25.0, 15.0]))
        r, rows = self._rows("SYMBOL   QTY    PRICE     VALUE\nSCHD     120    $28.00    $3,360.00\n"
                             "CASH                      $512.33")
        self.assertEqual((rows, r["cash"]), ([("SCHD", 120.0, None, None)], 512.33))

    def test_average_cost_column_becomes_total_cost(self):
        _, rows = self._rows("Name\tSymbol\tShares\tPrice\tAverage cost\tTotal return\tEquity\n"
                             "United States Oil Fund\tUSO\t11\t$144.12\t$137.16\t$76.56\t$1,585.32\n"
                             "Bitcoin\tBTC-USD\t0.01523505\t$83,232.59\t$105,026.24\t$332.03\t$1,268.05")
        self.assertEqual(rows, [("USO", 11.0, 1508.76, None), ("BTC-USD", 0.01523505, 1600.08, None)])

    def test_same_symbol_in_two_accounts_is_combined_and_junk_finds_nothing(self):
        _, rows = self._rows("VTI 10\nBND 5\nVTI 2.5")
        self.assertEqual(rows[0], ("VTI", 12.5, None, None))
        r, rows = self._rows("Welcome back! Your balance is $12,000 as of 9/29/2026\nTOTAL 12000")
        self.assertEqual(rows, [])


class PrivacyTests(TempDBMixin, unittest.TestCase):
    """Roadmap 9a / 9c: keep less than people share, and let them share nothing."""

    def test_delete_all_my_holdings_leaves_other_accounts_and_goals(self):
        import manual_entry as me
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "other", "pw")
        clean, cash, _ = me.validate([{"Symbol": "VTI", "Shares": 1}], [{"Cash": 5}])
        for uid in (self.user_id, other):
            meta, rows, totals, _ = me.build(clean, cash, {"VTI": {"price": 100.0}})
            portfolio.write_snapshot(conn, uid, meta, rows, totals, me.SOURCE)
        plans.save_plan(conn, self.user_id, {"goal_type": "Retirement"}, self.user_id)
        portfolio.delete_holdings(conn, self.user_id)
        for table in portfolio.HOLDINGS_TABLES:
            n = conn.execute(f"SELECT COUNT(*) n FROM {table} WHERE user_id = ?",
                             (self.user_id,)).fetchone()["n"]
            self.assertEqual(n, 0, table)
        self.assertIsNotNone(update_prices.latest_snapshot(conn, other))   # untouched
        self.assertEqual(plans.get_plan(conn, self.user_id)["goal_type"], "Retirement")
        conn.close()

    def test_uploads_leave_no_file_behind(self):
        with portfolio.temp_upload("../../evil/name.csv", b"a,b\n") as path:
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(os.path.basename(path), "name.csv")  # no path tricks
            folder = os.path.dirname(path)
        self.assertFalse(os.path.exists(folder))
        with self.assertRaises(RuntimeError):
            with portfolio.temp_upload("x.csv", b"1") as path:
                raise RuntimeError("import failed")
        self.assertFalse(os.path.exists(path))                     # gone on failure too
        self.assertEqual(portfolio.upload_label("C:/Users/me/Downloads/pos.csv"), "upload: pos.csv")

    def test_account_numbers_are_cut_to_three_digits(self):
        cases = {"Individual ...641": "Individual ...641", "Individual Z12345678": "Individual ...678",
                 "Roth IRA 1234-5678": "Roth IRA ...678", "Brokerage 2024": "Brokerage 2024",
                 "Joint 987 654 3210": "Joint ...210", "My account": "My account", None: None}
        for raw, want in cases.items():
            self.assertEqual(accounts.mask_number(raw), want, raw)

    def test_saved_snapshots_never_hold_a_full_account_number(self):
        import manual_entry as me
        clean, cash, _ = me.validate([{"Account": "Fidelity Z12345678", "Symbol": "VTI",
                                       "Shares": 1}], [{"Account": "Fidelity Z12345678",
                                                        "Cash": 5}])
        meta, rows, totals, _ = me.build(clean, cash, {"VTI": {"price": 100.0}})
        conn = portfolio.connect(self.db)
        portfolio.write_snapshot(conn, self.user_id, meta, rows, totals, me.SOURCE)
        stored = {r["account"] for r in conn.execute("SELECT account FROM positions")} | \
                 {r["account"] for r in conn.execute("SELECT account FROM account_totals")}
        self.assertEqual(stored, {"Fidelity ...678"})
        conn.close()

    def test_import_records_the_upload_name_not_a_path(self):
        conn = portfolio.connect(self.db)
        with open(FIXTURE, "rb") as fh:
            data = fh.read()
        with portfolio.temp_upload("positions.csv", data) as path:
            info = portfolio.import_csv(conn, path, self.user_id,
                                        source_name=portfolio.upload_label("positions.csv"))
        self.assertEqual(info["source_file"], "upload: positions.csv")
        self.assertEqual({r["source_file"] for r in conn.execute("SELECT source_file FROM snapshots")},
                         {"upload: positions.csv"})
        conn.close()

    def test_example_portfolio_loads_and_real_holdings_replace_it(self):
        import manual_entry as me
        import sample_data
        conn = portfolio.connect(self.db)
        n = sample_data.load(conn, self.user_id, today=date(2026, 9, 28))
        snap = update_prices.latest_snapshot(conn, self.user_id)
        self.assertEqual(portfolio.snapshot_source(conn, self.user_id, snap), portfolio.SAMPLE_SOURCE)
        self.assertEqual(conn.execute("SELECT COUNT(*) n FROM positions").fetchone()["n"], n)
        clean, cash, _ = me.validate([{"Symbol": "VTI", "Shares": 2}], [])
        meta, rows, totals, _ = me.build(clean, cash, {"VTI": {"price": 100.0}},
                                         today=date(2026, 9, 29))
        portfolio.write_snapshot(conn, self.user_id, meta, rows, totals, me.SOURCE)
        self.assertEqual([r["symbol"] for r in conn.execute("SELECT symbol FROM positions")], ["VTI"])
        self.assertEqual({r["source_file"] for r in conn.execute("SELECT source_file FROM snapshots")},
                         {me.SOURCE})                               # the example is gone
        sample_data.load(conn, self.user_id, today=date(2026, 9, 30))
        perf.log_open(self.db, self.user_id, {"portfolio_value": 40000.0}, min_gap_sec=0)
        sample_data.clear(conn, self.user_id)
        self.assertIsNone(conn.execute("SELECT 1 FROM value_log WHERE portfolio_value = 40000"
                                       ).fetchone())  # the example's visits go too
        self.assertEqual(update_prices.latest_snapshot(conn, self.user_id), "2026-09-29")
        conn.close()

    def test_percentages_portfolio(self):
        import manual_entry as me
        rows = [{"Symbol": "VTI", "Percent": 60, "Type": "ETF"},
                {"Symbol": "BND", "Percent": 30, "Type": "Bond"}]
        clean, cash, errors = me.validate_weights(rows, 10, 10_000)
        self.assertEqual(errors, [])
        found = {"VTI": {"price": 300.0, "name": "VTI"}, "BND": {"price": 75.0, "name": "BND"}}
        meta, out, totals, errors = me.build_weights(clean, cash, 10_000, found,
                                                     today=date(2026, 9, 29))
        self.assertEqual(errors, [])
        self.assertEqual([(r["symbol"], r["market_value"], r["quantity"]) for r in out],
                         [("VTI", 6000.0, 20.0), ("BND", 3000.0, 40.0)])
        self.assertEqual([r["reported_gain"] for r in out], [0.0, 0.0])  # tracked from today
        self.assertEqual(totals[me.DEFAULT_ACCOUNT]["cash_value"], 1000.0)
        self.assertIn("add up to 95", me.validate_weights(rows, 5, 10_000)[2][0])
        self.assertIn("pretend total", " ".join(me.validate_weights(rows, 10, 0)[2]))
        back, cash_pct = me.prefill_weights(
            [{"account": "A", "symbol": "VTI", "market_value": 6000.0, "asset_type": "Equity"},
             {"account": "A", "symbol": "BND", "market_value": 3000.0, "asset_type": "Fixed Income"}],
            {"A": 1000.0})
        self.assertEqual([(r["Symbol"], r["Percent"]) for r in back], [("VTI", 60.0), ("BND", 30.0)])
        self.assertEqual(cash_pct, 10.0)


class ManualEntryTests(TempDBMixin, unittest.TestCase):
    ROWS = [{"Account": "Roth", "Symbol": " vti ", "Shares": 10, "Total cost": 2000.0,
             "Type": "ETF"},
            {"Account": "", "Symbol": "VTSAX", "Shares": "5.5", "Total cost": None,
             "Type": "Mutual fund"},
            {"Account": "Roth", "Symbol": "", "Shares": None, "Total cost": None, "Type": "ETF"}]

    def test_validate_cleans_rows_and_reports_problems(self):
        import manual_entry as me
        clean, cash, errors = me.validate(self.ROWS, [{"Account": "Roth", "Cash": "1,000"}])
        self.assertEqual(errors, [])
        self.assertEqual([(h["account"], h["symbol"], h["quantity"], h["asset_type"]) for h in clean],
                         [("Roth", "VTI", 10.0, "ETFs & Closed End Funds"),
                          (me.DEFAULT_ACCOUNT, "VTSAX", 5.5, "Mutual Funds")])  # blank row dropped
        self.assertEqual(cash, {"Roth": 1000.0})
        bad = [{"Symbol": "VTI", "Shares": 0}, {"Symbol": "", "Shares": 3},
               {"Symbol": "B@D", "Shares": 1}, {"Symbol": "X", "Shares": 1, "Total cost": "abc"},
               {"Symbol": "Y", "Shares": 1}, {"Symbol": "Y", "Shares": 2}]
        _, _, errors = me.validate(bad, [{"Account": "A", "Cash": -5}])
        text = " ".join(errors)
        for bit in ("more than 0", "add a symbol", "doesn't look like", "dollar amount",
                    "listed twice", "Cash for A"):
            self.assertIn(bit, text)
        self.assertEqual(me.validate([], [])[2], ["Add at least one holding or some cash."])
        # cash on the default name follows a single renamed account
        one = [{"Account": "Roth", "Symbol": "VTI", "Shares": 1}]
        self.assertEqual(me.validate(one, [{"Account": me.DEFAULT_ACCOUNT, "Cash": 50}])[1],
                         {"Roth": 50.0})
        two = one + [{"Account": "Taxable", "Symbol": "BND", "Shares": 1}]
        self.assertEqual(me.validate(two, [{"Account": "", "Cash": 50}])[1],
                         {me.DEFAULT_ACCOUNT: 50.0})  # ambiguous: left alone

    def test_lookup_prefers_finnhub_then_yahoo_and_keeps_known_names(self):
        import manual_entry as me
        asked = []
        found = me.lookup(["VTI", "VTSAX", "NOPE"],
                          finnhub_quote=lambda s: {"VTI": 300.0}.get(s),
                          yahoo_info=lambda s: asked.append(s) or {"VTSAX": (150.0, "Vanguard TSM")
                                                                   }.get(s, (None, None)),
                          known_names={"VTI": "Vanguard Total Stock"})
        self.assertEqual(found["VTI"], {"price": 300.0, "name": "Vanguard Total Stock"})
        self.assertEqual(found["VTSAX"], {"price": 150.0, "name": "Vanguard TSM"})
        self.assertIsNone(found["NOPE"]["price"])
        self.assertNotIn("VTI", asked)  # had a price and a name: no Yahoo call

    def test_saved_like_an_import_and_replaces_the_same_day(self):
        import manual_entry as me
        clean, cash, _ = me.validate(self.ROWS, [{"Account": "Roth", "Cash": 500}])
        found = {"VTI": {"price": 300.0, "name": "Total Stock"},
                 "VTSAX": {"price": 150.0, "name": None}}
        meta, rows, totals, errors = me.build(clean, cash, found, today=date(2026, 9, 29))
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["market_value"], 3000.0)
        self.assertEqual(rows[0]["reported_gain"], 1000.0)
        conn = portfolio.connect(self.db)
        portfolio.write_snapshot(conn, self.user_id, meta, rows, totals, me.SOURCE)
        self.assertEqual(update_prices.latest_snapshot(conn, self.user_id), "2026-09-29")
        got = {r["symbol"]: r["market_value"] for r in conn.execute(
            "SELECT symbol, market_value FROM positions WHERE user_id = ?", (self.user_id,))}
        self.assertEqual(got, {"VTI": 3000.0, "VTSAX": 825.0})
        cash_rows = {r["account"]: r["cash_value"] for r in conn.execute(
            "SELECT account, cash_value FROM account_totals WHERE user_id = ?", (self.user_id,))}
        self.assertEqual(cash_rows, {"Roth": 500.0, me.DEFAULT_ACCOUNT: None})
        # saving again the same day replaces, it doesn't add
        meta2, rows2, totals2, _ = me.build(clean[:1], {}, found, today=date(2026, 9, 29))
        portfolio.write_snapshot(conn, self.user_id, meta2, rows2, totals2, me.SOURCE)
        self.assertEqual(conn.execute("SELECT COUNT(*) n FROM positions WHERE user_id = ?",
                                      (self.user_id,)).fetchone()["n"], 1)
        conn.close()

    def test_missing_price_is_an_error_and_prefill_round_trips(self):
        import manual_entry as me
        clean, _, _ = me.validate(self.ROWS[:1], [])
        self.assertIn("check the symbol", me.build(clean, {}, {"VTI": {"price": None}})[3][0])
        holdings, cash = me.prefill([{"account": "Roth", "symbol": "VTI", "quantity": 10,
                                      "cost_basis": 2000.0, "asset_type": "Fixed Income"}],
                                    {"Roth": 250.0, "Empty": 0})
        self.assertEqual(holdings[0]["Type"], "Bond")
        self.assertEqual(cash, [{"Account": "Roth", "Cash": 250.0}])
        self.assertEqual(me.validate(holdings, cash)[2], [])


class AssetClassTests(unittest.TestCase):
    AOR = {"quote_type": "ETF", "stock_pct": 0.6178, "bond_pct": 0.3754, "cash_pct": 0.0065,
           "other_pct": 0.0002}

    def test_yahoo_splits_funds_and_classes_stocks_and_money_market(self):
        import asset_classes as ac
        s = ac.from_yahoo(self.AOR)
        self.assertAlmostEqual(sum(s.values()), 1.0)
        self.assertAlmostEqual(s["Stocks"], 0.6178 / 0.9999, places=4)
        self.assertEqual(ac.main_class(s), "Stocks")
        self.assertEqual(ac.from_yahoo({"quote_type": "EQUITY"}), {"Stocks": 1.0})
        self.assertEqual(ac.from_yahoo({"quote_type": "MONEYMARKET"}), {"Cash": 1.0})
        self.assertIsNone(ac.from_yahoo({"quote_type": "ETF"}))       # no breakdown
        self.assertIsNone(ac.from_yahoo(None))
        self.assertEqual(ac.describe({"Bonds": 1.0}), "Bonds")
        self.assertEqual(ac.describe({"Stocks": 0.62, "Bonds": 0.38}), "62% stocks / 38% bonds")

    def test_override_beats_yahoo_beats_broker_type(self):
        import asset_classes as ac
        self.assertEqual(ac.split_for("AOR", "ETFs & Closed End Funds", self.AOR,
                                      {"AOR": "Bonds"}), ({"Bonds": 1.0}, "override"))
        self.assertEqual(ac.split_for("AOR", "ETFs & Closed End Funds", self.AOR)[1], "yahoo")
        self.assertEqual(ac.split_for("BND", "Fixed Income", None), ({"Bonds": 1.0}, "broker"))
        self.assertEqual(ac.split_for("XYZ", "ETFs & Closed End Funds", None),
                         ({"Other": 1.0}, "broker"))
        self.assertEqual(ac.split_for("X", "Equity", None, {"X": "Nonsense"})[1], "broker")

    def test_allocate_splits_balanced_funds_and_falls_back_to_broker_type(self):
        pos = [{"symbol": "AOR", "asset_type": "ETFs & Closed End Funds", "market_value": 1000,
                "account": "A"},
               {"symbol": "BND", "asset_type": "Fixed Income", "market_value": 500, "account": "A"},
               {"symbol": "VTI", "asset_type": "ETFs & Closed End Funds", "market_value": 250,
                "account": "A"}]
        out = allocation.allocate(pos, {"A": 250}, {"AOR": {"Stocks": 0.6, "Bonds": 0.4}})
        by = {r["label"]: r["value"] for r in out["by_asset_class"]}
        self.assertEqual(by, {"Stocks": 600.0, "Bonds": 900.0, "Cash": 250.0, "Other": 250.0})
        self.assertAlmostEqual(sum(r["pct"] for r in out["by_asset_class"]), 100.0)
        self.assertIn("ETF / CEF", {r["label"] for r in out["by_asset_type"]})  # still there

    def test_old_targets_convert_where_they_map(self):
        import asset_classes as ac
        self.assertEqual(ac.convert_targets({"Equity": 60, "Fixed Income": 35, "Cash": 5}),
                         ({"Stocks": 60.0, "Bonds": 35.0, "Cash": 5.0}, False))
        self.assertEqual(ac.convert_targets({"ETF / CEF": 70, "Fixed Income": 30}), ({}, True))
        self.assertEqual(ac.convert_targets({"Stocks": 60, "Bonds": 40}),
                         ({"Stocks": 60.0, "Bonds": 40.0}, False))
        self.assertEqual(ac.convert_targets({"Option": 10}), ({}, True))
        self.assertEqual(ac.convert_targets(None), ({}, False))


class AssetClassStorageTests(TempDBMixin, unittest.TestCase):
    def test_migration_moves_plans_and_models_once(self):
        import asset_classes as ac
        conn = portfolio.connect(self.db)
        other = auth.create_user(conn, "other", "pw")
        conn.execute("INSERT INTO plans (user_id, target_alloc) VALUES (?, ?)",
                     (self.user_id, json.dumps({"Equity": 60, "Fixed Income": 40})))
        conn.execute("INSERT INTO plans (user_id, target_alloc) VALUES (?, ?)",
                     (other, json.dumps({"ETF / CEF": 100})))
        conn.execute("INSERT INTO model_portfolios (advisor_id, name, target_alloc) VALUES (?, ?, ?)",
                     (self.user_id, "Old", json.dumps({"Mutual Funds": 50, "Cash": 50})))
        conn.commit()
        self.assertEqual(ac.migrate_targets(conn),
                         {"plans": 2, "plans_cleared": 1, "models": 1, "models_cleared": 1})
        conn.commit()
        self.assertEqual(plans.get_plan(conn, self.user_id)["target_alloc"],
                         {"Stocks": 60.0, "Bonds": 40.0})
        cleared = plans.get_plan(conn, other)
        self.assertEqual((cleared["target_alloc"], cleared["targets_cleared"]), ({}, 1))
        self.assertEqual(advising.list_models(conn, self.user_id)[0]["target_alloc"], {})
        self.assertEqual(ac.migrate_targets(conn)["plans"], 0)          # nothing left to do
        plans.save_plan(conn, other, {"target_alloc": {"Stocks": 100}}, other)
        self.assertEqual(plans.get_plan(conn, other)["targets_cleared"], 0)  # note goes away
        conn.close()

    def test_fund_split_is_stored_and_read_back(self):
        import asset_classes as ac
        conn = portfolio.connect(self.db)
        sync_history.upsert_info(conn, "AOR", {"name": "Balanced", **AssetClassTests.AOR})
        sync_history.upsert_info(conn, "AAPL", {"quote_type": "EQUITY"})
        conn.commit()
        pos = [{"symbol": "AOR", "asset_type": "ETFs & Closed End Funds"},
               {"symbol": "AAPL", "asset_type": "Equity"},
               {"symbol": "NEW", "asset_type": "Fixed Income"}]
        s = ac.splits(conn, pos, {"AAPL": "Other"})
        self.assertEqual(ac.main_class(s["AOR"]), "Stocks")
        self.assertEqual(s["AAPL"], {"Other": 1.0})                    # override wins
        self.assertEqual(s["NEW"], {"Bonds": 1.0})                     # no Yahoo row yet
        conn.close()


class FetchFundSplitTests(unittest.TestCase):
    """fetch_info asks Yahoo for a breakdown only for funds, and a failure
    there leaves the fund unclassified instead of breaking the sync."""

    def _fake_yf(self, info, asset_classes=None, fail=False):
        calls = []

        class FundsData:
            @property
            def asset_classes(self):
                if fail:
                    raise RuntimeError("no fund data")
                return asset_classes

        class Ticker:
            def __init__(self, t):
                self.info = info

            @property
            def funds_data(self):
                calls.append("funds_data")
                return FundsData()
        return type(sys)("yf"), Ticker, calls

    def _fetch(self, info, **kw):
        fake, ticker_cls, calls = self._fake_yf(info, **kw)
        fake.Ticker = ticker_cls
        with unittest.mock.patch.object(sync_history, "yf", fake):
            return sync_history.fetch_info("T"), calls

    def test_fund_gets_its_split(self):
        out, calls = self._fetch({"quoteType": "ETF", "category": "Allocation"},
                                 asset_classes={"stockPosition": 0.6, "bondPosition": 0.38,
                                                "cashPosition": 0.01, "preferredPosition": 0.01})
        self.assertEqual(calls, ["funds_data"])
        self.assertEqual((out["quote_type"], out["category"]), ("ETF", "Allocation"))
        self.assertEqual((out["stock_pct"], out["bond_pct"], out["other_pct"]), (0.6, 0.38, 0.01))

    def test_stock_makes_no_extra_request_and_failures_stay_blank(self):
        out, calls = self._fetch({"quoteType": "EQUITY"})
        self.assertEqual(calls, [])
        self.assertIsNone(out["stock_pct"])
        out, _ = self._fetch({"quoteType": "MUTUALFUND"}, fail=True)
        self.assertIsNone(out["bond_pct"])
        self.assertEqual(out["quote_type"], "MUTUALFUND")
        out, calls = self._fetch({"trailingPegRatio": None})     # Yahoo doesn't know it
        self.assertEqual((out["quote_type"], calls), ("", []))   # asked - don't keep asking


class WorkflowFileTests(unittest.TestCase):
    """GitHub rejects a workflow with a repeated key and runs nothing - the
    tests silently stopped running once that way (a doubled `cache: pip`)."""

    def test_no_repeated_keys_in_workflows(self):
        folder = os.path.join(REPO, ".github", "workflows")
        for name in os.listdir(folder):
            if not name.endswith((".yml", ".yaml")):
                continue
            seen = [(-1, set())]   # (indent, keys) of each open mapping
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    m = re.match(r"^( *)(- )?([A-Za-z0-9_-]+):(\s|$)", line)
                    if not m or line.lstrip().startswith("#"):
                        continue
                    indent = len(m.group(1)) + (2 if m.group(2) else 0)
                    while seen[-1][0] > indent:
                        seen.pop()
                    if seen[-1][0] < indent or m.group(2):
                        seen.append((indent, set()))
                    key = m.group(3)
                    self.assertNotIn(key, seen[-1][1], f"{name}:{n} repeats '{key}'")
                    seen[-1][1].add(key)


class TxnImportTests(TempDBMixin, unittest.TestCase):
    """Real transactions, phase 1 (txn_import.py): activity exports from any
    brokerage - read, kinds, duplicates, and imported history replacing what
    was worked out from holdings updates. The files are made up."""

    DIR = os.path.join(os.path.dirname(__file__), "fixtures", "brokers")

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _parse(self, name, **kw):
        import csv_import
        import txn_import
        with open(os.path.join(self.DIR, name), "rb") as fh:
            rows = csv_import.read_rows(fh.read())
        self.assertEqual(csv_import.find_header(rows)[1], "transactions")
        hi = txn_import.find_header(rows)
        return txn_import.parse(rows, txn_import.auto_mapping(rows[hi]), header_i=hi, **kw)

    def _kinds(self, found):
        return [(r["action"], r["symbol"], r["amount"]) for r in found["rows"]]

    def test_schwab_layout(self):
        found = self._parse("schwab_activity.csv", account_default="Individual ...678")
        self.assertEqual(self._kinds(found), [
            ("BUY", "VTI", -1550.0), ("DIV", "VTI", 18.4), ("DIV", "SCHD", 12.1),
            ("REINVEST", "SCHD", -12.1), ("DEPOSIT", None, 500.0), ("SELL", "AAPL", 689.98),
            ("INTEREST", None, 0.41), ("TRANSFER", None, -100.0)])
        self.assertEqual(found["rows"][1]["trade_date"], "2026-09-25")   # "as of" ignored
        self.assertEqual(found["rows"][5]["fees"], 0.02)
        self.assertNotIn("1234567890", found["rows"][4]["description"])  # bank number masked
        self.assertEqual(found["skipped"], 1)                             # the total line

    def test_fidelity_layout(self):
        found = self._parse("fidelity_activity.csv")
        self.assertEqual(self._kinds(found), [
            ("BUY", "FXAIX", -421.0), ("DIV", "SPAXX", 3.12), ("REINVEST", "SPAXX", -3.12),
            ("SELL", "AAPL", 230.99), ("DEPOSIT", None, 250.0), ("OTHER", "SPAXX", -250.0)])
        self.assertEqual(found["rows"][3]["quantity"], 1.0)               # sold -1 -> 1
        self.assertEqual(found["accounts"], ["Individual ...678"])        # number cut

    def test_vanguard_and_robinhood_layouts(self):
        v = self._parse("vanguard_activity.csv")
        self.assertEqual([k for k, _, _ in self._kinds(v)],
                         ["BUY", "DIV", "REINVEST", "OTHER", "DEPOSIT"])   # sweep isn't money in
        self.assertEqual(v["rows"][0]["description"], "Vanguard Total Stock Mkt Idx Adm")
        self.assertIsNone(v["rows"][1]["quantity"])
        r = self._parse("robinhood_activity.csv")
        self.assertEqual(self._kinds(r), [("BUY", "SLV", -110.2), ("SELL", "USO", 144.0)])

    def test_saving_twice_adds_nothing_and_replaces_worked_out_rows(self):
        import txn_import
        acct = "Individual ...678"
        for d in ("2026-09-10", "2026-10-02"):   # worked out from two updates
            self.conn.execute("INSERT INTO transactions (account, trade_date, action, symbol, "
                              "quantity, amount, user_id) VALUES (?, ?, 'BUY', 'VTI', 1, -300, ?)",
                              (acct, d, self.user_id))
        self.conn.commit()
        found = self._parse("schwab_activity.csv", account_default=acct)
        first = txn_import.save(self.conn, self.user_id, found["rows"], "upload: a.csv")
        self.assertEqual((first["added"], first["duplicates"], first["replaced"]), (8, 0, 1))
        again = txn_import.save(self.conn, self.user_id, found["rows"], "upload: a.csv")
        self.assertEqual((again["added"], again["duplicates"]), (0, 8))
        left = self.conn.execute("SELECT trade_date FROM transactions WHERE user_id = ? AND "
                                 "origin IS NULL", (self.user_id,)).fetchall()
        self.assertEqual([r[0] for r in left], ["2026-10-02"])           # after the history
        # a later update inside the covered dates doesn't add worked-out rows
        cover = txn_import.covered(self.conn, self.user_id)
        kept = txn_import.drop_covered([{"account": acct, "trade_date": "2026-09-20"},
                                        {"account": acct, "trade_date": "2026-10-05"},
                                        {"account": "Roth ...111", "trade_date": "2026-09-20"}],
                                       cover)
        self.assertEqual([(t["account"], t["trade_date"]) for t in kept],
                         [(acct, "2026-10-05"), ("Roth ...111", "2026-09-20")])

    def test_realized_gains_by_average_cost(self):
        import txn_import

        def row(i, d, kind, q=None, amount=None, sym="VTI", acct="A"):
            return {"id": i, "account": acct, "trade_date": d, "action": kind, "symbol": sym,
                    "quantity": q, "price": None, "amount": amount, "fees": None}
        rows = [
            row(1, "2026-01-05", "BUY", 10, -1000.0),
            row(2, "2026-02-05", "BUY", 10, -1400.0),          # average cost now 120
            row(3, "2026-03-05", "SELL", 5, 700.0),            # 700 - 5 x 120 = 100
            row(4, "2026-04-05", "SELL", 20, 3000.0),          # more than held: unknown
            row(5, "2026-05-05", "BUY", 2, -200.0),            # fresh start after selling out
            row(6, "2026-05-05", "SELL", 2, 260.0),            # same day: the buy goes first
            row(7, "2026-06-01", "SELL", 1, 50.0, sym="OLD"),  # held before the history
            row(8, "2026-06-02", "TRANSFER", 3, None, sym="XFR"),
            row(9, "2026-06-03", "SELL", 3, 90.0, sym="XFR"),  # transferred in: unknown cost
        ]
        gains = txn_import.replay_gains(rows, {("A", "OLD"): 4.0})
        self.assertEqual(gains, {3: 100.0, 4: None, 6: 60.0, 7: None, 9: None})

    def test_saving_works_out_gains_and_income_received(self):
        import income
        import txn_import
        found = self._parse("fidelity_activity.csv")
        txn_import.save(self.conn, self.user_id, found["rows"], "upload: f.csv")
        # the AAPL sale has no buy in this history: its gain is unknown
        g = self.conn.execute("SELECT realized_gain FROM transactions WHERE action = 'SELL' "
                              "AND user_id = ?", (self.user_id,)).fetchone()[0]
        self.assertIsNone(g)
        got = income.received(self.conn, self.user_id, date(2026, 10, 1))
        self.assertEqual(got["dividends"], 3.12)
        self.assertEqual(got["months"][-2]["month"], "2026-09")
        self.assertEqual(got["months"][-2]["by_symbol"], {"SPAXX": 3.12})
        self.assertEqual(got["since"], "2026-09-18")
        self.assertIsNone(income.received(self.conn, self.user_id + 99, date(2026, 10, 1)))

    def test_imported_deposits_are_money_added_once(self):
        import plans
        import reports
        import txn_import
        plans.add_contribution(self.conn, self.user_id, "2026-08-20", 100.0, "before")
        plans.add_contribution(self.conn, self.user_id, "2026-09-15", 500.0, "same deposit")
        found = self._parse("schwab_activity.csv")   # Sep 1-26: +500 deposit, -100 journal
        txn_import.save(self.conn, self.user_id, found["rows"], "upload: s.csv")
        moves = plans.money_moves(self.conn, self.user_id)
        hand = {m["note"]: m["counted"] for m in moves if m["source"] == "hand"}
        self.assertEqual(hand, {"before": True, "same deposit": False})
        # the deposit counts once; the journal between own accounts doesn't count
        self.assertEqual(plans.money_added(self.conn, self.user_id, "2026-09-01", "2026-09-30"),
                         500.0)
        self.assertEqual(plans.month_total(self.conn, self.user_id, 2026, 8), 100.0)
        facts = reports.build(self.conn, self.user_id, date(2026, 9, 1), date(2026, 9, 30),
                              value_now=None, today=date(2026, 10, 1))
        self.assertEqual(facts["money_in"], 500.0)

    def test_two_equal_buys_on_a_day_stay_two(self):
        import txn_import
        row = {"account": "A", "trade_date": "2026-09-01", "action": "BUY", "symbol": "VTI",
               "quantity": 1.0, "amount": -300.0}
        keys = txn_import.row_keys([row, dict(row)])
        self.assertEqual(len(set(keys)), 2)

    def test_match_account_by_last_digits(self):
        import txn_import
        mine = ["Individual ...678", "Roth IRA ...111"]
        self.assertEqual(txn_import.match_account("X12345678", mine), "Individual ...678")
        self.assertIsNone(txn_import.match_account("Joint", mine))


class AccountPageTests(TempDBMixin, unittest.TestCase):
    """The Account page's data side: name, email change by link, other
    devices, deleting your own account."""

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)
        self.conn.execute("UPDATE users SET email = 'testuser', email_verified_at = 'x' "
                          "WHERE id = ?", (self.user_id,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_name_shows_to_the_advisor(self):
        adv = auth.create_user(self.conn, "adv", "x" * 12)
        auth.link_client(self.conn, adv, self.user_id)
        auth.set_display_name(self.conn, self.user_id, "  Sam   Lee ")
        self.assertEqual(auth.display_name(self.conn, self.user_id), "Sam Lee")
        self.assertEqual(auth.list_clients(self.conn, adv), [(self.user_id, "Sam Lee")])
        auth.set_display_name(self.conn, self.user_id, "")
        self.assertEqual(auth.list_clients(self.conn, adv), [(self.user_id, "testuser")])

    def test_email_changes_only_through_the_link(self):
        bad = auth.start_email_change(self.conn, self.user_id, "new@example.com", "nope")
        self.assertFalse(bad["ok"])
        res = auth.start_email_change(self.conn, self.user_id, "New@Example.com", "testpass")
        self.assertTrue(res["ok"])
        self.assertEqual(res["to"], "new@example.com")
        self.assertEqual(auth.email_status(self.conn, self.user_id)["email"], "testuser")
        self.assertEqual(auth.pending_email_change(self.conn, self.user_id), "new@example.com")
        done = auth.confirm_email_change(self.conn, res["token"])
        self.assertTrue(done["ok"])
        # the login was the old email, so it follows
        self.assertEqual(done["username"], "new@example.com")
        self.assertEqual(auth.attempt_login(self.conn, "new@example.com", "testpass")["user_id"],
                         self.user_id)
        self.assertTrue(auth.email_status(self.conn, self.user_id)["confirmed"])
        self.assertFalse(auth.confirm_email_change(self.conn, res["token"])["ok"])  # used up

    def test_email_already_used_elsewhere_is_refused(self):
        other = auth.create_user(self.conn, "taken@example.com", "x" * 12)
        self.assertIsNotNone(other)
        res = auth.start_email_change(self.conn, self.user_id, "taken@example.com", "testpass")
        self.assertFalse(res["ok"])

    def test_sign_out_other_devices_keeps_this_one(self):
        here = auth.create_session(self.conn, self.user_id)
        there = auth.create_session(self.conn, self.user_id)
        self.assertEqual(auth.end_other_sessions(self.conn, self.user_id, here), 1)
        self.assertIsNotNone(auth.session_user(self.conn, here))
        self.assertIsNone(auth.session_user(self.conn, there))

    def test_delete_own_account(self):
        import admin
        adv = auth.create_user(self.conn, "adv", "x" * 12)
        auth.link_client(self.conn, adv, self.user_id)
        self.assertFalse(admin.delete_own(self.conn, self.user_id, "testpass")["ok"])  # managed
        self.assertFalse(admin.delete_own(self.conn, adv, "x" * 12)["ok"])           # has clients
        auth.unlink_client(self.conn, adv, self.user_id)
        self.assertFalse(admin.delete_own(self.conn, self.user_id, "wrong")["ok"])
        self.assertTrue(admin.delete_own(self.conn, self.user_id, "testpass")["ok"])
        self.assertIsNone(auth.get_username(self.conn, self.user_id))


class ExportTests(TempDBMixin, unittest.TestCase):
    """D1: Export everything - the account's own data, never secrets or
    anyone else's."""

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _files(self, user_id):
        import export
        with zipfile.ZipFile(io.BytesIO(export.export_zip(self.conn, user_id))) as z:
            return {n: z.read(n).decode("utf-8") for n in z.namelist()}

    def test_own_data_without_secrets_or_private_notes(self):
        import advising
        advisor = auth.create_user(self.conn, "adv", "x" * 12)
        auth.set_advisor(self.conn, "adv", True)
        auth.link_client(self.conn, advisor, self.user_id)
        advising.add_note(self.conn, self.user_id, advisor, "Note", "shared note", "2026-09-01")
        advising.add_note(self.conn, self.user_id, advisor, "Note", "secret note", "2026-09-01",
                          private=True)
        self.conn.execute("INSERT INTO watchlist (user_id, ticker) VALUES (?, 'VTI')",
                          (self.user_id,))
        self.conn.execute("INSERT INTO watchlist (user_id, ticker) VALUES (?, 'ZZZZ')", (advisor,))
        self.conn.commit()
        files = self._files(self.user_id)
        self.assertIn("README.txt", files)
        self.assertIn("testuser", files["account.csv"])
        csvs = "".join(v for n, v in files.items() if n.endswith(".csv"))
        secret = self.conn.execute("SELECT password_hash, password_salt FROM users WHERE id = ?",
                                   (self.user_id,)).fetchone()
        for text in ("password", "salt", secret[0], secret[1], "secret note", "ZZZZ"):
            self.assertNotIn(text, csvs)
        self.assertIn("shared note", files["from_your_advisor_notes.csv"])
        self.assertIn("VTI", files["watchlist.csv"])

    def test_every_account_table_is_exported_or_left_out_on_purpose(self):
        import admin
        import export
        exported = {t for _, t, _, _ in export.OWN}
        # sign-in records and links: secrets, never exported; an advisor's
        # links with clients are about the client too
        left_out = {"login_sessions", "email_tokens", "invites", "advisor_clients"}
        self.assertEqual(set(admin.ACCOUNT_TABLES) - exported - left_out, set())


class WeeklyEmailTests(TempDBMixin, unittest.TestCase):
    """The advisors' Monday email (weekly_email.py): counts only, once a week,
    only to a confirmed email, never when turned off."""

    def setUp(self):
        super().setUp()
        self.conn = portfolio.connect(self.db)
        auth.set_advisor(self.conn, "testuser", True)
        self.conn.execute("UPDATE users SET email = 'adv@example.com' WHERE id = ?",
                          (self.user_id,))
        self.client = auth.create_user(self.conn, "client one", "x" * 12)
        auth.link_client(self.conn, self.user_id, self.client)
        self.conn.commit()
        self.sent = []

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _send(self, to, link, lines):
        self.sent.append((to, link, lines))
        return True

    def test_counts_only_and_once_a_week(self):
        import weekly_email
        today = date(2026, 10, 5)
        done = weekly_email.run(self.conn, "https://app.example/", today, send=self._send)
        self.assertEqual(done["sent"], 1)
        to, link, lines = self.sent[0]
        self.assertEqual(to, "adv@example.com")
        self.assertEqual(link, "https://app.example/?page=your-clients")
        self.assertEqual(lines, ["1 client is due a review"])
        self.assertNotIn("client one", " ".join(lines))
        again = weekly_email.run(self.conn, "https://app.example/", today, send=self._send)
        self.assertEqual((again["sent"], again["already"]), (0, 1))

    def test_not_when_off_or_unconfirmed(self):
        import weekly_email
        prefs.save(self.conn, self.user_id, {weekly_email.PREF_OFF: True})
        self.assertEqual(weekly_email.run(self.conn, "https://a/", date(2026, 10, 5),
                                          send=self._send)["off"], 1)
        prefs.save(self.conn, self.user_id, {})
        self.conn.execute("UPDATE users SET terms_version = 'v1' WHERE id = ?", (self.user_id,))
        self.conn.commit()
        weekly_email.run(self.conn, "https://a/", date(2026, 10, 5), send=self._send)
        self.assertEqual(self.sent, [])


class HostingTests(unittest.TestCase):
    """L4: the visitor's address behind a proxy, the old address's moved page,
    and the Render blueprint."""

    def test_client_ip_uses_streamlits_address_unless_a_header_is_set(self):
        import hosting
        h = {"x-forwarded-for": "6.6.6.6, 1.2.3.4"}
        self.assertEqual(hosting.client_ip(h, "10.0.0.1", header=""), "10.0.0.1")
        # only the right-hand entry, the one the proxy added, is trusted
        self.assertEqual(hosting.client_ip(h, "10.0.0.1", header="x-forwarded-for"), "1.2.3.4")
        self.assertEqual(hosting.client_ip({"cf-connecting-ip": "5.5.5.5"}, "10.0.0.1",
                                           header="CF-Connecting-IP"), "5.5.5.5")
        self.assertEqual(hosting.client_ip({}, "10.0.0.1", header="x-forwarded-for"), "10.0.0.1")

    def test_moved_link_keeps_the_query(self):
        import hosting
        self.assertEqual(hosting.moved_link("https://app.northwend.app/", {}),
                         "https://app.northwend.app/")
        self.assertEqual(hosting.moved_link("https://app.northwend.app",
                                            {"confirm": "abc", "page": "plan"}),
                         "https://app.northwend.app/?confirm=abc&page=plan")

    def test_render_blueprint_keeps_secrets_out(self):
        with open(os.path.join(REPO, "render.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("healthCheckPath: /_stcore/health", text)
        self.assertIn("branch: main", text)
        for key in ("PORTFOLIO_DB", "ANTHROPIC_API_KEY", "RESEND_API_KEY"):
            i = text.index(f"key: {key}")
            self.assertIn("sync: false", text[i:i + 80], key)


class WebsiteTests(unittest.TestCase):
    """The Northwend website (website/): public/ is what Cloudflare Pages serves."""

    PUBLIC = os.path.join(REPO, "website", "public")

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(REPO, "website"))
        import build as site_build
        cls.site = site_build
        cls.pages = {}
        for n in site_build.PAGES:
            with open(os.path.join(cls.PUBLIC, n), encoding="utf-8") as fh:
                cls.pages[n] = fh.read()

    def test_pages_for_new_investors_and_advisors(self):
        self.assertIn("new-to-investing.html", self.pages)
        self.assertIn("advisors.html", self.pages)
        new, adv = self.pages["new-to-investing.html"], self.pages["advisors.html"]
        self.assertIn("not a prediction", new)                     # the tables are hypothetical
        self.assertIn("don't rank them", new)                      # brokerages as equals
        self.assertIn("not recommendations", new)
        self.assertIn(f'href="{self.site.ADVISOR_SIGNUP_URL}"', adv)
        self.assertIn("CRD", adv)
        # every page links to both, and marks its own link in the header
        for name, page in self.pages.items():
            self.assertIn('href="/new-to-investing"', page, name)
            self.assertIn('href="/advisors"', page, name)
        self.assertIn('href="/advisors" aria-current="page"', adv)
        self.assertNotIn('aria-current="page"', self.pages["about.html"])

    def test_worked_out_tables(self):
        site = self.site
        # $100 a month for 10 years at 6% a year, compounded monthly
        self.assertAlmostEqual(site.grown(100, 10, 0.06), 16387.93, places=1)
        self.assertAlmostEqual(site.after_fees(10_000, 30, 0.01, 0.06), 43219.42, places=1)
        self.assertEqual(site.money(16387.93), "$16,400")
        new = self.pages["new-to-investing.html"]
        self.assertIn(site.money(site.grown(100, 10)), new)
        self.assertIn("you put in $12,000", new)
        self.assertIn(f"assume it grows {site.RATE:.0%} every year", new)
        self.assertIn('<th scope="row">1.00% a year</th>', new)

    def test_phone_menu_contours_and_sitemap(self):
        for name, page in self.pages.items():
            self.assertIn('<details class="menu">', page, name)   # a menu with no script
            self.assertIn('class="contours"', page, name)
        # the contour lines are the app's own (static/topo-light.svg)
        with open(os.path.join(REPO, "static", "topo-light.svg"), encoding="utf-8") as fh:
            first = re.search(r'<path d="([^"]+)"', fh.read()).group(1)
        self.assertIn(first, self.pages["index.html"])
        with open(os.path.join(self.PUBLIC, "sitemap.xml"), encoding="utf-8") as fh:
            sitemap = fh.read()
        for _, _, _, path in self.site.PAGES.values():
            if path != "/404":
                self.assertIn(f"<loc>{self.site.SITE_URL}{path}</loc>", sitemap)
        self.assertNotIn("/404", sitemap)
        with open(os.path.join(self.PUBLIC, "robots.txt"), encoding="utf-8") as fh:
            self.assertIn("Sitemap: https://northwend.app/sitemap.xml", fh.read())

    def test_says_we_dont_sell_investments(self):
        # P1: the positioning is on the home page and the About page (from disclosures)
        self.assertIn("We don't sell investments", self.pages["index.html"])
        self.assertIn("How Northwend is paid", self.pages["about.html"])
        self.assertIn("doesn't sell investments", self.pages["about.html"])

    def test_public_is_up_to_date_with_the_build(self):
        for name, text in self.site.render().items():
            with open(os.path.join(self.PUBLIC, name), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), text, f"website/public/{name} is stale: "
                                 "run python website/build.py")
        for font in self.site.FONTS:
            with open(os.path.join(REPO, "static", font), "rb") as a, \
                    open(os.path.join(self.PUBLIC, "fonts", font), "rb") as b:
                self.assertEqual(a.read(), b.read())

    def test_about_page_carries_every_disclosure(self):
        import disclosures
        about = self.pages["about.html"]
        for title, _ in disclosures.SECTIONS:
            self.assertIn(f'id="{self.site.slug(title)}"', about)
        self.assertIn(disclosures.LAST_UPDATED, about)
        self.assertIn(disclosures.OPERATOR_NAME, about)
        self.assertNotIn("**", about)

    def test_buttons_open_the_app_and_nothing_else_loads_from_elsewhere(self):
        app = self.site.APP_URL
        self.assertNotIn("REPLACE", app)
        self.assertTrue(app.startswith("https://") and app.endswith("/"))
        for name, page in self.pages.items():
            self.assertNotIn("{{", page)
            self.assertNotIn("<script", page)      # no JavaScript at all
            self.assertNotIn("style=", page)       # the CSP allows only styles.css
            for url in re.findall(r'(?:href|src)="(https?://[^"]+)"', page):
                self.assertTrue(url.startswith((app, self.site.SITE_URL)), f"{name}: {url}")
        self.assertIn(f'href="{app}?signup=1"', self.pages["index.html"])
        self.assertIn(f'href="{app}"', self.pages["index.html"])

    def test_internal_links_and_anchors_resolve(self):
        ids = {n: set(re.findall(r'id="([^"]+)"', p)) for n, p in self.pages.items()}
        for name, page in self.pages.items():
            for href in re.findall(r'href="(/[^"]*|#[^"]*)"', page):
                path, _, anchor = href.partition("#")
                target = name if not path else ("index.html" if path == "/" else path.lstrip("/") + ".html")
                if path.endswith((".css", ".svg", ".woff2")):
                    self.assertTrue(os.path.exists(os.path.join(self.PUBLIC, path.lstrip("/"))), href)
                    continue
                self.assertIn(target, self.pages, f"{name}: {href}")
                if anchor:
                    self.assertIn(anchor, ids[target], f"{name}: {href}")


class DisclosureTests(unittest.TestCase):
    def test_text_is_safe_markdown_and_covers_the_basics(self):
        import disclosures
        text = disclosures.SUMMARY + "".join(t + b for t, b in disclosures.SECTIONS)
        self.assertNotIn("$", text)  # Streamlit reads a pair of them as math
        for must in ("not financial advice", "Anthropic", "percentages", "column names",
                     "ticker", "any brokerage", "hypothetical", "18 and over", "as-is",
                     "For advisors", "Cookies", "How long it's kept", "Neon", "GitHub",
                     "Resend", "support@northwend.app", "Andrew Zhang"):
            self.assertIn(must.lower(), text.lower())
        # everything is filled in: no placeholders left on the page
        self.assertEqual(disclosures.placeholders(), [])
        self.assertNotIn("[operator name]", text)
        self.assertNotIn("[contact email]", text)

    def test_the_no_tracking_promise_matches_the_config(self):
        with open(os.path.join(REPO, ".streamlit", "config.toml"), encoding="utf-8") as fh:
            self.assertIn("gatherUsageStats = false", fh.read())

    def test_the_ai_summary_it_describes_has_no_dollar_amounts(self):
        # disclosures promise the AI sees weights only; hold the code to it
        ctx = {"pos": {"symbol": "VTI", "account": "Brokerage 1234", "quantity": 123.0,
                       "market_value": 45678.9}, "metrics": {}}
        with unittest.mock.patch.object(advisor.M, "value",
                                        side_effect=lambda k, c: {"pct_of_portfolio": 100.0,
                                                                  "description": "Total Market"}.get(k)):
            text = advisor.portfolio_summary([ctx], {"Brokerage 1234": 250.0})
        for leak in ("45678", "45,678", "123", "Brokerage 1234", "250"):
            self.assertNotIn(leak, text)


class FriendlyErrorTests(unittest.TestCase):
    """An error in a page shows the friendly message, never the traceback
    (unless running locally with details on)."""

    SCRIPT = """
import sys
sys.path.insert(0, {repo!r})
import friendly_errors
import streamlit as st
friendly_errors.install(show_details={details})
st.write("before the error")
raise RuntimeError("secret detail")
"""

    def _run(self, details):
        from streamlit.testing.v1 import AppTest
        with contextlib.redirect_stderr(io.StringIO()) as err:
            at = AppTest.from_string(self.SCRIPT.format(repo=REPO, details=details)).run()
        return at, err.getvalue()

    def test_hosted_shows_the_message_and_logs_the_code(self):
        at, log = self._run(False)
        self.assertEqual(len(at.exception), 0)                      # no traceback on screen
        msg = at.error[0].value
        self.assertIn("Something went wrong", msg)
        self.assertNotIn("secret detail", msg)
        code = re.search(r"\*\*([0-9a-f]{6})\*\*", msg).group(1)
        self.assertIn(f"error code {code}", log)                    # matches the log line
        self.assertEqual([b.label for b in at.button], ["Try again"])
        self.assertEqual(at.markdown[0].value, "before the error")  # the page so far stays

    def test_local_run_can_show_details(self):
        at, _ = self._run(True)
        self.assertEqual(len(at.exception), 1)
        self.assertIn("secret detail", at.exception[0].message)

    def test_install_outside_a_streamlit_run_does_nothing(self):
        import friendly_errors
        self.assertFalse(friendly_errors.install())


class CodeFreshTests(unittest.TestCase):
    """A deploy that changes a module's file reloads all of the app's modules."""

    def setUp(self):
        import codefresh
        self.cf = codefresh
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        sys.path.insert(0, self.dir)
        self.addCleanup(sys.path.remove, self.dir)
        self.names = ["cf_mod_a", "cf_mod_b"]
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in self.names])
        for n in self.names:
            self._write(n, "VERSION = 1\n")

    def _write(self, name, text):
        with open(os.path.join(self.dir, name + ".py"), "w") as f:
            f.write(text)

    def _load(self):
        import importlib
        importlib.invalidate_caches()
        return [importlib.import_module(n) for n in self.names]

    def test_unchanged_files_keep_their_modules(self):
        a, _ = self._load()
        self.cf.mark_loaded(self.dir)
        self.assertEqual(self.cf.drop_stale(self.dir), {})
        self.assertIs(sys.modules["cf_mod_a"], a)

    def test_one_changed_file_reloads_them_all(self):
        a, b = self._load()
        self.cf.mark_loaded(self.dir)
        self._write("cf_mod_a", "VERSION = 2\nNEW = True\n")
        with contextlib.redirect_stderr(io.StringIO()):
            old = self.cf.drop_stale(self.dir)
        self.assertEqual(set(old), set(self.names))
        a2, b2 = self._load()
        self.assertEqual((a2.VERSION, a2.NEW), (2, True))
        self.assertIsNot(b2, b)  # the unchanged one is reloaded too
        self.cf.mark_loaded(self.dir)
        self.assertEqual(self.cf.drop_stale(self.dir), {})

    def test_modules_loaded_before_codefresh_count_as_stale(self):
        self._load()  # loaded, never stamped - a server running before the safeguard
        with unittest.mock.patch.object(self.cf, "_PREEXISTING", set(self.names)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(set(self.cf.drop_stale(self.dir)), set(self.names))
        self._load()  # imported after codefresh (e.g. a lazy import): trusted
        self.assertEqual(self.cf.drop_stale(self.dir), {})

    def test_connection_pools_carry_over(self):
        old_pg = type(sys)("pgcompat")
        old_pg._POOLS = {"dsn": "pool"}
        new_pg = type(sys)("pgcompat")
        new_pg._POOLS = {}
        with unittest.mock.patch.dict(sys.modules, {"pgcompat": new_pg}):
            self.cf.carry_over({"pgcompat": old_pg})
        self.assertEqual(new_pg._POOLS, {"dsn": "pool"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
