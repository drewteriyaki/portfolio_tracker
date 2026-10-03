# Part of dashboard.py, which runs this file with _view("admin") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# The Admin page (ROADMAP A1): accounts and logins, advisor requests, AI use,
# and which experience the admin's own account shows. Only admins see it
# (admin.is_admin, set from the command line). Logins only - never anyone's
# holdings or plans. Every action re-checks admin rights in the database.
# ruff: noqa: F821

import secrets

import admin
import error_alerts
import hosting
import two_step


def _admin_do(fn):
    """Run fn(conn) as the signed-in admin, or refuse; a message for the page."""
    c = connect(DB)
    try:
        if not admin.is_admin(c, st.session_state["user_id"]):
            st.session_state["admin_msg"] = ("error", "Only an admin can do that.")
            return
        st.session_state["admin_msg"] = fn(c)
    finally:
        c.close()


def _admin_set_own_role():
    want_advisor = st.session_state.get("admin_own_role") == "Advisor app"
    _admin_do(lambda c: (auth.set_advisor(c, st.session_state["username"], want_advisor),
                         ("success", "Your account now shows the "
                          + ("advisor app." if want_advisor else "investor app.")))[1])


def _admin_told(res):
    """What the approve / decline email did, for the message."""
    return {True: " We emailed them to let them know.",
            False: " The email to them couldn't be sent - let them know yourself.",
            None: ""}[res["emailed"]]


def _admin_decide(username, approve):
    """An advisor request: approve or decline it, and email them either way
    (admin.approve_advisor / decline_advisor)."""
    def act(c):
        if approve:
            res = admin.approve_advisor(c, username, _app_address())
            return ("success", f"{username} is now an advisor." + _admin_told(res))
        res = admin.decline_advisor(c, username, _app_address())
        if not res["ok"]:
            return ("info", f"{username} has no advisor request waiting.")
        return ("success", f"Declined {username}'s advisor request." + _admin_told(res))
    _admin_do(act)


def _admin_reset_link(user_id):
    def act(c):
        email = auth.email_status(c, user_id)["email"]
        res = auth.request_password_reset(c, email or "")
        if not res["ok"] or not res["token"]:
            return ("error", res["error"] or "This account has no email address.")
        sent = mailer.reset_password(res["to"], f"{_app_address()}?reset={res['token']}",
                                     auth.RESET_MINUTES)
        return (("success", f"Sent a password reset link to {res['to']}.") if sent
                else ("error", "The email couldn't be sent just now."))
    _admin_do(act)


def _admin_temp_password(user_id):
    def act(c):
        temp = secrets.token_urlsafe(9)
        auth.set_password(c, auth.get_username(c, user_id), temp)
        st.session_state["admin_temp"] = (user_id, temp)
        return ("success", "Temporary password set - shown below once. The account is signed "
                           "out everywhere.")
    _admin_do(act)


def _admin_unlock(username):
    _admin_do(lambda c: (auth.unlock_login(c, username),
                         two_step.unlock(c, auth.get_user_id(c, username)),
                         ("success", f"Unlocked {username}."))[2])


def _admin_reset_two_step(user_id, username):
    def act(c):
        two_step.reset(c, user_id)
        return ("success", f"Two-step sign-in is reset for {username}, and they're signed out "
                           "everywhere. They sign in with their password"
                + (" and set it up again straight away." if two_step.status(c, user_id)["required"]
                   else "; they can turn it on again on their Account page."))
    _admin_do(act)


def _admin_advisor(username, flag):
    """An account's role: making someone an advisor approves them, so they get
    the same "your advisor access is ready" email."""
    def act(c):
        if flag:
            res = admin.approve_advisor(c, username, _app_address())
            return ("success", f"{username} is now an advisor." + _admin_told(res))
        auth.set_advisor(c, username, False)
        return ("success", f"{username} is no longer an advisor.")
    _admin_do(act)


