# Part of dashboard.py, which runs this file with _view("meeting") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# Meeting prep (ROADMAP G7, meeting.py): at the top of a client's Advisor notes
# page, for their advisor - what changed since the last review, and talking
# points the AI drafts for the advisor to edit and keep as a private note.
# ruff: noqa: F821

import meeting


def _prep_draft(profile, summary, facts):
    import anthropic

    key = _anthropic_key()
    quota = _ai_status("prep")
    if not key:
        st.session_state["prep_msg"] = ("info", "Drafting talking points isn't available on "
                                                "this site - write your own below.")
        return
    if not quota["ok"]:
        st.session_state["prep_msg"] = ("info", ai_usage.used_up_text(quota, "prep"))
        return
    try:
        pts = meeting.talking_points(anthropic.Anthropic(api_key=key), profile, summary, facts)
    except anthropic.AnthropicError as exc:
        st.session_state["prep_msg"] = ("warning", _ai_failed(exc, "prep",
                                                              "Drafting talking points"))
        return
    _ai_record("prep")  # counted once it has answered
    st.session_state[f"prep_points_{USER_ID}"] = "\n".join(f"- {p}" for p in (pts or []))


def _prep_save():
    text = (st.session_state.get(f"prep_points_{USER_ID}") or "").strip()
    if not text:
        st.session_state["prep_msg"] = ("info", "Write or draft some talking points first.")
        return
    c = connect(DB)
    try:
        viewer, target = st.session_state["user_id"], st.session_state["active_user_id"]
        if viewer == target or not auth.can_view(c, viewer, target):
            return
        advising.add_note(c, target, viewer, "Note", "Meeting prep:\n" + text,
                          datetime.now().date().isoformat(), private=True)
    finally:
        c.close()
    st.session_state["prep_msg"] = ("success", "Saved as a private note - only you see it.")


def _render_meeting_prep(value, alloc_rows, contexts, cash):
    """Advisor only, on a client's Advisor notes page."""
    import advisor

    today = datetime.now().date()
    actual = {r["label"]: r["pct"] or 0.0 for r in (alloc_rows or [])}
    c = connect(DB)
    try:
        p = meeting.prep(c, USER_ID, today=today, value=value,
                         latest_snapshot=latest_snapshot(c, USER_ID), actual_pct=actual,
                         targets=load_alloc_targets(), drift_threshold=load_drift_threshold())
        profile = advisor.get_profile(c, USER_ID)
    finally:
        c.close()
    with st.expander(":material/event_note: Meeting prep", expanded=False):
        msg = st.session_state.pop("prep_msg", None)
        if msg:
            getattr(st, msg[0])(msg[1])
        lines = [f"**Last review:** {_fmt_date(p['last_review'][:10])} ({p['days_since']} days "
                 "ago)" if p["last_review"] else "**First review** - no earlier review logged."]
        if p["value_change"] is not None:
            lines.append(f"**Value since then:** {_signed_money(p['value_change'])} "
                         f"({mask_or(format(p['value_change_pct'], '+.1f') + '%')})")
        t = p["trades"]
        if t is not None:
            moves = [f"{label}: {', '.join(t[k])}" for label, k in
                     (("New", "new"), ("Added to", "bought"), ("Reduced", "sold"),
                      ("Sold out", "closed")) if t[k]]
            lines.append("**Changes in holdings:** " + ("; ".join(moves) if moves else "none"))
        g = p["goal"]
        if g:
            label, _tone = PLAN_STATUS[g["status"]]
            share = mask_or(format(g["pct_of_target"] or 0, ".0f") + "%")
            lines.append(f"**Goal:** {label.lower()} - {share} of {fmt_money0(g['target'])}, "
                         f"{_time_left(g['months'])}")
        else:
            lines.append("**Goal:** none set yet")
        for k, a, tgt, d in p["drift"]:
            lines.append(f"**Drift:** {k} {mask_or(format(a, '.0f') + '%')} vs {tgt:.0f}% target "
                         f"({mask_or(format(d, '+.0f'))} pts)")
        if p["next_steps"]:
            lines.append("**Open next steps:** " + "; ".join(
                n["body"].splitlines()[0][:80] for n in p["next_steps"]))
        for pr in p["proposals"]:
            lines.append(f"**Proposal:** {pr['title']} - "
                         + ("accepted" if pr["status"] == "accepted" else "waiting for an answer"))
        _md("  \n".join(lines))

        st.markdown("**Talking points**")
        key = f"prep_points_{USER_ID}"
        st.session_state.setdefault(key, "")
        facts = meeting.facts_for_ai(p)
        summary = advisor.portfolio_summary(contexts or [], cash or {}, CLASS_SPLITS)
        quota = _ai_status("prep")
        with st.container(horizontal=True):
            st.button(f":material/auto_awesome: Draft with {GUIDE}", key="prep_draft",
                      on_click=_prep_draft, args=(profile, summary, facts),
                      disabled=not quota["ok"],
                      help="Sends percentages and the facts above - never dollar amounts or "
                           "your notes. " + (ai_usage.left_text(quota, "prep") or ""))
        st.text_area("Your talking points", key=key, height=180,
                     placeholder="- What to celebrate\n- What to check in about\n- Questions "
                                 "to ask", label_visibility="collapsed")
        st.button("Save as a private note", key="prep_save", on_click=_prep_save)
        st.caption("Drafts are a starting point - edit them; the advice is yours.")
