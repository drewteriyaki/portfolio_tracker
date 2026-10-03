# Part of dashboard.py, which runs this file with _view("income") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# The Income page: dividends received (from an imported activity export),
# the next 12 months by month, and yields. Calm by default (ROADMAP S6): a
# short summary and one next step, with each part in a window; the full page
# for advisors and for Show everything (_show_everything).
# ruff: noqa: F821

# yield on cost, said once in plain words (the Income by holding table)
YOC_LINE = ("**Yield on cost** is this year's estimated dividends as a share of what you paid "
            "for the shares, rather than what they're worth today. When a company raises its "
            "dividend after you buy, it goes up. Shown only where your cost is known.")
YOC_HELP = ("This year's estimated dividends as a share of what you paid, for the holdings "
            "whose cost is known.")

INCOME_ASK = ("How do dividends and interest work, when do they get paid, and what does "
              "reinvesting them mean? Use examples, not recommendations.")


def _income_month_label(m):
    return datetime.strptime(m + "-01", "%Y-%m-%d").strftime("%b %Y")


def _income_held():
    """{symbol: shares held now}, across accounts."""
    qty = {}
    for p in positions:
        qty[p["symbol"]] = qty.get(p["symbol"], 0.0) + (p.get("quantity") or 0.0)
    return qty


def _income_reads():
    """What the Income page reads, on one connection: (_income_received(),
    the held symbols with price history, their past year of payments)."""
    import income
    today = datetime.now().date()
    qty = _income_held()
    c = connect(DB)
    try:
        return (income.received(c, USER_ID, today), income.has_history(c, qty),
                income.payments(c, qty, today))
    finally:
        c.close()


def _income_schedule(income_rows, synced, paid):
    """The next 12 months of estimated dividend income (income.py): each
    holding's past year of payments repeated, times the shares held now.
    `synced` / `paid` from _income_reads. Returns (schedule, unsynced symbols)."""
    import income
    today = datetime.now().date()
    qty, annual = _income_held(), {}
    for r in income_rows:
        annual[r["symbol"]] = annual.get(r["symbol"], 0.0) + r["est_income"]
    # payment dates come with the price history; fetch it once for holdings
    # synced before dividends were kept
    unsynced = sorted(set(qty) - synced)
    if unsynced and not st.session_state.get("income_synced"):
        st.session_state["income_synced"] = True
        _sync_history(unsynced, quick=True)
    plan = income.schedule([{"symbol": s, "quantity": q, "annual": annual.get(s)}
                            for s, q in sorted(qty.items())], paid, synced, today)
    return plan, unsynced


def _render_income_by_month(plan, unsynced, in_window=False):
    if not plan["total"]:
        if unsynced:
            st.caption("Payment dates fill in with tonight's price history.")
        return

    if not in_window:
        st.subheader("Next 12 months", anchor=False)
    months = plan["months"]
    peak = max(months, key=lambda r: r["total"])
    mc1, mc2 = st.columns(2)
    mc1.metric("Estimated income, next 12 months", fmt_money(plan["total"]))
    mc2.metric("Biggest month", _income_month_label(peak["month"]), fmt_money(peak["total"]),
               delta_color="off", delta_arrow="off")
    st.altair_chart(_income_bars(months, _income_month_label), width="stretch")
    notes = ["By ex-dividend month - the money usually arrives a few weeks later. Each holding "
             "repeats its last year of payments at today's share count; dividends can change."]
    if plan["spread"]:
        notes.append("No payment dates yet for " + ", ".join(plan["spread"])
                     + " - its yearly estimate is spread evenly across the months.")
    st.caption(" ".join(notes))
    if not in_window:
        st.divider()


def _income_bars(months, label):
    """Monthly bars: [{"month", "total", "by_symbol"}]."""
    chart_df = pd.DataFrame([{"Month": label(r["month"]), "order": i,
                              "Income": 0.0 if _hidden() else r["total"],
                              "Paid by": ", ".join(s for s, v in sorted(
                                  r["by_symbol"].items(), key=lambda kv: -kv[1]) if v)}
                             for i, r in enumerate(months)])
    color = (SERIES_DARK if st.context.theme.type == "dark" else SERIES_LIGHT)[0]
    return alt.Chart(chart_df).mark_bar(color=color, cornerRadiusTopLeft=3,
                                        cornerRadiusTopRight=3).encode(
        x=alt.X("Month:N", sort=alt.SortField("order"), title=None,
                axis=alt.Axis(labelAngle=0, labelExpr="slice(datum.label, 0, 3)")),
        y=alt.Y("Income:Q", title=None, axis=charts.y_axis(charts.MONEY_AXIS, labels=not _hidden())),
        tooltip=[alt.Tooltip("Month:N"), alt.Tooltip("Income:Q", format="$,.2f"),
                 alt.Tooltip("Paid by:N")])


