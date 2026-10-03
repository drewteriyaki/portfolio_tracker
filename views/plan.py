# Part of dashboard.py, which runs this file with _view("plan") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# The Plan page: goal, progress, contributions, money in vs growth, target mix.
# ruff: noqa: F821

# ---- Plan page ------------------------------------------------------------ #
# status -> (label, css tone); the label always goes with the color
PLAN_STATUS = {
    "reached": ("Goal reached", "pt-up"),
    "starting": ("Starting out", "pt-start"),   # nothing invested yet: neutral, never red
    "on_track": ("On track", "pt-up"),
    "within_reach": ("Within reach", "pt-warn"),
    "behind": ("Behind", "pt-down"),
    "past_date": ("Date passed", "pt-muted"),
}
# the profile's goal answers -> the plan's goal types
_GOAL_FROM_PROFILE = {"Retirement": "Retirement", "Buy a home": "Buy a home",
                      "Pay for education": "Pay for education",
                      "Build long-term wealth": "Build long-term wealth",
                      "Save for a big purchase": "Big purchase"}
RETURN_CHOICES = tuple(float(p) for p in range(2, 11))


def _fmt_month(d):
    """'Jan 2055' from '2055-01-01'."""
    try:
        d = datetime.strptime(str(d)[:10], "%Y-%m-%d")
    except ValueError:
        return str(d)
    return f"{d:%b} {d.year}"


def _time_left(months):
    if months <= 0:
        return "no time left"
    y, m = divmod(months, 12)
    parts = ([f"{y} year{'s' if y != 1 else ''}"] if y else []) + \
            ([f"{m} month{'s' if m != 1 else ''}"] if m else [])
    return " ".join(parts) + " left"


def _plan_author(plan):
    """Who last saved the plan, from the viewer's side. Only the owner and
    their advisor can save it, so anyone else an owner sees is the advisor."""
    by = plan.get("set_by")
    if not by or by == LOGIN_ID:
        return "Set by you"
    conn = connect(DB)
    try:
        name = auth.get_username(conn, by) or "someone else"
    finally:
        conn.close()
    return f"Set by your advisor {name}" if USER_ID == LOGIN_ID else f"Set by {name}"


def _plan_return_pct():
    """The assumed yearly return for projections: the slider's value this
    session, else the saved one, else plans.DEFAULT_RETURN_PCT."""
    if "plan_return" not in st.session_state:
        saved = _read_prefs().get("plan_return_pct")
        st.session_state["plan_return"] = float(saved) if saved in RETURN_CHOICES \
            else plans.DEFAULT_RETURN_PCT
    return st.session_state["plan_return"]


def _goal_progress(plan, value):
    return plans.progress(plan, value or 0.0, today=datetime.now().date(),
                          return_pct=_plan_return_pct())


# ---- suggested starting points (learn.suggestions) - Plan and Learn ---------- #
SUGGEST_LEAD = "Suggested starting point for your answers"
SUGGEST_HELP = ("Worked out from your answers as a place to start - not advice. Pick whatever "
                "suits you; you can change it any time.")


def _suggest(value=None, plan=None):
    """learn.suggestions() for this account: its answers, its plan (`plan`
    when the caller has a fresher one), what's invested now, and the plan's
    assumed yearly return."""
    return learn.suggestions(_profile(), plan if plan is not None else load_plan(),
                             today=datetime.now().date(), present=float(value or 0.0),
                             return_pct=_plan_return_pct())


def _set_state(**values):
    """A "Use the suggestion" button: put the suggestion in its fields
    (a callback, so it lands before they're drawn again)."""
    st.session_state.update(values)


def _suggestion_line(text, key=None, values=None):
    """"Suggested starting point for your answers: X" and, with `values`
    ({widget key: value}), a small Use the suggestion button that fills them
    in - shown as in use when they already hold it."""
    in_use = bool(values) and all(st.session_state.get(k) == v for k, v in values.items())
    with st.container(horizontal=True, vertical_alignment="center", gap="small",
                      key=f"pt_suggest_{key}" if key else None):
        st.caption(f":material/lightbulb: {SUGGEST_LEAD}: **{text}**".replace("$", r"\$"),
                   help=SUGGEST_HELP, width="stretch")
        if values:
            st.button("In use" if in_use else "Use the suggestion", key=key, type="tertiary",
                      icon=":material/check:" if in_use else None, disabled=in_use,
                      on_click=_set_state, kwargs=values)


def _years_text(n):
    return f"{n} year{'s' if n != 1 else ''}"


