"""Calm by default (ROADMAP S6): Income, Activity, Watchlist and Ask Northwend
lead with a short summary for investors, with the detail in a window; Show
everything (Account page) and advisors get the full pages. Runs dashboard.py
with streamlit's AppTest on a scratch database in a temp dir. (AppTest draws
a window when its button is clicked; a real browser check is still worth
doing for the look.)

    python -m unittest tests.test_calm_pages        (from the repo root)
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

import auth  # noqa: E402
import portfolio  # noqa: E402
import prefs  # noqa: E402
import sample_data  # noqa: E402
import two_step  # noqa: E402

WATCHED = ("NVDA", "MSFT", "TSLA", "AMZN", "GOOG", "META", "NFLX")


class CalmPagesTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # The app's first run reloads the repo's modules (codefresh.py). Put
        # back the ones other test files imported afterwards, so the mocks they
        # set on them (mailer.send, ...) still reach the code they test.
        cls.modules = {n: m for n, m in sys.modules.items()
                       if os.path.dirname(os.path.abspath(getattr(m, "__file__", None) or "")) == REPO}
        cls.dir = tempfile.mkdtemp(prefix="pt_calm_")
        cls.db = os.path.join(cls.dir, "app.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(cls.db))
        c = portfolio.connect(cls.db)
        try:
            cls.alice = auth.create_user(c, "alice", "pw-123456789")   # calm view
            cls.erin = auth.create_user(c, "erin", "pw-123456789")     # Show everything
            prefs.save(c, cls.erin, {"show_everything": True})
            cls.carol = auth.create_user(c, "carol", "pw-123456789")   # an advisor
            auth.set_advisor(c, "carol", True)
            secret = two_step.new_secret()
            two_step.enable(c, cls.carol, secret, two_step.totp(secret))
            cls.carol_ok = f"{cls.carol}:{two_step.status(c, cls.carol)['stamp']}"
            cls.dave = auth.create_user(c, "dave", "pw-123456789")     # her client
            auth.link_client(c, cls.carol, cls.dave)
            today = date.today()
            for uid in (cls.alice, cls.erin, cls.dave):
                sample_data.load(c, uid)
                for i, (act, sym, qty, amount, gain, origin) in enumerate([
                        ("BUY", "VTI", 5, -1500.0, None, None),
                        ("SELL", "AAPL", 3, 690.0, 215.0, None),
                        ("DEPOSIT", None, None, 2000.0, None, "imported"),
                        ("DIV", "SCHD", None, 25.5, None, "imported"),
                        ("BUY", "BND", 20, -1460.0, None, None),
                        ("WITHDRAWAL", None, None, -300.0, None, "imported"),
                        ("BUY", "VOO", 1, -550.0, None, None)]):
                    c.execute("INSERT INTO transactions (user_id, account, trade_date, action, "
                              "symbol, quantity, amount, realized_gain, origin) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (uid, "Brokerage", (today - timedelta(days=10 * i)).isoformat(),
                               act, sym, qty, amount, gain, origin))
                for t in WATCHED:
                    c.execute("INSERT INTO watchlist (user_id, ticker) VALUES (?, ?)", (uid, t))
            c.execute("UPDATE positions SET div_yield_pct = 1.5 WHERE symbol IN ('VTI', 'SCHD')")
            for n, t in enumerate(WATCHED):   # NVDA falls most, NFLX rises most
                c.execute("INSERT INTO price_history (ticker, price, prev_close, change, "
                          "pct_change, ok) VALUES (?, ?, 100, ?, ?, 1)",
                          (t, 100 + n, n - 3, (n - 3) * 1.1))
            for t in ("VTI", "VXUS", "BND", "AAPL", "VOO", "SCHD"):
                for m in (2, 5, 8, 11):
                    c.execute("INSERT INTO daily_bars (ticker, date, close, dividend) "
                              "VALUES (?, ?, 100, 0.5)",
                              (t, (today - timedelta(days=30 * m)).isoformat()))
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
        # no prices from the internet: Yahoo and any socket fail at once
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(yfinance, "Ticker", offline), \
                unittest.mock.patch("socket.socket.connect", offline):
            at.run()
            self.assertEqual([e.message for e in at.exception], [])
            yield at
            self.assertEqual([e.message for e in at.exception], [])

    def _advisor(self, page):
        return self._run(self.carol, "carol", page, active_user_id=self.dave,
                         two_step_ok=self.carol_ok)

    @staticmethod
    def _keys(at):
        return [b.key for b in at.button if b.key]

    @staticmethod
    def _stats(at):
        # (the line-break chances _stat_row puts after thousands commas left out)
        body = " ".join(h.proto.body for h in at.get("html")).replace(",<wbr>", ",")
        return dict(re.findall(r"pt-stat-label'>([^<]*)</div><div class='pt-stat-value'>(.*?)</div>",
                               body)), body

    def test_income(self):
        with self._run(self.alice, "alice", "Income") as at:
            stats, _ = self._stats(at)
            self.assertEqual(list(stats), ["Expected, next 12 months", "Next payment",
                                           "Received, last 12 months"])
            self.assertEqual(stats["Received, last 12 months"], "$25.50")
            self.assertEqual(len(at.dataframe), 0)
            self.assertEqual(len(at.metric), 0)
            for k in ("next_income", "tile_income_months", "tile_income_received",
                      "tile_income_holdings"):
                self.assertIn(k, self._keys(at))
            # the tile's two amounts stay amounts: a bare "$...$" is math in markdown
            paid = [c.value for c in at.caption if c.value.startswith("Dividends")]
            self.assertEqual(paid, ["Dividends \\$25.50 · interest \\$0.00"])
            at.button(key="tile_income_holdings").click().run()
            self.assertEqual(len(at.dataframe), 1)
            self.assertEqual(len(at.get("download_button")), 1)
            self.assertTrue(at.session_state["dialog_open"])   # live prices wait
        with self._run(self.alice, "alice", "Income", hide_amounts=True) as at:
            stats, _ = self._stats(at)
            self.assertEqual(stats["Expected, next 12 months"], "•••")
            self.assertEqual(stats["Received, last 12 months"], "•••")
        for view in (self._run(self.erin, "erin", "Income"), self._advisor("Income")):
            with view as at:
                self.assertEqual([s.value for s in at.subheader],
                                 ["Received, last 12 months", "Next 12 months"])
                self.assertEqual(len(at.dataframe), 1)
                self.assertNotIn("tile_income_months", self._keys(at))

    def test_activity(self):
        with self._run(self.alice, "alice", "Activity") as at:
            stats, body = self._stats(at)
            self.assertEqual(stats["Money added, last 12 months"], "+$1,700.00")
            self.assertEqual(stats["Bought"], "$3,510.00")
            self.assertEqual(stats["Sold"], "$690.00")
            self.assertIn("Buy 5 VTI", body)
            self.assertNotIn("Buy 1 VOO", body)          # only the latest five
            self.assertEqual(len(at.dataframe), 0)
            self.assertEqual(at.button(key="activity_open").label, "See all activity (7)")
            at.button(key="activity_open").click().run()
            table = at.dataframe[0].value
            self.assertEqual(len(table.data if hasattr(table, "data") else table), 7)
            self.assertEqual(len(at.get("download_button")), 1)
        for view in (self._run(self.erin, "erin", "Activity"), self._advisor("Activity")):
            with view as at:
                self.assertEqual(len(at.dataframe), 1)
                self.assertNotIn("activity_open", self._keys(at))

    def test_watchlist(self):
        with self._run(self.alice, "alice", "Watchlist") as at:
            stats, _ = self._stats(at)
            self.assertEqual((stats["Watching"], stats["Biggest rise today"],
                              stats["Biggest fall today"]), ("7", "NFLX", "NVDA"))
            rows = [k[8:] for k in self._keys(at) if k.startswith("wl_open_")]
            self.assertEqual(rows, ["NFLX", "NVDA", "META", "MSFT", "GOOG"])   # biggest moves
            at.button(key="watch_open").click().run()
            self.assertEqual([k[10:] for k in self._keys(at) if k.startswith("wl_w_open_")],
                             sorted(WATCHED))
            # picking one in the window closes it, and its chart opens on the page
            at.button(key="wl_w_open_TSLA").click().run()
            self.assertEqual(at.session_state["watchlist_pill"], "TSLA")
            self.assertFalse(at.session_state["dialog_open"])
            self.assertIn("## TSLA", [m.value for m in at.markdown])
            # a watched ticker has no shares: today's move per share, not "—"
            price = next(m for m in at.metric if m.label == "Price")
            self.assertEqual(price.proto.delta, "-1.00 (-1.10%) today")
        for view in (self._run(self.erin, "erin", "Watchlist"), self._advisor("Watchlist")):
            with view as at:
                self.assertEqual([k[8:] for k in self._keys(at) if k.startswith("wl_open_")],
                                 sorted(WATCHED))
                self.assertNotIn("watch_open", self._keys(at))

    def test_ask_northwend(self):
        with self._run(self.alice, "alice", "AI Assistant") as at:
            keys = self._keys(at)
            self.assertTrue(any(k.startswith("quick_") for k in keys))
            self.assertIn("next_assistant", keys)         # the profile's questions
            self.assertFalse(any(k.startswith("FormSubmitter:investor_profile_form") for k in keys))
            self.assertFalse(any("investing profile" in e.label or "Client plan" in e.label
                                 for e in at.expander))
            at.button(key="assist_profile_open").click().run()
            self.assertTrue(any(k.startswith("FormSubmitter:investor_profile_form")
                                for k in self._keys(at)))
            at.button(key="assist_plan_open").click().run()
            self.assertIn("Create plan", [b.label for b in at.button])
        for view in (self._run(self.erin, "erin", "AI Assistant"), self._advisor("AI Assistant")):
            with view as at:
                labels = [e.label for e in at.expander]
                self.assertTrue(any(lb.startswith("Your investing profile") for lb in labels))
                self.assertIn("Client plan (PDF)", labels)
                self.assertNotIn("assist_profile_open", self._keys(at))

    def test_screen_readers(self):
        """The summaries' custom HTML (ROADMAP Polish: Accessibility): stat
        boxes are a list of label + value items, the latest moves a list of
        date, move and amount, gains said in words as well as color, the
        watchlist's numbers named; ui_enhancements.js keeps icon names out of
        buttons' names."""
        def lists(body, label):
            return re.findall(rf"role='list' aria-label='{label}'.*?(?=role='list'|$)", body, re.S)
        with self._run(self.alice, "alice", "Activity") as at:
            _, body = self._stats(at)
            summary = lists(body, "Summary")
            self.assertEqual(len(summary), 1)
            self.assertEqual(summary[0].count("class='pt-stat' role='listitem'"), 3)
            moves = lists(body, "Latest moves")
            self.assertEqual(len(moves), 1)
            self.assertEqual(moves[0].count("role='listitem'"), 5)
            today = date.today().isoformat()
            self.assertIn(f"datetime='{today}'>", moves[0])        # each move's date ...
            self.assertIn("Buy 5 VTI", moves[0])                     # ... what it was ...
            self.assertIn("-$1,500.00", moves[0])                    # ... and its amount, signed
            self.assertIn("+$215.00 gain<", body)                    # not only green
        with self._run(self.alice, "alice", "Activity", hide_amounts=True) as at:
            _, body = self._stats(at)
            self.assertIn("gain/loss", body)                         # hidden: no direction given
            self.assertNotIn(" gain<", body)
        with self._run(self.alice, "alice", "Watchlist") as at:
            _, body = self._stats(at)
            self.assertEqual(lists(body, "Summary")[0].count("role='listitem'"), 3)
            self.assertIn("<span class='pt-sr'>Price </span><b>106.00</b>", body)
            self.assertIn("+3.00 (+3.30%)</span><span class='pt-sr'> today</span>", body)
            self.assertIn("-3.00 (-3.30%)", body)                    # a sign, not only red
            at.button(key="watch_open").click().run()
            removes = [k for k in self._keys(at) if k.startswith(("wl_del_", "wl_w_del_"))]
            self.assertIn("wl_w_del_TSLA", removes)                  # the window's copies too
            self.assertEqual(at.button(key="wl_w_del_TSLA").help,
                             "Remove TSLA from your watchlist")
        with self._run(self.alice, "alice", "AI Assistant") as at:
            _, body = self._stats(at)
            self.assertIn("Next step<span class='pt-sr'>:</span>", body)
            self.assertRegex(body, r"\(\d+ of \d+ done\)")           # not "0/8"
        with open(os.path.join(REPO, "ui_enhancements.js"), encoding="utf-8") as fh:
            js = fh.read()
        # icon-only buttons named, here and in the watchlist window ...
        rule = re.search(r"\[/(\^st-key-wl_.*?)/,", js).group(1)
        for key in removes:
            self.assertRegex(f"st-key-{key}", rule)
        # ... the profile button's count said in words, and icons beside
        # words left out of names ("open_in_new See all 7" reads "See all 7")
        self.assertIn("st-key-assist_profile_open", js)
        self.assertIn('[data-testid="stIconMaterial"]', js)
        self.assertIn('setAttribute("aria-hidden", "true")', js)

    def test_levels_unsigned_changes_signed(self):
        """A level - a yield, a share of the portfolio - reads "1.23%"; a
        change - a gain, a day's move - keeps its sign. Hidden amounts mask
        both."""
        level = r"^\d+\.\d\d%$"
        with self._run(self.erin, "erin", "Income") as at:
            self.assertRegex(next(m.value for m in at.metric if m.label == "Yield on holdings"),
                             level)
            table = at.dataframe[0].value
            yields = [v for v in table["Div Yield %"] if v != "—"]
            self.assertTrue(yields)
            for v in yields:
                self.assertRegex(v, level)
        with self._run(self.erin, "erin", "Income", hide_amounts=True) as at:
            self.assertEqual(next(m.value for m in at.metric if m.label == "Yield on holdings"),
                             "•••")
        with self._run(self.erin, "erin", "Dashboard", holdings_pill="VTI") as at:
            self.assertRegex(next(m.value for m in at.metric if m.label == "% of Portfolio"),
                             level)
            self.assertRegex(next(m.proto.delta for m in at.metric if m.label == "Price change"),
                             r"^[+-]\d+\.\d\d%$")
            _, body = self._stats(at)
            # Home's gain %: a change, signed (the price change, with the
            # dividends imported for SCHD a total return beside it)
            self.assertRegex(body, r"Price change</div>.*?pt-stat-sub'><span class='pt-up'>"
                                   r"\+\d+\.\d\d%")
            self.assertRegex(body, r"Total return, with dividends</div>.*?pt-stat-sub'>"
                                   r"<span class='pt-up'>\+\d+\.\d\d%")

    def test_stat_rows_are_lists(self):
        """Every row of stat boxes reads to a screen reader as a named list,
        one item per box (Home, Plan, Learn, Your clients and the calm
        summaries), and a long amount can wrap at its thousands commas."""
        for (uid, name, page, state), label in (
                ((self.alice, "alice", "Dashboard", {}), "Portfolio summary"),
                ((self.alice, "alice", "Plan", {}), "Money in and growth"),
                ((self.carol, "carol", "Clients", {"two_step_ok": self.carol_ok}),
                 "Client summary")):
            with self._run(uid, name, page, **state) as at:
                body = " ".join(h.proto.body for h in at.get("html"))
                self.assertIn(f"<div class='pt-stats' role='list' aria-label='{label}'", body)
                row = body.split(f"aria-label='{label}'", 1)[1].split("role='list'", 1)[0]
                # Home: four, with the dividends imported for SCHD (the price
                # change, the total return with dividends, holdings, cash)
                self.assertEqual(row.count("<div class='pt-stat' role='listitem'>"),
                                 4 if page == "Dashboard" else 3, label)
                if page == "Dashboard":
                    self.assertIn("$32,<wbr>250.00", row)       # wraps at a comma on a phone
        # the rows built by hand in the code, Learn's among them
        for path in ["dashboard.py"] + [os.path.join("views", f) for f in
                                        sorted(os.listdir(os.path.join(REPO, "views")))
                                        if f.endswith(".py")]:
            with open(os.path.join(REPO, path), encoding="utf-8") as fh:
                src = fh.read()
            for m in re.finditer(r"class='pt-stats'([^>]*)>", src):
                self.assertRegex(m.group(1), r"^ role='list' aria-label='[^']+'", path)
            for m in re.finditer(r"class='pt-stat'([^>]*)>", src):
                self.assertEqual(m.group(1), " role='listitem'", path)

    def test_show_everything_switch(self):
        with self._run(self.alice, "alice", "Account") as at:
            switch = at.toggle(key="acct_show_all")
            self.assertFalse(switch.value)
            switch.set_value(True).run()
            c = portfolio.connect(self.db)
            try:
                self.assertTrue(prefs.load(c, self.alice)["show_everything"])
                prefs.save(c, self.alice, {})
            finally:
                c.close()
        with self._run(self.carol, "carol", "Account", two_step_ok=self.carol_ok) as at:
            self.assertNotIn("acct_show_all", [t.key for t in at.toggle])


if __name__ == "__main__":
    unittest.main()
