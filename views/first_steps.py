# Part of dashboard.py, which runs this file with _view("first_steps") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# First steps (ROADMAP S1): a new investor's start as a slideshow - one screen
# at a time, Back / Next and progress dots - instead of the whole Get started
# page at once. Welcome -> a few tap questions (the profile, two at a time) ->
# a goal -> their direction -> bring holdings in. Skippable and resumable
# (where they are is kept in their settings, so it follows them to another
# device); Get started shows the full page once it's done or skipped, with a
# way to go through it again.
# ruff: noqa: F821

# (key, title, profile fields asked on that screen)
FIRST_STEPS = (
    ("welcome", "Welcome", ()),
    ("why", "What it's for", ("goal", "time_horizon_years")),
    ("ups", "Ups and downs", ("risk_tolerance", "drawdown_reaction")),
    ("you", "A bit about you", ("experience", "age_range")),
    ("safety", "Your safety net", ("income_stability", "emergency_fund", "high_interest_debt",
                                     "employer_match")),
    ("goal", "Your goal", ()),
    ("direction", "Your direction", ()),
    ("bring", "Bring it in", ()),
)
# a trusted, public place to read more about each screen's idea (learn.LEARN_MORE)
FIRST_STEPS_LINKS = {
    "ups": "risk_tolerance",
    "safety": "emergency_fund",
    "direction": "asset_allocation",
}


def _fs_state():
    return dict(_read_prefs().get("first_steps") or {})


def _fs_save(**changes):
    p = _read_prefs()
    p["first_steps"] = {**(p.get("first_steps") or {}), **changes}
    _write_prefs(p)


def _fs_steps():
    """The screens for this account: no goal screen when an advisor sets it,
    no bring-it-in screen when the advisor brings statements in. An
    advisor's client answers the questions only - no example mix (their
    direction comes from their advisor) and no beginner's way in."""
    return [s for s in FIRST_STEPS
            if not (s[0] == "goal" and not CAN_MANAGE)
            and not (s[0] == "bring" and (not CAN_IMPORT or CLIENT_MODE))
            and not (s[0] == "direction" and CLIENT_MODE)]


def first_steps_active(has_holdings):
    """Show the slideshow instead of Get started: an investor's own account
    that hasn't finished or skipped it, and still has something to do."""
    if IS_ADVISOR or USER_ID != LOGIN_ID or st.session_state.get("fs_hide"):
        return False
    s = _fs_state()
    if s.get("done") or s.get("skipped"):
        return False
    import advisor
    missing = advisor.missing_fields(_profile())   # read once per run (dashboard.py)
    # (an advisor's client's statements are their advisor's to bring in)
    return bool(missing) or (not has_holdings and not CLIENT_MODE)


def _fs_save_answers(fields):
    """Save this screen's answers (only the ones given; nothing is cleared)."""
    import advisor
    got = {}
    for f in fields:
        v = st.session_state.get(f"fs_{f}")
        if isinstance(v, list):
            order = advisor.MULTI_CHOICES[f]
            v = advisor.MULTI_SEP.join(sorted(v, key=lambda x: order.index(x)
                                              if x in order else len(order)))
        if v not in (None, "", []):
            got[f] = v
    if got:
        c = connect(DB)
        try:
            advisor.save_profile(c, USER_ID, got)
        finally:
            c.close()


def _fs_goal_fallback():
    """The goal type when none is picked: the profile's goal, else Other."""
    import advisor
    first = (advisor.split_multi(_profile().get("goal")) or [None])[0]
    return _GOAL_FROM_PROFILE.get(first) or "Other"


