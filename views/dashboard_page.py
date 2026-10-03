# Part of dashboard.py, which runs this file with _view("dashboard_page") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# The Dashboard page: value, goal, alerts, performance chart, allocation, holdings.
# ruff: noqa: F821

ROUTE_ASK = {   # what "Ask Northwend" starts with, per next step
    "storm": "The market has dropped and my portfolio is down. What have drops like this "
             "looked like before, and how do long-term investors usually think about them?",
    "drift": "My mix has drifted from my target. What does rebalancing mean, and how do people "
             "usually decide when and how to do it?",
    "gap": "I'm behind on my goal. What are the usual ways to close a gap like mine - adding "
           "more, waiting longer, or changing the target?",
    "monthly": "How do people decide how much to invest each month toward a goal like mine?",
    "steady": "I'm on track for my goal. What should I keep an eye on from here?",
    "reached": "I've reached my goal. What do people usually think about when they hit a goal "
               "and set the next one?",
    **CLIENT_ASK,   # an advisor's client's next steps (views/start_home.py)
}


def _ask_route(key):
    st.session_state["coach_prompt"] = ROUTE_ASK.get(
        key, "Looking at my plan and portfolio, what's a sensible next step for me to learn about?")
    st.session_state["page"] = "AI Assistant"


def _route_words(step, gp, monthly, plan):
    """(title, explanation, button label, what the button does) for route.next_step()."""
    k = step["key"]
    when = _fmt_month(plan["target_date"]) if plans.has_goal(plan) else ""
    if k == "goal":
        return ("Set your goal", "Pick what you're investing for and roughly when. Everything "
                "else on your route follows from it.", "Set a goal", ("learn", "goal"))
    if k == "goal_wait":
        return ("Your advisor sets your goal with you", "It shows up here once they have. Their "
                "notes to you are under Advisor notes.", "Advisor notes", ("page", "Advisor notes"))
    if k == "profile":
        return ("Tell us a little about you", "A few quick questions - your timeline and how you "
                "feel about ups and downs - so your route fits you.", "Answer the questions",
                ("page", "Get started"))
    if k == "holdings":
        return ("Bring in your holdings", "Paste them from any brokerage, type them in, or use "
                "percentages only.", "Add holdings", ("dialog", "manual"))
    if k == "monthly":
        need = step.get("needed")
        return ("Choose how much to add each month", "Regular amounts do most of the work on the "
                "way to a goal." + (f" About {fmt_money0(need)} a month would reach yours by "
                                    f"{when} at the plan's assumed return." if need else ""),
                "Open your plan", ("page", "Plan"))
    if k == "gap":
        return ("Close the gap to your goal",
                f"You're adding {fmt_money0(monthly)} a month. About {fmt_money0(step['needed'])} "
                f"({fmt_money0(step['extra'])} more) would get you there by {when} at the plan's "
                "assumed return - or you could move the date or the target.",
                "Open your plan", ("page", "Plan"))
    if k == "drift":
        actual = mask_or(f"{step['actual']:.0f}%")
        return ("Your mix has drifted from its target",
                f"{step['label']} is {actual} of your portfolio against a target of "
                f"{step['target']:.0f}%. Bringing it back is called rebalancing.",
                "See your target mix", ("page", "Plan"))
    if k == "update":
        return ("Update your holdings", f"You last brought them in {step['days']} days ago - "
                "an update keeps your route and plan accurate.", "Update holdings",
                ("dialog", "manual"))
    if k == "learn":   # the stage and step are in the line above ("You're in ...")
        return (step["title"], f"Next on your route in {_label('Get started')}.",
                "Continue", ("learn", step["step"]))
    if k == "reached":
        return ("You've reached your goal", "Well done. Set your next goal whenever you're "
                "ready.", "Set the next goal", ("page", "Plan"))
    return ("You're on track", f"Keep adding {fmt_money0(monthly)} a month and your plan gets "
            f"you there by {when}.", "Open your plan", ("page", "Plan"))


def _route_go(action):
    kind, target = action
    if kind == "dialog":
        _open_holdings_dialog(target)
    elif kind == "learn":   # that waypoint open on Learn (Set a goal is walked there)
        st.session_state["gs_at"] = target
        st.session_state.pop("gs_goal_part", None)
        _go("Get started")
    else:
        _go(target)


