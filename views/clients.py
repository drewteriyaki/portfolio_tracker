# Part of dashboard.py, which runs this file with _view("clients") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# Advisor side: notes and next steps, the advisor card, model portfolios,
# the Clients page, and the weekly summary notice.
# ruff: noqa: F821

# ---- advisor notes, the advisor card, the clients page ------------------------ #
_NOTE_ICON = {"Review": ":material/event:", "Note": ":material/notes:",
              "Next step": ":material/flag:"}


def _render_advisor_card(card):
    """How a managed client sees their advisor: name, firm, contact, message."""
    name = card.get("name") or card.get("username") or "Your advisor"
    contact = " · ".join(v for v in (card.get("email"), card.get("phone")) if v)
    with st.container(border=True):
        _md(f"**Your advisor: {name}**" + (f" · {card['firm']}" if card.get("firm") else ""))
        if contact:
            st.caption(contact)
        if card.get("message"):
            _md(card["message"])


def _notes_for_view(conn):
    """This account's advisor notes as the viewer may see them: an advisor on a
    client's account sees private ones too; the client never does."""
    return advising.list_notes(conn, USER_ID, include_private=ON_CLIENT)


def _render_notes():
    today = datetime.now().date()
    conn = connect(DB)
    try:
        notes = _notes_for_view(conn)
    finally:
        conn.close()
    if IS_MANAGED_CLIENT:
        _render_advisor_card(MY_ADVISOR_CARD)

    if ON_CLIENT:
        with st.expander("Add a note", expanded=not notes):
            with st.form("note_form", clear_on_submit=True, border=False):
                c1, c2 = st.columns([2, 1])
                kind = c1.segmented_control("Type", advising.NOTE_KINDS, default="Note",
                                            help="A Review is a meeting - the latest one is "
                                                 "this client's last review. A Next step is "
                                                 "something to do; tick it off when it's done.")
                on = c2.date_input("Date", value=today, max_value=today)
                body = st.text_area("Note", placeholder="e.g. Reviewed the plan together; "
                                                        "moving the monthly amount to $600.")
                private = st.checkbox("Private - only you see this, never the client")
                if st.form_submit_button("Save note", type="primary"):
                    c = connect(DB)
                    try:
                        advising.add_note(c, USER_ID, LOGIN_ID, kind or "Note", body,
                                          on.isoformat(), private)
                    except ValueError as exc:
                        st.error(str(exc).capitalize() + ".")
                    else:
                        st.rerun()
                    finally:
                        c.close()

    def _set_done(note_id, done):
        c = connect(DB)
        try:
            advising.set_done(c, USER_ID, note_id, done)
        finally:
            c.close()

    def _delete(note_id):
        c = connect(DB)
        try:
            advising.delete_note(c, USER_ID, note_id)
        finally:
            c.close()

    steps = advising.open_next_steps(notes)
    st.markdown("#### Next steps")
    if not steps:
        st.caption("No open next steps." if notes or ON_CLIENT else
                   "Nothing here yet - your advisor's next steps for you show up here.")
    for n in steps:
        with st.container(border=True):
            _md(f":material/flag: {n['body']}")
            with st.container(horizontal=True, vertical_alignment="center"):
                st.caption(f"From {_fmt_date(n['note_date'])}"
                           + (" · Private" if n["private"] else ""))
                if ON_CLIENT:
                    st.button("Mark done", key=f"note_done_{n['id']}", type="tertiary",
                              on_click=_set_done, args=(n["id"], True))

    st.markdown("#### Timeline")
    if not notes:
        st.caption("No notes yet." if ON_CLIENT else "Notes from your advisor show up here.")
    for n in notes:
        label = n["kind"] + (" (done)" if n["kind"] == "Next step" and n["done"] else "")
        with st.container(border=True):
            st.caption(f"{_NOTE_ICON.get(n['kind'], '')} **{label}** · {_fmt_date(n['note_date'])}"
                       + (" · :orange[Private]" if n["private"] else ""))
            _md(n["body"])
            if ON_CLIENT:
                with st.container(horizontal=True):
                    if n["kind"] == "Next step" and n["done"]:
                        st.button("Reopen", key=f"note_undo_{n['id']}", type="tertiary",
                                  on_click=_set_done, args=(n["id"], False))
                    st.button("Delete", key=f"note_del_{n['id']}", type="tertiary",
                              on_click=_delete, args=(n["id"],))
    if ON_CLIENT:
        st.caption("The client sees everything here except private notes.")


