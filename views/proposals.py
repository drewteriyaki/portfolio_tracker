# Part of dashboard.py, which runs this file with _view("proposals") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# Advisor proposals (ROADMAP G4, proposals.py): on a client's Plan page the
# advisor drafts a mix, compares it with today's, and shares it; the client
# answers it on their Advisor notes page.
# ruff: noqa: F821

import proposals


def _prop_advisor_ok(c):
    """The signed-in advisor working on a client they may view."""
    viewer, target = st.session_state["user_id"], st.session_state["active_user_id"]
    return viewer != target and auth.is_advisor(c, viewer) and auth.can_view(c, viewer, target)


def _prop_msg(kind, text):
    st.session_state["prop_msg"] = (kind, text)


def _prop_who_for_client(c, client_id):
    """Before telling a client about a shared proposal: (their email or None,
    the advisor's name as the client knows it, what to tell the advisor if no
    email can go)."""
    who = proposals.who_to_tell(c, client_id)
    card = prefs.load(c, st.session_state["user_id"]).get("advisor_card") or {}
    name = card.get("name") or st.session_state["username"]
    if who["email"]:
        return who["email"], name, ""
    if not who["signed_in"]:
        return None, name, (" They haven't set up their login yet, so no email went - send "
                            "them a setup link from Client login at the top of their pages.")
    return None, name, " They'll see it next time they sign in."


def _prop_tell_client(email, advisor_name, note):
    """Email the client that a proposal is waiting - no figures. What to add
    to the advisor's message."""
    if not email:
        return note
    if mailer.proposal_shared(email, f"{_app_address()}?page=your-advisor", advisor_name):
        return f" We emailed {email} to let them know."
    return " (The email to let them know couldn't be sent.)"


def _prop_start_from(mixes):
    """Fill the new-proposal inputs from the chosen starting point."""
    pick = st.session_state.get("prop_start")
    mix = mixes.get(pick) or {}
    for cls in asset_classes.CLASSES:
        st.session_state[f"prop_mix_{cls}"] = float(round(mix.get(cls, 0.0)))


def _prop_save(share):
    mix = {cls: st.session_state.get(f"prop_mix_{cls}") or 0.0 for cls in asset_classes.CLASSES}
    tell = None
    c = connect(DB)
    try:
        if not _prop_advisor_ok(c):
            _prop_msg("error", "Only this client's advisor can make a proposal.")
            return
        pid = proposals.save(c, st.session_state["user_id"], st.session_state["active_user_id"],
                             title=st.session_state.get("prop_title") or "",
                             mix=mix, note=st.session_state.get("prop_note") or "")
        if share and proposals.share(c, st.session_state["user_id"], pid):
            tell = _prop_who_for_client(c, st.session_state["active_user_id"])
    except ValueError as exc:
        _prop_msg("error", str(exc))
        return
    finally:
        c.close()
    for k in ("prop_title", "prop_note"):
        st.session_state[k] = ""
    _prop_msg("success", "Proposal shared - it's on their Advisor notes page now."
              + _prop_tell_client(*tell) if share and tell else
              "Saved as a draft - only you can see it until you share it.")


def _prop_act(action, pid, mix=None):
    tell = None
    c = connect(DB)
    try:
        if not _prop_advisor_ok(c):
            _prop_msg("error", "Only this client's advisor can do that.")
            return
        if action == "share":
            if proposals.share(c, st.session_state["user_id"], pid):
                tell = _prop_who_for_client(c, st.session_state["active_user_id"])
        elif action == "delete":
            proposals.delete(c, st.session_state["user_id"], pid)
            _prop_msg("success", "Proposal deleted.")
    finally:
        c.close()
    if action == "share":
        _prop_msg("success", "Shared with your client." + (_prop_tell_client(*tell)
                                                           if tell else ""))
    if action == "target":
        save_alloc_targets(mix)
        _prop_msg("success", "That mix is now this client's target mix.")


