# Part of dashboard.py, which runs this file with _view("get_started") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# The Get started page: the new-investor path and the practice portfolio.
# ruff: noqa: F821

# ---- Get started page ------------------------------------------------------ #
# readiness state -> (icon markdown, words); the words always go with the icon
_READY_ICON = {
    learn.GOOD: (":green[:material/check_circle:]", "Good"),
    learn.CAUTION: (":orange[:material/error:]", "Look at this"),
    learn.STOP: (":red[:material/cancel:]", "Start here"),
    learn.UNKNOWN: (":gray[:material/help:]", "Not answered"),
}
# Every waypoint, in two stages (route.STAGE_KEYS): Learn - education first,
# for someone brand new - then Start investing, onto their own investing
# path. Who walks which is route.learn_first(); an advisor-managed client's
# Start investing is the last one only (route.MANAGED_INVEST_KEYS).
GET_STARTED_STEPS = (
    ("profile", "About you"),
    ("ready", "Are you ready to invest?"),
    ("goal", "Set a goal"),
    ("basics", "Learn the basics"),
    ("mix", "An example mix"),
    ("practice", "Try it with practice money"),
    ("brokerage", "Choose a brokerage"),
    ("account", "Open your account"),
    ("first", "Your first investments"),
    ("bring", "Bring it in"),
)
# each waypoint opens with one plain line: why it matters, and what you'll do
WAYPOINT_WHY = {
    "profile": "So the rest of your route fits you - a few taps about your timeline and how "
               "you feel about ups and downs.",
    "ready": "So you start on solid ground - a quick check of what most people take care of "
             "before they invest.",
    "goal": "So you know how much to invest each month to get there - your goal, a monthly "
            "amount and a mix, one at a time.",
    "basics": "So the words and ideas make sense - six short reads, a minute or two each.",
    "mix": "So you can see what a simple portfolio looks like - an example split for someone "
           "with your answers.",
    "practice": "So you can feel the ups and downs before using real money - try a mix on "
                "real past prices.",
    "brokerage": "So your money has a good home - what to compare, and some well-known "
                 "brokerages to look at side by side.",
    "account": "So you're ready to invest - open the account online, usually in one sitting, "
               "and move some money in.",
    "first": "So your first buy feels simple, not scary - what it looks like, and the kinds "
             "of funds many people start with.",
    "bring": "So you can follow your own investments here - your plan and your goal then "
             "track the real thing.",
}
# "Set a goal" in short parts, each with its own Complete button (key, title)
GOAL_PARTS = (
    ("what", "What are you saving for?"),
    ("monthly", "How much you'll invest each month"),
    ("how", "How it's going"),
    ("mix", "Your target mix"),
)
# questions a step can hand to the AI Assistant
COACH_PROMPTS = {
    "ready": "Looking at my situation, what should I take care of before I start investing, "
             "and in what order?",
    "basics": "Explain stocks, bonds, index funds and ETFs to me like I'm brand new to investing.",
    "mix": "Explain why a mix of US stocks, international stocks and bonds might fit my time "
           "horizon and comfort with risk. Use examples, not recommendations.",
    "practice": "What should I expect emotionally when my investments drop 20% or more, and "
                "what do long-term investors usually do?",
    "brokerage": "What should I compare when choosing a brokerage, and which questions should "
                 "I ask? Please don't recommend a specific one.",
    "account": "What's the difference between a regular brokerage account, a Roth IRA and a "
               "401(k), and which questions should I ask to pick one?",
    "first": "What does a first investment usually look like for someone starting out, and "
             "what mistakes do beginners often make? Use examples, not recommendations.",
}
PRACTICE_MIXES = ("Example mix", "All stocks", "Mostly bonds")
# each basics topic -> where to read more (learn.LEARN_MORE)
BASICS_LINKS = {"funds": "index_funds", "spread": "diversification", "time": "compound_interest",
                "fees": "expense_ratios", "ups": "risk", "accounts": "account_types"}


def _usd0(v):
    """Whole dollars, never masked - for hypothetical practice numbers."""
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _ask_coach(step):
    st.session_state["coach_prompt"] = COACH_PROMPTS[step]
    st.session_state["page"] = "AI Assistant"


def _coach_button(step):
    st.button(f":material/forum: Ask {GUIDE} about this", key=f"coach_{step}",
              type="tertiary", on_click=_ask_coach, args=(step,))


def _complete(step):
    """A waypoint's Complete this step: noted in get_started_done (what
    completes Learn the basics, An example mix, Practice and Set a goal; the
    others are complete by what they ask for, and the button is only open
    then), then on to the next waypoint not complete yet."""
    p = _read_prefs()
    p["get_started_done"] = sorted(set(p.get("get_started_done") or []) | {step})
    if step == "brokerage":   # the account checklist's first tick (ACCOUNT_STEPS)
        ticks = set(p.get("account_steps") or []) | {"chosen"}
        p["account_steps"] = [k for k, _, _ in ACCOUNT_STEPS if k in ticks]
    _write_prefs(p)
    _skip(step)


def _skip(step):
    """Skip for now (and a completed waypoint's Next): on to the next
    waypoint not complete yet, without marking this one."""
    st.session_state["gs_advance"] = step
    st.session_state.pop("gs_goal_part", None)


def _next_open(at, keys, done):
    """The next waypoint after `at` that isn't complete (wrapping round), or None."""
    i = keys.index(at)
    return next((k for k in keys[i + 1:] + keys[:i] if not done[k]), None)


def _waypoint_footer(at, keys, titles, done, pressed, *, ready=True, why_not=None,
                     action=None, main=True, back=None):
    """The bottom of a waypoint: one big button - Complete this step, which
    marks it and moves on (`ready` False: not yet, `why_not` says what
    completes it; `action` (label, on_click, args): a button that does what
    completes it instead) - then a small Back and Skip for now. `pressed`:
    completed with the button before, so it reads Next instead. `main`
    False: no big button (Set a goal's parts have their own); `back`: what
    Back does instead of opening the waypoint before."""
    nxt = _next_open(at, keys, done)
    if main:
        if action:
            label, fn, args, key = action
            st.button(label, key=key, type="primary", width="stretch", on_click=fn, args=args,
                      icon=":material/move_to_inbox:")
        elif done[at] and pressed:
            if nxt:
                st.button(f"Next: {titles[nxt]}", key="gs_complete", type="primary",
                          width="stretch", icon=":material/arrow_forward:", icon_position="right",
                          on_click=_skip, args=(at,))
        else:
            st.button("Complete this step ✓", key="gs_complete", type="primary",
                      width="stretch", disabled=not ready, on_click=_complete, args=(at,))
        if why_not and not (done[at] or ready):
            st.caption(why_not)
    i = keys.index(at)
    with st.container(horizontal=True, vertical_alignment="center", key="pt_gs_nav"):
        if back:
            st.button(":material/arrow_back: Back", key="gs_prev", type="tertiary",
                      on_click=back[0], args=back[1])
        elif i:
            st.button(":material/arrow_back: Back", key="gs_prev", type="tertiary",
                      on_click=_gs_go, args=(keys[i - 1],),
                      help=f"Back to {titles[keys[i - 1]]}")
        st.space("stretch")
        if nxt and not done[at]:
            st.button("Skip for now", key="gs_skip", type="tertiary", on_click=_skip, args=(at,),
                      help=f"On to {titles[nxt]} - this step stays open for later.")


