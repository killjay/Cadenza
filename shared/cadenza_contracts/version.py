"""Contract version. Bump on any breaking change to ledger / messages / geometry.

The frontend sends its compiled-in value in `client.hello`; the backend refuses
the connection on a MAJOR mismatch (`PROTOCOL_MISMATCH`). During the prototype,
minor bumps must be additive-only so an older client keeps working.
"""

from __future__ import annotations

CONTRACT_VERSION = "1.0.0"


def major(version: str) -> int:
    return int(version.split(".", 1)[0])


def compatible(client_version: str) -> bool:
    """True when a client may connect. Major must match; minor/patch are free."""
    try:
        return major(client_version) == major(CONTRACT_VERSION)
    except (ValueError, IndexError):
        return False
