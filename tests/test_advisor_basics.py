"""Advisor basics, part 1 - what an advisor needs before trusting Northwend
with real clients: emails on approve / decline, client names and households,
one "add and invite" step from the advisor's own name, reports to clients
who can't sign in yet, a tidier Your clients, and one message to all clients.

    python -m unittest tests.test_advisor_basics        (from the repo root)
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

import admin  # noqa: E402
import advising  # noqa: E402
import alerts  # noqa: E402
import auth  # noqa: E402
import mailer  # noqa: E402
import manage_users  # noqa: E402
import overview  # noqa: E402
import portfolio  # noqa: E402
import prefs  # noqa: E402
import sample_data  # noqa: E402
import two_step  # noqa: E402


class _Outbox:
    """mailer.send, recorded: every email (to, subject, text, from_name)."""

    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text, html=None, *, from_name=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html or "",
                          "from_name": from_name, "from": mailer.sender(from_name)})
        return True

    def to(self, address):
        return [m for m in self.sent if m["to"] == address]


class _DB:
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pt_adv_")
        self.db = os.path.join(self.dir, "adv.db")
        self.env = unittest.mock.patch.dict(os.environ, {"PORTFOLIO_DB": self.db,
                                                         "MAIL_DRY_RUN": "1"})
        self.env.start()
        for key in ("ANTHROPIC_API_KEY", "FINNHUB_API_KEY", "RESEND_API_KEY",
                    "NORTHWEND_ADMINS", "APP_URL"):
            os.environ.pop(key, None)
        # The app's first run reloads the repo's modules (codefresh.py): put
        # back the ones imported here afterwards, for the other test files
        self.modules = {n: m for n, m in sys.modules.items() if os.path.dirname(
            os.path.abspath(getattr(m, "__file__", None) or "")) == REPO}
        self.conn = portfolio.connect(self.db)
        self.outbox = _Outbox()
        self.mail = unittest.mock.patch.object(mailer, "send", self.outbox)
        self.mail.start()
        self.app_mail = []

    def _catch_app_mail(self):
        """The app's own mailer (a fresh copy after codefresh) sends here too."""
        app_mailer = sys.modules.get("mailer")
        if app_mailer is not None and app_mailer.send is not self.outbox:
            p = unittest.mock.patch.object(app_mailer, "send", self.outbox)
            p.start()
            self.app_mail.append(p)

    def tearDown(self):
        for p in reversed(self.app_mail):
            p.stop()
        self.mail.stop()
        self.conn.close()
        self.env.stop()
        sys.modules.update(self.modules)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _advisor(self, login="carol", card=None):
        uid = auth.create_user(self.conn, login, "pw-123456789")
        auth.set_advisor(self.conn, login, True)
        if card:
            prefs.save(self.conn, uid, {"advisor_card": card})
        return uid