def _practice_prices(conn):
    """{ticker: [(date, price)]} for the practice funds, dividends included
    (adjusted close, falling back to close)."""
    out = {}
    for t in learn.PRACTICE_TICKERS.values():
        out[t] = [(r["date"], float(r["adj_close"] if r["adj_close"] is not None else r["close"]))
                  for r in conn.execute(
                      "SELECT date, adj_close, close FROM daily_bars WHERE ticker = ? "
                      "AND COALESCE(adj_close, close) IS NOT NULL ORDER BY date", (t,))]
    return out


def _render_mix_bar(weights):
    palette = SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT
    segs = legend = ""
    for i, b in enumerate(learn.BLOCKS):
        pct = weights[b["key"]]
        if pct > 0:
            segs += (f"<div class='pt-alloc-seg' style='flex:{pct} 0 0;background:{palette[i]}' "
                     f"title='{b['label']} {pct}%'></div>")
        legend += ("<div class='pt-legend-row'>"
                   f"<span class='pt-swatch' style='background:{palette[i]}'></span>"
                   f"<span class='pt-legend-label'><b>{b['label']}</b> - {b['about']}</span>"
                   f"<span class='pt-legend-pct'>{pct}%</span></div>"
                   f"<div class='pt-goal-sub' style='margin:0 0 .5rem 1.1rem'>Examples: "
                   f"{', '.join(b['examples'])}</div>")
    st.html(f"<div class='pt-alloc-bar' aria-hidden='true'>{segs}</div>"
            f"<div class='pt-legend'>{legend}</div>")


def _step_profile(advisor, profile, missing):
    if missing:
        st.caption("Every answer is a tap, saved as you go, and you can change them any time.")
        for f in advisor.REQUIRED_PROFILE_FIELDS:
            _fs_question(f, profile, on_change=_ready_answer, args=(f,))
        return
    known = [f"{advisor.PROFILE_FIELDS[f]}: **{profile[f]}**" for f in
             ("goal", "time_horizon_years", "risk_tolerance", "experience", "age_range")
             if profile.get(f) not in (None, "")]
    st.markdown("  \n".join(known))
    if st.toggle("Change my answers", key="gs_edit_profile"):
        _render_profile_form(advisor, profile)


# the readiness questions, answered right in waypoint 2 (learn.readiness)
READY_FIELDS = ("emergency_fund", "high_interest_debt", "employer_match")


def _ready_answer(field):
    """A tap on one of waypoint 1's or 2's questions: saved straight away."""
    _fs_save_answers((field,))


def _step_ready(items, profile):
    st.markdown(f"**{learn.readiness_summary(items)}**")
    editing = st.session_state.get("gs_ready_edit")
    for it in items:
        icon, words = _READY_ICON[it["state"]]
        if it["key"] in READY_FIELDS and (it["state"] == learn.UNKNOWN or editing):
            # the answer, as taps, in place of "answer it in your profile"
            _md(f"{icon} **{it['label']}**"   # (_md: "$500-$1,000" isn't a formula)
                + ("" if it["state"] == learn.UNKNOWN else f" ({words}) - {it['text']}"))
            _fs_question(it["key"], profile, on_change=_ready_answer, args=(it["key"],))
        else:
            _md(f"{icon} **{it['label']}** ({words}) - {it['text']}")
    if any(it["key"] in READY_FIELDS and it["state"] != learn.UNKNOWN for it in items):
        st.toggle("Change my answers", key="gs_ready_edit")
    st.caption("These are common first steps many people take before investing, not rules - "
               "your situation may differ.")
    _coach_button("ready")


def _goal_parts_done(plan):
    """{part: completed} for Set a goal's parts: the goal itself is there
    once it's saved; the others once their Complete button is pressed."""
    walked = set(_read_prefs().get("goal_parts") or [])
    return {k: plans.has_goal(plan) if k == "what" else k in walked for k, _ in GOAL_PARTS}


def _goal_part(part):
    st.session_state["gs_goal_part"] = part


def _goal_part_done(part, then=None):
    p = _read_prefs()
    p["goal_parts"] = sorted(set(p.get("goal_parts") or []) | {part})
    _write_prefs(p)
    st.session_state["gs_goal_part"] = then


def _save_goal_what():
    """Part 1's button: the goal into the plan (Plan's own fields), then part 2."""
    target = float(st.session_state.get("gs_goal_target") or 0)
    when = st.session_state.get("gs_goal_date")
    if target <= 0 or not when:
        st.session_state["gs_goal_err"] = "Enter how much you'd like to have - an amount above $0."
        return
    save_plan_fields({"goal_type": st.session_state.get("gs_goal_type") or _fs_goal_fallback(),
                      "target_amount": target, "target_date": when.isoformat()})
    st.session_state.pop("gs_goal_monthly", None)   # its suggestion follows the new goal
    _goal_part("monthly")


def _save_goal_monthly():
    save_plan_fields({"monthly_contribution": float(st.session_state.get("gs_goal_monthly") or 0)})
    _goal_part_done("monthly", "how")


def _save_goal_mix():
    """The last part: the target mix into the plan, and the waypoint complete."""
    stocks = float(st.session_state.get("gs_goal_stocks") or 0)
    save_alloc_targets({"Stocks": stocks, "Bonds": 100.0 - stocks})
    _goal_part_done("mix")
    _complete("goal")


def _two_part_bar(stocks):
    """Stocks and bonds as one bar, with a two-line legend."""
    palette = SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT
    parts = (("Stocks", stocks, palette[0], "grow more over time, and drop more along the way"),
             ("Bonds", 100 - stocks, palette[2], "steadier, and grow more slowly"))
    st.html("<div class='pt-alloc-bar' aria-hidden='true'>"
            + "".join(f"<div class='pt-alloc-seg' style='flex:{p} 0 0;background:{c}'></div>"
                      for _, p, c, _ in parts if p > 0)
            + "</div><div class='pt-legend'>"
            + "".join("<div class='pt-legend-row'>"
                      f"<span class='pt-swatch' style='background:{c}'></span>"
                      f"<span class='pt-legend-label'><b>{n}</b> - {about}</span>"
                      f"<span class='pt-legend-pct'>{p:g}%</span></div>"
                      for n, p, c, about in parts)
            + "</div>")