def _render_plan_form(plan, today, value=None):
    import advisor

    plan = plan or {}
    conn = connect(DB)
    try:
        profile = advisor.get_profile(conn, USER_ID)
    finally:
        conn.close()
    first_goal = (advisor.split_multi(profile.get("goal")) or [None])[0]
    tip = learn.suggestions(profile, plan, today=today)
    when_default = (datetime.strptime(plan["target_date"], "%Y-%m-%d").date()
                    if plan.get("target_date") else tip["goal_date"])
    if not plans.has_goal(plan):
        st.markdown("#### Set a goal")
        st.caption("What you're saving for, how much you'd like to have, and by when. The plan "
                   "then shows whether you're on track and what it would take to get there.")
    with st.form("plan_form"):
        goal_type = st.pills("What are you saving for?", plans.GOAL_TYPES,
                             default=plan.get("goal_type") or _GOAL_FROM_PROFILE.get(first_goal))
        goal_name = st.text_input("Name it (optional)", value=plan.get("goal_name") or "",
                                  placeholder="e.g. Retire at 60", max_chars=60)
        c1, c2 = st.columns(2)
        target = c1.number_input("I want to have ($)", min_value=0.0, step=1000.0, format="%.0f",
                                 value=float(plan.get("target_amount") or 0.0))
        when = c2.date_input("by (date)", value=when_default,
                             min_value=min(when_default, plans.add_months(today, 1)),
                             max_value=date(today.year + 80, 12, 31))
        if not plan.get("target_date"):
            _suggestion_line(f"by {_fmt_month(tip['goal_date'].isoformat())} - "
                             f"{_years_text(tip['goal_years'])} from now, from your timeline")
        monthly = c1.number_input("I'll invest each month ($)", min_value=0.0, step=50.0,
                                  format="%.0f",
                                  value=float(plan.get("monthly_contribution") or 0.0))
        if plans.has_goal(plan):
            need = _suggest(value, plan)["monthly"]
            if need:
                _suggestion_line(f"{fmt_money0(need)} a month - what reaches this goal at "
                                 f"{_plan_return_pct():g}% a year")
        notes = st.text_area("Notes (optional)", value=plan.get("notes") or "",
                             placeholder="Anything worth remembering about this goal")
        with st.container(horizontal=True):
            save = st.form_submit_button("Save my plan", type="primary")
            cancel = plans.has_goal(plan) and st.form_submit_button("Cancel")
    if save:
        if not goal_type or target <= 0:
            st.error("Pick what you're saving for and an amount above $0.")
            return
        save_plan_fields({"goal_type": goal_type, "goal_name": goal_name.strip() or None,
                          "target_amount": float(target), "target_date": when.isoformat(),
                          "monthly_contribution": float(monthly), "notes": notes.strip() or None})
        st.session_state["plan_editing"] = False
        st.rerun()
    if cancel:
        st.session_state["plan_editing"] = False
        st.rerun()


def fmt_money0(v):
    """Whole dollars ('$1,000,000'), for goals and projections."""
    if _hidden():
        return MASK
    if _blank(v):
        return "—"
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _md(text):
    """st.markdown for text with dollar amounts: a pair of '$' would
    otherwise be read as a math formula."""
    st.markdown(text.replace("$", r"\$"))


def _render_plan_status(plan, value, today):
    rp = _plan_return_pct()
    prog = plans.progress(plan, value or 0.0, today=today, return_pct=rp)
    label, tone = PLAN_STATUS[prog["status"]]
    title = plan.get("goal_name") or plan.get("goal_type") or "Your goal"
    target, when = prog["target"], _fmt_month(plan["target_date"])

    h1, h2 = st.columns([0.8, 0.2], vertical_alignment="center")
    h1.markdown(f"#### {title}")
    if CAN_MANAGE and h2.button("Edit goal", key="plan_edit", width="stretch"):
        st.session_state["plan_editing"] = True
        st.rerun()
    pct = prog["pct_of_target"] or 0.0
    # nothing invested yet: lead with what gets there, not "0% of"
    lead = (f"About {fmt_money0(prog['needed_monthly'])} a month gets you there"
            if prog["status"] == "starting" and prog["needed_monthly"] else
            f"{mask_or(f'{pct:.1f}%')} of {fmt_money0(target)}")
    st.html(
        "<div class='pt-goal'><div class='pt-goal-top'>"
        f"<span class='pt-chip {tone}'>{label}</span>"
        f"<span class='pt-goal-pct'>{lead}</span></div>"
        f"<div class='pt-goal-track' aria-hidden='true'><div class='pt-goal-fill' style='width:{min(100.0, pct):.1f}%'>"
        "</div></div>"
        f"<div class='pt-goal-sub'>{fmt_money0(prog['current'])} now · goal {fmt_money0(target)} by "
        f"{when} · {_time_left(prog['months'])} · "
        f"{fmt_money0(prog['monthly'])}/month planned · {_plan_author(plan)}</div></div>")

    if plan.get("notes"):
        st.caption(f"Notes: {plan['notes']}")


