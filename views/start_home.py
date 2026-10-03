# Part of dashboard.py, which runs this file with _view("start_home") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# The pages before anything is brought in. Someone not investing yet gets a
# Home that is their route - the next waypoint, their direction, and calm
# ways to look around (practice money, the example portfolio) or bring in
# what they already own - instead of an import form; Activity and Income say
# in one line what will show up there. An advisor looking at an empty
# client's account (or their own) gets the ways to bring statements in.
# ruff: noqa: F821

# the next waypoint, in a sentence for Home's route card
START_WAYPOINT_LINES = {
    "profile": "A few quick taps about you, so your route fits you.",
    "ready": "A quick look at what many people sort out first: savings, high-interest debt "
             "and any employer match.",
    "goal": "Pick what you're investing for and roughly when - a rough goal is plenty.",
    "basics": "Six short ideas worth knowing before you invest.",
    "mix": "What a mix could look like for someone with your answers - an example, not a "
           "recommendation.",
    "practice": "See how a mix would have done with real past prices - no real money.",
    "brokerage": "What to compare, and some well-known brokerages side by side - none of "
                 "them ranked.",
    "account": "Opening it online, usually in one sitting, and moving some money in.",
    "first": "What a first buy looks like, and the kinds of funds many people start with.",
    "bring": "Bring in what you own from any brokerage, so your plan follows the real thing.",
}
START_ASK = "I'm new to investing. What should I do before I invest, and in what order?"


def _start_words(step):
    """(title, line, button label, action) for route.next_step(starting=True)."""
    k = step["key"]
    if k == "goal":
        return ("Set your goal", START_WAYPOINT_LINES["goal"], "Set a goal", ("learn", "goal"))
    if k == "goal_wait":
        return ("Your advisor sets your goal with you", "It shows up here once they have. Their "
                "notes to you are under Advisor notes.", "Advisor notes", ("page", "Advisor notes"))
    if k == "profile":
        return ("Tell us a little about you", START_WAYPOINT_LINES["profile"],
                "Answer the questions", ("learn", "profile"))
    if k == "learn" and step["step"] == "bring" and CAN_IMPORT:   # straight to the window
        return (step["title"], START_WAYPOINT_LINES["bring"], "Add holdings",
                ("dialog", "manual"))
    if k == "learn":   # the stage and step are in the line above ("You're in ...")
        return (step["title"], START_WAYPOINT_LINES.get(step["step"], ""), "Continue",
                ("learn", step["step"]))
    return ("Bring in what you own", "Paste it from any brokerage, type it in, or use "
            "percentages only.", "Add holdings", ("dialog", "manual"))


def _start_go(action):
    kind, target = action
    if kind == "dialog":
        _open_holdings_dialog(target)
    elif kind == "learn":
        st.session_state["gs_at"] = target   # that waypoint open on Learn
        _go("Get started")
    else:
        _go(target)


def _start_ask(step_key):
    st.session_state["coach_prompt"] = COACH_PROMPTS.get(step_key, START_ASK)
    st.session_state["page"] = "AI Assistant"


def _start_bring_toggle():
    st.session_state["start_bring_open"] = not st.session_state.get("start_bring_open")