def _advisor_notes_card():
    """Dashboard line for an account with an advisor: the latest note and the
    open next steps, linking to Advisor notes."""
    conn = connect(DB)
    try:
        notes = _notes_for_view(conn)
        last = advising.last_review(conn, USER_ID)
    finally:
        conn.close()
    steps = advising.open_next_steps(notes)
    with st.container(border=True, horizontal=True, vertical_alignment="center"):
        if ON_CLIENT:
            review, days = advising.review_status(last, datetime.now().date())
            text = ("No review yet" if review == "never" else
                    f"Last review {days} day{'s' if days != 1 else ''} ago"
                    + (" - due" if review == "due" else ""))
        else:
            latest = next((n for n in notes), None)
            first_line = (latest["body"].splitlines() or [""])[0] if latest else ""
            text = (f"**From {_advisor_display_name()}**: "
                    + (first_line[:90] + ("…" if len(first_line) > 90 else "") if latest
                       else "no notes yet"))
        text += f" · {len(steps)} open next step{'s' if len(steps) != 1 else ''}" if steps else ""
        st.markdown(text.replace("$", r"\$"), width="stretch")
        st.button("Advisor notes", key="dash_notes", type="tertiary", on_click=_go,
                  args=("Advisor notes",))


def _render_advisor_settings():
    """The advisor's own card as clients see it, stored in their settings."""
    conn = connect(DB)
    try:
        p = prefs.load(conn, LOGIN_ID)
    finally:
        conn.close()
    card = p.get("advisor_card") or {}
    with st.expander("How clients see you"):
        with st.form("advisor_card_form", border=False):
            c1, c2 = st.columns(2)
            name = c1.text_input("Your name", value=card.get("name") or "", max_chars=60,
                                 placeholder=st.session_state["username"])
            firm = c2.text_input("Firm (optional)", value=card.get("firm") or "", max_chars=80)
            email = c1.text_input("Email (optional)", value=card.get("email") or "", max_chars=100)
            phone = c2.text_input("Phone (optional)", value=card.get("phone") or "", max_chars=40)
            message = st.text_area("A note for your clients (optional)", max_chars=300,
                                   value=card.get("message") or "",
                                   placeholder="e.g. Questions any time - I'll reply within a day.")
            if st.form_submit_button("Save", type="primary"):
                p["advisor_card"] = {k: v.strip() for k, v in (
                    ("name", name), ("firm", firm), ("email", email), ("phone", phone),
                    ("message", message)) if v.strip()}
                c = connect(DB)
                try:
                    prefs.save(c, LOGIN_ID, p)
                finally:
                    c.close()
                st.toast("Saved - your clients see this on their Advisor notes page.")
        st.caption("Shown to clients whose accounts you manage: on their **Your advisor** page "
                   "and in the menu under their name.")
        st.session_state["weekly_email_on"] = not p.get("weekly_email_off")  # what's saved
        st.toggle("Monday email", key="weekly_email_on", on_change=_set_weekly_email,
                  help="A short email on Monday mornings when reviews are due or a client "
                       "accepted a proposal - counts only, no client names or figures. Sent to "
                       "your login email once it's confirmed.")


def _set_weekly_email():
    c = connect(DB)
    try:
        p = prefs.load(c, LOGIN_ID)
        if st.session_state.get("weekly_email_on"):
            p.pop("weekly_email_off", None)
        else:
            p["weekly_email_off"] = True
        prefs.save(c, LOGIN_ID, p)
    finally:
        c.close()


