"""Advisor basics, the client's side: proposal emails (the client hears when
one is shared, the advisor when it's answered - never a figure), client mode
(an advisor's client sees their advisor's next steps, never the beginner's
example funds) and the AI's errors (one calm sentence, never the raw error,
and a failed request doesn't use up the month's allowance). Runs dashboard.py
with streamlit's AppTest on a scratch database in a temp dir, plus the pure
pieces.

    python -m unittest tests.test_advisor_client        (from the repo root)
"""

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import advising  # noqa: E402
import advisor  # noqa: E402
import ai_usage  # noqa: E402
import auth  # noqa: E402
import gear  # noqa: E402
import mailer  # noqa: E402
import portfolio  # noqa: E402
import prefs  # noqa: E402
import proposals  # noqa: E402
import reports  # noqa: E402
import route  # noqa: E402
import two_step  # noqa: E402

PROFILE = {"goal": "Build long-term wealth", "time_horizon_years": 10,
           "risk_tolerance": "moderate", "drawdown_reaction": "Hold",
           "experience": "new", "age_range": "35-44", "income_stability": "Very stable",
           "emergency_fund": "3-6 months", "high_interest_debt": "None",
           "employer_match": "No match or no plan"}
MIX = {"Stocks": 63.0, "Bonds": 37.0}
RAW = "Error code: 401 - {'type': 'error', 'error': {'message': 'invalid x-api-key'}}"


def _api_error(cls, status):
    """An anthropic API error as the SDK raises it, with a raw message."""
    import types

    import anthropic
    if cls is anthropic.APIConnectionError:
        return cls(message=RAW, request=None)
    return cls(RAW, response=types.SimpleNamespace(request=None, status_code=status, headers={}),
               body=None)


class PureTests(unittest.TestCase):

    def test_proposal_emails_carry_no_figures(self):
        sent = []
        with unittest.mock.patch.object(mailer, "send",
                                        lambda *a, **k: sent.append(a) or True):
            self.assertTrue(mailer.proposal_shared("dana@example.com", "https://x/?page=your-advisor",
                                                   "Carol Lee"))
            self.assertTrue(mailer.proposal_answered("carol@example.com",
                                                     "https://x/?page=plan&client=7", "Dana", True))
            self.assertTrue(mailer.proposal_answered("carol@example.com",
                                                     "https://x/?page=plan&client=7", "Dana", False))
        (to1, subj1, text1, html1), (to2, subj2, text2, _), (_, subj3, text3, _) = sent
        self.assertEqual((to1, subj1), ("dana@example.com", "Your advisor has a proposal for you"))
        self.assertIn("Carol Lee has shared a proposal", text1)
        self.assertIn("?page=your-advisor", text1)
        self.assertEqual((to2, subj2), ("carol@example.com", "Dana accepted your proposal"))
        self.assertIn("?page=plan&client=7", text2)
        self.assertEqual(subj3, "Dana answered your proposal")
        self.assertIn("not right now", text3)
        for text in (text1, html1, text2, text3):
            self.assertNotIn("%", text)
            self.assertNotIn("$", text)

    def test_advisor_steps(self):
        step = route.advisor_step
        self.assertEqual(step(proposals_waiting=1, reports_new=1, profile_missing=True,
                              has_holdings=False)["key"], "proposal")
        self.assertEqual(step(proposals_waiting=0, reports_new=2, profile_missing=True,
                              has_holdings=False)["key"], "report")
        self.assertEqual(step(proposals_waiting=0, reports_new=0, profile_missing=True,
                              has_holdings=True)["key"], "profile_advisor")
        self.assertEqual(step(proposals_waiting=0, reports_new=0, profile_missing=False,
                              has_holdings=False)["key"], "bring_advisor")
        # nothing from the advisor: the usual next step decides (goal, drift...)
        self.assertIsNone(step(proposals_waiting=0, reports_new=0, profile_missing=False,
                               has_holdings=True))

    def test_a_clients_kit_has_no_practice_rope(self):
        self.assertIn("rope", gear.kit_keys())
        self.assertNotIn("rope", gear.kit_keys(managed=True))
        facts = {k: True for k in gear.NEED.values()}
        self.assertNotIn("rope", gear.earned(facts, gear.kit_keys(True)))
        self.assertNotEqual(gear.next_up(["map"], gear.kit_keys(True)), "rope")

    def test_failures_in_plain_words(self):
        import anthropic
        busy = (_api_error(anthropic.RateLimitError, 429),
                _api_error(anthropic.OverloadedError, 529),
                _api_error(anthropic.InternalServerError, 500),
                _api_error(anthropic.APIConnectionError, 0))
        for exc in busy:
            self.assertEqual(ai_usage.failure_kind(exc), ai_usage.BUSY, type(exc).__name__)
            self.assertEqual(ai_usage.failure_text(exc),
                             "Northwend is busy right now - try again in a minute.")
        for exc in (_api_error(anthropic.AuthenticationError, 401),
                    _api_error(anthropic.PermissionDeniedError, 403),
                    _api_error(anthropic.BadRequestError, 400)):
            self.assertEqual(ai_usage.failure_kind(exc), ai_usage.UNAVAILABLE)
            self.assertEqual(ai_usage.failure_text(exc),
                             "Ask Northwend isn't available right now.")
        self.assertEqual(ai_usage.failure_text(busy[0], feature="Reading screenshots"),
                         "Northwend is busy right now - try again in a minute.")
        self.assertEqual(ai_usage.failure_text(_api_error(anthropic.AuthenticationError, 401),
                                               feature="Reading screenshots"),
                         "Reading screenshots isn't available right now.")

    def test_screenshot_and_csv_failures_say_so_calmly(self):
        import anthropic
        import csv_import
        import screenshot_read
        import txn_import

        class _Raises:
            def __init__(self, exc):
                self.messages = self
                self.exc = exc

            def create(self, **kw):
                raise self.exc
        exc = _api_error(anthropic.AuthenticationError, 401)
        out = screenshot_read.read([(b"x", "image/png")], "key", client=_Raises(exc), model="m")
        self.assertFalse(out["answered"])        # not counted
        self.assertIs(out["failure"], exc)       # for the server log
        self.assertEqual(out["error"], "Reading screenshots isn't available right now.")
        self.assertNotIn("401", out["error"])
        # the column guess: a failed request raises, so the page can say so
        # (and not count it); an answer that doesn't help is None
        for mod in (csv_import, txn_import):
            with self.assertRaises(anthropic.AuthenticationError):
                mod.ai_mapping(["A", "B"], [["TEXT", "NUMBER"]], "key", client=_Raises(exc),
                               model="m")