def _render_income_received(got, in_window=False):
    if got is None:
        return
    if not in_window:
        st.subheader("Received, last 12 months", anchor=False)
    rc1, rc2, rc3 = st.columns(3)
    rc1.metric("Dividends received", fmt_money(got["dividends"]))
    rc2.metric("Interest received", fmt_money(got["interest"]))
    rc3.metric("Total", fmt_money(got["total"]))
    if got["total"]:
        st.altair_chart(_income_bars(got["months"], _income_month_label), width="stretch")
    st.caption("From your brokerage's activity history that you imported - what was actually "
               "paid, including dividends that were reinvested."
               + (f" It starts on {_fmt_date(got['since'])}, so earlier months show nothing."
                  if got["since"] else ""))
    if not in_window:
        st.divider()


def _income_yield_rows():
    """Each holding with a dividend yield from the brokerage's file, its
    estimated yearly income (market value x yield), and its yield on cost
    (that income against what was paid - None when the cost isn't known, and
    for a percentages-only portfolio, whose "cost" is only today's value)."""
    import income
    real_cost = SNAPSHOT_SOURCE != manual_entry.PCT_SOURCE
    rows = []
    for p, ctx in zip(positions, contexts):
        yld = M.value("div_yield_pct", ctx)
        mv = M.eff_mv(ctx)
        if yld is None or mv is None:
            continue
        cost = p.get("cost_basis") if real_cost else None
        est = mv * yld / 100
        yoc = income.yield_on_cost(est, cost)
        rows.append({
            "symbol": p["symbol"], "description": p.get("description"), "market_value": mv,
            "yield_pct": yld, "est_income": est,
            "cost": cost if yoc is not None else None, "yoc_pct": yoc,
            "last_pay_date": p.get("div_pay_date"),
            "reinvest": {1: "Yes", 0: "No"}.get(p.get("reinvest")), "account": p["account"],
        })
    rows.sort(key=lambda r: r["est_income"], reverse=True)
    return rows


def _render_income_table(income_rows):
    """The yields: totals, then one row per income-producing holding, with a CSV."""
    if not income_rows:
        st.caption("No dividend-yield figures from your brokerage's file - the table of yields "
                   "appears here when your broker's export includes a **Dividend Yield** column.")
        return
    import income
    total_income = sum(r["est_income"] for r in income_rows)
    yield_on_holdings = (total_income / tot_mv * 100) if tot_mv else None
    yoc_total = income.yield_on_cost_total(income_rows)

    cols = st.columns(4 if yoc_total is not None else 3)
    cols[0].metric("Est. annual dividend income", fmt_money(total_income))
    cols[1].metric("Yield on holdings", fmt_pct_level(yield_on_holdings))
    if yoc_total is not None:
        cols[2].metric("Your yield on cost", fmt_pct_level(yoc_total), help=YOC_HELP)
    cols[-1].metric("Income-producing positions", f"{len(income_rows)} / {len(positions)}")

    idf_raw = pd.DataFrame([{
        "Symbol": r["symbol"], "Description": r["description"],
        "Market Value": r["market_value"], "Div Yield %": r["yield_pct"],
        "Yield on Cost %": r["yoc_pct"], "Est. Annual Income": r["est_income"],
        "Last Pay Date": r["last_pay_date"],
        "Reinvest": r["reinvest"], "Account": r["account"],
    } for r in income_rows])
    idf = pd.DataFrame([{
        "Symbol": r["symbol"], "Description": r["description"],
        "Market Value": fmt_money(r["market_value"]), "Div Yield %": fmt_pct_level(r["yield_pct"]),
        # blank when the cost isn't known
        "Yield on Cost %": fmt_pct_level(r["yoc_pct"]) if r["yoc_pct"] is not None else "",
        "Est. Annual Income": fmt_money(r["est_income"]),
        "Last Pay Date": r["last_pay_date"] or "—", "Reinvest": r["reinvest"] or "—",
        "Account": r["account"],
    } for r in income_rows])
    st.dataframe(idf, width="stretch", hide_index=True)
    st.download_button(
        "Download CSV", idf_raw.to_csv(index=False).encode("utf-8"),
        file_name="income.csv", mime="text/csv", key="income_dl", on_click="ignore",
        disabled=hide_amounts, help=(
            "Disabled while amounts are hidden — turn off Hide amounts to export real figures."
            if hide_amounts else None),
    )
    st.caption("Est. Annual Income = market value × dividend yield, both as reported in the CSV "
               "— a simple estimate, not a payment schedule. **Last Pay Date** is the most "
               "recently known payment from your brokerage's file, not a prediction of the "
               "next one.")
    if yoc_total is not None:
        st.caption(YOC_LINE)


