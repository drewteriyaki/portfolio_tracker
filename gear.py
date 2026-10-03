"""Milestones and gear (ROADMAP T3): the expedition's rewards. Pure logic, no
Streamlit.

Each piece of gear is earned by learning or a steady habit - never by
trading more, taking more risk or chasing returns - and nothing is ever
lost or for sale. Whether one is earned is worked out from what the app
already knows (the profile, the plan, Get started, holdings, money added,
value history); only which ones a person has already been shown (and
when) is kept, in their settings, so the "milestone reached" window
appears once each. Every piece says what it's for (FOR), how it's earned
(HOW - the real rule below, in plain words) and why it matters (WHY).

The icons are line drawings (24 x 24, 1.8 stroke, the design system's
"Milestones and gear"). Streamlit's st.html strips inline SVG, so each is
an image, one per theme (the app's styles show the one that matches).
"""

from __future__ import annotations

import base64
from datetime import date
from html import escape

# key, name, the milestone's title, the one thing to do next to earn it, the
# region (route.REGIONS), and the icon's SVG paths. What each is for, how it's
# earned and why it matters are in FOR, HOW and WHY below.
GEAR = (
    ("map", "Map", "Base camp: you know where you're starting",
     "Answer the questions about you on Learn.", "Base camp",
     ('<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z"/>', '<path d="M9 4v14"/>',
      '<path d="M15 6v14"/>')),
    ("compass", "Compass", "First camp: your goal is set",
     "Set a goal with an amount and a date on Learn.", "The foothills",
     ('<circle cx="12" cy="12" r="9"/>', '<path d="M15.5 8.5l-2 5-5 2 2-5z"/>')),
    ("tent", "Tent", "Learner's ridge: the basics, learned",
     "Read the basics on Learn, then complete that step.", "Learner's ridge",
     ('<path d="M4 19c0-5 3.5-9 8-9s8 4 8 9"/>', '<path d="M12 10V4"/>',
      '<path d="M8 19l4-9 4 9"/>')),
    ("rope", "Rope", "The practice range: tried with practice money",
     "Try practice money on Learn, then complete that step.", "The practice range",
     ('<circle cx="12" cy="12" r="4"/>', '<circle cx="12" cy="12" r="8"/>',
      '<path d="M12 4V3"/>', '<path d="M12 21v-1"/>')),
    ("boots", "Boots", "On the trail: your own holdings are in",
     "Bring in your holdings from your brokerage.", "On the trail",
     ('<path d="M7 3h5v9l6 3a2 2 0 0 1 1 1.7V19H5V3z"/>', '<path d="M5 15h14"/>')),
    ("lantern", "Lantern", "A steady pace: money added three months running",
     "Log money you add on Plan - any amount, three months in a row.", None,
     ('<path d="M9 6h6"/>', '<path d="M10 3h4v3h-4z"/>',
      '<rect x="7" y="6" width="10" height="13" rx="3"/>', '<path d="M12 10v5"/>')),
    ("cloak", "Storm cloak", "Storm weathered: you held steady through a drop",
     "Nothing to do now - it comes if the market has a rough patch and you stay in.", None,
     ('<path d="M3 15h18"/>', '<path d="M6 15c0-4 2.7-7 6-7s6 3 6 7"/>',
      '<path d="M12 4v2"/>', '<path d="M5 9l1.5 1"/>', '<path d="M19 9l-1.5 1"/>')),
    ("flag", "Summit flag", "The summit: you reached your goal",
     "Keep going toward your goal - see where you are on Plan.", "The summit",
     ('<path d="M5 21V4"/>', '<path d="M5 4h11l-2 4 2 4H5"/>')),
)
KEYS = tuple(g[0] for g in GEAR)
BY_KEY = {g[0]: g for g in GEAR}
STORM_DROP_PCT = 10.0
STREAK_MONTHS = 3
_NUMBER_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}

# the fact (earned()'s `facts`) that earns each piece
NEED = {"map": "profile_done", "compass": "goal_set", "tent": "basics_done",
        "rope": "practice_done", "boots": "statement_in", "lantern": "steady",
        "cloak": "storm", "flag": "goal_reached"}

