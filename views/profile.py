# Part of dashboard.py, which runs this file with _view("profile") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# The investing-profile form and the downloadable plan.
# ruff: noqa: F821

# The investing-profile form: every answer is a tap, not typing. Keys match
# advisor.PROFILE_FIELDS; the wording here is just for the form.
PROFILE_QUESTIONS = {
    "goal": "What are you investing for? Pick all that apply.",
    "time_horizon_years": "When will you need most of this money?",
    "target_return_pct": "What yearly return are you hoping for?",
    "risk_tolerance": "How much risk are you comfortable with?",
    "drawdown_reaction": "If your portfolio dropped 20% in a month, you would...",
    "experience": "How much investing experience do you have?",
    "age_range": "Your age",
    "income_stability": "How steady is your income?",
    "emergency_fund": "Emergency savings outside this portfolio",
    "high_interest_debt": "High-interest debt, like credit cards",
    "employer_match": "Does your employer match what you put into a retirement plan?",
    "contributions": "How often will you add money?",
    "withdrawal_needs": "Planning to take money out in the next 3 years?",
    "preferences": "Anything you'd like in your investments? Pick any.",
}
PROFILE_SECTIONS = (
    ("Goals", ("goal", "time_horizon_years", "target_return_pct")),
    ("Comfort with risk", ("risk_tolerance", "drawdown_reaction", "experience")),
    ("Your situation", ("age_range", "income_stability", "emergency_fund",
                        "high_interest_debt", "employer_match", "contributions",
                        "withdrawal_needs")),
    ("Preferences", ("preferences",)),
)
# a section's idea -> where to read more (learn.LEARN_MORE)
PROFILE_SECTION_LINKS = {"Comfort with risk": "risk_tolerance"}
HORIZON_YEARS = (1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 40)
TARGET_RETURNS = (3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 15.0)
_NOT_SET = "Not set"


def _with_current(options, current):
    """`options` plus a saved value that isn't one of them (answers saved
    before the presets existed), so the form still shows it."""
    return sorted({*options, current}) if current is not None and current not in options \
        else list(options)


def _render_profile_form(advisor, profile):
    with st.form("investor_profile_form", border=False):
        answers = {}
        for title, fields in PROFILE_SECTIONS:
            st.markdown(f"**{title}**")
            learn_more(PROFILE_SECTION_LINKS.get(title))
            for field in fields:
                q, cur = PROFILE_QUESTIONS[field], profile.get(field)
                if field in advisor.MULTI_CHOICES:
                    chosen = advisor.split_multi(cur)
                    opts = list(advisor.MULTI_CHOICES[field]) + [c for c in chosen
                                                                 if c not in advisor.MULTI_CHOICES[field]]
                    answers[field] = st.pills(q, opts, selection_mode="multi", default=chosen)
                elif field in advisor.CHOICES:
                    opts = list(advisor.CHOICES[field]) + (
                        [cur] if cur and cur not in advisor.CHOICES[field] else [])
                    answers[field] = st.pills(q, opts, default=cur or None,
                                              format_func=lambda v: v[:1].upper() + v[1:])
                elif field == "time_horizon_years":
                    cur = int(cur) if cur else None
                    answers[field] = st.select_slider(
                        q, [_NOT_SET, *_with_current(HORIZON_YEARS, cur)], value=cur or _NOT_SET,
                        format_func=lambda v: v if v == _NOT_SET else
                        f"{v} year{'s' if v != 1 else ''}{'+' if v == HORIZON_YEARS[-1] else ''}")
                elif field == "target_return_pct":
                    cur = float(cur) if cur else None
                    answers[field] = st.select_slider(
                        q, ["Not sure", *_with_current(TARGET_RETURNS, cur)],
                        value=cur or "Not sure",
                        format_func=lambda v: v if isinstance(v, str) else f"{v:g}%")
        notes = st.text_area("Other notes", value=profile["notes"] or "",
                             placeholder="Anything else worth knowing: a date you're saving "
                                         "toward, accounts elsewhere, investments to avoid...")
        if st.form_submit_button("Save profile", type="primary"):
            fields = {}
            for field, v in answers.items():
                if isinstance(v, list):
                    order = advisor.MULTI_CHOICES[field]
                    v = advisor.MULTI_SEP.join(sorted(
                        v, key=lambda x: order.index(x) if x in order else len(order)))
                fields[field] = None if v in (None, "", _NOT_SET, "Not sure") else v
            fields["notes"] = notes.strip() or None
            conn = connect(DB)
            try:
                advisor.save_profile(conn, USER_ID, fields, replace=True)
            finally:
                conn.close()
            st.session_state["dialog_open"] = False   # the rerun closes a window it's in
            st.rerun()


def _render_plan_export(api_key, profile, memory, contexts, cash_by_account, display,
                        in_window=False):
    """'Client plan' block: one API call for next steps, then a PDF download.
    The PDF lives in session state only, so switching accounts drops it.
    `in_window`: drawn inside a window (Ask Northwend's calm view), not an expander."""
    import advisor
    import anthropic
    import client_plan

    with st.container() if in_window else st.expander("Client plan (PDF)", expanded=False):
        st.caption("A printable plan for this account: profile, allocation, holdings with "
                   "dollar amounts, things to watch, and AI-suggested next steps. The AI only "
                   "sees percentages; the dollar figures are added on this machine.")
        blocked = ("Turn off Hide amounts to create a plan - it includes dollar figures."
                   if _hidden() else
                   "Import positions for this account first." if not contexts else None)
        if st.button("Create plan", disabled=bool(blocked), help=blocked):
            conn = connect(DB)
            try:
                facts = client_plan.build_facts(conn, USER_ID, contexts, cash_by_account,
                                                _rules_for(USER_ID))
            finally:
                conn.close()
            steps = None
            quota = _ai_status("plan")  # this month's allowance (ai_usage.py)
            if not quota["ok"]:
                st.info(ai_usage.used_up_text(quota, "plan") + " This plan was made without "
                        "suggested next steps.")
            else:
                with st.spinner("Writing suggested next steps..."):
                    try:
                        steps = client_plan.next_steps(
                            anthropic.Anthropic(api_key=api_key), profile,
                            advisor.portfolio_summary(contexts, cash_by_account, CLASS_SPLITS),
                            client_plan.chat_transcript(display), memory)
                    except anthropic.AnthropicError as exc:
                        st.warning(_ai_failed(exc, "plan", "Writing suggested next steps")
                                   + " This plan was made without suggested next steps.")
                    else:
                        _ai_record("plan")  # counted once it has answered
            today = datetime.now().date()
            st.session_state["plan_pdf"] = {
                "data": client_plan.render_pdf(
                    facts, steps, account_name=ACTIVE_NAME, today=today,
                    advisor_name=None if USER_ID == LOGIN_ID else st.session_state["username"]),
                "name": f"plan-{ACTIVE_NAME}-{today.isoformat()}.pdf",
            }
        plan = st.session_state.get("plan_pdf")
        if plan and not blocked:
            st.download_button("Download plan", plan["data"], file_name=plan["name"],
                               mime="application/pdf", type="primary")
