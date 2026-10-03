"""The beginner path: someone who isn't investing yet. Home is their route
(not an import form), the example portfolio isn't an opened account, a goal
typed in first steps is kept, a goal with nothing invested is "Starting
out" (never "Behind"), and Learn's waypoints answer and tick in place. Runs
dashboard.py with streamlit's AppTest on a scratch database in a temp dir,
plus the pure pieces (plans, route, learn).

    python -m unittest tests.test_beginner_path        (from the repo root)
"""

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import advisor  # noqa: E402
import auth  # noqa: E402
import learn  # noqa: E402
import plans  # noqa: E402
import portfolio  # noqa: E402
import prefs  # noqa: E402
import route  # noqa: E402
import sample_data  # noqa: E402
import two_step  # noqa: E402

PROFILE = {"goal": "Build long-term wealth", "time_horizon_years": 10,
           "risk_tolerance": "conservative", "drawdown_reaction": "Sell some",
           "experience": "new", "age_range": "Under 25", "income_stability": "Very stable",
           "emergency_fund": "Under 3 months"}
READY = {**PROFILE, "high_interest_debt": "None", "employer_match": "No match or no plan"}
STEPS_DONE = {"first_steps": {"done": True}}
GOAL = {"goal_type": "Build long-term wealth", "target_amount": 20000.0,
        "target_date": "2036-10-01", "monthly_contribution": 100.0}


class BeginnerPathTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # the app's first run reloads the repo's modules (codefresh.py): put
        # back the ones other test files imported, so their mocks still reach
        cls.modules = {n: m for n, m in sys.modules.items()
                       if os.path.dirname(os.path.abspath(getattr(m, "__file__", None) or "")) == REPO}
        cls.dir = tempfile.mkdtemp(prefix="pt_beginner_")
        cls.db = os.path.join(cls.dir, "app.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(cls.db))
        c = portfolio.connect(cls.db)
        try:
            # nothing brought in, profile answered, a goal set
            cls.nina = auth.create_user(c, "nina", "pw-123456789")
            advisor.save_profile(c, cls.nina, PROFILE)
            prefs.save(c, cls.nina, STEPS_DONE)
            plans.save_plan(c, cls.nina, GOAL, set_by=cls.nina)
            # only the example portfolio
            cls.ezra = auth.create_user(c, "ezra", "pw-123456789")
            advisor.save_profile(c, cls.ezra, PROFILE)
            prefs.save(c, cls.ezra, STEPS_DONE)
            sample_data.load(c, cls.ezra)
            # on the first steps' goal screen
            cls.gina = auth.create_user(c, "gina", "pw-123456789")
            advisor.save_profile(c, cls.gina, PROFILE)
            prefs.save(c, cls.gina, {"first_steps": {"step": 5}})
            # an advisor and her empty client
            cls.carol = auth.create_user(c, "carol", "pw-123456789")
            auth.set_advisor(c, "carol", True)
            secret = two_step.new_secret()
            two_step.enable(c, cls.carol, secret, two_step.totp(secret))
            cls.carol_ok = f"{cls.carol}:{two_step.status(c, cls.carol)['stamp']}"
            cls.dana = auth.create_user(c, "dana", "pw-123456789")
            auth.link_client(c, cls.carol, cls.dana)
            # Learn's route (the reworked waypoints): every question answered,
            # no goal yet
            cls.gary = auth.create_user(c, "gary", "pw-123456789")
            advisor.save_profile(c, cls.gary, READY)
            prefs.save(c, cls.gary, STEPS_DONE)
            cls.fay = auth.create_user(c, "fay", "pw-123456789")
            advisor.save_profile(c, cls.fay, READY)
            prefs.save(c, cls.fay, STEPS_DONE)
            # a goal set before Set a goal was walked in Learn, compass shown
            cls.olga = auth.create_user(c, "olga", "pw-123456789")
            advisor.save_profile(c, cls.olga, READY)
            prefs.save(c, cls.olga, {**STEPS_DONE, "gear_seen": ["map", "compass"]})
            plans.save_plan(c, cls.olga, GOAL, set_by=cls.olga)
            # the same, without the compass: the goal's parts are still to walk
            cls.walt = auth.create_user(c, "walt", "pw-123456789")
            advisor.save_profile(c, cls.walt, READY)
            prefs.save(c, cls.walt, STEPS_DONE)
            plans.save_plan(c, cls.walt, GOAL, set_by=cls.walt)
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
        for k, v in {"user_id": uid, "username": name, "page": page, **state}.items():
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

    @staticmethod
    def _keys(at):
        return [b.key for b in at.button if b.key]

    @staticmethod
    def _html(at):
        return " ".join(h.proto.body for h in at.get("html"))

    @staticmethod
    def _md(at):
        return " ".join(m.value for m in at.markdown)

    # ---- Home with nothing invested --------------------------------------- #
    def test_home_without_holdings_is_the_route(self):
        with self._run(self.nina, "nina", "Dashboard") as at:
            keys = self._keys(at)
            # Your route and the next step, not an import form
            self.assertIn("route_go", keys)
            self.assertIn("Your route", self._html(at))
            # new to investing: the route starts in Learn, and says so
            self.assertIn("You're in <b>Learn · step 2 of 6</b>", self._html(at))
            self.assertIn("Next: Are you ready to invest?", self._md(at))
            # calm ways to look around, or bring in what they own
            for k in ("start_practice", "start_example", "start_bring"):
                self.assertIn(k, keys)
            self.assertEqual(len(at.get("file_uploader")), 0)
            self.assertNotIn("no data yet", self._md(at))
            # the direction, one line
            self.assertIn("start_direction", keys)
            # "I already invest" shows the existing ways in
            at.button(key="start_bring").click().run()
            self.assertIn("start_manual", self._keys(at))
            self.assertIn("start_import", self._keys(at))

    def test_more_pages_without_holdings(self):
        for page, words in (("Income", "the dividends it pays"),
                            ("Activity", "your buys and sells")):
            with self._run(self.nina, "nina", page) as at:
                self.assertIn(words, self._md(at))
                self.assertEqual(len(at.get("file_uploader")), 0)
        # the watchlist works before anything is brought in (Learn's example funds)
        c = portfolio.connect(self.db)
        try:
            c.execute("INSERT INTO watchlist (user_id, ticker) VALUES (?, 'VTI')", (self.nina,))
            c.commit()
        finally:
            c.close()
        with self._run(self.nina, "nina", "Watchlist") as at:
            self.assertTrue(any(k.startswith("wl_open_VTI") for k in self._keys(at)))

    def test_advisor_sees_an_empty_client_in_her_own_words(self):
        with self._run(self.carol, "carol", "Dashboard", active_user_id=self.dana,
                       two_step_ok=self.carol_ok) as at:
            self.assertIn("Bring dana's statements in", self._md(at))
            self.assertIn("onboard_paste", self._keys(at))
            self.assertNotIn("start_practice", self._keys(at))

    # ---- the example portfolio isn't an opened account ---------------------- #
    def test_example_portfolio_is_not_an_opened_account(self):
        with self._run(self.ezra, "ezra", "Get started", gs_at="account") as at:
            # Open your account: its two ticks complete it, saved as they're ticked
            self.assertTrue(at.button(key="gs_complete").disabled)
            at.checkbox(key="gs_acct_opened").check().run()
            at.checkbox(key="gs_acct_funded").check().run()
            self.assertFalse(at.button(key="gs_complete").disabled)
            at.button(key="gs_complete").click().run()
            self.assertEqual(at.session_state["gs_at"], "first")
        c = portfolio.connect(self.db)
        try:
            self.assertEqual(prefs.load(c, self.ezra)["account_steps"], ["opened", "funded"])
        finally:
            c.close()
        with self._run(self.ezra, "ezra", "Get started", gs_at="bring") as at:
            keys = self._keys(at)
            self.assertNotIn("gs_open_dash", keys)    # (the reached state)
            self.assertIn("gs_import", keys)          # Bring it in: the paste window
            self.assertIn("Start investing · step 4 of 4 · 2 complete", self._html(at))

    def test_learn_keeps_its_place_with_only_the_example(self):
        with self._run(self.ezra, "ezra", None) as at:
            # lands on Learn, and the menu keeps one order before and after holdings
            self.assertEqual(at.session_state["page"], "Get started")
            tabs = [b.key for b in at.button if (b.key or "").startswith("tab_")]
            self.assertEqual(tabs, ["tab_Dashboard", "tab_Plan", "tab_Get started",
                                    "tab_AI Assistant", "tab_Money"])

    # ---- first steps ---------------------------------------------------------- #
    def test_goal_typed_in_first_steps_is_kept(self):
        with self._run(self.gina, "gina", "Get started") as at:
            # the profile's goal is picked for them, and a tap can't un-pick it
            self.assertEqual(at.session_state["fs_goal_type"], "Build long-term wealth")
            at.number_input(key="fs_goal_target").set_value(20000.0)
            at.number_input(key="fs_goal_monthly").set_value(100.0)
            at.button(key="fs_next").click().run()
        c = portfolio.connect(self.db)
        try:
            row = plans.get_plan(c, self.gina)
        finally:
            c.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["goal_type"], "Build long-term wealth")
        self.assertEqual(row["target_amount"], 20000.0)

    def test_new_investor_is_led_into_learn(self):
        c = portfolio.connect(self.db)
        try:
            prefs.save(c, self.gina, {"first_steps": {"step": 7}})   # the last screen
        finally:
            c.close()
        with self._run(self.gina, "gina", "Get started") as at:
            self.assertEqual(at.button(key="fs_no_account").proto.type, "primary")
            self.assertNotEqual(at.button(key="fs_manual").proto.type, "primary")
            at.button(key="fs_no_account").click().run()
            # their route starts in Learn, at its first step not complete
            self.assertEqual(at.session_state["page"], "Get started")
            self.assertEqual(at.session_state["gs_at"], "ready")
            self.assertEqual(at.session_state["gs_stage"], "learn")
            self.assertIn("Learn · step 2 of 6", self._html(at))

    # ---- Plan with nothing invested ------------------------------------------- #
    def test_plan_with_nothing_invested_is_starting_out(self):
        with self._run(self.nina, "nina", "Plan") as at:
            body = self._html(at)
            self.assertIn("Starting out", body)
            self.assertIn("a month gets you there", body)
            self.assertNotIn(">Behind<", body)

    # ---- Learn's waypoint 2 answers in place; Ask's suggestions ---------------- #
    def test_readiness_questions_answer_in_the_waypoint(self):
        with self._run(self.nina, "nina", "Get started", gs_at="ready") as at:
            pills = [p.key for p in at.get("button_group")]
            self.assertIn("fs_high_interest_debt", pills)
            self.assertIn("fs_employer_match", pills)
            self.assertNotIn("in your profile", self._md(at))

    def test_practice_prices_that_dont_load_say_so(self):
        import sync_history
        with unittest.mock.patch.object(sync_history, "sync", lambda *a, **k: None), \
                self._run(self.nina, "nina", "Get started", gs_at="practice") as at:
            at.button(key="gs_load_prices").click().run()
            self.assertIn("Couldn't load past prices right now - try again later.",
                          " ".join(i.value for i in at.info))

    def test_ask_offers_beginner_questions_without_holdings(self):
        with self._run(self.nina, "nina", "AI Assistant") as at:
            labels = [b.label for b in at.button]
            self.assertIn("What should I do before I invest?", labels)
            self.assertNotIn("Review my portfolio", labels)
            # the profile count includes the two readiness questions still open
            self.assertTrue(any("(8/10)" in b for b in labels), labels)

    # ---- Learn's route: Complete this step, Skip for now, Set a goal's parts ---- #
    def _prefs(self, uid):
        c = portfolio.connect(self.db)
        try:
            return prefs.load(c, uid)
        finally:
            c.close()

    def _plan(self, uid):
        c = portfolio.connect(self.db)
        try:
            return plans.get_plan(c, uid)
        finally:
            c.close()

    def _set_prefs(self, uid, **changes):
        c = portfolio.connect(self.db)
        try:
            prefs.save(c, uid, {**prefs.load(c, uid), **changes})
        finally:
            c.close()

    def test_complete_marks_the_step_and_moves_on_skip_does_not(self):
        self._set_prefs(self.olga, get_started_done=[])
        with self._run(self.olga, "olga", "Get started", gs_at="basics") as at:
            keys = self._keys(at)
            # one big button; no Mark as done, no next-waypoint button
            self.assertIn("gs_complete", keys)
            self.assertEqual(at.button(key="gs_complete").label, "Complete this step ✓")
            self.assertEqual(at.button(key="gs_complete").proto.type, "primary")
            for gone in ("done_basics", "undone_basics", "gs_next"):
                self.assertNotIn(gone, keys)
            self.assertIn("Learn · step 4 of 6", self._html(at))
            self.assertIn("progressbar", self._html(at))   # the bar, for screen readers too
            # Skip for now: on to An example mix, basics still open
            at.button(key="gs_skip").click().run()
            self.assertEqual(at.session_state["gs_at"], "mix")
            self.assertNotIn("basics", self._prefs(self.olga).get("get_started_done") or [])
            # Complete this step: marked, and on to the next one not complete
            at.button(key="gs_complete").click().run()
            self.assertEqual(self._prefs(self.olga)["get_started_done"], ["mix"])
            self.assertEqual(at.session_state["gs_at"], "practice")
            self.assertIn("Learn · step 6 of 6 · 4 complete", self._html(at))
            # Back is there, small
            self.assertEqual(at.button(key="gs_prev").proto.type, "tertiary")

    def test_real_world_steps_complete_by_their_answers(self):
        with self._run(self.gina, "gina", "Get started", fs_hide=True, gs_at="ready") as at:
            # questions still open: the button waits for them, Skip is there
            self.assertTrue(at.button(key="gs_complete").disabled)
            self.assertIn("gs_skip", self._keys(at))
        with self._run(self.olga, "olga", "Get started", gs_at="ready") as at:
            self.assertFalse(at.button(key="gs_complete").disabled)
            at.button(key="gs_complete").click().run()
            self.assertNotEqual(at.session_state["gs_at"], "ready")

    def test_goal_parts_save_a_plan_without_leaving_learn(self):
        today = date.today()
        with self._run(self.gary, "gary", "Get started", gs_at="goal") as at:
            self.assertNotIn("gs_set_goal", self._keys(at))     # no trip to Plan
            self.assertIn("Part 1 of 4", " ".join(c.value for c in at.caption))
            # part 1: the date starts at the suggestion from their timeline
            tip = learn.suggestions(READY, None, today=today)
            self.assertEqual(at.session_state["gs_goal_date"], tip["goal_date"])
            at.number_input(key="gs_goal_target").set_value(30000.0)
            at.button(key="gs_goal_save").click().run()
            self.assertEqual(at.session_state["page"], "Get started")
            plan = self._plan(self.gary)
            self.assertEqual((plan["target_amount"], plan["target_date"]),
                             (30000.0, tip["goal_date"].isoformat()))
            # part 2: the monthly amount that reaches it, pre-filled
            tip = learn.suggestions(READY, plan, today=today)
            self.assertEqual(at.session_state["gs_goal_monthly"], tip["monthly"])
            self.assertIn(f"About **${tip['monthly']:,.0f} a month**".replace("$", r"\$"),
                          self._md(at))
            at.number_input(key="gs_goal_monthly").set_value(150.0)
            at.button(key="gs_goal_save").click().run()
            self.assertEqual(self._plan(self.gary)["monthly_contribution"], 150.0)
            # part 3: how it's going, then part 4: the target mix
            self.assertEqual(at.session_state["plan_return"], learn.SUGGESTED_RETURN_PCT)
            at.button(key="gs_goal_save").click().run()
            self.assertEqual(at.session_state["gs_goal_stocks"], tip["stocks_pct"])
            at.button(key="gs_goal_save").click().run()
            self.assertEqual(at.session_state["page"], "Get started")
            self.assertEqual(at.session_state["gs_at"], "basics")
        self.assertEqual(self._plan(self.gary)["target_alloc"],
                         {"Stocks": float(tip["stocks_pct"]),
                          "Bonds": float(100 - tip["stocks_pct"])})
        self.assertIn("goal", self._prefs(self.gary)["get_started_done"])

    def test_a_goal_from_before_the_walk_keeps_its_compass(self):
        # olga's compass was shown: Set a goal stays complete. walt's wasn't:
        # its parts open at the monthly amount (the goal itself is there)
        with self._run(self.olga, "olga", "Get started", gs_at="goal") as at:
            self.assertIn("Learn · step 3 of 6 · 3 complete", self._html(at))
        with self._run(self.walt, "walt", "Get started", gs_at="goal") as at:
            self.assertIn("Learn · step 3 of 6 · 2 complete", self._html(at))
            self.assertIn("Part 2 of 4", " ".join(c.value for c in at.caption))
            self.assertIn("gs_goal_save", self._keys(at))

    def test_suggestions_match_the_helper(self):
        today = date.today()
        tip = learn.suggestions(PROFILE, GOAL, today=today)
        with self._run(self.nina, "nina", "Plan") as at:
            # What if: pre-filled from the plan, with the suggestion offered
            self.assertEqual(at.session_state["wi_years"], tip["years"])
            self.assertIn(f"${tip['monthly']:,.0f} a month · {tip['years']} years · "
                          f"{tip['stocks_pct']}% in stocks", " ".join(c.value for c in at.caption)
                          .replace("\\$", "$"))
            at.slider(key="wi_years").set_value(30).run()
            at.button(key="wi_use_tip").click().run()
            self.assertEqual(at.session_state["wi_years"], tip["years"])
            self.assertEqual(at.session_state["wi_monthly"], tip["monthly"])
            self.assertEqual(at.session_state["wi_stocks"], tip["stocks_pct"])
        # the target mix: the example mix for their answers
        mix = learn.suggestions(PROFILE, None, today=today)["target_mix"]
        with self._run(self.ezra, "ezra", "Plan") as at:
            at.button(key="plan_target_use").click().run()
            self.assertEqual(at.session_state["plan_target_Stocks"], mix["Stocks"])
            self.assertEqual(at.session_state["plan_target_Bonds"], mix["Bonds"])

    def test_plan_has_a_way_back_to_the_route(self):
        with self._run(self.nina, "nina", "Plan") as at:
            self.assertEqual(at.button(key="plan_back_route").label,
                             ":material/arrow_back: Back to your route")
            at.button(key="plan_back_route").click().run()
            self.assertEqual(at.session_state["page"], "Get started")
        # an advisor's own portfolio isn't on a route
        with self._run(self.carol, "carol", "Plan", two_step_ok=self.carol_ok) as at:
            self.assertNotIn("plan_back_route", self._keys(at))

    def test_advisor_and_client_see_learn(self):
        # the advisor in a client's Learn: the same route, nothing breaks -
        # it opens on their one waypoint, their statements brought in
        with self._run(self.carol, "carol", "Get started", active_user_id=self.dana,
                       two_step_ok=self.carol_ok) as at:
            self.assertIn("gs_import", self._keys(at))
            self.assertIn("Start investing · step 1 of 1", self._html(at))
        with self._run(self.carol, "carol", "Get started", active_user_id=self.dana,
                       two_step_ok=self.carol_ok, gs_at="profile") as at:
            self.assertIn("gs_complete", self._keys(at))
            self.assertIn("Learn (optional for you) · step 1 of 3", self._html(at))
        # the client herself (client mode): Learn is the reads only - her
        # advisor sets the goal, and no example mix or practice money
        with self._run(self.dana, "dana", "Get started", gs_at="goal", fs_hide=True) as at:
            self.assertNotIn("gs_goal_save", self._keys(at))
            self.assertEqual(at.session_state["gs_at"], "bring")   # goal isn't on it
            at.segmented_control(key="gs_stage").set_value("learn").run()
            self.assertEqual(at.pills(key="gs_pick").options,
                             ["1. About you", "2. Are you ready to invest?",
                              "3. Learn the basics"])

    def test_plan_wording(self):
        with self._run(self.fay, "fay", "Plan") as at:
            labels = [n.label for n in at.number_input]
            self.assertIn("I want to have ($)", labels)
            self.assertIn("I'll invest each month ($)", labels)
            self.assertNotIn("Target amount ($)", labels)
            self.assertIn("by (date)", [d.label for d in at.date_input])
            self.assertIn("Save my plan", [b.label for b in at.button])