def _render_models():
    st.subheader("Model portfolios")
    conn = connect(DB)
    try:
        models = advising.list_models(conn, LOGIN_ID)
    finally:
        conn.close()

    def _delete_model(model_id):
        c = connect(DB)
        try:
            advising.delete_model(c, LOGIN_ID, model_id)
        finally:
            c.close()

    if not models:
        st.caption("Save a target mix you use often, then apply it to any client from their "
                   "Plan page.")
    for m in models:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{m['name']}** - " + (advising.mix_text(m["target_alloc"]) or
                        "*no targets - the old mix used ETF / CEF or Mutual Funds, which can't be "
                        "moved to stocks / bonds. Save it again under the same name.*"),
                        width="stretch")
            st.button(":material/delete:", key=f"model_del_{m['id']}", type="tertiary",
                      on_click=_delete_model, args=(m["id"],), help="Delete this model")
    with st.expander("New model portfolio"):
        with st.form("model_form", border=False):
            name = st.text_input("Name", max_chars=60, placeholder="e.g. Balanced 60/40",
                                 help="Saving with an existing name replaces that model.")
            cols = st.columns(len(advising.MODEL_ASSET_TYPES))
            mix = {t: cols[i].number_input(f"{t} %", min_value=0.0, max_value=100.0,
                                           step=5.0, format="%.0f", key=f"model_{t}")
                   for i, t in enumerate(advising.MODEL_ASSET_TYPES)}
            if st.form_submit_button("Save model", type="primary"):
                c = connect(DB)
                try:
                    advising.save_model(c, LOGIN_ID, name, mix)
                except ValueError as exc:
                    st.error(str(exc).capitalize() + ".")
                else:
                    st.rerun()
                finally:
                    c.close()
        st.caption("Targets are by what holdings hold: a stock fund counts as stocks, a bond "
                   "fund as bonds, and a balanced fund is split between them.")


def _set_can_import(client_id):
    allowed = bool(st.session_state.get(f"can_import_{client_id}"))
    c = connect(DB)
    try:
        # set_client_can_import only touches the advisor's own clients
        advising.set_client_can_import(c, st.session_state["user_id"], client_id, allowed)
    finally:
        c.close()


def _client_rows(today):
    """One row per client of this advisor: their summary, goal, review and
    why they need a look - the Clients page and the weekly summary."""
    import overview

    conn = connect(DB)
    try:
        # only the tickers these clients hold
        _ids = tuple(cid for cid, _ in CLIENTS)
        _in = ", ".join("?" for _ in _ids)
        quotes = overview.latest_quotes(conn, [r["symbol"] for r in conn.execute(
            f"SELECT DISTINCT symbol FROM positions WHERE user_id IN ({_in})", _ids)])
        # read for the whole book at once, not once per client
        logins, emails = {}, {}
        for r in conn.execute(f"SELECT id, last_login_at, email FROM users WHERE id IN ({_in})",
                              _ids):
            logins[r["id"]], emails[r["id"]] = r["last_login_at"], r["email"]
        all_props = {}
        for r in conn.execute("SELECT client_id, status, COUNT(*) AS n FROM proposals WHERE "
                              f"client_id IN ({_in}) AND status IN ('shared', 'accepted') "
                              "GROUP BY client_id, status", _ids):
            all_props.setdefault(r["client_id"], {})[r["status"]] = r["n"]
        last_reports = {r["client_id"]: r["period_label"] for r in conn.execute(
            "SELECT client_id, period_label FROM progress_reports p WHERE id = (SELECT MAX(id) "
            f"FROM progress_reports q WHERE q.client_id = p.client_id) AND client_id IN ({_in})",
            _ids)}
        can_import = advising.clients_can_import(conn, _ids)
        # their settings (alert limits, asset-class choices), summaries, plans
        # and notes: each read once for the whole book
        saved = prefs.load_many(conn, _ids, _legacy_prefs_path)
        summaries = overview.account_summaries(
            conn, _ids, quotes, {cid: _rules_from(saved[cid]) for cid in _ids},
            overrides={cid: asset_classes.overrides_in(saved[cid]) for cid in _ids})
        all_plans = plans.get_plans(conn, _ids)
        all_notes = advising.notes_for(conn, _ids, include_private=True)
        rows = []
        for cid, name in CLIENTS:
            summ = summaries[cid]
            plan = all_plans[cid]
            goal = (plans.progress(plan, summ["portfolio_value"] or 0.0, today=today)
                    if plans.has_goal(plan) else None)
            drift = (advising.max_drift(summ["alloc_pct"], (plan or {}).get("target_alloc"))
                     if summ["has_data"] else None)
            notes = all_notes[cid]
            review, days = advising.review_status(advising.last_review_in(notes), today)
            steps = advising.open_next_steps(notes)
            login = logins.get(cid)
            login_days = ((today - date.fromisoformat(login[:10])).days if login else None)
            props = all_props.get(cid, {})
            rows.append({**summ, "name": name, "email": emails.get(cid), "plan": plan,
                         "goal": goal, "drift": drift,
                         "can_import": cid in can_import,
                         "review": review, "review_days": days, "n_steps": len(steps),
                         "login_days": login_days, "proposals": props,
                         "last_report": last_reports.get(cid),
                         "reasons": advising.attention(
                             has_data=summ["has_data"],
                             goal_status=goal["status"] if goal else None, review=review,
                             n_alerts=summ["n_alerts_attention"], drift=drift,
                             profile_done=summ["profile_answered"] >= summ["profile_total"],
                             proposal_accepted=bool(props.get("accepted")),
                             days_since_login=login_days)})
    finally:
        conn.close()
    # who needs a look first, then the biggest accounts
    rows.sort(key=lambda r: (-len(r["reasons"]), -(r["portfolio_value"] or 0.0)))
    return rows