@st.fragment
def _render_projection(plan, value, today):
    """How it's going: where the plan leads at an assumed return (its own
    tab; moving the slider redraws only this)."""
    rp = _plan_return_pct()
    prog = plans.progress(plan, value or 0.0, today=today, return_pct=rp)
    target, when = prog["target"], _fmt_month(plan["target_date"])
    projected, needed = fmt_money0(prog["projected"]), fmt_money0(prog["needed_monthly"])
    status = prog["status"]
    if status == "reached":
        _md("You've reached this goal. Edit it to set the next one.")
    elif status == "past_date":
        _md(f"The goal date has passed with {fmt_money0(target - prog['current'])} still to "
                    "go. Edit the goal to set a new date.")
    elif status == "starting":
        # day one: what it takes, not how far there is to go
        _md(f"About **{needed}** a month gets you to {fmt_money0(target)} by {when}, at "
            f"**{rp:g}%** a year."
            + (f" You've planned {fmt_money0(prog['monthly'])} a month - at that pace you'd "
               f"have about {projected}." if prog["monthly"] else ""))
    elif status == "on_track":
        _md(f"At **{rp:g}%** a year, adding {fmt_money0(prog['monthly'])} a month, you'd have "
                    f"about **{projected}** by {when}.")
    elif status == "within_reach":
        _md(f"At **{rp:g}%** a year you'd have about **{projected}** by {when} - short of "
                    f"the goal unless returns run higher. About **{needed}** a month would get you "
                    "there at this rate.")
    else:
        _md(f"At **{rp:g}%** a year you'd have about **{projected}** by {when}. Reaching "
                    f"{fmt_money0(target)} would take about **{needed}** a month.")

    if prog["months"] > 0:
        # set again just before it's drawn: a value put in session state on an
        # earlier run without the slider (Learn's goal parts read it first)
        # isn't sent to a newly drawn slider, which would show - and then
        # save - its first choice, 2%
        st.session_state["plan_return"] = rp
        st.select_slider("How much your money grows in a typical year (the assumed return)",
                         RETURN_CHOICES, key="plan_return", format_func=lambda v: f"{v:g}%",
                         help="Nobody knows future returns. Long-run averages for a mix of "
                              "stocks and bonds have been somewhere in this range.")
        _suggestion_line(f"{learn.SUGGESTED_RETURN_PCT:g}% a year - a typical middle value",
                         key="plan_return_use",
                         values={"plan_return": learn.SUGGESTED_RETURN_PCT})
        if st.session_state["plan_return"] != _read_prefs().get("plan_return_pct"):
            _p = _read_prefs()
            _p["plan_return_pct"] = st.session_state["plan_return"]
            _write_prefs(_p)
        df = pd.DataFrame(plans.projection_series(
            prog["current"], prog["monthly"], prog["months"], today=today, return_pct=rp))
        df["date"] = pd.to_datetime(df["date"])
        tips = [alt.Tooltip("date:T", title="Date", format="%b %Y")]
        if not _hidden():
            tips += [alt.Tooltip("mid:Q", title=f"At {rp:g}%", format="$,.0f"),
                     alt.Tooltip("low:Q", title=f"At {rp - plans.SPREAD_PCT:g}%", format="$,.0f"),
                     alt.Tooltip("high:Q", title=f"At {rp + plans.SPREAD_PCT:g}%", format="$,.0f")]
        palette = SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT
        st.altair_chart(charts.projection(df, target=target, color=palette[0], mask=_hidden(),
                                          tooltip=tips), width="stretch")
        st.caption(f"The line assumes {rp:g}% a year; the shaded range is "
                   f"{rp - plans.SPREAD_PCT:g}-{rp + plans.SPREAD_PCT:g}%. The dashed line is the "
                   "goal. Before inflation, fees and taxes - an illustration of the plan, not a "
                   "prediction.")
        learn_more("compound_interest")