class BeginnerPiecesTests(unittest.TestCase):
    TODAY = date(2026, 10, 2)

    def test_nothing_invested_is_starting_out(self):
        p = plans.progress(GOAL, 0.0, today=self.TODAY)
        self.assertEqual(p["status"], "starting")
        self.assertGreater(p["needed_monthly"], 100)
        # money in: the usual statuses
        self.assertEqual(plans.progress(GOAL, 1000.0, today=self.TODAY)["status"], "behind")
        self.assertEqual(plans.progress({**GOAL, "target_date": "2026-01-01"}, 0.0,
                                        today=self.TODAY)["status"], "past_date")

    def test_suggested_starting_points(self):
        tip = learn.suggestions(PROFILE, GOAL, today=self.TODAY)
        months = plans.months_until(GOAL["target_date"], self.TODAY)
        self.assertEqual(tip["years"], round(months / 12))
        self.assertEqual(tip["monthly"],
                         round(plans.required_monthly(0.0, 20000.0, 6.0, months)))
        self.assertEqual(tip["stocks_pct"], learn.starter_mix(PROFILE, months / 12)["stocks_pct"])
        self.assertEqual(sum(tip["target_mix"].values()), 100.0)
        self.assertEqual(tip["return_pct"], plans.DEFAULT_RETURN_PCT)
        # a different assumed return changes only the monthly amount
        self.assertLess(learn.suggestions(PROFILE, GOAL, today=self.TODAY,
                                          return_pct=8.0)["monthly"], tip["monthly"])
        # no goal: the date from their timeline, no monthly amount
        tip = learn.suggestions(PROFILE, None, today=self.TODAY)
        self.assertIsNone(tip["monthly"])
        self.assertEqual(tip["goal_date"], plans.add_months(self.TODAY, 120))
        self.assertEqual(tip["stocks_pct"], learn.starter_mix(PROFILE)["stocks_pct"])
        # nothing answered yet: middle-of-the-road starting points
        tip = learn.suggestions({}, None, today=self.TODAY)
        self.assertEqual((tip["years"], tip["stocks_pct"]),
                         (learn.DEFAULT_YEARS, learn.DEFAULT_STOCKS_PCT))

    def test_route_when_starting_is_the_next_waypoint(self):
        wps = [("profile", "About you", True), ("ready", "Ready?", False),
               ("goal", "Set a goal", True)]
        kw = dict(has_goal=True, can_manage=True, profile_missing=False, has_holdings=False,
                  monthly=100.0, goal=None, drift=[], days_since_holdings=None, waypoints=wps)
        self.assertEqual(route.next_step(**kw)["key"], "holdings")
        step = route.next_step(**kw, starting=True)
        self.assertEqual((step["key"], step["step"], step["number"]), ("learn", "ready", 2))

    def test_mix_reasons_add_up_to_the_split(self):
        mix = learn.starter_mix(PROFILE)
        self.assertEqual(mix["stocks_pct"], 55)
        self.assertIn("75%", mix["reasons"][0])
        self.assertIn("15 points less", mix["reasons"][1])
        self.assertIn("5 points less", mix["reasons"][2])
        self.assertIn("55%", mix["reasons"][-1])

    def test_legend_never_grows_past_its_card(self):
        with open(os.path.join(REPO, "dashboard.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("grid-template-columns: minmax(0, 1fr)", src.split(".pt-legend {", 1)[1][:80])
        self.assertNotIn("white-space: nowrap", src.split(".pt-legend-label {", 1)[1][:80])


if __name__ == "__main__":
    unittest.main()