class _AppBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # the app's first run reloads the repo's modules (codefresh.py): put
        # back the ones other test files imported, so their mocks still reach
        cls.modules = {n: m for n, m in sys.modules.items()
                       if os.path.dirname(os.path.abspath(getattr(m, "__file__", None) or "")) == REPO}
        cls.dir = tempfile.mkdtemp(prefix="pt_advclient_")
        cls.db = os.path.join(cls.dir, "app.db")
        portfolio._SCHEMA_READY.discard(os.path.abspath(cls.db))
        c = portfolio.connect(cls.db)
        try:
            cls.seed(c)
        finally:
            c.close()

    @classmethod
    def seed(cls, c):
        cls.carol = auth.create_user(c, "carol", "pw-123456789")
        auth.set_advisor(c, "carol", True)
        c.execute("UPDATE users SET email = 'carol@example.com', email_verified_at = "
                  "'2026-09-01T10:00:00Z' WHERE id = ?", (cls.carol,))
        secret = two_step.new_secret()
        two_step.enable(c, cls.carol, secret, two_step.totp(secret))
        cls.carol_ok = f"{cls.carol}:{two_step.status(c, cls.carol)['stamp']}"
        # her client, signed in before and with a confirmed email
        cls.dana = auth.create_user(c, "dana", "pw-123456789")
        auth.link_client(c, cls.carol, cls.dana)
        c.execute("UPDATE users SET email = 'dana@example.com', email_verified_at = "
                  "'2026-09-01T10:00:00Z', last_login_at = '2026-09-01T10:00:00Z' WHERE id = ?",
                  (cls.dana,))
        # her client with no login yet (a setup link not used)
        cls.eve = auth.create_client(c, cls.carol, "eve@example.com")
        # someone investing on their own
        cls.nina = auth.create_user(c, "nina", "pw-123456789")
        advisor.save_profile(c, cls.nina, PROFILE)
        prefs.save(c, cls.nina, {"first_steps": {"done": True}})
        c.commit()

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

    def _db(self):
        return portfolio.connect(self.db)

    @staticmethod
    def _keys(at):
        return [b.key for b in at.button if b.key]

    @staticmethod
    def _text(at):
        """Everything written on the page."""
        parts = [m.value for m in at.markdown] + [h.proto.body for h in at.get("html")]
        for kind in ("success", "info", "warning", "error", "caption"):
            parts += [str(e.value) for e in getattr(at, kind)]
        return " ".join(parts)

    @contextlib.contextmanager
    def _mail(self):
        """mailer.send, caught (the app's copy of the module)."""
        sent = []
        with unittest.mock.patch.object(sys.modules["mailer"], "send",
                                        lambda *a, **k: sent.append(a) or True):
            yield sent