def _admin_ai(user_id, username, unlimited):
    _admin_do(lambda c: (ai_usage.set_unlimited(c, user_id, unlimited),
                         ("success", f"{username} {'has no' if unlimited else 'has the normal'} "
                                     "monthly AI limits."))[1])


def _admin_link(client_id, client_name):
    advisor_id = st.session_state.get(f"admin_link_to_{client_id}")
    if advisor_id is None:
        return

    def act(c):
        auth.link_client(c, advisor_id, client_id)
        return ("success", f"{client_name} is now a client of {auth.get_username(c, advisor_id)}.")
    _admin_do(act)


def _admin_unlink(advisor_name, client_id, client_name):
    def act(c):
        auth.unlink_client(c, auth.get_user_id(c, advisor_name), client_id)
        return ("success", f"{client_name} is no longer {advisor_name}'s client.")
    _admin_do(act)


def _admin_delete(user_id, username):
    if (st.session_state.get(f"admin_del_confirm_{user_id}") or "").strip() != username:
        st.session_state["admin_msg"] = ("error", "Type the login exactly to confirm.")
        return

    def act(c):
        res = admin.delete_account(c, user_id, by=st.session_state["user_id"])
        if not res["ok"]:
            return ("error", res["error"])
        st.session_state.pop("admin_pick", None)
        return ("success", f"Deleted {username} and all its data."
                + (f" {res['orphaned_clients']} client(s) no longer have an advisor."
                   if res["orphaned_clients"] else ""))
    _admin_do(act)


def _admin_create():
    login = st.session_state.get("admin_new_login") or ""
    as_advisor = st.session_state.get("admin_new_role") == "Advisor"
    client_of = st.session_state.get("admin_new_client_of")

    def act(c):
        res = admin.create_account(c, login)
        if not res["ok"]:
            return ("error", res["error"])
        if as_advisor:
            auth.set_advisor(c, res["username"], True)
        elif client_of:
            auth.link_client(c, client_of, res["user_id"])
        st.session_state["admin_new_login"] = ""
        if res["email"]:
            link = auth.setup_link(c, res["user_id"])
            sent = link["ok"] and mailer.account_created(
                link["to"], f"{_app_address()}?reset={link['token']}", auth.SETUP_DAYS)
            return (("success", f"Created {res['username']} and emailed a link to choose a "
                                f"password (works for {auth.SETUP_DAYS} days).") if sent else
                    ("warning", f"Created {res['username']}, but the email couldn't be sent - "
                                "use Send password reset email on the account."))
        st.session_state["admin_temp"] = (res["user_id"], res["temp_password"])
        return ("success", f"Created {res['username']}. Its temporary password is shown below "
                           "once - pass it on privately.")
    _admin_do(act)


def _admin_clear_cache():
    _admin_do(lambda c: (st.cache_data.clear(),
                         ("success", "Cleared the cached prices, charts and news - they "
                                     "reload on the next page."))[1])


def _admin_test_email():
    def act(c):
        email = auth.email_status(c, LOGIN_ID)["email"]
        if not email:
            return ("error", "Your account has no email address.")
        sent = mailer.send(email, f"{APP_NAME} test email",
                           f"This is a test from the Admin page of {_app_address() or APP_NAME}. "
                           "If you can read it, email sending works.")
        return (("success", f"Sent a test email to {email}.") if sent else
                ("error", "It couldn't be sent - check RESEND_API_KEY and the server log."))
    _admin_do(act)


def _admin_clear_errors():
    _admin_do(lambda c: (error_alerts.clear(c), ("success", "Cleared the list of errors."))[1])


