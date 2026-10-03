# Part of dashboard.py, which runs this file with _view("fund_overlap") at the
# point where this code used to sit, in dashboard.py's own namespace: the names
# here (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# Fund overlap (fund_holdings.py): do my funds hold the same companies? A small
# card on Home (beside Fee check) with one line, and a window with the funds
# side by side and the companies owned most once funds are looked through.
# Based on each fund's top 10 holdings from Yahoo - fetched when the window
# opens (never while Home draws), kept a week. It describes overlap only,
# never says to sell. Uses positions / contexts / sec_info / fund_tops /
# portfolio_value, which exist by the time Home is drawn.
# ruff: noqa: F821

import fees

OVERLAP_ASK = ("What does it mean when two of my funds hold the same companies, and how do "
               "people usually think about overlap between funds? Use examples, not "
               "recommendations.")


def _overlap_holdings():
    """[{symbol, name, kind, value}] for fund_holdings.look_through(): each
    position at today's value, with its kind (fees.holding_type)."""
    out = []
    for p, ctx in zip(positions, contexts):
        info = sec_info.get(p["symbol"]) or {}
        out.append({"symbol": p["symbol"], "name": info.get("name") or p.get("description"),
                    "kind": fees.holding_type(info.get("quote_type"), p.get("asset_type")),
                    "value": M.eff_mv(ctx) or 0.0})
    return out


def _overlap_worth_it(holdings):
    """Two funds, or a fund and a single stock - something that can overlap."""
    kinds = {}
    for h in holdings:
        kinds.setdefault(h["kind"], set()).add(h["symbol"])
    n_funds, n_stocks = len(kinds.get("fund", ())), len(kinds.get("stock", ()))
    return n_funds >= 2 or (n_funds >= 1 and n_stocks >= 1)


