# Part of dashboard.py, which runs this file with _view("assistant") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# Ask Northwend: the AI guide's chat page. Calm by default (ROADMAP S6): the
# hello, a few suggested questions and the chat, with the investing profile
# and the printable plan in a window; the full page (both open above the chat)
# for advisors and for Show everything (_show_everything).
# ruff: noqa: F821

ASSIST_PROFILE_NOTE = (f"{GUIDE} also fills this in from what you tell it in the chat, and "
                       "keeps short notes of its own so the next conversation picks up where "
                       "this one left off.")
ASSIST_DISCLAIMER = (f"Educational information only - not financial advice. {GUIDE} is not a "
                     "licensed financial advisor; do your own research before making any "
                     "investment decision.")


def _assist_profile():
    import advisor
    c = connect(DB)
    try:
        return advisor.get_profile_and_memory(c, USER_ID)
    finally:
        c.close()


@st.dialog("Your investing profile", width="large", on_dismiss=_dialog_closed)
def _assist_profile_window():
    import advisor
    st.caption(ASSIST_PROFILE_NOTE)
    _render_profile_form(advisor, _assist_profile()[0])   # Save closes the window


@st.dialog("Printable plan (PDF)", width="large", on_dismiss=_dialog_closed)
def _assist_plan_window(api_key, contexts, cash_by_account):
    profile, memory = _assist_profile()
    _render_plan_export(api_key, profile, memory, contexts, cash_by_account,
                        st.session_state.get("chat_display", []), in_window=True)


