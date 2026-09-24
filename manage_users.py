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

Password is always prompted interactively via getpass for `create`/`passwd`
(never a CLI arg, so it never ends up in shell history or process
listings). `bulk-create` is the exception - see its own docstring below.
"""

from __future__ import annotations

import argparse
import getpass
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
    rows = conn.execute("SELECT id, username, created_at, is_advisor FROM users ORDER BY id").fetchall()
    if not rows:
        print("No users yet - use `create` to add one.")
        return 0
    for r in rows:
        role = "advisor" if r["is_advisor"] else ""
        print(f"  {r['id']:>3}  {r['username']:<20} {role:<8} created {r['created_at']}")
    return 0


def cmd_set_advisor(args, flag: bool) -> int:
    conn = connect(args.db)
    if not auth.set_advisor(conn, args.username, flag):
        print(f"No such user: '{args.username}'.")
        return 1
    print(f"'{args.username}' is {'now' if flag else 'no longer'} an advisor.")
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
    ap.add_argument("--db", default=DEFAULT_DB, help=f"database (default: {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create a new account")
    p_create.add_argument("username")

    p_passwd = sub.add_parser("passwd", help="change an existing account's password")
    p_passwd.add_argument("username")

    p_bulk = sub.add_parser("bulk-create", help="create many accounts at once from a text file")
    p_bulk.add_argument("file", help="one 'username' or 'username,password' per line")

    sub.add_parser("list", help="list existing accounts")

    for name, help_text in (("make-advisor", "let an account manage client accounts"),
                            ("remove-advisor", "take advisor rights away from an account")):
        sub.add_parser(name, help=help_text).add_argument("username")
    for name, help_text in (("link", "make <client> a client of <advisor>"),
                            ("unlink", "remove <client> from <advisor>'s clients")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("advisor")
        p.add_argument("client")
    sub.add_parser("clients", help="list an advisor's clients").add_argument("advisor")

    args = ap.parse_args(argv)
    if args.cmd == "create":
        return cmd_create(args)
    if args.cmd == "passwd":
        return cmd_passwd(args)
    if args.cmd == "bulk-create":
        return cmd_bulk_create(args)
    if args.cmd in ("make-advisor", "remove-advisor"):
        return cmd_set_advisor(args, args.cmd == "make-advisor")
    if args.cmd == "link":
        return cmd_link(args)
    if args.cmd == "unlink":
        return cmd_unlink(args)
    if args.cmd == "clients":
        return cmd_clients(args)
    return cmd_list(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import pgcompat
        pgcompat.close_all_pools()