# ---- the calm view's windows ------------------------------------------------ #
@st.dialog("Next 12 months", width="large", on_dismiss=_dialog_closed)
def _income_months_window(plan, unsynced):
    _render_income_by_month(plan, unsynced, in_window=True)


@st.dialog("Received, last 12 months", width="large", on_dismiss=_dialog_closed)
def _income_received_window(got):
    _render_income_received(got, in_window=True)


@st.dialog("Income by holding", width="large", on_dismiss=_dialog_closed)
def _income_table_window(income_rows):
    _render_income_table(income_rows)


def _render_income_calm(income_rows, plan, unsynced, got):
    """A few numbers, one next step, and tiles that open each part in a window."""
    yearly = sum(r["est_income"] for r in income_rows)
    expected = plan["total"] or yearly or None
    upcoming = next((r for r in plan["months"] if r["total"]), None) if plan["total"] else None
    if not (expected or got):
        st.info(":material/payments: No income to show yet. When your holdings pay dividends "
                "or interest, what's expected and what was paid show up here.")
        if unsynced:
            st.caption("Payment dates fill in with tonight's price history.")
    else:
        stats = [("Expected, next 12 months", fmt_money(expected), None)]
        if upcoming:
            stats.append(("Next payment", _income_month_label(upcoming["month"]),
                          fmt_money(upcoming["total"])))
        elif income_rows:
            stats.append(("Yield on holdings", fmt_pct_level(yearly / tot_mv * 100 if tot_mv else None),
                          None))
        if got is not None:
            stats.append(("Received, last 12 months", fmt_money(got["total"]), None))
        else:
            payers = len({r["symbol"] for r in income_rows}
                         | {s for m in plan["months"] for s, v in m["by_symbol"].items() if v})
            stats.append(("Holdings that pay", f"{payers} of {len(positions)}", None))
        _summary_stats(stats)

    if got is None and CAN_IMPORT:
        _next_step_card("income", "See what was actually paid: upload your brokerage's "
                        "<b>activity</b> export (any brokerage), and the dividends and interest "
                        "you received show here.",
                        (":material/upload_file: Upload a CSV", _open_holdings_dialog, ("import",)))
    else:
        _next_step_card("income", f"New to dividends? {GUIDE} can explain how they're paid "
                        "and what reinvesting them means.",
                        (f":material/forum: Ask {GUIDE}", _ask_guide, (INCOME_ASK,)))

    tiles = []
    if plan["total"]:
        peak = max(plan["months"], key=lambda r: r["total"])
        tiles.append(("income_months", ":material/calendar_month:", "Next 12 months",
                      f"Biggest month: {_income_month_label(peak['month'])}",
                      "See the months", _income_months_window, (plan, unsynced)))
    if got is not None:
        tiles.append(("income_received", ":material/savings:", "What was paid",
                      f"Dividends {fmt_money(got['dividends'])} · interest "
                      f"{fmt_money(got['interest'])}", "See what was paid",
                      _income_received_window, (got,)))
    if income_rows:
        import income
        yoc = income.yield_on_cost_total(income_rows)
        tiles.append(("income_holdings", ":material/list:", "By holding",
                      f"{len(income_rows)} holding{'s' if len(income_rows) != 1 else ''} with a "
                      f"dividend yield · most from {income_rows[0]['symbol']}"
                      + (f" · your yield on cost {fmt_pct_level(yoc)}"
                         if yoc is not None else ""),
                      "See all holdings", _income_table_window, (income_rows,)))
    _detail_tiles(tiles)
    st.caption("Estimates repeat each holding's last year of payments at today's share count - "
               "dividends can change.")
    learn_more("dividends")
    _calm_footer()


if PAGE == "Income":
    # ---- income: received, the next 12 months, and yields per holding ------ #
    _income_rows = _income_yield_rows()
    # what was actually paid in the last 12 months, from an imported activity
    # export (income.received; None when none was imported), and what the
    # schedule needs - one connection
    _income_got, _income_synced, _income_paid = _income_reads()
    _income_plan, _income_unsynced = _income_schedule(_income_rows, _income_synced,
                                                      _income_paid)
    if _show_everything():
        _render_income_received(_income_got)
        _render_income_by_month(_income_plan, _income_unsynced)
        _render_income_table(_income_rows)
    else:
        _render_income_calm(_income_rows, _income_plan, _income_unsynced, _income_got)
