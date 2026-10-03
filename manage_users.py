#!/usr/bin/env python3
"""Admin-only account management - the ONLY way a new login account gets
created (there is no signup anywhere in the web app itself).

  python manage_users.py create <username> [--db portfolio.db]
  python manage_users.py passwd <username> [--db portfolio.db]
  python manage_users.py list   [--db portfolio.db]
  python manage_users.py bulk-create <file.txt> [--db portfolio.db]
  python manage_users.py make-advisor | remove-advisor <username>
  python manage_users.py link | unlink <advisor> <client>
  python manage_users.py clients <advisor>
  python manage_users.py make-admin | remove-admin <username>
  python manage_users.py reset-two-step <username>

--db can go before or after the command. Without it: the PORTFOLIO_DB
environment variable if it's set, else ./portfolio.db. Commands that change
an account say which database they changed - a live (Postgres) database
needs --db or PORTFOLIO_DB, or the change lands in the local file.

Password is always prompted interactively via getpass for `create`/`passwd`
(never a CLI arg, so it never ends up in shell history or process
listings). `bulk-create` is the exception - see its own docstring below.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys

import auth
from portfolio import DBError, DEFAULT_DB, connect


def cmd_create(args) -> int:
    conn = connect(args.db)
    pw = getpass.getpass(f"Password for new user '{args.username}': ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("Passwords didn't match.")
        return 1
    if not pw:
        print("Password can't be blank.")
        return 1
    try:
        user_id = auth.create_user(conn, args.username, pw)
    except DBError as exc:
        print(f"Could not create user (username already taken?): {exc}")
        return 1
    print(f"Created user '{args.username}' (id={user_id}).")
    return 0


def cmd_passwd(args) -> int:
    conn = connect(args.db)
    pw = getpass.getpass(f"New password for '{args.username}': ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("Passwords didn't match.")
        return 1
    if not pw:
        print("Password can't be blank.")
        return 1
    if auth.set_password(conn, args.username, pw):
        print(f"Password updated for '{args.username}'.")
        return 0
    print(f"No such user: '{args.username}'.")
    return 1


def parse_user_list(text: str) -> list[tuple[str, str | None]]:
    """One account per line: `username` or `username,password`. Blank lines
    and `#`-comments are skipped. A line with no password gets a randomly
    generated one (returned as None here, filled in by the caller) - that's
    the normal case for adding a batch of new people quickly without
    inventing N passwords by hand."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",", 1)]
        username = parts[0]
        password = parts[1] if len(parts) > 1 and parts[1] else None
        if username:
            out.append((username, password))
    return out


def cmd_bulk_create(args) -> int:
    """Create many accounts from a text file in one pass - the file itself
    can supply a password per line, or leave it out for a random one
    (shown once in the output table, never stored anywhere recoverable).
    Unlike `create`/`passwd`, a bulk file necessarily has passwords in
    plain text on disk if you choose your own - delete it once you've
    shared the credentials, and never commit it (it's exactly the kind of
    file `git status`/`git add -A` could sweep in by accident)."""
    conn = connect(args.db)
    with open(args.file, encoding="utf-8") as fh:
        entries = parse_user_list(fh.read())
    if not entries:
        print(f"No usernames found in {args.file} (one per line, blank lines/# comments skipped).")
        return 1

    rows = []
    for username, password in entries:
        generated = password is None
        pw = password or secrets.token_urlsafe(9)
        try:
            user_id = auth.create_user(conn, username, pw)
        except DBError:
            rows.append((username, "already exists - skipped", None))
            continue
        rows.append((username, f"created (id={user_id})", pw if generated else "(as supplied)"))

    w = max(len(r[0]) for r in rows)
    print(f"{'Username':<{w}}  {'Status':<26}  Password")
    for username, status, shown_pw in rows:
        print(f"{username:<{w}}  {status:<26}  {shown_pw or ''}")
    print("\nGenerated passwords are shown ONLY above, ONLY this once - save them now. "
          "Delete the input file once everyone has their credentials.")
    return 0


def cmd_list(args) -> int:
    conn = connect(args.db)
    rows = conn.execute("SELECT id, username, created_at, is_advisor, is_admin, ai_unlimited "
                        "FROM users ORDER BY id").fetchall()
    if not rows:
        print("No users yet - use `create` to add one.")
        return 0
    for r in rows:
        role = "admin" if r["is_admin"] else "advisor" if r["is_advisor"] else ""
        ai = "  (no AI limits)" if r["ai_unlimited"] else ""
        print(f"  {r['id']:>3}  {r['username']:<20} {role:<8} created {r['created_at']}{ai}")
    return 0


def _emailed_note(emailed) -> str:
    return {True: "Emailed them about it.",
            False: "The email to them couldn't be sent - let them know yourself.",
            None: ""}[emailed]