@st.fragment
def _render_contributions(plan, today):
    conn = connect(DB)
    try:
        window = plans.imported_window(conn, USER_ID)
        moves = plans.money_moves(conn, USER_ID, window=window)   # read once: the month's total too
        recent = plans.list_contributions(conn, USER_ID, limit=10)
    finally:
        conn.close()
    this_month = plans.month_total(None, USER_ID, today.year, today.month, moves=moves)
    moves = moves[:10]
    planned = float((plan or {}).get("monthly_contribution") or 0.0)
    _md(f"This month: **{fmt_money0(this_month)}**"
                + (f" of {fmt_money0(planned)} planned" if planned else ""))
    if CAN_MANAGE:
        with st.expander("Log money added or taken out"):
            with st.form("contribution_form", clear_on_submit=True):
                c1, c2, c3 = st.columns([1, 1, 1])
                kind = c1.segmented_control("Type", ["Added", "Took out"], default="Added")
                amount = c2.number_input("Amount ($)", min_value=0.0, step=50.0, format="%.2f")
                on = c3.date_input("Date", value=today, max_value=today)
                note = st.text_input("Note (optional)", max_chars=100)
                if st.form_submit_button("Save", type="primary"):
                    if amount <= 0:
                        st.error("Enter an amount above $0.")
                    else:
                        c = connect(DB)
                        try:
                            plans.add_contribution(c, USER_ID, on.isoformat(),
                                                   -amount if kind == "Took out" else amount, note)
                        finally:
                            c.close()
                        st.rerun(scope="fragment")
            st.caption("Importing your brokerage's **activity** history adds its deposits and "
                       "withdrawals by itself; a holdings statement doesn't."
                       + (f" Entries you log between {_fmt_date(window[0])} and "
                          f"{_fmt_date(window[1])} aren't counted - that history already has "
                          "the real figures." if window else ""))
            if recent:
                by_id = {r["id"]: r for r in recent}
                c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
                drop = c1.selectbox(
                    "Remove an entry", list(by_id), index=None, placeholder="Pick one to remove",
                    format_func=lambda i: f"{_fmt_date(by_id[i]['date'])}  {_signed_money(by_id[i]['amount'])}"
                                          + (f"  {by_id[i]['note']}" if by_id[i]["note"] else ""))
                if c2.button("Remove", disabled=drop is None, width="stretch"):
                    c = connect(DB)
                    try:
                        plans.delete_contribution(c, USER_ID, drop)
                    finally:
                        c.close()
                    st.rerun(scope="fragment")
    if moves:
        def _note(m):
            tag = ("from your brokerage" if m["source"] == "brokerage"
                   else "not counted - in your imported history" if not m["counted"] else "")
            text = " · ".join(x for x in (m["note"] or "", tag) if x)
            return html.escape(text)
        st.html("<div class='pt-legend'>" + "".join(
            "<div class='pt-legend-row'>"
            f"<span class='pt-legend-val' style='text-align:left'>{_fmt_date(m['date'])}</span>"
            f"<span class='pt-legend-label'>{_note(m)}</span>"
            f"<span class='pt-legend-pct'>"
            + (_tone(m["amount"], _signed_money(m["amount"])) if m["counted"]
               else f"<s>{_signed_money(m['amount'])}</s>")
            + "</span></div>" for m in moves) + "</div>")


@st.fragment
def _render_money_in(value, growth):
    money_in = value - growth
    st.html(_stat_row(
            "<div class='pt-stats' role='list' aria-label='Money in and growth'>"
            f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Money in</div>"
            f"<div class='pt-stat-value'>{fmt_money(money_in)}</div></div>"
            f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Growth</div>"
            f"<div class='pt-stat-value'>{_tone(growth, _signed_money(growth))}</div></div>"
            f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>Value</div>"
            f"<div class='pt-stat-value'>{fmt_money(value)}</div></div></div>"))
    conn = connect(DB)
    try:
        hist = plans.money_in_history(conn, USER_ID)
    finally:
        conn.close()
    if len(hist) >= 2:
        df = pd.DataFrame(hist)
        df["date"] = pd.to_datetime(df["date"])
        palette = SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT
        st.altair_chart(charts.money_in_chart(df, money_color=SERIES_OTHER, value_color=palette[0],
                                              mask=_hidden()), width="stretch")
    st.caption("Money in is what you paid for your holdings plus cash; growth is the rest. "
               + ("The chart uses each statement's own figures. " if len(hist) >= 2 else
                  "Import more statements over time and this becomes a chart. ")
               + "Dividends and gains you've sold count as money in, since they're in the "
                 "account as cash or cost.")


