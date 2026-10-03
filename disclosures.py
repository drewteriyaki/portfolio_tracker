"""The "About and disclosures" text, kept apart from the page code so the
wording can be reviewed and edited on its own. DRAFT - have the final wording
reviewed by someone qualified before launch (ROADMAP.md, item 5). Fill in
OPERATOR_NAME and CONTACT first; placeholders() lists what's still missing.

Each statement about data here must stay true to the code:
- The AI guide (Ask Northwend) / plan next steps: advisor.portfolio_summary() (tickers, names, % of
  portfolio, asset type and class, sector, gain/loss %, dividend yield, beta,
  P/E - no dollar amounts, share counts or account names), the profile
  answers, the chat, and the guide's saved notes (advisor.system_prompt).
- CSV column guess (only when asked): column names and cell kinds only
  (csv_import.ai_mapping / sample_shapes).
- Screenshots: opt-in, the images themselves (screenshot_read.read); only
  symbols / shares / cost / percent / cash survive screenshot_read.clean().
- Market data: tickers only (update_prices.py / live_prices.py / news.py ->
  Finnhub, sync_history.py / live_prices.py -> Yahoo Finance); the scheduled
  jobs run on GitHub Actions (.github/workflows/scheduled-sync.yml).
- Passwords: PBKDF2 with a per-user salt; stay-signed-in cookies hold a random
  token whose hash is stored; 5 wrong passwords lock a username for 15
  minutes (auth.py).
- Two-step sign-in (two_step.py): TOTP; the key is stored readable (needed to
  check codes), backup codes as SHA-256 hashes; never exported or shown to
  an admin; wrong codes lock like passwords; "remember this device" is a
  date on the stay-signed-in session (login_sessions.two_step_until).
- Sign-up: the email is the login, not shown to others or sent to the AI; only
  Resend gets it, to deliver the confirm / reset emails (mailer.py, auth.sign_up);
  bot checks keep only a SHA-256 of the internet address for a day (signups table); the version agreed to is stored (users.terms_version).
  Email links: hashed, one-time, confirm 3 days / reset 60 minutes; email-send
  limits keep only hashes for a day (email_tokens / email_sends). Unconfirmed
  self-serve accounts can't use the AI (ai_usage.CONFIRM_FOR_AI). Advisor
  sign-ups store firm + licence (advisor_requests) and email them to the
  support address (mailer.advisor_request) for the admin to check; the
  decision is emailed to them (admin.approve_advisor / decline_advisor).
- Advisors' emails to clients (setup link, report waiting, a message waiting)
  carry the advisor's name and firm in the From name only (mailer.sender);
  report and message emails say something is waiting - never figures or the
  message text. The advisor's name for a client ("Chen household") is
  advisor_clients.client_name, seen only by that advisor.
- Meeting prep talking points (meeting.facts_for_ai / talking_points): profile,
  advisor.portfolio_summary, and percentage facts - no dollars, no note text.
- Advisors' Monday email (weekly_email.py, GitHub Actions): counts only (reviews
  due / coming due, accepted proposals, open next steps), no client names or
  figures; confirmed or admin-made emails only; off switch in prefs
  (weekly_email_off); once a week (weekly_email_week).
- Export everything (export.py): the account's own rows as CSV; never password
  hashes, tokens, IP hashes, private advisor notes or other accounts' data.
- Admin portal (admin.py, views/admin.py): logins only - username, email,
  role, created, last sign-in (users.last_login_at), locks; no holdings,
  plans or profile answers.
- Uploads: read from a temporary copy that's deleted (portfolio.temp_upload);
  account numbers cut to 3 digits on save (accounts.mask_number via
  portfolio.write_snapshot). Example / percentages portfolios: sample_data.py,
  manual_entry.PCT_SOURCE. Pasted text: paste_parse / csv_import, no AI.
- Delete all my holdings: portfolio.delete_holdings / HOLDINGS_TABLES (keeps
  plans, profile, notes, settings, watchlist, login); only for an account
  that manages itself (dashboard CAN_MANAGE).
- No usage statistics: .streamlit/config.toml gatherUsageStats = false.
Change this text when any of those change.

Plain text, no "$" (Streamlit would read a pair of them as math).
"""

LAST_UPDATED = "October 1, 2026"
MIN_AGE = 18

# Fill these in before launch - see placeholders().
OPERATOR_NAME = "Andrew Zhang"
CONTACT = "support@northwend.app"

_OPERATOR = OPERATOR_NAME or "[operator name]"
_CONTACT = CONTACT or "[contact email]"

