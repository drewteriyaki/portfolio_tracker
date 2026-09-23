"""Rule-based position alerts.

A rule is data: a registry metric plus a threshold. `evaluate()` walks every
position, reads the metric from `metrics.py`, and fires when it goes past the
limit. Pure and cheap, so the dashboard just calls it on every page load - no
scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass

import metrics as M

# Each rule: key (stable id), label, the metrics.py metric to read, and the
# limit. `abs_gt` fires when |value| exceeds the limit (i.e. beyond +/-limit).
DEFAULT_RULES = [
    {"key": "day_move", "label": "Day move", "metric": "day_change_pct", "abs_gt": 5.0},
    {"key": "total_gl", "label": "Total gain/loss", "metric": "unrealized_pct", "abs_gt": 20.0},
]


@dataclass(frozen=True)
class Alert:
    rule_key: str
    rule_label: str
    symbol: str
    account: str
    metric_key: str
    value: float
    threshold: float
    direction: str  # "up" | "down"
    is_pct: bool = True

    @property
    def message(self) -> str:
        arrow = "▲" if self.direction == "up" else "▼"
        shown = f"{self.value:+.2f}%" if self.is_pct else f"{self.value:+,.2f}"
        lim = f"{self.threshold:g}%" if self.is_pct else f"{self.threshold:g}"
        return (f"{arrow}  **{self.symbol}** ({self.account}) — "
                f"{self.rule_label} {shown}  ·  limit ±{lim}")

    @property
    def masked_message(self) -> str:
        """`message` with the number hidden (privacy mode)."""
        arrow = "▲" if self.direction == "up" else "▼"
        return f"{arrow}  **{self.symbol}** ({self.account}) — {self.rule_label} past its limit"


def evaluate(contexts, rules=None) -> list[Alert]:
    """contexts: iterable of metric-context dicts (pos / quote / port_value /
    acct_value). Returns Alerts, worst (largest magnitude) first."""
    rules = rules if rules is not None else DEFAULT_RULES
    fired: list[Alert] = []
    for rule in rules:
        mk = rule["metric"]
        limit = float(rule.get("abs_gt") or 0.0)
        if not limit:
            continue
        is_pct = (M.BY_KEY[mk].fmt == "pct") if mk in M.BY_KEY else True
        for ctx in contexts:
            raw = M.value(mk, ctx)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if abs(v) > limit:
                pos = ctx.get("pos") or {}
                fired.append(Alert(
                    rule["key"], rule.get("label", mk), pos.get("symbol"),
                    pos.get("account"), mk, v, limit,
                    "up" if v >= 0 else "down", is_pct,
                ))
    fired.sort(key=lambda a: abs(a.value), reverse=True)
    return fired
