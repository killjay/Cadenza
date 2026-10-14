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
import asyncio
from dataclasses import dataclass, field

import os
from typing import TYPE_CHECKING

from cadenza_backend.artifacts import ArtifactStore
from cadenza_backend.config import get_settings
from cadenza_backend.contracts_bridge import Ledger, new_session_id
from cadenza_backend.ledger_store import LedgerStore

if TYPE_CHECKING:
    from cadenza_backend.part_views import SourceView
    from cadenza_backend.edit_preview import PendingPreview
    from cadenza_backend.sketch_drafts import PendingDraft
    from cadenza_backend.wire import GeometryReady


class SessionLogger:
    def __init__(self, session_id: str):
        self.session_id = session_id

    def log(self, event_type: str, **kwargs) -> None:
        from cadenza_backend.diagnostics import CURRENT, Trace
        trace = CURRENT.get()
        if trace is None or trace.session_id != self.session_id:
            trace = Trace(self.session_id, kwargs.get("message_id"))
        trace.log(event_type, **kwargs)


@dataclass
class Session:
    id: str = field(default_factory=new_session_id)
    primary_ledger_id: str = ""
    stores: dict[str, LedgerStore] = field(default_factory=dict)
    artifacts: ArtifactStore = field(default_factory=ArtifactStore)
    logger: SessionLogger = field(init=False)
    created_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_seen: float = field(default_factory=time.time)
    history: list[dict[str, Ledger]] = field(default_factory=list)
    redo: list[dict[str, Ledger]] = field(default_factory=list)
    project_id: str | None = None
    project_version: int = 0
    project_name: str = "Untitled Part"
    saved_revision: int | None = None
    last_geometry: GeometryReady | None = None
    source_view: SourceView | None = None
    preview: PendingPreview | None = None
    draft: PendingDraft | None = None

    def __post_init__(self):
        self.logger = SessionLogger(self.id)
        if not self.stores:
            store = LedgerStore.new("Untitled Part")
            self.primary_ledger_id = store.ledger.project_id
            self.stores[self.primary_ledger_id] = store

    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def revision(self) -> int:
        """A session-wide accepted build generation, including undo."""
        return self.store.ledger.revision

    def snapshot(self) -> dict[str, Ledger]:
        return {lid: store.ledger.model_copy(deep=True) for lid, store in self.stores.items()}

    def stage(self, updates: dict[str, Ledger]) -> dict[str, Ledger]:
        ledgers = self.snapshot()
        ledgers.update(updates)
        return self._next_generation(ledgers)

    def _next_generation(self, ledgers: dict[str, Ledger]) -> dict[str, Ledger]:
        return {lid: ledger.model_copy(deep=True, update={"revision": self.revision + 1}) for lid, ledger in ledgers.items()}

    def stage_undo(self, target_revision: int | None = None) -> tuple[dict[str, Ledger], int] | None:
        for index in range(len(self.history) - 1, -1, -1):
            snapshot = self.history[index]
            if target_revision is None or snapshot[self.primary_ledger_id].revision == target_revision:
                return self._next_generation(snapshot), index
        return None

    def stage_redo(self) -> dict[str, Ledger] | None:
        return self._next_generation(self.redo[-1]) if self.redo else None

    def commit(self, ledgers: dict[str, Ledger], undo_index: int | None = None, *, redo: bool = False) -> None:
        """Adopt a fully built snapshot without yielding; callers hold `lock`."""
        if self.primary_ledger_id not in ledgers or any(
            ledger.revision != self.revision + 1 or ledger.project_id != lid
            for lid, ledger in ledgers.items()
        ):
            raise ValueError("Candidate does not match the next session generation")
        if redo:
            if not self.redo:
                raise ValueError("Nothing to redo")
            self.history.append(self.snapshot())
            self.redo.pop()
        elif undo_index is None:
            self.history.append(self.snapshot())
            self.redo.clear()
        else:
            self.redo.extend([self.snapshot(), *reversed(self.history[undo_index + 1:])])
            del self.history[undo_index:]
        self.stores = {lid: LedgerStore(ledger=ledger) for lid, ledger in ledgers.items()}
        self.preview = None
        self.source_view = None
        self.draft = None

    @property
    def store(self) -> LedgerStore:
        return self.stores[self.primary_ledger_id]

    @property
    def age_seconds(self) -> float:
        return time.time() - self.last_seen


class SessionRegistry:
    def register(self, session: Session) -> None:
        self._evict_expired()
        if len(self._sessions) >= self._settings.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
            oldest.logger.log("session.evicted", reason="capacity")
            self._sessions.pop(oldest.id, None)
        self._sessions[session.id] = session

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._settings = get_settings()

    def create(self, name: str = "Untitled Part") -> Session:
        self._evict_expired()
        if len(self._sessions) >= self._settings.max_sessions:
            # Drop the least recently used rather than refusing the new client.
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
            oldest.logger.log("session.evicted", reason="capacity")
            self._sessions.pop(oldest.id, None)
        
        store = LedgerStore.new(name)
        session = Session(
            primary_ledger_id=store.ledger.project_id,
            stores={store.ledger.project_id: store}
        )
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
            self._sessions[sid].logger.log("session.evicted", reason="expired")
            self._sessions.pop(sid, None)

    def __len__(self) -> int:
        return len(self._sessions)


registry = SessionRegistry()
