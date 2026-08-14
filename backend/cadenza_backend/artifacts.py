"""In-memory blob store for built geometry. `WORKSTREAMS.md` names this file.

Two consumers, and they want different things:

  * The **viewport** wants the bytes as fast as possible and gets them pushed
    as binary WebSocket frames the moment the build finishes. It never fetches.
  * The **download button** (and anything debugging by hand with curl) wants a
    URL. `GET /artifacts/{revision}/model.{glb,step}` serves from here.

Only the last few revisions are kept. A GLB/STEP pair for a milestone-1 part is
tens of kilobytes, but the ceiling has to exist or a long session is an
unbounded leak — and nothing legitimately asks for a revision five edits back,
because the client is showing the current one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

KEEP_REVISIONS = 5


@dataclass(frozen=True)
class Artifact:
    revision: int
    kind: str  # "glb" | "step"
    content_type: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def url_for(self, session_id: str) -> str:
        """Session-scoped: revision numbers are only meaningful within a ledger."""
        return f"/sessions/{session_id}/artifacts/{self.revision}/model.{self.kind}"


class ArtifactStore:
    """Per-session. Keyed by `(revision, kind)`, pruned to the newest revisions."""

    def __init__(self, keep: int = KEEP_REVISIONS) -> None:
        self._items: dict[tuple[int, str], Artifact] = {}
        self._keep = max(1, keep)

    def put(self, revision: int, kind: str, content_type: str, data: bytes) -> Artifact:
        artifact = Artifact(revision=revision, kind=kind, content_type=content_type, data=data)
        self._items[(revision, kind)] = artifact
        self._prune()
        return artifact

    def get(self, revision: int, kind: str) -> Artifact | None:
        return self._items.get((revision, kind))

    def _prune(self) -> None:
        revisions = sorted({rev for rev, _ in self._items}, reverse=True)
        doomed = set(revisions[self._keep :])
        for key in [k for k in self._items if k[0] in doomed]:
            self._items.pop(key, None)

    def __len__(self) -> int:
        return len(self._items)