@st.fragment
def _render_target_mix(alloc_rows):
    targets = load_alloc_targets()
    actual = {r["label"]: r["pct"] or 0.0 for r in alloc_rows}
    labels = sorted(set(actual) | set(targets), key=lambda lbl: -(actual.get(lbl) or 0.0))
    if (load_plan() or {}).get("targets_cleared"):
        st.info("Targets are now set by what holdings actually hold - stocks, bonds, cash - "
                "instead of by fund type. The old target mix used ETF / CEF or Mutual Funds, "
                "which can't be translated, so it was cleared. Set it again below.")
    if not targets:
        st.caption("No targets yet. Set a target % for stocks, bonds and cash to see how far "
                   "the portfolio is from its plan.")
    else:
        rows = ""
        for lbl in labels:
            a, t = actual.get(lbl, 0.0), targets.get(lbl)
            diff = "" if t is None else f"{a - t:+.1f} pts"
            rows += ("<div class='pt-legend-row'>"
                     f"<span class='pt-legend-label'>{html.escape(lbl)}</span>"
                     f"<span class='pt-legend-pct'>{mask_or(f'{a:.1f}%')}</span>"
                     f"<span class='pt-legend-val'>target {'—' if t is None else f'{t:g}%'}</span>"
                     f"<span class='pt-legend-val'>{mask_or(diff) if diff else ''}</span></div>"
                     "<div class='pt-mix-track' aria-hidden='true'>"
                     f"<div class='pt-mix-fill' style='width:{min(100.0, a):.1f}%'></div>"
                     + (f"<div class='pt-mix-target' style='left:{min(100.0, t):.1f}%'></div>"
                        if t is not None else "")
                     + "</div>")
        st.html(f"<div class='pt-legend'>{rows}</div>")
        st.caption("The bar is where the portfolio is now; the mark is the target.")
    learn_more("asset_allocation")
    if IS_ADVISOR:
        conn = connect(DB)
        try:
            models = advising.list_models(conn, LOGIN_ID)
        finally:
            conn.close()
        # read fresh here (a fragment); the Proposals tab, drawn after this
        # one in the same full run, uses this read (they change only on Your
        # clients)
        _RUN["models"] = models
        if any(m["target_alloc"] for m in models):
            by_id = {m["id"]: m for m in models if m["target_alloc"]}
            c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
            pick = c1.selectbox("Apply a model portfolio", list(by_id), index=None,
                                placeholder="Pick one of your models",
                                format_func=lambda i: f"{by_id[i]['name']} - "
                                                      f"{advising.mix_text(by_id[i]['target_alloc'])}")
            if c2.button("Apply", disabled=pick is None, width="stretch", key="apply_model"):
                save_alloc_targets(by_id[pick]["target_alloc"])
                for lbl in asset_classes.CLASSES:   # the form below shows the new targets
                    st.session_state.pop(f"plan_target_{lbl}", None)
                st.rerun(scope="fragment")
    if CAN_MANAGE:
        with st.expander("Edit target mix", expanded=not targets):
            tip = _suggest()["target_mix"]
            for lbl in asset_classes.CLASSES:
                st.session_state.setdefault(f"plan_target_{lbl}", float(targets.get(lbl, 0.0)))
            _suggestion_line(", ".join(f"{v:g}% {k.lower()}" for k, v in tip.items())
                             + " - the example mix for your answers", key="plan_target_use",
                             values={f"plan_target_{lbl}": float(tip.get(lbl, 0.0))
                                     for lbl in asset_classes.CLASSES})
            with st.form("target_mix_form", border=False):
                cols = st.columns(len(asset_classes.CLASSES))
                new = {lbl: cols[i].number_input(
                           f"% in {lbl.lower()}", min_value=0.0, max_value=100.0, step=5.0,
                           format="%.0f", key=f"plan_target_{lbl}")
                       for i, lbl in enumerate(asset_classes.CLASSES)}
                if st.form_submit_button("Save target mix", type="primary"):
                    total = sum(new.values())
                    if total and abs(total - 100) > 0.5:
                        st.error(f"The targets add up to {total:g}% - make them total 100%.")
                    else:
                        save_alloc_targets(new)
                        st.rerun(scope="fragment")


def _wi_use_monthly(amount):
    save_plan_fields({"monthly_contribution": float(amount)})
    st.session_state["wi_msg"] = f"Your plan now adds {fmt_money0(amount)} a month."


def _months_text(n):
    y, m = divmod(abs(n), 12)
    return " ".join(([f"{y} year{'s' if y != 1 else ''}"] if y else [])
                    + ([f"{m} month{'s' if m != 1 else ''}"] if m else [])) or "no time"


