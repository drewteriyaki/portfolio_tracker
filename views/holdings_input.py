# Part of dashboard.py, which runs this file with _view("holdings_input") at the point
# where this code used to sit, in dashboard.py's own namespace: the names here
# (st, DB, USER_ID, PAGE, the helpers...) are dashboard.py's, and what this
# defines is visible there afterwards. See _view() in dashboard.py.
#
# Getting holdings in: paste, type by hand, screenshots, CSV files, the shared
# review-and-save step, and the example portfolio.
# ruff: noqa: F821

import starter_funds
import ticker_search
import txn_import
from portfolio import (PRETEND_SOURCES, current_holdings, prepare_save, remove_account,
                       save_prepared)


def _after_import():
    """A new statement can bring new tickers: fetch their prices and history
    on the next run instead of waiting for the scheduled jobs."""
    st.session_state.pop("auto_backfilled", None)
    st.session_state["dialog_open"] = False  # saved: the dialog is closing
    for k in ("last_open_snapshot", "value_logged", "export_zip"):  # it changed: start afresh
        st.session_state.pop(k, None)
    # "Since your last visit" starts again from the holdings just saved: the
    # next run logs their value at once (dashboard.py), and perf.last_open
    # skips visits logged before the save - a save isn't a market move
    st.session_state["value_rebase"] = True


def _manual_saved(current_positions, current_cash):
    """The latest holdings as saved - account names as the broker's, not
    nicknames: (positions, {account: cash})."""
    to_broker = {v: k for k, v in ACCOUNT_LABELS.items()}
    base = [{**p, "account": p.get("broker_account") or p.get("account")} for p in current_positions]
    return base, {to_broker.get(a, a): v for a, v in current_cash.items()}


def _manual_rows_init(current_positions, current_cash, current_source=None):
    """Start the hand-entry form from the latest snapshot (once per opening)."""
    if "me_ids" in st.session_state:
        return
    base, cash = _manual_saved(current_positions, current_cash)
    holdings, cash_rows = manual_entry.prefill(base, cash)
    weights, cash_pct = manual_entry.prefill_weights(base, cash)
    pct_by = {(w["Account"], w["Symbol"]): w["Percent"] for w in weights}
    holdings = holdings or [{"Account": manual_entry.DEFAULT_ACCOUNT, "Type": "Other"}]
    cash_rows = cash_rows or [{"Account": holdings[0]["Account"], "Cash": None}]
    ss = st.session_state
    ss["me_next"] = 0
    ss["me_ids"], ss["me_cash_ids"] = [], []
    ss["me_mode"] = "Percentages" if current_source == manual_entry.PCT_SOURCE else "Shares"
    # Number fields keep their values here, not in their widget keys: Streamlit
    # drops a widget's value on a run where it isn't shown, and the Shares and
    # Percentages fields take turns being hidden.
    ss["me_vals"] = {"cash_pct": cash_pct if base else None, "total": manual_entry.DEFAULT_TOTAL}
    if current_source == manual_entry.PCT_SOURCE and base:
        # keep the pretend total it was made with
        ss["me_vals"]["total"] = round(sum(p.get("market_value") or 0.0 for p in base)
                               + sum(v or 0.0 for v in cash.values()), 2)
    names = {p.get("symbol"): p.get("description") for p in base}
    for r in holdings:
        _manual_add_row({**r, "Percent": pct_by.get((r["Account"], r.get("Symbol"))),
                         "Name": names.get(r.get("Symbol"))})
    for r in cash_rows:
        _manual_add_cash(r)
    # names this copy already knows, for the name-or-ticker box when Yahoo's
    # search is out of reach (ticker_search.resolve's `extra`)
    conn = connect(DB)
    try:
        ss["me_known"] = ticker_search.known(conn, USER_ID)
    finally:
        conn.close()


def _manual_number(label, name, *, min_value=0.0, **kw):
    """A number field whose value lives in me_vals (see _manual_rows_init)."""
    vals = st.session_state["me_vals"]
    wkey = f"me_w_{name}"

    def keep():
        vals[name] = st.session_state.get(wkey)
        st.session_state.pop("me_review", None)
    st.session_state[wkey] = vals.get(name)
    return st.number_input(label, key=wkey, min_value=min_value, on_change=keep, **kw)


def _manual_new_id():
    st.session_state["me_next"] += 1
    return st.session_state["me_next"]


def _manual_add_row(r=None, *, account=None, keep_review=False):
    """A row in the form. `r` (prefill, paste, screenshots) gives its
    symbol - a ticker already, taken as is - and numbers; with none it's an
    empty row in `account` (else the last row's)."""
    r = r or {"Account": account or _manual_last_account(), "Type": "Other"}
    i = _manual_new_id()
    st.session_state[f"me_acct_{i}"] = r.get("Account") or manual_entry.DEFAULT_ACCOUNT
    st.session_state[f"me_sym_{i}"] = r.get("Symbol") or ""
    if r.get("Symbol"):   # from a list: a ticker, not a name to look up
        st.session_state[f"me_name_{i}"] = {"symbol": str(r["Symbol"]).strip().upper(),
                                            "name": r.get("Name")}
    st.session_state["me_vals"].update({f"qty_{i}": r.get("Shares"), f"cost_{i}": r.get("Total cost"),
                                        f"pct_{i}": r.get("Percent")})
    st.session_state[f"me_type_{i}"] = r.get("Type") or "Other"
    st.session_state["me_ids"].append(i)
    if not keep_review:
        st.session_state.pop("me_review", None)


def _manual_add_account(existing):
    """+ Add another account: a new card, named so no account has it yet."""
    taken = list(existing) + [_manual_acct(i) for i in st.session_state["me_ids"]]
    _manual_add_row(account=accounts.suggest_account(None, taken, ACCOUNT_LABELS))


def _manual_acct(i, cash=False):
    """Row (or cash row) i's account."""
    return (st.session_state.get(f"me_{'c' if cash else ''}acct_{i}") or "").strip() or \
        manual_entry.DEFAULT_ACCOUNT


def _manual_groups():
    """The accounts in the form, in the order first met - each drawn as its
    own card. Rows are kept together by account, so "Row 3" in a message
    is the third one down."""
    ss = st.session_state
    order = []
    for i in ss["me_ids"]:
        if _manual_acct(i) not in order:
            order.append(_manual_acct(i))
    for i in ss["me_cash_ids"]:
        if ss["me_vals"].get(f"cash_{i}") is not None and _manual_acct(i, True) not in order:
            order.append(_manual_acct(i, True))
    ss["me_ids"] = sorted(ss["me_ids"], key=lambda i: order.index(_manual_acct(i)))
    return order