def cmd_set_advisor(args, flag: bool) -> int:
    """make-advisor also emails them that it's ready (admin.approve_advisor,
    linking to APP_URL); remove-advisor sends nothing."""
    conn = connect(args.db)
    if flag:
        import admin
        res = admin.approve_advisor(conn, args.username)
        found = res["ok"]
    else:
        found, res = auth.set_advisor(conn, args.username, flag), {"emailed": None}
    if not found:
        print(f"No such user: '{args.username}'.")
        return 1
    print(f"'{args.username}' is {'now' if flag else 'no longer'} an advisor. "
          + _emailed_note(res["emailed"]))
    return 0


def where(db: str) -> str:
    """Which database, for messages - a Postgres host, never its password."""
    from urllib.parse import urlparse
    from pgcompat import is_postgres_dsn
    if is_postgres_dsn(db):
        host = urlparse(db).hostname if "://" in db else next(
            (part[5:] for part in db.split() if part.startswith("host=")), None)
        return f"the Postgres database at {host or 'an unnamed host'}"
    return f"the local file {os.path.abspath(db)}"


def cmd_set_admin(args, flag: bool) -> int:
    """Admin rights (the in-app Admin portal) - granted only here, never in the app."""
    import admin
    conn = connect(args.db)
    if not admin.set_admin(conn, args.username, flag):
        print(f"No such user: '{args.username}' in {where(args.db)}.")
        return 1
    print(f"'{args.username}' is now an admin: the Admin page appears on their next page load."
          if flag else f"'{args.username}' is no longer an admin.")
    print(f"(changed in {where(args.db)})")
    return 0


def cmd_advisor_requests(args) -> int:
    """Accounts waiting for advisor access: check the firm and licence (e.g.
    FINRA BrokerCheck or the SEC's adviser search for a CRD number), then
    make-advisor or decline-advisor."""
    conn = connect(args.db)
    rows = auth.pending_advisor_requests(conn)
    if not rows:
        print("No advisor requests waiting.")
        return 0
    for r in rows:
        print(f"  {r['username']:<30} {r['firm']:<30} licence {r['licence']:<14} "
              f"asked {r['requested_at']}")
    print("\nCheck each one (BrokerCheck: https://brokercheck.finra.org), then "
          "make-advisor <username> or decline-advisor <username>.")
    return 0


def cmd_decline_advisor(args) -> int:
    import admin
    conn = connect(args.db)
    res = admin.decline_advisor(conn, args.username)
    if not res["ok"]:
        print(f"'{args.username}' has no advisor request waiting.")
        return 1
    print(f"Declined '{args.username}''s advisor request - the account stays an investor "
          "account. " + _emailed_note(res["emailed"]))
    return 0


def _two_ids(conn, advisor, client):
    a, c = auth.get_user_id(conn, advisor), auth.get_user_id(conn, client)
    for name, uid in ((advisor, a), (client, c)):
        if uid is None:
            print(f"No such user: '{name}'.")
    return a, c


def cmd_link(args) -> int:
    conn = connect(args.db)
    a, c = _two_ids(conn, args.advisor, args.client)
    if a is None or c is None:
        return 1
    if not auth.is_advisor(conn, a):
        print(f"'{args.advisor}' isn't an advisor - run make-advisor first.")
        return 1
    auth.link_client(conn, a, c)
    print(f"'{args.client}' is now a client of '{args.advisor}'.")
    return 0


def cmd_unlink(args) -> int:
    conn = connect(args.db)
    a, c = _two_ids(conn, args.advisor, args.client)
    if a is None or c is None:
        return 1
    auth.unlink_client(conn, a, c)
    print(f"'{args.client}' is no longer a client of '{args.advisor}'.")
    return 0


def cmd_unlock(args) -> int:
    conn = connect(args.db)
    if auth.unlock_login(conn, args.username):
        print(f"Cleared failed logins for '{args.username}' - they can try again now.")
    else:
        print(f"'{args.username}' had no failed logins or lock to clear.")
    return 0


def cmd_reset_two_step(args) -> int:
    """For someone locked out of two-step sign-in (lost phone and backup
    codes) - an admin's own account too. Signs the account out everywhere."""
    import two_step
    conn = connect(args.db)
    uid = auth.get_user_id(conn, args.username)
    if uid is None:
        print(f"No such user: '{args.username}'.")
        return 1
    was_on = two_step.reset(conn, uid)
    print((f"Two-step sign-in reset for '{args.username}'" if was_on
           else f"'{args.username}' didn't have two-step sign-in on") +
          f" in {where(args.db)}; signed out everywhere. Advisors and admins set it up "
          "again when they next sign in.")
    return 0


def cmd_set_ai_unlimited(args, flag: bool) -> int:
    import ai_usage
    conn = connect(args.db)
    uid = auth.get_user_id(conn, args.username)
    if uid is None:
        print(f"No such user: '{args.username}'.")
        return 1
    ai_usage.set_unlimited(conn, uid, flag)
    print(f"'{args.username}' {'now has no' if flag else 'has the normal'} monthly AI limits.")
    return 0