def _step_goal(plan, value, profile, keys, titles, done, pressed):
    """Set a goal, in four short parts inside Learn - never a trip to Plan:
    the goal, the monthly amount that gets there, how it's going, and a
    target mix. Each saves into the same plan the Plan page shows."""
    today = datetime.now().date()
    if not CAN_MANAGE:   # an advisor's client: the advisor sets the goal with them
        if plans.has_goal(plan):
            name = plan.get("goal_name") or plan["goal_type"]
            _md(f"Your advisor set your goal with you: **{name}**, "
                f"{fmt_money0(float(plan['target_amount']))} by {_fmt_month(plan['target_date'])}.")
            _render_projection(plan, value, today)
        else:
            st.caption("Your advisor sets your goal with you - it shows up here and on the Plan "
                       "page once they have.")
        _waypoint_footer("goal", keys, titles, done, pressed, ready=plans.has_goal(plan),
                         why_not="This step completes once your advisor has set your goal.")
        return

    parts_done = _goal_parts_done(plan)
    pkeys = [k for k, _ in GOAL_PARTS]
    part = st.session_state.get("gs_goal_part")
    if part not in pkeys or (part != "what" and not plans.has_goal(plan)):
        part = next((k for k in pkeys if not parts_done[k]), "what")
    n = pkeys.index(part)
    _fs_dots(n, len(pkeys), label=f"Part {n + 1} of {len(pkeys)}, "
                                  f"{sum(parts_done.values())} complete",
             on=[i for i, k in enumerate(pkeys) if parts_done[k]])
    st.caption(f"Part {n + 1} of {len(pkeys)}"
               + (" · :material/check_circle: Complete" if parts_done[part] else ""))
    st.markdown(f"#### {dict(GOAL_PARTS)[part]}")
    if part != "what":
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            _md(f":material/flag: Your goal: **{fmt_money0(float(plan['target_amount']))}** by "
                f"**{_fmt_month(plan['target_date'])}**")
            st.button("Change", key="gs_goal_change", type="tertiary", on_click=_goal_part,
                      args=("what",))
    tip = _suggest(value, plan)
    back = ((_goal_part, (pkeys[n - 1],)) if n else None)

    if part == "what":
        p = plan or {}
        if not st.session_state.get("gs_goal_type"):
            st.session_state["gs_goal_type"] = p.get("goal_type") or _fs_goal_fallback()
        st.session_state.setdefault("gs_goal_target", float(p.get("target_amount") or 0.0))
        st.session_state.setdefault(
            "gs_goal_date", date.fromisoformat(p["target_date"][:10]) if p.get("target_date")
            else tip["goal_date"])
        st.pills("What are you saving for?", plans.GOAL_TYPES, key="gs_goal_type", required=True)
        c1, c2 = st.columns(2)
        c1.number_input("I want to have ($)", min_value=0.0, step=1000.0, format="%.0f",
                        key="gs_goal_target",
                        help="A rough number is fine - you can change it any time.")
        c2.date_input("by (date)", key="gs_goal_date",
                      min_value=min(st.session_state["gs_goal_date"], plans.add_months(today, 1)),
                      max_value=date(today.year + 80, 12, 31), format="MM/DD/YYYY")
        _suggestion_line(f"by {_fmt_month(tip['goal_date'].isoformat())} - "
                         f"{_years_text(tip['goal_years'])} from now, from your timeline",
                         key="gs_goal_date_use", values={"gs_goal_date": tip["goal_date"]})
        err = st.session_state.pop("gs_goal_err", None)
        if err:
            st.error(err)
        st.button("Save my goal ✓", key="gs_goal_save", type="primary", width="stretch",
                  on_click=_save_goal_what)
    elif part == "monthly":
        need = tip["monthly"]
        st.session_state.setdefault(
            "gs_goal_monthly", float((plan or {}).get("monthly_contribution") or need or 0.0))
        if need:
            _md(f"About **{fmt_money0(need)} a month** gets you to "
                f"{fmt_money0(float(plan['target_amount']))} by {_fmt_month(plan['target_date'])}, "
                f"if your money grows about {_plan_return_pct():g}% a year.")
        elif need == 0:
            _md("What you have now could grow to your goal on its own at "
                f"{_plan_return_pct():g}% a year - anything you add each month gets you there "
                "sooner.")
        st.number_input("I'll invest each month ($)", min_value=0.0, step=25.0, format="%.0f",
                        key="gs_goal_monthly",
                        help="The part of your income you'll put toward this goal each month.")
        if need:
            _suggestion_line(f"{fmt_money0(need)} a month - what reaches your goal",
                             key="gs_goal_monthly_use", values={"gs_goal_monthly": need})
        st.caption("Many people start with a smaller amount and raise it over time - any amount "
                   "counts, and you can change it whenever you like.")
        st.button("Save my monthly amount ✓", key="gs_goal_save", type="primary",
                  width="stretch", on_click=_save_goal_monthly)
    elif part == "how":
        _render_projection(plan, value, today)   # Plan's own "How it's going" (views/plan.py)
        st.button("Got it ✓", key="gs_goal_save", type="primary", width="stretch",
                  on_click=_goal_part_done, args=("how", "mix"))
    else:
        saved = (plan or {}).get("target_alloc") or {}
        st.session_state.setdefault("gs_goal_stocks", int(5 * round(saved["Stocks"] / 5))
                                    if saved.get("Stocks") else tip["stocks_pct"])
        st.markdown("How you'd like to split your money between stocks and bonds. Once you "
                    "invest, your plan shows when your portfolio drifts away from it.")
        st.slider("% in stocks (the rest in bonds)", 0, 100, step=5, key="gs_goal_stocks")
        _two_part_bar(st.session_state["gs_goal_stocks"])
        _suggestion_line(f"{tip['stocks_pct']}% stocks, {100 - tip['stocks_pct']}% bonds - "
                         "the example mix for your answers (An example mix shows how it adds up)",
                         key="gs_goal_stocks_use", values={"gs_goal_stocks": tip["stocks_pct"]})
        if set(saved) - {"Stocks", "Bonds"}:
            st.caption("Saving here sets stocks and bonds only; the Plan page can add cash and "
                       "other targets.")
        st.button("Save and complete this step ✓", key="gs_goal_save", type="primary",
                  width="stretch", on_click=_save_goal_mix)
        st.caption("You can change any of this later on the Plan page.")
    _waypoint_footer("goal", keys, titles, done, pressed, main=False, back=back)