def _manual_rename(old, wkey):
    """An account card's name changed: its rows and cash move with it."""
    ss = st.session_state
    new = (ss.get(wkey) or "").strip()
    if not new or new == old:
        return
    for i in ss["me_ids"]:
        if _manual_acct(i) == old:
            ss[f"me_acct_{i}"] = new
    for i in ss["me_cash_ids"]:
        if _manual_acct(i, True) == old:
            ss[f"me_cacct_{i}"] = new
    ss.pop("me_review", None)


def _manual_lookup(i):
    """What row i's "Stock or fund" box holds (ticker_search.resolve): a
    ticker taken as is, a name to confirm, or not found. A row filled from
    a list, or a suggestion already picked, isn't looked up again."""
    ss = st.session_state
    text = (ss.get(f"me_sym_{i}") or "").strip()
    picked = ss.get(f"me_name_{i}")
    if text and picked and picked["symbol"] == text.upper():
        return {"status": "ticker", "query": text, "symbol": picked["symbol"],
                "name": picked.get("name"), "kind": None, "choices": [], "online": True}
    return ticker_search.resolve(text, extra=ss.get("me_known") or ())


def _manual_take(i, choice):
    """Row i is `choice` ({symbol, name, kind}): the box shows its ticker."""
    ss = st.session_state
    ss[f"me_sym_{i}"] = choice["symbol"]
    ss[f"me_name_{i}"] = {"symbol": choice["symbol"], "name": choice.get("name")}
    if choice.get("kind") in manual_entry.TYPES:
        ss[f"me_type_{i}"] = choice["kind"]
    ss.pop("me_review", None)


def _manual_typed(i):
    """Something new typed in row i's box: an exact ticker is taken at once
    (upper-cased, its kind noted); a name waits for a pick."""
    ss = st.session_state
    ss.pop(f"me_name_{i}", None)
    ss.pop(f"me_alt_{i}", None)
    ss.pop("me_review", None)
    found = _manual_lookup(i)
    if found["status"] == "ticker":
        _manual_take(i, found)


def _manual_alt(i, choices):
    sym = st.session_state.get(f"me_alt_{i}")
    pick = next((c for c in choices if c["symbol"] == sym), None)
    if pick:
        _manual_take(i, pick)


def _manual_add_cash(r=None):
    r = r or {"Account": _manual_last_account()}
    i = _manual_new_id()
    st.session_state[f"me_cacct_{i}"] = r.get("Account") or manual_entry.DEFAULT_ACCOUNT
    st.session_state["me_vals"][f"cash_{i}"] = r.get("Cash")
    st.session_state["me_cash_ids"].append(i)
    st.session_state.pop("me_review", None)


def _manual_last_account():
    ids = st.session_state.get("me_ids") or []
    return (st.session_state.get(f"me_acct_{ids[-1]}") if ids else None) or \
        manual_entry.DEFAULT_ACCOUNT


def _manual_remove(kind, i):
    st.session_state[kind].remove(i)
    st.session_state.pop("me_review", None)


def _manual_form_rows():
    ss = st.session_state
    v = ss["me_vals"]
    holdings = [{"Account": ss.get(f"me_acct_{i}"), "Symbol": ss.get(f"me_sym_{i}"),
                 "Shares": v.get(f"qty_{i}"), "Total cost": v.get(f"cost_{i}"),
                 "Percent": v.get(f"pct_{i}"), "Type": ss.get(f"me_type_{i}")}
                for i in ss["me_ids"]]
    cash = [{"Account": ss.get(f"me_cacct_{i}"), "Cash": v.get(f"cash_{i}")}
            for i in ss["me_cash_ids"]]
    return holdings, cash


def _manual_accounts(saved):
    """The accounts the form knows: those saved (`saved`) and those named in
    its rows (a row with a symbol, or cash) - for the account choice."""
    ss = st.session_state
    v = ss.get("me_vals") or {}
    named = [ss.get(f"me_acct_{i}") for i in ss.get("me_ids", [])
             if (ss.get(f"me_sym_{i}") or "").strip()]
    named += [ss.get(f"me_cacct_{i}") for i in ss.get("me_cash_ids", [])
              if v.get(f"cash_{i}") is not None]
    out = []
    for a in [*saved, *named]:
        a = (a or "").strip()
        if a and a not in out:
            out.append(a)
    return out


def _account_choice(label, key, existing, suggested, *, help=None):
    """Which account some holdings are for: one already here, or a new one
    (type its name). Starts on `suggested` (accounts.suggest_account) until
    a choice is made. Returns the account's name, as it's saved."""
    ss = st.session_state
    if not ss.get(f"{key}_chosen") or not ss.get(key):
        ss[key] = suggested
    options = [*existing, *[a for a in (suggested, ss.get(key)) if a and a not in existing]]
    options = list(dict.fromkeys(options))
    st.selectbox(label, options, key=key, accept_new_options=True, help=help,
                 format_func=lambda a: (accounts.display(a, ACCOUNT_LABELS) or a)
                 + ("" if a in existing else " (new account)"),
                 on_change=lambda: ss.__setitem__(f"{key}_chosen", True))
    return (ss.get(key) or suggested).strip()


def _paste_account(text, existing):
    """The account a paste goes in, unless one was chosen: the brokerage's
    name when the text gives it away, else a new neutral name."""
    ss = st.session_state
    if ss.get("me_paste_acct_chosen") and (ss.get("me_paste_acct") or "").strip():
        return ss["me_paste_acct"].strip()
    return accounts.suggest_account(accounts.guess_broker(text), existing, ACCOUNT_LABELS)


def _manual_from_paste(existing):
    """Add what paste_parse finds in the pasted text to the form, in the
    chosen account, then forget the text."""
    ss = st.session_state
    text = ss.get("me_paste") or ""
    acct = _paste_account(text, existing)
    found = paste_parse.parse(text)
    ss["me_paste"] = ""  # the pasted text isn't kept, even in this session
    if not found["holdings"]:
        ss["me_paste_msg"] = ("warning", "Couldn't find any holdings in that text. Try copying "
                              "just the positions table from your brokerage's site - or add "
                              "them one by one under **Type them in**.")
        return
    ss["me_fill_msg"] = _manual_fill(found, acct)
    ss.pop("me_paste_acct_chosen", None)  # the next paste is asked about afresh
    ss["me_way"] = WAY_TYPE   # show the rows it filled in