def _render_start_home():
    """Home before anything is invested: Your route, the direction, and calm
    ways to look around or bring in what they own."""
    st.session_state["gs_has_holdings"] = False   # what Your direction's window reads
    state = _route_state(False)
    plan, profile = state["plan"], state["profile"]
    waypoints = state["waypoints"]   # their route: Learn first only if they're new
    has_goal = plans.has_goal(plan)
    gp = _goal_progress(plan, 0.0) if has_goal else None
    monthly = float((plan or {}).get("monthly_contribution") or 0.0)
    step = route.next_step(
        has_goal=has_goal, can_manage=CAN_MANAGE, profile_missing=bool(state["missing"]),
        has_holdings=False, monthly=monthly, goal=gp, drift=[], days_since_holdings=None,
        waypoints=waypoints, starting=True)
    title, line, button, action = _start_words(step)
    mix = learn.starter_mix(profile, state["horizon"])
    kind = learn.investor_type(profile, mix, state["items"])

    # ---- Your route: the goal, the trail, the one next step ------------------- #
    with st.container(border=True, key="pt_route"):
        head = ("<div class='pt-route-label'>Your route"
                + (f" · {html.escape(kind['name'])}" if kind else "") + "</div>")
        if has_goal:
            label, tone = PLAN_STATUS[gp["status"]]
            need = gp.get("needed_monthly")
            head += (f"<div class='pt-goal-top'><b>{html.escape(plan.get('goal_name') or plan['goal_type'] or 'Goal')}</b>"
                     f"<span class='pt-chip {tone}'>{label}</span></div>"
                     f"<div class='pt-goal-sub'>{fmt_money0(gp['target'])} by "
                     f"{_fmt_month(plan['target_date'])}"
                     + (f" · about {fmt_money0(need)} a month gets you there"
                        if need and gp["status"] == "starting" else "") + "</div>")
        n_done = sum(1 for _, _, d in waypoints if d)
        head += route.trail_html(route.dots(waypoints, False),
                                 f"{n_done} of {len(waypoints)} waypoints reached, then your goal")
        head += _where_html(state)   # "You're in Learn · step 3 of 6 · ..." (get_started.py)
        st.html(head)
        with st.container(horizontal=True, vertical_alignment="center"):
            # "$" escaped: a pair of them would be read as a math formula
            st.markdown((f":material/flag: **Next: {title}**" + (f"  \n{line}" if line else ""))
                        .replace("$", r"\$"), width="stretch")
            st.button(button, key="route_go", type="primary", on_click=_start_go, args=(action,))
            st.button(f"Ask {GUIDE}", key="route_ask", type="tertiary", on_click=_start_ask,
                      args=(step.get("step") or step["key"],))

    # ---- your direction, in one line (the whole card in a window) ----------- #
    if kind and not state["missing"]:
        with st.container(border=True, horizontal=True, vertical_alignment="center",
                          key="pt_start_direction"):
            st.html(f"<span class='pt-route-label'>Your direction</span><br>"
                    f"<b>{html.escape(kind['name'])}</b> - {html.escape(kind['line'])}",
                    width="stretch")
            if st.button("See your mix", key="start_direction", type="tertiary",
                         icon=":material/open_in_new:"):
                _direction_window(kind["key"])

    # ---- calm ways to look around, or bring in what they already own -------- #
    st.markdown("**Not investing yet? That's fine.** Look around at your own pace - nothing "
                "here uses real money.")
    with st.container(horizontal=True, key="pt_start_options"):
        st.button(":material/savings: Try practice money", key="start_practice",
                  on_click=_start_go, args=(("learn", "practice"),),
                  help="See how a mix would have done with real past prices.")
        st.button(":material/science: See an example portfolio", key="start_example",
                  on_click=_load_sample,
                  help="A made-up portfolio to explore with. Removed when you add your own.")
        st.button(":material/move_to_inbox: I already invest - bring it in", key="start_bring",
                  type="tertiary" if st.session_state.get("start_bring_open") else "secondary",
                  on_click=_start_bring_toggle)
    if st.session_state.get("start_bring_open"):
        with st.container(border=True, key="pt_start_bring"):
            st.markdown("Bring in what you own from **any brokerage**: paste your positions "
                        "from its website, read screenshots, type them in, or upload a CSV. "
                        "You'll check everything before it's saved.")
            st.caption(":material/lock: " + TRUST_LINE)
            with st.container(horizontal=True):
                st.button(":material/content_paste: Paste or type", key="start_manual",
                          type="primary", on_click=_open_holdings_dialog, args=("manual",))
                st.button(":material/upload_file: Upload a CSV", key="start_import",
                          on_click=_open_holdings_dialog, args=("import",))

    render_kit_card(None)      # milestones and gear: learning counts too (views/kit.py)
    check_milestones(None)


def _client_ask(step_key):
    st.session_state["coach_prompt"] = CLIENT_ASK.get(step_key, CLIENT_ASK["bring_advisor"])
    st.session_state["page"] = "AI Assistant"


# what "Ask Northwend" starts with for an advisor's client's next step
# (route.advisor_step; Home's ROUTE_ASK too): questions to understand and to
# bring to their advisor - never what to buy
CLIENT_ASK = {
    "proposal": "My advisor has shared a proposal for my investments. Help me understand "
                "what to look at in it, and what questions I could ask them. Explain, don't "
                "recommend.",
    "report": "My advisor sent me a progress report. Help me understand the terms in it and "
              "what questions I could ask them.",
    "profile_advisor": "My advisor asked me about my timeline and how I feel about ups and "
                       "downs. What do those questions mean, and why do they matter?",
    "bring_advisor": "I'm starting to work with a financial advisor. What's useful to know "
                     "before our first conversation, and what could I ask them?",
}