def _render_errors(c):
    """Recent unexpected errors and failed jobs, one line per kind, newest
    first (error_alerts.py). Type and place only - never anyone's data."""
    rows = error_alerts.recent(c)
    st.markdown("**Recent errors**")
    if not rows:
        st.caption("None noted. Unexpected errors and failed scheduled jobs show up here.")
        return
    st.dataframe(pd.DataFrame([{
        "Last seen": _admin_when(r["last_seen"]),
        "What": "Scheduled job failed" if r["source"] == "job" else r["error_type"],
        "Where": r["place"] + (f", line {r['line']}" if r["line"] else ""),
        "Times": r["times"], "Since": _admin_when(r["first_seen"]),
        "Emailed": _admin_when(r["emailed_at"]) if r["emailed_at"] else "",
    } for r in rows]), hide_index=True, width="stretch")
    st.button("Clear the list", key="admin_clear_errors", on_click=_admin_clear_errors,
              help="Starts the list afresh. A cleared kind is emailed again the next time "
                   "it happens.")


def _render_system(c):
    """Developer facts about this copy of the app - never a secret's value."""
    from manage_users import where
    sha = hosting.version(HERE)
    last_price = c.execute("SELECT MAX(fetched_at) AS t FROM price_history").fetchone()["t"]
    last_bar = c.execute("SELECT MAX(date) AS d FROM daily_bars").fetchone()["d"]
    flagged = [r["username"] for r in c.execute(
        "SELECT username FROM users WHERE is_admin = 1 ORDER BY username")]
    listed = sorted(admin.listed_admins())
    admins = [x for x in (", ".join(flagged) + " (make-admin)" if flagged else "",
                          ", ".join(listed) + " (NORTHWEND_ADMINS)" if listed else "") if x]
    mail = {"sending": "sending (Resend)", "dry run": "dry run - written to the log, not sent",
            "off": "off - RESEND_API_KEY isn't set"}[mailer.status()]
    rows = [
        ("This copy", "Staging" if STAGING else "Live" if pgcompat.is_postgres_dsn(DB)
         else "Local"),
        ("Runs on", hosting.host_name()),
        ("Version", f"[{sha}](https://github.com/drewteriyaki/portfolio_tracker/commit/{sha})"
         if sha else "unknown"),
        ("Address", _app_address() or "unknown"),
        ("Database", where(DB)),
        ("Email", mail),
        ("Error alerts", f"emailed to {error_alerts.alert_to()}, at most once an hour per kind"
         if pgcompat.is_postgres_dsn(DB) else "listed below only - a local copy doesn't email"),
        ("AI (Anthropic key)", "set" if _anthropic_key() else "not set - AI features are off"),
        ("Live prices (Finnhub key)", "set" if resolve_key(None) else "not set"),
        ("Last price update", _admin_when(last_price)),
        ("Newest daily price history", last_bar or "none"),
        ("Admins", "; ".join(admins)),
    ]
    st.markdown("\n".join(f"- **{k}:** {v}" for k, v in rows))
    with st.container(horizontal=True):
        st.button("Clear cached data", key="admin_clear_cache", on_click=_admin_clear_cache,
                  help="Prices, charts and news are kept for a few minutes to keep pages "
                       "quick. Clear them to see fresh data straight away.")
        st.button("Send me a test email", key="admin_test_email", on_click=_admin_test_email)
    st.caption("Settings like keys and NORTHWEND_ADMINS live in the app's Secrets (Streamlit "
               "Cloud: Manage app, Settings, Secrets; Render: Environment). Only whether a key "
               "is set is shown here, never its value.")
    _render_errors(c)


def _admin_when(stamp):
    if not stamp:
        return "never"
    return str(stamp)[:16].replace("T", " ")