def _manual_fill(found, acct):
    """Add `found` (paste_parse.parse() / screenshot_read shape) to the form as
    account `acct`'s holdings - other accounts' rows stay. The first fill of
    an account replaces the rows it had (they're its holdings as last saved;
    the paste is the new list); a later one adds to them (the next part of a
    long list), a symbol already there taking the new numbers. Returns the
    (kind, message) to show."""
    ss = st.session_state
    acct = (acct or "").strip() or manual_entry.DEFAULT_ACCOUNT
    filled = ss.setdefault("me_filled", [])
    v = ss["me_vals"]
    # rows with nothing in them go (the empty row a new form starts with)
    ss["me_ids"] = [i for i in ss["me_ids"] if (ss.get(f"me_sym_{i}") or "").strip()]
    ss["me_cash_ids"] = [i for i in ss["me_cash_ids"] if v.get(f"cash_{i}") is not None]
    mine = [i for i in ss["me_ids"] if (ss.get(f"me_acct_{i}") or "").strip() == acct]
    replaced = 0
    if acct not in filled:
        replaced = len(mine)
        ss["me_ids"] = [i for i in ss["me_ids"] if i not in mine]
        mine = []
    by_sym = {(ss.get(f"me_sym_{i}") or "").strip().upper(): i for i in mine}
    for h in found["holdings"]:
        r = {"Account": acct, "Symbol": h["Symbol"], "Shares": h["Shares"],
             "Total cost": h["Total cost"], "Percent": h["Percent"],
             "Type": h.get("Type") or "Other"}
        i = by_sym.get(h["Symbol"])
        if i is None:
            _manual_add_row(r)
        else:
            v.update({f"qty_{i}": r["Shares"], f"cost_{i}": r["Total cost"],
                      f"pct_{i}": r["Percent"]})
    cash = found["cash"] if found["mode"] == "Shares" else None
    cash_ids = [i for i in ss["me_cash_ids"] if (ss.get(f"me_cacct_{i}") or "").strip() == acct]
    if cash is not None:
        ss["me_cash_ids"] = [i for i in ss["me_cash_ids"] if i not in cash_ids]
        _manual_add_cash({"Account": acct, "Cash": cash})
    elif not cash_ids:
        _manual_add_cash({"Account": acct, "Cash": None})
    if acct not in filled:
        filled.append(acct)
    ss["me_mode"] = found["mode"]
    ss.pop("me_review", None)
    n = len(found["holdings"])
    shown = accounts.display(acct, ACCOUNT_LABELS)
    return ("success", f"Found {n} holding{'s' if n != 1 else ''}"
                          + (f" and {fmt_money(cash)} cash" if cash else "")
                          + f" for **{shown}**"
                          + (f", in place of the {replaced} it had" if replaced else "")
                          + " - check them below, then look up prices. Your other accounts "
                            "stay as they are. Type is set to Other; Yahoo works out what "
                            "each one holds.")


def _render_screenshot_reader(existing=()):
    """Read from screenshots: opt-in, the images go to Anthropic's AI (see
    screenshot_read.py). They're read from memory and never kept. What's
    read is added to the form as the account chosen here (`existing`: the
    accounts already known)."""
    ss = st.session_state
    msg = ss.pop("me_shot_msg", None)
    with st.container():
        key = _anthropic_key()
        if not key:
            st.caption("Reading screenshots needs the AI, which isn't set up on this site.")
            return
        st.caption("For phone apps and sites where copying is hard. **Crop each screenshot to "
                   "just your holdings list first** - the whole image is sent to Anthropic's AI "
                   "to read it. Only symbols, share counts and cost are taken from what it "
                   "reads, and the images aren't saved.")
        # both keyed by me_shots_n: a read starts them afresh (empty, unticked)
        # with new keys - a drawn widget's own key can't be set in the same run
        n = ss.get("me_shots_n", 0)
        shots = st.file_uploader(
            "Screenshots", type=sorted(screenshot_read.MEDIA_TYPES), accept_multiple_files=True,
            key=f"me_shots_{n}", label_visibility="collapsed")
        shot_acct = _account_choice(
            "Which account are they from?", "me_shot_acct", list(existing),
            accounts.suggest_account(None, existing, ACCOUNT_LABELS),
            help="Choose one of your accounts to update it, or type a new name to add one. "
                 "Your other accounts stay as they are.")
        agreed = st.checkbox("Send these images to Anthropic's AI to read them",
                             key=f"me_shots_ok_{n}")
        if msg:
            getattr(st, msg[0])(msg[1])
        quota = _ai_status("screenshot")  # this month's allowance (ai_usage.py)
        if not quota["ok"]:
            st.info(ai_usage.used_up_text(quota, "screenshot") + " You can still paste or "
                    "type your holdings.")
        elif quota["limit"]:
            st.caption(ai_usage.left_text(quota, "screenshot").capitalize() + ".")
        if st.button("Read screenshots", key="me_shots_btn",
                     disabled=not (shots and agreed and quota["ok"])):
            images, errors = screenshot_read.check_images([(f.name, f.getvalue()) for f in shots])
            if errors:
                st.error("  \n".join(errors))
                return
            with st.spinner("Reading your screenshots..."):
                found = screenshot_read.read(images, key)
            if found.get("answered"):
                _ai_record("screenshot")  # counted once the AI has read them
            elif found.get("failure") is not None:
                found["error"] = (_ai_failed(found["failure"], "screenshot", "Reading screenshots")
                                  + " You can still paste or type your holdings.")
            del images, shots  # nothing of the images is kept past this point
            ss["me_shots_n"] = n + 1   # empties the uploader and unticks the box
            if found["error"]:
                ss["me_shot_msg"] = ("error", found["error"])
            elif not found["holdings"]:
                ss["me_shot_msg"] = ("warning", "No holdings could be read from those "
                                     "screenshots. Try cropping closer to the list.")
            else:
                ss["me_fill_msg"] = _manual_fill(found, shot_acct)
                ss.pop("me_shot_acct_chosen", None)  # the next read is asked about afresh
                ss["me_way_next"] = WAY_TYPE   # show the rows it filled in
            st.rerun(scope="fragment")


def _manual_clear():
    for k in [k for k in st.session_state if k.startswith("me_")]:
        del st.session_state[k]


def _manual_save(prepared, source):
    """Write a reviewed save (portfolio.prepare_save's result) - any way of
    adding holdings: the snapshot, with every account not in it carried
    forward, and the day's worked-out buys and sells for its accounts."""
    conn = connect(DB)
    try:
        save_prepared(conn, USER_ID, prepared, source)
    except DBError:
        conn.rollback()
        raise
    finally:
        conn.close()


def _acct_name(a):
    """An account as the person knows it: its nickname, else the saved name."""
    return accounts.display(a, ACCOUNT_LABELS) or a