class ProposalEmailTests(_AppBase):

    def _share(self, client_id):
        state = {f"prop_mix_{k}": v for k, v in MIX.items()}
        with self._run(self.carol, "carol", "Plan", active_user_id=client_id,
                       two_step_ok=self.carol_ok, prop_title="A steadier mix",
                       **state) as at:
            with self._mail() as sent:
                at.button(key="prop_save_share").click().run()
            return at, sent

    def test_sharing_tells_the_client_and_answering_tells_the_advisor(self):
        at, sent = self._share(self.dana)
        self.assertEqual(len(sent), 1)
        to, subject, text = sent[0][:3]
        self.assertEqual((to, subject), ("dana@example.com", "Your advisor has a proposal for you"))
        self.assertIn("?page=your-advisor", text)
        for figure in ("63", "37", "%", "$", "steadier"):   # no mix, no title
            self.assertNotIn(figure, text)
        self.assertIn("We emailed dana@example.com to let them know.", self._text(at))
        c = self._db()
        try:
            pid = proposals.for_client(c, self.dana, include_drafts=False)[0]["id"]
        finally:
            c.close()
        # Dana answers on Your advisor: Carol hears the same day
        with self._run(self.dana, "dana", "Advisor notes") as at2:
            with self._mail() as sent2:
                at2.button(key=f"prop_yes_{pid}").click().run()
        self.assertEqual(len(sent2), 1)
        to, subject, text = sent2[0][:3]
        self.assertEqual((to, subject), ("carol@example.com", "dana accepted your proposal"))
        self.assertIn(f"?page=plan&client={self.dana}", text)
        for figure in ("63", "37", "%", "$"):
            self.assertNotIn(figure, text)

    def test_a_client_with_no_login_yet_isnt_emailed(self):
        at, sent = self._share(self.eve)
        self.assertEqual(sent, [])
        self.assertIn("They haven't set up their login yet, so no email went", self._text(at))

    def test_who_can_be_emailed(self):
        c = self._db()
        try:
            self.assertEqual(proposals.who_to_tell(c, self.dana)["email"], "dana@example.com")
            eve = proposals.who_to_tell(c, self.eve)
            self.assertEqual((eve["email"], eve["signed_in"]), (None, False))
            self.assertEqual(proposals.who_to_tell(c, self.carol)["email"], "carol@example.com")
            # an advisor an admin set up (no sign-up terms): their email is
            # theirs, as for the Monday email (weekly_email.recipients)
            ana = auth.create_user(c, "ana", "pw-123456789")
            auth.set_advisor(c, "ana", True)
            c.execute("UPDATE users SET email = 'ana@example.com' WHERE id = ?", (ana,))
            c.commit()
            self.assertEqual(proposals.who_to_tell(c, ana)["email"], "ana@example.com")
            # ...but not someone's own unconfirmed sign-up
            c.execute("UPDATE users SET terms_version = '2026-01-01' WHERE id = ?", (ana,))
            c.commit()
            self.assertIsNone(proposals.who_to_tell(c, ana)["email"])
        finally:
            c.close()


