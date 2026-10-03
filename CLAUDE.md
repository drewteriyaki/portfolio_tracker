# Northwend (portfolio-tracker)

A Streamlit portfolio app: people bring holdings from any brokerage (CSV, paste,
screenshots, by hand, or percentages only) and get live values, charts, a plan,
income and an AI guide with the app's name ("Ask Northwend"; was Waypoint / Sage). Advisors can manage clients.
Live on Streamlit Community Cloud with Neon Postgres; locally it runs on SQLite.

## Working rules
- `ROADMAP.md` is the plan. Work items in order (or the one named), tick them
  `[x]` with a short note in the same commit.
- **Always ask "Commit and push?" before committing or pushing.** Every push is approved.
- **Staging first.** Commit on the `staging` branch and push it; the staging app
  (its own Streamlit Cloud app and Neon database, banner "Staging copy") and the
  GitHub Tests check update. When the change looks right there and Tests is
  green, ask "Release to main?" and then `git push origin staging:main` (a
  fast-forward). `main` is protected: it takes only commits whose Tests check
  passed, no force pushes. Before starting work: `git checkout staging && git pull`.
- Explain the plan before refactoring CSV import (`csv_import.py`) or price fetching
  (`live_prices.py`, `update_prices.py`, `sync_history.py`).
- Never touch the real `portfolio.db` or `.env`. Use scratch copies (see Testing).
- No broker is "primary" - Schwab, Fidelity, Robinhood etc. are all equal.
- Copy for new users should be reassuring, not technical: privacy first, nothing scary.

## Commands
- Tests (all must pass): `python -m unittest discover -s tests` (~30s). Quiet:
  `... 2>&1 | grep -E "^(Ran|OK|FAILED|FAIL:|ERROR:)"`
- Run locally: `python -m streamlit run dashboard.py`. In the desktop app use the
  `.claude/launch.json` previews (`review-class` = scratch `classtest.db`, user alice).
- Python 3.13; `pandas==2.2.3` is pinned on purpose (3.0 is blocked on one machine).

