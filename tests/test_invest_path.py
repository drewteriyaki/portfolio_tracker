"""The investing path: the route in two stages - Learn (education first, for
someone brand new) and then Start investing (choose a brokerage, open your
account, your first investments, bring it in). Who starts where, the
brokerage list (alphabetical, no per-brokerage claims, not ranked), old
account ticks carried over, and milestones still earned. Pure pieces
(route.py, brokerages.py, gear.py) plus dashboard.py with streamlit's
AppTest on a scratch database in a temp dir.

    python -m unittest tests.test_invest_path        (from the repo root)
"""

import ast
import contextlib
import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date
from urllib.parse import urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import advisor  # noqa: E402
import auth  # noqa: E402
import brokerages  # noqa: E402
import gear  # noqa: E402
import manual_entry  # noqa: E402
import plans  # noqa: E402
import portfolio  # noqa: E402
import prefs  # noqa: E402
import route  # noqa: E402

READY = {"goal": "Build long-term wealth", "time_horizon_years": 10,
         "risk_tolerance": "moderate", "drawdown_reaction": "Hold and wait",
         "experience": "new", "age_range": "25-34", "income_stability": "Very stable",
         "emergency_fund": "3-6 months", "high_interest_debt": "None",
         "employer_match": "No match or no plan"}
DONE = {"first_steps": {"done": True}}
GOAL = {"goal_type": "Build long-term wealth", "target_amount": 20000.0,
        "target_date": "2036-10-01", "monthly_contribution": 100.0}