# --------------------------------------------------------------------------- #
# 1. approval and decline emails
# --------------------------------------------------------------------------- #
class ApprovalEmailTests(_DB, unittest.TestCase):

    def setUp(self):
        super().setUp()
        made = auth.sign_up(self.conn, "dana@example.com", "goodpass1", agreed=True, adult=True,
                            terms_version="v", seconds_open=10)
        self.uid = made["user_id"]
        auth.request_advisor(self.conn, self.uid, "Ruiz Wealth", "1234567")

    def _cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage_users.main(["--db", self.db, *args])
        return code, out.getvalue()

    def test_approving_emails_what_to_do_next(self):
        res = admin.approve_advisor(self.conn, "dana@example.com", "https://app.example/?x=1")
        self.assertEqual(res, {"ok": True, "emailed": True})
        self.assertTrue(auth.is_advisor(self.conn, self.uid))
        self.assertEqual(auth.advisor_request(self.conn, self.uid)["decision"], "approved")
        (mail,) = self.outbox.to("dana@example.com")
        self.assertIn("advisor access is ready", mail["subject"])
        for words in ("two-step sign-in", "first client", "https://app.example/?page=your-clients"):
            self.assertIn(words, mail["text"])
        # approving again (already an advisor) doesn't email twice
        self.assertEqual(admin.approve_advisor(self.conn, "dana@example.com")["emailed"], None)
        self.assertEqual(len(self.outbox.sent), 1)
        self.assertFalse(admin.approve_advisor(self.conn, "nobody")["ok"])

    def test_declining_emails_politely(self):
        res = admin.decline_advisor(self.conn, "dana@example.com", "https://app.example/")
        self.assertEqual(res, {"ok": True, "emailed": True})
        self.assertFalse(auth.is_advisor(self.conn, self.uid))
        (mail,) = self.outbox.to("dana@example.com")
        self.assertIn("support@northwend.app", mail["text"])
        self.assertIn("investor account", mail["text"])
        self.assertNotIn("ready", mail["subject"])
        # nothing waiting any more: nothing declined, nothing sent
        self.assertFalse(admin.decline_advisor(self.conn, "dana@example.com")["ok"])
        self.assertEqual(len(self.outbox.sent), 1)

    def test_command_line_emails_too(self):
        code, out = self._cli("make-advisor", "dana@example.com")
        self.assertEqual(code, 0)
        self.assertIn("Emailed them", out)
        (mail,) = self.outbox.to("dana@example.com")
        self.assertIn(admin.DEFAULT_APP_URL, mail["text"])   # no APP_URL set here
        other = auth.sign_up(self.conn, "lee@example.com", "goodpass1", agreed=True, adult=True,
                             terms_version="v", seconds_open=10)
        auth.request_advisor(self.conn, other["user_id"], "Lee & Co", "7654321")
        code, out = self._cli("decline-advisor", "lee@example.com")
        self.assertEqual(code, 0)
        self.assertIn("Emailed them", out)
        self.assertEqual(len(self.outbox.to("lee@example.com")), 1)

    def test_an_account_without_an_email_is_approved_quietly(self):
        auth.create_user(self.conn, "sam", "pw-123456789")
        self.assertEqual(admin.approve_advisor(self.conn, "sam"), {"ok": True, "emailed": None})
        self.assertEqual(self.outbox.sent, [])

    def test_the_owner_is_pointed_at_the_admin_portal(self):
        mailer.advisor_request("dana@example.com", "Ruiz Wealth", "1234567",
                               "https://app.example/?page=clients")
        (mail,) = self.outbox.to(mailer.REPLY_TO)
        self.assertIn("https://app.example/?page=admin", mail["text"])
        self.assertIn("make-advisor dana@example.com", mail["text"])   # still there as a fallback