def _basics_topics(monthly, years):
    """The basics, one topic each: (key, icon, title, one line, the full text)."""
    yrs = max(1, int(round(years)))
    put_in = monthly * 12 * yrs
    grown = learn.grow_monthly(monthly, yrs, 6)
    later = learn.grow_monthly(monthly, yrs - 10, 6) if yrs > 10 else None
    fee = learn.fee_cost(monthly, yrs, 6, 0.05, 1.0)
    fmt = lambda v: _usd0(v).replace("$", r"\$")  # noqa: E731
    return [
        ("funds", ":material/category:", "Stocks, bonds and funds", "What you can actually buy",
         "A stock is a small piece of one company. A bond is a loan to a government or company "
         "that pays you interest. A fund holds many stocks or bonds at once; an **ETF** is a "
         "fund you buy and sell like a stock, and an **index fund** simply holds a whole market "
         "(like every US company) instead of trying to pick winners."),
        ("spread", ":material/scatter_plot:", "Why spread it out", "One stumble can't sink you",
         "Any one company can stumble or fail. A total-market fund holds thousands of "
         "companies, so no single one can sink you - that's diversification, and index funds "
         "give it to you in one purchase."),
        ("time", ":material/hourglass_bottom:", "Time does the heavy lifting",
         "Why starting early matters",
         f"Putting in {fmt(monthly)} a month for {yrs} years is {fmt(put_in)} of your own "
         f"money. At 6% a year it could grow to about **{fmt(grown)}**."
         + (f" Starting 10 years later, the same {fmt(monthly)} a month gets to about "
            f"{fmt(later)} - most of the growth comes from the early years." if later else "")
         + "\n\n*An illustration at 6% a year, not a prediction.*"),
        ("fees", ":material/percent:", "Fees add up", "Small percentages, big dollars",
         f"Funds charge a yearly fee called the expense ratio. On {fmt(monthly)} a month for "
         f"{yrs} years, a fund charging 1% instead of 0.05% would leave you about "
         f"**{fmt(fee)} less**. Broad index funds are usually among the cheapest."),
        ("ups", ":material/show_chart:", "Ups and downs are normal", "What drops look like",
         "The US stock market fell about a third in a month in early 2020 and by more than "
         "half in 2007-09, then recovered over the following years. Staying invested through "
         "drops has historically mattered more than timing them. Money you'll need in the next "
         "few years usually belongs in savings instead."),
        ("accounts", ":material/account_balance:", "Account types",
         "Brokerage, Roth IRA, 401(k)",
         "A regular brokerage account has no limits but you pay tax on gains and dividends. A "
         "**Roth IRA** is for retirement: you put in money you've already paid tax on, and it "
         "can grow and come out tax-free later. A **401(k)** through work often comes with an "
         "employer match. IRAs and 401(k)s have yearly limits - check IRS.gov for this year's."),
    ]


@st.dialog("The basics", width="medium")
def _basics_window(key, monthly, years):
    for k, icon, title, _line, body in _basics_topics(monthly, years):
        if k == key:
            st.markdown(f"### {icon} {title}")
            st.markdown(body)
            learn_more(BASICS_LINKS.get(k))
    # (in a window: switch pages with a full rerun, which also closes it)
    if st.button(f":material/forum: Ask {GUIDE} about this", key="basics_ask", type="tertiary"):
        _ask_coach("basics")
        st.rerun()


def _step_basics(monthly, years):
    st.caption("Six short ideas worth knowing before you invest. Open any of them.")
    topics = _basics_topics(monthly, years)
    cols = st.columns(3)
    for n, (k, icon, title, line, _body) in enumerate(topics):
        with cols[n % 3].container(border=True, key=f"pt_tile_basics_{k}"):
            st.markdown(f"{icon} **{title}**")
            st.caption(line)
            if st.button("Read", key=f"basics_{k}", type="tertiary",
                         icon=":material/open_in_new:"):
                _basics_window(k, monthly, years)
    render_fee_step()   # your own funds' fees, once there are holdings (views/fees.py)


def _step_mix(mix, profile, plan):
    if mix is None:
        st.caption("Answer the time horizon question in step 1 to see an example mix.")
        return
    if mix["short_horizon"]:
        st.info("You'll need this money within about 3 years. Money needed that soon usually "
                "goes in a high-yield savings account, CDs or Treasury bills rather than stocks. "
                "This is how a cautious mix would look if you do invest some of it.")
    st.markdown(f"An example for someone with your answers: **{mix['stocks_pct']}% stocks, "
                f"{mix['weights']['bonds']}% bonds.**")
    _render_mix_bar(mix["weights"])
    st.markdown(f"How it adds up to {mix['stocks_pct']}% stocks:  \n"
                + "  \n".join(f"- {r}" for r in mix["reasons"]))
    extra = []
    year = learn.target_date_year(plan, profile.get("age_range"), datetime.now().date())
    prefs_set = set((profile.get("preferences") or "").split("; "))
    if year and ((plan or {}).get("goal_type") == "Retirement"
                 or "Hands-off / set and forget" in prefs_set or "Retirement" in (profile.get("goal") or "")):
        extra.append(f"**One-fund option:** a target-date fund (look for a name with **{year}** "
                     "in it, like \"Target Retirement " + str(year) + "\") holds a mix like this "
                     "in a single fund and gradually shifts toward bonds as that year gets closer.")
    if "Sustainable (ESG) investing" in prefs_set:
        extra.append("**Sustainable investing:** ESG versions of broad index funds exist - for "
                     "example ESGV for US stocks.")
    if "Dividend income" in prefs_set:
        extra.append("**Dividend income:** dividend-focused funds, for example SCHD or VYM, lean "
                     "toward companies that pay regular dividends.")
    for e in extra:
        st.markdown(e)
    st.caption("An example for learning, based on common rules of thumb - not a recommendation "
               "to buy these funds. The tickers are examples of well-known, low-cost index "
               "funds; many similar funds exist.")
    learn_more("asset_allocation")

    def _watch_examples():
        c = connect(DB)
        try:
            for t in learn.PRACTICE_TICKERS.values():
                watchlist.add(c, USER_ID, t)
        finally:
            c.close()
        st.session_state["refresh_msg"] = (
            "toast", "Added " + ", ".join(learn.PRACTICE_TICKERS.values()) + " to your watchlist.")

    with st.container(horizontal=True):
        st.button("Watch these example funds", key="gs_watch", on_click=_watch_examples,
                  help="Adds " + ", ".join(learn.PRACTICE_TICKERS.values())
                       + " to your Watchlist so you can follow their prices.")
        _coach_button("mix")