def _review_and_save(meta, rows, totals, source, *, pct_mode=False, key="save_holdings",
                     after=None):
    """"What we'll keep", the change summary and Save, for holdings about to be
    saved as a snapshot - hand entry, paste, screenshots and CSV files alike.
    The save updates only the accounts in it; the others are kept as they are
    (portfolio.prepare_save says how). `after` runs once saved (e.g. clearing
    the form)."""
    conn = connect(DB)
    try:
        p = prepare_save(conn, USER_ID, meta, rows, totals, source, whole=pct_mode)
    finally:
        conn.close()
    mine = set(p["updating"])
    if p["older"]:
        st.info(f"This file is from {_fmt_date(p['file_date'])}, older than your current "
                f"holdings ({_fmt_date(p['older'])}) - its accounts are updated, the rest kept.")
    if p["replaces"] == "example":
        st.caption("These replace the example portfolio.")
    elif p["replaces"] == "percentages":
        st.caption("These real holdings replace your percentages-only portfolio.")
    elif p["replaces"] == "everything":
        st.caption("A percentages-only portfolio is one pretend total, so it replaces all the "
                   "holdings you have now.")
    merged = p["trade_accounts"] is not None   # into the holdings you have now
    if merged or len(mine) > 1:
        new = set(p["new"]) if merged else set()
        _md("**Updating:** " + ", ".join(
            _acct_name(a) + (" (new account)" if a in new else "") for a in p["updating"]))
    if p["kept"]:
        def worth(a):
            return sum(r.get("market_value") or 0.0 for r in p["rows"] if r["account"] == a) + \
                ((p["totals"].get(a) or {}).get("cash_value") or 0.0)
        _md("**Kept as is:** " + ", ".join(f"{_acct_name(a)} ({fmt_money(worth(a))})"
                                         for a in p["kept"]))
        st.caption("Your other accounts stay just as they were - updating one never removes "
                   f"another. To take an account out, use **Remove** under Accounts on "
                   f"{_label('Dashboard')}.")
    st.markdown("**What we'll keep**")
    st.dataframe(pd.DataFrame([{
        "Account": _acct_name(r["account"]), "Symbol": r["symbol"],
        "Name": r["description"] or "",
        "Shares": round(r["quantity"], 4), "Value": fmt_money(r["market_value"]),
        **({} if pct_mode else {"Total cost": fmt_money(r["cost_basis"])
                                if r["cost_basis"] is not None else ""})}
        for r in p["rows"] if r["account"] in mine]), hide_index=True, width="stretch")
    d = p["diff"]
    n_changed = len(d["increased"]) + len(d["decreased"])
    _md(f"Total **{fmt_money(p['total'])}**"
        + (" (pretend)" if pct_mode else " across all your accounts" if p["kept"] else "")
        + " · " + ("in the accounts updated, " if p["kept"] else "")
        + f"{len(d['new'])} new, {n_changed} changed, {len(d['closed'])} removed since "
        + (_fmt_date(p["base_date"]) if p["base_date"] else "nothing yet") + "."
        + (" Not recorded as buys or sells, as the file is older than your holdings."
           if p["older"] and (n_changed or d["closed"]) else ""))
    st.caption(NOT_KEPT)
    if st.button("Save holdings", type="primary", key=key):
        try:
            _manual_save(p, source)
        except DBError as exc:
            st.error(f"Saving failed, nothing was changed: {exc}")
        else:
            n, k = sum(r["account"] in mine for r in p["rows"]), len(p["kept"])
            flash = f"Saved {n} holding{'s' if n != 1 else ''}"
            if k:
                flash += " in " + ", ".join(_acct_name(a) for a in p["updating"])
            flash += f" for {_fmt_date(p['meta']['snapshot_date'])}"
            if k:
                flash += (f"; your {k} other account{'s are' if k != 1 else ' is'} "
                          "kept as before")
            st.session_state["import_flash"] = flash + "."
            if after:
                after()
            _after_import()
            st.rerun()


def _remove_account(account):
    """Remove on Home's Accounts: a new snapshot without `account` (its saved
    name); the others are kept as they are (portfolio.remove_account)."""
    ss = st.session_state
    conn = connect(DB)
    try:
        done = remove_account(conn, USER_ID, account)
    except DBError as exc:
        ss["import_flash"] = f"Removing it failed, nothing was changed: {exc}"
        return
    finally:
        conn.close()
    ss.pop(f"acct_rm_ok_{account}", None)
    if done:
        ss["import_flash"] = (f"Removed {_acct_name(account)} from your holdings. Your other "
                              "accounts are as they were, and its past stays in your history.")
        _after_import()


# the hand-entry window's ways in, as its tabs (key me_way)
WAY_TYPE, WAY_PASTE, WAY_SHOTS, WAY_NEW = ("Type them in", "Paste a list from your brokerage",
                                           "From screenshots", "Not sure yet")
_MODE_LABELS = {"Shares": "Shares I own", "Percentages": "Just percentages"}


def _manual_cash_ids(acct):
    """The cash rows of an account card - one is made (empty) if it has none."""
    ss = st.session_state
    ids = [i for i in ss["me_cash_ids"] if _manual_acct(i, True) == acct]
    if not ids:
        i = _manual_new_id()
        ss[f"me_cacct_{i}"] = acct
        ss["me_vals"][f"cash_{i}"] = None
        ss["me_cash_ids"].append(i)
        ids = [i]
    return ids


def _manual_row(i, pct_mode):
    """One holding in its own box (on a phone its fields stack, and the box
    keeps them together): see _manual_row_fields."""
    with st.container(border=True, key=f"pt_me_hold_{i}"):
        return _manual_row_fields(i, pct_mode)