def _render_assistant(contexts, cash_by_account):
    import advisor

    if st.session_state.pop("profile_toast", False):
        st.toast("Profile updated from the conversation.")
    api_key = _anthropic_key()
    if not api_key:
        # the set-up detail (no ANTHROPIC_API_KEY) is on Admin > System
        st.info(f"Ask {GUIDE} isn't available on this site right now.")
        return

    profile, memory = _assist_profile()

    # the count covers the readiness questions too (debt, employer match), so
    # it never says every question is answered while some are still open
    missing = [f for f in advisor.KEY_PROFILE_FIELDS if profile.get(f) in (None, "")]
    display = st.session_state.setdefault("chat_display", [])
    history = st.session_state.setdefault("chat_api", [])
    n_required = len(advisor.KEY_PROFILE_FIELDS)
    answered = f"{n_required - len(missing)}/{n_required}"
    full = _show_everything()
    if full:
        with st.expander(f"Your investing profile ({answered} key questions answered)",
                         expanded=bool(missing) and not display):
            _render_profile_form(advisor, profile)
            st.caption(ASSIST_PROFILE_NOTE)

        st.caption(ASSIST_DISCLAIMER)

        _render_plan_export(api_key, profile, memory, contexts, cash_by_account, display)
    elif missing and not display:
        # the calm view's one next step: the questions that make answers fit
        if _next_step_card("assistant", "Answer a few quick questions about you "
                           f"({n_required - len(missing)} of {n_required} done), so "
                           f"{GUIDE}'s answers fit your timeline and comfort with "
                           "ups and downs.", ("Answer the questions", None, ())):
            _open_window(_assist_profile_window)
    # new messages are written into this box too, so they land above the input
    chat_box = st.container()
    with chat_box:
        if not display:
            with st.chat_message("assistant", avatar=SAGE_AVATAR):
                st.markdown(f"Hi, I'm **{GUIDE}**, your guide. Ask me anything "
                            "about investing or your portfolio - what a fund is, whether your mix "
                            "fits your goal, what to look at next. I'll explain in plain "
                            "language, and I won't tell you what to buy.")
        for msg in display:
            with st.chat_message(msg["role"], avatar=_avatar(msg["role"])):
                st.markdown(msg["text"])

    prompt = None
    if not display:
        if not full:
            st.caption("Not sure where to start? Try one of these:")
        starts = QUICK_STARTS if contexts else QUICK_STARTS_NEW   # nothing to review yet
        cols = st.columns(len(starts))
        for col, (label, text) in zip(cols, starts.items()):
            if col.button(label, width="stretch", key=f"quick_{label}"):
                prompt = text

    n_sent = sum(1 for m in display if m["role"] == "user")
    # this month's allowance (ai_usage.py); this page is drawn in the full run
    quota = _ai_status("chat", full_run=True)
    at_limit = n_sent >= CHAT_MESSAGE_LIMIT or not quota["ok"]
    # Inside a container the input sits inline under the chat instead of pinned to
    # the bottom of the screen. Pinned, Streamlit also keeps the page stuck to the
    # bottom, and on phones scrolling up (which resizes the browser's address bar)
    # snapped it straight back down.
    with st.container():
        typed = st.chat_input(f"Ask {GUIDE} about investing or your portfolio...",
                              disabled=at_limit)
    # a question handed over from a Get started step
    prompt = typed or prompt or st.session_state.pop("coach_prompt", None)

    if prompt and not at_limit:
        import anthropic

        display.append({"role": "user", "text": prompt})
        history.append({"role": "user", "content": prompt})
        n_history = len(history)
        with chat_box, st.chat_message("user"):
            st.markdown(prompt)

        system = advisor.system_prompt(profile, advisor.portfolio_summary(contexts, cash_by_account, CLASS_SPLITS),
                                       memory)
        updated = []

        def on_update(fields):
            c = connect(DB)
            try:
                advisor.save_profile(c, USER_ID, fields)
            finally:
                c.close()
            updated.append(fields)

        def on_memory(text):
            c = connect(DB)
            try:
                advisor.save_memory(c, USER_ID, text)
            finally:
                c.close()

        with chat_box, st.chat_message("assistant", avatar=SAGE_AVATAR):
            try:
                reply = st.write_stream(advisor.stream_reply(
                    anthropic.Anthropic(api_key=api_key), history, system, on_update,
                    on_memory))
            except anthropic.AnthropicError as exc:
                # one calm sentence, never the error's text (_ai_failed); the
                # question leaves the history so the next try asks it afresh,
                # and nothing is counted against the month's allowance
                reply = _ai_failed(exc, "chat")
                del history[n_history - 1:]
                st.warning(reply)
            else:
                _ai_record("chat")  # counted once it has answered
                if quota["left"] is not None:
                    quota["left"] -= 1
        display.append({"role": "assistant", "text": reply if isinstance(reply, str) else "".join(reply)})
        if updated:
            # rerun so the profile form shows the new values; the toast is
            # carried across the rerun, since one fired right before it is lost
            st.session_state["profile_toast"] = True
            st.rerun()

    if not quota["ok"]:
        st.info(ai_usage.used_up_text(quota, "chat"))
    elif at_limit:
        st.info(f"This conversation hit the {CHAT_MESSAGE_LIMIT}-message limit. Start a new one "
                "to keep going.")
    def _new_conversation():
        st.session_state["chat_display"] = []
        st.session_state["chat_api"] = []
    if full:
        if display:
            st.button("New conversation", on_click=_new_conversation)
    else:
        # the calm view: the profile and the printable plan open in a window
        with st.container(horizontal=True, vertical_alignment="center"):
            if display:
                st.button("New conversation", on_click=_new_conversation)
            if st.button(f"Your investing profile ({answered})", key="assist_profile_open",
                         type="tertiary", icon=":material/person:"):
                _open_window(_assist_profile_window)
            if st.button("Printable plan (PDF)", key="assist_plan_open", type="tertiary",
                         icon=":material/picture_as_pdf:"):
                _open_window(_assist_plan_window, api_key, contexts, cash_by_account)
        st.caption(ASSIST_DISCLAIMER)
    st.caption(f"Your holdings are shared with {GUIDE} as percentages only - no dollar "
               "amounts, share counts, or account names."
               + (f" {ai_usage.left_text(quota, 'chat')}." if quota["ok"] and quota["limit"] else ""))