def _fs_save_goal():
    target = st.session_state.get("fs_goal_target") or 0
    if target <= 0:
        return  # a goal is optional here; the Plan page asks again later
    # an amount without a type (a tap can un-pick the one picked for them) is
    # still their goal: never dropped without a word
    goal_type = st.session_state.get("fs_goal_type") or _fs_goal_fallback()
    years = int(st.session_state.get("fs_goal_years") or 10)
    when = plans.add_months(datetime.now().date(), 12 * years)
    save_plan_fields({"goal_type": goal_type, "target_amount": float(target),
                      "target_date": when.isoformat(),
                      "monthly_contribution": float(st.session_state.get("fs_goal_monthly") or 0)})


def _fs_move(i, delta):
    """Back / Next: keep what's on this screen, then move."""
    steps = _fs_steps()
    key, _, fields = steps[i]
    if delta > 0:
        if fields:
            _fs_save_answers(fields)
        elif key == "goal":
            _fs_save_goal()
    nxt = i + delta
    if nxt >= len(steps):
        _fs_save(done=True, step=0)
        # an advisor's client goes back Home, to their advisor's next step
        st.session_state["page"] = "Dashboard" if CLIENT_MODE else "Get started"
        return
    _fs_save(step=max(nxt, 0))


def _fs_skip():
    _fs_save(skipped=True)


def _fs_finish(then=None):
    _fs_save(done=True, step=0)
    if then == "example":
        _load_sample()
        st.session_state["page"] = "Dashboard"   # straight to seeing it
    elif then in ("manual", "import"):
        _open_holdings_dialog(then)
    elif then == "route":
        # Learn, open where their route starts: Learn for someone new,
        # Start investing (Choose a brokerage) for someone with experience
        st.session_state.pop("gs_at", None)
        st.session_state.pop("gs_goal_part", None)
        st.session_state["page"] = "Get started"


def _fs_dots(i, n, label=None, on=None):
    """Progress dots: the first `i` + 1 lit, or those in `on` (indexes) when
    given - Learn's goal parts light the ones completed. `label` is what a
    screen reader hears (default "Step i of n")."""
    lit = set(range(i + 1)) if on is None else set(on)
    dots = "".join(f"<span class='pt-fs-dot{' pt-fs-dot-on' if k in lit else ''}"
                   f"{' pt-fs-dot-at' if on is not None and k == i else ''}'></span>"
                   for k in range(n))
    label = html.escape(label or f"Step {i + 1} of {n}", quote=True)
    st.html(f"<div class='pt-fs-dots' role='img' aria-label='{label}'>{dots}</div>")


def _fs_question(field, profile, on_change=None, args=None):
    """One profile question as taps, prefilled with any saved answer
    (`on_change`: Learn's waypoint 2 uses these too, and saves each tap)."""
    import advisor
    q, cur = PROFILE_QUESTIONS[field], profile.get(field)
    key = f"fs_{field}"
    cb = {"on_change": on_change, "args": args}
    if field in advisor.MULTI_CHOICES:
        st.session_state.setdefault(key, advisor.split_multi(cur))
        st.pills(q, list(advisor.MULTI_CHOICES[field]), selection_mode="multi", key=key, **cb)
    elif field == "time_horizon_years":
        st.session_state.setdefault(key, int(cur) if cur else None)
        st.pills(q, list(HORIZON_YEARS), key=key, **cb,
                 format_func=lambda v: f"{v} year{'s' if v != 1 else ''}"
                 f"{'+' if v == HORIZON_YEARS[-1] else ''}")
    else:
        st.session_state.setdefault(key, cur or None)
        st.pills(q, list(advisor.CHOICES[field]), key=key, **cb,
                 format_func=lambda v: v[:1].upper() + v[1:])