# What each piece is for, in one short line - shown wherever it appears.
FOR = {
    "map": "For knowing where you're starting from: your time, your comfort with ups "
           "and downs, and your safety net.",
    "compass": "For knowing what you're investing for and roughly when you'll need it.",
    "tent": "For learning the basics: funds, spreading out, time, fees, ups and downs, "
            "and kinds of accounts.",
    "rope": "For trying a mix with practice money and real past prices, before any "
            "real money.",
    "boots": "For starting to follow your own investments here.",
    "lantern": "For a steady habit of adding money - any amount counts.",
    "cloak": "For holding steady through a market drop instead of selling.",
    "flag": "For reaching the goal you set.",
}

# How each is earned: the rule earned() and the facts behind it really use
# (views/kit.py _read_gear_facts), in plain words - a test keeps them in step.
HOW = {
    "map": "Earned when you've answered all the questions about you (About you, on Learn).",
    "compass": ("Earned when you set a goal with an amount and a date and walk through "
                "\"Set a goal\" on Learn."),
    "tent": "Earned when you complete \"Learn the basics\" on Learn.",
    "rope": "Earned when you complete \"Try it with practice money\" on Learn.",
    "boots": "Earned when you bring in your own holdings from any brokerage (the example "
             "portfolio doesn't count).",
    "lantern": (f"Earned when you add money in {_NUMBER_WORDS[STREAK_MONTHS]} calendar "
                "months in a row, up to this month or last - logged on Plan or in your "
                "brokerage's activity."),
    "cloak": (f"Earned when your portfolio falls {STORM_DROP_PCT:.0f}% or more below its "
              "high and you don't sell anything between the high and the low."),
    "flag": "Earned when your holdings reach your goal's amount (the example portfolio "
            "doesn't count).",
}

# Why it matters, in one sentence - the "milestone reached" window.
WHY = {
    "map": "Your time, your comfort with ups and downs and your safety net are what the "
           "rest of your route is built on.",
    "compass": "A goal with a date gives every later choice a direction - how much up and "
               "down makes sense, and how long you have.",
    "tent": "These few ideas explain most of what you'll see as an investor, so less of "
            "it comes as a surprise.",
    "rope": "Watching a mix rise and fall with practice money first makes the real ups "
            "and downs easier to sit through.",
    "boots": "With your own holdings here, you can see where you are on your route "
             "instead of guessing.",
    "lantern": "Adding regularly, whatever the amount, is one of the habits that matters "
               "most over the years.",
    "cloak": "Selling in a drop turns a fall on paper into a real loss; staying in gives "
             "your investments the chance to recover.",
    "flag": "You set a goal and stayed with it until you got there - a good moment to "
            "enjoy, then pick the next one.",
}

# Where to go to earn it: (button label, ("learn", Get started waypoint) |
# ("page", page) | ("dialog", holdings dialog)). The storm cloak has none -
# there's nothing to do but stay in.
GO = {
    "map": ("Answer the questions", ("learn", "profile")),
    "compass": ("Set a goal", ("learn", "goal")),
    "tent": ("Learn the basics", ("learn", "basics")),
    "rope": ("Try practice money", ("learn", "practice")),
    "boots": ("Bring in holdings", ("dialog", "manual")),
    "lantern": ("Log money added", ("page", "Plan")),
    "flag": ("See your plan", ("page", "Plan")),
}

# per theme: earned (dawn on dawn-soft) and not yet (line-strong) - the design system's
COLORS = {"light": {"earned": "#a16207", "todo": "#74838f"},
          "dark": {"earned": "#f2bd57", "todo": "#62748a"}}


def _month_index(d: str) -> int:
    return int(d[:4]) * 12 + int(d[5:7]) - 1


def steady_months(dates_added: list[str], today: date) -> bool:
    """Money added in each of STREAK_MONTHS calendar months in a row, ending
    this month or last (`dates_added`: the dates of the additions)."""
    have = {_month_index(d) for d in dates_added if d and len(d) >= 7}
    now = today.year * 12 + today.month - 1
    for end in (now, now - 1):
        if all(end - k in have for k in range(STREAK_MONTHS)):
            return True
    return False