def _client_msg():
    """The message from Add client, renaming or a send - with an Open button
    for the client it's about."""
    msg = st.session_state.pop("client_msg", None)
    if not msg:
        return
    getattr(st, msg[0])(msg[1])
    if len(msg) > 2 and msg[2] in dict(CLIENTS):
        st.button(f"Open {dict(CLIENTS)[msg[2]]}", key="client_msg_open", icon=":material/login:",
                  on_click=_open_client, args=(msg[2],))


def _render_add_client():
    """Add client, at the top of Your clients: their name or household and
    their email; with an email, the setup link goes out in the same step.
    Before the first invite, the advisor's own name and firm (who it's from)."""
    _client_msg()
    with st.expander(":material/person_add: Add client", expanded=not CLIENTS):
        c1, c2 = st.columns(2)
        c1.text_input("Name or household", key="new_client_name", max_chars=auth.CLIENT_NAME_MAX,
                      placeholder="e.g. Dana Lee or Chen household",
                      help="How they're listed for you. Only you see it; you can change it "
                           "later.")
        c2.text_input("Their email", key="new_client_email", max_chars=100,
                      placeholder="name@example.com",
                      help="Where the setup link goes. Leave it blank to manage the account "
                           "yourself for now.")
        has_email = bool((st.session_state.get("new_client_email") or "").strip())
        invite = st.checkbox("Email them a setup link", value=True, key="new_client_invite",
                             disabled=not has_email,
                             help="They choose their own password - you never see it - then "
                                  "answer a few questions about their goals before your first "
                                  "meeting.") and has_email
        if invite:
            conn = connect(DB)
            try:
                card = prefs.load(conn, LOGIN_ID).get("advisor_card") or {}
            finally:
                conn.close()
            if not card.get("name"):
                st.markdown("**Who's it from?** Your name and firm go in the invite, so they "
                            "know it's you.")
                c1, c2 = st.columns(2)
                c1.text_input("Your name", key="new_adv_name", max_chars=60,
                              placeholder="e.g. Dana Ruiz")
                c2.text_input("Firm (optional)", key="new_adv_firm", max_chars=80,
                              placeholder="e.g. Ruiz Wealth")
                st.caption("Saved under **How clients see you** at the bottom of this page, "
                           "where you can change it any time.")
        st.button("Add and send invite" if invite else "Add client", key="add_client",
                  on_click=_add_client, type="primary")


def _rename_client(client_id):
    name = st.session_state.get(f"rename_{client_id}")
    c = connect(DB)
    try:
        ok = auth.set_client_name(c, st.session_state["user_id"], client_id, name)
    finally:
        c.close()
    if ok:
        st.session_state["client_msg"] = ("success", "Saved the new name."
                                          if auth.clean_client_name(name) else
                                          "Name cleared - they're listed by their own name or "
                                          "login.")


