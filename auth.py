"""Individual login accounts. Admin-provisioned only (see manage_users.py) -
there is no self-service signup anywhere in this app; the web dashboard
only ever calls verify_login().

Passwords are never stored in plain text: pbkdf2_hmac('sha256', ...) with a
per-user random salt, both stored as hex in the `users` table. No external
dependency needed - this is stdlib-only (hashlib, hmac, os), same
"standard library only" spirit as the rest of the CLI-facing code.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3

PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    """Create a new account. Raises the backend's own integrity error
    (sqlite3.IntegrityError / psycopg's equivalent, both covered by
    portfolio.DBError) if `username` is already taken."""
    salt = os.urandom(16)
    pw_hash = _hash_password(password, salt)
    conn.execute(
        "INSERT INTO users (username, password_hash, password_salt) VALUES (?, ?, ?)",
        (username, pw_hash, salt.hex()))
    conn.commit()
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return row["id"]


def verify_login(conn: sqlite3.Connection, username: str, password: str) -> int | None:
    """The user's id on a correct username/password, else None. Never
    reveals whether the username or the password was wrong (avoids
    username enumeration) - both a missing user and a wrong password just
    return None."""
    row = conn.execute(
        "SELECT id, password_hash, password_salt FROM users WHERE username = ?",
        (username,)).fetchone()
    if row is None:
        return None
    salt = bytes.fromhex(row["password_salt"])
    candidate = _hash_password(password, salt)
    if hmac.compare_digest(candidate, row["password_hash"]):
        return row["id"]
    return None


def set_password(conn: sqlite3.Connection, username: str, new_password: str) -> bool:
    """Change an existing user's password. Returns False if no such user."""
    salt = os.urandom(16)
    pw_hash = _hash_password(new_password, salt)
    cur = conn.execute(
        "UPDATE users SET password_hash = ?, password_salt = ? WHERE username = ?",
        (pw_hash, salt.hex(), username))
    conn.commit()
    return cur.rowcount > 0


def get_user_id(conn: sqlite3.Connection, username: str) -> int | None:
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return row["id"] if row else None
