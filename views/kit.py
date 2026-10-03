# Part of dashboard.py, which runs this file with _view("kit") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# Milestones and gear (ROADMAP T3, gear.py): the "milestone reached" window
# when a piece is first earned, and Your kit on Home. Gear is earned by
# learning and steady habits only; nothing is lost or for sale.
# ruff: noqa: F821

import gear
import storms


def _gear_facts(value):
    """What gear.earned() needs, from what the app already knows. Read once
    per run (Home's kit card and the milestone check both ask - _RUN)."""
    if ("gear_facts", value) not in _RUN:
        _RUN[("gear_facts", value)] = _read_gear_facts(value)
    return dict(_RUN[("gear_facts", value)])


def _read_gear_facts(value):
    state = _route_state(HAS_HOLDINGS)
    plan = state["plan"]
    reached = False
    # made-up money never reaches a real goal (the example portfolio)
    if plans.has_goal(plan) and value is not None and SNAPSHOT_SOURCE != SAMPLE_SOURCE:
        reached = _goal_progress(plan, value)["status"] == "reached"
    c = connect(DB)
    try:
        added = [m["date"] for m in plans.money_moves(c, USER_ID)
                 if m["counted"] and m["amount"] > 0]
        values = [(r["logged_at"], r["portfolio_value"]) for r in c.execute(
            "SELECT logged_at, portfolio_value FROM value_log WHERE user_id = ? AND "
            "portfolio_value IS NOT NULL", (USER_ID,))]
        sells = [r["trade_date"] for r in c.execute(
            "SELECT trade_date FROM transactions WHERE user_id = ? AND action = 'SELL'",
            (USER_ID,))]
    finally:
        c.close()
    return {"profile_done": state["done"]["profile"], "goal_set": state["done"]["goal"],
            "basics_done": state["done"]["basics"], "practice_done": state["done"]["practice"],
            "statement_in": HAS_HOLDINGS and SNAPSHOT_SOURCE != SAMPLE_SOURCE,
            "steady": gear.steady_months(added, datetime.now().date()),
            "storm": gear.weathered_storm(values, sells), "goal_reached": reached}


def _kit_shown():
    """Gear and milestones are an investor's own: not the advisor app, and not
    while an advisor is looking at a client."""
    return not IS_ADVISOR and USER_ID == LOGIN_ID


def _kit_keys():
    """The pieces in this account's kit (gear.kit_keys): an advisor's client's
    has no rope, as Learn has no practice money for them (CLIENT_MODE)."""
    return gear.kit_keys(CLIENT_MODE)


def _milestone_done():
    """Continue, the X or Escape: the window is finished with."""
    st.session_state.pop("milestone_queue", None)
    st.session_state["dialog_open"] = False


def _gear_go(target):
    """A gear's button (Your kit, its window, the milestone window): close
    any window and go where that piece is earned (gear.GO)."""
    _milestone_done()
    kind, where = target
    if kind == "dialog":
        _open_holdings_dialog(where)
    elif kind == "learn":
        # that waypoint open on Learn (if Learn still has it)
        if where in dict(globals().get("GET_STARTED_STEPS") or ()):
            st.session_state["gs_at"] = where
        _go("Get started")
    else:
        _go(where)


def _gear_can_go(k):
    """A button to where the piece is earned - not for a managed client's
    goal or money added, which their advisor keeps (Plan is read-only)."""
    return k in gear.GO and (CAN_MANAGE or k not in ("compass", "lantern"))


def _gear_name(k):
    return gear.BY_KEY[k][1]


def _gear_hover(k):
    """'Storm cloak - for holding steady...' (a tile's hover text on Home)."""
    return f"{_gear_name(k)} - {gear.FOR[k][0].lower()}{gear.FOR[k][1:]}"


def _next_html(k, lead="Next to earn"):
    """'Next to earn: the compass - set a goal...' in one line."""
    step = gear.BY_KEY[k][3]
    return (f"<div class='pt-region'>{lead}: <b>{html.escape(_gear_name(k).lower())}</b> - "
            f"{html.escape(step[0].lower() + step[1:])}</div>")