def _send_message():
    """Message clients: a Note on each picked client's Your advisor page, and
    a short email to those who can sign in and have a confirmed email - it
    only says there's a message (no text, no figures)."""
    viewer = st.session_state["user_id"]
    picked = st.session_state.get("msg_clients") or []
    body = st.session_state.get("msg_body") or ""
    st.session_state["msg_confirm"] = False
    c = connect(DB)
    try:
        res = advising.message_clients(c, viewer, picked, body,
                                       now=datetime.now(timezone.utc),
                                       today=datetime.now().date())
        to_email = []
        if res["ok"]:
            ids = tuple(res["sent_to"])
            to_email = [r["email"] for r in c.execute(
                "SELECT email FROM users WHERE id IN (" + ", ".join("?" for _ in ids) + ") "
                "AND email IS NOT NULL AND email_verified_at IS NOT NULL "
                "AND last_login_at IS NOT NULL ORDER BY id", ids)]
        card = prefs.load(c, viewer).get("advisor_card") or {}
    finally:
        c.close()
    if not res["ok"]:
        st.session_state["msg_result"] = ("warning", res["error"])
        return
    body_name, from_name = _advisor_names(card, st.session_state["username"])
    emailed = 0
    for i, email in enumerate(to_email):
        if i:
            time.sleep(0.6)   # the email service takes a couple a second
        emailed += bool(mailer.advisor_message(email, f"{_app_address()}?page=your-advisor",
                                               body_name, from_name=from_name))
    n, in_app = len(res["sent_to"]), len(res["sent_to"]) - emailed
    text = (f"Sent to {n} client{'s' if n != 1 else ''} - it's on their Advisor notes page. "
            + (f"Emailed {emailed} that it's there. " if emailed else "")
            + (f"{in_app} will see it next time they sign in "
               "(no confirmed email or login yet" + (", or the email couldn't be sent"
                                                    if len(to_email) > emailed else "") + ")."
               if in_app else ""))
    st.session_state["msg_result"] = ("success", text.strip())
    st.session_state["msg_body"] = ""


def _render_message_clients(rows):
    """Your clients: one message to all clients (or some), checked before it goes."""
    with st.expander(":material/campaign: Message clients"):
        res = st.session_state.pop("msg_result", None)
        if res:
            getattr(st, res[0])(res[1])
        names = {r["user_id"]: r["name"] for r in rows}
        if "msg_clients" not in st.session_state:
            st.session_state["msg_clients"] = list(names)
        st.session_state["msg_clients"] = [i for i in st.session_state["msg_clients"]
                                           if i in names]
        st.multiselect("To", list(names), key="msg_clients", format_func=names.get,
                       placeholder="Pick clients",
                       on_change=lambda: st.session_state.update(msg_confirm=False))
        st.text_area("Message", key="msg_body", max_chars=advising.MESSAGE_MAX,
                     placeholder="e.g. Markets have been bumpy this week. Your plan already "
                                 "allows for this - no need to do anything. Happy to talk any "
                                 "time.",
                     on_change=lambda: st.session_state.update(msg_confirm=False))
        n = len(st.session_state.get("msg_clients") or [])
        ready = n and (st.session_state.get("msg_body") or "").strip()
        if st.session_state.get("msg_confirm") and ready:
            st.warning(f"Send this message to {n} client{'s' if n != 1 else ''}? It's added to "
                       "their Advisor notes page, and those with a confirmed email get a short "
                       "note that it's there.")
            with st.container(horizontal=True):
                st.button(f"Yes, send to {n}", key="msg_send", type="primary",
                          on_click=_send_message)
                st.button("Cancel", key="msg_cancel", type="tertiary",
                          on_click=lambda: st.session_state.update(msg_confirm=False))
        else:
            st.button(f"Review and send to {n} client{'s' if n != 1 else ''}", key="msg_review",
                      type="primary", disabled=not ready,
                      on_click=lambda: st.session_state.update(msg_confirm=True))
        st.caption("Each client sees it as a note from you on their Advisor notes page. The "
                   "email only says there's a message - the text stays in Northwend.")


