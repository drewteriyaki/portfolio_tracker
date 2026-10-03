"""Advisor tools: notes and next steps per client, reviews, model portfolios,
and what makes a client need attention. Pure logic and storage, no
Streamlit.

Notes (`advisor_notes`) are written by the advisor on a client's account:
a *Review* (a meeting - the latest one is the client's last review), a
*Note*, or a *Next step* (an action item the advisor marks done). Anything
marked private is for the advisor only and never shown to the client.

Model portfolios (`model_portfolios`) are an advisor's saved target mixes
by asset class (Stocks / Bonds / Cash / Other), applied to a client's plan
in one step.
"""

from __future__ import annotations

import json
from datetime import date

from asset_classes import CLASSES

NOTE_KINDS = ("Review", "Note", "Next step")
REVIEW_EVERY_DAYS = 90       # a client is due for a review this long after the last one
DRIFT_ATTENTION_PTS = 5.0    # a target-mix gap past this many points needs attention
# what a model portfolio targets: asset classes, not broker types (asset_classes.py)
MODEL_ASSET_TYPES = CLASSES


def advisor_of(conn, client_id: int) -> int | None:
    """The advisor managing this account, if any."""
    row = conn.execute("SELECT advisor_id FROM advisor_clients WHERE client_id = ? "
                       "ORDER BY advisor_id LIMIT 1", (client_id,)).fetchone()
    return row["advisor_id"] if row else None


def waiting_for_client(conn, client_id: int) -> dict:
    """What their advisor has left for a client to look at, in one query:
    {"proposals": shared proposals waiting for their answer, "reports":
    progress reports not opened yet} - Home's next step (route.advisor_step)."""
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM proposals WHERE client_id = ? AND status = 'shared') "
        "AS proposals, (SELECT COUNT(*) FROM progress_reports WHERE client_id = ? AND "
        "read_at IS NULL) AS reports", (client_id, client_id)).fetchone()
    return {"proposals": int(row["proposals"] or 0), "reports": int(row["reports"] or 0)}


def client_can_import(conn, client_id: int) -> bool:
    """Whether a managed client may import their own statements - off unless
    their advisor turned it on (advisor_clients.client_can_import)."""
    row = conn.execute("SELECT 1 FROM advisor_clients WHERE client_id = ? "
                       "AND client_can_import = 1 LIMIT 1", (client_id,)).fetchone()
    return row is not None


def clients_can_import(conn, client_ids) -> set:
    """client_can_import() for several clients in one query: the ids allowed."""
    ids = tuple(client_ids)
    if not ids:
        return set()
    return {r["client_id"] for r in conn.execute(
        "SELECT DISTINCT client_id FROM advisor_clients WHERE client_can_import = 1 AND "
        f"client_id IN ({', '.join('?' for _ in ids)})", ids)}


def set_client_can_import(conn, advisor_id: int, client_id: int, allowed: bool) -> bool:
    """Turn a client's own imports on or off. Only for the advisor's own
    clients; returns False (and changes nothing) otherwise."""
    cur = conn.execute("UPDATE advisor_clients SET client_can_import = ? "
                       "WHERE advisor_id = ? AND client_id = ?",
                       (1 if allowed else 0, advisor_id, client_id))
    conn.commit()
    return cur.rowcount > 0