def _manual_row_fields(i, pct_mode):
    """One holding: what it is, how many (or its %), what was paid, remove -
    then, under it, what the box was matched to."""
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small",
                      key=f"pt_me_row_{i}"):
        st.text_input("Stock or fund (name or ticker)", key=f"me_sym_{i}", width=270,
                      placeholder="e.g. Apple or AAPL", on_change=_manual_typed, args=(i,),
                      help="Type the company or fund's name, or its ticker - the short code "
                           "like AAPL. We'll find it; a name is shown for you to confirm.")
        if pct_mode:
            _manual_number("% of portfolio", f"pct_{i}", max_value=100.0, step=5.0,
                           format="%.1f", width=150, placeholder="e.g. 25")
        else:
            _manual_number("How many shares", f"qty_{i}", step=1.0, format="%g", width=140,
                           placeholder="e.g. 10")
            _manual_number("What you paid in total", f"cost_{i}", step=100.0, format="%.2f",
                           width=180, placeholder="Optional",
                           help="Optional: what you paid for all of these shares together, "
                                "so gains and losses can be shown. Leave it empty if you're "
                                "not sure.")
        st.button(":material/close:", key=f"me_del_{i}", type="tertiary",
                  on_click=_manual_remove, args=("me_ids", i), help="Remove this one")
    found = _manual_lookup(i)
    status = found["status"]
    if status == "ticker" and found.get("name"):
        st.caption(f":green[:material/check:] {found['name']} ({found['symbol']})"
                   .replace("$", r"\$"))
    elif status == "suggest":
        best, others = found["choices"][0], found["choices"][1:]
        with st.container(horizontal=True, vertical_alignment="center", gap="small",
                          key=f"pt_me_pick_{i}"):
            st.markdown(f"Did you mean **{ticker_search.label(best)}**?".replace("$", r"\$"),
                        width="content")
            st.button("Yes", key=f"me_yes_{i}", on_click=_manual_take, args=(i, best),
                      icon=":material/check:")
            if others:
                st.selectbox("Or pick another", [c["symbol"] for c in others], index=None,
                             key=f"me_alt_{i}", label_visibility="collapsed",
                             placeholder="Or pick another...", width=280,
                             format_func=lambda s, o=others: ticker_search.label(
                                 next(c for c in o if c["symbol"] == s)),
                             on_change=_manual_alt, args=(i, others))
    elif status == "unknown":
        st.caption(":material/help: " + (
            f"We couldn't find **{found['symbol']}** by name, so it's taken as a ticker - "
            "its price is checked when you review." if found["online"] else
            f"We'll check **{found['symbol']}** when you review. If it's a company's name "
            "rather than its ticker, try the ticker - the short code, like AAPL for Apple."))
    elif status == "none":
        st.caption(f":orange[:material/search_off:] Couldn't find \"{found['query']}\". "
                   "Check the spelling, or type its ticker - the short code, like AAPL for "
                   "Apple.")
    return found


def _manual_name_errors(found_by_row):
    """[(row number, message)] for boxes that still need a pick or weren't found."""
    out = []
    for n, f in found_by_row:
        if f["status"] == "suggest":
            out.append((n, f"Row {n}: choose which \"{f['query']}\" you mean - tap **Yes** "
                           "under it, or pick another."))
        elif f["status"] == "none":
            out.append((n, f"Row {n}: couldn't find \"{f['query']}\" - check the spelling, "
                           "or type its ticker."))
    return out


def _starter_horizon():
    """Years to the goal date (the plan's), else None: the profile's time
    horizon is used then (learn.starter_mix)."""
    plan = load_plan()
    today = datetime.now().date()
    if plans.has_goal(plan) and plans.months_until(plan["target_date"], today) > 0:
        return plans.months_until(plan["target_date"], today) / 12
    return None


@st.dialog("Add or update holdings", width="large", on_dismiss=_dialog_closed)
def _manual_dialog(current_positions, current_cash, current_source=None):
    """Type in holdings (no file needed); saved as today's snapshot, like an import.
    Shares mode records real holdings; Percentages mode records only each
    holding's share of a pretend total. Tabs: type them in (one row per
    holding), paste a list, read screenshots, or - nothing bought yet - the
    example-funds card (starter_funds.py)."""
    st.session_state["dialog_open"] = True  # live prices wait (see _live_status)
    _manual_rows_init(current_positions, current_cash, current_source)
    ss = st.session_state
    saved_rows, saved_cash = _manual_saved(current_positions, current_cash)
    saved_accts = sorted({r["account"] for r in saved_rows} | set(saved_cash))
    existing = _manual_accounts(saved_accts)
    st.caption(":material/lock: " + TRUST_LINE)
    if ss.get("me_way_next"):   # a screenshot read just filled the rows: show them
        ss["me_way"] = ss.pop("me_way_next")
    # "Not sure yet" (the example funds) isn't for an advisor's client: their
    # advisor recommends what to buy (CLIENT_MODE)
    ways = [WAY_TYPE, WAY_PASTE, WAY_SHOTS] + ([] if CLIENT_MODE else [WAY_NEW])
    if ss.get("me_way") not in ways:
        ss.pop("me_way", None)
    t_type, t_paste, t_shots, *t_new = st.tabs(ways, key="me_way", on_change="rerun")
    with t_paste:
        st.caption("Good for a long list. On your brokerage's website, select your positions "
                   "table, copy it, and paste it here. The app reads it itself - no AI - and "
                   "keeps only symbols, share counts and cost. The pasted text isn't saved.")
        st.text_area("Pasted positions", key="me_paste", height=140,
                     label_visibility="collapsed",
                     placeholder="Paste the positions table copied from your brokerage's site")
        _account_choice("Which account are these from?", "me_paste_acct", existing,
                        _paste_account(ss.get("me_paste") or "", existing),
                        help="Choose one of your accounts to update it, or type a new name to "
                             "add one - one per brokerage account. Your other accounts stay as "
                             "they are.")
        st.button("Fill in from pasted text", key="me_paste_btn", type="primary",
                  on_click=_manual_from_paste, args=(existing,))
        _pm = ss.pop("me_paste_msg", None)
        if _pm:
            getattr(st, _pm[0])(_pm[1])
    with t_shots:
        _render_screenshot_reader(existing)
    for tab in t_new:
        with tab:
            starter_funds.render(_profile(), _starter_horizon(), db=DB, user_id=USER_ID,
                                 key="me_starter")
            st.caption("When you do buy something, come back here and add it under "
                       f"**{WAY_TYPE}**.")
    with t_type:
        _manual_type_tab(current_positions, current_source, saved_rows, saved_cash, existing)


