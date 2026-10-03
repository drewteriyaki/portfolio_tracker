"""Fund overlap (fund_holdings.py, views/fund_overlap.py) and yield on cost
(income.yield_on_cost, views/income.py). No network: Yahoo is made up.

    python -m unittest discover -s tests        (from the repo root)
"""

import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import auth  # noqa: E402
import fund_holdings as fh  # noqa: E402
import income  # noqa: E402
import manual_entry  # noqa: E402
import portfolio  # noqa: E402
import sample_data  # noqa: E402
import sync_history  # noqa: E402


def _tops(*pairs):
    return [{"symbol": s, "name": n, "weight": w} for s, n, w in pairs]


# made-up top holdings (weights are fractions of each fund)
VTI = _tops(("AAPL", "Apple Inc", 0.06), ("MSFT", "Microsoft Corp", 0.055),
            ("NVDA", "NVIDIA Corp", 0.05), ("AMZN", "Amazon.com Inc", 0.03),
            ("META", "Meta Platforms Inc", 0.02), ("BRK-B", "Berkshire Hathaway Inc", 0.015))
VOO = _tops(("AAPL", "Apple Inc", 0.07), ("MSFT", "Microsoft Corp", 0.065),
            ("NVDA", "NVIDIA Corp", 0.06), ("AMZN", "Amazon.com Inc", 0.035),
            ("META", "Meta Platforms Inc", 0.025), ("GOOGL", "Alphabet Inc", 0.02))
SCHD = _tops(("KO", "Coca-Cola Co", 0.04), ("PEP", "PepsiCo Inc", 0.04),
             ("AAPL", "Apple Inc", 0.001), ("ABBV", "AbbVie Inc", 0.04),
             ("CVX", "Chevron Corp", 0.04), ("HD", "Home Depot Inc", 0.04))
VXUS = _tops(("2330.TW", "Taiwan Semiconductor", 0.03), ("NESN", "Nestle SA", 0.01))


def _cached(**funds):
    return {f: {"holdings": h, "fetched_at": "2026-10-01T00:00:00Z"} for f, h in funds.items()}


class PairOverlapTests(unittest.TestCase):
    def test_shared_count_and_weight(self):
        p = fh.pair_overlap(VTI, VOO)
        self.assertEqual((p["n_shared"], p["of"], p["level"]), (5, 6, "most"))
        # the smaller weight of each shared holding, added up
        self.assertAlmostEqual(p["weight"], 0.06 + 0.055 + 0.05 + 0.03 + 0.02)
        self.assertEqual(p["shared"][0], "Apple Inc")       # biggest shared weight first

    def test_levels(self):
        self.assertEqual(fh.pair_overlap(VTI, SCHD)["level"], "few")      # 1 of 6
        self.assertEqual(fh.pair_overlap(VTI, VXUS)["level"], "none")
        self.assertEqual(fh.pair_overlap(VTI, VTI[:2] + SCHD[:2])["level"], "some")  # 2 of 4
        self.assertEqual(fh.pair_overlap([], VTI)["level"], "none")

    def test_ticker_spellings_and_holdings_without_one_match(self):
        a = _tops(("BRK.B", "Berkshire", 0.02), ("", "US Treasury Note 4.25%", 0.01))
        b = _tops(("BRK-B", "Berkshire", 0.03), (None, "US  Treasury Note 4.25%", 0.02))
        p = fh.pair_overlap(a, b)
        self.assertEqual(p["n_shared"], 2)
        self.assertAlmostEqual(p["weight"], 0.03)

    def test_overlaps_and_words(self):
        tops = _cached(VTI=VTI, VOO=VOO, SCHD=SCHD, BND=[])
        pairs = fh.overlaps(tops, ["VTI", "VOO", "SCHD", "BND", "NEWF"])
        self.assertEqual([(p["a"], p["b"]) for p in pairs],
                         [("VOO", "VTI"), ("SCHD", "VOO"), ("SCHD", "VTI")])
        self.assertEqual(fh.describe(pairs[0]),
                         "VOO and VTI share most of their largest holdings (5 of their top 6).")
        self.assertEqual(fh.describe(pairs[1]),
                         "SCHD and VOO share a few of their largest holdings (1 of their top 6).")
        none = fh.overlaps(_cached(VTI=VTI, VXUS=VXUS), ["VTI", "VXUS"])[0]
        self.assertEqual(fh.describe(none),
                         "VTI and VXUS have none of their largest holdings in common.")
        self.assertEqual(fh.overlaps(tops, ["VTI"]), [])


