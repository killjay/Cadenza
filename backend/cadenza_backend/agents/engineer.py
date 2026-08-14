"""The Engineer — blueprint §3. The smart-defaults database.

"Translates layman terms ('fits a wall screw', 'ergonomic grip') into strict
mathematical parameters (radius=4.5, fillet=5)."

Implemented as a lookup table, not a model call, and that is a deliberate
choice rather than a shortcut. The whole value of this layer is that
"fits an M4 screw" resolves to 4.5 mm *every single time* — a clearance fit is a
table lookup in the real world (ISO 273 medium), so making it a table lookup
here is the faithful implementation, not an approximation of one. A model asked
the same question will happily answer 4.3 one day and 4.6 the next, and the user
has no way to see that it drifted.

So: the table answers what the table knows, and anything it does not know falls
through to the Draftsman or Machinist, which must then record an `Assumption`
the UI can badge. The seam for an LLM fallback is `hints_for()` — add it there
if the table's coverage becomes the bottleneck.

Values below are nominal engineering defaults for a prototype, not a certified
standards database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SmartDefault:
    """One resolved layman term."""

    term: str
    field: str
    value: float | str
    basis: str
    confidence: float

    def as_assumption(self) -> dict:
        return {
            "field": self.field,
            "value": self.value,
            "basis": f"{self.basis} (matched \"{self.term}\")",
            "confidence": self.confidence,
        }


# Clearance holes, ISO 273 "medium" series — the diameter you drill so a screw
# of that size passes through without threading.
_CLEARANCE_MM = {
    "m2": 2.4,
    "m2.5": 2.9,
    "m3": 3.4,
    "m4": 4.5,
    "m5": 5.5,
    "m6": 6.6,
    "m8": 9.0,
    "m10": 11.0,
    "m12": 14.0,
}

SMART_DEFAULTS: tuple[SmartDefault, ...] = (
    SmartDefault(
        "wall screw",
        "hole.diameter",
        4.5,
        "Clearance hole for an M4 screw, ISO 273 medium series",
        0.7,
    ),
    SmartDefault(
        "wall plug",
        "hole.diameter",
        6.0,
        "Standard 6 mm brown wall plug",
        0.7,
    ),
    SmartDefault(
        "countersunk screw",
        "hole.diameter",
        4.5,
        "Clearance hole for an M4 countersunk screw, ISO 273 medium series",
        0.6,
    ),
    SmartDefault(
        "ergonomic grip",
        "cylinder.diameter",
        32.0,
        "Comfortable power-grip diameter for an adult hand (30-35 mm)",
        0.5,
    ),
    SmartDefault(
        "finger hole",
        "hole.diameter",
        22.0,
        "Clears an adult index finger with room to spare",
        0.5,
    ),
    SmartDefault(
        "3d print",
        "wall.thickness",
        2.0,
        "Minimum robust wall for FDM at a 0.4 mm nozzle (5 perimeters)",
        0.6,
    ),
    SmartDefault(
        "sturdy",
        "wall.thickness",
        4.0,
        "Doubled nominal wall for a part described as load-bearing",
        0.4,
    ),
    SmartDefault(
        "cable pass",
        "hole.diameter",
        8.0,
        "Passes a typical moulded power cable plus strain relief",
        0.5,
    ),
)

_METRIC_SCREW_RE = re.compile(r"\bM(2|2\.5|3|4|5|6|8|10|12)\b", re.IGNORECASE)


def hints_for(text: str) -> list[SmartDefault]:
    """Resolve every layman term the table recognises in `text`.

    Longest match wins where terms overlap, so "countersunk screw" is not
    shadowed by a bare "screw".
    """
    lowered = text.lower()
    hits: list[SmartDefault] = []

    for entry in sorted(SMART_DEFAULTS, key=lambda d: -len(d.term)):
        if entry.term in lowered and not any(entry.term in h.term for h in hits):
            hits.append(entry)

    # An explicit "M4" beats any fuzzy phrase match — the user named the size.
    for match in _METRIC_SCREW_RE.finditer(text):
        size = f"m{match.group(1).lower()}"
        diameter = _CLEARANCE_MM.get(size)
        if diameter is None:
            continue
        hits.insert(
            0,
            SmartDefault(
                match.group(0),
                "hole.diameter",
                diameter,
                f"Clearance hole for {match.group(0).upper()}, ISO 273 medium series",
                0.9,
            ),
        )

    return hits


def format_hints(hints: list[SmartDefault]) -> str:
    """Render hints for injection into an agent's user message."""
    if not hints:
        return ""
    lines = [
        f'  - "{h.term}" -> {h.field} = {h.value} mm  ({h.basis})' for h in hints
    ]
    return (
        "SMART DEFAULTS (resolved by the Engineer; use these numbers unless the user "
        "gave an explicit dimension, and carry them into `assumptions`):\n"
        + "\n".join(lines)
    )
