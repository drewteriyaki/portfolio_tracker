#!/usr/bin/env python3
"""Admin-only account management - the ONLY way a new login account gets
created (there is no signup anywhere in the web app itself).

  python manage_users.py create <username> [--db portfolio.db]
  python manage_users.py passwd <username> [--db portfolio.db]
  python manage_users.py list   [--db portfolio.db]

Password is always prompted interactively via getpass (never a CLI arg,
so it never ends up in shell history or process listings).
"""

from __future__ import annotations

import argparse
import getpass
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


def cmd_list(args) -> int:
    conn = connect(args.db)
    rows = conn.execute("SELECT id, username, created_at FROM users ORDER BY id").fetchall()
    if not rows:
        print("No users yet - use `create` to add one.")
        return 0
    for r in rows:
        print(f"  {r['id']:>3}  {r['username']:<20} created {r['created_at']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manage portfolio-tracker login accounts.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"database (default: {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create a new account")
    p_create.add_argument("username")

    p_passwd = sub.add_parser("passwd", help="change an existing account's password")
    p_passwd.add_argument("username")

    sub.add_parser("list", help="list existing accounts")

    args = ap.parse_args(argv)
    if args.cmd == "create":
        return cmd_create(args)
    if args.cmd == "passwd":
        return cmd_passwd(args)
    return cmd_list(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import pgcompat
        pgcompat.close_all_pools()