def _view_literal(name):
    """A constant from views/get_started.py (the view runs inside
    dashboard.py, so it can't be imported on its own)."""
    with open(os.path.join(REPO, "views", "get_started.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise KeyError(name)


class StagesTests(unittest.TestCase):
    """The stage split and who starts where (route.py)."""

    def test_two_stages_cover_every_waypoint_in_order(self):
        steps = _view_literal("GET_STARTED_STEPS")
        keys = tuple(k for k, _ in steps)
        self.assertEqual(route.STAGE_KEYS[route.LEARN],
                         ("profile", "ready", "goal", "basics", "mix", "practice"))
        self.assertEqual(route.STAGE_KEYS[route.INVEST], ("brokerage", "account", "first", "bring"))
        self.assertEqual(keys, route.STAGE_KEYS[route.LEARN] + route.STAGE_KEYS[route.INVEST])
        self.assertEqual(dict(steps)["brokerage"], "Choose a brokerage")
        self.assertEqual(dict(steps)["account"], "Open your account")
        self.assertEqual(dict(steps)["first"], "Your first investments")
        self.assertEqual(dict(steps)["bring"], "Bring it in")
        self.assertEqual(route.STAGE_NAMES, {"learn": "Learn", "invest": "Start investing"})
        # every waypoint says what it's for, in one line starting "So "
        why = _view_literal("WAYPOINT_WHY")
        self.assertEqual(set(why), set(keys))
        for k, line in why.items():
            self.assertTrue(line.startswith("So ") and line.endswith("."), k)
        # each stage's own regions on the trail
        regions = {k: n for n, ks in route.REGIONS for k in ks}
        self.assertEqual(set(regions), set(keys))
        self.assertEqual({regions[k] for k in route.STAGE_KEYS[route.INVEST]},
                         {"The trailhead", "On the trail"})

    def test_stage_words(self):
        self.assertEqual(route.stage_words("goal"), "Learn · step 3 of 6")
        self.assertEqual(route.stage_words("brokerage"), "Start investing · step 1 of 4")
        self.assertEqual(route.stage_words("bring", managed=True), "Start investing · step 1 of 1")
        self.assertEqual(route.stage_of("practice"), "learn")
        self.assertEqual(route.stage_of("first"), "invest")

    def test_learn_only_for_the_brand_new(self):
        self.assertTrue(route.learn_first("new", False))
        self.assertTrue(route.learn_first(None, False))        # not answered yet: Learn first
        self.assertTrue(route.learn_first("New", False))
        self.assertFalse(route.learn_first("some", False))
        self.assertFalse(route.learn_first("experienced", False))
        self.assertFalse(route.learn_first("new", True))       # already invested
        learn, invest = route.STAGE_KEYS[route.LEARN], route.STAGE_KEYS[route.INVEST]
        self.assertEqual(route.route_keys(True), learn + invest)
        self.assertEqual(route.route_keys(False), invest)
        # an advisor's client: Start investing is their advisor's - one
        # waypoint - and Learn is never required, its reads only (client mode)
        self.assertFalse(route.learn_first("new", False, managed=True))
        self.assertEqual(route.route_keys(True, managed=True),
                         ("profile", "ready", "basics", "bring"))
        self.assertEqual(route.route_keys(False, managed=True), ("bring",))
        self.assertEqual(route.stage_keys(route.LEARN, managed=True),
                         ("profile", "ready", "basics"))

    def test_where_the_route_opens(self):
        done = {k: False for ks in route.STAGE_KEYS.values() for k in ks}
        shown = list(route.route_keys(True))
        self.assertEqual(route.opening(list(route.route_keys(True)), shown, done), "profile")
        self.assertEqual(route.opening(list(route.route_keys(False)), shown, done), "brokerage")
        # their route walked: the rest shown (optional Learn) next, then the last
        walked = {**done, **{k: True for k in route.STAGE_KEYS[route.INVEST]}}
        self.assertEqual(route.opening(list(route.route_keys(False)), shown, walked), "profile")
        self.assertEqual(route.opening(shown, shown, {k: True for k in done}), "bring")

    def test_home_next_step_follows_their_route(self):
        def nxt(waypoints, **kw):
            args = dict(has_goal=False, can_manage=True, profile_missing=False,
                        has_holdings=False, monthly=0.0, goal=None, drift=[],
                        days_since_holdings=None, waypoints=waypoints, starting=True)
            args.update(kw)
            return route.next_step(**args)
        invest = [(k, k.title(), False) for k in route.STAGE_KEYS[route.INVEST]]
        learn = [(k, k.title(), k == "profile") for k in route.STAGE_KEYS[route.LEARN]]
        # someone with experience and no goal yet: straight to Start investing
        self.assertEqual(nxt(invest)["step"], "brokerage")
        # someone new: Learn's next waypoint (Set a goal is one of them)
        self.assertEqual(nxt(learn + invest)["step"], "ready")
        # an advisor's client waits for the goal first
        self.assertEqual(nxt(invest, can_manage=False)["key"], "goal_wait")


class BrokerageListTests(unittest.TestCase):
    """Choose a brokerage: equals, alphabetically, never ranked or priced."""

    def test_alphabetical_with_a_link_to_each_ones_own_site(self):
        names = [n for n, _ in brokerages.BROKERAGES]
        self.assertGreaterEqual(len(names), 5)
        self.assertEqual(names, sorted(names, key=brokerages.sort_key))
        self.assertEqual(len(set(names)), len(names))
        for name in ("Charles Schwab", "Fidelity", "Robinhood", "Vanguard"):
            self.assertIn(name, names)
        for name, url in brokerages.BROKERAGES:
            u = urlparse(url)
            self.assertEqual(u.scheme, "https", name)
            self.assertTrue(u.netloc, name)
            self.assertFalse(re.search(r"[\s()\[\]<>\"']", url), name)
        md = brokerages.list_markdown().split("\n")
        self.assertEqual(len(md), len(names))
        for line, (name, url) in zip(md, brokerages.BROKERAGES):
            self.assertIn(f"({url})", line)
            self.assertIn(name.replace("*", "\\*"), line)

    def test_no_claims_about_any_one_brokerage(self):
        # each entry is just a name and its site - nothing that could go stale
        for entry in brokerages.BROKERAGES:
            self.assertEqual(len(entry), 2)
        for line in brokerages.list_markdown().split("\n"):
            words = re.sub(r"\(https://[^)]*\)", "", line).lower()
            self.assertNotRegex(words, r"[\d$%]")
            for claim in ("fee", "free", "commission", "minimum", "cheap", "best", "top",
                          "recommend", "fractional", "no-cost"):
                self.assertNotIn(claim, words)
        self.assertEqual(brokerages.NOT_RANKED,
                         "Northwend isn't paid by any of them and doesn't rank them.")
        self.assertIn("check each one's site for current fees", brokerages.CHECK_SITES)
        compare = brokerages.compare_markdown().lower()
        for thing in ("fees", "minimums", "fractional shares", "roth ira",
                      "regular brokerage account", "app", "customer help"):
            self.assertIn(thing, compare)
        for name, _ in brokerages.BROKERAGES:     # what to compare names no brokerage
            self.assertNotIn(name.lower(), compare)


class MilestoneTests(unittest.TestCase):
    """The kit still lines up with the route's waypoints."""

    def test_gear_regions_are_on_the_route(self):
        regions = {k: n for n, ks in route.REGIONS for k in ks}
        names = {n for n, _ in route.REGIONS}
        for k in gear.KEYS:
            region = gear.BY_KEY[k][4]
            if region and k != "flag":     # the summit flag is the goal itself
                self.assertIn(region, names, k)
        self.assertEqual(gear.BY_KEY["map"][4], regions["profile"])
        self.assertEqual(gear.BY_KEY["compass"][4], regions["goal"])
        self.assertEqual(gear.BY_KEY["tent"][4], regions["basics"])
        self.assertEqual(gear.BY_KEY["rope"][4], regions["practice"])
        self.assertEqual(gear.BY_KEY["boots"][4], regions["bring"])
        # the waypoints the kit sends people to are still Learn's
        steps = dict(_view_literal("GET_STARTED_STEPS"))
        for _label, (kind, where) in gear.GO.values():
            if kind == "learn":
                self.assertEqual(route.stage_of(where), route.LEARN)
                self.assertIn(where, steps)


class InvestPathAppTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # the app's first run reloads the repo's modules (codefresh.py): put
        # back the ones other test files imported, so their mocks still reach
        cls.modules = {n: m for n, m in sys.modules.items()
                       if os.path.dirname(os.path.abspath(getattr(m, "__file__", None) or "")) == REPO}
        cls.dir = tempfile.mkdtemp(prefix="pt_investpath_")
        cls.db = os.path.join(cls.dir, "app.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(cls.db))
        c = portfolio.connect(cls.db)
        try:
            # some experience, nothing brought in, no goal yet
            cls.ivy = auth.create_user(c, "ivy", "pw-123456789")
            advisor.save_profile(c, cls.ivy, {**READY, "experience": "some"})
            prefs.save(c, cls.ivy, DONE)
            cls.bo = auth.create_user(c, "bo", "pw-123456789")
            advisor.save_profile(c, cls.bo, {**READY, "experience": "experienced"})
            prefs.save(c, cls.bo, DONE)
            # experienced, on first steps' last screen
            cls.eli = auth.create_user(c, "eli", "pw-123456789")
            advisor.save_profile(c, cls.eli, {**READY, "experience": "experienced"})
            prefs.save(c, cls.eli, {"first_steps": {"step": 7}})
            # new; ticked "I've opened an account" on the one-waypoint checklist
            cls.otto = auth.create_user(c, "otto", "pw-123456789")
            advisor.save_profile(c, cls.otto, READY)
            prefs.save(c, cls.otto, {**DONE, "account_steps": ["chosen", "opened"]})
            # new; every tick of the old checklist
            cls.abe = auth.create_user(c, "abe", "pw-123456789")
            advisor.save_profile(c, cls.abe, READY)
            prefs.save(c, cls.abe, {**DONE, "account_steps": [
                "chosen", "opened", "funded", "first_buy", "monthly"]})
            plans.save_plan(c, cls.abe, GOAL, set_by=cls.abe)
            # new, Learn walked, own holdings brought in; gear already shown
            cls.rae = auth.create_user(c, "rae", "pw-123456789")
            advisor.save_profile(c, cls.rae, READY)
            plans.save_plan(c, cls.rae, GOAL, set_by=cls.rae)
            prefs.save(c, cls.rae, {**DONE, "get_started_done": ["goal", "basics", "practice"],
                                    "gear_seen": list(gear.KEYS)})
            meta, rows, totals, _ = manual_entry.build(
                [{"account": "Brokerage", "symbol": "VTI", "quantity": 10, "cost_basis": 2500.0,
                  "asset_type": "ETF"}], {"Brokerage": 50.0}, {"VTI": {"price": 300.0}},
                today=date(2026, 9, 30))
            portfolio.write_snapshot(c, cls.rae, meta, rows, totals, manual_entry.SOURCE)
            # an advisor and her client (new to investing)
            cls.cara = auth.create_user(c, "cara", "pw-123456789")
            auth.set_advisor(c, "cara", True)
            import two_step
            secret = two_step.new_secret()
            two_step.enable(c, cls.cara, secret, two_step.totp(secret))
            cls.cara_ok = f"{cls.cara}:{two_step.status(c, cls.cara)['stamp']}"
            cls.dot = auth.create_user(c, "dot", "pw-123456789")
            advisor.save_profile(c, cls.dot, READY)
            prefs.save(c, cls.dot, DONE)
            auth.link_client(c, cls.cara, cls.dot)
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

    def _prefs(self, uid):
        c = portfolio.connect(self.db)
        try:
            return prefs.load(c, uid)
        finally:
            c.close()

    # ---- who starts where -------------------------------------------------- #
    def test_some_experience_starts_at_start_investing(self):
        with self._run(self.ivy, "ivy", "Get started") as at:
            self.assertEqual(at.session_state["gs_at"], "brokerage")
            self.assertEqual(at.session_state["gs_stage"], "invest")
            body = self._html(at)
            self.assertIn("Start investing · step 1 of 4 · 0 complete", body)
            self.assertIn("You're in <b>Start investing · step 1 of 4</b>", body)
            stage = at.get("button_group")[0]
            self.assertIn("Learn (optional)", stage.proto.options[0].content)
            # Learn is still there, marked optional
            stage.set_value("learn").run()
            self.assertEqual(at.session_state["gs_at"], "goal")   # its first not complete
            self.assertIn("Learn (optional for you) · step 3 of 6", self._html(at))
            self.assertIn("Learn is optional for you", " ".join(c.value for c in at.caption))
        # Home's route card: their next step is choosing a brokerage, not Learn
        with self._run(self.ivy, "ivy", "Dashboard") as at:
            self.assertIn("Next: Choose a brokerage", self._md(at))
            self.assertIn("You're in <b>Start investing · step 1 of 4</b>", self._html(at))
            at.button(key="route_go").click().run()
            self.assertEqual((at.session_state["page"], at.session_state["gs_at"]),
                             ("Get started", "brokerage"))

    def test_first_steps_last_screen_with_experience(self):
        with self._run(self.eli, "eli", "Get started") as at:
            # bringing it in leads; no account yet is still a tap away
            self.assertEqual(at.button(key="fs_manual").proto.type, "primary")
            self.assertEqual(at.button(key="fs_no_account").proto.type, "tertiary")
            at.button(key="fs_no_account").click().run()
            self.assertEqual(at.session_state["page"], "Get started")
            self.assertEqual(at.session_state["gs_at"], "brokerage")

    def test_already_invested_lands_on_home_with_learn_optional(self):
        with self._run(self.rae, "rae", None) as at:
            self.assertEqual(at.session_state["page"], "Dashboard")
            # their route (Start investing) is walked: no Learn step pushed on Home
            self.assertNotIn("Next: Waypoint", self._md(at))
            self.assertIn("Every step of your route is complete", self._html(at))
        with self._run(self.rae, "rae", "Get started") as at:
            self.assertEqual(at.session_state["gs_at"], "mix")    # optional Learn: next open
            self.assertIn("Learn (optional for you)", self._html(at))
            self.assertIn("Every step of your route is complete",
                          " ".join(s.value for s in at.success))

    # ---- Start investing's waypoints ----------------------------------------- #
    def test_choose_a_brokerage(self):
        with self._run(self.bo, "bo", "Get started", gs_at="brokerage") as at:
            md = self._md(at)
            self.assertIn(brokerages.NOT_RANKED, md)
            self.assertIn(brokerages.CHECK_SITES, md)
            self.assertIn(brokerages.list_markdown(), md)
            # the brokerages appear in alphabetical order
            where = [md.index(f"]({url})") for _, url in brokerages.BROKERAGES]
            self.assertEqual(where, sorted(where))
            captions = " ".join(c.value for c in at.caption)
            self.assertIn("brokerage accounts", captions)       # Learn more (FINRA)
            self.assertIn("retirement accounts", captions)
            at.button(key="gs_complete").click().run()
            self.assertEqual(at.session_state["gs_at"], "account")
        self.assertIn("chosen", self._prefs(self.bo)["account_steps"])

    def test_first_investments_show_the_example_funds(self):
        with self._run(self.abe, "abe", "Get started", gs_at="first") as at:
            md = self._md(at)
            self.assertIn("What your first buy looks like", md)
            self.assertIn("examples to learn from, not recommendations", md)  # starter_funds
            self.assertIn("gs_starter_watch", self._keys(at))
            self.assertIn("Your direction:", md)
            self.assertTrue(at.checkbox(key="gs_acct_first_buy").value)

    # ---- old account ticks carry over ----------------------------------------- #
    def test_old_ticks_carry_over(self):
        # "I've opened an account" (chosen + opened): a brokerage is chosen, and
        # Open your account waits only for the money-in tick
        with self._run(self.otto, "otto", "Get started", gs_at="account") as at:
            self.assertIn("Start investing · step 2 of 4 · 1 complete", self._html(at))
            self.assertTrue(at.checkbox(key="gs_acct_opened").value)
            self.assertFalse(at.checkbox(key="gs_acct_funded").value)
        # every old tick: three of Start investing's four complete; Bring it in next
        with self._run(self.abe, "abe", "Get started", gs_at="bring") as at:
            self.assertIn("Start investing · step 4 of 4 · 3 complete", self._html(at))
            self.assertIn("gs_import", self._keys(at))
        self.assertEqual(self._prefs(self.abe)["account_steps"],
                         ["chosen", "opened", "funded", "first_buy", "monthly"])

    # ---- milestones ------------------------------------------------------------ #
    def test_milestones_still_earned(self):
        with self._run(self.rae, "rae", "Dashboard") as at:
            # map, compass, tent, rope and boots: Learn's waypoints and their holdings
            self.assertIn("Your kit · 5 of 8 earned", self._html(at))
            self.assertIn("On the trail · your expedition", self._html(at))

    # ---- an advisor's client ------------------------------------------------- #
    def test_managed_client_start_investing_is_the_advisors(self):
        with self._run(self.dot, "dot", "Get started", gs_at="bring", fs_hide=True) as at:
            self.assertIn("Your advisor,", self._md(at))
            self.assertIn("Start investing · step 1 of 1", self._html(at))
            self.assertTrue(at.button(key="gs_complete").disabled)
            self.assertNotIn("gs_import", self._keys(at))
        # no brokerage list for them: that waypoint isn't on their route
        with self._run(self.dot, "dot", "Get started", gs_at="brokerage", fs_hide=True) as at:
            self.assertNotEqual(at.session_state["gs_at"], "brokerage")
        # the advisor looking at it brings the statements in
        with self._run(self.cara, "cara", "Get started", active_user_id=self.dot,
                       two_step_ok=self.cara_ok, gs_at="bring") as at:
            self.assertIn("dot's Start investing is yours", self._md(at))
            self.assertIn("gs_import", self._keys(at))


if __name__ == "__main__":
    unittest.main()
