# Part of dashboard.py, which runs this file with _view("ticker_detail") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# One ticker's chart and details, opened from the Dashboard or the Watchlist.
# ruff: noqa: F821

import fees


def _ticker_learn_more(sym, pos):
    """One quiet Learn more under a fund's or bond's name: how bonds work for
    something that's mostly bonds (BND, a single bond), ETFs and mutual funds
    for any other fund. Nothing for a single stock."""
    info = sec_info.get(sym) or {}
    split, _ = asset_classes.split_for(sym, pos.get("asset_type"), info, CLASS_OVERRIDES)
    if asset_classes.main_class(split) == "Bonds":
        learn_more("bonds")
    elif fees.holding_type(info.get("quote_type"), pos.get("asset_type")) == "fund":
        learn_more("etfs")


if PAGE in ("Dashboard", "Watchlist"):
    # Each page only renders its own pill strip, so the open ticker comes
    # from that page's strip alone.
    _pill_sym = st.session_state.get("holdings_pill" if PAGE == "Dashboard" else "watchlist_pill")
    # ---- ticker detail: chart + everything about the position/security ----- #
    _idx = next((i for i, p in enumerate(positions) if p["symbol"] == _pill_sym), None) \
        if _pill_sym else None
    _is_held = _idx is not None
    if _pill_sym and not _is_held:
        # Watchlist-only ticker: synthesize a position-less context. eff_price /
        # eff_mv / etc. all read via ctx.get(...).get(...) so a missing "pos"
        # field just resolves to None instead of raising.
        _pos = {"symbol": _pill_sym, "description": sec_info.get(_pill_sym, {}).get("name")}
        _ctx = {"pos": _pos, "quote": quotes.get(_pill_sym, {}), "stats": bar_stats.get(_pill_sym, {}),
                "info": sec_info.get(_pill_sym, {}), "port_value": portfolio_value, "acct_value": None}
    if _pill_sym:
        if _is_held:
            _pos = positions[_idx]
            _ctx = contexts[_idx]
        _sym = _pos["symbol"]
        _has_yahoo = perf.ticker_has_bars(DB, _sym)

        with st.container(border=True):
            # ---- header: symbol, description, live price, today's move ---- #
            hc1, hc2 = st.columns([0.65, 0.35])
            with hc1:
                st.markdown(f"## {_sym}")
                if _pos.get("description"):
                    st.caption(_pos["description"])
                _ticker_learn_more(_sym, _pos)
            _price = M.eff_price(_ctx)
            _dchg_pct = M.value("day_change_pct", _ctx)
            _dchg_usd = M.value("day_change_usd", _ctx)
            # a held ticker: the holding's move in dollars; a watched one has no
            # shares, so the move per share, as on its watchlist row
            _dchg_share = (_ctx.get("quote") or {}).get("change")
            _dchg_text = (fmt_money(_dchg_usd) if _is_held or _dchg_share is None
                          else f"{_dchg_share:+,.2f}")
            with hc2:
                st.metric("Price", fmt_price(_price),
                          delta=(None if hide_amounts or _dchg_pct is None
                                 else f"{_dchg_text} ({_dchg_pct:+.2f}%) today"))
            _price_at = M.value("price_at", _ctx)
            if _price_at:
                st.caption(f"As of {_fmt_when(_price_at)}")

            t1, t2 = st.columns([0.6, 0.4])
            t1.markdown("#### Price history")
            _series = perf.PRICE_SERIES if _has_yahoo else perf.TICKER_SERIES
            _series_label = perf.PRICE_SERIES_LABEL if _has_yahoo else perf.TICKER_SERIES_LABEL
            _series_fmt = perf.PRICE_SERIES_FMT if _has_yahoo else perf.TICKER_SERIES_FMT
            tk_col = t2.selectbox("Ticker series", [c for c, _, _ in _series],
                                  format_func=lambda c: _series_label[c],
                                  key="tk_series_sel", label_visibility="collapsed")

            rng = st.segmented_control("Range", charts.RANGE_LABELS, default="1D",
                                       key="tk_range", label_visibility="collapsed") or "1D"

            if _has_yahoo:
                # ticker_series() already picks the finest resolution Yahoo has for this
                # window (1-minute up through daily) and clips to it at the SQL level.
                _rows, _interval = perf.ticker_series(DB, _sym, charts.RANGE_DAYS[rng])
                _short = False
            else:
                _th = perf.ticker_history(DB, _sym)
                _rows = [{**r, "t": r["fetched_at"]} for r in _th]
                _interval = "sparse"

            if len(_rows) < 2:
                st.info(f"Not enough history for **{_sym}** in this range yet - it fills in by "
                        "itself (price history loads each evening). Try a longer range.")
            else:
                tdf = pd.DataFrame(_rows)
                tdf["t"] = pd.to_datetime(tdf["t"], utc=True, format="mixed")
                full = tdf.dropna(subset=[tk_col]) if tk_col in tdf.columns else tdf.iloc[0:0]

                if full.empty:
                    st.info(f"No **{_series_label[tk_col]}** recorded for {_sym} at this resolution.")
                else:
                    _fname = _series_fmt[tk_col]
                    _title = _series_label[tk_col]
                    if _has_yahoo:
                        win = full  # already clipped to the range by ticker_series()
                    else:
                        win, _short = charts.window(full, "t", charts.RANGE_DAYS[rng])

                    mas = []
                    if _interval == "1d" and tk_col == "close":
                        picked = st.segmented_control(
                            "Moving averages", list(charts.MA_STYLE), format_func=lambda w: f"{w}-day",
                            selection_mode="multi", key="tk_ma", label_visibility="collapsed") or []
                        mas = [(f"ma_{w}", *charts.MA_STYLE[w]) for w in picked if f"ma_{w}" in win.columns]

                    first, last, pct = charts.window_change(win, "t", tk_col)
                    mcol, _sp = st.columns([0.4, 0.6])
                    mcol.metric(
                        _title, FORMATTERS[_fname](last) if _fname in FORMATTERS else mask_or(f"{last:,.2f}"),
                        delta=(None if hide_amounts or pct is None else f"{pct:+.2f}% over {rng}"))

                    _date_fmt = "%b %d, %Y" if _interval == "1d" else "%b %d, %Y  %H:%M"
                    _tips = [alt.Tooltip("t:T", title="Date", format=_date_fmt)]
                    if not hide_amounts:
                        _tips.append(alt.Tooltip(f"{tk_col}:Q", title=_title, format=TOOLTIP_FORMAT[_fname]))
                        for _mc, _, _ in mas:
                            _tips.append(alt.Tooltip(f"{_mc}:Q", title=_mc.replace("ma_", "") + "-day MA",
                                                     format="$,.2f"))
                    st.altair_chart(
                        charts.line(win, x="t", y=tk_col, y_title=_title, y_format=AXIS_FORMAT[_fname],
                                    overlays=mas, tooltip=_tips, mask=hide_amounts,
                                    compress_gaps=(_interval in ("1m", "5m", "15m", "60m"))),
                        width="stretch",
                    )
                    _res_label = perf.INTERVAL_LABEL.get(_interval, _interval)
                    st.caption(
                        f"{len(win)}" + (f" of {len(full)}" if len(win) != len(full) else "") + " points · "
                        + (f"**{_res_label}** Yahoo bars." if _has_yahoo
                           else "sparse refresh history — sync history (:material/history:) for real bars.")
                        + (f"  ·  *{rng} is shorter than the data interval — showing the last {len(win)}.*"
                           if _short else "")
                    )

            # ---- your position (held) or watchlist note ------------------- #
            st.divider()
            if _is_held:
                st.markdown("#### Your position")
                _qty = M.value("quantity", _ctx)
                _cost_basis = M.value("cost_basis", _ctx)
                _avg_cost = (_cost_basis / _qty) if (_qty and _cost_basis is not None) else None
                _mv = M.eff_mv(_ctx)
                _unreal_usd = M.value("unrealized_usd", _ctx)
                _unreal_pct = M.value("unrealized_pct", _ctx)
                _pct_port = M.value("pct_of_portfolio", _ctx)

                pc1, pc2, pc3, pc4 = st.columns(4)
                pc1.metric("Shares", fmt_qty(_qty))
                pc2.metric("Avg Cost", fmt_price(_avg_cost))
                pc3.metric("Market Value", fmt_money(_mv))
                # market value minus cost: the price change (dividends below, when known)
                pc4.metric("Price change", fmt_money(_unreal_usd),
                          delta=(None if hide_amounts or _unreal_pct is None else f"{_unreal_pct:+.2f}%"))

                pc5, pc6, pc7, pc8 = st.columns(4)
                pc5.metric("Cost Basis", fmt_money(_cost_basis))
                pc6.metric("Today's Return", fmt_money(_dchg_usd),
                          delta=(None if hide_amounts or _dchg_pct is None else f"{_dchg_pct:+.2f}%"))
                pc7.metric("% of Portfolio", fmt_pct_level(_pct_port))
                pc8.metric("Account", _pos.get("account") or "—")

                # total return: the price change plus the dividends it paid
                # while held - only when they're known (else the price change alone)
                _tr_usd = M.value("total_return_usd", _ctx)
                if _tr_usd is not None:
                    _tr_pct = M.value("total_return_pct", _ctx)
                    _div = DIVIDENDS.get(_sym) or {}
                    pc9, pc10, _pc11, _pc12 = st.columns(4)
                    pc9.metric("Dividends received", fmt_money(M.value("dividends_usd", _ctx)))
                    pc10.metric("Total return, with dividends", fmt_money(_tr_usd),
                                delta=(None if hide_amounts or _tr_pct is None
                                       else f"{_tr_pct:+.2f}%"))
                    if _div.get("source") == "brokerage":
                        _from = ("From your brokerage's activity history that you imported"
                                 + (f", since {_fmt_date(_div['since'])}" if _div.get("since")
                                    else "") + ".")
                    else:
                        _from = (f"Estimated from what {_sym} paid per share on each ex-dividend "
                                 "date while you held it here"
                                 + (f" (since {_fmt_date(_div['since'])}, when it first shows in "
                                    "your holdings)" if _div.get("since") else "") + ".")
                    st.caption("Total return is the price change plus the dividends this holding "
                               f"paid. {_from} Dividends can change.")
            else:
                st.markdown("#### On your watchlist")
                st.caption("Not a position you own — tracking it for the chart and stats only.")

                def _remove_from_watchlist(sym=_sym):
                    # Must clear the "watchlist_pill" widget's state from a
                    # callback, not the main script body below where it renders -
                    # Streamlit forbids writing to a widget's key after that
                    # widget has already been instantiated in the same run.
                    _wl_conn = connect(DB)
                    try:
                        watchlist.remove(_wl_conn, USER_ID, sym)
                    finally:
                        _wl_conn.close()
                    st.session_state["watchlist_pill"] = None

                st.button("Remove from watchlist", key="wl_remove_from_detail",
                         on_click=_remove_from_watchlist)

            # ---- stats: day range, fundamentals, income -------------------- #
            st.markdown("#### Stats")
            _stat_tiles(_ctx, [
                "prev_close", "day_open", "day_high", "day_low",
                "week52_high", "week52_low", "pct_off_52wk_high",
                "volume", "avg_volume", "beta", "pe_ttm", "pb_ratio", "market_cap", "sector",
                "ma_20", "ma_50", "ma_200", "price_vs_ma50",
                "div_yield_pct", "div_pay_date", "reinvest", "next_earnings",
            ])
            if not _blank(M.value("div_yield_pct", _ctx)):
                learn_more("dividends")   # beside its dividend yield
            if not (_covered or bar_stats or perf.has_bars(DB)):   # any Yahoo history at all
                st.caption("Fundamentals (52-wk range, beta, P/E, market cap, sector, moving averages) "
                           "fill in after you tap sync history (:material/history:) up top.")

            # ---- news: cached Finnhub headlines, fetched when stale --------- #
            st.markdown("#### Recent News")
            _news_key = resolve_key(None, ENV_PATH)
            if not _news_key:
                st.caption("No `FINNHUB_API_KEY` in `.env` — news uses the same key as "
                           "price refresh.")
            else:
                _news_conn = connect(DB)
                try:
                    _n_new, _news_err = news.sync_ticker(_news_conn, _sym, _news_key)
                    _articles = news.latest_news(_news_conn, _sym)
                finally:
                    _news_conn.close()
                if _news_err and not _articles:
                    st.caption(f"Couldn't load news for {_sym}: {_news_err}")
                elif not _articles:
                    st.caption(f"No recent news for {_sym} in the last {news.LOOKBACK_DAYS} days.")
                else:
                    for _a in _articles:
                        _pub = (pd.to_datetime(_a["published_at"], utc=True).strftime("%b %d, %Y %H:%M UTC")
                               if _a["published_at"] else "")
                        st.markdown(f"**[{_a['headline']}]({_a['url']})**  \n*{_a['source']} · {_pub}*")
                        if _a.get("summary"):
                            _sumtext = _a["summary"]
                            st.caption(_sumtext[:220] + ("…" if len(_sumtext) > 220 else ""))
        st.divider()
    else:
        st.caption("↑ Tap a ticker above to see its chart and full details here.")
        st.divider()