def _render_route():
    """The investor home's first card (ROADMAP G2): the goal, the waypoints,
    and the one next step (route.py decides which)."""
    state = _route_state(True)
    plan = state["plan"]
    has_goal = plans.has_goal(plan)
    gp = _goal_progress(plan, portfolio_value) if has_goal else None
    monthly = float((plan or {}).get("monthly_contribution") or 0.0)
    pct = {r["label"]: r["pct"] for r in
           allocate(positions, cash_by_account, CLASS_SPLITS)["by_asset_class"]}
    try:
        days = (datetime.now().date() - date.fromisoformat(str(snapshot)[:10])).days
    except ValueError:
        days = None
    waypoints = state["waypoints"]   # their route (Learn only while it's theirs)
    # an advisor's client: what their advisor left them first (client mode)
    client_step = _client_step(state, bool(positions))
    step = client_step or route.next_step(
        has_goal=has_goal, can_manage=CAN_MANAGE, profile_missing=bool(state["missing"]),
        has_holdings=bool(positions), monthly=monthly, goal=gp,
        drift=route.drifted(pct, load_alloc_targets(), load_drift_threshold()),
        days_since_holdings=days, waypoints=waypoints)
    reached = bool(gp and gp["status"] == "reached")
    title, text, button, action = (_client_step_words(step) if client_step else
                                   _route_words(step, gp, monthly, plan))

    with st.container(border=True, key="pt_route_reached" if reached else "pt_route"):
        # the investor type comes with an example mix - not beside an advisor's
        kind = None if CLIENT_MODE else learn.investor_type(
            state["profile"], learn.starter_mix(state["profile"], state["horizon"]),
            state["items"])
        head = ("<div class='pt-route-label'>"
                + ("Your next step, with your advisor" if CLIENT_MODE else "Your route")
                + (f" · {html.escape(kind['name'])}" if kind else "") + "</div>")
        if has_goal:
            label, tone = PLAN_STATUS[gp["status"]]
            share = min(100.0, max(0.0, gp["pct_of_target"] or 0.0))
            head += (f"<div class='pt-goal-top'><b>{html.escape(plan.get('goal_name') or plan['goal_type'] or 'Goal')}</b>"
                     f"<span class='pt-goal-pct'>{mask_or(f'{share:.0f}%')}</span>"
                     f"<span class='pt-chip {tone}'>{label}</span></div>"
                     f"<div class='pt-goal-track' role='progressbar' aria-label='Progress to your goal' "
                     f"aria-valuenow='{share:.0f}' aria-valuemin='0' aria-valuemax='100'>"
                     f"<div class='pt-goal-fill' style='width:{share:.1f}%'></div></div>"
                     f"<div class='pt-goal-sub'>{fmt_money0(gp['current'])} of "
                     f"{fmt_money0(gp['target'])} by {_fmt_month(plan['target_date'])}</div>")
        if not CLIENT_MODE:   # an advisor's client walks with their advisor, not a trail
            n_done = sum(1 for _, _, d in waypoints if d)
            # the route as a trail through the expedition's regions (route.py)
            head += route.trail_html(route.dots(waypoints, reached),
                                     f"{n_done} of {len(waypoints)} waypoints reached, then "
                                     "your goal")
            head += _where_html(state)   # "You're in Start investing · step 2 of 4 · ..."
        st.html(head)
        with st.container(horizontal=True, vertical_alignment="center"):
            # "$" escaped: a pair of them would be read as a math formula
            st.markdown((f":material/flag: **Next: {title}**" + (f"  \n{text}" if text else ""))
                        .replace("$", r"\$"), width="stretch")
            st.button(button, key="route_go", type="primary", on_click=_route_go,
                      args=(action,))
            st.button(f"Ask {GUIDE}", key="route_ask", type="tertiary", on_click=_ask_route,
                      args=(step["key"],))


