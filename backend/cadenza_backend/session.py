"""Session management. One connected client, one ledger.

In-memory and single-process, which is the right scope for a prototype and the
obvious seam for Redis later: `SessionRegistry` is the only thing that knows
where sessions live.

Sessions outlive their WebSocket on purpose — a dropped connection should not
destroy the part the user is working on, so a client that reconnects with a
known session id gets its ledger back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from cadenza_backend.artifacts import ArtifactStore
from cadenza_backend.config import get_settings
from cadenza_backend.contracts_bridge import new_session_id
from cadenza_backend.ledger_store import LedgerStore


@dataclass
class Session:
    id: str = field(default_factory=new_session_id)
    store: LedgerStore = field(default_factory=LedgerStore.new)
    artifacts: ArtifactStore = field(default_factory=ArtifactStore)
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.last_seen


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._settings = get_settings()

    def create(self, name: str = "Untitled Part") -> Session:
        self._evict_expired()
        if len(self._sessions) >= self._settings.max_sessions:
            # Drop the least recently used rather than refusing the new client.
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
            self._sessions.pop(oldest.id, None)
        session = Session(store=LedgerStore.new(name))
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session:
            session.touch()
        return session

    def get_or_create(self, session_id: str | None) -> Session:
        if session_id:
            existing = self.get(session_id)
            if existing:
                return existing
        return self.create()

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _evict_expired(self) -> None:
        ttl = self._settings.session_ttl_seconds
        for sid in [s.id for s in self._sessions.values() if s.age_seconds > ttl]:
            self._sessions.pop(sid, None)

    def __len__(self) -> int:
        return len(self._sessions)


registry = SessionRegistry()