def _step_practice(mix, plan, profile, value):
    today = datetime.now().date()
    conn = connect(DB)
    try:
        prices = _practice_prices(conn)
    finally:
        conn.close()
    have = [p for p in prices.values() if p]
    first = max(p[0][0] for p in have) if len(have) == len(prices) else None
    last = min(p[-1][0] for p in have) if have else None
    years_avail = ((today - date.fromisoformat(first)).days / 365.25) if first else 0
    stale = not last or (today - date.fromisoformat(last)).days > 7
    if years_avail < 5 or stale:
        st.caption("The practice portfolio uses real past prices for "
                   + ", ".join(learn.PRACTICE_TICKERS.values()) + ". Load them first (takes a "
                   "few seconds).")
        if st.session_state.pop("gs_prices_failed", False):
            st.info(":material/cloud_off: Couldn't load past prices right now - try again "
                    "later.")
        if st.button("Load price history", key="gs_load_prices", type="primary"):
            before = sum(len(p) for p in prices.values())
            try:
                import sync_history
                with st.spinner("Loading 10 years of prices..."):
                    sync_history.sync(DB, list(learn.PRACTICE_TICKERS.values()), period="10y",
                                      with_intraday=False, with_info=False, delay=0.0)
                conn = connect(DB)
                try:
                    got = sum(len(p) for p in _practice_prices(conn).values())
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 - no prices is a calm note, never an error page
                got = before
            # nothing new came back (the price source unreachable, or busy):
            # say so after the rerun instead of leaving the button as it was
            st.session_state["gs_prices_failed"] = got <= before
            st.rerun()
        if not first:
            return

    tip_monthly = _suggest(value, plan)["monthly"]
    st.session_state.setdefault(
        "gs_monthly", float((plan or {}).get("monthly_contribution") or tip_monthly or 200.0))
    st.session_state.setdefault("gs_initial", 0.0)
    c1, c2 = st.columns(2)
    monthly = c1.number_input("How much you'll invest each month ($)", min_value=0.0, step=50.0,
                              format="%.0f", key="gs_monthly",
                              help="The part of your income you'd put in each month.")
    initial = c2.number_input("Amount to start with ($)", min_value=0.0, step=100.0,
                              format="%.0f", key="gs_initial",
                              help="Money put in on the first day, if any - 0 is fine.")
    if tip_monthly:
        _suggestion_line(f"{fmt_money0(tip_monthly)} a month - what reaches your goal",
                         key="gs_monthly_use", values={"gs_monthly": tip_monthly})
    span_opts = [y for y in (1, 3, 5, 10) if y <= years_avail + 0.05] or [1]
    years = st.segmented_control("When you'd have started", span_opts, default=span_opts[-1],
                                 key="gs_years",
                                 format_func=lambda y: f"{y} year{'s' if y != 1 else ''} ago") \
        or span_opts[-1]
    which = st.segmented_control("Mix to practice with", PRACTICE_MIXES,
                                 default=PRACTICE_MIXES[0], key="gs_mix") or PRACTICE_MIXES[0]
    stocks = {"Example mix": (mix or {}).get("stocks_pct", 60), "All stocks": 100,
              "Mostly bonds": 20}[which]
    us = round(stocks * learn.US_SHARE_OF_STOCKS)
    t = learn.PRACTICE_TICKERS
    weights = {t["us"]: us, t["intl"]: stocks - us, t["bonds"]: 100 - stocks}
    start = plans.add_months(today, -12 * years).isoformat()
    rows = learn.simulate(prices, weights, monthly=monthly, initial=initial, start=start)
    if len(rows) < 2 or not rows[-1]["money_in"]:
        st.caption("Enter an amount to see how it would have gone.")
        return
    end = rows[-1]
    growth = end["value"] - end["money_in"]
    dd = learn.max_drawdown(rows)
    tone = "pt-up" if growth > 0 else "pt-down" if growth < 0 else ""
    st.html(_stat_row(
            "<div class='pt-stats' role='list' aria-label='How it would have gone'>"
            f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Put in</div>"
            f"<div class='pt-stat-value'>{_usd0(end['money_in'])}</div></div>"
            f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Worth today</div>"
            f"<div class='pt-stat-value'>{_usd0(end['value'])}</div>"
            f"<div class='pt-stat-sub {tone}'>{'+' if growth >= 0 else ''}{_usd0(growth)}</div></div>"
            f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Worst drop</div>"
            f"<div class='pt-stat-value'>{dd:.0f}%</div></div></div>"))
    df = pd.DataFrame(rows[::5] + ([rows[-1]] if len(rows) % 5 != 1 else []))
    df["date"] = pd.to_datetime(df["date"])
    palette = SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT
    st.altair_chart(charts.money_in_chart(df, money_color=SERIES_OTHER, value_color=palette[0]),
                    width="stretch")
    mix_words = f"{stocks}% stocks / {100 - stocks}% bonds ({', '.join(f'{k} {v}%' for k, v in weights.items() if v)})"
    reaction = profile.get("drawdown_reaction")
    _md(f"Starting {_fmt_month(rows[0]['date'])} with {_usd0(initial)} and {_usd0(monthly)} a "
        f"month in {mix_words}. At its worst, the mix was **{abs(dd):.0f}% below its high**"
        + (f" - you said you'd *{reaction.lower()}* after a 20% drop. Selling during a drop "
           "locks in the loss; the chart shows what staying in would have looked like."
           if reaction in ("Sell everything", "Sell some") and dd <= -15 else "."))
    st.caption("Real past prices with dividends reinvested; no fees or taxes; nothing is "
               "rebalanced. Past results don't predict future ones - this is practice, not "
               "a forecast.")
    _coach_button("practice")


# ---- Start investing: a brokerage, the account, a first buy, bring it in ---- #
# The account checklist they tick off (saved in prefs "account_steps", the
# same keys as before the route had two stages, so old ticks carry over):
# (key, the step, a line about it). "chosen" is ticked by completing Choose
# a brokerage; Open your account shows "opened" and "funded"; Your first
# investments "first_buy" and "monthly" (ACCOUNT_TICKS).
ACCOUNT_STEPS = (
    ("chosen", "Chosen a brokerage", "Choose a brokerage, the step before."),
    ("opened", "Opened the account",
     "Usually online, in one sitting - choose the account type as you go."),
    ("funded", "Moved money in",
     "Link your bank and move in what you'd like to start with - any amount is fine."),
    ("first_buy", "Made a first buy", "What it looks like is just above."),
    ("monthly", "Set up a monthly amount (any time)",
     "Automatic investing each month, so it happens without you having to remember."),
)
ACCOUNT_TICKS = {"account": ("opened", "funded"), "first": ("first_buy", "monthly")}


def _account_ticks():
    return set(_read_prefs().get("account_steps") or [])


def _tick_account(*keys, done=None):
    """Tick (or untick) checklist steps; `done` None reads the checkbox."""
    p = _read_prefs()
    ticks = set(p.get("account_steps") or [])
    for k in keys:
        on = st.session_state.get(f"gs_acct_{k}") if done is None else done
        (ticks.add if on else ticks.discard)(k)
        st.session_state[f"gs_acct_{k}"] = bool(on)
    p["account_steps"] = [k for k, _, _ in ACCOUNT_STEPS if k in ticks]
    _write_prefs(p)


def _account_checklist(step, monthly):
    """The ticks that belong to a waypoint (ACCOUNT_TICKS), saved as they're
    ticked."""
    ticks = _account_ticks()
    lines = {k: (label, line) for k, label, line in ACCOUNT_STEPS}
    for k in ACCOUNT_TICKS[step]:
        label, line = lines[k]
        if k == "monthly" and monthly:
            line = (f"Automatic investing of {_usd0(monthly)} a month - the amount in your plan "
                    "- so it happens without you having to remember.")
        st.session_state.setdefault(f"gs_acct_{k}", k in ticks)
        st.checkbox(f"**{label}** - {line}".replace("$", r"\$"), key=f"gs_acct_{k}",
                    on_change=_tick_account, args=(k,))