def _fs_screen_goal(profile):
    import advisor
    plan = load_plan() or {}
    first = (advisor.split_multi(profile.get("goal")) or [None])[0]
    st.session_state.setdefault("fs_goal_type", plan.get("goal_type")
                                or _GOAL_FROM_PROFILE.get(first))
    st.session_state.setdefault("fs_goal_target", float(plan.get("target_amount") or 0.0))
    st.session_state.setdefault("fs_goal_years", int(profile.get("time_horizon_years") or 10))
    st.session_state.setdefault("fs_goal_monthly", float(plan.get("monthly_contribution") or 0))
    st.markdown("A rough goal is plenty - you can change it any time on the Plan page.")
    if not st.session_state.get("fs_goal_type"):
        st.session_state["fs_goal_type"] = _fs_goal_fallback()
    # required: tapping the chosen one keeps it chosen (a second tap used to
    # un-pick it, and the goal was then quietly not saved)
    st.pills("What are you saving for?", plans.GOAL_TYPES, key="fs_goal_type", required=True)
    c1, c2 = st.columns(2)
    c1.number_input("I want to have ($)", min_value=0.0, step=1000.0,
                    format="%.0f", key="fs_goal_target")
    c2.number_input("in how many years", min_value=1, max_value=60, step=1,
                    key="fs_goal_years")
    c1.number_input("I'll invest each month ($)", min_value=0.0, step=50.0,
                    format="%.0f", key="fs_goal_monthly")
    target = float(st.session_state.get("fs_goal_target") or 0)
    if target > 0:
        # what reaches it, worked out the same way as everywhere else
        today = datetime.now().date()
        when = plans.add_months(today, 12 * int(st.session_state.get("fs_goal_years") or 10))
        tip = learn.suggestions(profile, {"target_amount": target, "target_date": when.isoformat()},
                                today=today, return_pct=_plan_return_pct())
        if tip["monthly"]:
            _suggestion_line(f"{fmt_money0(tip['monthly'])} a month - what reaches "
                             f"{fmt_money0(target)} by {_fmt_month(when.isoformat())} at "
                             f"{_plan_return_pct():g}% a year", key="fs_goal_monthly_use",
                             values={"fs_goal_monthly": tip["monthly"]})
    st.caption("Not sure yet? Leave the amount at 0 and press Next.")


def _fs_screen_direction(profile):
    plan = load_plan()
    today = datetime.now().date()
    horizon = (plans.months_until(plan["target_date"], today) / 12
               if plans.has_goal(plan) and plans.months_until(plan["target_date"], today) > 0
               else None)
    mix = learn.starter_mix(profile, horizon)
    kind = learn.investor_type(profile, mix, learn.readiness(profile))
    if kind:
        _render_direction(kind, mix)
    else:
        st.markdown("Answer the questions on the earlier screens and this shows what kind of "
                    "investor you are, with an example mix that fits.")


def _fs_screen_bring(profile):
    if route.learn_first(profile.get("experience"), False):
        # new to investing: most people here don't have an account yet, so
        # their route is the first way on - Learn, then Start investing;
        # bringing one in is still a tap away
        st.markdown("Last step. Most people new to investing don't have an account yet - "
                    "that's fine. Your route starts with **Learn**: the basics, one short step "
                    "at a time. Then **Start investing** walks you through choosing a "
                    "brokerage, opening an account and what a first buy looks like.")
        st.button(":material/route: I don't have an account yet - show me how",
                  key="fs_no_account", type="primary", on_click=_fs_finish, args=("route",))
        st.caption("Already have one? Bring it in from **any brokerage** - or look around with "
                   "an example portfolio first.")
        with st.container(horizontal=True):
            st.button(":material/content_paste: Paste or type", key="fs_manual",
                      on_click=_fs_finish, args=("manual",))
            st.button(":material/science: Try an example", key="fs_example", type="tertiary",
                      on_click=_fs_finish, args=("example",))
        st.caption(":material/lock: " + TRUST_LINE)
        return
    st.markdown("Last step: bring in what you own - from **any brokerage**, by pasting, a CSV, "
                "screenshots or typing it in. Or look around with an example portfolio first.")
    st.caption(":material/lock: " + TRUST_LINE)
    with st.container(horizontal=True):
        st.button(":material/content_paste: Paste or type", key="fs_manual", type="primary",
                  on_click=_fs_finish, args=("manual",))
        st.button(":material/upload_file: Upload a CSV", key="fs_import",
                  on_click=_fs_finish, args=("import",))
        st.button(":material/science: Try an example", key="fs_example",
                  on_click=_fs_finish, args=("example",))
    st.button(":material/route: I don't have an account yet - show me how", key="fs_no_account",
              type="tertiary", on_click=_fs_finish, args=("route",),
              help="Start investing walks you through choosing a brokerage, opening an account "
                   "and a first buy. Learn's basics stay there if you'd like them.")