def _overlap_pct(pct):
    """A share of the portfolio, hidden with the amounts."""
    if pct is None:
        return "—"
    return mask_or(f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%")


def _overlap_names(items):
    items = list(items)
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _overlap_company(c):
    name = c["name"] or c["symbol"] or "?"
    sym = c["symbol"]
    return f"{name} ({sym})" if sym and sym.upper() != name.upper() else name


def _overlap_ask():
    st.session_state["coach_prompt"] = OVERLAP_ASK
    st.session_state["page"] = "AI Assistant"


@st.dialog("Fund overlap", width="large", on_dismiss=_dialog_closed)
def _overlap_window():
    holdings = _overlap_holdings()
    funds = fund_holdings.funds_in(positions, sec_info)
    try:
        with st.spinner("Looking up what your funds hold..."):
            c = connect(DB)
            try:
                tops, failed = fund_holdings.ensure(c, funds)
            finally:
                c.close()
    except Exception:   # never an error for this: say it isn't available
        tops, failed = {}, list(funds)
    look = fund_holdings.look_through(holdings, tops, portfolio_value or None)
    pairs = fund_holdings.overlaps(tops, funds)
    st.caption("Funds hold many companies. When two of your funds hold the same ones, or a "
               "fund holds a company you also own yourself, you own more of it than any one "
               f"line shows. This is based on each fund's top {fund_holdings.TOP_N} holdings - "
               "most funds hold many more, so the full overlap is usually larger.")

    sharing = [p for p in pairs if p["n_shared"]]
    if pairs:
        st.markdown("**Your funds side by side**")
        for p in sharing:
            st.markdown(f"- {fund_holdings.describe(p)}")
        if not sharing:
            st.markdown("- None of your funds share any of their largest holdings.")
        elif len(sharing) < len(pairs):
            st.markdown("- Your other funds don't share any of their largest holdings.")
    if sharing:
        st.dataframe(pd.DataFrame([{
            "Funds": f"{p['a']} and {p['b']}",
            "Shared top holdings": f"{p['n_shared']} of {p['of']}",
            "Weight in common": f"{p['weight'] * 100:.0f}%",
            "Shared, largest first": (", ".join(p["shared"][:4])
                                      + ("..." if p["n_shared"] > 4 else "")),
        } for p in sharing]), width="stretch", hide_index=True)
        st.caption("Weight in common: for each shared holding, the smaller of its two weights, "
                   "added up - how much of each fund sits in the same companies, counting the "
                   "top holdings only.")

    seen = [c for c in look["companies"] if c["via"]]
    if seen:
        top = look["companies"][:10]
        st.markdown("**What you own most of, with funds looked through**")
        first = top[0]
        _md(f"{_overlap_company(first)}: about **{_overlap_pct(first['pct'])}** of your "
            f"portfolio ({fmt_money0(first['value'])}), through "
            f"{_overlap_names(fund_holdings.through(first))}.")
        st.dataframe(pd.DataFrame([{
            "Company": _overlap_company(c),
            "Share of your portfolio": _overlap_pct(c["pct"]),
            "Value": fmt_money0(c["value"]),
            "Through": _overlap_names(fund_holdings.through(c)),
        } for c in top]), width="stretch", hide_index=True)
        st.caption("Each fund's share of a company is the fund's value at today's price times "
                   "that company's weight in the fund. Holdings beyond each fund's top "
                   f"{fund_holdings.TOP_N} aren't counted, so these are the least you own.")
        if _hidden():
            st.caption(f"Amounts are hidden ({MASK}). Show amounts at the top of the page to "
                       "see them.")

    not_listed = [f for f in funds if f not in failed and not (tops.get(f) or {}).get("holdings")]
    if failed:
        st.markdown(f":material/cloud_off: We couldn't get the top holdings for "
                    f"{_overlap_names(failed)} just now, so "
                    f"{'it isn' if len(failed) == 1 else 'they aren'}'t counted. Try again in "
                    "a few minutes.")
    if not_listed:
        st.markdown(f":material/help: Top holdings aren't available for "
                    f"{_overlap_names(not_listed)} (often the case for bond funds and funds "
                    "made of other funds), so "
                    f"{'it isn' if len(not_listed) == 1 else 'they aren'}'t counted.")
    if not (pairs or seen) and not failed and not not_listed:
        st.markdown(":material/info: Nothing to compare yet.")
    st.caption("This describes what your funds hold - it isn't a suggestion to buy or sell "
               "anything. Some overlap is normal: a total-market fund holds the same large "
               "companies as an S&P 500 fund, for example.")
    dates = sorted(str(t["fetched_at"])[:10] for t in tops.values() if t.get("fetched_at"))
    if dates:
        st.caption(f"Top holdings from Yahoo Finance, as of {_fmt_date(dates[0])}; "
                   "checked again after a week.")
    learn_more("diversification")
    # (in a window: switch pages with a full rerun, which also closes it)
    if st.button(f":material/forum: Ask {GUIDE} about overlap", key="overlap_ask",
                 type="tertiary"):
        _overlap_ask()
        _dialog_closed()
        st.rerun()


def open_overlap_window():
    st.session_state["dialog_open"] = True   # live prices wait (_dialog_closed)
    _overlap_window()


def _overlap_line(holdings):
    """The card's one line, from the kept top holdings only (no fetching):
    the most-shared pair of funds, else the company owned most once funds are
    looked through, else an invitation. None when there's nothing to show."""
    funds = fund_holdings.funds_in(positions, sec_info)
    known = {f: fund_tops[f] for f in funds if f in fund_tops}
    if not any(t["holdings"] for t in known.values()):
        if all(not fund_holdings.is_stale(known.get(f)) for f in funds):
            return None   # asked this week, and Yahoo lists none for any of them
        return "Do your funds hold the same companies? See how much they overlap."
    pairs = fund_holdings.overlaps(known, funds)
    best = next((p for p in pairs if p["level"] in ("most", "some")), None)
    if best:
        return html.escape(fund_holdings.describe(best))
    look = fund_holdings.look_through(holdings, known, portfolio_value or None)
    first = next((c for c in look["companies"] if c["via"]), None)
    if first:
        return (f"Your largest company once funds are looked through: "
                f"{html.escape(first['name'] or first['symbol'] or '')}, about "
                f"<span style='font-weight:600'>{_overlap_pct(first['pct'])}</span> of your "
                "portfolio.")
    return "Your funds' largest holdings don't overlap much."


def render_overlap_card():
    """Fund overlap on Home: one line, the window on a tap. Nothing without
    two funds (or a fund and a stock) to compare."""
    holdings = _overlap_holdings()
    if not _overlap_worth_it(holdings):
        return
    line = _overlap_line(holdings)
    if line is None:
        return
    with st.container(border=True, horizontal=True, vertical_alignment="center",
                      key="pt_overlap"):
        st.html(f"<div class='pt-route-label'>Fund overlap</div><div class='pt-region'>{line}"
                "</div>", width="stretch")
        if st.button("See overlap", key="overlap_open", type="tertiary"):
            open_overlap_window()