def _first_buy_steps():
    """What a first buy looks like, with an example ticker already shown in
    An example mix - the steps, not a recommendation."""
    t = learn.PRACTICE_TICKERS["us"]
    _md(f"Every brokerage's screens look a little different, but a first buy usually goes like "
        f"this - here with **{t}**, one of the example funds from *An example mix*:\n\n"
        f"1. **Search the ticker.** Type {t} into the brokerage's search or Trade box.\n"
        "2. **Choose a dollar amount.** Many brokerages let you buy in dollars (fractional "
        "shares), so you can enter $100 rather than a number of whole shares.\n"
        "3. **Review.** Check the ticker, the amount and the order type - a *market order* buys "
        "at the going price while the market is open.\n"
        "4. **Confirm.** The shares show up in your account, usually within moments.")
    st.caption(f"An example of the steps, not a recommendation to buy {t} or any other fund.")


def _advisor_start_line():
    """A managed client's Start investing, in their advisor's voice (the
    advisor viewing it sees whose it is)."""
    if ON_CLIENT:
        return (f"{ACTIVE_NAME}'s Start investing is yours: bring their statements in once the "
                f"account is open - {ACTIVE_NAME} sees their portfolio as soon as it's saved.")
    return (f"Your advisor, {_advisor_display_name()}, helps you open the account and brings "
            "your statements in - your portfolio shows up on Home once they have.")


def _step_brokerage():
    import brokerages

    st.markdown(brokerages.INTRO)
    st.markdown(brokerages.compare_markdown())
    learn_more("account_types")
    st.markdown(f"**{brokerages.LIST_INTRO}**")
    st.markdown(brokerages.list_markdown())
    st.caption(brokerages.OTHERS)
    st.markdown(f":material/balance: {brokerages.NOT_RANKED} {brokerages.CHECK_SITES}")
    learn_more("brokerage_accounts")
    _coach_button("brokerage")


def _step_open_account(has_holdings, items, monthly):
    if any(i["key"] == "emergency_fund" and i["state"] in (learn.CAUTION, learn.STOP)
           for i in items):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(":material/savings: Many people finish a 3-6 month emergency fund "
                        "first - there's no rush to open an account.", width="stretch")
            st.button("See where you stand", key="gs_acct_ready", type="tertiary",
                      on_click=_gs_go, args=("ready",))
    st.markdown("On the brokerage's website or app, look for **Open an account**. You'll "
                "usually need your ID and your bank details, and you'll pick the kind of "
                "account - a regular brokerage account, a Roth IRA, or both.")
    learn_more("account_types")
    if has_holdings:
        st.caption(":material/science: You're exploring with the example portfolio. When you "
                   "bring in your own, it replaces the example.")
    st.markdown("**Your checklist**")
    _account_checklist("account", monthly)
    _coach_button("account")


def _step_first(profile, plan, mix, kind, horizon, monthly):
    import starter_funds

    target = (plan or {}).get("target_alloc") or {}
    if target.get("Stocks") is not None:
        st.markdown(":material/explore: **Your direction:** the target mix you set - "
                    f"{target['Stocks']:g}% stocks, {target.get('Bonds', 0):g}% bonds"
                    + (f" ({kind['name']})." if kind else "."))
    elif mix:
        st.markdown(":material/explore: **Your direction:** "
                    + (f"{kind['name']} - " if kind else "")
                    + f"an example mix of {mix['stocks_pct']}% stocks, "
                    f"{mix['weights']['bonds']}% bonds, from your answers.")
    st.markdown("#### What your first buy looks like")
    _first_buy_steps()
    if not CLIENT_MODE:   # never beside an advisor's recommendations
        with st.expander("Not sure what to start with?", icon=":material/help:"):
            starter_funds.render(profile, horizon, db=DB, user_id=USER_ID, key="gs_starter")
    st.markdown("**Your checklist**")
    _account_checklist("first", monthly)
    _coach_button("first")


def _step_bring(real, has_holdings, managed):
    if real:
        st.markdown("You've brought in your holdings - **Home** shows your real portfolio and "
                    "the **Plan** tracks it against your goal.")
        st.button("Open Home", key="gs_open_dash", type="tertiary", on_click=_go,
                  args=("Dashboard",))
        return
    if managed:
        st.markdown(_advisor_start_line())
        if not CAN_IMPORT:
            _coach_button("account")
            return
    else:
        st.markdown("Bring in what you own from **any brokerage**: paste your positions from "
                    "its website, read screenshots, type them in, or upload a CSV. You'll check "
                    "everything before it's saved.")
    st.caption(":material/lock: " + TRUST_LINE)
    if has_holdings:
        st.caption(":material/science: You're exploring with the example portfolio. When you "
                   "bring in your own, it replaces the example.")
    st.button(":material/upload_file: Upload a CSV instead", key="gs_import_csv",
              type="tertiary", on_click=_open_holdings_dialog, args=("import",))


def _route_state(has_holdings):
    """Where this account is on Get started's route - shared with the Home
    page's "Your route" card (route.py). Returns a dict: profile, missing
    (unanswered profile questions), items (readiness), plan, horizon (years
    to the goal date, or None), done ({waypoint key: bool}), pressed,
    learn_required (Learn is on their route - route.learn_first), managed
    (an advisor's client: Start investing is their advisor's), real (their
    own holdings are in), route (their waypoint keys in order), shown (every
    waypoint Learn shows them) and waypoints ([(key, title, done)] along
    their route)."""
    import advisor

    today = datetime.now().date()
    profile = _profile()   # read once per run (dashboard.py)
    plan = load_plan()
    missing = advisor.missing_fields(profile)
    items = learn.readiness(profile)
    p = _read_prefs()
    manual = set(p.get("get_started_done") or [])
    ticks = set(p.get("account_steps") or [])
    horizon = (plans.months_until(plan["target_date"], today) / 12
               if plans.has_goal(plan) and plans.months_until(plan["target_date"], today) > 0 else None)
    # Set a goal is walked in Learn (its last part completes it). Its gear,
    # the compass, already shown means the goal was set before that walk
    # existed: still complete, so nothing earned is lost. A client's goal is
    # their advisor's to set.
    walked = ("goal" in manual or not CAN_MANAGE
              or "compass" in (p.get("gear_seen") or []))
    # their own holdings brought in - the example portfolio is for looking
    # around, not an account they've opened. Once they're in, every Start
    # investing waypoint is behind them.
    real = bool(has_holdings and HAS_REAL_HOLDINGS)
    done = {
        "profile": not missing,
        "ready": all(i["state"] != learn.UNKNOWN for i in items),
        "goal": plans.has_goal(plan) and walked,
        "basics": "basics" in manual,
        "mix": "mix" in manual,
        "practice": "practice" in manual,
        # the account checklist's ticks - the same keys as the one-waypoint
        # checklist before, so old ticks count here
        "brokerage": real or "brokerage" in manual or bool(ticks & {"chosen", "opened"}),
        "account": real or {"opened", "funded"} <= ticks,
        "first": real or "first_buy" in ticks,
        "bring": real,
    }
    managed = CLIENT_MODE
    learn_required = route.learn_first(profile.get("experience"), real, managed)
    keys = route.route_keys(learn_required, managed)
    titles = dict(GET_STARTED_STEPS)
    return {"profile": profile, "missing": missing, "items": items, "plan": plan,
            "horizon": horizon, "done": done, "pressed": manual,
            "learn_required": learn_required, "managed": managed, "real": real,
            "route": list(keys),
            "shown": list(route.stage_keys(route.LEARN, managed)
                          + route.stage_keys(route.INVEST, managed)),
            "waypoints": [(k, titles[k], done[k]) for k in keys]}


