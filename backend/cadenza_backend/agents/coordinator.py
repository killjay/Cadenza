"""The Coordinator — blueprint §3. The router.

"Evaluates incoming payloads (image upload vs. text prompt vs. Point & Speak
patch) and routes to the correct agent below."

Implemented with rules rather than a model call. The blueprint lists the three
inputs it routes on, and all three are *structural* facts the transport already
knows for certain: is there an image attached, does the ledger have features
yet, did the payload carry a click coordinate. Asking a model to re-derive
facts the request object already states is latency and spend with a failure mode
attached — the routing table below cannot hallucinate.

The seam is real, though: `route()` returns a decision object, so swapping in a
model call for the genuinely ambiguous case (a text prompt against a non-empty
ledger that might be an edit *or* a new part) is a change to one function.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cadenza_backend.contracts_bridge import Ledger


class AgentKind(str, Enum):
    DRAFTSMAN = "draftsman"
    MACHINIST = "machinist"


@dataclass(frozen=True)
class Route:
    agent: AgentKind
    reason: str
    """Human-readable, surfaced in the status stream so routing is never a mystery."""


def route(
    ledger: Ledger,
    prompt: str,
    *,
    has_target: bool = False,
    has_images: bool = False,
) -> Route:
    """Pick the agent for this payload."""
    live_features = [f for f in ledger.features if not f.suppressed]

    if has_images:
        return Route(
            AgentKind.DRAFTSMAN,
            "An image was uploaded, so the Draftsman reads the sketch.",
        )

    if not live_features:
        return Route(
            AgentKind.DRAFTSMAN,
            "The part is empty, so this describes something to create.",
        )

    if has_target:
        return Route(
            AgentKind.MACHINIST,
            "The user clicked a feature, so this edits the existing part.",
        )

    # Text against an existing part with no click. Treated as an edit: the user
    # is looking at a part and talking about it. Adding a feature is expressible
    # as a patch appending to /features/-, so the Machinist covers this case too.
    return Route(
        AgentKind.MACHINIST,
        "The part already exists, so this is an edit to it.",
    )