def _prop_answer(pid, accept):
    me = st.session_state["user_id"]
    advisor_email = None
    c = connect(DB)
    try:
        ok = proposals.respond(c, me, pid, accept)
        if ok:
            # the advisor hears the same day, by email - no figures
            p = proposals.get(c, pid)
            advisor_email = proposals.who_to_tell(c, p["advisor_id"])["email"] if p else None
            client_name = proposals.who_to_tell(c, me)["name"]
    finally:
        c.close()
    if advisor_email:
        mailer.proposal_answered(advisor_email, f"{_app_address()}?page=plan&client={me}",
                                 client_name, accept)
    _prop_msg("success" if ok else "error",
              ("Thanks - your advisor will see that you'd like to go ahead." if accept else
               "Noted - your advisor will see you'd rather not right now.") if ok else
              "That proposal can't be answered any more.")


def _prop_ask(title):
    st.session_state["coach_prompt"] = (
        f"My advisor proposed a new mix for me called \"{title}\". Help me understand what "
        "changes compared with my current mix, and what questions I could ask my advisor about "
        "it. Explain, don't recommend.")
    st.session_state["page"] = "AI Assistant"


_PROP_STATUS = {"draft": ("Draft - only you see it", "pt-muted"),
                "shared": ("Waiting for an answer", "pt-warn"),
                "accepted": ("Client wants to go ahead", "pt-up"),
                "declined": ("Client said not right now", "pt-muted")}
_PROP_STATUS_CLIENT = {"shared": ("Waiting for your answer", "pt-warn"),
                       "accepted": ("You said let's go ahead", "pt-up"),
                       "declined": ("You said not right now", "pt-muted")}


def _prop_compare(p, today_pct, value):
    plan = load_plan()
    months = (plans.months_until(plan["target_date"], datetime.now().date())
              if plans.has_goal(plan) else None)
    return proposals.compare(today_pct, p["mix"], value=value,
                             monthly=float((plan or {}).get("monthly_contribution") or 0.0),
                             months=months)


def _prop_card(p, cmp, *, as_advisor):
    label, tone = (_PROP_STATUS if as_advisor else _PROP_STATUS_CLIENT).get(
        p["status"], (p["status"], ""))
    with st.container(border=True, key=f"prop_{p['id']}"):
        st.html(f"<b>{html.escape(p['title'])}</b>&nbsp; <span class='pt-chip {tone}'>{label}</span>"
                f"<div class='pt-goal-sub'>{(p.get('shared_at') or p['updated_at'])[:10]}</div>")
        if p.get("note"):
            st.markdown(("**From your advisor:** " if not as_advisor else "**Your note:** ")
                        + p["note"].replace("$", r"\$"))
        st.dataframe(pd.DataFrame([{
            "Asset class": cls,
            "Today": "-" if now is None else mask_or(f"{now:.0f}%"),
            "Proposed": f"{new:.0f}%",
            "Change": "-" if change is None else mask_or(f"{change:+.0f} pts"),
        } for cls, now, new, change in cmp["rows"]]), hide_index=True, width="stretch")
        t_ret, p_ret = cmp["assumed_return"]
        lines = [f"**Assumed long-run return:** "
                 f"{'-' if t_ret is None else f'{t_ret:.1f}%'} today, {p_ret:.1f}% proposed"]
        for year, (now, new) in cmp["hard_years"].items():
            lines.append(f"**In a year like {year}:** about "
                         f"{'-' if now is None else f'{now:+.0f}%'} today, {new:+.0f}% proposed")
        if cmp.get("projected"):
            now, new = cmp["projected"]
            lines.append(f"**At your goal date:** about {fmt_money0(now)} today, "
                         f"{fmt_money0(new)} proposed")
        st.markdown("  \n".join(lines).replace("$", r"\$"))
        st.caption(proposals.ASSUMPTIONS_NOTE)
        with st.container(horizontal=True):
            if as_advisor:
                if p["status"] == "draft":
                    st.button("Share with client", key=f"prop_share_{p['id']}", type="primary",
                              on_click=_prop_act, args=("share", p["id"]))
                if p["status"] == "accepted":
                    st.button("Make this the target mix", key=f"prop_target_{p['id']}",
                              type="primary", on_click=_prop_act,
                              args=("target", p["id"], p["mix"]))
                st.button("Delete", key=f"prop_del_{p['id']}", type="tertiary",
                          on_click=_prop_act, args=("delete", p["id"]))
            elif p["status"] == "shared":
                st.button("Let's go ahead", key=f"prop_yes_{p['id']}", type="primary",
                          on_click=_prop_answer, args=(p["id"], True))
                st.button("Not right now", key=f"prop_no_{p['id']}",
                          on_click=_prop_answer, args=(p["id"], False))
            if not as_advisor:
                st.button(f":material/forum: Ask {GUIDE} to explain", key=f"prop_ask_{p['id']}",
                          type="tertiary", on_click=_prop_ask, args=(p["title"],))
            key = f"prop_pdf_{p['id']}"
            if st.session_state.get(key):
                st.download_button("Download PDF", st.session_state[key],
                                   file_name=f"proposal-{p['id']}.pdf", mime="application/pdf",
                                   key=f"prop_dl_{p['id']}")
            elif st.button("PDF", key=f"prop_mkpdf_{p['id']}", type="tertiary",
                           help="A one-page PDF of this proposal to share or print."):
                c = connect(DB)
                try:
                    adv = auth.get_username(c, p["advisor_id"]) or "your advisor"
                    client = auth.get_username(c, p["client_id"]) or "client"
                finally:
                    c.close()
                st.session_state[key] = proposals.render_pdf(
                    p, cmp if not _hidden() else {**cmp, "projected": None},
                    client_name=client, advisor_name=adv)
                st.rerun()