def render_first_steps(has_holdings):
    steps = _fs_steps()
    i = min(int(_fs_state().get("step") or 0), len(steps) - 1)
    key, title, fields = steps[i]
    profile = _profile()   # read once per run (dashboard.py)
    # the phone tab bar steps aside while these are open, so Back / Next
    # (pinned to the bottom there) are never behind it; Skip for now leaves
    st.html("<style>body .st-key-pt_tabbar { display: none !important; }</style>")
    _, mid, _ = st.columns([1, 3, 1])
    with mid:
        _fs_dots(i, len(steps))
        # a new key per screen, so each one slides in (the styles animate it)
        with st.container(border=True, key=f"pt_slide_{key}"):
            st.caption(f"Step {i + 1} of {len(steps)}")
            if key == "welcome" and CLIENT_MODE:
                st.markdown(f"### Welcome to {APP_NAME}")
                st.markdown("A few quick questions about you - every answer a tap - so your "
                            "advisor knows your timeline and how you feel about ups and downs "
                            "before you talk.")
                st.markdown(":material/lock: **Private by design.** Only you and your advisor "
                            "see your account. We never ask for your brokerage login.  \n"
                            ":material/handshake: **Your plan is made with your advisor.** "
                            f"{APP_NAME} is where you follow it together, and Learn has short "
                            "reads whenever you'd like them.")
            elif key == "welcome":
                st.markdown(f"### Welcome to {APP_NAME}")
                st.markdown(f"{APP_NAME} is your guide from first step to goal. A few quick "
                            "questions - every answer a tap - and it shows what kind of investor "
                            "you are and a route to follow, one waypoint at a time.")
                st.markdown(":material/lock: **Private by design.** We never ask for your "
                            "brokerage login, and you can use percentages or an example "
                            "portfolio instead of real numbers.  \n"
                            ":material/school: **A guide, not a salesperson.** It explains and "
                            "shows examples; it never tells you what to buy, and has nothing to "
                            "sell you.")
            else:
                st.markdown(f"### {title}")
                if fields:
                    for f in fields:
                        _fs_question(f, profile)
                elif key == "goal":
                    _fs_screen_goal(profile)
                elif key == "direction":
                    _fs_screen_direction(profile)
                elif key == "bring":
                    _fs_screen_bring(profile)
            if key in FIRST_STEPS_LINKS:
                learn_more(FIRST_STEPS_LINKS[key])
        # pt_fs_nav: on a phone it stays in reach at the bottom of the screen
        with st.container(horizontal=True, vertical_alignment="center", key="pt_fs_nav"):
            if i:
                st.button(":material/arrow_back: Back", key="fs_back", on_click=_fs_move,
                          args=(i, -1))
            st.button("Skip for now", key="fs_skip", type="tertiary", on_click=_fs_skip,
                      help=f"Go to the {_label('Get started')} page instead - it has "
                           "everything on one page, and you "
                           "can come back to these steps there.")
            st.space("stretch")
            last = i == len(steps) - 1
            st.button("Finish" if last else ("Let's go" if key == "welcome" else "Next"),
                      key="fs_next", type="secondary" if key == "bring" else "primary",
                      on_click=_fs_move, args=(i, 1))


def _fs_restart():
    _fs_save(done=False, skipped=False, step=0)