def _manual_type_tab(current_positions, current_source, saved_rows, saved_cash, existing):
    """The rows - one card per account - then look up prices, review, save."""
    ss = st.session_state
    _fm = ss.pop("me_fill_msg", None)   # what a paste or screenshots just filled in
    if _fm:
        getattr(st, _fm[0])(_fm[1])
    if not ss["me_ids"] and not any(ss["me_vals"].get(f"cash_{i}") is not None
                                    for i in ss["me_cash_ids"]):
        _manual_add_row(account=existing[0] if existing else None, keep_review=True)
    st.segmented_control("How to enter them", list(_MODE_LABELS), key="me_mode",
                         required=True, format_func=_MODE_LABELS.get,
                         on_change=lambda: ss.pop("me_review", None))
    pct_mode = ss.get("me_mode") == "Percentages"
    if pct_mode:
        st.caption("No real amounts: give each holding's share of the portfolio, and the app "
                   "works with a pretend total. Allocation, the stock / bond mix, risk and "
                   "projections all work; gains are tracked from today.")
    else:
        st.caption("Add each stock or fund you own: its name or ticker, and how many shares. "
                   "Its value comes from today's price, and nothing is saved until you've "
                   "checked it. To update later, open this again and change what's different.")
    empty = not any((ss.get(f"me_sym_{i}") or "").strip() for i in ss["me_ids"])
    if empty and not saved_rows and not saved_cash:
        st.button("I haven't bought anything yet - not sure what to start with",
                  key="me_go_new", type="tertiary", icon=":material/explore:",
                  on_click=lambda: ss.__setitem__("me_way", WAY_NEW))
    groups = _manual_groups()
    found_by_row, n = [], 0
    for g, acct in enumerate(groups):
        ids = [i for i in ss["me_ids"] if _manual_acct(i) == acct]
        cash_ids = [] if pct_mode else _manual_cash_ids(acct)
        first = (ids or cash_ids or [g])[0]
        with st.container(border=True, key=f"pt_me_acct_{first}"):
            wkey = f"me_gname_{first}"
            ss[wkey] = acct
            st.text_input("Account", key=wkey, width=270, on_change=_manual_rename,
                          args=(acct, wkey),
                          help="Any name you like - the brokerage's, say. One card per "
                               "account; your other saved accounts stay as they are.")
            for i in ids:
                n += 1
                found_by_row.append((n, _manual_row(i, pct_mode)))
            st.button(":material/add: Add another", key="me_add" if g == 0 else f"me_add_{g}",
                      on_click=_manual_add_row, kwargs={"account": acct})
            for i in cash_ids:
                with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
                    _manual_number("Cash in this account (optional)", f"cash_{i}", step=100.0,
                                   format="%.2f", width=270, placeholder="Optional")
    st.button(":material/add_card: Add another account", key="me_add_acct", type="tertiary",
              on_click=_manual_add_account, args=(existing,))
    if pct_mode:
        with st.container(horizontal=True, gap="small"):
            _manual_number("Cash %", "cash_pct", max_value=100.0, step=5.0, format="%.1f",
                           width=150)
            _manual_number("Pretend total", "total", min_value=1.0, step=1000.0, format="%.0f",
                           width=180, help="Any amount - it only sets the scale of the numbers "
                                           "shown.")

    holdings, cash_rows = _manual_form_rows()
    if pct_mode:
        clean, cash_pct, errors = manual_entry.validate_weights(
            holdings, ss["me_vals"].get("cash_pct"), ss["me_vals"].get("total"))
    else:
        clean, cash, errors = manual_entry.validate(holdings, cash_rows)
    # a name still to confirm, or not found: say so instead of "not a ticker"
    name_errors = _manual_name_errors(found_by_row)
    pending = {k for k, _ in name_errors}
    errors = [m for _, m in name_errors] + [
        e for e in errors if not any(e.startswith((f"Row {k}:", f"Row {k} (")) for k in pending)]
    if st.button("Look up prices and review", type="primary", key="me_review_btn"):
        same = set()
        if not errors and not pct_mode and current_source not in PRETEND_SOURCES:
            # an account left just as it was saved is kept as saved, not
            # priced and saved again (the form starts from every account)
            same = manual_entry.unchanged_accounts(clean, cash, saved_rows, saved_cash)
            clean = [h for h in clean if h["account"] not in same]
            cash = {a: c for a, c in cash.items() if a not in same}
        if errors:
            ss.pop("me_review", None)
            st.error("  \n".join(errors))
        elif same and not clean and not cash:
            ss.pop("me_review", None)
            st.info("Nothing has changed - your holdings are just as saved. Change a holding, "
                    "or paste or add another account, then review again.")
        else:
            known = {p["symbol"]: p.get("description") for p in current_positions}
            for i in ss["me_ids"]:   # the names picked in the form
                picked = ss.get(f"me_name_{i}") or {}
                if picked.get("name"):
                    known[picked["symbol"]] = picked["name"]
            with st.spinner("Looking up prices..."):
                found = manual_entry.lookup(
                    [h["symbol"] for h in clean],
                    finnhub_quote=manual_entry.finnhub_price(resolve_key(None, ENV_PATH)),
                    yahoo_info=manual_entry.yahoo_price_and_name, known_names=known)
            ss["me_review"] = (manual_entry.build_weights(clean, cash_pct,
                                                          ss["me_vals"]["total"], found)
                               if pct_mode else manual_entry.build(clean, cash, found))
    review = ss.get("me_review")
    if not review:
        return
    meta, rows, totals, price_errors = review
    if price_errors:
        st.error("  \n".join(price_errors))
        return
    _review_and_save(meta, rows, totals,
                     manual_entry.PCT_SOURCE if pct_mode else manual_entry.SOURCE,
                     pct_mode=pct_mode, key="me_save", after=_manual_clear)


def _load_sample():
    c = connect(DB)
    try:
        sample_data.load(c, USER_ID)
    finally:
        c.close()
    st.session_state["import_flash"] = ("Loaded an example portfolio - explore freely. Clear it "
                                        "any time from the banner at the top.")
    _after_import()


def _clear_sample():
    c = connect(DB)
    try:
        sample_data.clear(c, USER_ID)
    finally:
        c.close()
    st.session_state["import_flash"] = "Example portfolio removed."
    _after_import()


CSV_FIELDS = ("symbol", "quantity", "cost", "avg_cost", "value", "percent", "account",
              "account_number", "description")


def _ai_guess_columns(ai_key, ai_mapping, header, shapes):
    """"Let AI guess the columns" (csv_import / txn_import .ai_mapping): its
    guess goes in session state under `ai_key` ({} when it couldn't tell),
    counted once the AI has answered. None, or what to say if the request
    failed - then nothing is stored or counted, so the button stays to try
    again."""
    import anthropic

    with st.spinner("Working out the columns..."):
        try:
            guess = ai_mapping(header, shapes, _anthropic_key())
        except anthropic.AnthropicError as exc:
            return _ai_failed(exc, "csv", "Guessing the columns")
    _ai_record("csv")  # counted once it has answered
    st.session_state[ai_key] = guess or {}
    return None