SUMMARY = ("Northwend is an educational tool for following your investments. "
           "It is not financial advice, and it isn't connected to any brokerage.")

SECTIONS = [
    ("Who runs Northwend", f"""
Northwend is a free, early (beta) version, run by an individual developer,
{_OPERATOR}. It may change, have mistakes, or be unavailable at times. Questions,
problems or requests about your data: **{_CONTACT}**.

Northwend is for people **{MIN_AGE} and over**.
"""),
    ("Educational, not advice", """
Everything in this app - the dashboard, plans and projections, the Get started
path, example funds, model portfolios, alerts and the AI guide (Ask Northwend) - is for
education and information only. None of it is a recommendation to buy, sell or
hold any security, and none of it is personalized investment, tax or legal advice.

The people who built and run this app are not acting as your financial advisor
and are not a registered investment adviser or broker-dealer. If an advisor gave
you access, their advice comes from them, not from the app. Consider talking to a
licensed professional before making investment decisions.
"""),
    ("How Northwend is paid", """
Northwend is free while it's in beta. It **doesn't sell investments**: it has no
funds of its own, takes no commissions or trading fees, and no fund company or
brokerage pays to be mentioned in examples or answers. It doesn't show ads or
sell your data. If Northwend ever charges for anything, it will say so here
first, and nothing is charged without your say-so.
"""),
    ("Projections, examples and practice", """
- **Projections are hypothetical.** Plan and goal projections assume a steady
  yearly return (6% unless it's changed), shown with a lower and a higher case
  around it. Real returns go up and down, and can be negative for years at a
  time. A projection is not a promise.
- **Past performance doesn't predict future results.** Historical figures and
  the practice simulation use past prices, which won't repeat the same way.
- **Example funds are examples.** Funds named in Get started, model portfolios
  or AI answers show what a kind of investment looks like. Research any fund
  yourself - its costs, risks and holdings - before investing.
- **All investing involves risk,** including losing the money you put in.
"""),
    ("For advisors", """
If you use Northwend with clients, you remain responsible for your own advice,
licensing, record-keeping and compliance, and for having your clients' consent
to put their holdings here. Northwend doesn't supervise advice or check it for
suitability. A client you add can see their own portfolio, plan and your notes
to them (not ones you mark private); you can see everything in their account.
"""),
    ("Your data", f"""
- **What's stored:** the holdings you or your advisor add (symbols, shares,
  cost, value and account names), any activity history you import (each
  row's date, kind, symbol, shares, price, amount, fees and description, with
  account and bank numbers cut to their last 3 digits), your plan and goals,
  your investing-profile
  answers, notes, and settings, and the name you'd like to be called, if you
  give one (shown in the app, and to your advisor). If you created your
  account yourself, or added an email on the Account page, also
  your email address - used only to sign in and to send you account emails
  (confirming the address, resetting your password; for advisors, an optional
  Monday summary with counts only - no client names or figures), never shown
  to anyone else or sent to the AI - and which version of this page you agreed
  to. Northwend sends no newsletters or marketing email. If you ask for advisor access, also
  your firm's name and your CRD or licence number, so it can be checked.
- **Less is kept than you share:** an uploaded file is read and then deleted -
  the file itself is never kept - and any account number in an account name is
  cut to its last 3 digits before it's saved. Pasted text is read by the app
  itself (not by AI) and isn't saved; only symbols, share counts and cost are
  taken from it.
- **You don't have to share real numbers at all:** try the example portfolio,
  or enter only percentages of a pretend total. Everything except real gains
  and income works the same.
- **Brokerage logins:** Northwend never asks for or stores your brokerage
  username or password, and never connects to your brokerage. It only reads
  what you choose to paste, upload, type in or photograph.
- **Who can see it:** you, and - if your account is managed by an advisor -
  that advisor. To look after accounts, the person who runs Northwend can see
  login details (your email or username, your role, when the account was made
  and last signed in) - not your holdings, plan or answers. It isn't sold,
  rented or shared for advertising.
- **How long it's kept:** until you delete it. The database provider keeps a
  short rolling backup (currently about 6 hours) so data can be recovered after
  an outage; deleted data is gone from it after that.
- **Deleting:** on the **Account** page you can delete all your holdings
  (holdings, cash, activity and value history; your goals, profile answers,
  notes and settings stay), or your whole account and everything in it. If an
  advisor manages your account, ask them, or contact **{_CONTACT}**.
- **Taking a copy:** the **Account** page also downloads everything held for your
  account as spreadsheet (CSV) files - holdings, history, plan, answers,
  settings and what your advisor shared with you. Passwords and sign-in
  records aren't included.
"""),
    ("Security", """
- **Passwords** are stored only as a salted, one-way hash, never as text. After
  5 wrong passwords a username is locked for 15 minutes.
- **Two-step sign-in:** after your password, a 6-digit code from an app on your
  phone, so a password alone isn't enough. Advisor and admin accounts always use
  it; anyone can turn it on from the **Account** page. Backup codes are stored
  only as a scrambled copy. On a device you trust you can skip the code for 30
  days.
- **New accounts:** to stop automated sign-ups, only a few accounts can be
  made from one internet address each day. For that, a scrambled copy of the
  address (not the address itself) is kept for one day.
- **"Stay signed in"** keeps a random token in a cookie on your device; only a
  scrambled copy of it is stored. Changing your password or logging out ends it,
  and a password change signs out your other devices too.
- The site is served over an encrypted connection (HTTPS). No system is
  perfectly secure, though - don't store anything here you couldn't afford to
  have exposed, and use a password you don't use anywhere else.
"""),
    ("Cookies and tracking", """
Northwend sets one cookie of its own, only if you tick **Stay signed in**: it keeps
you signed in on that device. There are no advertising or tracking cookies, and
the app doesn't send usage analytics. The hosting service may set cookies it needs
to run the site.
"""),
    ("Services Northwend uses", """
- **Streamlit Community Cloud** hosts the app.
- **Neon** runs the database, in the United States.
- **Anthropic** (Claude) powers the AI guide, screenshot reading and the optional column
  guess - see the next section for exactly what's sent.
- **Finnhub** and **Yahoo Finance** provide prices, fund details and news; only
  ticker symbols are sent to them.
- **GitHub** runs the scheduled price updates.
- **Resend** delivers the account emails (confirming your address, resetting
  your password, an advisor's Monday summary); it receives only your email
  address and that message.

Each has its own privacy policy.
"""),
    ("What's sent to the AI", """
The app's AI guide (Ask Northwend) and the plan's suggested next steps use Claude,
an AI model from Anthropic. When you use them, the app sends:

- your investing-profile answers (goals, time horizon, risk tolerance and so on),
- your holdings as **tickers, fund names, types and sectors, each one's share of the
  portfolio, its gain or loss as a percentage, and figures like dividend yield,
  beta and P/E** - never dollar amounts, share counts, account names or numbers,
- what you type in the chat, and short notes the guide saved from earlier
  conversations.

If you have an advisor, they can ask the AI to draft **talking points** before
a meeting. That sends the same profile answers and holdings summary, plus facts
in percentages - how your portfolio and goal have moved since the last review,
which holdings were added or reduced, and how far the mix is from its target -
never dollar amounts or your advisor's notes.

If you choose to **read holdings from screenshots**, the images you upload are sent
to the AI so it can read them - that's the only time an image leaves the app, and
you're asked first. Crop them to just your holdings list. Only symbols, share
counts, cost and cash are taken from what it reads, and the images aren't saved.
(Pasted text is different: the app reads it itself and nothing is sent.)

Uploaded files are read by the app itself. Only if a file's columns can't be
matched and you press **Let AI guess the columns** is anything sent: the
**column names and the kind of each cell** ("text", "number", "money") - never
the values, holdings or account numbers in it.

AI answers can be wrong or out of date. Check anything important before acting on it.
"""),
    ("Market data", """
Prices, company details and news come from Finnhub and Yahoo Finance. Prices
update by themselves about every minute while the market is open (crypto around
the clock, mutual funds hourly), but may be delayed (often by 15 minutes) or
occasionally wrong - check your brokerage for exact figures before trading.
"""),
    ("No guarantees", """
Northwend is provided as-is, without warranties of any kind. Figures, prices,
classifications and AI answers may be incomplete, delayed or wrong, and you use
them at your own risk. To the extent the law allows, the people who run Northwend
aren't liable for losses from using it or relying on it.
"""),
    ("Not affiliated", """
Northwend is independent. It isn't affiliated with, endorsed by or connected to any
brokerage, or to Finnhub, Yahoo or Anthropic. Brokerage names are used only to
describe which files and screens it can read.
"""),
    ("Changes to this page", f"""
When this page changes in a way that matters - what's stored, who it's shared
with, what's sent to the AI - the date below changes and the app says so the next
time you sign in. Questions: **{_CONTACT}**.
"""),
]


def placeholders() -> list[str]:
    """What still has to be filled in before launch."""
    return [name for name, value in (("OPERATOR_NAME", OPERATOR_NAME), ("CONTACT", CONTACT))
            if not value]
