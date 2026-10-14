"""Shared structural routing for the production graph.

An explicit selection is an edit. Otherwise Architect distinguishes edits,
new parts and assemblies using the instruction, design context and images.
An attachment alone does not establish whether the user wants a new part.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cadenza_backend.contracts_bridge import Ledger


class AgentKind(str, Enum):
    ARCHITECT = "architect"
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
    if has_target:
        return Route(
            AgentKind.MACHINIST,
            "The user clicked a feature, so this edits the existing part.",
        )

    return Route(
        AgentKind.ARCHITECT,
        "Interpreting the instruction and attached drawing in the current design." if has_images
        else "Interpreting the instruction in the current design.",
    )
