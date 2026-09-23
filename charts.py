"""Shared time-series chart for the dashboard.

A gridded Altair line with a hover rule that snaps to the nearest date, plus
helpers for the "last N days" range picker and the change-over-window figure.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

# (label, days back). None = everything on record.
RANGES = [("1D", 1), ("5D", 5), ("2W", 14), ("1M", 30), ("6M", 182), ("1Y", 365), ("All", None)]
RANGE_DAYS = dict(RANGES)
RANGE_LABELS = [label for label, _ in RANGES]


def clip_range(df: pd.DataFrame, tcol: str, days: int | None) -> pd.DataFrame:
    """Rows within `days` of the most recent point. `days=None` returns all."""
    if days is None or df.empty:
        return df
    cutoff = df[tcol].max() - pd.to_timedelta(int(days), unit="D")
    return df[df[tcol] >= cutoff]


def window(df: pd.DataFrame, tcol: str, days: int | None, *, min_points: int = 2):
    """Clip to the last `days`. If that leaves fewer than `min_points` rows
    (e.g. "1D" against once-a-day bars), fall back to the last `min_points` rows
    — never the whole history. Returns (rows, fell_back)."""
    if df.empty or days is None:
        return df, False
    clipped = clip_range(df, tcol, days)
    if len(clipped) >= min_points:
        return clipped, False
    return df.sort_values(tcol).tail(min_points), True


def window_change(df: pd.DataFrame, tcol: str, ycol: str):
    """(first, last, pct_change) across the rows given (already range-clipped)."""
    s = df.dropna(subset=[ycol]).sort_values(tcol)
    if s.empty:
        return None, None, None
    first, last = float(s[ycol].iloc[0]), float(s[ycol].iloc[-1])
    pct = ((last - first) / first * 100.0) if first else None
    return first, last, pct


# moving-average overlay styling: window -> (colour, dash pattern)
MA_STYLE = {20: ("#f59e0b", [1, 0]), 50: ("#a78bfa", [5, 3]), 200: ("#64748b", [2, 2])}


def line(df: pd.DataFrame, *, x: str, y: str, y_title: str, y_format: str,
         color: str | None = None, color_scale=None, line_color: str | None = None,
         tooltip=None, overlays=None, mask: bool = False, compress_gaps: bool = False,
         height: int = 320):
    """Layered chart: a smooth gridded line (no permanent point markers), a
    nearest-date hover rule, and an emphasised dot that only appears at the
    hovered date. `tooltip` is a list of alt.Tooltip. `overlays` is a list of
    (column, colour, dash) drawn as thin dashed lines. `mask=True` hides the
    y-axis tick labels (privacy mode). `line_color` sets a single fixed colour
    for the whole line (use this, not `color`, when there's only one series —
    `color`/`color_scale` are for colouring by a categorical column instead.

    `compress_gaps=True` spaces points evenly by *sequence* instead of by
    elapsed time, so market-closed stretches (overnight, weekends) don't
    stretch the chart out of proportion to how much trading actually
    happened — the same trick real stock-chart tools use for intraday data.
    `x` must already be sorted ascending; only meaningful for intraday
    resolutions (daily bars have no large gaps to compress).

    `compress_gaps=True` spaces points evenly by *sequence* instead of by
    elapsed time, so market-closed stretches (overnight, weekends) don't
    stretch the chart out of proportion to how much trading actually
    happened — the same trick real stock-chart tools use for intraday data.
    `x` must already be sorted ascending; only meaningful for intraday
    resolutions (daily bars have no large gaps to compress).
    """
    df = df.sort_values(x).reset_index(drop=True)
    grid = dict(grid=True, gridOpacity=0.25, gridDash=[2, 2])

    if compress_gaps:
        df = df.copy()
        # Include the year only if the window actually spans more than one -
        # otherwise it's dead weight on every label (1D/5D/1M never cross a
        # year boundary; 1Y/All sometimes do, and "Apr 01" / "Feb 17" with no
        # year looks like the axis is out of order when it's really just Apr
        # of one year followed by Feb of the next).
        fmt = ("%b %d %H:%M" if df[x].dt.year.nunique() <= 1 else "%b %d '%y %H:%M")
        df["_x"] = df[x].dt.strftime(fmt)  # unique per bar at our finest (1-min) resolution
        x_field, x_type = "_x", "O"
        x_axis = alt.Axis(labelAngle=-40, **grid)
        x_sort = df["_x"].tolist()  # explicit chronological order (row order), not alphabetical
    else:
        x_field, x_type = x, "T"
        x_axis = alt.Axis(**grid)
        x_sort = "ascending"

    x_key = f"{x_field}:{x_type}"
    enc = {
        "x": alt.X(x_key, title=None, sort=x_sort, axis=x_axis),
        "y": alt.Y(f"{y}:Q", title=y_title, scale=alt.Scale(zero=False),
                   axis=alt.Axis(format=y_format, labels=not mask, **grid)),
    }
    if color is not None:
        enc["color"] = (alt.Color(f"{color}:N", title="Point source", scale=color_scale)
                        if color_scale is not None else alt.Color(f"{color}:N", title=None))
    elif line_color is not None:
        enc["color"] = alt.value(line_color)

    base = alt.Chart(df).encode(**enc)
    layers = [base.mark_line(point=False, interpolate="monotone")]

    for col, colour, dash in (overlays or []):
        layers.append(alt.Chart(df).mark_line(strokeDash=dash, strokeWidth=1.5, opacity=0.8)
                      .encode(x=alt.X(x_key, sort=x_sort), y=alt.Y(f"{col}:Q"), color=alt.value(colour)))

    nearest = alt.selection_point(nearest=True, on="pointerover", fields=[x_field],
                                  empty=False, clear="pointerout")
    layers.append(alt.Chart(df).mark_rule(color="#94a3b8", strokeWidth=1)
                  .encode(x=alt.X(x_key, sort=x_sort), opacity=alt.condition(nearest, alt.value(0.7), alt.value(0))))
    layers.append(base.mark_point(size=90, filled=True)
                  .encode(opacity=alt.condition(nearest, alt.value(1), alt.value(0))))
    layers.append(alt.Chart(df).mark_rule(strokeWidth=24)
                  .encode(x=alt.X(x_key, sort=x_sort), opacity=alt.value(0),
                          tooltip=tooltip if tooltip is not None else [])
                  .add_params(nearest))

    return alt.layer(*layers).properties(height=height)
