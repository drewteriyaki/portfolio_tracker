"""Unit tests for the pure logic modules. Standard library only.

    python -m unittest discover -s tests        (from the repo root)
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import alerts  # noqa: E402
import allocation  # noqa: E402
import auth  # noqa: E402
import changes  # noqa: E402
import charts  # noqa: E402
import metrics as M  # noqa: E402
import pandas as pd  # noqa: E402
import perf  # noqa: E402
import portfolio  # noqa: E402
import sync_history  # noqa: E402
import update_prices  # noqa: E402
import watchlist  # noqa: E402
import news  # noqa: E402
import pgcompat  # noqa: E402

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


class ChartsTests(unittest.TestCase):
    def _df(self, n=40):
        idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
        return pd.DataFrame({"t": idx, "v": [100.0 + i for i in range(n)]})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
