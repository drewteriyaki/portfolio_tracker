"""AI Assistant: an educational investing chatbot for advisors and new investors.

Pure logic, no Streamlit, so everything here is unit-testable; dashboard.py's
_render_assistant() owns the UI.

What leaves the machine: portfolio_summary() builds the only holdings data
the model sees - tickers, names, asset types, sectors, and percentages. It
never includes dollar amounts, share counts, or account names.
"""

from __future__ import annotations

import metrics as M
from allocation import CONCENTRATION_PCT, allocate

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 3

RISK_LEVELS = ("conservative", "moderate", "aggressive")
EXPERIENCE_LEVELS = ("new", "some", "experienced")

# field -> label shown to the model and in the form
PROFILE_FIELDS = {
    "goal": "Long-term goal",
    "time_horizon_years": "Time horizon (years)",
    "target_return_pct": "Target annual return %",
    "risk_tolerance": "Risk tolerance",
    "experience": "Investing experience",
    "notes": "Other notes",
}
# notes is optional context, not something worth asking about
REQUIRED_PROFILE_FIELDS = ("goal", "time_horizon_years", "target_return_pct",
                           "risk_tolerance", "experience")

REFUSAL_TEXT = "Sorry - I can't help with that one. Try asking it a different way."


# --------------------------------------------------------------------------- #
# profile storage
# --------------------------------------------------------------------------- #
def get_profile(conn, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM investor_profiles WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return {f: None for f in PROFILE_FIELDS}
    return {f: row[f] for f in PROFILE_FIELDS}


def save_profile(conn, user_id: int, fields: dict, *, replace: bool = False) -> dict:
    """Merge `fields` into the saved profile and return the result. A None
    value leaves that field alone, unless `replace` is set (the profile form,
    where emptying a field means clearing it)."""
    profile = get_profile(conn, user_id)
    if replace:
        profile = {f: fields.get(f) for f in PROFILE_FIELDS}
    else:
        profile.update({k: v for k, v in fields.items() if k in PROFILE_FIELDS and v is not None})
    cols = list(PROFILE_FIELDS)
    conn.execute(
        f"INSERT INTO investor_profiles (user_id, {', '.join(cols)}, updated_at) "
        f"VALUES (?, {', '.join('?' for _ in cols)}, datetime('now')) "
        f"ON CONFLICT(user_id) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in cols)
        + ", updated_at = datetime('now')",
        (user_id, *(profile[c] for c in cols)))
    conn.commit()
    return profile


def missing_fields(profile: dict) -> list[str]:
    return [f for f in REQUIRED_PROFILE_FIELDS if profile.get(f) in (None, "")]


# --------------------------------------------------------------------------- #
# the one tool: saving profile answers the user gives in chat
# --------------------------------------------------------------------------- #
PROFILE_TOOL = {
    "name": "update_investor_profile",
    "description": (
        "Save facts the user has told you about their investing situation to their "
        "profile. Call it as soon as the user states any of these; pass null for "
        "anything they haven't mentioned in this message so it stays unchanged."
    ),
    "strict": True,
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "goal": {"type": ["string", "null"],
                     "description": "Their long-term goal in a sentence, e.g. 'retire at 60'."},
            "time_horizon_years": {"type": ["integer", "null"],
                                   "description": "Years until they need the money."},
            "target_return_pct": {"type": ["number", "null"],
                                  "description": "Annual return they're aiming for, in percent."},
            "risk_tolerance": {"anyOf": [{"type": "string", "enum": list(RISK_LEVELS)},
                                         {"type": "null"}]},
            "experience": {"anyOf": [{"type": "string", "enum": list(EXPERIENCE_LEVELS)},
                                     {"type": "null"}]},
            "notes": {"type": ["string", "null"],
                      "description": "Other relevant context (income stability, big upcoming expenses, preferences)."},
        },
        "required": list(PROFILE_FIELDS),
        "additionalProperties": False,
    },
}


def validate_profile_input(args) -> tuple[dict | None, str]:
    """(fields_to_save, error). Only non-null, valid values are returned.
    Eager input streaming means the API doesn't validate the tool input for
    us, so this is the real check."""
    if not isinstance(args, dict):
        return None, "input must be an object"
    unknown = set(args) - set(PROFILE_FIELDS)
    if unknown:
        return None, f"unknown fields: {', '.join(sorted(unknown))}"
    out = {}
    for field, value in args.items():
        if value is None:
            continue
        if field in ("goal", "notes"):
            if not isinstance(value, str) or len(value) > 1000:
                return None, f"{field} must be text under 1000 characters"
            if value.strip():
                out[field] = value.strip()
        elif field == "time_horizon_years":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 80:
                return None, "time_horizon_years must be between 1 and 80"
            out[field] = int(value)
        elif field == "target_return_pct":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 50:
                return None, "target_return_pct must be between 0 and 50"
            out[field] = float(value)
        elif field == "risk_tolerance":
            if value not in RISK_LEVELS:
                return None, f"risk_tolerance must be one of {', '.join(RISK_LEVELS)}"
            out[field] = value
        elif field == "experience":
            if value not in EXPERIENCE_LEVELS:
                return None, f"experience must be one of {', '.join(EXPERIENCE_LEVELS)}"
            out[field] = value
    return out, ""