def _render_admin():
    st.caption("Only admin accounts see this page. It shows logins - who has an account and "
               "its role - never anyone's holdings or plans.")
    msg = st.session_state.pop("admin_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])
    temp = st.session_state.get("admin_temp")
    c = connect(DB)
    try:
        if not admin.is_admin(c, LOGIN_ID):
            st.error("Only an admin can open this page.")
            return
        accounts = admin.list_accounts(c)
        requests = auth.pending_advisor_requests(c)
        usage = c.execute("SELECT u.username, a.kind, a.used FROM ai_usage a JOIN users u "
                          "ON u.id = a.user_id WHERE a.month = ? ORDER BY u.username, a.kind",
                          (ai_usage.month_of(),)).fetchall()
    finally:
        c.close()
    by_id = {a["id"]: a for a in accounts}
    advisors = [a for a in accounts if a["is_advisor"]]

    # ---- your own account: which app it shows ---------------------------- #
    with st.container(border=True):
        st.markdown("**Your own account**")
        st.segmented_control("Show my account as", ["Investor app", "Advisor app"],
                             default="Advisor app" if IS_ADVISOR else "Investor app",
                             key="admin_own_role", on_change=_admin_set_own_role)
        st.caption("Switch to check what each kind of account sees. In the advisor app you can "
                   "add test clients; switching back keeps them, just out of sight.")

    # ---- this copy of the app, for the developer ------------------------- #
    with st.expander(":material/developer_mode: System"):
        c = connect(DB)
        try:
            _render_system(c)
        finally:
            c.close()

    # ---- advisor requests ------------------------------------------------- #
    st.subheader(f"Advisor requests ({len(requests)})", anchor=False)
    if not requests:
        st.caption("None waiting.")
    else:
        st.caption("Approve or decline - either way they get a short email saying so. "
                   "Approved advisors are asked to set up two-step sign-in, then add a client.")
    for r in requests:
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{r['username']}** · {r['firm']} · CRD/licence **{r['licence']}** · "
                        f"asked {_admin_when(r['requested_at'])}", width="stretch")
            st.link_button("Check on BrokerCheck", "https://brokercheck.finra.org/",
                           type="tertiary")
            st.button("Approve", key=f"admin_ok_{r['username']}", type="primary",
                      on_click=_admin_decide, args=(r["username"], True))
            st.button("Decline", key=f"admin_no_{r['username']}",
                      on_click=_admin_decide, args=(r["username"], False))

    # ---- accounts ------------------------------------------------------------ #
    st.subheader(f"Accounts ({len(accounts)})", anchor=False)
    roles = {}
    for a in accounts:
        roles[a["role"]] = roles.get(a["role"], 0) + 1
    st.caption(" · ".join(f"{n} {r}{'s' if n != 1 else ''}" for r, n in sorted(roles.items())))
    find = st.text_input("Find an account", key="admin_find", placeholder="Email or username")
    shown = [a for a in accounts if find.strip().lower() in (a["username"] or "").lower()]
    st.dataframe(pd.DataFrame([{
        "Login": a["username"], "Role": a["role"],
        "Email confirmed": "-" if a["confirmed"] is None else ("yes" if a["confirmed"] else "no"),
        "Advisor": a["advisor"] or "", "Clients": str(a["clients"]) if a["clients"] else "",
        "Created": _admin_when(a["created_at"]), "Last sign-in": _admin_when(a["last_login_at"]),
        "Locked": "locked" if a["locked"] else "", "Two-step": "on" if a["two_step"] else "",
        "AI limits": "none" if a["ai_unlimited"] else "",
    } for a in shown]), hide_index=True, width="stretch")

    pick = st.selectbox("Open an account", [a["id"] for a in shown], index=None,
                        format_func=lambda i: by_id[i]["username"], key="admin_pick",
                        placeholder="Pick an account to manage it")
    if pick in by_id:
        a = by_id[pick]
        with st.container(border=True):
            st.markdown(f"#### {a['username']}")
            facts = [f"Role: **{a['role']}**",
                     f"made {'by themselves' if a['signed_up'] else 'by an admin or advisor'} "
                     f"on {_admin_when(a['created_at'])[:10]}",
                     f"last sign-in {_admin_when(a['last_login_at'])}"]
            if a["email"]:
                facts.append("email confirmed" if a["confirmed"] else "email not confirmed yet")
            if a["advisor"]:
                facts.append(f"client of **{a['advisor']}**")
            if a["clients"]:
                facts.append(f"**{a['clients']}** client{'s' if a['clients'] != 1 else ''}")
            facts.append("two-step sign-in on" if a["two_step"] else "two-step sign-in off")
            st.markdown(" · ".join(facts))
            if temp and temp[0] == pick:
                st.code(temp[1], language=None)
                st.caption("Temporary password - shown only now. Pass it on privately; they can "
                           "change it under Change password.")
            with st.container(horizontal=True):
                if a["email"]:
                    st.button("Send password reset email", key="admin_reset",
                              on_click=_admin_reset_link, args=(pick,))
                else:
                    st.button("Set a temporary password", key="admin_temp_pw",
                              on_click=_admin_temp_password, args=(pick,))
                if a["locked"]:
                    st.button("Unlock", key="admin_unlock", on_click=_admin_unlock,
                              args=(a["username"],))
                if not a["is_admin"]:
                    st.button("Remove advisor" if a["is_advisor"] else "Make advisor",
                              key="admin_adv", on_click=_admin_advisor,
                              args=(a["username"], not a["is_advisor"]))
                st.button("Normal AI limits" if a["ai_unlimited"] else "No AI limits",
                          key="admin_ai", on_click=_admin_ai,
                          args=(pick, a["username"], not a["ai_unlimited"]))
            if not a["is_advisor"] and not a["is_admin"]:
                if a["advisor"]:
                    st.button(f"Unlink from {a['advisor']}", key="admin_unlink", type="tertiary",
                              on_click=_admin_unlink, args=(a["advisor"], pick, a["username"]))
                elif advisors:
                    with st.container(horizontal=True, vertical_alignment="bottom"):
                        st.selectbox("Make this a client of", [x["id"] for x in advisors],
                                     index=None, key=f"admin_link_to_{pick}",
                                     format_func=lambda i: by_id[i]["username"],
                                     placeholder="Pick an advisor")
                        st.button("Link", key="admin_link", on_click=_admin_link,
                                  args=(pick, a["username"]))
            if a["two_step"]:
                with st.expander("Reset two-step sign-in"):
                    st.caption("For someone who lost their phone and their backup codes. Only "
                               "do this once you're sure it's really them (for example, they "
                               "wrote from the account's email). It turns two-step off and "
                               "signs them out everywhere; advisors and admins set it up again "
                               "as soon as they sign in. Their key is never shown here.")
                    st.button("Reset two-step sign-in", key="admin_two_step_reset",
                              on_click=_admin_reset_two_step, args=(pick, a["username"]))
            if not a["is_admin"]:
                with st.expander("Delete this account"):
                    st.caption("Deletes the login and everything it holds - holdings, plan, "
                               "profile, notes, settings. This can't be undone."
                               + (f" Its {a['clients']} client(s) keep their accounts but no "
                                  "longer have an advisor." if a["clients"] else ""))
                    st.text_input(f"Type {a['username']} to confirm",
                                  key=f"admin_del_confirm_{pick}")
                    st.button("Delete account", key="admin_delete", type="primary",
                              on_click=_admin_delete, args=(pick, a["username"]))

    # ---- add an account -------------------------------------------------- #
    with st.expander("Add an account"):
        st.text_input("Email address or username", key="admin_new_login",
                      help="With an email address they get a link to choose their own "
                           "password. A username gets a temporary password shown here once.")
        role = st.segmented_control("Kind of account", ["Investor", "Advisor"],
                                    default="Investor", key="admin_new_role")
        if role != "Advisor" and advisors:
            st.selectbox("Client of (optional)", [x["id"] for x in advisors], index=None,
                         key="admin_new_client_of", format_func=lambda i: by_id[i]["username"],
                         placeholder="No advisor")
        st.button("Create account", key="admin_create", type="primary", on_click=_admin_create)

    # ---- AI use ---------------------------------------------------------- #
    st.subheader(f"AI use in {ai_usage.month_of()}", anchor=False)
    if usage:
        st.dataframe(pd.DataFrame([{"Account": r["username"], "Feature": r["kind"],
                                    "Used": r["used"]} for r in usage]),
                     hide_index=True, width="stretch")
    else:
        st.caption("No AI use yet this month.")