class ClientModeTests(_AppBase):

    def _share_one(self):
        c = self._db()
        try:
            pid = proposals.save(c, self.carol, self.dana, title="A mix", mix=MIX)
            proposals.share(c, self.carol, pid)
        finally:
            c.close()
        return pid

    def test_a_clients_home_speaks_for_their_advisor(self):
        pid = self._share_one()
        with self._run(self.dana, "dana", "Dashboard") as at:
            text = self._text(at)
            self.assertIn("Your advisor has a proposal waiting for you", text)
            self.assertEqual(at.button(key="route_go").label, "Read the proposal")
            # none of the beginner's ways in: practice money, the example portfolio
            for key in ("start_practice", "start_example", "start_bring"):
                self.assertNotIn(key, self._keys(at))
            self.assertNotIn("Rope", text)     # no practice money, so no rope in her kit
            at.button(key="route_go").click().run()
            self.assertEqual(at.session_state["page"], "Advisor notes")
        # answered: then the questions for her advisor, then her statements
        c = self._db()
        try:
            proposals.respond(c, self.dana, pid, True)
        finally:
            c.close()
        with self._run(self.dana, "dana", "Dashboard") as at:
            self.assertIn("Answer a few questions for your advisor", self._text(at))
        c = self._db()
        try:
            advisor.save_profile(c, self.dana, PROFILE)
            reports.save(c, self.carol, self.dana, label="September 2026",
                         start=__import__("datetime").date(2026, 9, 1),
                         end=__import__("datetime").date(2026, 9, 30), facts={}, message="")
        finally:
            c.close()
        with self._run(self.dana, "dana", "Dashboard") as at:
            self.assertIn("A new progress report from your advisor", self._text(at))
        c = self._db()
        try:
            for r in reports.for_client(c, self.dana):
                reports.mark_read(c, self.dana, r["id"])
        finally:
            c.close()
        with self._run(self.dana, "dana", "Dashboard") as at:
            self.assertIn("Bring your statements in with your advisor", self._text(at))
        # her advisor in her account sees what she sees, under the ways in
        with self._run(self.carol, "carol", "Dashboard", active_user_id=self.dana,
                       two_step_ok=self.carol_ok) as at:
            text = self._text(at)
            self.assertIn("What dana sees on Home", text)
            self.assertIn("Bring your statements in with your advisor", text)

    def test_a_self_directed_investor_keeps_the_beginner_home(self):
        with self._run(self.nina, "nina", "Dashboard") as at:
            text = self._text(at)
            self.assertNotIn("with your advisor", text)
            self.assertIn("start_practice", self._keys(at))
            self.assertIn("Rope", text)

    def test_no_example_funds_for_a_client(self):
        import starter_funds
        c = self._db()
        try:
            advising.set_client_can_import(c, self.carol, self.dana, True)
        finally:
            c.close()
        # the hand-entry window: no "Not sure yet" (the example funds)
        with self._run(self.dana, "dana", "Dashboard", open_dialog="manual") as at:
            self.assertNotIn(starter_funds.INTRO, self._text(at))
            self.assertNotIn("Not sure yet", [t.label for t in at.tabs])
        with self._run(self.nina, "nina", "Dashboard", open_dialog="manual") as at:
            self.assertIn("Not sure yet", [t.label for t in at.tabs])
        # Learn: the reads only - no example mix, practice money or first buy
        with self._run(self.dana, "dana", "Get started", fs_hide=True) as at:
            self.assertNotIn("gs_watch", self._keys(at))
            self.assertNotIn("Your direction", self._text(at))
        # and she lands on Home, not Learn
        with self._run(self.dana, "dana", None) as at:
            pass
        self.assertEqual(at.session_state["page"], "Dashboard")


class AIErrorTests(_AppBase):

    def _used(self, uid, kind):
        c = self._db()
        try:
            return ai_usage.used(c, uid, kind)
        finally:
            c.close()

    def test_chat_failures_are_calm_and_not_counted(self):
        import anthropic
        cases = ((anthropic.AuthenticationError, 401, "Ask Northwend isn't available right now."),
                 (anthropic.RateLimitError, 429,
                  "Northwend is busy right now - try again in a minute."))
        for cls, status, words in cases:
            exc = _api_error(cls, status)

            def fails(*a, **k):
                raise exc
            with self._run(self.nina, "nina", "AI Assistant") as at:
                with unittest.mock.patch.object(sys.modules["advisor"], "stream_reply", fails):
                    at.chat_input[0].set_value("What is an index fund?").run()
                text = self._text(at)
                self.assertIn(words, text)
                for raw in ("Error code", "invalid x-api-key", cls.__name__):
                    self.assertNotIn(raw, text)
                # the question isn't left half-asked in the conversation
                self.assertEqual(at.session_state["chat_api"], [])
            self.assertEqual(self._used(self.nina, "chat"), 0)

        def answers(*a, **k):
            yield "An index fund holds a whole market."
        with self._run(self.nina, "nina", "AI Assistant") as at:
            with unittest.mock.patch.object(sys.modules["advisor"], "stream_reply", answers):
                at.chat_input[0].set_value("What is an index fund?").run()
        self.assertEqual(self._used(self.nina, "chat"), 1)   # counted once it answered

    def test_meeting_prep_failure_is_calm_and_not_counted(self):
        import anthropic
        exc = _api_error(anthropic.AuthenticationError, 401)

        def fails(*a, **k):
            raise exc
        with self._run(self.carol, "carol", "Advisor notes", active_user_id=self.dana,
                       two_step_ok=self.carol_ok) as at:
            with unittest.mock.patch.object(sys.modules["meeting"], "talking_points", fails):
                at.button(key="prep_draft").click().run()
            text = self._text(at)
            self.assertIn("Drafting talking points isn't available right now.", text)
            for raw in ("Error code", "invalid x-api-key", "Couldn't reach"):
                self.assertNotIn(raw, text)
        self.assertEqual(self._used(self.carol, "prep"), 0)


if __name__ == "__main__":
    unittest.main()