def weathered_storm(values: list[tuple[str, float]], sells: list[str]) -> bool:
    """Held steady through a drop: the portfolio's value (`values`: (date,
    value), any order) fell STORM_DROP_PCT or more below its high, and
    nothing was sold (`sells`: dates) between that high and the low."""
    peak_d, peak = None, None
    worst = None   # (drop %, peak date, low date)
    for d, v in sorted(values):
        if v is None or v <= 0:
            continue
        if peak is None or v > peak:
            peak_d, peak = d, v
            continue
        drop = (peak - v) / peak * 100
        if drop >= STORM_DROP_PCT and (worst is None or drop > worst[0]):
            worst = (drop, peak_d, d)
    if worst is None:
        return False
    _, high, low = worst
    return not any(high[:10] <= (s or "")[:10] <= low[:10] for s in sells)


def kit_keys(managed: bool = False) -> tuple[str, ...]:
    """The pieces in this person's kit. An advisor's client has no practice
    money on Learn (its example funds could cross their advisor's advice), so
    no rope; the rest - learning and habits - are theirs too."""
    return tuple(k for k in KEYS if not (managed and k in NOT_FOR_CLIENTS))


NOT_FOR_CLIENTS = ("rope",)


def earned(facts: dict, keys: tuple[str, ...] = KEYS) -> list[str]:
    """The keys of the gear earned, in kit order. `facts`: profile_done,
    goal_set, basics_done, practice_done, statement_in, steady, storm,
    goal_reached (booleans). `keys`: the kit (kit_keys())."""
    return [k for k in keys if facts.get(NEED[k])]


def next_up(earned_keys: list[str], keys: tuple[str, ...] = KEYS) -> str | None:
    """The next piece to point to: the first not earned that there's something
    to do for (the storm cloak just comes, so it waits its turn behind them),
    or None when the kit is full."""
    todo = [k for k in keys if k not in earned_keys]
    return next((k for k in todo if k in GO), todo[0] if todo else None)


def stamp(dates: dict | None, earned_keys: list[str], fresh: list[str],
          today: str) -> dict:
    """When each piece was earned, for the kit window: {key: {"on": date}} for
    one celebrated today (`fresh`), {"by": date} for one already earned before
    dates were kept (it was earned on that day or earlier). Kept once set."""
    out = dict(dates or {})
    for k in earned_keys:
        if k not in out:
            out[k] = {"on" if k in fresh else "by": today}
    return out


def when_text(entry: dict | None, fmt) -> str:
    """'Earned Oct 2, 2026', 'Earned by Oct 2, 2026', or 'Earned' (`fmt`
    turns an ISO date into words)."""
    if entry and entry.get("on"):
        return f"Earned {fmt(entry['on'])}"
    if entry and entry.get("by"):
        return f"Earned by {fmt(entry['by'])}"
    return "Earned"


def new_since(earned_keys: list[str], seen: list[str] | None) -> tuple[list[str], list[str]]:
    """(gear to celebrate now, the seen list to save). With no seen list yet
    (an account from before gear existed) everything earned so far is taken
    as seen, quietly - no flood of windows on the first visit."""
    if seen is None:
        return [], list(earned_keys)
    fresh = [k for k in earned_keys if k not in seen]
    return fresh, list(seen) + fresh


def icon_html(key: str, is_earned: bool, size: int = 24) -> str:
    """The gear's icon as two images, one per theme, its name as their text."""
    paths = "".join(BY_KEY[key][5])
    name = escape(BY_KEY[key][1] + ("" if is_earned else ", not earned yet"), quote=True)
    out = []
    for theme, c in COLORS.items():
        color = c["earned"] if is_earned else c["todo"]
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{size}" '
               f'height="{size}" fill="none" stroke="{color}" stroke-width="1.8" '
               f'stroke-linecap="round" stroke-linejoin="round">{paths}</svg>')
        data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        out.append(f"<img class='pt-gear-icon pt-on-{theme}' width='{size}' height='{size}' "
                   f"alt='{name}' src='data:image/svg+xml;base64,{data}'>")
    return "".join(out)