def _import_csv_file(src_path, source_name):
    """A positions CSV from any brokerage - one path for every file
    (csv_import.py): find the table, check the columns (matched by name or a
    remembered layout; the AI only if asked, from column names and cell kinds),
    then the same review and save as every other way of adding holdings."""
    ss = st.session_state
    if not os.path.isfile(src_path):
        st.error(f"No file at: {src_path}")
        return
    with open(src_path, "rb") as fh:
        rows = csv_import.read_rows(fh.read())
    header_i, problem = csv_import.find_header(rows)
    if problem == "transactions" or (txn_import.find_header(rows) is not None
                                     and header_i is None):
        _import_txn_file(rows, source_name)
        return
    if header_i is None:
        header_i = csv_import.guess_header(rows)
    if header_i is None:
        st.error("Couldn't find a table of holdings in this file. Try your brokerage's "
                 "Positions export, or paste the positions table instead.")
        return
    header = rows[header_i]
    sig = csv_import.signature(header)
    conn = connect(DB)
    try:
        known = csv_import.remembered(conn, header)
    finally:
        conn.close()
    mapping = known or csv_import.auto_mapping(header)
    ai_key = f"csv_ai_{sig[:12]}"
    if ss.get(ai_key):
        mapping = {**mapping, **ss[ai_key]}
    quota = (_ai_status("csv") if not csv_import.usable(mapping) and _anthropic_key()
             and ai_key not in ss else None)  # this month's allowance (ai_usage.py)
    if quota and not quota["ok"]:
        st.caption(ai_usage.used_up_text(quota, "csv") + " Choose the columns below.")
    elif quota:
        if st.button(":material/auto_awesome: Let AI guess the columns", key=f"{ai_key}_btn",
                     help="Sends only the column names and what kind of thing each cell is "
                          "(text, number, money) - never your holdings or amounts."
                          + (f" {ai_usage.left_text(quota, 'csv').capitalize()}."
                             if quota["limit"] else "")):
            failed = _ai_guess_columns(ai_key, csv_import.ai_mapping, header,
                                       csv_import.sample_shapes(rows, header_i))
            mapping = {**mapping, **ss.get(ai_key, {})}
            if failed:
                st.warning(failed + " Choose the columns below.")
            elif not ss[ai_key]:
                st.warning("The AI couldn't tell either - choose the columns below.")

    names = [f"{c or '(blank)'}  ·  column {i + 1}" for i, c in enumerate(header)]
    ver = "ai" if ss.get(ai_key) else "auto"  # new widgets when the AI's guess arrives
    with st.expander("Check the columns", expanded=not (known and csv_import.usable(mapping))):
        st.caption("Which column holds what. Only these are read; every other column is "
                   "ignored." + (" This layout was remembered from an earlier file." if known
                                 else ""))
        cols = st.columns(3)
        chosen = {}
        for n, field in enumerate(CSV_FIELDS):
            pick = cols[n % 3].selectbox(
                csv_import.LABELS.get(field, "Account number"), [None, *range(len(header))],
                index=(mapping[field] + 1) if field in mapping else 0,
                format_func=lambda i: "—" if i is None else names[i],
                key=f"csvmap_{sig[:10]}_{ver}_{field}")
            if pick is not None:
                chosen[field] = pick
        # the optional extras (dividends, earnings, day change ...) come along
        # when the file names them; they aren't worth a dropdown each
        chosen.update({f: i for f, i in mapping.items() if f not in CSV_FIELDS})
    if not csv_import.usable(chosen):
        st.info("Choose at least the **Symbol** column and **Shares** (or **Value**).")
        return
    found = csv_import.parse(rows, chosen, filename=os.path.basename(source_name.replace("upload: ", "")))
    if not found["holdings"]:
        st.warning("No holdings were found with these columns - check the choices above.")
        return
    if found["mode"] == "Percentages":
        st.info("This file only has percentages, no share counts. Use **Paste or type holdings** "
                "(under **Add holdings** at the top) and its Percentages mode instead.")
        return

    account_default = manual_entry.DEFAULT_ACCOUNT
    if any(not h["Account"] for h in found["holdings"]) or None in found["cash"]:
        # the file doesn't say which account: ask, suggesting the brokerage's
        # name when the file gives it away - never quietly one already here
        conn = connect(DB)
        try:
            cur = current_holdings(conn, USER_ID)
        finally:
            conn.close()
        existing = [] if cur["source"] in PRETEND_SOURCES else sorted(
            {r["account"] for r in cur["rows"]} | set(cur["totals"]))
        broker = accounts.guess_broker(
            "\n".join(" ".join(c for c in r if c) for r in rows), header=header,
            filename=os.path.basename(source_name.replace("upload: ", "")))
        account_default = _account_choice(
            "Which account is this?", f"csv_acct_{sig[:10]}", existing,
            accounts.suggest_account(broker, existing, ACCOUNT_LABELS),
            help="The file doesn't name its account. Choose one of yours to update it, or "
                 "type a new name to add it - your other accounts stay as they are.")
    meta, prow, totals = csv_import.to_snapshot(found, account_default=account_default)
    # the file's own values where it has them; today's price for the rest
    unpriced = [r["symbol"] for r in prow if r["market_value"] is None]
    if unpriced:
        pkey = f"csv_prices_{sig[:10]}"
        if pkey not in ss:
            with st.spinner("Looking up prices..."):
                ss[pkey] = manual_entry.lookup(
                    unpriced, finnhub_quote=manual_entry.finnhub_price(resolve_key(None, ENV_PATH)),
                    yahoo_info=manual_entry.yahoo_price_and_name)
        for r in prow:
            got = ss[pkey].get(r["symbol"]) or {}
            if r["market_value"] is None and got.get("price") and r["quantity"]:
                r["market_value"] = round(got["price"] * r["quantity"], 2)
                r["description"] = r["description"] or got.get("name")
        still = [r["symbol"] for r in prow if r["market_value"] is None]
        if still:
            st.error("No price found for " + ", ".join(still) + " - check those symbols.")
            return
    cash = sum(t["cash_value"] or 0 for t in totals.values())
    _md(f"Found **{len(prow)} holding(s)**" + (f" and {fmt_money(cash)} cash" if cash else "")
        + f" from {_fmt_date(meta['snapshot_date'])}.")
    left_out = found.get("left_out") or []
    if left_out:
        # (a "$" pair would read as math in markdown)
        st.info(csv_import.left_out_text(left_out, fmt_money).replace("$", "\\$")
                + " Everything else in the file is imported as usual.")
    meta["as_of_text"] = meta["as_of_text"] or \
        f"Imported from {os.path.basename(source_name.replace('upload: ', ''))}"

    def _remember_layout():
        c = connect(DB)
        try:
            csv_import.remember(c, header, chosen)  # column names only
        finally:
            c.close()
    _review_and_save(meta, prow, totals, source_name, key="csv_save", after=_remember_layout)


def _save_txns(found_rows, source_name, header, chosen):
    c = connect(DB)
    try:
        res = txn_import.save(c, USER_ID, found_rows, source_name)
        csv_import.remember(c, header, chosen)  # column names only
    except DBError as exc:
        st.session_state["import_flash"] = f"Saving failed, nothing was changed: {exc}"
        return
    finally:
        c.close()
    st.session_state["import_flash"] = (
        f"Added {res['added']} activity row(s) from your brokerage's history"
        + (f"; {res['duplicates']} were already here" if res["duplicates"] else "")
        + (f"; it replaces {res['replaced']} worked out from your updates"
           if res["replaced"] else "") + ". See them on Activity.")
    _after_import()