if PAGE == "Dashboard":
    render_weather()                          # a storm note while well below the high (T4)
    if INVESTOR_VIEW:
        _render_route()
        render_kit_card(portfolio_value)      # milestones and gear (views/kit.py)
        render_fee_card()                     # fee check (views/fees.py)
        render_overlap_card()                 # fund overlap (views/fund_overlap.py)
        check_milestones(portfolio_value)

    # ---- hero: value, today's move, since last visit, headline stats ----- #
    _day_base = portfolio_value - day_change_total
    _day_pct = (day_change_total / _day_base * 100) if _day_base else None
    if hide_amounts:
        _day_html = f"{MASK} today"
    elif n_live:
        _arrow = (f"<span aria-hidden='true'>{'▲' if day_change_total >= 0 else '▼'}</span>"
                  f"<span class='pt-sr'>{'Up' if day_change_total >= 0 else 'Down'}</span>")
        _day_html = _tone(day_change_total, f"{_arrow} {fmt_money(abs(day_change_total))}"
                          + (f" ({_day_pct:+.2f}%)" if _day_pct is not None else "") + " today")
    else:
        _day_html = ""

    _since_html = ""
    _last_open = st.session_state["last_open_snapshot"]
    # Only when it's the same statement - a new import's jump is new holdings
    # data, not the market moving.
    if (_last_open and _last_open.get("portfolio_value")
            and _last_open.get("snapshot_date") == snapshot):
        _prev_val = _last_open["portfolio_value"]
        _since_delta = portfolio_value - _prev_val
        _since_pct = (_since_delta / _prev_val * 100) if _prev_val else None
        _last_ts = pd.to_datetime(_last_open["logged_at"], utc=True).to_pydatetime()
        _secs = (datetime.now(timezone.utc) - _last_ts).total_seconds()
        if _secs < 3600:
            _ago = f"{max(1, int(_secs // 60))} min ago"
        elif _secs < 86400:
            _ago = f"{int(_secs // 3600)}h ago"
        else:
            _ago = f"{int(_secs // 86400)}d ago"
        _since_html = (f"Since your last visit ({_ago}): " + _signed_money(_since_delta)
                       + ("" if hide_amounts or _since_pct is None else f" ({_since_pct:+.2f}%)"))

    def _stat(label, value, sub=""):
        return (f"<div class='pt-stat' role='listitem'><div class='pt-stat-label'>{label}</div>"
                f"<div class='pt-stat-value'>{value}</div>"
                + (f"<div class='pt-stat-sub'>{sub}</div>" if sub else "") + "</div>")

    st.html(_stat_row(
        "<div class='pt-hero'>"
        "<div class='pt-hero-label'>Portfolio value</div>"
        f"<div class='pt-hero-value'>{fmt_money(portfolio_value)}</div>"
        + (f"<div class='pt-hero-delta'>{_day_html}</div>" if _day_html else "")
        + (f"<div class='pt-hero-sub'>{_since_html}</div>" if _since_html else "")
        + "</div><div class='pt-stats' role='list' aria-label='Portfolio summary'>"
        # with dividends known: the price change, then the total return beside it
        + _stat("Price change" if tot_return else "Total gain/loss",
                _tone(tot_gl, _signed_money(tot_gl)),
                _tone(tot_glp, fmt_pct(tot_glp)) if tot_glp is not None else "")
        + (_stat("Total return, with dividends",
                 _tone(tot_return["usd"], _signed_money(tot_return["usd"])),
                 _tone(tot_return["usd"], fmt_pct(tot_return["pct"]))
                 if tot_return["pct"] is not None else "") if tot_return else "")
        + _stat("Holdings", fmt_money(tot_mv), f"{len(positions)} positions")
        + _stat("Cash", fmt_money(cash),
                "" if hide_amounts or not portfolio_value
                else f"{cash / portfolio_value * 100:.1f}% of total")
        + "</div>"
    ))
    if tot_return:
        # "$" escaped: two amounts would be read as a math formula
        st.caption((f"Total return adds the {fmt_money(tot_return['dividends'])} in dividends "
                    "your holdings paid to their price change. "
                    + income.source_words(DIVIDENDS[p["symbol"]]["source"]
                                          for p, c in zip(positions, contexts)
                                          if c["dividends"] and p["cost_basis"] is not None))
                   .replace("$", r"\$"))

    # ---- goal: one line from the plan, or a nudge to set one ----------- #
    # (the advisor's own portfolio; the investor home has it in Your route)
    _plan = load_plan()
    _goal_box = (st.container(border=True, horizontal=True, vertical_alignment="center")
                 if not INVESTOR_VIEW else None)
    if _goal_box is not None:
        with _goal_box:
            if plans.has_goal(_plan):
                _gp = _goal_progress(_plan, portfolio_value)
                _glabel, _gtone = PLAN_STATUS[_gp["status"]]
                _gpct = mask_or(f"{_gp['pct_of_target'] or 0:.0f}%")
                st.html(f"<span class='pt-chip {_gtone}'>{_glabel}</span>&nbsp; "
                        f"<b>{html.escape(_plan.get('goal_name') or _plan['goal_type'] or 'Goal')}</b>"
                        f" · {_gpct} of "
                        f"{fmt_money0(_gp['target'])} by {_fmt_month(_plan['target_date'])}",
                        width="stretch")
                st.button("Open plan", key="dash_open_plan", type="tertiary", on_click=_go,
                          args=("Plan",))
            elif CAN_MANAGE:
                st.markdown("Set a goal to see whether you're on track.", width="stretch")
                st.button("Set a goal", key="dash_set_goal", type="tertiary", on_click=_go,
                          args=("Plan",))
            else:
                st.markdown("Your advisor hasn't set a goal for you yet.", width="stretch")

    if ON_CLIENT or IS_MANAGED_CLIENT:
        _advisor_notes_card()

    # ---- alerts: one line, open for the list and the limits ------------ #
    _rules = load_rules()
    _fired = alerts.evaluate(contexts, _rules)
    _alert_label = (f":red[:material/notifications_active:] **{len(_fired)} "
                    f"alert{'s' if len(_fired) != 1 else ''}** · positions past your limits"
                    if _fired else ":material/notifications: No alerts")
    with st.expander(_alert_label):
        for _a in _fired:
            st.markdown(_a.masked_message if hide_amounts else _a.message)
        if not _fired:
            st.caption("No position is past its day-move or gain/loss limit.")
        if CAN_MANAGE:
            st.markdown("**Limits**")
            _new = []
            for _col, _r in zip(st.columns(len(alerts.DEFAULT_RULES)), alerts.DEFAULT_RULES):
                cur = next((x["abs_gt"] for x in _rules if x["key"] == _r["key"]), _r["abs_gt"])
                val = _col.number_input(f"{_r['label']} — flag beyond ±%", min_value=0.0,
                                        max_value=1000.0, value=float(cur), step=0.5,
                                        key=f"rule_{_r['key']}")
                _new.append({**_r, "abs_gt": val})
            if _new != _rules:
                save_rules(_new)
                st.rerun()
            st.caption("Checked against the latest prices every time the page loads.")
        else:
            st.caption("Limits set by your advisor: " + " · ".join(
                f"{r['label']} beyond ±{r['abs_gt']:g}%" for r in _rules) + ".")

    st.divider()

    # ---- performance over time ---------------------------------------- #
    p1, p2 = st.columns([0.65, 0.35])
    p1.subheader("Performance")
    series_col = p2.selectbox(
        "Series", [c for c, _, _ in perf.SERIES],
        index=[c for c, _, _ in perf.SERIES].index(load_perf_series()),
        format_func=lambda c: perf.SERIES_LABEL[c], key="perf_series_sel",
        label_visibility="collapsed",
    )
    if series_col != load_perf_series():
        save_perf_series(series_col)

    prng = st.segmented_control("Range", charts.RANGE_LABELS, default="1D",
                                key="perf_range", label_visibility="collapsed") or "1D"

    # history() picks the reconstruction's resolution to match `prng`, exactly like
    # ticker_series() does for a single ticker (finest Yahoo interval that covers
    # the window). With only daily bars so far (right after the automatic
    # backfill, before the nightly intraday sync) a short range can come back
    # empty - then show the shortest wider range that has data, and say so.
    _shown_rng = prng
    _hist_conn = connect(DB)   # one connection for every range tried
    try:
        _hist = perf.history(_hist_conn, USER_ID, days=charts.RANGE_DAYS[prng],
                             include_app_open=False, basis=PERF_BASIS)
        for _wider in charts.RANGE_LABELS[charts.RANGE_LABELS.index(prng) + 1:]:
            if len(_hist) >= 2:
                break
            _shown_rng = _wider
            _hist = perf.history(_hist_conn, USER_ID, days=charts.RANGE_DAYS[_wider],
                                 include_app_open=False, basis=PERF_BASIS)
    finally:
        _hist_conn.close()
    if _shown_rng != prng and len(_hist) >= 2:
        st.caption(f"No {prng} data yet - showing {_shown_rng}. Intraday history loads each evening.")
    if len(_hist) < 2:
        st.caption("Your performance chart fills in once price history for your holdings has "
                   "loaded - it updates on its own every trading day.")
    else:
        _fmtname = perf.SERIES_FMT[series_col]
        y_title = perf.SERIES_LABEL[series_col]
        hist_df = pd.DataFrame(_hist)
        hist_df["t"] = pd.to_datetime(hist_df["t"], utc=True, format="mixed")
        pwin = hist_df.dropna(subset=[series_col]).sort_values("t")

        if len(pwin) < 2:
            st.caption(f"No **{y_title}** recorded in this window yet.")
        else:
            # Intraday-resolution data (minutes/hours apart) gets the gaps-compressed
            # axis; daily-resolution data (~1 day apart, weekends aside) doesn't need it.
            _pcompress = pwin["t"].diff().median() < pd.Timedelta(hours=20)

            _pf, _pl, ppct = charts.window_change(pwin, "t", series_col)
            pmcol, _ = st.columns([0.4, 0.6])
            pmcol.metric(y_title, mask_or(FORMATTERS[_fmtname](_pl)),
                         delta=(None if hide_amounts or ppct is None else f"{ppct:+.2f}% over {_shown_rng}"))

            _ptips = [alt.Tooltip("t:T", title="When", format="%b %d, %Y  %H:%M")]
            if not hide_amounts:
                _ptips.append(alt.Tooltip(f"{series_col}:Q", title=y_title, format=TOOLTIP_FORMAT[_fmtname]))
            st.altair_chart(
                charts.line(
                    pwin, x="t", y=series_col, y_title=y_title, y_format=AXIS_FORMAT[_fmtname],
                    mask=hide_amounts, compress_gaps=bool(_pcompress),
                    line_color=(SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT)[0],
                    tooltip=_ptips),
                width="stretch",
            )
            st.caption(
                f"{len(pwin)} points · reconstructed from current holdings × each bar's close. "
                + (f"Sync {len(_missing)} more ticker(s) to extend the line: {', '.join(_missing)}."
                   if _missing else "")
            )

    st.divider()

    # ---- allocation ----------------------------------------------------- #
    alloc = allocate(positions, cash_by_account, CLASS_SPLITS)
    # What the bars group by: asset class (what's held - targets and drift use
    # this) or the broker's own asset type. One color per group across the page.
    _by_type = st.session_state.get("alloc_group") == "Broker type"
    _group = "by_asset_type" if _by_type else "by_asset_class"
    _asset_slots = _slot_map({r["label"] for r in alloc[_group]},
                             ASSET_SLOT if _by_type else CLASS_SLOT)

    al1, al2 = st.columns([0.75, 0.25])
    al1.subheader("Allocation")
    if CAN_MANAGE:
        with al2.popover("Targets", width="stretch"):
            st.caption("Set a target % of portfolio for stocks, bonds, cash or other - leave "
                       "at 0 for no target.")
            _saved_targets = load_alloc_targets()
            _new_targets = {}
            for _lbl in asset_classes.CLASSES:
                _new_targets[_lbl] = st.number_input(
                    _lbl, min_value=0.0, max_value=100.0, step=1.0,
                    value=float(_saved_targets.get(_lbl, 0.0)), key=f"target_{_lbl}")
            _new_thresh = st.number_input(
                "Flag drift beyond ± this many percentage points", min_value=0.5, max_value=50.0,
                step=0.5, value=load_drift_threshold(), key="drift_threshold_input")
            if {k: v for k, v in _new_targets.items() if v} != _saved_targets:
                save_alloc_targets(_new_targets)
            if _new_thresh != load_drift_threshold():
                save_drift_threshold(_new_thresh)
    st.segmented_control("Group by", ["Asset class", "Broker type"], default="Asset class",
                         key="alloc_group", label_visibility="collapsed",
                         help="Asset class is what holdings hold - a bond ETF counts as bonds. "
                              "Broker type is how the statement labels them.")

    _title = "By broker type" if _by_type else "By asset class"
    if len(alloc["by_account"]) > 1:
        a1, a2 = st.columns(2, gap="large")
        a1.html(_alloc_bar(alloc[_group], _title, _asset_slots))
        a2.html(_account_mix(alloc["by_account"], positions, cash_by_account, _asset_slots,
                             _group))
    else:
        st.html(_alloc_bar(alloc[_group], _title, _asset_slots))
    _render_classification(positions)

    if alloc["concentration"]:
        lines = "  \n".join(
            f"- **{r['symbol']}** ({r['account']}) — {fmt_money(r['value'])}, "
            + (MASK if hide_amounts else f"**{r['pct']:.1f}%**") + " of portfolio"
            for r in alloc["concentration"]
        )
        st.warning(f"Positions over {CONCENTRATION_PCT:.0f}% of portfolio value:  \n{lines}")
    else:
        st.caption(f"No single position exceeds {CONCENTRATION_PCT:.0f}% of portfolio value.")
    learn_more("diversification")

    _targets = load_alloc_targets()
    if _targets:
        _thresh = load_drift_threshold()
        _pct_by_label = {r["label"]: r["pct"] for r in alloc["by_asset_class"]}
        _drift = []
        for _lbl, _target in _targets.items():
            _actual = _pct_by_label.get(_lbl, 0.0) or 0.0
            _delta = _actual - _target
            if abs(_delta) > _thresh:
                _drift.append((_lbl, _actual, _target, _delta))
        _drift.sort(key=lambda r: abs(r[3]), reverse=True)
        if _drift:
            lines = "  \n".join(
                f"- {'▲' if d > 0 else '▼'} **{lbl}** — "
                + (MASK if hide_amounts else f"{actual:.1f}% vs {target:.1f}% target ({d:+.1f} pts)")
                for lbl, actual, target, d in _drift
            )
            st.warning(f"Drifted beyond ±{_thresh:g} pts from target:  \n{lines}")
        else:
            st.caption(f"Every targeted asset class is within ±{_thresh:g} pts of target.")
        learn_more("rebalancing")

    st.divider()

    # ---- accounts: side-by-side comparison -------------------------------- #
    ac1, ac2, ac3 = st.columns([0.5, 0.25, 0.25])
    ac1.subheader("Accounts")
    # the broker's own names, recovered from the display names in use
    _to_broker = {v: k for k, v in ACCOUNT_LABELS.items()}
    _broker_accts = sorted({p["broker_account"] for p in positions}
                           | {_to_broker.get(a, a) for a in cash_by_account})
    if CAN_MANAGE:
        with ac2.popover("Rename", width="stretch"):
            with st.form("rename_accounts", border=False):
                st.caption("Give an account a name you'll recognize. Leave blank to use the "
                           "broker's name.")
                _typed = {a: st.text_input(a, value=ACCOUNT_LABELS.get(a, ""), placeholder=a,
                                           max_chars=accounts.MAX_LEN, key=f"acct_name_{a}")
                          for a in _broker_accts}
                if st.form_submit_button("Save names", type="primary"):
                    _proposed = {a: n.strip() for a, n in _typed.items() if n.strip()}
                    _bad = next((a for a in _broker_accts
                                 if accounts.clash(a, _proposed.get(a, a), _broker_accts, _proposed)), None)
                    if _bad:
                        st.error(f"Two accounts can't share the name "
                                 f"“{accounts.display(_bad, _proposed)}”.")
                    else:
                        _c = connect(DB)
                        try:
                            for a in _broker_accts:
                                if _proposed.get(a) != ACCOUNT_LABELS.get(a):
                                    accounts.set_label(_c, USER_ID, a, _proposed.get(a))
                        finally:
                            _c.close()
                        st.rerun()

    _acct_stats = {}
    for _p, _ctx in zip(positions, contexts):
        _a = _p["account"]
        _s = _acct_stats.setdefault(_a, {"mv": 0.0, "cost": 0.0, "gain": 0.0, "day_change": 0.0, "n": 0})
        _mv = M.eff_mv(_ctx)
        if _mv is not None:
            _s["mv"] += _mv
            _s["n"] += 1
            _cost = _p["cost_basis"]
            if _cost is not None:
                _s["cost"] += _cost
                _s["gain"] += _mv - _cost
            _dchg = M.value("day_change_usd", _ctx)
            if _dchg is not None:
                _s["day_change"] += _dchg

    _all_accounts = sorted(set(list(_acct_stats) + list(cash_by_account)))
    _acct_rows = []
    for _a in _all_accounts:
        _s = _acct_stats.get(_a, {"mv": 0.0, "cost": 0.0, "gain": 0.0, "day_change": 0.0, "n": 0})
        _csh = cash_by_account.get(_a, 0.0) or 0.0
        _total = _s["mv"] + _csh
        _acct_rows.append({
            "account": _a, "total": _total, "holdings": _s["mv"], "cash": _csh,
            "gain_usd": _s["gain"], "gain_pct": (_s["gain"] / _s["cost"] * 100) if _s["cost"] else None,
            "day_change": _s["day_change"], "n_positions": _s["n"],
            "pct_of_portfolio": (_total / portfolio_value * 100) if portfolio_value else None,
        })
    _acct_rows.sort(key=lambda r: r["total"], reverse=True)

    if CAN_IMPORT and len(_broker_accts) > 1:
        # Updating one account never removes another (an import replaces only
        # the accounts in it), so taking one out is done here, on purpose.
        with ac3.popover("Remove", width="stretch"):
            _worth = {_to_broker.get(r["account"], r["account"]): r["total"] for r in _acct_rows}
            st.caption("Take an account out of your holdings - one you closed or moved. Your "
                       "other accounts stay as they are, and its past stays in your history.")
            _rm = st.selectbox("Account to remove", _broker_accts, key="acct_rm_pick",
                               format_func=lambda a: accounts.display(a, ACCOUNT_LABELS))
            _ok = st.checkbox(f"Yes, remove {accounts.display(_rm, ACCOUNT_LABELS)} "
                              f"({fmt_money(_worth.get(_rm, 0.0))})".replace("$", r"\$"),
                              key=f"acct_rm_ok_{_rm}")   # asked again for another account
            st.button("Remove this account", key="acct_rm_btn", type="primary",
                      disabled=not _ok, on_click=_remove_account, args=(_rm,))

    if len(_acct_rows) < 2:
        st.caption("Only one account in this portfolio — nothing to compare yet.")
    else:
        _adf_raw = pd.DataFrame(_acct_rows)
        _adf = pd.DataFrame([{
            "Account": r["account"], "Total Value": fmt_money(r["total"]),
            "% of Portfolio": fmt_pct_level(r["pct_of_portfolio"]), "Holdings": fmt_money(r["holdings"]),
            "Cash": fmt_money(r["cash"]), "Gain/Loss": fmt_money(r["gain_usd"]),
            "Gain/Loss %": fmt_pct(r["gain_pct"]), "Today": fmt_money(r["day_change"]),
            "Positions": r["n_positions"],
        } for r in _acct_rows])
        _gain_raw = [r["gain_usd"] for r in _acct_rows]
        _today_raw = [r["day_change"] for r in _acct_rows]
        _acct_styler = (
            _adf.style
            .apply(lambda col: [color_sign(v) for v in _gain_raw], subset=["Gain/Loss"])
            .apply(lambda col: [color_sign(v) for v in _today_raw], subset=["Today"])
        )
        st.dataframe(_acct_styler, width="stretch", hide_index=True)
        st.download_button(
            "Download CSV", _adf_raw.to_csv(index=False).encode("utf-8"),
            file_name="accounts.csv", mime="text/csv", key="accounts_dl",
            disabled=hide_amounts, help=(
                "Disabled while amounts are hidden — turn off Hide amounts to export real figures."
                if hide_amounts else None),
        )
    learn_more("account_types")


    st.divider()

    # ---- holdings (configurable columns) -------------------------------- #
    if "col_keys" not in st.session_state:
        st.session_state["col_keys"] = load_columns()

    h1, h2 = st.columns([0.75, 0.25])
    h1.subheader("Holdings")
    with h2.popover("Columns", width="stretch"):
        labels = st.multiselect(
            "Columns — add or remove as many as you want",
            [m.label for m in M.AVAILABLE],
            default=[M.BY_KEY[k].label for k in st.session_state["col_keys"] if k in M.BY_KEY],
            key="col_labels",
        )
        new_keys = [M.BY_LABEL[lbl].key for lbl in labels]
        if new_keys and new_keys != st.session_state["col_keys"]:
            st.session_state["col_keys"] = new_keys
            save_columns(new_keys)
        # any Yahoo history at all (what's already read shows it without asking)
        if not (_covered or bar_stats or perf.has_bars(DB)):
            st.caption("The **Yahoo history** columns (MA, Volume, 52-wk, Beta, P/E, Sector) "
                       "stay blank until you tap sync history (:material/history:) up top.")

    # A tappable strip of ticker symbols — the Robinhood-style "click the name"
    # entry point into the detail view below. Deliberately separate from the
    # data grid's own row/cell interactions (Streamlit's dataframe treats a plain
    # cell click as spreadsheet-style cell focus, not row selection — only its
    # checkbox actually selects a row, which isn't the one-tap feel we want here).
    # The search box keeps this usable as the holdings list grows past a couple
    # dozen tickers, where a flat pill strip alone starts taking real scrolling.
    # Only one of the Holdings / Watchlist pill strips can be "the" open ticker
    # at a time — picking one clears the other via on_change (see _pick_holdings
    # / _pick_watchlist below), so there's a single unambiguous selection.
    st.caption("Tap a ticker for its chart and full details:")
    _desc_by_sym = {p["symbol"]: (p.get("description") or "") for p in positions}
    _symbols_held = sorted(_desc_by_sym)
    _search = st.text_input("Search tickers", key="ticker_search",
                            placeholder="Filter by symbol or name…", label_visibility="collapsed")
    if _search.strip():
        _q = _search.strip().upper()
        _pill_options = [s for s in _symbols_held if _q in s.upper() or _q in _desc_by_sym[s].upper()]
    else:
        _pill_options = _symbols_held
    # Never let a search term hide the ticker you already have open.
    _cur_pill = st.session_state.get("holdings_pill")
    if _cur_pill and _cur_pill not in _pill_options:
        _pill_options = sorted(_pill_options + [_cur_pill])


    if not _pill_options:
        st.caption("No ticker matches your search.")
    else:
        st.pills("Tickers", _pill_options, key="holdings_pill", label_visibility="collapsed",
                on_change=_pick_holdings)

    chosen = [M.BY_KEY[k] for k in st.session_state["col_keys"] if k in M.BY_KEY] \
        or [M.BY_KEY[k] for k in M.DEFAULT_KEYS]
    # the total-return columns only when some holding has dividends to add:
    # otherwise the price change alone, without empty columns beside it
    chosen = [m for m in chosen if m.key not in M.SHOWN_WHEN_KNOWN
              or any(M.value(m.key, ctx) is not None for ctx in contexts)]

    records = [{m.label: M.value(m.key, ctx) for m in chosen} for ctx in contexts]

    df = pd.DataFrame(records, columns=[m.label for m in chosen])
    fmt_map = {m.label: FORMATTERS[m.fmt] for m in chosen if m.fmt in FORMATTERS}
    color_cols = [m.label for m in chosen if m.color_sign]
    # The table draws an empty cell as a grey "None", whatever the format says:
    # a column with blanks (a holding entered without its cost) is shown as its
    # formatted text instead (still right-aligned, like numbers), "—" for the blanks.
    shown, as_text = df.copy(), {}
    for col in df.columns:
        if df[col].isna().any():
            fmt = fmt_map.pop(col, None)
            shown[col] = [fmt(v) if fmt else ("—" if _blank(v) else str(v)) for v in df[col]]
            if fmt:
                as_text[col] = st.column_config.TextColumn(alignment="right")
    styler = shown.style.format(fmt_map, na_rep="—")
    if color_cols:   # colored by the numbers, not the text shown
        styler = styler.apply(lambda s: [color_sign(v) for v in df[s.name]], subset=color_cols)
    st.dataframe(styler, width="stretch", hide_index=True, column_config=as_text or None)
    st.download_button(
        "Download CSV", df.to_csv(index=False).encode("utf-8"),
        file_name="holdings.csv", mime="text/csv", key="holdings_dl",
        disabled=hide_amounts, help=(
            "Disabled while amounts are hidden — turn off Hide amounts to export real figures."
            if hide_amounts else None),
    )
    st.caption("Green = gain, red = loss. Price / Market Value / Gain-Loss use the live price where "
               "available, otherwise the CSV's figures. Edit the column set with **Columns**."
               + (" Unrealized G/L is the price change; **Total return** adds the dividends "
                  "each holding paid (blank where none are known)."
                  if any(m.key in M.SHOWN_WHEN_KNOWN for m in chosen) else ""))

    st.divider()