def _render_clients():
    today = datetime.now().date()
    _render_add_client()
    if not CLIENTS:
        st.info("No clients yet - add your first one with **Add client** just above.")
    else:
        rows = _client_rows(today)
        _week_seen()  # the Clients page shows this week's summary itself
        _summary = advising.weekly_summary(rows)
        with st.expander(":material/event_upcoming: This week", expanded=_summary["any"]):
            _render_week_summary(_summary, where="clients")

        st.html(_stat_row(
                "<div class='pt-stats' role='list' aria-label='Client summary'>"
                f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Clients</div>"
                f"<div class='pt-stat-value'>{len(rows)}</div></div>"
                f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Total value</div>"
                f"<div class='pt-stat-value'>{fmt_money0(sum(r['portfolio_value'] or 0 for r in rows))}"
                "</div></div>"
                f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Need attention</div>"
                f"<div class='pt-stat-value'>{sum(1 for r in rows if r['reasons'])}</div>"
                f"<div class='pt-stat-sub'>{sum(1 for r in rows if r['review'] != 'ok')} review(s) due"
                "</div></div></div>"))

        _render_reports_bulk(rows)
        _render_message_clients(rows)

        # narrow the book down (ROADMAP G9)
        with st.container(horizontal=True, vertical_alignment="bottom"):
            show = st.segmented_control(
                "Show", ["Everyone", "Needs a look", "Review due", "Waiting on you"],
                default="Everyone", key="book_show",
                help="Waiting on you: an accepted proposal to act on, or open next steps."
            ) or "Everyone"
            find = st.text_input("Find a client", key="book_find", placeholder="Find a client",
                                 label_visibility="collapsed")
        wanted = {
            "Everyone": lambda r: True,
            "Needs a look": lambda r: bool(r["reasons"]),
            "Review due": lambda r: r["review"] != "ok",
            "Waiting on you": lambda r: bool(r["proposals"].get("accepted") or r["n_steps"]),
        }[show]
        _find = find.strip().lower()
        rows = [r for r in rows if wanted(r) and (_find in r["name"].lower()
                                                   or _find in (r["email"] or "").lower())]
        if not rows:
            st.caption("No clients match.")

        cols = st.columns(2)
        for i, r in enumerate(rows):
            with cols[i % 2], st.container(border=True):
                gain = r["gain_pct"]
                chips = "".join(f"<span class='pt-chip pt-warn'>{html.escape(x)}</span> "
                                for x in r["reasons"]) or "<span class='pt-chip pt-up'>All good</span>"
                bits = []
                if r["goal"]:
                    g, plan = r["goal"], r["plan"]
                    goal_name = html.escape(plan.get("goal_name") or plan["goal_type"] or "Goal")
                    pct_txt = mask_or(f"{g['pct_of_target'] or 0:.0f}%")
                    bits.append(f"{goal_name}: {pct_txt} of {fmt_money0(g['target'])}, "
                                f"{PLAN_STATUS[g['status']][0].lower()}")
                bits.append("never reviewed" if r["review"] == "never"
                            else f"reviewed {r['review_days']}d ago")
                if r["n_steps"]:
                    bits.append(f"{r['n_steps']} open next step{'s' if r['n_steps'] != 1 else ''}")
                if r["proposals"].get("shared"):
                    bits.append("proposal waiting for their answer")
                if r["snapshot_date"]:
                    bits.append(f"statement {_fmt_date(r['snapshot_date'])}")
                if r["last_report"]:
                    bits.append(f"last report {r['last_report']}")
                if r["login_days"] is not None:
                    bits.append("signed in today" if r["login_days"] == 0
                                else f"signed in {r['login_days']}d ago")
                else:
                    bits.append("login not set up yet")
                # the email as plain text (st.html: no mailto link), under the name
                email = (r["email"] if r["email"] and r["email"] != r["name"] else "")
                st.html(
                    "<div class='pt-goal-top'>"
                    f"<b>{html.escape(r['name'])}</b>"
                    + (f"<span>{fmt_money0(r['portfolio_value'])}</span>"
                       f"{_tone(gain, fmt_pct(gain)) if gain is not None else ''}"
                       if r["has_data"] else "<span class='pt-muted'>no statement yet</span>")
                    + "</div>"
                    + (f"<div class='pt-goal-sub'>{html.escape(email)}</div>" if email else "")
                    + f"<div style='margin:.45rem 0'>{chips}</div>"
                    f"<div class='pt-goal-sub'>{' · '.join(bits)}</div>")
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.button("Open", key=f"open_client_{r['user_id']}", on_click=_open_client,
                              args=(r["user_id"],))
                    with st.popover("Rename", type="tertiary", width=110):
                        st.text_input("Name or household", value=r["name"],
                                      key=f"rename_{r['user_id']}",
                                      max_chars=auth.CLIENT_NAME_MAX,
                                      help="Only you see it. Leave it blank to go back to "
                                           "their own name or login.")
                        st.button("Save name", key=f"rename_save_{r['user_id']}",
                                  type="primary", on_click=_rename_client, args=(r["user_id"],))
                    key = f"can_import_{r['user_id']}"
                    st.session_state[key] = r["can_import"]  # always what's saved
                    st.toggle("Client can import", key=key, on_change=_set_can_import,
                              args=(r["user_id"],),
                              help="Let this client import their own statements. Their plan, "
                                   "goal, target mix and alert limits stay yours to set.")
        st.caption(f"Sorted by what needs a look. Reviews are due {advising.REVIEW_EVERY_DAYS} days "
                   f"after the last one; drift is flagged past {advising.DRIFT_ATTENTION_PTS:g} "
                   "points from the plan's target mix; alerts count holdings past the "
                   "client's own day-move limit, or down past their gain/loss limit (gains "
                   "don't count); a "
                   f"client who used to sign in is flagged after {advising.INACTIVE_DAYS} days "
                   "away.")
    st.divider()
    _render_models()
    st.divider()
    _render_advisor_settings()