class LookThroughTests(unittest.TestCase):
    HOLDINGS = [
        {"symbol": "VTI", "name": "Total Stock", "kind": "fund", "value": 6000},
        {"symbol": "VTI", "name": "Total Stock", "kind": "fund", "value": 4000},   # 2nd account
        {"symbol": "VOO", "name": "S&P 500", "kind": "fund", "value": 5000},
        {"symbol": "AAPL", "name": "Apple Inc", "kind": "stock", "value": 1000},
        {"symbol": "BND", "name": "Total Bond", "kind": "fund", "value": 3000},
        {"symbol": "SPAXX", "name": "Money market", "kind": "cash", "value": 500},
        {"symbol": "KO", "name": "Coca-Cola", "kind": "stock", "value": 200}]

    def test_apple_through_two_funds_and_directly(self):
        r = fh.look_through(self.HOLDINGS, _cached(VTI=VTI, VOO=VOO, BND=[]), total=20_000)
        apple = r["companies"][0]
        self.assertEqual(apple["symbol"], "AAPL")
        self.assertAlmostEqual(apple["value"], 10_000 * 0.06 + 5000 * 0.07 + 1000)
        self.assertAlmostEqual(apple["pct"], apple["value"] / 20_000 * 100)
        self.assertAlmostEqual(apple["direct"], 1000)
        self.assertEqual([f for f, _ in apple["via"]], ["VTI", "VOO"])    # biggest first
        self.assertEqual(fh.through(apple), ["VTI", "VOO", "directly"])
        msft = next(c for c in r["companies"] if c["symbol"] == "MSFT")
        self.assertAlmostEqual(msft["value"], 10_000 * 0.055 + 5000 * 0.065)
        self.assertEqual(fh.through(msft), ["VTI", "VOO"])
        ko = next(c for c in r["companies"] if c["symbol"] == "KO")
        self.assertEqual((ko["value"], fh.through(ko)), (200, ["directly"]))
        self.assertEqual(r["funds_seen"], ["VOO", "VTI"])
        self.assertEqual(r["funds_missing"], ["BND"])                    # none listed
        self.assertNotIn("SPAXX", [c["symbol"] for c in r["companies"]])  # cash isn't a company
        values = [c["value"] for c in r["companies"]]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_total_defaults_to_the_holdings_and_nothing_known_is_fine(self):
        r = fh.look_through(self.HOLDINGS, {})
        self.assertEqual(r["total"], 19_700)
        self.assertEqual([c["symbol"] for c in r["companies"]], ["AAPL", "KO"])   # direct only
        self.assertEqual(sorted(r["funds_missing"]), ["BND", "VOO", "VTI"])
        self.assertEqual(fh.look_through([], {})["companies"], [])
        self.assertIsNone(fh.look_through(self.HOLDINGS[3:4], {}, total=0)["companies"][0]["pct"])

    def test_funds_in_uses_the_fee_checks_kinds(self):
        positions = [{"symbol": "VTI", "asset_type": "ETFs & Closed End Funds"},
                     {"symbol": "VTI", "asset_type": "ETFs & Closed End Funds"},
                     {"symbol": "AAPL", "asset_type": "Equity"},
                     {"symbol": "FXAIX", "asset_type": None},
                     {"symbol": "SPAXX", "asset_type": "Mutual Funds"}]
        info = {"FXAIX": {"quote_type": "MUTUALFUND"}, "SPAXX": {"quote_type": "MONEYMARKET"}}
        self.assertEqual(fh.funds_in(positions, info), ["FXAIX", "VTI"])
        self.assertEqual(fh.funds_in([], None), [])


class _FakeFrame:
    """Just enough of yfinance's top_holdings DataFrame."""

    def __init__(self, rows):
        import pandas as pd
        self.df = pd.DataFrame({"Symbol": [r[0] for r in rows], "Name": [r[1] for r in rows],
                                "Holding Percent": [r[2] for r in rows]}).set_index("Symbol")