def _render_proposals_advisor(alloc_rows, value):
    """On a client's Plan page, for their advisor (its own tab there)."""
    st.caption("Propose a mix for this client and compare it with today's. It stays a draft "
               "until you share it; they answer on their Advisor notes page.")
    msg = st.session_state.pop("prop_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])
    today_pct = {r["label"]: r["pct"] or 0.0 for r in (alloc_rows or [])}
    c = connect(DB)
    try:
        # the Target mix tab just above read them, this run (views/plan.py)
        models = _RUN.get("models")
        if models is None:
            models = advising.list_models(c, LOGIN_ID)
        mine = proposals.for_client(c, USER_ID, include_drafts=True)
    finally:
        c.close()
    starts = {"Today's mix": {k: v for k, v in today_pct.items() if v},
              "The current target": load_alloc_targets(),
              **{f"Model: {m['name']}": m["target_alloc"] for m in models if m["target_alloc"]}}
    with st.expander("New proposal", expanded=not mine):
        st.selectbox("Start from", list(starts), index=None, key="prop_start",
                     placeholder="Pick a starting point (optional)",
                     on_change=_prop_start_from, args=(starts,))
        cols = st.columns(len(asset_classes.CLASSES))
        for col, cls in zip(cols, asset_classes.CLASSES):
            col.number_input(f"{cls} %", min_value=0.0, max_value=100.0, step=5.0,
                             key=f"prop_mix_{cls}")
        total = sum(st.session_state.get(f"prop_mix_{cls}") or 0.0
                    for cls in asset_classes.CLASSES)
        st.caption(f"Adds up to {total:g}%" + ("" if abs(total - 100) <= 0.5 else
                                               " - make it total 100%."))
        st.text_input("Title", key="prop_title", placeholder="e.g. A steadier mix for 2029")
        st.text_area("Why - in words your client will read", key="prop_note",
                     placeholder="What changes, and why it fits their goal")
        with st.container(horizontal=True):
            st.button("Save and share", key="prop_save_share", type="primary",
                      on_click=_prop_save, args=(True,))
            st.button("Save as draft", key="prop_save_draft", on_click=_prop_save, args=(False,))
    for p in mine:
        _prop_card(p, _prop_compare(p, today_pct, value), as_advisor=True)


def _render_proposals_client(alloc_rows, value):
    """On a client's Advisor notes page: what their advisor shared."""
    c = connect(DB)
    try:
        shared = proposals.for_client(c, USER_ID, include_drafts=False)
    finally:
        c.close()
    if not shared:
        return
    st.markdown("#### Proposals from your advisor")
    msg = st.session_state.pop("prop_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])
    today_pct = {r["label"]: r["pct"] or 0.0 for r in (alloc_rows or [])}
    for p in shared:
        _prop_card(p, _prop_compare(p, today_pct, value), as_advisor=False)
    st.caption("Going ahead doesn't buy or sell anything here - your advisor takes it from "
               "there with you.")