# --------------------------------------------------------------------------- #
# what the model is told
# --------------------------------------------------------------------------- #
def _pct(v):
    return "n/a" if v is None else f"{v:.1f}%"


def _num(v):
    return "n/a" if v is None else f"{v:.2f}"


def portfolio_summary(contexts: list[dict], cash_by_account: dict) -> str:
    """Weights-only description of the holdings. `contexts` is dashboard.py's
    per-position metric context list."""
    if not contexts:
        return "No holdings yet - this person hasn't imported any positions."

    alloc = allocate([c["pos"] for c in contexts], cash_by_account)
    rows = []
    for c in contexts:
        rows.append((
            M.value("pct_of_portfolio", c) or 0.0,
            f"- {c['pos']['symbol']} ({M.value('description', c) or 'unknown'}): "
            f"{_pct(M.value('pct_of_portfolio', c))} of portfolio; "
            f"type {M.value('asset_type', c) or 'unknown'}; "
            f"sector {M.value('sector', c) or 'n/a'}; "
            f"gain/loss {_pct(M.value('unrealized_pct', c))}; "
            f"dividend yield {_pct(M.value('div_yield_pct', c))}; "
            f"beta {_num(M.value('beta', c))}; P/E {_num(M.value('pe_ttm', c))}",
        ))
    rows.sort(key=lambda r: r[0], reverse=True)

    mix = ", ".join(f"{r['label']} {_pct(r['pct'])}" for r in alloc["by_asset_type"])
    n_accounts = len({c["pos"].get("account") for c in contexts} | set(cash_by_account))
    return "\n".join([
        f"{len(contexts)} positions across {n_accounts} account(s).",
        f"Asset mix: {mix}.",
        "Positions, largest first:",
        *(r[1] for r in rows),
    ])


def system_prompt(profile: dict, summary: str) -> str:
    known = [f"- {PROFILE_FIELDS[f]}: {profile[f]}" for f in PROFILE_FIELDS
             if profile.get(f) not in (None, "")]
    missing = missing_fields(profile)

    parts = [
        "You are the investing assistant inside a portfolio-tracking website. The people "
        "you talk to are financial advisors working with clients, and individual investors - "
        "often new ones who find investing overwhelming. Your job is to understand their "
        "situation, then help them build or improve a diversified portfolio that fits it.",

        "Keep it educational. Explain your reasoning in plain language, tie suggestions to "
        "their stated goals and risk tolerance, and prefer categories of investment (broad "
        "index funds, bond funds, international exposure, and so on) over single-stock picks. "
        "You may name specific funds or tickers as examples, but frame them as options to "
        "research, not instructions to buy. You are not a licensed financial advisor, and "
        "this isn't personalized financial advice - say so briefly when you make "
        "recommendations, without repeating it in every message.",

        "## Their profile\n" + ("\n".join(known) if known else "Nothing saved yet."),
    ]
    if missing:
        parts.append(
            "Still unknown: " + ", ".join(PROFILE_FIELDS[f] for f in missing) + ". "
            "Before giving portfolio recommendations, ask about these conversationally, one "
            "or two at a time. You can still answer a direct general question first."
        )
    parts += [
        "Whenever they tell you something that belongs in their profile, call the "
        "update_investor_profile tool so it's saved for next time.",

        "## Their current holdings\n" + summary,

        "When reviewing holdings, look for: any single position above "
        f"{CONCENTRATION_PCT:.0f}% of the portfolio; funds that overlap heavily in what they "
        "hold; sector concentration; overall risk (beta, asset mix) compared with their risk "
        "tolerance and time horizon; and positions with large losses worth a second look.",

        "## Limits\n"
        "You only see holdings as percentages - no dollar amounts, share counts, or account "
        "names. If a question needs amounts, ask for them. You have no live news or prices "
        "beyond the figures above, and you judge overlap between funds from general knowledge "
        "of what they typically hold, not live holdings data; say so when it matters.",
    ]
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# talking to the model
# --------------------------------------------------------------------------- #
def stream_reply(client, history: list, system: str, on_profile_update):
    """Yield the assistant's reply as text chunks. `history` is the API
    message list and is extended in place (assistant turns, tool results).
    `on_profile_update(fields)` is called with validated profile fields
    whenever the model uses the tool."""
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=history,
                tools=[PROFILE_TOOL],
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
            ) as stream:
                for event in stream:
                    if event.type == "text":
                        yield event.text
                message = stream.get_final_message()
        except ValueError:
            # tool input the SDK couldn't parse at all
            yield "\n\n(Something went wrong saving your profile - please try again.)"
            return

        history.append({"role": "assistant", "content": message.content})

        if message.stop_reason == "refusal":
            yield REFUSAL_TEXT
            return
        tool_uses = [b for b in message.content if b.type == "tool_use"]
        if message.stop_reason != "tool_use" or not tool_uses:
            return

        results = []
        for block in tool_uses:
            fields, error = validate_profile_input(block.input)
            if error:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "is_error": True, "content": error})
            else:
                on_profile_update(fields)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Saved to their profile."})
        history.append({"role": "user", "content": results})