def _import_txn_file(rows, source_name):
    """An activity (transaction history) export from any brokerage
    (txn_import.py): check the columns, say which account it is, review, save."""
    ss = st.session_state
    header_i = txn_import.find_header(rows)
    if header_i is None:
        st.error("Couldn't find the activity table in this file (a date column and an "
                 "action or amount column).")
        return
    header = rows[header_i]
    sig = csv_import.signature(header)
    conn = connect(DB)
    try:
        known = txn_import.remembered(conn, header)
        existing = [r["account"] for r in conn.execute(
            "SELECT DISTINCT account FROM positions WHERE user_id = ? ORDER BY account",
            (USER_ID,))]
    finally:
        conn.close()
    st.markdown(":material/receipt_long: This is your **activity history** - buys, sells, "
                "dividends and deposits. It fills in your Activity page with what really "
                "happened.")
    mapping = known or txn_import.auto_mapping(header)
    ai_key = f"txn_ai_{sig[:12]}"
    if ss.get(ai_key):
        mapping = {**mapping, **ss[ai_key]}
    quota = (_ai_status("csv") if not txn_import.usable(mapping) and _anthropic_key()
             and ai_key not in ss else None)
    if quota and not quota["ok"]:
        st.caption(ai_usage.used_up_text(quota, "csv") + " Choose the columns below.")
    elif quota and st.button(":material/auto_awesome: Let AI guess the columns",
                             key=f"{ai_key}_btn",
                             help="Sends only the column names and what kind of thing each "
                                  "cell is (date, text, money) - never your activity."):
        failed = _ai_guess_columns(ai_key, txn_import.ai_mapping, header,
                                   csv_import.sample_shapes(rows, header_i))
        mapping = {**mapping, **ss.get(ai_key, {})}
        if failed:
            st.warning(failed + " Choose the columns below.")

    names = [f"{c or '(blank)'}  ·  column {i + 1}" for i, c in enumerate(header)]
    ver = "ai" if ss.get(ai_key) else "auto"
    with st.expander("Check the columns", expanded=not (known and txn_import.usable(mapping))):
        st.caption("Which column holds what. Only these are read." +
                   (" This layout was remembered from an earlier file." if known else ""))
        cols = st.columns(3)
        chosen = {}
        for n, field in enumerate(txn_import.FIELDS):
            pick = cols[n % 3].selectbox(
                txn_import.LABELS[field], [None, *range(len(header))],
                index=(mapping[field] + 1) if field in mapping else 0,
                format_func=lambda i: "—" if i is None else names[i],
                key=f"txnmap_{sig[:10]}_{ver}_{field}")
            if pick is not None:
                chosen[field] = pick
    if not txn_import.usable(chosen):
        st.info("Choose at least the **Date** column and **Action** (or **Amount**).")
        return

    # which of your accounts this is: the file's own names, matched to your holdings'
    default = existing[0] if len(existing) == 1 else manual_entry.DEFAULT_ACCOUNT
    found = txn_import.parse(rows, chosen, account_default=default, header_i=header_i)
    if not found["rows"]:
        st.warning("No activity rows were found with these columns - check the choices above.")
        return
    file_accounts = found["accounts"]
    options = existing + [a for a in file_accounts if a not in existing]
    if "account" in chosen or "account_number" in chosen or not existing or len(existing) > 1:
        st.markdown("**Which account is this?**")
        renames = {}
        for a in file_accounts:
            guess = txn_import.match_account(a, existing) or a
            renames[a] = st.selectbox(
                f"In the file: {a}" if len(file_accounts) > 1 or "account" in chosen
                or "account_number" in chosen else "Account",
                options, index=options.index(guess), key=f"txnacct_{sig[:10]}_{a}",
                format_func=lambda x: accounts.display(x, ACCOUNT_LABELS),
                help="Match it to the account in your holdings, so this history replaces "
                     "what was worked out from your updates for that account.")
        for r in found["rows"]:
            r["account"] = renames.get(r["account"], r["account"])

    s = txn_import.summary(found["rows"])
    conn = connect(DB)
    try:
        have = txn_import.existing_keys(conn, USER_ID)
    finally:
        conn.close()
    already = sum(k in have for k in txn_import.row_keys(found["rows"]))
    kinds = " · ".join(f"{n} {txn_import.TYPES[k].lower()}" for k, n in s["by_type"].most_common())
    _md(f"Found **{len(found['rows'])} row(s)** from {_fmt_date(s['first'])} to "
        f"{_fmt_date(s['last'])}: {kinds}."
        + (f" **{already}** are already here and will be skipped." if already else ""))
    if s["other"]:
        st.caption(f"{len(s['other'])} row(s) didn't match a kind we know - they're kept as "
                   "**Other**, with the brokerage's own wording.")
    st.dataframe(pd.DataFrame([{
        "Date": r["trade_date"], "Kind": txn_import.TYPES[r["action"]],
        "Brokerage says": r["raw_action"], "Symbol": r["symbol"] or "",
        "Shares": fmt_qty(r["quantity"]) if r["quantity"] is not None else "",
        "Amount": fmt_money(r["amount"]) if r["amount"] is not None else "",
        "Account": accounts.display(r["account"], ACCOUNT_LABELS),
    } for r in found["rows"][:200]]), hide_index=True, width="stretch")
    st.caption("Saved: date, kind, symbol, shares, price, amount, fees and the description, "
               "with account and bank numbers cut to their last 3 digits. The file isn't kept. "
               "For each account, this replaces activity worked out from your updates up to "
               f"{_fmt_date(s['last'])}.")
    if st.button("Save activity", type="primary", key="txn_save"):
        _save_txns(found["rows"], source_name, header, chosen)
        st.rerun()


@st.dialog("Import a CSV", width="large", on_dismiss=_dialog_closed)
def _import_dialog():
    """Upload a positions export from any brokerage; check it; save it."""
    st.session_state["dialog_open"] = True  # live prices wait (see _live_status)
    st.caption("Upload your brokerage's **Positions** (or Holdings) export to update what you "
               "hold, or its **Activity** (transaction history) export for your real buys, "
               "sells and dividends - any brokerage. You'll check it before anything is saved.")
    st.caption(":material/lock: " + TRUST_LINE)
    up = st.file_uploader("Positions or activity export (.csv)", type=["csv"], key="csv_upload")
    # A path on "this machine" is only meaningful running locally - on the
    # hosted app it would be a path on the server, which users must not read.
    path_in = "" if pgcompat.is_postgres_dsn(DB) else st.text_input(
        "…or a path to a CSV on this machine",
        key="csv_path",
        placeholder="C:\\Users\\you\\Downloads\\positions.csv",
    ).strip().strip('"')

    if up is not None:
        with temp_upload(up.name, up.getbuffer()) as src_path:  # deleted right after
            _import_csv_file(src_path, upload_label(up.name))
    elif path_in:
        _import_csv_file(path_in, os.path.abspath(path_in))