def cmd_ai_usage(args) -> int:
    """This month's AI use per account, against each allowance."""
    import ai_usage
    conn = connect(args.db)
    month = ai_usage.month_of()
    rows = conn.execute("SELECT u.id, u.username, a.kind, a.used FROM ai_usage a "
                        "JOIN users u ON u.id = a.user_id WHERE a.month = ? "
                        "ORDER BY u.username, a.kind", (month,)).fetchall()
    if not rows:
        print(f"No AI use yet in {month}.")
        return 0
    print(f"AI use in {month}:")
    for r in rows:
        limit = ai_usage.limit_for(conn, r["id"], r["kind"])
        print(f"  {r['username']:<20} {r['kind']:<11} {r['used']:>4} of "
              f"{'unlimited' if limit is None else limit}")
    return 0


def cmd_clients(args) -> int:
    conn = connect(args.db)
    a = auth.get_user_id(conn, args.advisor)
    if a is None:
        print(f"No such user: '{args.advisor}'.")
        return 1
    clients = auth.list_clients(conn, a)
    if not clients:
        print(f"'{args.advisor}' has no clients.")
    for cid, name in clients:
        print(f"  {cid:>3}  {name}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manage portfolio-tracker login accounts.")
    default_db = os.environ.get("PORTFOLIO_DB") or DEFAULT_DB
    ap.add_argument("--db", default=default_db,
                    help="database: a file or a Postgres connection string (default: "
                         "PORTFOLIO_DB if set, else ./portfolio.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create a new account")
    p_create.add_argument("username")

    p_passwd = sub.add_parser("passwd", help="change an existing account's password")
    p_passwd.add_argument("username")

    p_bulk = sub.add_parser("bulk-create", help="create many accounts at once from a text file")
    p_bulk.add_argument("file", help="one 'username' or 'username,password' per line")

    sub.add_parser("list", help="list existing accounts")

    for name, help_text in (("make-advisor", "let an account manage client accounts "
                                             "(approves its advisor request)"),
                            ("remove-advisor", "take advisor rights away from an account"),
                            ("decline-advisor", "turn down an account's advisor request")):
        sub.add_parser(name, help=help_text).add_argument("username")
    sub.add_parser("advisor-requests", help="accounts waiting for advisor access")
    for name, help_text in (("make-admin", "give an account the in-app Admin portal"),
                            ("remove-admin", "take the Admin portal away from an account")):
        sub.add_parser(name, help=help_text).add_argument("username")
    for name, help_text in (("link", "make <client> a client of <advisor>"),
                            ("unlink", "remove <client> from <advisor>'s clients")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("advisor")
        p.add_argument("client")
    sub.add_parser("clients", help="list an advisor's clients").add_argument("advisor")
    sub.add_parser("unlock", help="clear a login lock after too many wrong passwords"
                   ).add_argument("username")
    sub.add_parser("reset-two-step", help="turn off two-step sign-in for someone who lost "
                   "their phone (signs them out everywhere)").add_argument("username")
    for name, help_text in (("ai-unlimited", "lift the monthly AI limits for an account"),
                            ("ai-limited", "give an account the normal monthly AI limits")):
        sub.add_parser(name, help=help_text).add_argument("username")
    sub.add_parser("ai-usage", help="this month's AI use per account")

    # --db also works after the command (make-admin admin1 --db ...)
    for p in sub.choices.values():
        p.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.cmd == "create":
        return cmd_create(args)
    if args.cmd == "passwd":
        return cmd_passwd(args)
    if args.cmd == "bulk-create":
        return cmd_bulk_create(args)
    if args.cmd in ("make-advisor", "remove-advisor"):
        return cmd_set_advisor(args, args.cmd == "make-advisor")
    if args.cmd == "advisor-requests":
        return cmd_advisor_requests(args)
    if args.cmd in ("make-admin", "remove-admin"):
        return cmd_set_admin(args, args.cmd == "make-admin")
    if args.cmd == "decline-advisor":
        return cmd_decline_advisor(args)
    if args.cmd == "link":
        return cmd_link(args)
    if args.cmd == "unlink":
        return cmd_unlink(args)
    if args.cmd == "clients":
        return cmd_clients(args)
    if args.cmd == "unlock":
        return cmd_unlock(args)
    if args.cmd == "reset-two-step":
        return cmd_reset_two_step(args)
    if args.cmd in ("ai-unlimited", "ai-limited"):
        return cmd_set_ai_unlimited(args, args.cmd == "ai-unlimited")
    if args.cmd == "ai-usage":
        return cmd_ai_usage(args)
    return cmd_list(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import pgcompat
        pgcompat.close_all_pools()