# --------------------------------------------------------------------------- #
# 2 and 3: names, households, the sender's name
# --------------------------------------------------------------------------- #
class ClientNameTests(_DB, unittest.TestCase):

    def test_a_household_without_an_email(self):
        carol = self._advisor()
        cid = auth.create_client(self.conn, carol, "", name="  Chen   household ")
        self.assertEqual(auth.get_username(self.conn, cid), "chen.household")
        again = auth.create_client(self.conn, carol, "", name="Chen household")
        self.assertEqual(auth.get_username(self.conn, again), "chen.household2")
        self.assertEqual(auth.list_clients(self.conn, carol),
                         [(cid, "Chen household"), (again, "Chen household")])
        with self.assertRaises(ValueError):
            auth.create_client(self.conn, carol, "")            # no name, no login

    def test_name_shown_and_editable_by_their_advisor_only(self):
        carol = self._advisor()
        dana = auth.create_client(self.conn, carol, "dana@example.com", name="Dana Lee")
        zed = auth.create_client(self.conn, carol, "zed@example.com")
        self.assertEqual(auth.list_clients(self.conn, carol), [(dana, "Dana Lee"),
                                                               (zed, "zed@example.com")])
        # the client's own Account name is theirs: shown when the advisor gave none
        auth.set_display_name(self.conn, zed, "Zed Abbott")
        self.assertEqual(dict(auth.list_clients(self.conn, carol))[zed], "Zed Abbott")
        self.assertTrue(auth.set_client_name(self.conn, carol, dana, "Lee family"))
        self.assertEqual(dict(auth.list_clients(self.conn, carol))[dana], "Lee family")
        self.assertIsNone(auth.display_name(self.conn, dana))    # not written into theirs
        other = self._advisor("olga")
        self.assertFalse(auth.set_client_name(self.conn, other, dana, "Mine now"))
        self.assertEqual(dict(auth.list_clients(self.conn, carol))[dana], "Lee family")
        auth.set_client_name(self.conn, carol, dana, "   ")      # blank: back to the login
        self.assertEqual(dict(auth.list_clients(self.conn, carol))[dana], "dana@example.com")

    def test_name_column_in_both_schemas_and_the_back_fill(self):
        for name in ("schema.sql", "schema_pg.sql"):
            with open(os.path.join(REPO, name), encoding="utf-8") as fh:
                self.assertIn("client_name TEXT", fh.read(), name)
        with open(os.path.join(REPO, "portfolio.py"), encoding="utf-8") as fh:
            self.assertIn('("client_name", "TEXT")', fh.read())

    def test_sender_carries_the_advisor_name_and_never_a_new_address(self):
        self.assertEqual(mailer.sender(None), mailer.SENDER)
        self.assertEqual(mailer.sender("Dana Ruiz, Ruiz Wealth"),
                         '"Dana Ruiz, Ruiz Wealth via Northwend" <hello@northwend.app>')
        sneaky = mailer.sender('Eve" <eve@evil.example>\r\nBcc: x@y.z')
        self.assertTrue(sneaky.endswith(" <hello@northwend.app>"))
        self.assertEqual(sneaky.count("<"), 1)
        self.assertEqual(sneaky.count('"'), 2)
        self.assertNotIn("\n", sneaky)

    def test_dry_run_logs_the_from_line(self):
        self.mail.stop()
        try:
            with contextlib.redirect_stderr(io.StringIO()) as log:
                self.assertTrue(mailer.client_invite("pat@example.com", "https://x/?invite=t",
                                                     "Dana Ruiz (Ruiz Wealth)", 7,
                                                     from_name="Dana Ruiz, Ruiz Wealth"))
        finally:
            self.mail.start()
        self.assertIn('from="Dana Ruiz, Ruiz Wealth via Northwend" <hello@northwend.app>',
                      log.getvalue())