## Where things live
- `dashboard.py` (~1.7k lines) - the app's one Streamlit script: styles, sign-in,
  top bar, settings, formatting helpers, the header, live prices, loading holdings.
  Each page's code is in `views/` and runs inside it via `_view("name")` at the
  point it's listed (same names, no imports needed - read the header of any view):
  `dashboard_page`, `ticker_detail` (one ticker, from Dashboard/Watchlist),
  `watchlist`, `activity`, `income`, `plan`, `get_started` (and `first_steps`, the
  new investor's slideshow shown in its place), `assistant` (Ask Northwend),
  `profile`, `account` (the login's own account: name, email, password, data),
  `clients` (advisor side, weekly summary), `holdings_input` (paste,
  by hand, screenshots, CSV, the save step). Open just the view you need.
  Internal page "AI Assistant" is shown as "Ask Northwend" (`PAGE_LABELS`, `GUIDE = APP_NAME`).
- Data: `portfolio.py` (connect, schema setup + column back-fill, `write_snapshot`,
  delete/sample helpers), `schema.sql` / `schema_pg.sql` (keep **both** in step),
  `pgcompat.py` (SQLite-style SQL on Postgres, pooled connections).
- Getting holdings in: `csv_import.py` (the one CSV engine, any broker, layouts
  remembered; AI only on a button), `paste_parse.py`, `screenshot_read.py` (opt-in AI),
  `manual_entry.py`, `sample_data.py`. All save through dashboard `_review_and_save`.
  Activity (transaction history) exports go to `txn_import.py` from the same
  upload (rows with `origin` 'imported'; worked-out rows have NULL origin).
- Prices: `live_prices.py` (in-app, every minute while open), `update_prices.py`
  (Finnhub; the 15-min job), `sync_history.py` (Yahoo daily/intraday bars, dividends,
  fundamentals; nightly job).
- Numbers: `perf.py` (value over time, bar stats), `income.py`, `allocation.py`,
  `asset_classes.py`, `metrics.py`, `alerts.py`, `changes.py` (buys/sells from
  snapshot differences), `plans.py`, `overview.py` (advisor clients), `fees.py` +
  `views/fees.py` (Fee check; `security_info.expense_ratio` is a fraction - Yahoo's
  `netExpenseRatio` is a percent, the others fractions: `sync_history._expense_ratio`),
  `fund_holdings.py` + `views/fund_overlap.py` (Fund overlap on Home: each fund's top 10
  holdings from Yahoo, fetched only when the window opens, kept a week in the shared
  `fund_top_holdings` table; yield on cost is `income.yield_on_cost`).
- People: `auth.py` (logins, sessions, client setup links, self-serve sign-up,
  confirm / reset links, advisor requests), `two_step.py` + `views/two_step.py`
  (two-step sign-in: `_two_step_gate()` runs inside `_login()` after any way in;
  required for advisors and admins - an AppTest signing one in sets
  `two_step_ok`, see tests/test_menu.py; `manage_users.py reset-two-step`), `admin.py` + `views/admin.py` (the
  Admin portal: logins only, never holdings; admins made only from outside the app: `manage_users.py make-admin` or the
  `NORTHWEND_ADMINS` secret (a list of logins); its System panel shows the copy's
  version, database, email and keys (set or not, never values); a new table with account data must be added to
  `admin.ACCOUNT_TABLES` - a test checks), `route.py` (the route's two stages, Learn - only
  required for the brand new - and Start investing; the investor home's next step),
  `brokerages.py` (Choose a brokerage: names and links only, alphabetical, never a fee
  or a ranking), `proposals.py` (advisor proposals, `views/proposals.py`),
  `gear.py` + `views/kit.py` (milestones and gear: learning and habits only;
  storms.py, the storm note on Home, is drawn there too),
  `meeting.py` (meeting prep, `views/meeting.py`), `reports.py` (client
  progress reports, `views/reports.py`), `mailer.py` (Resend; `MAIL_DRY_RUN=1` logs instead of
  sending - use it for local runs), `manage_users.py`
  (admin account creation, AI limits), `ai_usage.py` (monthly AI allowances - any new
  AI feature checks `_ai_status`, counts with `_ai_record` only after a
  successful answer, and shows failures via `_ai_failed` - never raw error text),
  `advising.py`,
  `advisor.py` (the AI guide, Claude API with prompt caching), `prefs.py`, `accounts.py`.
- Look: `.streamlit/config.toml` (the Northwend theme: colors per light/dark,
  Figtree text and Newsreader titles from `static/`, served at `app/static/`),
  and the `--pt-*` colors at the top of dashboard.py's styles. Keep both in
  step with the Northwend design system.
- Website (northwend.app, Cloudflare Pages): `website/` - templates and assets,
  `build.py` writes `website/public/` (committed, served as is). Edit the
  templates, then run `python website/build.py`; a test fails if `public/` is
  stale. The About page comes from `disclosures.py`; `APP_URL` is in build.py.
  Pages: Home, New to investing, For advisors, About, 404 (`PAGES`); its
  tables are worked out in build.py (no scripts: the CSP allows none) and
  its contour lines are the app's `static/topo-light.svg`. The design is
  the "Northwend website redesign" Claude Design canvas.
- Text/other: `disclosures.py` (draft legal text, placeholders), `learn.py`,
  `client_plan.py` (PDF), `news.py`, `ui_enhancements.js`, `codefresh.py`
  (reloads changed modules on deploy), `friendly_errors.py` (the "something went wrong"
  message; it also hands the error to `error_alerts.py`, R1: type and place only, no
  user_id, emailed to ALERT_EMAIL at most once an hour per kind, hosted copies only -
  a new scheduled job needs its own "Tell the admin it failed" step, a test checks).
- `export.py`: Export everything (Your data); a new table with account data goes
  in `export.OWN` or the test's left-out list.
- Hosting (L4): `render.yaml` (the app on Render, app.northwend.app) and
  `hosting.py` (`CLIENT_IP_HEADER` for the visitor's address behind a proxy;
  `MOVED_TO` turns an old copy into a "has moved" page).
- Packaging: `pyproject.toml` (`pip install -e .`) and `cli.py` (the `northwend*`
  commands). Its `dependencies` match requirements.txt and `py-modules` lists every
  top-level module - a new module goes there too (a test checks).
- Jobs: `.github/workflows/scheduled-sync.yml` (prices every 15 min in market
  hours, history nightly, advisors' Monday email via `weekly_email.py`), `tests.yml`.

## Gotchas
- New columns on old tables: add them to the back-fill list in
  `portfolio._ensure_schema`, not only to the schema files. Indexes on `user_id`
  live in `USER_INDEXES` there (the column is back-filled on old databases).
- Postgres differences: `REAL` becomes DOUBLE PRECISION; `interval` is a keyword
  (qualify it, `b.interval`); timestamps are ISO text `YYYY-MM-DDTHH:MM:SSZ`.
- Every per-account query filters `user_id = ?`; advisors see clients only via `can_view`.
  An advisor's name for a client is `advisor_clients.client_name` (not the client's
  own Account name). Emails an advisor sends go out from their name via
  `mailer.sender` (the address stays hello@); approving or declining an advisor
  goes through `admin.approve_advisor` / `decline_advisor` (they send the email).
- Account numbers are masked to the last 3 digits (`accounts.mask_number`);
  uploads are never stored (`portfolio.temp_upload`).
- Never write tag-like text (`<html>`, `<div>`) in comments inside the app's
  `<style>` block or `ui_enhancements.js`: Streamlit drops the whole block
  (a test checks). Colors go through the `--pt-up` / `--pt-down` / `--pt-warn` variables.
- `dashboard.py` runs top to bottom, views included at their `_view(...)` line: a
  function used while the page is being drawn (sign-in, the top bar) must be
  defined above that point. Button callbacks run later, so they can sit anywhere.
  A new view file needs its `_view("name")` line (a test checks they match).
- The menu is `NAV` (the top bar `pt_topbar` and the phone bar `pt_tabbar`, no
  sidebar, nothing behind a "More") plus `ACCOUNT_MENU` (the name menu `pt_me`:
  Account, About, Admin, Log out). Money is one tab grouping `MONEY_PAGES` (Income,
  Activity, Watchlist - still pages of their own). `PAGES` is every page the
  account can open. A new page goes in `NAV`, under Money or in the name menu -
  keep the bar short. A label change (`PAGE_LABELS`; investors see
  Get started as "Learn") changes its `?page=` slug: add the old one to `OLD_SLUGS`.
- An advisor's client (or an advisor in a client's account) is `CLIENT_MODE`:
  no example funds, no beginner trail or practice money; Home's next step is
  the advisor's (`route.advisor_step`).
- Never name the folder `pages/`: Streamlit turns that into its own page menu.
- Edit files with the Edit tool. Python patch scripts inside Bash heredocs have
  turned `\n` in strings into real newlines before.
- Commit messages: `git commit -F -` with a heredoc (PowerShell breaks on quotes).
- `AppTest` can't drive multi-step dialogs or `data_editor`; check those in the browser.
- Sections that change on their own (the Plan tabs) are `@st.fragment`: a click
  redraws just that part. Use `st.rerun(scope="fragment")` inside one, and a
  plain `st.rerun()` only when something outside it must change too.

## Testing on scratch data
Scratch DBs and scripts live in the session scratchpad, never the repo. To count
the queries a page makes, hook `sqlite3.connect` with `set_trace_callback` and
run the page twice with `AppTest`, timing the second run.