# ---- client mode: Home's next step in their advisor's voice --------------- #

def _advisor_waiting():
    """What their advisor left for this account (advising.waiting_for_client),
    read once per run."""
    if "advisor_waiting" not in _RUN:
        c = connect(DB)
        try:
            _RUN["advisor_waiting"] = advising.waiting_for_client(c, USER_ID)
        finally:
            c.close()
    return _RUN["advisor_waiting"]


def _client_step(state, has_holdings):
    """An advisor's client's next step (route.advisor_step), or None - also
    for anyone else (CLIENT_MODE), so the usual route decides."""
    if not CLIENT_MODE:
        return None
    w = _advisor_waiting()
    return route.advisor_step(proposals_waiting=w["proposals"], reports_new=w["reports"],
                              profile_missing=bool(state["missing"]),
                              has_holdings=has_holdings)


def _client_step_words(step):
    """(title, line, button label, action) for route.advisor_step()."""
    notes = _label("Advisor notes")
    k = step["key"]
    if k == "proposal":
        return ("Your advisor has a proposal waiting for you",
                "A suggested mix, with a note on why. Read it and let them know what you think "
                "- nothing is bought or sold until you decide together.",
                "Read the proposal", ("page", "Advisor notes"))
    if k == "report":
        return ("A new progress report from your advisor",
                "How your investments have been doing, with a note from them.",
                "Read the report", ("page", "Advisor notes"))
    if k == "profile_advisor":
        return ("Answer a few questions for your advisor",
                "Your timeline and how you feel about ups and downs - a few taps, so they can "
                "prepare for your conversations.", "Answer the questions", ("learn", "profile"))
    if CAN_IMPORT:   # bring_advisor
        return ("Bring your statements in with your advisor",
                "Your advisor can bring them in for you, or you can add them yourself - paste "
                "them from any brokerage, type them in or upload a CSV.", "Add holdings",
                ("dialog", "manual"))
    return ("Bring your statements in with your advisor",
            "Your advisor brings them in - your portfolio shows up here once they have. How "
            f"to reach them is under {notes}.", f"Open {notes}", ("page", "Advisor notes"))


def _on_the_route():
    """Someone still walking their route (the investor experience, a
    waypoint not complete yet): Plan offers a clear way back to it."""
    if not INVESTOR_VIEW or "Get started" not in PAGES:
        return False
    return not all(d for _, _, d in _route_state(HAS_HOLDINGS)["waypoints"])


def _where_html(state):
    """"You're in Learn · step 3 of 6 · The foothills": the stage and step of
    their first waypoint not complete, and its region (route.REGIONS) - on
    Learn and on Home's route card."""
    waypoints = state["waypoints"]
    here = next((k for k, _, d in waypoints if not d), None)
    region, _nxt = route.region(waypoints)
    if here is None:
        return (f"<div class='pt-region'>Every step of your route is complete · "
                f"<b>{html.escape(region)}</b></div>")
    then = (f" · then {route.STAGE_NAMES[route.INVEST]}"
            if route.stage_of(here) == route.LEARN else "")
    return (f"<div class='pt-region'>You're in "
            f"<b>{html.escape(route.stage_words(here, state['managed']))}</b> · "
            f"{html.escape(region)}{then}</div>")


def _ask_type(name):
    st.session_state["coach_prompt"] = (
        f"Northwend says I'm a \"{name}\". Explain what that means for someone like me, what "
        "the example mix is built from, and what I should understand before investing. Use "
        "examples, not recommendations.")
    st.session_state["page"] = "AI Assistant"


def _render_direction(kind, mix):
    """The "Find your direction" card (ROADMAP G3): the investor type from the
    answers, with the example mix and the kinds of funds that usually fill it."""
    with st.container(border=True, key="pt_direction"):
        st.html(f"<div class='pt-route-label'>Your direction</div>"
                f"<div class='pt-type-name'>{html.escape(kind['name'])}</div>"
                f"<div class='pt-type-line'>{html.escape(kind['line'])}</div>")
        st.markdown(kind["about"])
        if kind["key"] not in ("short_term",):
            st.markdown(f"**An example mix for this type:** {mix['stocks_pct']}% stocks, "
                        f"{mix['weights']['bonds']}% bonds.")
            _render_mix_bar(mix["weights"])
        st.markdown("**Kinds of funds that usually fill it:**  \n"
                    + "  \n".join(f"- {k}" for k in kind["kinds"]))
        st.markdown(f":material/info: {kind['watch']}")
        st.caption("A common rule of thumb for learning, from your answers - not a "
                   "recommendation to buy anything. Change your answers in waypoint 1 any time.")
        st.button(f":material/forum: Ask {GUIDE} what this means for me", key="type_ask",
                  type="tertiary", on_click=_ask_type, args=(kind["name"],))


def _gs_go(key):
    st.session_state["gs_at"] = key
    st.session_state.pop("gs_goal_part", None)   # Set a goal opens at its first open part


def _gs_pick():
    if st.session_state.get("gs_pick"):
        _gs_go(st.session_state["gs_pick"])


@st.dialog("Your direction", width="large")
def _direction_window(kind_key):
    state = _route_state(st.session_state.get("gs_has_holdings", False))
    mix = learn.starter_mix(state["profile"], state["horizon"])
    kind = learn.investor_type(state["profile"], mix, state["items"])
    if kind:
        _render_direction(kind, mix)
    if st.session_state.get("page") == "AI Assistant":   # its Ask button: go there
        st.rerun()


def _progress_html(stage, keys, titles, done, at, optional=False):
    """The stage's progress bar: "Learn · step 3 of 6 · 2 complete" over one
    segment per waypoint of the stage - complete ones filled, the open one
    outlined - like the trail."""
    n, n_done = len(keys), sum(done[k] for k in keys)
    name = route.STAGE_NAMES[stage] + (" (optional for you)" if optional else "")
    words = (f"{name} · all {n} steps complete" if n_done == n else
             f"{name} · step {keys.index(at) + 1} of {n} · {n_done} complete")
    segs = "".join(f"<span class='pt-steps-seg{' pt-steps-done' if done[k] else ''}"
                   f"{' pt-steps-at' if k == at else ''}' "
                   f"title='{html.escape(titles[k], quote=True)}'></span>" for k in keys)
    label = html.escape(route.STAGE_NAMES[stage], quote=True)
    return ("<div class='pt-steps'>"
            f"<div class='pt-steps-top'><b>{html.escape(words)}</b>"
            f"<span>{round(100 * n_done / n)}%</span></div>"
            f"<div class='pt-steps-bar' role='progressbar' aria-label='{label}' "
            f"aria-valuemin='0' aria-valuemax='{n}' aria-valuenow='{n_done}' "
            f"aria-valuetext='{n_done} of {n} steps complete'>{segs}</div></div>")


