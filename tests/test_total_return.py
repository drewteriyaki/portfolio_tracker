"""Total return with dividends (Home, a holding's details) and what the
portfolio could pay each year (the Plan's retirement view): the maths in
income.py and plans.py, then dashboard.py drawn with streamlit's AppTest on a
scratch database in a temp dir.

    python -m unittest tests.test_total_return        (from the repo root)
"""

import contextlib
import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import advisor  # noqa: E402
import auth  # noqa: E402
import income  # noqa: E402
import manual_entry  # noqa: E402
import metrics as M  # noqa: E402
import plans  # noqa: E402
import portfolio  # noqa: E402
import sample_data  # noqa: E402

TODAY = date(2026, 10, 3)


def _snapshot(c, uid, day, holdings, source=manual_entry.SOURCE, cash=None):
    """Save a holdings update: holdings [(symbol, shares, cost)] in "Brokerage"."""
    meta, rows, totals, _ = manual_entry.build(
        [{"account": "Brokerage", "symbol": s, "quantity": q, "cost_basis": cost,
          "asset_type": "ETFs & Closed End Funds"} for s, q, cost in holdings],
        {"Brokerage": cash} if cash is not None else {},
        {s: {"price": 100.0, "name": s} for s, _, _ in holdings}, today=day)
    portfolio.write_snapshot(c, uid, meta, rows, totals, source)


def _txn(c, uid, day, action, symbol, amount, origin="imported"):
    c.execute("INSERT INTO transactions (user_id, account, trade_date, action, symbol, amount, "
              "origin) VALUES (?, 'Brokerage', ?, ?, ?, ?, ?)",
              (uid, day.isoformat(), action, symbol, amount, origin))


def _bar(c, ticker, day, dividend):
    c.execute("INSERT INTO daily_bars (ticker, date, close, dividend) VALUES (?, ?, 100, ?) "
              "ON CONFLICT (ticker, date) DO UPDATE SET dividend = excluded.dividend",
              (ticker, day.isoformat(), dividend))


