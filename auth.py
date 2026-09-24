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
import re
import secrets
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


def get_username(conn: sqlite3.Connection, user_id: int) -> str | None:
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else None


# --------------------------------------------------------------------------- #
# advisor mode
# --------------------------------------------------------------------------- #
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,50}$")


def valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username or ""))


def is_advisor(conn: sqlite3.Connection, user_id: int) -> bool:
    """False for NULL too - accounts created before advisor mode have no value."""
    row = conn.execute("SELECT is_advisor FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["is_advisor"])


def set_advisor(conn: sqlite3.Connection, username: str, flag: bool) -> bool:
    """Returns False if no such user."""
    cur = conn.execute("UPDATE users SET is_advisor = ? WHERE username = ?",
                       (1 if flag else 0, username))
    conn.commit()
    return cur.rowcount > 0


def link_client(conn: sqlite3.Connection, advisor_id: int, client_id: int) -> None:
    conn.execute("INSERT INTO advisor_clients (advisor_id, client_id) VALUES (?, ?) "
                 "ON CONFLICT (advisor_id, client_id) DO NOTHING", (advisor_id, client_id))
    conn.commit()


def unlink_client(conn: sqlite3.Connection, advisor_id: int, client_id: int) -> None:
    conn.execute("DELETE FROM advisor_clients WHERE advisor_id = ? AND client_id = ?",
                 (advisor_id, client_id))
    conn.commit()


def list_clients(conn: sqlite3.Connection, advisor_id: int) -> list[tuple[int, str]]:
    return [(r["id"], r["username"]) for r in conn.execute(
        "SELECT u.id, u.username FROM advisor_clients ac JOIN users u ON u.id = ac.client_id "
        "WHERE ac.advisor_id = ? ORDER BY u.username", (advisor_id,))]


def can_view(conn: sqlite3.Connection, viewer_id: int, target_id: int) -> bool:
    """Whose data a logged-in user may see: their own, plus - if they're an
    advisor - accounts linked to them as clients."""
    if viewer_id == target_id:
        return True
    if not is_advisor(conn, viewer_id):
        return False
    return conn.execute("SELECT 1 FROM advisor_clients WHERE advisor_id = ? AND client_id = ?",
                        (viewer_id, target_id)).fetchone() is not None


def create_client(conn: sqlite3.Connection, advisor_id: int, username: str,
                  password: str | None = None) -> int:
    """Create an account managed by `advisor_id`. Without a password it gets a
    random one nobody knows, so only the advisor can reach it until they set
    a real one with set_password(). Raises ValueError for a non-advisor or
    an invalid username, and the backend's integrity error for a taken one."""
    if not is_advisor(conn, advisor_id):
        raise ValueError("only advisors can create client accounts")
    if not valid_username(username):
        raise ValueError("usernames are 1-50 letters, digits, or . _ @ -")
    client_id = create_user(conn, username, password or secrets.token_urlsafe(32))
    link_client(conn, advisor_id, client_id)
    return client_id