def _gs_stage(first_of):
    """The stage switch: open that stage at its first waypoint not complete."""
    stage = st.session_state.get("gs_stage")
    if stage in first_of:
        _gs_go(first_of[stage])


def _render_get_started(has_holdings, value):
    import advisor

    if first_steps_active(has_holdings):   # a new investor: the slideshow (first_steps.py)
        render_first_steps(has_holdings)
        return

    st.session_state["gs_has_holdings"] = has_holdings
    state = _route_state(has_holdings)
    profile, missing, items, plan = (state["profile"], state["missing"], state["items"],
                                     state["plan"])
    horizon, done, pressed = state["horizon"], state["done"], state["pressed"]
    managed, real = state["managed"], state["real"]
    mix = learn.starter_mix(profile, horizon)
    monthly = float((plan or {}).get("monthly_contribution") or 0.0)
    years = horizon or float(profile.get("time_horizon_years") or 20)
    kind = learn.investor_type(profile, mix, items)
    titles = dict(GET_STARTED_STEPS)
    mine, shown = state["route"], state["shown"]   # their route; every waypoint here
    by_stage = {s: list(route.stage_keys(s, managed)) for s in (route.LEARN, route.INVEST)}
    optional = {route.LEARN: not state["learn_required"], route.INVEST: False}

    def nav_keys(k):
        """Where Complete / Skip / Back move: along their route, or within
        optional Learn for someone whose route starts at Start investing."""
        return mine if k in mine else by_stage[route.stage_of(k)]

    # which waypoint is open: their first not complete, unless they picked
    # one; Complete this step and Skip for now (_complete, _skip) move on to
    # the next one not complete
    after = st.session_state.pop("gs_advance", None)
    if after in shown:
        st.session_state["gs_at"] = _next_open(after, nav_keys(after), done) or after
    at = st.session_state.get("gs_at")
    if at not in shown:
        at = route.opening(mine, shown, done)
    # kept open until they move on: answering a waypoint's last question
    # completes it, but they stay to see it and press Complete this step
    st.session_state["gs_at"] = at
    stage = route.stage_of(at)
    keys = nav_keys(at)
    stage_keys = by_stage[stage]

    # ---- the two stages, and how far along this one is ---------------------- #
    first_of = {s: next((k for k in ks if not done[k]), ks[0]) for s, ks in by_stage.items()}
    st.session_state["gs_stage"] = stage   # always the open one's
    st.segmented_control(
        "Stage", [route.LEARN, route.INVEST], key="gs_stage", label_visibility="collapsed",
        on_change=_gs_stage, args=(first_of,), required=True,
        format_func=lambda s: (("✓ " if all(done[k] for k in by_stage[s]) else "")
                               + route.STAGE_NAMES[s]
                               + (" (optional)" if optional[s] else "")))
    st.html(_progress_html(stage, stage_keys, titles, done, at, optional[stage]))
    if all(done[k] for k in mine):
        st.success("Every step of your route is complete. Home keeps track of your goal from "
                   "here, and these pages are here whenever you'd like a refresher.",
                   icon=":material/flag:")
    elif managed and stage == route.LEARN:
        st.caption(":material/info: Learn is here whenever you'd like it - short reads on the "
                   "basics. Your plan is made with your advisor: see Plan and "
                   f"{_label('Advisor notes')}.")
    elif optional[stage]:
        st.caption(":material/info: Learn is optional for you - the basics are here whenever "
                   "you'd like them. Your route starts at Start investing.")

    # ---- their route drawn as a trail, then this stage's waypoints to tap ---- #
    waypoints = state["waypoints"]
    n_done = sum(d for _, _, d in waypoints)
    st.html(route.trail_html(route.dots(waypoints, False),
                             f"{n_done} of {len(waypoints)} waypoints reached")
            + _where_html(state))
    st.session_state["gs_pick"] = at   # always the open one
    st.pills("Waypoints", stage_keys, key="gs_pick", label_visibility="collapsed",
             on_change=_gs_pick,
             format_func=lambda k: ("✓ " if done[k] else f"{stage_keys.index(k) + 1}. ")
             + titles[k])

    # ---- the open waypoint: why, the step, then Complete this step -------- #
    with st.container(border=True, key=f"pt_slide_gs_{at}"):
        st.caption(route.stage_words(at, managed)
                   + (" · :material/check_circle: Complete" if done[at] else ""))
        st.markdown(f"### {titles[at]}")
        st.html(f"<div class='pt-why'>{html.escape(WAYPOINT_WHY[at])}</div>")
        footer = {}
        ticks_why = ("Tick the steps above as you do them to complete this step - or skip it "
                     "for now and come back later.")
        if at == "profile":
            _step_profile(advisor, profile, missing)
            footer = {"ready": not missing,
                      "why_not": "Answer the questions above to complete this step - or skip "
                                 "it for now and come back later."}
        elif at == "ready":
            _step_ready(items, profile)
            footer = {"ready": done["ready"],
                      "why_not": "Answer the questions above to complete this step - or skip "
                                 "it for now and come back later."}
        elif at == "goal":
            _step_goal(plan, value, profile, keys, titles, done, "goal" in pressed)
            footer = None   # its parts have their own buttons
        elif at == "basics":
            _step_basics(monthly or 200.0, years)
        elif at == "mix":
            _step_mix(mix, profile, plan)
        elif at == "practice":
            _step_practice(mix, plan, profile, value)
        elif at == "brokerage":
            _step_brokerage()
        elif at == "account":
            _step_open_account(has_holdings, items, monthly)
            footer = {"ready": done["account"], "why_not": ticks_why}
        elif at == "first":
            _step_first(profile, plan, mix, kind, horizon, monthly)
            footer = {"ready": done["first"], "why_not": ticks_why}
        else:
            _step_bring(real, has_holdings, managed)
            if not real:
                # what completes it: straight to the paste / import window
                footer = ({"action": ("Bring it in to complete this step",
                                      _open_holdings_dialog, ("manual",), "gs_import")}
                          if CAN_IMPORT else
                          {"ready": False, "why_not": "This step completes when your advisor "
                                                      "brings your statements in."})
        if footer is not None:
            _waypoint_footer(at, keys, titles, done, at in pressed, **footer)

    # ---- your direction, in one line (the whole card in a window) --------- #
    # (not for an advisor's client: its example mix could cross their advisor's)
    if kind and not missing and not managed:
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            st.html(f"<span class='pt-route-label'>Your direction</span><br>"
                    f"<b>{html.escape(kind['name'])}</b> - {html.escape(kind['line'])}",
                    width="stretch")
            if st.button("See your mix", key="gs_direction", type="tertiary",
                         icon=":material/open_in_new:"):
                _direction_window(kind["key"])
    st.caption("Learn explains and shows examples; it never tells you what to buy.")
    if not IS_ADVISOR and USER_ID == LOGIN_ID:
        st.button(":material/replay: Go through the first steps again", key="fs_restart",
                  type="tertiary", on_click=_fs_restart)
    check_milestones(value)   # a waypoint just reached may earn gear (views/kit.py)