# --------------------------------------------------------------------------- #
# 4, 5, 6: the logic under the pages
# --------------------------------------------------------------------------- #
class LogicTests(_DB, unittest.TestCase):

    def test_choosing_a_password_from_the_link_counts_as_signing_in(self):
        carol = self._advisor()
        cid = auth.create_client(self.conn, carol, "pat@example.com", name="Pat")
        self.assertFalse(auth.has_signed_in(self.conn, cid))
        token = auth.create_invite(self.conn, carol, cid)
        self.assertTrue(auth.accept_invite(self.conn, token, "clientpass1")["ok"])
        self.assertTrue(auth.has_signed_in(self.conn, cid))

    def test_report_email_for_a_client_without_a_login_has_a_setup_link(self):
        mailer.report_ready("pat@example.com", "https://x/?page=advisor-notes", "Dana", "Q3 2026",
                            setup_link="https://x/?invite=abc", days=7, from_name="Dana")
        mailer.report_ready("lee@example.com", "https://x/?page=advisor-notes", "Dana", "Q3 2026")
        pat, lee = self.outbox.to("pat@example.com")[0], self.outbox.to("lee@example.com")[0]
        self.assertIn("?invite=abc", pat["text"])
        self.assertIn("choose your password", pat["text"])
        self.assertNotIn("?invite=", lee["text"])
        self.assertIn("Sign in to read it", lee["text"])

    def test_only_meaningful_alerts_make_a_client_need_a_look(self):
        def alert(key, sym, value, acct="IRA"):
            return alerts.Alert(key, key, sym, acct, "m", value, 5.0,
                                "up" if value >= 0 else "down")
        fired = [alert("total_gl", "AAPL", 45.0), alert("total_gl", "VTI", 30.0),
                 alert("total_gl", "XYZ", -35.0), alert("day_move", "XYZ", -7.0),
                 alert("day_move", "NVDA", 6.0)]
        # AAPL and VTI are just up a lot: good news, not a worry; XYZ counts once
        self.assertEqual(overview.attention_alerts(fired), 2)
        # the example portfolio: three holdings up past +20%, nothing to look at
        cid = auth.create_user(self.conn, "x", "pw-123456789")
        sample_data.load(self.conn, cid)
        s = overview.account_summaries(self.conn, [cid], overview.latest_quotes(self.conn))[cid]
        self.assertEqual((s["n_alerts"], s["n_alerts_attention"]), (3, 0))

    def test_this_week_lists_each_client_once_with_every_reason(self):
        every = advising.REVIEW_EVERY_DAYS
        row = lambda uid, review, days, reasons: {"user_id": uid, "name": f"c{uid}",  # noqa: E731
                                                   "review": review, "review_days": days,
                                                   "reasons": reasons}
        s = advising.weekly_summary([
            row(1, "never", None, ["No review yet", "No goal", "2 alerts"]),
            row(2, "ok", every - 3, ["Drift 9 pts"]),
            row(3, "ok", 10, ["Profile incomplete"]),
        ])
        self.assertEqual([(r["user_id"], r["why"]) for r in s["clients"]], [
            (1, ["never reviewed", "No goal", "2 alerts"]),
            (2, ["review due in 4 days", "Drift 9 pts"]),
            (3, ["Profile incomplete"])])
        self.assertEqual([r["user_id"] for r in s["others"]], [3])

    def test_message_goes_to_own_clients_only_and_not_twice(self):
        carol, olga = self._advisor(), self._advisor("olga")
        a = auth.create_client(self.conn, carol, "a@example.com", name="Ann")
        b = auth.create_client(self.conn, carol, "", name="Bo household")
        theirs = auth.create_client(self.conn, olga, "t@example.com", name="Not hers")
        now = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
        res = advising.message_clients(self.conn, carol, [a, b, theirs, carol, a],
                                       "  Markets were bumpy - no need to act.  ", now=now)
        self.assertEqual(res["sent_to"], [a, b])
        for cid in (a, b):
            (note,) = advising.list_notes(self.conn, cid, include_private=False)
            self.assertEqual((note["kind"], note["body"], note["advisor_id"]),
                             ("Note", "Markets were bumpy - no need to act.", carol))
        self.assertEqual(advising.list_notes(self.conn, theirs, include_private=True), [])
        # the same message again a minute later (a double click): refused
        again = advising.message_clients(self.conn, carol, [a, b],
                                         "Markets were bumpy - no need to act.",
                                         now=now + timedelta(minutes=1))
        self.assertFalse(again["ok"])
        self.assertIn("a moment ago", again["error"])
        self.assertEqual(len(advising.list_notes(self.conn, a, include_private=False)), 1)
        later = now + timedelta(minutes=advising.MESSAGE_REPEAT_MINUTES + 1)
        self.assertTrue(advising.message_clients(self.conn, carol, [a], "Markets were bumpy - "
                                                 "no need to act.", now=later)["ok"])
        self.assertFalse(advising.message_clients(self.conn, carol, [a], " ", now=later)["ok"])
        self.assertFalse(advising.message_clients(self.conn, carol, [theirs], "Hi",
                                                  now=later)["ok"])