class YahooTopTests(unittest.TestCase):
    def _top(self, rows=None, error=None):
        import yfinance

        class Ticker:
            def __init__(self, t):
                if error:
                    raise error

            @property
            def funds_data(self):
                return type("FD", (), {"top_holdings": None if rows is None
                                       else _FakeFrame(rows).df})()
        with unittest.mock.patch.object(yfinance, "Ticker", Ticker):
            return fh.yahoo_top("VTI")

    def test_reads_fractions_and_skips_blanks(self):
        out = self._top([("AAPL", "Apple Inc", 0.0634), ("", "Cash", float("nan")),
                         (None, "US Treasury", 0.01)])
        self.assertEqual(out, [{"symbol": "AAPL", "name": "Apple Inc", "weight": 0.0634},
                               {"symbol": "", "name": "US Treasury", "weight": 0.01}])

    def test_percents_become_fractions_and_ten_at_most(self):
        out = self._top([(f"T{i}", f"Co {i}", 6.0) for i in range(12)])
        self.assertEqual(len(out), fh.TOP_N)
        self.assertAlmostEqual(out[0]["weight"], 0.06)

    def test_none_listed_and_offline(self):
        self.assertEqual(self._top(None), [])
        self.assertEqual(self._top([]), [])
        with self.assertRaises(RuntimeError):
            self._top(error=RuntimeError("offline"))
        from yfinance.exceptions import YFDataException   # answered: no fund data
        self.assertEqual(self._top(error=YFDataException("VTI: No Fund data found.")), [])


