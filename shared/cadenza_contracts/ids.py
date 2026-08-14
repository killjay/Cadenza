"""ID minting. One place, so IDs are greppable and prefix-stable."""

from __future__ import annotations

import re
import secrets

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _token(n: int = 8) -> str:
    return secrets.token_hex(n // 2)


def new_project_id() -> str:
    return f"prj_{_token(12)}"


def new_session_id() -> str:
    return f"ses_{_token(12)}"


def new_message_id() -> str:
    """Correlation id. The client mints it; every server frame answering that
    turn echoes it back in `message_id`."""
    return f"msg_{_token(12)}"


def new_feature_id(hint: str = "feature") -> str:
    """`feat_<slug>_<rand4>` — stable prefix, human-readable middle.

    The slug comes from the agent's own naming ("base plate" ->
    `feat_base_plate_9f2a`). Uniqueness comes from the suffix, never from the
    hint, so two features called "hole" never collide.
    """
    slug = _SLUG_RE.sub("_", hint.strip().lower()).strip("_") or "feature"
    return f"feat_{slug[:32]}_{_token(4)}"