# ---- notes and next steps --------------------------------------------------- #
def add_note(conn, client_id: int, advisor_id: int, kind: str, body: str, on: str,
             private: bool = False) -> None:
    if kind not in NOTE_KINDS:
        raise ValueError(f"kind must be one of {NOTE_KINDS}")
    body = (body or "").strip()
    if not body:
        raise ValueError("a note needs some text")
    conn.execute("INSERT INTO advisor_notes (client_id, advisor_id, kind, body, note_date, private) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (client_id, advisor_id, kind, body, on, 1 if private else 0))
    conn.commit()


def list_notes(conn, client_id: int, *, include_private: bool) -> list[dict]:
    """Newest first. `include_private` only for the advisor's own view."""
    sql = "SELECT * FROM advisor_notes WHERE client_id = ?"
    if not include_private:
        sql += " AND private = 0"
    return [dict(r) for r in conn.execute(sql + " ORDER BY note_date DESC, id DESC", (client_id,))]


def notes_for(conn, client_ids, *, include_private: bool) -> dict:
    """list_notes() for several clients in one query: {client_id: notes}."""
    ids = tuple(dict.fromkeys(client_ids))
    if not ids:
        return {}
    sql = f"SELECT * FROM advisor_notes WHERE client_id IN ({', '.join('?' for _ in ids)})"
    if not include_private:
        sql += " AND private = 0"
    out = {i: [] for i in ids}
    for r in conn.execute(sql + " ORDER BY client_id, note_date DESC, id DESC", ids):
        out[r["client_id"]].append(dict(r))
    return out


MESSAGE_MAX = 2000           # characters in one message to clients
MESSAGE_REPEAT_MINUTES = 10  # the same message can't go out again this soon (a double click)


def message_clients(conn, advisor_id: int, client_ids, body: str, *, now,
                    today: date | None = None) -> dict:
    """One message from an advisor to several clients (Your clients > Message
    clients): saved as a Note each client sees on their Your advisor page,
    like any other. Only the advisor's own clients (auth.can_view); the same
    text from this advisor within MESSAGE_REPEAT_MINUTES is refused, so a
    double click or a reload can't send it twice. `now` is a UTC datetime;
    `today` the note's date (now's date if None).
    Returns {"ok", "error", "sent_to": [client ids]}."""
    import auth
    from datetime import timedelta

    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "Write a message first.", "sent_to": []}
    if len(body) > MESSAGE_MAX:
        return {"ok": False, "error": f"Keep the message under {MESSAGE_MAX} characters.",
                "sent_to": []}
    ids = [cid for cid in dict.fromkeys(client_ids)
           if cid != advisor_id and auth.can_view(conn, advisor_id, cid)]
    if not ids:
        return {"ok": False, "error": "Pick at least one client.", "sent_to": []}
    since = (now - timedelta(minutes=MESSAGE_REPEAT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    if conn.execute("SELECT 1 FROM advisor_notes WHERE advisor_id = ? AND body = ? AND "
                    "created_at >= ? LIMIT 1", (advisor_id, body, since)).fetchone():
        return {"ok": False, "sent_to": [],
                "error": "You sent this message a moment ago, so it wasn't sent again. "
                         "Change it if you meant to send another."}
    stamp, on = now.strftime("%Y-%m-%d %H:%M:%S"), (today or now.date()).isoformat()
    for cid in ids:
        conn.execute("INSERT INTO advisor_notes (client_id, advisor_id, kind, body, note_date, "
                     "private, created_at) VALUES (?, ?, 'Note', ?, ?, 0, ?)",
                     (cid, advisor_id, body, on, stamp))
    conn.commit()
    return {"ok": True, "error": None, "sent_to": ids}


def open_next_steps(notes: list[dict]) -> list[dict]:
    return [n for n in notes if n["kind"] == "Next step" and not n["done"]]


def set_done(conn, client_id: int, note_id: int, done: bool) -> None:
    conn.execute("UPDATE advisor_notes SET done = ? WHERE id = ? AND client_id = ?",
                 (1 if done else 0, note_id, client_id))
    conn.commit()


def delete_note(conn, client_id: int, note_id: int) -> None:
    conn.execute("DELETE FROM advisor_notes WHERE id = ? AND client_id = ?", (note_id, client_id))
    conn.commit()


def last_review(conn, client_id: int) -> str | None:
    row = conn.execute("SELECT MAX(note_date) AS d FROM advisor_notes "
                       "WHERE client_id = ? AND kind = 'Review'", (client_id,)).fetchone()
    return row["d"] if row else None


def last_review_in(notes: list[dict]) -> str | None:
    """last_review() from the client's notes already read (list_notes with
    include_private - a private Review counts too)."""
    return max((n["note_date"] for n in notes if n["kind"] == "Review"), default=None)


def review_status(last: str | None, today: date) -> tuple[str, int | None]:
    """('never', None), ('due', days since), or ('ok', days since)."""
    if not last:
        return "never", None
    days = (today - date.fromisoformat(str(last)[:10])).days
    return ("due" if days > REVIEW_EVERY_DAYS else "ok"), days


# ---- the weekly summary ------------------------------------------------------ #
SOON_DAYS = 14   # "coming due": a review falls due within this many days
_REVIEW_REASONS = ("Review due", "No review yet")


def week_of(today: date) -> str:
    """'2026-W40' - the summary is shown once per week (Monday to Sunday)."""
    iso = today.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weekly_summary(rows: list[dict]) -> dict:
    """An advisor's week, from the Clients page rows ({"user_id", "name",
    "review", "review_days", "reasons"}): reviews due now (never reviewed
    first, then the most overdue), reviews falling due within SOON_DAYS
    (soonest first, with "in_days"), and clients needing a look for other
    reasons ("other": those reasons). "any" is False when there's nothing to say."""
    due = sorted((r for r in rows if r["review"] in ("never", "due")),
                 key=lambda r: (r["review"] != "never", -(r["review_days"] or 0)))
    soon = sorted(({**r, "in_days": REVIEW_EVERY_DAYS + 1 - r["review_days"]} for r in rows
                   if r["review"] == "ok" and r["review_days"] is not None
                   and r["review_days"] > REVIEW_EVERY_DAYS - SOON_DAYS),
                  key=lambda r: r["in_days"])
    attention = [{**r, "other": other} for r in rows
                 if (other := [x for x in r["reasons"] if x not in _REVIEW_REASONS])]
    # each client once, with every reason: reviews due, then coming due, then the rest
    why = {}
    for r in due:
        why[r["user_id"]] = (r, ["never reviewed" if r["review"] == "never"
                                 else f"last review {r['review_days']} days ago"])
    for r in soon:
        why[r["user_id"]] = (r, [f"review due in {r['in_days']} day"
                                 f"{'s' if r['in_days'] != 1 else ''}"])
    for r in attention:
        why.setdefault(r["user_id"], (r, []))[1].extend(r["other"])
    clients = [{**r, "why": reasons} for r, reasons in why.values()]
    review_ids = {r["user_id"] for r in due} | {r["user_id"] for r in soon}
    return {"due": due, "soon": soon, "attention": attention, "clients": clients,
            # clients to look at for reasons other than a review
            "others": [r for r in attention if r["user_id"] not in review_ids],
            "any": bool(due or soon or attention)}


# ---- model portfolios ------------------------------------------------------- #
def _clean_mix(mix: dict) -> dict:
    return {k: float(v) for k, v in (mix or {}).items() if v and float(v) > 0}


def list_models(conn, advisor_id: int) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM model_portfolios WHERE advisor_id = ? ORDER BY name",
                          (advisor_id,)):
        m = dict(r)
        try:
            m["target_alloc"] = _clean_mix(json.loads(m["target_alloc"]))
        except ValueError:
            m["target_alloc"] = {}
        out.append(m)
    return out


def save_model(conn, advisor_id: int, name: str, mix: dict) -> None:
    """Create a model, or replace the one with the same name."""
    name = (name or "").strip()
    mix = _clean_mix(mix)
    if not name:
        raise ValueError("give the model a name")
    if abs(sum(mix.values()) - 100) > 0.5:
        raise ValueError(f"the mix adds up to {sum(mix.values()):g}% - make it total 100%")
    conn.execute("DELETE FROM model_portfolios WHERE advisor_id = ? AND name = ?", (advisor_id, name))
    conn.execute("INSERT INTO model_portfolios (advisor_id, name, target_alloc) VALUES (?, ?, ?)",
                 (advisor_id, name, json.dumps(mix)))
    conn.commit()


def delete_model(conn, advisor_id: int, model_id: int) -> None:
    conn.execute("DELETE FROM model_portfolios WHERE id = ? AND advisor_id = ?", (model_id, advisor_id))
    conn.commit()


def mix_text(mix: dict) -> str:
    return " · ".join(f"{k} {v:g}%" for k, v in sorted(mix.items(), key=lambda kv: -kv[1]))


# ---- attention ------------------------------------------------------------- #
def max_drift(actual_pct: dict, targets: dict) -> float | None:
    """The largest gap, in percentage points, between the actual mix and the
    target mix (over the asset classes that have a target). None if no targets."""
    if not targets:
        return None
    return max(abs((actual_pct.get(k) or 0.0) - t) for k, t in targets.items())


INACTIVE_DAYS = 60   # a client who signs in has been away this long: worth a check-in


def attention(*, has_data: bool, goal_status: str | None, review: str, n_alerts: int,
              drift: float | None, profile_done: bool, proposal_accepted: bool = False,
              days_since_login: int | None = None) -> list[str]:
    """Why a client needs a look, most pressing first; empty when all good.
    `days_since_login` is None for a client who has never signed in (many
    are managed by the advisor alone) - only someone who used to sign in and
    stopped counts as inactive."""
    reasons = []
    if proposal_accepted:
        reasons.append("Proposal accepted")
    if not has_data:
        reasons.append("No statement yet")
    if goal_status in ("behind", "past_date"):
        reasons.append("Goal behind" if goal_status == "behind" else "Goal date passed")
    elif goal_status is None:
        reasons.append("No goal")
    if review == "never":
        reasons.append("No review yet")
    elif review == "due":
        reasons.append("Review due")
    if n_alerts:
        reasons.append(f"{n_alerts} alert{'s' if n_alerts != 1 else ''}")
    if drift is not None and drift > DRIFT_ATTENTION_PTS:
        reasons.append(f"Drift {drift:.0f} pts")
    if not profile_done:
        reasons.append("Profile incomplete")
    if days_since_login is not None and days_since_login > INACTIVE_DAYS:
        reasons.append(f"Not signed in for {days_since_login} days")
    return reasons
