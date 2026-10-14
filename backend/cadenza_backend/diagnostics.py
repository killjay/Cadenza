"""Local, best-effort request evidence. No CAD or provider imports."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import threading
import time
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from cadenza_backend.config import get_settings

RUN_ID = uuid4().hex
CURRENT: ContextVar[Trace | None] = ContextVar("diagnostic_trace", default=None)
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
SECRET = re.compile(r"authorization|api[_-]?key|cookie|password|secret|access[_-]?token", re.I)


def clean(value: Any, depth=0) -> Any:
    if depth > 30: return {"omitted": "nesting limit"}
    if isinstance(value, dict):
        if value.get("type") in ("thinking", "redacted_thinking", "reasoning"):
            return {"omitted": "hidden reasoning"}
        result = {}
        for key, item in list(value.items())[:8192]:
            key = str(key)
            if SECRET.search(key) or key in ("thinking", "reasoning", "reasoning_content", "headers"):
                result[key] = "[redacted]"
            elif key in ("data", "data_base64") and isinstance(item, str) and (value.get("media_type") or value.get("encoding") == "base64" or value.get("type") == "base64"):
                result[key] = {"omitted": "image bytes stored at request boundary", "sha256_base64": hashlib.sha256(item.encode()).hexdigest()}
            else: result[key] = clean(item, depth + 1)
        if len(value) > 8192: result["_omitted_keys"] = len(value) - 8192
        return result
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in value[:8192]] + ([{"omitted": "item limit"}] if len(value) > 8192 else [])
    if isinstance(value, str):
        if value.startswith("data:image/"): return {"omitted": "inline image", "sha256": hashlib.sha256(value.encode()).hexdigest()}
        value = re.sub(r"https?://[^\s\"<>]+", lambda m: m[0].split("?")[0].split("#")[0] if "@" not in m[0] else "[credential URL redacted]", value)
        return value if len(value) <= 1_000_000 else {"omitted": "string limit", "chars": len(value), "sha256": hashlib.sha256(value.encode()).hexdigest()}
    if value is None or type(value) in (bool, int, float): return value
    return {"type": type(value).__name__}


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


class DiagnosticStore:
    def __init__(self, root: Path, *, enabled=True, images=True, session_limit=128*1024**2, total_limit=1024**3, days=14):
        self.root = Path(os.path.abspath(root))
        self.enabled, self.images = enabled, images
        self.session_limit, self.total_limit, self.days = session_limit, total_limit, days
        self.lock = threading.RLock()
        self.active: set[Path] = set()
        self.dropped = 0
        self.last_failure: str | None = None
        self._sizes: dict[Path, int] | None = None
        self._pruned_at = 0.0

    def failed(self, exc: BaseException) -> None:
        self._sizes = None  # A partial write may have changed disk usage.
        self.dropped += 1
        self.last_failure = type(exc).__name__
        if self.dropped == 1 or self.dropped % 100 == 0:
            logging.getLogger(__name__).warning("Diagnostic capture degraded (%s); %s records omitted", self.last_failure, self.dropped)

    def safe(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts)
        if ".." in path.parts or not path.is_relative_to(self.root): raise ValueError("Invalid diagnostic path")
        for parent in (path, *path.parents):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
                raise ValueError("Diagnostic links are forbidden")
        return path

    def files(self, path: Path) -> list[Path]:
        result = []
        self.safe(*path.relative_to(self.root).parts)
        if not path.exists(): return result
        def walk(directory):
            for child in directory.iterdir():
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
                    raise ValueError("Diagnostic links are forbidden")
                if stat.S_ISDIR(info.st_mode): walk(child)
                elif stat.S_ISREG(info.st_mode): result.append(child)
        walk(path)
        return result

    def usage(self, path: Path) -> int:
        return sum(p.stat().st_size for p in self.files(path)) if path.exists() else 0

    def room(self, directory: Path, size: int) -> None:
        session = directory.parent
        # This process owns managed writes under lock. Recount after restart,
        # failures or periodic retention; do not rescan every file for each event.
        now = time.monotonic()
        if now - self._pruned_at >= 60: self._sizes = None
        if self._sizes is None:
            self._sizes = {}
            for path in self.files(self.root):
                self._sizes[path.parent] = self._sizes.get(path.parent, 0) + path.stat().st_size
        total_bytes = sum(self._sizes.values())
        session_bytes = sum(value for path, value in self._sizes.items() if path.parent == session)
        pressure = total_bytes + size > self.total_limit or session_bytes + size > self.session_limit
        if not pressure and now - self._pruned_at < 60: return
        self._pruned_at = now
        traces = [path for path in self._sizes if path.is_dir()]
        for trace in sorted(traces, key=lambda p: p.stat().st_mtime):
            self.safe(*trace.relative_to(self.root).parts)
            old = time.time() - trace.stat().st_mtime > self.days * 86400
            pressure = total_bytes + size > self.total_limit or (trace.parent == session and session_bytes + size > self.session_limit)
            if (old or pressure) and trace not in self.active and trace != directory:
                # Verify all descendants before recursive removal; only managed UUID traces.
                if len(trace.relative_to(self.root).parts) != 2 or not re.fullmatch(r"[0-9a-f]{32}", trace.name): continue
                removed_bytes = self.usage(trace)
                shutil.rmtree(trace)
                self._sizes.pop(trace, None)
                total_bytes -= removed_bytes
                if trace.parent == session: session_bytes -= removed_bytes
        if session_bytes + size > self.session_limit or total_bytes + size > self.total_limit:
            raise OSError("Diagnostic storage limit")

    def account(self, directory: Path, size: int) -> None:
        if self._sizes is not None:
            self._sizes[directory] = self._sizes.get(directory, 0) + size

    def blob(self, directory: Path, value: Any, *, image=False) -> dict:
        data = value if image else encoded(clean(value))
        digest = hashlib.sha256(data).hexdigest()
        reference = dict(sha256=digest, bytes=len(data), kind="image" if image else "json")
        if len(data) > 8*1024**2 or (image and not self.images):
            return {**reference, "omitted": "capture disabled or payload limit"}
        path = self.safe(*directory.relative_to(self.root).parts, digest + (".image" if image else ".json"))
        if not path.exists():
            self.room(directory, len(data))
            temporary = path.with_suffix(".tmp")
            self.safe(*temporary.relative_to(self.root).parts)
            temporary.write_bytes(data)
            os.replace(temporary, path)
            self.account(directory, len(data))
        return {**reference, "file": path.name}

    def write(self, trace: Trace, event: str, data: dict) -> None:
        if not self.enabled: return
        try:
            with self.lock:
                directory = self.safe(trace.session_id, trace.id)
                directory.mkdir(parents=True, exist_ok=True)
                payload = clean(data)
                if len(encoded(payload)) > 32*1024:
                    payload = {"payload": self.blob(directory, payload)}
                trace.sequence += 1
                record = dict(version=1, run_id=RUN_ID, timestamp=datetime.now(timezone.utc).isoformat(),
                    trace_id=trace.id, session_id=trace.session_id, message_id=trace.message_id,
                    sequence=trace.sequence, event=event, data=payload)
                raw = encoded(record) + b"\n"
                if len(raw) > 64*1024: raise ValueError("Event envelope limit")
                self.room(directory, len(raw))
                with self.safe(trace.session_id, trace.id, "events.jsonl").open("ab") as stream: stream.write(raw)
                self.account(directory, len(raw))
        except Exception as exc:
            self.failed(exc)

    def reference(self, trace: Trace, value: Any, *, image=False) -> dict:
        if not self.enabled: return {"omitted": "diagnostics disabled"}
        try:
            with self.lock:
                directory = self.safe(trace.session_id, trace.id)
                directory.mkdir(parents=True, exist_ok=True)
                return self.blob(directory, value, image=image)
        except Exception as exc:
            self.failed(exc)
            return {"omitted": "storage failure", "error_type": type(exc).__name__}

    def read(self, session_id: str) -> tuple[list[dict], list[str]]:
        if not IDENTIFIER.fullmatch(session_id): raise ValueError("Invalid session ID")
        records, missing = [], []
        directory = self.safe(session_id)
        for path in sorted(directory.glob("*/events.jsonl")):
            self.safe(*path.relative_to(self.root).parts)
            if path.stat().st_size > self.session_limit: raise ValueError("Oversized event stream")
            expected_sequence = 1
            for line in path.read_bytes().splitlines():
                try:
                    row = json.loads(line)
                    if row["session_id"] != session_id or row["trace_id"] != path.parent.name: raise ValueError()
                    if not isinstance(row["timestamp"], str) or type(row["sequence"]) is not int: raise ValueError()
                    if not isinstance(row["event"], str) or not isinstance(row["data"], dict): raise ValueError()
                    if "message_id" not in row: raise ValueError()
                    if row["sequence"] != expected_sequence:
                        missing.append(f"Sequence gap in {path.parent.name}: expected {expected_sequence}, found {row['sequence']}")
                    expected_sequence = row["sequence"] + 1
                    records.append(row)
                except (ValueError, KeyError, TypeError): missing.append(f"Incomplete/corrupt event in {path.parent.name}")
        records.sort(key=lambda row: (row["timestamp"], row["trace_id"], row["sequence"]))
        return records, missing

    def status(self) -> dict:
        with self.lock:
            self.safe()
            sessions = []
            if self.root.exists():
                for directory in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                    if directory.is_dir() and IDENTIFIER.fullmatch(directory.name):
                        records, missing = self.read(directory.name)
                        ended = {r["trace_id"] for r in records if r["event"] == "request.end"}
                        requests = []
                        for record in records:
                            if record["event"] not in ("request.begin", "request.end", "design.committed"): continue
                            data = record["data"]
                            summary = {key: data[key] for key in ("outcome", "committed", "revision") if key in data}
                            if record["event"] == "request.begin" and record["trace_id"] not in ended:
                                summary["outcome"] = "running" if directory / record["trace_id"] in self.active else "incomplete / unknown"
                            requests.append(dict(trace_id=record["trace_id"], message_id=record["message_id"], event=record["event"], data=summary))
                        sessions.append(dict(session_id=directory.name, events=len(records), last=records[-1]["timestamp"] if records else None,
                            requests=requests[-40:], missing=missing))
            return dict(enabled=self.enabled, healthy=self.last_failure is None, omitted_events=self.dropped,
                last_failure=self.last_failure, capture_images=self.images, sessions=sessions)

    def export(self, session_id: str, include_images: bool, browser: list, browser_omitted: int = 0) -> bytes:
        with self.lock:
            records, missing = self.read(session_id)
            if not records: raise KeyError(session_id)
            if self.usage(self.safe(session_id)) > self.session_limit: raise ValueError("Session export limit")
            buffer = io.BytesIO()
            with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
                archive.writestr("events.jsonl", b"".join(encoded(r) + b"\n" for r in records))
                visited = set()
                def check_references(value, trace_id):
                    if isinstance(value, dict):
                        filename = value.get("file")
                        if isinstance(filename, str) and "sha256" in value:
                            if not re.fullmatch(r"[0-9a-f]{64}\.(json|image)", filename):
                                missing.append(f"Invalid object reference in {trace_id}")
                            else:
                                path = self.safe(session_id, trace_id, filename)
                                if path not in visited:
                                    visited.add(path)
                                    if not path.is_file(): missing.append(f"Missing object: {trace_id}/{filename}")
                                    elif path.stat().st_size > 8*1024**2: missing.append(f"Oversized object: {filename}")
                                    else:
                                        data = path.read_bytes()
                                        if hashlib.sha256(data).hexdigest() != path.stem:
                                            missing.append(f"Corrupt object: {filename}")
                                        elif path.suffix != ".image" or include_images:
                                            archive.writestr("objects/" + trace_id + "/" + filename, data)
                                            if path.suffix == ".json":
                                                try: check_references(json.loads(data), trace_id)
                                                except ValueError: missing.append(f"Invalid JSON object: {filename}")
                        for child in value.values(): check_references(child, trace_id)
                    elif isinstance(value, list):
                        for child in value: check_references(child, trace_id)
                for row in records:
                    check_references(row["data"], row["trace_id"])
                begun = {r["trace_id"] for r in records if r["event"] == "request.begin"}
                ended = {r["trace_id"] for r in records if r["event"] == "request.end"}
                summary: dict[str, Any] = dict(version=1, session_id=session_id, events=len(records), incomplete_traces=sorted(begun-ended),
                    runtime=dict(python=sys.version.split()[0], diagnostic_schema=1),
                    limits=dict(session_bytes=self.session_limit, total_bytes=self.total_limit, retention_days=self.days, capture_images=self.images),
                    missing=missing, drawings_included=include_images, health=dict(scope="current_process", omitted_events=self.dropped, last_failure=self.last_failure),
                    limitations=["Retention may have removed older requests.", "Incomplete requests have unknown completion; inspect commit events.", "Not a project backup; STEP/GLB excluded."])
                archive.writestr("summary.json", json.dumps(summary, indent=2))
                archive.writestr("summary.txt", f"CADenza local diagnostics: {session_id}\n{len(records)} events; {len(begun-ended)} incomplete requests.\nIncludes proprietary prompt/design data.\n" + "\n".join(summary["limitations"]))
                archive.writestr("browser.json", encoded(dict(authoritative=False, partial=True, omitted=browser_omitted,
                    limitation="Metadata only; resets on page reload.", events=clean(browser))))
            return buffer.getvalue()


@lru_cache
def store() -> DiagnosticStore:
    settings = get_settings()
    return DiagnosticStore(settings.diagnostics_path, enabled=settings.diagnostics_enabled, images=settings.diagnostics_capture_images,
        session_limit=settings.diagnostics_session_bytes, total_limit=settings.diagnostics_total_bytes, days=settings.diagnostics_retention_days)


@dataclass
class Trace:
    session_id: str
    message_id: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    sequence: int = 0
    committed: bool = False
    rejected: bool = False

    def log(self, event_type: str, **kwargs) -> None:
        try:
            store().write(self, event_type, kwargs)
        except Exception:
            # Instrumentation must never replace a modeling or delivery outcome.
            pass


def event(name: str, **data) -> None:
    trace = CURRENT.get()
    if trace is not None: trace.log(name, **data)


def snapshot(ledgers) -> dict:
    trace = CURRENT.get()
    if trace is None: return {"omitted": "no request context"}
    try: return store().reference(trace, {k: v.model_dump(mode="json") for k,v in ledgers.items()})
    except Exception as exc:
        store().failed(exc)
        return {"omitted": "snapshot failure"}


@contextmanager
def request_trace(session, request: dict):
    if request.get("type") in ("ping", "client.log"):
        yield None
        return
    trace = Trace(session.id if session else "operation_" + uuid4().hex, request.get("message_id"))
    token = CURRENT.set(trace)
    started = time.monotonic()
    diagnostic_store = None
    directory = None
    outcome = "completed"
    try:
        try:
            diagnostic_store = store()
            directory = diagnostic_store.root / trace.session_id / trace.id
            with diagnostic_store.lock:
                diagnostic_store.active.add(directory)
            images = []
            for image in request.get("images", []):
                try:
                    raw = base64.b64decode(image["data"].split(",")[-1], validate=True)
                    images.append(dict(media_type=image["media_type"], **diagnostic_store.reference(trace, raw, image=True)))
                except Exception: images.append({"omitted": "invalid image"})
            trace.log("request.begin", request=request, images=images, revision=session.revision if session else None,
                primary_ledger_id=getattr(session, "primary_ledger_id", None),
                project_id=session.project_id if session else None, project_version=session.project_version if session else None,
                starting_snapshot=snapshot(session.snapshot()) if session else None)
        except Exception as exc:
            if diagnostic_store is not None: diagnostic_store.failed(exc)
        yield trace
    except BaseException as exc:
        outcome = "delivery_failed_after_commit" if trace.committed else "interrupted_or_failed"
        trace.log("request.exception", error_type=type(exc).__name__, committed=trace.committed)
        raise
    finally:
        trace.log("request.end", outcome="rejected" if trace.rejected and outcome == "completed" else outcome,
            committed=trace.committed, duration_ms=(time.monotonic()-started)*1000)
        try:
            if diagnostic_store is not None and directory is not None:
                with diagnostic_store.lock:
                    diagnostic_store.active.discard(directory)
        finally:
            CURRENT.reset(token)