class TotalReturnMathsTests(unittest.TestCase):
    """income.received_while_held and friends, on a scratch database."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pt_tr_")
        self.db = os.path.join(self.dir, "t.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(self.db))
        self.c = portfolio.connect(self.db)
        self.uid = auth.create_user(self.c, "tess", "pw-123456789")
        self.other = auth.create_user(self.c, "otto", "pw-123456789")

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_imported_dividends_are_what_was_paid(self):
        _snapshot(self.c, self.uid, TODAY, [("SCHD", 50, 4000.0), ("VTI", 10, 2000.0)])
        _txn(self.c, self.uid, TODAY - timedelta(days=90), "DIV", "SCHD", 30.0)
        _txn(self.c, self.uid, TODAY - timedelta(days=90), "REINVEST", "SCHD", -30.0)
        _txn(self.c, self.uid, TODAY - timedelta(days=180), "DIV", "SCHD", 28.5)
        _txn(self.c, self.uid, TODAY - timedelta(days=10), "INTEREST", None, 4.0)  # cash
        _txn(self.c, self.uid, TODAY - timedelta(days=5), "DIV", "SCHD", 99.0, origin=None)
        _txn(self.c, self.other, TODAY - timedelta(days=5), "DIV", "SCHD", 77.0)
        # VTI paid nothing in the history and has no Yahoo payments: no figure
        got = income.received_while_held(self.c, self.uid, ["SCHD", "VTI"], TODAY)
        self.assertEqual(got, {"SCHD": {"amount": 58.5, "source": "brokerage",
                                        "since": (TODAY - timedelta(days=180)).isoformat()}})
        # the reinvested ones counted once, not twice; worked-out rows, cash
        # interest and another account's rows left out

    def test_reinvest_rows_alone_count_what_was_reinvested(self):
        _snapshot(self.c, self.uid, TODAY, [("VOO", 5, 2000.0)])
        _txn(self.c, self.uid, TODAY - timedelta(days=60), "REINVEST", "VOO", -12.25)
        got = income.received_while_held(self.c, self.uid, ["VOO"], TODAY)
        self.assertEqual(got["VOO"]["amount"], 12.25)
        self.assertEqual(got["VOO"]["source"], "brokerage")

    def test_yahoo_dividends_times_the_shares_held_before_each_ex_date(self):
        d1, d2 = TODAY - timedelta(days=200), TODAY - timedelta(days=50)
        _snapshot(self.c, self.uid, d1, [("VTI", 10, 2000.0)])
        _snapshot(self.c, self.uid, d2, [("VTI", 12, 2400.0)])
        _snapshot(self.c, self.uid, TODAY, [("VTI", 12, 2400.0)])
        _bar(self.c, "VTI", d1, 9.0)                          # its first day: not counted
        _bar(self.c, "VTI", TODAY - timedelta(days=300), 9.0)  # before it shows: not counted
        _bar(self.c, "VTI", TODAY - timedelta(days=100), 1.0)  # 10 shares then
        _bar(self.c, "VTI", TODAY - timedelta(days=20), 0.5)   # 12 shares then
        _bar(self.c, "VTI", TODAY + timedelta(days=20), 5.0)   # not paid yet
        _bar(self.c, "VTI", TODAY - timedelta(days=30), 0.0)   # a plain day
        self.c.commit()
        got = income.received_while_held(self.c, self.uid, ["VTI"], TODAY)
        self.assertEqual(got, {"VTI": {"amount": 16.0, "source": "estimated",
                                       "since": d1.isoformat()}})

    def test_a_gap_starts_the_holding_again(self):
        d1, d2 = TODAY - timedelta(days=300), TODAY - timedelta(days=200)
        _snapshot(self.c, self.uid, d1, [("VTI", 10, 2000.0)])
        _snapshot(self.c, self.uid, d2, [("BND", 10, 700.0)])       # sold VTI
        _snapshot(self.c, self.uid, TODAY - timedelta(days=100), [("VTI", 4, 800.0)])
        _snapshot(self.c, self.uid, TODAY, [("VTI", 4, 800.0)])
        _bar(self.c, "VTI", TODAY - timedelta(days=250), 1.0)      # the earlier stretch
        _bar(self.c, "VTI", TODAY - timedelta(days=40), 1.0)
        self.c.commit()
        got = income.received_while_held(self.c, self.uid, ["VTI"], TODAY)
        self.assertEqual(got["VTI"]["amount"], 4.0)

    def test_brokerage_wins_and_pretend_shares_are_left_out(self):
        d1 = TODAY - timedelta(days=200)
        _snapshot(self.c, self.uid, d1, [("VTI", 10, 2000.0), ("BND", 10, 700.0)],
                  source=manual_entry.PCT_SOURCE)
        _snapshot(self.c, self.uid, TODAY, [("VTI", 10, 2000.0), ("BND", 10, 700.0)])
        _bar(self.c, "VTI", TODAY - timedelta(days=100), 1.0)
        _bar(self.c, "BND", TODAY - timedelta(days=100), 1.0)
        _txn(self.c, self.uid, TODAY - timedelta(days=100), "DIV", "BND", 8.0)
        self.c.commit()
        got = income.received_while_held(self.c, self.uid, ["VTI", "BND"], TODAY,
                                         skip_sources=(manual_entry.PCT_SOURCE,))
        # BND from the brokerage; VTI first shows today once the
        # percentages update is left out, so nothing is estimated for it
        self.assertEqual(got, {"BND": {"amount": 8.0, "source": "brokerage",
                                       "since": (TODAY - timedelta(days=100)).isoformat()}})

    def test_neither_means_price_only(self):
        _snapshot(self.c, self.uid, TODAY, [("VTI", 10, 2000.0)])
        self.assertEqual(income.received_while_held(self.c, self.uid, ["VTI"], TODAY), {})
        self.assertEqual(income.received_while_held(self.c, self.uid, [], TODAY), {})
        self.assertIsNone(income.total_return(150.0, 2000.0, 0.0))
        self.assertIsNone(income.total_return(None, None, 25.0))

    def test_total_return_adds_dividends_to_the_price_change(self):
        tr = income.total_return(150.0, 2000.0, 50.0)
        self.assertEqual(tr["usd"], 200.0)
        self.assertAlmostEqual(tr["pct"], 10.0)
        self.assertEqual(tr["dividends"], 50.0)
        # a loss can turn into a gain once dividends count
        self.assertAlmostEqual(income.total_return(-30.0, 1000.0, 40.0)["pct"], 1.0)
        self.assertIsNone(income.total_return(10.0, 0.0, 5.0)["pct"])

    def test_split_between_accounts_by_shares(self):
        positions = [{"symbol": "VTI", "quantity": 30.0}, {"symbol": "VTI", "quantity": 10.0},
                     {"symbol": "BND", "quantity": 5.0}]
        self.assertEqual(income.split_by_holding(positions, {"VTI": {"amount": 100.0}}),
                         [75.0, 25.0, None])

    def test_metrics_with_and_without_dividends(self):
        ctx = {"pos": {"symbol": "VTI", "quantity": 10, "cost_basis": 1000.0,
                       "market_value": 1100.0}, "quote": {}, "dividends": 40.0}
        self.assertEqual(M.value("unrealized_usd", ctx), 100.0)
        self.assertEqual(M.value("total_return_usd", ctx), 140.0)
        self.assertAlmostEqual(M.value("total_return_pct", ctx), 14.0)
        self.assertEqual(M.value("dividends_usd", ctx), 40.0)
        for d in (None, 0.0):
            ctx["dividends"] = d
            self.assertIsNone(M.value("total_return_usd", ctx))
            self.assertIsNone(M.value("total_return_pct", ctx))
        self.assertIsNone(M.value("total_return_usd", {"pos": {"symbol": "X"}, "quote": {}}))

    def test_source_words(self):
        self.assertIn("brokerage", income.source_words(["brokerage"]))
        self.assertIn("estimated", income.source_words(["estimated", "estimated"]))
        both = income.source_words(["brokerage", "estimated"])
        self.assertIn("brokerage", both)
        self.assertIn("estimated", both)

    def test_yearly_income_adds_cash_interest_only(self):
        self.assertEqual(income.yearly_income(120.0, None),
                         {"dividends": 120.0, "interest": 0.0, "total": 120.0})
        self.assertEqual(income.yearly_income(120.0, {"interest": 30.0, "cash_interest": 10.0}),
                         {"dividends": 120.0, "interest": 10.0, "total": 130.0})

    def test_received_says_which_interest_was_on_cash(self):
        _txn(self.c, self.uid, TODAY - timedelta(days=10), "INTEREST", None, 4.0)
        _txn(self.c, self.uid, TODAY - timedelta(days=10), "INTEREST", "T-BILL", 6.0)
        got = income.received(self.c, self.uid, TODAY)
        self.assertEqual((got["interest"], got["cash_interest"]), (10.0, 4.0))


class RetirementMathsTests(unittest.TestCase):
    """plans.withdrawals / months_lasting / retirement_first."""

    def test_withdrawals_at_the_stated_rates(self):
        rows = plans.withdrawals(500_000)
        self.assertEqual([r["rate"] for r in rows], [3.0, 4.0, 5.0])
        self.assertEqual([r["yearly"] for r in rows], [15_000.0, 20_000.0, 25_000.0])
        self.assertAlmostEqual(rows[1]["monthly"], 20_000 / 12)
        self.assertEqual([r["yearly"] for r in plans.withdrawals(None)], [0.0, 0.0, 0.0])

    def test_months_lasting_without_growth_is_simple_division(self):
        self.assertEqual(plans.months_lasting(120_000, 12_000, 0.0), 120)   # ten years
        self.assertEqual(plans.months_lasting(100_000, 30_000, 0.0), 40)    # 3 years 4 months
        self.assertEqual(plans.months_lasting(0, 12_000, 4.0), 0)

    def test_growth_makes_it_last_longer(self):
        flat = plans.months_lasting(500_000, 40_000, 0.0)
        grown = plans.months_lasting(500_000, 40_000, 4.0)
        self.assertEqual(flat, 150)
        self.assertGreater(grown, flat)
        # 8% of the value at 4% growth runs out in roughly 15-20 years
        self.assertTrue(15 * 12 < grown < 20 * 12, grown)

    def test_still_paying_after_the_cap_or_nothing_taken(self):
        # 3% a year with 4% growth: growth covers it
        self.assertIsNone(plans.months_lasting(500_000, 15_000, 4.0))
        self.assertIsNone(plans.months_lasting(500_000, 0, 4.0))
        self.assertEqual(plans.months_lasting(100, 12_000, 0.0, cap_years=1), 0)

    def test_retirement_first(self):
        today = TODAY
        near = {"goal_type": "Retirement", "target_amount": 900_000,
                "target_date": date(2030, 1, 1).isoformat()}
        far = {**near, "target_date": date(2060, 1, 1).isoformat()}
        self.assertTrue(plans.retirement_first(near, {}, today))
        self.assertFalse(plans.retirement_first(far, {}, today))
        self.assertTrue(plans.retirement_first(None, {"goal": "Retirement; Buy a home",
                                                      "time_horizon_years": 6}, today))
        self.assertFalse(plans.retirement_first(None, {"goal": "Retirement",
                                                       "time_horizon_years": 30}, today))
        self.assertTrue(plans.retirement_first(None, {"goal": "Generate income"}, today))
        self.assertTrue(plans.retirement_first(far, {"age_range": "65 or older"}, today))
        self.assertTrue(plans.retirement_first(None, {"contributions": "Withdrawing regularly"},
                                               today))
        # a short horizon for something else, or nothing known: as before
        self.assertFalse(plans.retirement_first(
            {"goal_type": "Buy a home", "target_amount": 60_000,
             "target_date": date(2028, 1, 1).isoformat()}, {"time_horizon_years": 2}, today))
        self.assertFalse(plans.retirement_first(None, None, today))


class PagesTests(unittest.TestCase):
    """Home and Plan drawn for an investor with holdings (imported dividends;
    Yahoo's), with none, and with percentages only."""

    @classmethod
    def setUpClass(cls):
        # The app's first run reloads the repo's modules (codefresh.py): put
        # back the ones other test files imported, so their mocks still work.
        cls.modules = {n: m for n, m in sys.modules.items()
                       if os.path.dirname(os.path.abspath(getattr(m, "__file__", None) or ""))
                       == REPO}
        cls.dir = tempfile.mkdtemp(prefix="pt_tr_app_")
        cls.db = os.path.join(cls.dir, "app.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(cls.db))
        today = date.today()
        c = portfolio.connect(cls.db)
        try:
            # dividends from the imported activity history; nearly retired
            cls.rita = auth.create_user(c, "rita", "pw-123456789")
            sample_data.load(c, cls.rita)
            _txn(c, cls.rita, today - timedelta(days=40), "DIV", "SCHD", 40.0)
            _txn(c, cls.rita, today - timedelta(days=130), "DIV", "SCHD", 38.0)
            _txn(c, cls.rita, today - timedelta(days=20), "INTEREST", None, 12.0)
            advisor.save_profile(c, cls.rita, {"goal": "Retirement", "time_horizon_years": 3,
                                               "age_range": "55-64"})
            # Yahoo's dividends, two holdings updates apart; a long way off
            cls.yuri = auth.create_user(c, "yuri", "pw-123456789")
            _snapshot(c, cls.yuri, today - timedelta(days=200), [("VTI", 10, 1000.0)], cash=50.0)
            _snapshot(c, cls.yuri, today, [("VTI", 10, 1000.0), ("BND", 5, 500.0)], cash=50.0)
            _bar(c, "VTI", today - timedelta(days=100), 1.5)
            _bar(c, "VTI", today - timedelta(days=10), 1.0)
            advisor.save_profile(c, cls.yuri, {"goal": "Retirement", "time_horizon_years": 30})
            # price only: nothing paid that the app knows of
            cls.nora = auth.create_user(c, "nora", "pw-123456789")
            _snapshot(c, cls.nora, today, [("AAPL", 3, 200.0)])
            # nothing in yet
            cls.ned = auth.create_user(c, "ned", "pw-123456789")
            # percentages only
            cls.pat = auth.create_user(c, "pat", "pw-123456789")
            holdings, cash, errors = manual_entry.validate_weights(
                [{"Symbol": "VTI", "Percent": 60, "Type": "ETF"},
                 {"Symbol": "BND", "Percent": 30, "Type": "ETF"}], 10, 10000)
            assert not errors, errors
            meta, rows, totals, errors = manual_entry.build_weights(
                holdings, cash, 10000, {"VTI": {"price": 300.0}, "BND": {"price": 72.0}},
                today=today)
            portfolio.write_snapshot(c, cls.pat, meta, rows, totals, manual_entry.PCT_SOURCE)
            _bar(c, "BND", today - timedelta(days=15), 0.25)
            c.commit()
        finally:
            c.close()

    @classmethod
    def tearDownClass(cls):
        sys.modules.update(cls.modules)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @contextlib.contextmanager
    def _run(self, uid, name, page, **state):
        import yfinance
        from streamlit.testing.v1 import AppTest

        def offline(*a, **k):
            raise RuntimeError("offline in tests")
        at = AppTest.from_file(os.path.join(REPO, "dashboard.py"), default_timeout=120)
        for k, v in {"user_id": uid, "username": name, "page": page, "auto_backfilled": True,
                     "income_synced": True, **state}.items():
            at.session_state[k] = v
        env = {k: v for k, v in os.environ.items()
               if k not in ("FINNHUB_API_KEY", "NORTHWEND_ADMINS")}
        env.update(PORTFOLIO_DB=self.db, MAIL_DRY_RUN="1", ANTHROPIC_API_KEY="sk-test-unused")
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(yfinance, "Ticker", offline), \
                unittest.mock.patch("socket.socket.connect", offline):
            at.run()
            self.assertEqual([e.message for e in at.exception], [])
            yield at
            self.assertEqual([e.message for e in at.exception], [])

    @staticmethod
    def _stats(at):
        body = " ".join(h.proto.body for h in at.get("html")).replace(",<wbr>", ",")
        return dict(re.findall(r"pt-stat-label'>([^<]*)</div><div class='pt-stat-value'>(.*?)</div>",
                               body))

    @staticmethod
    def _holdings(at):
        """The Holdings table (the one with share counts)."""
        return next(d.value for d in at.dataframe if "Qty" in d.value.columns)

    @staticmethod
    def _text(at):
        return " ".join([m.value for m in at.markdown] + [c.value for c in at.caption])

    # ---- Home ---------------------------------------------------------------- #
    def test_home_adds_imported_dividends_to_the_price_change(self):
        with self._run(self.rita, "rita", "Dashboard") as at:
            stats = self._stats(at)
            self.assertNotIn("Total gain/loss", stats)
            # the example portfolio: 26,460 cost, 32,250 value; 78 in SCHD dividends
            self.assertIn("+$5,790.00", stats["Price change"])
            self.assertIn("+$5,868.00", stats["Total return, with dividends"])
            text = self._text(at)
            self.assertIn(r"Total return adds the \$78.00 in dividends", text)
            self.assertIn("from your brokerage's activity history", text)
            cols = list(self._holdings(at).columns)
            self.assertIn("Total return $ (with dividends)", cols)
            table = self._holdings(at).set_index("Symbol")
            # (a column with blanks is shown as text, "—" for the blanks)
            self.assertEqual(table.loc["SCHD", "Total return $ (with dividends)"], "$198.00")
            self.assertEqual(set(table.drop(index="SCHD")["Total return $ (with dividends)"]),
                             {"—"})

    def test_a_holdings_details_show_its_total_return(self):
        with self._run(self.rita, "rita", "Dashboard", holdings_pill="SCHD") as at:
            metrics = {m.label: m for m in at.metric}
            self.assertEqual(metrics["Price change"].value, "$120.00")
            self.assertEqual(metrics["Dividends received"].value, "$78.00")
            self.assertEqual(metrics["Total return, with dividends"].value, "$198.00")
            self.assertTrue(any("activity history" in c.value for c in at.caption))
        with self._run(self.rita, "rita", "Dashboard", holdings_pill="VTI") as at:
            labels = [m.label for m in at.metric]
            self.assertIn("Price change", labels)
            self.assertNotIn("Total return, with dividends", labels)

    def test_home_estimates_from_yahoo_dividends_while_held(self):
        with self._run(self.yuri, "yuri", "Dashboard") as at:
            stats = self._stats(at)
            # 10 shares x (1.50 + 1.00); the price change is 0 (bought at 100)
            self.assertIn("$25.00", stats["Total return, with dividends"])
            self.assertIn("estimated from what each fund paid", self._text(at))

    def test_price_only_when_nothing_is_known(self):
        with self._run(self.nora, "nora", "Dashboard") as at:
            stats = self._stats(at)
            self.assertIn("Total gain/loss", stats)
            self.assertNotIn("Total return, with dividends", stats)
            self.assertNotIn("Price change", stats)
            cols = list(self._holdings(at).columns)
            self.assertNotIn("Total return $ (with dividends)", cols)
            self.assertNotIn("Total return", self._text(at))

    def test_hidden_amounts_hide_the_total_return(self):
        with self._run(self.rita, "rita", "Dashboard", hide_amounts=True) as at:
            stats = self._stats(at)
            self.assertEqual(stats["Total return, with dividends"], "•••")
            text = self._text(at)
            self.assertNotIn("78.00", text)
            self.assertIn("•••", text)

    def test_home_with_no_holdings_and_percentages_only(self):
        with self._run(self.ned, "ned", "Dashboard"):
            pass
        with self._run(self.pat, "pat", "Dashboard") as at:
            stats = self._stats(at)
            self.assertNotIn("Total return, with dividends", stats)

    # ---- Plan ---------------------------------------------------------------- #
    def test_plan_leads_with_retirement_income_when_retirement_is_near(self):
        with self._run(self.rita, "rita", "Plan") as at:
            tabs = [t.label for t in at.tabs]
            self.assertEqual(tabs[0], "Retirement income")
            text = self._text(at)
            self.assertIn("What your portfolio could pay you each year", text)
            self.assertIn("rules of thumb, not a promise", text)
            self.assertIn("Social Security", text)
            self.assertNotIn("you should", text.lower())
            stats = self._stats(at)
            # the example portfolio's 33,340 (with cash): 4% is 1,334 a year
            self.assertEqual(stats["4% a year"], "$1,334")
            self.assertEqual(stats["Interest on cash"], "$12")
            self.assertIn("About a year", stats)
            yearly = at.number_input(key="retire_yearly")
            self.assertEqual(yearly.value, 1300.0)     # about 4%, rounded
            # under 4% a year with 4% growth: still paying at the cap
            self.assertIn("would still be paying after 60 years", text)
            yearly.set_value(33_340.0).run()
            text = self._text(at)
            self.assertIn("would last about", text)
            self.assertIn("With no growth at all, about 1 year.", text)

    def test_plan_shows_it_later_for_a_long_way_off(self):
        with self._run(self.yuri, "yuri", "Plan") as at:
            tabs = [t.label for t in at.tabs]
            self.assertIn("Retirement income", tabs)
            self.assertNotEqual(tabs[0], "Retirement income")

    def test_plan_hidden_amounts(self):
        with self._run(self.rita, "rita", "Plan", hide_amounts=True) as at:
            stats = self._stats(at)
            self.assertEqual(stats["4% a year"], "•••")
            self.assertEqual(stats["About a year"], "•••")
            self.assertNotIn("1,334", self._text(at))
            with self.assertRaises(KeyError):   # no amount to try while hidden
                at.number_input(key="retire_yearly")

    def test_plan_with_no_holdings_and_percentages_only(self):
        with self._run(self.ned, "ned", "Plan") as at:
            self.assertIn("Retirement income", [t.label for t in at.tabs])
            stats = self._stats(at)
            self.assertEqual(stats["4% a year"], "$4,000")   # per $100,000
            self.assertIn("Once your holdings are in", self._text(at))
        with self._run(self.pat, "pat", "Plan", hide_amounts=True) as at:
            stats = self._stats(at)
            self.assertEqual(stats["4% a year"], "$4,000")   # an example, not theirs
            self.assertIn("pretend", self._text(at))
            self.assertIn("Pays about", stats)
            self.assertNotIn("About a year", stats)
            with self.assertRaises(KeyError):
                at.number_input(key="retire_yearly")


if __name__ == "__main__":
    unittest.main()
