"""Rule-based tagging of team-radio transcripts.

Each message gets a category and a direction for the *target* driver's win
probability (-1 bad news, +1 good news, 0 unclear). The target is the speaker
unless the message names another driver ("Leclerc has stopped").

Rules are deliberately conservative: a backtest that only trades messages
with an unambiguous reading is the honest version of this strategy. Review
the tags by hand before trusting any result; `direction_override` in the
transcripts file wins over the rules.
"""
from __future__ import annotations

import re

import pandas as pd

# (category, direction for target, pattern). First match wins, so the most
# decisive categories come first.
RULES: list[tuple[str, int, str]] = [
    ("retirement", -1, r"\b(retire|stop the car|park (it|the car)|pull over|box to retire|we('re| are) done)\b"),
    ("failure", -1, r"\b(lost|losing|no) (power|drive|gears?|brakes?|hydraulics|engine)\b|\b(gearbox|engine|mgu|"
                    r"battery|hydraulic|hydraulics|brake) (problem|issue|failure|gone|fault)\b|\bsmoke\b|\bfire\b"),
    ("damage", -1, r"\b(puncture|damage|damaged|front wing|floor is|broken|something broke|hit (the )?wall)\b"),
    ("problem", -1, r"\b(problem|issue|overheating|vibration|can'?t (see|change|shift)|losing (time|pace))\b"),
    ("tyres_gone", -1, r"\b(tyres? (are|is) (gone|dead|finished)|no grip|graining|degradation is)\b"),
    ("penalty", -1, r"\b(penalty|investigation|under investigation|time penalty|drive.?through)\b"),
    ("team_order", 0, r"\b(let (him|them|\w+) (by|through|past)|hold position|swap|don'?t (fight|attack))\b"),
    ("pit_call", 0, r"\b(box,? box|box this lap|box now|stay out|plan [a-f]\b)"),
]
_COMPILED = [(c, d, re.compile(p, re.I)) for c, d, p in RULES]


def classify_text(text: str) -> tuple[str, int]:
    for cat, direction, pat in _COMPILED:
        if pat.search(text or ""):
            return cat, direction
    return "other", 0


def mentioned_driver(text: str, drivers: pd.DataFrame, speaker: int) -> int | None:
    """Another driver named in the message, if exactly one is."""
    hits = set()
    for _, d in drivers.iterrows():
        if int(d["driver_number"]) == speaker or not d["last_name"]:
            continue
        if re.search(rf"\b{re.escape(d['last_name'])}\b", text or "", re.I):
            hits.add(int(d["driver_number"]))
    return hits.pop() if len(hits) == 1 else None


def tag(radio: pd.DataFrame, drivers: pd.DataFrame) -> pd.DataFrame:
    """Add category, direction and target_driver to a radio table with a `text` column."""
    out = radio.copy()
    cats, dirs, targets = [], [], []
    for _, r in out.iterrows():
        text = r.get("text") if isinstance(r.get("text"), str) else ""
        cat, direction = classify_text(text)
        other = mentioned_driver(text, drivers, int(r["driver_number"]))
        # "Hamilton has a problem" is bad news for Hamilton, not for the speaker.
        targets.append(other if other is not None and direction != 0 else int(r["driver_number"]))
        cats.append(cat)
        dirs.append(direction)
    out["category"], out["direction"], out["target_driver"] = cats, dirs, targets
    if "direction_override" in out:
        ov = pd.to_numeric(out["direction_override"], errors="coerce")
        out["direction"] = ov.fillna(out["direction"]).astype(int)
    if "target_override" in out:
        tv = pd.to_numeric(out["target_override"], errors="coerce")
        out["target_driver"] = tv.fillna(out["target_driver"]).astype(int)
    return out