class CacheTests(unittest.TestCase):
    """Fetched once, kept a week, an old answer kept when Yahoo is away."""

    def setUp(self):
        fh.clear_pause()
        self.dir = tempfile.mkdtemp(prefix="pt_overlap_")
        self.db = os.path.join(self.dir, "t.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(self.db))
        self.conn = portfolio.connect(self.db)
        self.asked = []
        self.now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        fh.clear_pause()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _yahoo(self, answers):
        def raw(fund):
            self.asked.append(fund)
            a = answers.get(fund)
            if isinstance(a, Exception):
                raise a
            return a
        return raw

    def test_fetch_once_then_from_the_table(self):
        raw = self._yahoo({"VTI": VTI, "VOO": VOO, "BND": []})
        tops, failed = fh.ensure(self.conn, ["VTI", "VOO", "BND"], now=self.now, raw=raw)
        self.assertEqual((sorted(self.asked), failed), (["BND", "VOO", "VTI"], []))
        self.assertEqual(tops["VTI"]["holdings"], VTI)
        self.assertEqual(tops["BND"]["holdings"], [])
        again, _ = fh.ensure(self.conn, ["VTI", "VOO", "BND"], now=self.now + timedelta(days=6),
                             raw=raw)
        self.assertEqual(len(self.asked), 3)                 # nothing asked again in a week
        self.assertEqual(again["VOO"]["holdings"], VOO)
        self.assertEqual(again["BND"]["holdings"], [])       # "none listed" is remembered
        self.assertEqual(again["VTI"]["fetched_at"], "2026-10-03T12:00:00Z")
        # stored as shared market data: no user_id, one row per holding plus "asked"
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(fund_top_holdings)")}
        self.assertNotIn("user_id", cols)
        n = self.conn.execute("SELECT COUNT(*) AS n FROM fund_top_holdings WHERE fund = 'VTI'")
        self.assertEqual(n.fetchone()["n"], len(VTI) + 1)

    def test_refreshed_after_a_week(self):
        fh.ensure(self.conn, ["VTI"], now=self.now, raw=self._yahoo({"VTI": VTI}))
        newer = VTI[:3]
        tops, _ = fh.ensure(self.conn, ["VTI"], now=self.now + timedelta(days=8),
                            raw=self._yahoo({"VTI": newer}))
        self.assertEqual(self.asked, ["VTI", "VTI"])
        self.assertEqual(tops["VTI"]["holdings"], newer)
        self.assertEqual(fh.cached(self.conn, ["VTI"])["VTI"]["holdings"], newer)  # replaced

    def test_offline_keeps_the_old_answer_and_says_what_is_missing(self):
        fh.ensure(self.conn, ["VTI"], now=self.now, raw=self._yahoo({"VTI": VTI}))
        down = RuntimeError("offline")
        tops, failed = fh.ensure(self.conn, ["VTI", "VOO"], now=self.now + timedelta(days=30),
                                 raw=self._yahoo({"VTI": down, "VOO": down}))
        self.assertEqual(tops["VTI"]["holdings"], VTI)       # a month old, still shown
        self.assertNotIn("VOO", tops)
        self.assertEqual(failed, ["VOO"])
        # ... and for a couple of minutes Yahoo isn't asked again at all
        n = len(self.asked)
        _, failed = fh.ensure(self.conn, ["VOO"], now=self.now + timedelta(days=30),
                              raw=self._yahoo({"VOO": VOO}))
        self.assertEqual((len(self.asked), failed), (n, ["VOO"]))
        fh.clear_pause()
        tops, failed = fh.ensure(self.conn, ["VOO"], raw=self._yahoo({"VOO": VOO}))
        self.assertEqual((tops["VOO"]["holdings"], failed), (VOO, []))

    def test_a_slow_answer_is_not_waited_for(self):
        gate = threading.Event()

        def slow(fund):
            gate.wait(5)
            return VTI
        t0 = time.time()
        out = fh.fetch(["VTI", "VOO"], timeout=0.2, raw=slow)
        self.assertLess(time.time() - t0, 2)
        gate.set()
        self.assertEqual(out, {"VTI": None, "VOO": None})

    def test_no_funds_no_query(self):
        self.assertEqual(fh.cached(self.conn, []), {})
        self.assertEqual(fh.ensure(self.conn, [], raw=self._yahoo({})), ({}, []))
        self.assertEqual(self.asked, [])

    def test_stale(self):
        self.assertTrue(fh.is_stale(None))
        self.assertTrue(fh.is_stale({"fetched_at": "garbage"}))
        e = {"fetched_at": "2026-10-01T00:00:00Z"}
        self.assertFalse(fh.is_stale(e, datetime(2026, 10, 7, tzinfo=timezone.utc)))
        self.assertTrue(fh.is_stale(e, datetime(2026, 10, 9, tzinfo=timezone.utc)))

    def test_an_older_database_gains_the_table(self):
        self.conn.execute("DROP TABLE fund_top_holdings")
        self.conn.commit()
        self.conn.close()
        portfolio._SCHEMA_READY.discard(os.path.abspath(self.db))
        with contextlib.closing(sqlite3.connect(self.db)) as raw:
            self.assertIsNone(raw.execute("SELECT name FROM sqlite_master WHERE "
                                          "name = 'fund_top_holdings'").fetchone())
        self.conn = portfolio.connect(self.db)
        self.assertEqual(fh.cached(self.conn, ["VTI"]), {})

    def test_both_schemas_have_it(self):
        for name in ("schema.sql", "schema_pg.sql"):
            with open(os.path.join(REPO, name), encoding="utf-8") as fh_:
                text = fh_.read()
            with self.subTest(name):
                self.assertIn("CREATE TABLE IF NOT EXISTS fund_top_holdings", text)


class YieldOnCostTests(unittest.TestCase):
    def test_maths(self):
        self.assertAlmostEqual(income.yield_on_cost(41, 1000), 4.1)
        # bought cheaper than today: higher than the current yield
        value, cost, current_yield = 2000.0, 1000.0, 2.0
        self.assertAlmostEqual(income.yield_on_cost(value * current_yield / 100, cost), 4.0)
        self.assertEqual(income.yield_on_cost(0, 1000), 0)

    def test_unknown_cost_shows_nothing(self):
        for cost in (None, 0, -5, "", float("nan")):
            with self.subTest(cost=cost):
                self.assertIsNone(income.yield_on_cost(40, cost))
        self.assertIsNone(income.yield_on_cost(None, 1000))

    def test_total_counts_only_known_costs(self):
        rows = [{"est_income": 40, "cost": 1000}, {"est_income": 10, "cost": 250},
                {"est_income": 99, "cost": None}]
        self.assertAlmostEqual(income.yield_on_cost_total(rows), 50 / 1250 * 100)
        self.assertIsNone(income.yield_on_cost_total([{"est_income": 99, "cost": None}]))
        self.assertIsNone(income.yield_on_cost_total([]))


class PageTests(unittest.TestCase):
    """Home's Fund overlap card and window, and the Income table's yield on
    cost, drawn by AppTest with Yahoo out of reach."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="pt_overlap_app_")
        cls.db = os.path.join(cls.dir, "app.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(cls.db))
        conn = portfolio.connect(cls.db)
        cls.uid = auth.create_user(conn, "alice", "pw-123456")
        sample_data.load(conn, cls.uid)    # VTI VXUS BND AAPL VOO SCHD
        cls.bob = auth.create_user(conn, "bob", "pw-123456")
        sample_data.load(conn, cls.bob)
        for t, qt in (("VTI", "ETF"), ("VOO", "ETF"), ("SCHD", "ETF"), ("BND", "ETF"),
                      ("VXUS", "ETF"), ("AAPL", "EQUITY")):
            sync_history.upsert_info(conn, t, {"quote_type": qt})
        now = datetime.now(timezone.utc)
        for f, h in (("VTI", VTI), ("VOO", VOO), ("SCHD", SCHD), ("BND", []), ("VXUS", VXUS)):
            fh.store(conn, f, h, now)
        # yields from the brokerage's file; SCHD's cost unknown
        conn.execute("UPDATE positions SET div_yield_pct = 3.5 WHERE symbol = 'SCHD'")
        conn.execute("UPDATE positions SET div_yield_pct = 1.2 WHERE symbol = 'VTI'")
        conn.execute("UPDATE positions SET cost_basis = NULL WHERE symbol = 'SCHD' "
                     "AND user_id = ?", (cls.uid,))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        fh.clear_pause()

    @contextlib.contextmanager
    def _run(self, page, uid=None, **state):
        import yfinance
        from streamlit.testing.v1 import AppTest

        def offline(*a, **k):
            raise RuntimeError("offline in tests")
        at = AppTest.from_file(os.path.join(REPO, "dashboard.py"), default_timeout=120)
        uid = uid or self.uid
        for k, v in {"user_id": uid, "username": "alice" if uid == self.uid else "bob",
                     "page": page, "auto_backfilled": True, "income_synced": True,
                     **state}.items():
            at.session_state[k] = v
        env = {k: v for k, v in os.environ.items() if k != "FINNHUB_API_KEY"}
        env.update(PORTFOLIO_DB=self.db, MAIL_DRY_RUN="1", ANTHROPIC_API_KEY="sk-test-unused")
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(yfinance, "Ticker", offline), \
                unittest.mock.patch("socket.socket.connect", offline):
            at.run()
            self.assertEqual([e.message for e in at.exception], [])
            yield at
            self.assertEqual([e.message for e in at.exception], [])

    @staticmethod
    def _html(at):
        return " ".join(h.proto.body for h in at.get("html"))

    def _set_fetched(self, when):
        with contextlib.closing(portfolio.connect(self.db)) as c:
            c.execute("UPDATE fund_top_holdings SET fetched_at = ?", (when,))
            c.commit()

    def test_home_card_and_window(self):
        with self._run("Dashboard") as at:
            self.assertIn("Fund overlap", self._html(at))
            self.assertIn("VOO and VTI share most of their largest holdings", self._html(at))
            at.button(key="overlap_open").click().run()
            self.assertTrue(at.session_state["dialog_open"])   # live prices wait
            text = " ".join(m.value for m in at.markdown)
            self.assertIn("VOO and VTI share most of their largest holdings (5 of their top 6)",
                          text)
            self.assertIn("Top holdings aren't available for BND", text)
            # pairs that share nothing (VXUS here) are one calm line, not a row each
            self.assertIn("Your other funds don't share any of their largest holdings", text)
            self.assertNotIn("VXUS have none", text)
            captions = " ".join(c.value for c in at.caption)
            self.assertIn("top 10 holdings", captions)
            self.assertIn("isn't a suggestion to buy or sell", captions)
            frames = [d.value for d in at.dataframe]
            pairs = next(d for d in frames if "Funds" in d.columns)
            self.assertEqual(list(pairs["Funds"]), ["VOO and VTI", "SCHD and VOO", "SCHD and VTI"])
            companies = next(d for d in frames if "Company" in d.columns)
            self.assertTrue(companies.iloc[0]["Company"].startswith("Apple"))
            self.assertEqual(companies.iloc[0]["Through"], "VTI, VOO, SCHD and directly")
            self.assertRegex(companies.iloc[0]["Share of your portfolio"], r"^\d+(\.\d)?%$")
            self.assertRegex(companies.iloc[0]["Value"], r"^\$[\d,]+$")

    def test_hidden_amounts(self):
        with self._run("Dashboard", hide_amounts=True) as at:
            at.button(key="overlap_open").click().run()
            companies = next(d.value for d in at.dataframe if "Company" in d.value.columns)
            self.assertTrue((companies["Value"] == "•••").all())
            self.assertTrue((companies["Share of your portfolio"] == "•••").all())
            self.assertIn("about **•••** of your", " ".join(m.value for m in at.markdown))

    def test_offline_with_nothing_kept_says_so(self):
        with contextlib.closing(portfolio.connect(self.db)) as c:
            saved = [tuple(r) for r in c.execute("SELECT * FROM fund_top_holdings")]
            c.execute("DELETE FROM fund_top_holdings")
            c.commit()
        try:
            with self._run("Dashboard") as at:
                self.assertIn("Do your funds hold the same companies?", self._html(at))
                at.button(key="overlap_open").click().run()
                text = " ".join(m.value for m in at.markdown)
                self.assertIn("We couldn't get the top holdings for", text)
                self.assertFalse([d for d in at.dataframe if "Funds" in d.value.columns])
        finally:
            with contextlib.closing(portfolio.connect(self.db)) as c:
                c.executemany(f"INSERT INTO fund_top_holdings VALUES "
                              f"({', '.join('?' for _ in saved[0])})", saved)
                c.commit()

    def test_a_stale_answer_is_still_shown_offline(self):
        self._set_fetched("2026-01-01T00:00:00Z")
        try:
            with self._run("Dashboard") as at:
                self.assertIn("share most of their largest holdings", self._html(at))
                at.button(key="overlap_open").click().run()
                self.assertTrue([d for d in at.dataframe if "Funds" in d.value.columns])
                self.assertIn("Jan 1, 2026", " ".join(c.value for c in at.caption))
        finally:
            self._set_fetched(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def _income_table(self, at):
        at.button(key="tile_income_holdings").click().run()
        return next(d.value for d in at.dataframe if "Symbol" in d.value.columns)

    def test_income_yield_on_cost(self):
        with self._run("Income") as at:
            self.assertTrue(any("your yield on cost" in c.value for c in at.caption))
            table = self._income_table(at)
            row = table.set_index("Symbol")
            # VTI: 40 shares at the sample's price, cost $9,200
            self.assertRegex(row.loc["VTI", "Yield on Cost %"], r"^\d+\.\d\d%$")
            self.assertEqual(row.loc["SCHD", "Yield on Cost %"], "")      # cost unknown
            labels = [m.label for m in at.metric]
            self.assertIn("Your yield on cost", labels)
            self.assertTrue(any("**Yield on cost** is this year's" in c.value for c in at.caption))
        with self._run("Income", hide_amounts=True) as at:
            row = self._income_table(at).set_index("Symbol")
            self.assertEqual(row.loc["VTI", "Yield on Cost %"], "•••")

    def test_percentages_only_has_no_yield_on_cost(self):
        with contextlib.closing(portfolio.connect(self.db)) as c:
            c.execute("UPDATE snapshots SET source_file = ? WHERE user_id = ?",
                      (manual_entry.PCT_SOURCE, self.bob))
            c.execute("UPDATE positions SET div_yield_pct = 3.5 WHERE user_id = ? AND "
                      "symbol = 'SCHD'", (self.bob,))
            c.commit()
        with self._run("Income", uid=self.bob) as at:
            self.assertFalse(any("yield on cost" in c.value for c in at.caption))
            table = self._income_table(at)
            self.assertTrue((table["Yield on Cost %"] == "").all())
            self.assertNotIn("Your yield on cost", [m.label for m in at.metric])


if __name__ == "__main__":
    unittest.main()