@st.fragment
def _render_what_if(plan, value, alloc_rows, today):
    """The "what if" playground (ROADMAP G5): change the monthly amount, the
    years and the mix and see the range move. Saves nothing unless asked."""
    has_goal = plans.has_goal(plan)
    present = float(value or 0.0)
    target = float(plan["target_amount"]) if has_goal else None
    base_monthly = float((plan or {}).get("monthly_contribution") or 0.0)
    actual = {r["label"]: r["pct"] or 0.0 for r in (alloc_rows or [])}
    tip = _suggest(value, plan)
    tip_monthly = float(tip["monthly"] or 0.0)
    base_stocks = float(round(actual.get("Stocks") or load_alloc_targets().get("Stocks")
                              or tip["stocks_pct"]))
    # pre-filled with the plan's own numbers, else the suggested starting points
    st.session_state.setdefault("wi_monthly", float(base_monthly or tip_monthly or 200))
    st.session_state.setdefault("wi_years", tip["years"])
    st.session_state.setdefault("wi_stocks", int(5 * round(base_stocks / 5)))
    st.session_state.setdefault("wi_extra", 0.0)
    top = float(max(2000.0, 3 * base_monthly, 1.5 * tip_monthly,
                    st.session_state["wi_monthly"]))

    with st.container():
        st.caption("Try different amounts, years and mixes to see how the range moves. "
                   "Nothing is saved unless you choose to.")
        c1, c2 = st.columns(2)
        c1.slider("How much you'll invest each month ($)", 0.0, 50 * -(-top // 50), step=1.0,
                  key="wi_monthly", format="$%d")
        c2.slider("How many years you'll keep investing", 1, 40, key="wi_years")
        c3, c4 = st.columns(2)
        c3.slider("% in stocks (the rest in bonds)", 0, 100, step=5, key="wi_stocks")
        c4.number_input("Add a one-off amount now ($)", min_value=0.0, step=500.0,
                        key="wi_extra", format="%.0f")
        _suggestion_line(" · ".join(
            ([f"{fmt_money0(tip_monthly)} a month"] if tip_monthly else [])
            + [_years_text(tip["years"]), f"{tip['stocks_pct']}% in stocks"]),
            key="wi_use_tip",
            values={**({"wi_monthly": tip_monthly} if tip_monthly else {}),
                    "wi_years": tip["years"], "wi_stocks": tip["stocks_pct"]})
        monthly, months = st.session_state["wi_monthly"], 12 * st.session_state["wi_years"]
        stocks, start = st.session_state["wi_stocks"], present + st.session_state["wi_extra"]
        ret, base_ret = plans.mix_return(stocks), plans.mix_return(base_stocks)
        lo, mid, hi = (plans.future_value(start, monthly, r, months)
                       for r in (ret - plans.SPREAD_PCT, ret, ret + plans.SPREAD_PCT))
        base_then = plans.future_value(present, base_monthly, base_ret, months)
        when = _fmt_month(plans.add_months(today, months).isoformat())
        lines = [f"About **{fmt_money0(mid)}** by {when} "
                 f"({fmt_money0(lo)} to {fmt_money0(hi)}), assuming {ret:.1f}% a year.",
                 ("That's **" + fmt_money0(abs(mid - base_then)) + (" more" if mid >= base_then
                  else " less") + "** than your current plan would have by then.")
                 if abs(mid - base_then) >= 1 else "The same as your current plan."]
        if target:
            reach = plans.months_to_reach(start, monthly, ret, target)
            base_reach = plans.months_to_reach(present, base_monthly, base_ret, target)
            if reach is None:
                lines.append(f"At this pace {fmt_money0(target)} isn't reached within 50 years.")
            else:
                lines.append(f"You'd reach your goal of {fmt_money0(target)} around "
                             f"**{_fmt_month(plans.add_months(today, reach).isoformat())}**"
                             + ("" if base_reach is None or base_reach == reach else
                                f" - {_months_text(base_reach - reach)} "
                                + ("sooner" if reach < base_reach else "later")
                                + " than with your current plan") + ".")
        _md("  \n".join(lines))
        df = pd.DataFrame(plans.projection_series(start, monthly, months, today=today,
                                                  return_pct=ret))
        df["date"] = pd.to_datetime(df["date"])
        palette = SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT
        st.altair_chart(charts.projection(df, target=target, color=palette[0], mask=_hidden(),
                                          tooltip=[alt.Tooltip("date:T", title="Date",
                                                               format="%b %Y")]),
                        width="stretch")
        st.caption(f"Assumes about {plans.STOCK_RETURN_PCT:g}% a year for stocks and "
                   f"{plans.BOND_RETURN_PCT:g}% for bonds, a range of ±{plans.SPREAD_PCT:g}%, "
                   "no fees, taxes or inflation. Real returns go up and down - this "
                   "illustrates, it doesn't predict.")
        learn_more("risk")
        msg = st.session_state.pop("wi_msg", None)
        if msg:
            st.success(msg)
        if CAN_MANAGE and has_goal and abs(monthly - base_monthly) >= 1:
            # the goal card above changes too: redraw the whole page
            if st.button(f"Use {fmt_money0(monthly)} a month in my plan", key="wi_use"):
                _wi_use_monthly(monthly)
                st.rerun()


# ---- what the portfolio could pay each year (the retirement view) ---------- #
RETIRE_TAB = "Retirement income"
RETIRE_EXAMPLE = 100_000.0   # the example amount without real dollars to go on


def _retire_income_today():
    """What the holdings pay in a year (income.yearly_income): the Income
    page's next-12-months dividend estimate plus cash interest from an
    imported activity history. Only with holdings (the Income page's helpers
    in views/income.py read them). Doesn't fetch anything."""
    import income
    today = datetime.now().date()
    qty, annual = _income_held(), {}
    for r in _income_yield_rows():
        annual[r["symbol"]] = annual.get(r["symbol"], 0.0) + r["est_income"]
    c = connect(DB)
    try:
        got = income.received(c, USER_ID, today)
        synced = income.has_history(c, qty)
        paid = income.payments(c, qty, today)
    finally:
        c.close()
    sched = income.schedule([{"symbol": s, "quantity": q, "annual": annual.get(s)}
                             for s, q in sorted(qty.items())], paid, synced, today)
    return income.yearly_income(sched["total"], got), got is not None


@st.fragment
def _render_retirement_income(value, today):
    """What the portfolio could pay each year: the dividends and interest it
    pays now, steady withdrawals at a few rule-of-thumb rates, and how long
    a chosen yearly amount could last at one stated growth rate. Education,
    never advice: no amount here is a recommendation."""
    pretend = SNAPSHOT_SOURCE == manual_entry.PCT_SOURCE
    real = value is not None and value > 0 and not pretend
    st.markdown("#### What your portfolio could pay you each year")
    st.caption("Two ways to look at it: what your holdings pay on their own, and what taking a "
               "steady share out each year could look like. Illustrations to learn from, not "
               "advice and not a promise.")

    # ---- what it pays today ---------------------------------------------- #
    if value:
        paid, has_history = _retire_income_today()
        yld = paid["total"] / value * 100 if value else None
        st.markdown("**What it pays today**")
        if paid["total"]:
            stats = [("Dividends, next 12 months", fmt_money0(paid["dividends"]), "estimated")]
            if has_history:
                stats.append(("Interest on cash", fmt_money0(paid["interest"]),
                              "last 12 months"))
            stats.append(("About a year", fmt_money0(paid["total"]),
                          f"{mask_or(f'{yld:.1f}%')} of today's value"))
            if pretend:   # pretend dollars: the share of the portfolio is what's real
                stats = [("Pays about", mask_or(f"{yld:.1f}%"), "of its value a year")]
            _summary_stats(stats)
            st.caption("Paid to you without selling anything. The dividends repeat each "
                       "holding's last year of payments at today's share count - companies and "
                       "funds change their dividends, and some years they cut them."
                       + (" Interest is what your brokerage paid on cash in the last 12 months."
                          if has_history and not pretend else ""))
        else:
            st.caption("Your holdings don't show any dividends or interest yet. Payment dates "
                       "fill in with each evening's price history.")
        learn_more("dividends")

    # ---- steady withdrawals at a few rates -------------------------------- #
    st.markdown("**If you took a steady share out each year**")
    base = value if real else RETIRE_EXAMPLE
    # an example amount isn't anyone's own, so it isn't hidden
    money = fmt_money0 if real else (lambda v: f"${v:,.0f}")
    if not real:
        lead = ("Your dollar amounts are pretend, so here" if pretend else
                "Once your holdings are in, this uses their value. For now, here")
        st.caption((f"{lead} is what each rate means for every {money(RETIRE_EXAMPLE)} "
                    "invested.").replace("$", r"\$"))
    _summary_stats([(f"{w['rate']:g}% a year", money(w["yearly"]),
                     f"about {money(w['monthly'])} a month")
                    for w in plans.withdrawals(base)])
    _md("These are **rules of thumb, not a promise**. 4% is the best-known one: studies of past "
        "US markets found that taking about 4% of the starting value in the first year, then "
        "raising the amount with inflation, lasted 30 years in most periods. A lower rate leaves "
        "more room for bad years; a higher one leaves less.  \n"
        "Markets move: in a year the portfolio falls, the same amount is a bigger share of what's "
        "left. The dividends and interest above are part of these amounts, not extra.  \n"
        "Not included: taxes, fees, inflation, and income from elsewhere - Social Security or a "
        "pension, for example.")

    # ---- how long a yearly amount could last ------------------------------- #
    if real:
        st.markdown("**How long the money could last**")
        if _hidden():
            st.caption("Show amounts (the eye beside the page title) to try a yearly amount.")
        else:
            st.session_state.setdefault("retire_yearly",
                                        float(max(100, round(value * 0.04 / 100) * 100)))
            yearly = st.number_input("A yearly amount to try ($)", min_value=0.0, step=1000.0,
                                     format="%.0f", key="retire_yearly")
            if yearly > 0:
                g = plans.LASTS_GROWTH_PCT
                months = plans.months_lasting(value, yearly, g)
                flat = plans.months_lasting(value, yearly, 0.0)
                share = yearly / value * 100
                if months is None:
                    lasts = (f"would still be paying after {plans.LASTS_CAP_YEARS} years if the "
                             f"rest grew a steady {g:g}% a year")
                else:
                    lasts = (f"would last about **{_months_text(months)}** - to around "
                             f"{_fmt_month(plans.add_months(today, months).isoformat())} - if "
                             f"the rest grew a steady {g:g}% a year")
                _md(f"Taking {fmt_money0(yearly)} a year ({share:.1f}% of today's "
                    f"{fmt_money0(value)}), a twelfth each month, the money {lasts}."
                    + ("" if flat is None else
                       f" With no growth at all, about {_months_text(flat)}."))
            st.caption(f"Hypothetical: one steady growth rate, {plans.LASTS_GROWTH_PCT:g}% a "
                       "year, and the same amount every year - before inflation, fees and "
                       "taxes. Real returns go up and down, and a fall early on matters more "
                       "than one later. Not a prediction.")
    learn_more("risk")


def _render_plan(value, growth, alloc_rows):
    """The Plan page. `value` / `growth` / `alloc_rows` are None for an
    account with no holdings yet - the goal and contributions still work."""
    today = datetime.now().date()
    plan = load_plan()
    if _on_the_route():
        # the plan is changed here, but planning is part of the route on
        # Learn: a clear way back to it, so nobody is left here wondering
        st.button(":material/arrow_back: Back to your route", key="plan_back_route",
                  on_click=_go, args=("Get started",),
                  help=f"Back to where you were on {_label('Get started')}.")
    if not CAN_MANAGE and not plans.has_goal(plan):
        st.info(f"Your advisor, {_advisor_display_name()}, sets your goal - it shows up here "
                "once they have.")
    elif CAN_MANAGE and (st.session_state.get("plan_editing") or not plans.has_goal(plan)):
        _render_plan_form(plan, today, value)
    else:
        _render_plan_status(plan, value, today)
    editing = CAN_MANAGE and (st.session_state.get("plan_editing") or not plans.has_goal(plan))
    # Everything else one tab at a time (ROADMAP S3); each tab redraws on its
    # own when something in it changes, not the whole page.
    sections = []
    if plans.has_goal(plan) and not editing:
        sections.append(("How it's going", lambda: _render_projection(plan, value, today)))
    if not editing:
        sections.append(("What if", lambda: _render_what_if(plan, value, alloc_rows, today)))
    sections.append(("Contributions", lambda: _render_contributions(plan, today)))
    if value is not None:
        sections.append(("Money in vs growth", lambda: _render_money_in(value, growth)))
    if alloc_rows:
        sections.append(("Target mix", lambda: _render_target_mix(alloc_rows)))
    # what it could pay each year: for everyone, first for someone retired or
    # nearly (plans.retirement_first: the goal, the timeline, the profile)
    retire = (RETIRE_TAB, lambda: _render_retirement_income(value, today))
    if plans.retirement_first(plan, _profile(), today):
        sections.insert(0, retire)
    else:
        sections.append(retire)
    if ON_CLIENT:   # the client's advisor: proposals (views/proposals.py)
        sections.append(("Proposals", lambda: _render_proposals_advisor(alloc_rows, value)))
    for tab, (_name, draw) in zip(st.tabs([s[0] for s in sections]), sections):
        with tab:
            draw()