WEEK_LIST_MAX = 5  # clients listed per group in the weekly summary; the rest are counted


def _week_seen():
    """This week's summary was seen (per advisor login, on any device)."""
    week = advising.week_of(datetime.now().date())
    if st.session_state.get("week_seen") == week:
        return
    _save_login_pref("week_seen", week)
    st.session_state["week_seen"] = week


def _open_from_summary(client_id):
    _week_seen()
    _open_client(client_id)


def _render_week_summary(summary, *, where):
    """Each client who needs a look this week, once, with all their reasons
    (reviews due first, then coming due, then the rest), each with Open."""
    if not summary["any"]:
        st.markdown(":material/check_circle: Nothing due this week - every review is up "
                    "to date and no client needs a look.")
        return
    items = summary["clients"]
    for r in items[:WEEK_LIST_MAX]:
        with st.container(horizontal=True, vertical_alignment="center"):
            # plain text (st.html), so a client listed by email isn't a mailto link
            st.html(f"<b>{html.escape(r['name'])}</b> - {html.escape(', '.join(r['why']))}",
                    width="stretch")
            st.button("Open", key=f"wk_{where}_{r['user_id']}", type="tertiary",
                      on_click=_open_from_summary, args=(r["user_id"],),
                      help=f"Open {r['name']}'s portfolio")
    if len(items) > WEEK_LIST_MAX:
        st.caption(f"and {len(items) - WEEK_LIST_MAX} more on Your clients."
                   if where != "clients" else
                   f"and {len(items) - WEEK_LIST_MAX} more - they're first in the list below.")


# Advisors: once a week (from Monday), the first visit opens with this week's
# reviews and who needs a look; "Got it" hides it until next week. Nothing is
# shown in a week with nothing to say. The Clients page always has it.
if IS_ADVISOR and CLIENTS and PAGE != "Clients":
    _week = advising.week_of(datetime.now().date())
    if "week_seen" not in st.session_state:
        _wc = connect(DB)
        try:
            st.session_state["week_seen"] = prefs.load(_wc, LOGIN_ID).get("week_seen")
        finally:
            _wc.close()
    if st.session_state["week_seen"] != _week:
        # worked out once per session, not on every rerun (live prices rerun the page)
        if st.session_state.get("week_summary", (None,))[0] != _week:
            st.session_state["week_summary"] = (
                _week, advising.weekly_summary(_client_rows(datetime.now().date())))
        _summary = st.session_state["week_summary"][1]
        if _summary["any"]:
            with st.container(border=True, key="week_notice"):
                st.markdown(f":material/event_upcoming: **This week** - "
                            f"{len(_summary['due'])} review{'s' if len(_summary['due']) != 1 else ''}"
                            f" due, {len(_summary['soon'])} coming up, "
                            f"{len(_summary['others'])} other client"
                            f"{'s' if len(_summary['others']) != 1 else ''} to look at.")
                _render_week_summary(_summary, where="notice")
                with st.container(horizontal=True):
                    st.button("Your clients", key="week_clients", type="primary",
                              on_click=lambda: (_week_seen(), _go("Clients")))
                    st.button("Got it", key="week_ok", type="tertiary", on_click=_week_seen,
                              help="Hide this until next week")