def _render_client_home(preview=False):
    """Home before anything is brought in, for an advisor's client (client
    mode, CLIENT_MODE): their advisor's next step, their goal once it's set,
    and Learn's reads there for them - no practice money, example portfolio
    or example funds, which could cross what their advisor recommends.
    `preview`: their advisor, under the ways to bring statements in."""
    st.session_state["gs_has_holdings"] = False
    state = _route_state(False)
    plan = state["plan"]
    has_goal = plans.has_goal(plan)
    gp = _goal_progress(plan, 0.0) if has_goal else None
    step = _client_step(state, False)   # always one before anything is in
    title, line, button, action = _client_step_words(step)
    if preview:
        st.markdown(f"#### What {ACTIVE_NAME} sees on Home")
    with st.container(border=True, key="pt_route"):
        head = "<div class='pt-route-label'>Your next step, with your advisor</div>"
        if has_goal:
            label, tone = PLAN_STATUS[gp["status"]]
            head += (f"<div class='pt-goal-top'><b>{html.escape(plan.get('goal_name') or plan['goal_type'] or 'Goal')}</b>"
                     f"<span class='pt-chip {tone}'>{label}</span></div>"
                     f"<div class='pt-goal-sub'>{fmt_money0(gp['target'])} by "
                     f"{_fmt_month(plan['target_date'])}</div>")
        else:
            head += ("<div class='pt-goal-sub'>Your advisor sets your goal with you - it shows "
                     "up here once they have.</div>")
        st.html(head)
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown((f":material/flag: **Next: {title}**" + (f"  \n{line}" if line else ""))
                        .replace("$", r"\$"), width="stretch")
            st.button(button, key="route_go", type="primary", on_click=_start_go, args=(action,))
            st.button(f"Ask {GUIDE}", key="route_ask", type="tertiary", on_click=_client_ask,
                      args=(step["key"],))
    with st.container(horizontal=True, vertical_alignment="center", key="pt_client_learn"):
        st.markdown(":material/school: **Learn at your own pace.** Short reads on the basics - "
                    "funds, spreading your money out, fees, ups and downs - whenever you'd like "
                    "them.", width="stretch")
        st.button(f"Open {_label('Get started')}", key="client_learn", type="tertiary",
                  on_click=_start_go, args=(("learn", "basics"),))
    if not preview:
        render_kit_card(None)      # learning and habits only (views/kit.py)
        check_milestones(None)


# what each page shows once there's something in it
NOT_YET_LINES = {
    "Income": (":material/payments:", "Once you've made your first investment, the dividends "
                                      "it pays show up here."),
    "Activity": (":material/history:", "Once you've made your first investment, your buys and "
                                       "sells show up here - they fill in by themselves."),
}


def _render_not_yet(page):
    """Activity or Income before anything is invested: one friendly line."""
    icon, line = NOT_YET_LINES.get(page, (":material/info:", "This fills in once you've "
                                                             "brought in what you own."))
    st.markdown(f"{icon} {line}")
    with st.container(horizontal=True):
        st.button("Back to your route", key="not_yet_home", type="tertiary", on_click=_go,
                  args=("Dashboard",), icon=":material/flag:")
        st.button("I already invest - bring it in", key="not_yet_bring", type="tertiary",
                  on_click=_open_holdings_dialog, args=("manual",),
                  icon=":material/move_to_inbox:")


def _render_bring_in(client):
    """An advisor's view of an account with nothing in it yet: `client`'s
    name (their client's account), or None (their own)."""
    if client:
        st.markdown(f"#### Bring {client}'s statements in")
        st.markdown(f"Nothing's here yet. Paste {client}'s positions from any brokerage's "
                    "website, read them from screenshots, type them in, or upload a positions "
                    f"CSV - {client} sees their portfolio as soon as it's saved.")
    else:
        st.markdown("#### Bring your holdings in")
        st.markdown("Nothing's here yet. Paste your positions from any brokerage's website, "
                    "read them from screenshots, type them in, or upload a positions CSV.")
    st.caption(":material/lock: " + TRUST_LINE)
    with st.container(horizontal=True):
        st.button(":material/content_paste: Paste or type", key="onboard_paste", type="primary",
                  on_click=_open_holdings_dialog, args=("manual",))
        st.button(":material/upload_file: Upload a CSV", key="onboard_import",
                  on_click=_open_holdings_dialog, args=("import",))
        if not client:
            st.button(":material/science: Try it with example data", key="onboard_sample",
                      on_click=_load_sample,
                      help="A made-up portfolio to explore with. Removed when you add your own.")