@st.dialog("Milestone reached", width="small", on_dismiss=_milestone_done)
def _milestone_window(keys, have):
    for k in keys:
        _key, name, title, _step, region, _paths = gear.BY_KEY[k]
        what = gear.FOR[k][0].lower() + gear.FOR[k][1:]
        st.html("<div class='pt-milestone'>"
                f"<div class='pt-milestone-badge'>{gear.icon_html(k, True, 44)}</div>"
                + (f"<div class='pt-eyebrow' style='margin:0'>{html.escape(region)}</div>"
                   if region else "")
                + f"<div class='pt-milestone-title'>{html.escape(title)}</div>"
                f"<div>You've earned the <b>{html.escape(name.lower())}</b> for your kit - "
                f"{html.escape(what)}</div>"
                f"<div class='pt-gear-why'>{html.escape(gear.WHY[k])}</div>"
                "</div>")
    nxt = gear.next_up(have, _kit_keys())
    st.html(_next_html(nxt, "Next") if nxt else
            "<div class='pt-region'>That's every piece in your kit.</div>")
    with st.container(horizontal=True):
        if st.button("Continue", key="milestone_ok", type="primary"):
            _milestone_done()
            st.rerun()
        # a click inside a window redraws just the window: st.rerun() closes it
        if nxt and _gear_can_go(nxt) and st.button(gear.GO[nxt][0], key="milestone_go"):
            _gear_go(gear.GO[nxt][1])
            st.rerun()


def check_milestones(value):
    """Show the window once for gear earned since last time (an account from
    before gear existed takes what it has quietly - gear.new_since), and keep
    when each piece was earned for the kit window (gear.stamp)."""
    if not _kit_shown():
        return
    have = gear.earned(_gear_facts(value), _kit_keys())
    p = _read_prefs()
    fresh, seen = gear.new_since(have, p.get("gear_seen"))
    dates = gear.stamp(p.get("gear_dates"), have, fresh, datetime.now().date().isoformat())
    if seen != p.get("gear_seen") or dates != (p.get("gear_dates") or {}):
        p["gear_seen"], p["gear_dates"] = seen, dates
        _write_prefs(p)
    # kept until it's closed: a redraw that didn't open the window again
    # would close it (live prices wait while it's open - dialog_open)
    queue = st.session_state.get("milestone_queue", []) + fresh
    if queue:
        st.session_state["milestone_queue"] = queue
        st.session_state["dialog_open"] = True
        _milestone_window(queue, have)


@st.dialog("Your kit", width="large", on_dismiss=_dialog_closed)
def _kit_window(have):
    st.caption(f"{len(have)} of {len(_kit_keys())} earned. Each piece of gear marks something "
               "you've learned or a steady habit - never trading more or taking more risk. "
               "Nothing is ever lost, and there's no hurry.")
    dates = _read_prefs().get("gear_dates") or {}
    for k in _kit_keys():
        got = k in have
        chip = (f"<span class='pt-gear-chip pt-gear-chip-earned'>"
                f"{html.escape(gear.when_text(dates.get(k), _fmt_date))}</span>" if got else
                "<span class='pt-gear-chip'>Not yet</span>")
        with st.container(border=True, horizontal=True, vertical_alignment="center",
                          key=f"pt_gear_{k}"):
            st.html("<div class='pt-gear-item'>"
                    f"<span class='pt-gear-tile{' pt-gear-earned' if got else ''}'>"
                    f"{gear.icon_html(k, got, 28)}</span><div>"
                    f"<div class='pt-gear-head'><b>{html.escape(_gear_name(k))}</b>{chip}</div>"
                    f"<div>{html.escape(gear.FOR[k])}</div>"
                    f"<div class='pt-gear-how'>{html.escape(gear.HOW[k])}</div>"
                    # no button (the cloak just comes): what to do, in words
                    + ("" if got or _gear_can_go(k) else
                       f"<div class='pt-gear-how'>{html.escape(gear.BY_KEY[k][3])}</div>")
                    + "</div></div>", width="stretch")
            # (a click in a window redraws just the window: st.rerun() closes it)
            if not got and _gear_can_go(k) and st.button(gear.GO[k][0], key=f"kit_go_{k}"):
                _gear_go(gear.GO[k][1])
                st.rerun()


def render_kit_card(value):
    """Your kit on Home: each piece with its name (what it's for on hover),
    the next one to earn, and the window that explains every piece."""
    if not _kit_shown():
        return
    have = gear.earned(_gear_facts(value), _kit_keys())
    nxt = gear.next_up(have, _kit_keys())
    with st.container(border=True, key="pt_kit"):
        cells = "".join(
            f"<li class='pt-gear-cell' title='{html.escape(_gear_hover(k), quote=True)}'>"
            f"<span class='pt-gear-tile{' pt-gear-earned' if k in have else ''}'>"
            f"{gear.icon_html(k, k in have)}</span>"
            f"<span class='pt-gear-label' aria-hidden='true'>{html.escape(_gear_name(k))}</span></li>"
            for k in _kit_keys())
        st.html("<div class='pt-route-label'>Your kit · "
                f"{len(have)} of {len(_kit_keys())} earned</div>"
                "<div class='pt-region' style='margin:0'>Gear for learning and steady habits. "
                "Filled ones are earned.</div>"
                f"<ul class='pt-gear-row' aria-label='Your kit'>{cells}</ul>"
                + (_next_html(nxt) if nxt else
                   "<div class='pt-region'>Every piece earned.</div>"))
        with st.container(horizontal=True, key="pt_kit_buttons"):
            if st.button("What each piece is for", key="kit_open",
                         icon=":material/open_in_new:"):
                st.session_state["dialog_open"] = True   # live prices wait (_dialog_closed)
                _kit_window(have)
            if nxt and _gear_can_go(nxt):
                st.button(gear.GO[nxt][0], key="kit_go", type="tertiary", on_click=_gear_go,
                          args=(gear.GO[nxt][1],))