# --------------------------------------------------------------------------- #
# the pages, with streamlit's AppTest
# --------------------------------------------------------------------------- #
class YourClientsPageTests(_DB, unittest.TestCase):

    def setUp(self):
        super().setUp()
        c = self.conn
        self.carol = self._advisor()
        secret = two_step.new_secret()
        two_step.enable(c, self.carol, secret, two_step.totp(secret))
        self.carol_ok = f"{self.carol}:{two_step.status(c, self.carol)['stamp']}"

    def _client(self, email, name, *, signed_in=False, sample=True):
        cid = auth.create_client(self.conn, self.carol, email, name=name)
        if sample:
            sample_data.load(self.conn, cid)
        if signed_in:
            self.conn.execute("UPDATE users SET last_login_at = '2026-10-01 10:00:00', "
                              "email_verified_at = '2026-10-01 10:00:00' WHERE id = ?", (cid,))
            self.conn.commit()
        return cid

    def _run(self, page="your-clients", at=None, **state):
        from streamlit.testing.v1 import AppTest
        if at is None:
            at = AppTest.from_file(os.path.join(REPO, "dashboard.py"), default_timeout=120)
            at.session_state["user_id"] = self.carol
            at.session_state["username"] = "carol"
            at.session_state["two_step_ok"] = self.carol_ok
            for k, v in state.items():
                at.session_state[k] = v
            if page:
                at.query_params["page"] = page
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self._catch_app_mail()
        return at

    def test_add_and_invite_asks_who_its_from_then_sends_from_that_name(self):
        at = self._run()
        at.text_input(key="new_client_name").input("Chen household")
        at.text_input(key="new_client_email").input("wei.chen@example.com")
        at = self._run(at=at)
        self.assertEqual(at.button(key="add_client").label, "Add and send invite")
        # no "How clients see you" yet: asked for here, before the first invite
        at.button(key="add_client").click()
        at = self._run(at=at)
        self.assertTrue(any("Add your name first" in e.value for e in at.error))
        self.assertEqual(self.outbox.sent, [])
        self.assertEqual(auth.list_clients(self.conn, self.carol), [])
        at.text_input(key="new_adv_name").input("Dana Ruiz")
        at.text_input(key="new_adv_firm").input("Ruiz Wealth")
        at.button(key="add_client").click()
        at = self._run(at=at)
        ((cid, name),) = auth.list_clients(self.conn, self.carol)
        self.assertEqual(name, "Chen household")
        (mail,) = self.outbox.to("wei.chen@example.com")
        self.assertEqual(mail["from"],
                         '"Dana Ruiz, Ruiz Wealth via Northwend" <hello@northwend.app>')
        self.assertIn("Dana Ruiz (Ruiz Wealth) invited you", mail["subject"])
        self.assertIn("?invite=", mail["text"])
        self.assertIsNotNone(auth.pending_invite(self.conn, cid))
        self.assertTrue(any("Added Chen household and sent wei.chen@example.com a setup link"
                            in s.value for s in at.success), [s.value for s in at.success])
        card = prefs.load(self.conn, self.carol)["advisor_card"]
        self.assertEqual((card["name"], card["firm"]), ("Dana Ruiz", "Ruiz Wealth"))
        # the name shows on the list, the email under it as plain text
        html = " ".join(h.proto.body for h in at.get("html"))
        self.assertIn("<b>Chen household</b>", html)
        self.assertIn("wei.chen@example.com", html)
        self.assertNotIn("mailto:", html)

    def test_rename_a_client(self):
        cid = self._client("dana@example.com", "Dana Lee")
        at = self._run()
        at.text_input(key=f"rename_{cid}").input("Lee family")
        at.button(key=f"rename_save_{cid}").click()
        at = self._run(at=at)
        self.assertEqual(dict(auth.list_clients(self.conn, self.carol))[cid], "Lee family")
        self.assertIn("<b>Lee family</b>", " ".join(h.proto.body for h in at.get("html")))
        # ...and that's the name in the Viewing bar and the picker in the client's account
        at = self._run(page="home", active_user_id=cid)
        self.assertTrue(any("Viewing **Lee family**'s account" in m.value for m in at.markdown))
        sel = at.selectbox(key="viewing_select")
        self.assertIn("Lee family", sel.options)

    def test_viewing_bar_is_hidden_on_your_clients_and_each_client_listed_once(self):
        cid = self._client("dana@example.com", "")   # listed by her email
        at = self._run(active_user_id=cid)
        self.assertEqual(at.session_state["active_user_id"], cid)
        self.assertEqual([b.key for b in at.button if (b.key or "").startswith("viewing_")], [])
        self.assertFalse([m for m in at.markdown if "Viewing" in m.value])
        # This week: one line for her with all her reasons, as plain text (no mailto)
        week = [b.key for b in at.button if (b.key or "").startswith("wk_clients_")]
        self.assertEqual(week, [f"wk_clients_{cid}"])
        line = next(h.proto.body for h in at.get("html") if "never reviewed" in h.proto.body)
        self.assertIn("dana@example.com", line)
        self.assertIn("No goal", line)
        self.assertFalse([m for m in at.markdown if "dana@example.com" in m.value])
        # the example portfolio's +20% gains aren't "alerts" to look at
        self.assertNotIn("alert", line)

    def test_report_to_a_client_without_a_login_brings_a_setup_link(self):
        new = self._client("new@example.com", "New Client")
        old = self._client("old@example.com", "Old Client", signed_in=True)
        at = self._run()
        self.assertEqual(sorted(at.multiselect(key="rep_all_clients").value), sorted([new, old]))
        at.button(key="rep_all_send").click()
        at = self._run(at=at)
        (to_new,) = self.outbox.to("new@example.com")
        (to_old,) = self.outbox.to("old@example.com")
        self.assertIn("?invite=", to_new["text"])
        self.assertIsNotNone(auth.pending_invite(self.conn, new))
        self.assertNotIn("?invite=", to_old["text"])
        self.assertIn("Sign in to read it", to_old["text"])
        self.assertTrue(any("1 hadn't set up their login yet" in s.value for s in at.success),
                        [s.value for s in at.success])

    def test_message_all_clients(self):
        a = self._client("a@example.com", "Ann", signed_in=True)
        b = self._client("b@example.com", "Bo")          # no login yet
        c = self._client("", "Cy household")             # no email at all
        at = self._run()
        self.assertEqual(sorted(at.multiselect(key="msg_clients").value), sorted([a, b, c]))
        at.text_area(key="msg_body").input("Bumpy week - no need to do anything.")
        at = self._run(at=at)
        at.button(key="msg_review").click()
        at = self._run(at=at)
        self.assertTrue(any("Send this message to 3 clients?" in w.value for w in at.warning))
        self.assertEqual(self.outbox.sent, [])          # nothing yet: it's a check first
        at.button(key="msg_send").click()
        at = self._run(at=at)
        for cid in (a, b, c):
            notes = advising.list_notes(self.conn, cid, include_private=False)
            self.assertEqual([n["body"] for n in notes], ["Bumpy week - no need to do anything."])
        (mail,) = self.outbox.sent                      # only Ann can sign in with a confirmed email
        self.assertEqual(mail["to"], "a@example.com")
        self.assertNotIn("Bumpy", mail["text"])          # never the message itself
        self.assertNotIn("$", mail["text"])
        self.assertIn("sent you a message", mail["subject"])
        self.assertIn("?page=your-advisor", mail["text"])
        done = " ".join(s.value for s in at.success)
        self.assertIn("Sent to 3 clients", done)
        self.assertIn("Emailed 1", done)
        self.assertIn("2 will see it next time they sign in", done)
        # the same text again straight away: refused
        at.text_area(key="msg_body").input("Bumpy week - no need to do anything.")
        at = self._run(at=at)
        at.button(key="msg_review").click()
        at = self._run(at=at)
        at.button(key="msg_send").click()
        at = self._run(at=at)
        self.assertTrue(any("a moment ago" in w.value for w in at.warning))
        self.assertEqual(len(advising.list_notes(self.conn, a, include_private=False)), 1)
        self.assertEqual(len(self.outbox.sent), 1)


if __name__ == "__main__":
    unittest.main()