# ---- storms (ROADMAP T4, storms.py): a sharp drop as weather to wait out --- #

def _weather_now():
    """storms.weather() for the holdings, worked out once a day per statement
    (the closes only change with the nightly sync)."""
    today = datetime.now().date()
    key = (USER_ID, snapshot, today.isoformat())
    kept = st.session_state.get("weather")
    if not kept or kept[0] != key:
        since = (today - timedelta(days=storms.WINDOW_DAYS)).isoformat()
        kept = (key, storms.weather(perf.daily_values(DB, PERF_BASIS, since), today))
        st.session_state["weather"] = kept
    return kept[1]


def _hide_weather(w):
    p = _read_prefs()
    p["storm_hidden"] = {"level": w["level"], "high_date": w["high_date"]}
    _write_prefs(p)


@st.dialog("What storms have looked like", width="medium", on_dismiss=_dialog_closed)
def _storms_window():
    st.caption("The S&P 500 - the 500 largest US companies - from its high to its low, "
               "and how long until it passed that high again. Rounded; its price "
               "without dividends.")
    rows = "".join(f"<tr><td>{html.escape(name)}</td><td>{fall}%</td><td>{html.escape(back)}</td></tr>"
                   for name, fall, back in storms.PAST_STORMS)
    st.html("<table class='pt-storm-table'><thead><tr><th>Storm</th><th>Fell</th>"
            f"<th>Back to the old high</th></tr></thead><tbody>{rows}</tbody></table>")
    st.markdown("Every one of these passed, though some took years - and nobody knew "
                "at the time how long it would last. That's why people investing for "
                "goals years away usually plan for storms instead of trying to dodge "
                "them: selling after a fall turns a drop on paper into a real loss, and "
                "some of the market's best days have come soon after its worst. Past "
                "storms don't promise what the next one will do.")
    learn_more("market_drops")


def render_weather():
    """A calm note on Home while the holdings are well below their recent
    high - nothing to do, never buy or sell."""
    if not HAS_HOLDINGS:
        return
    w = _weather_now()
    own = USER_ID == LOGIN_ID
    if not w or (own and storms.hidden(w, _read_prefs().get("storm_hidden"))):
        return
    storm = w["level"] == "storm"
    title = "A storm on the trail" if storm else "Rough weather"
    lead = (f"Your holdings are about {w['drop_pct']:.0f}% below their high on "
            f"{_fmt_date(w['high_date'])} (as of the close on {_fmt_date(w['as_of'])}).")
    body = ("Drops of 10% or more have come along about once every year or two on "
            "average, and the market has climbed past every one so far - sometimes "
            "in months, sometimes in years." if storm else
            "Dips like this happen a few times in a typical year, and most pass "
            "without much notice.")
    nothing = ("Nothing needs doing today. If your goal is years away, the plan you "
               "set still holds; if you'll need this money soon, that's worth a look "
               "at your plan.")
    cloak = (storm and _kit_shown()
             and "cloak" not in (_read_prefs().get("gear_seen") or []))
    with st.container(border=True, key="pt_storm"):
        st.html("<div class='pt-eyebrow' style='margin:0'>Weather on the trail</div>"
                f"<div class='pt-storm-title'>{title}</div>"
                f"<div>{html.escape(lead)} {html.escape(body)}</div>"
                f"<div class='pt-region' style='margin-top:.4rem'>{html.escape(nothing)}"
                + (" Holding steady through a storm earns the <b>storm cloak</b> for "
                   "your kit." if cloak else "") + "</div>")
        with st.container(horizontal=True):
            if st.button("What storms have looked like", key="storm_more", type="tertiary"):
                st.session_state["dialog_open"] = True   # live prices wait (_dialog_closed)
                _storms_window()
            st.button(f"Ask {GUIDE}", key="storm_ask", type="tertiary",
                      on_click=_ask_route, args=("storm",))
            if own:
                st.button("Hide for now", key="storm_hide", type="tertiary",
                          on_click=_hide_weather, args=(w,))
