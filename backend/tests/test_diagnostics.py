import asyncio
import io
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from cadenza_backend import diagnostics as d


@pytest.fixture
def capture(tmp_path, monkeypatch):
    value = d.DiagnosticStore(tmp_path / "diagnostics")
    monkeypatch.setattr(d, "store", lambda: value)
    return value


def session(name="session_a"):
    return SimpleNamespace(id=name, revision=2, project_id=None, project_version=None, snapshot=lambda: {})


def test_commit_then_failed_delivery_is_not_reported_as_rollback(capture):
    with pytest.raises(ConnectionError):
        with d.request_trace(session(), {"type": "edit.apply", "message_id": "same"}) as trace:
            assert trace is not None
            trace.committed = True
            d.event("design.committed", revision=3)
            raise ConnectionError("private exception text")
    records, missing = capture.read("session_a")
    assert not missing
    assert records[-1]["data"]["outcome"] == "delivery_failed_after_commit"
    assert records[-1]["data"]["committed"] is True
    assert "private exception text" not in json.dumps(records)
    assert d.CURRENT.get() is None
    assert not capture.active


def test_concurrent_contexts_and_repeated_message_ids(capture):
    async def request(name):
        with d.request_trace(session(name), {"type": "prompt", "message_id": "same"}):
            await asyncio.sleep(0)
            d.event("model.answer", owner=name)
    async def run():
        await asyncio.gather(request("a"), request("b"), request("a"))
    asyncio.run(run())
    rows, _ = capture.read("a")
    assert len({r["trace_id"] for r in rows}) == 2
    assert all(r["data"]["owner"] == "a" for r in rows if r["event"] == "model.answer")
    assert d.CURRENT.get() is None


def test_snapshot_failure_does_not_prevent_operation(capture):
    current = session()
    def broken():
        raise PermissionError("private path")
    current.snapshot = broken
    with d.request_trace(current, {"type": "prompt"}):
        d.event("operation.executed")
    assert capture.dropped == 1
    assert d.CURRENT.get() is None
    assert any(r["event"] == "operation.executed" for r in capture.read(current.id)[0])


def test_disk_failure_preserves_original_exception(capture, monkeypatch):
    def denied(*args):
        raise PermissionError("private path")
    monkeypatch.setattr(capture, "safe", denied)
    with pytest.raises(RuntimeError, match="original"):
        with d.request_trace(session(), {"type": "prompt"}):
            raise RuntimeError("original")
    assert capture.dropped > 0
    assert d.CURRENT.get() is None


def test_redaction_and_image_export_opt_in(capture):
    with d.request_trace(session(), {"type": "prompt", "images": [{"data": "aW1hZ2U=", "media_type": "image/png"}]}):
        d.event("model.answer", api_key="secret-value", blocks=[{"type": "thinking", "text": "hidden-value"}],
                url="https://example.test/image?credential=value")
    for include in (False, True):
        with ZipFile(io.BytesIO(capture.export("session_a", include, []))) as archive:
            assert any(n.endswith(".image") for n in archive.namelist()) == include
            raw = archive.read("events.jsonl").decode()
            assert "secret-value" not in raw and "hidden-value" not in raw and "credential=value" not in raw
            assert "aW1hZ2U=" not in raw


def test_restart_corrupt_tail_and_incomplete_request(capture):
    trace = d.Trace("retained")
    trace.log("request.begin", request={"type": "prompt"})
    path = capture.safe("retained", trace.id, "events.jsonl")
    with path.open("ab") as stream:
        stream.write(b'{"session_id":"retained","trace_id":"' + trace.id.encode() + b'"}\n{broken')
    restarted = d.DiagnosticStore(capture.root)
    with ZipFile(io.BytesIO(restarted.export("retained", False, []))) as archive:
        summary = json.loads(archive.read("summary.json"))
        assert summary["incomplete_traces"] == [trace.id]
        assert len(summary["missing"]) == 2


@pytest.mark.parametrize("identifier", ["../outside", "a/b", "C:\\outside", ""])
def test_invalid_session_ids(capture, identifier):
    with pytest.raises(ValueError):
        capture.read(identifier)


def test_active_trace_is_preserved_under_storage_pressure(capture):
    capture.session_limit = capture.total_limit = 1500
    with d.request_trace(session(), {"type": "prompt"}) as trace:
        assert trace is not None
        for _ in range(8):
            d.event("large", text="x" * 1000)
        assert capture.safe("session_a", trace.id, "events.jsonl").exists()
    assert capture.dropped > 0
    assert capture.usage(capture.root) <= 1500


@pytest.mark.parametrize("kind", ["ping", "client.log"])
def test_transport_housekeeping_creates_no_verbose_record(capture, kind):
    with d.request_trace(session(), {"type": kind}):
        pass
    assert not capture.root.exists()


def test_missing_snapshot_is_reported_in_export(capture):
    trace = d.Trace("retained")
    reference = capture.reference(trace, {"synthetic": True})
    trace.log("request.begin", snapshot=reference)
    capture.safe("retained", trace.id, reference["file"]).unlink()
    with ZipFile(io.BytesIO(capture.export("retained", False, []))) as archive:
        assert "Missing object" in json.loads(archive.read("summary.json"))["missing"][0]


def test_disabled_image_capture_keeps_hash_without_bytes(capture):
    capture.images = False
    reference = capture.reference(d.Trace("retained"), b"drawing", image=True)
    assert reference["sha256"] and reference["omitted"]
    assert "file" not in reference


def test_oversized_snapshot_and_serialization_failure_are_explicit(capture):
    trace = d.Trace("retained")
    reference = capture.reference(trace, ["x" * 900_000] * 10)
    assert reference["omitted"] and reference["bytes"] > 8 * 1024**2
    trace.log("invalid", number=float("nan"))
    assert capture.dropped == 1


def test_model_client_uses_ambient_trace_without_extra_attempts(capture, monkeypatch):
    import httpx
    from cadenza_backend.agents.model_client import ModelClient
    from cadenza_backend.agents.sketch_editor import CARD
    attempts = []
    def respond(request):
        attempts.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"question":"Size?"}',
            "reasoning_content": "hidden-provider-thought"}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with d.request_trace(session(), {"type": "design.prompt", "message_id": "model_test"}):
        client = ModelClient()
        client.s = client.s.model_copy(update={"openrouter_api_key": "synthetic-key", "model_machinist": "openrouter:fixture"})
        assert asyncio.run(client.complete_json(stage="machinist", agent_card=CARD, user="synthetic"))["question"] == "Size?"
    rows, _ = capture.read("session_a")
    assert len(attempts) == 1
    assert len([r for r in rows if r["event"] == "model.attempt"]) == 1
    assert "hidden-provider-thought" not in json.dumps(rows)
    assert "synthetic-key" not in json.dumps(rows)


def test_real_acceptance_boundary_records_committed_revision_before_delivery(capture):
    from cadenza_backend.design import accept_design
    from cadenza_backend.geometry_bridge import BuildOutcome
    from cadenza_backend.session import Session
    current = Session()
    async def disconnected(_):
        raise ConnectionError("closed")
    with pytest.raises(ConnectionError):
        with d.request_trace(current, {"type": "preview.decision", "message_id": "apply"}):
            asyncio.run(accept_design(current, current.stage({}), BuildOutcome(ok=True, stub=False, detail="fixture"), "apply", disconnected))
    assert current.revision == 1
    records, _ = capture.read(current.id)
    commits = [r for r in records if r["event"] == "design.committed"]
    assert len(commits) == 1 and commits[0]["data"]["revision"] == 1
    assert records[-1]["data"]["outcome"] == "delivery_failed_after_commit"


def test_failed_geometry_keeps_candidate_and_context(capture, monkeypatch):
    from cadenza_backend import geometry_jobs as jobs
    from cadenza_backend.contracts_bridge import ErrorCode
    from cadenza_backend.session import Session
    current = Session()
    async def failed(request):
        jobs.GeometryJobRunner._record({"job_id": "fixture"}, d.time.monotonic(), "failed")
        raise jobs.GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "synthetic")
    monkeypatch.setattr(jobs.runner, "run", failed)
    with d.request_trace(current, {"type": "design.command", "message_id": "failed_candidate"}):
        outcome = asyncio.run(jobs.build_design(current.stage({}), current.primary_ledger_id))
    assert not outcome.ok and current.revision == 0
    records, _ = capture.read(current.id)
    assert next(r for r in records if r["event"] == "geometry.candidate")["data"]["snapshot"]["file"]
    assert next(r for r in records if r["event"] == "geometry.job")["data"]["message_id"] == "failed_candidate"


def test_accounting_matches_disk_and_pressure_prunes_only_completed_traces(capture):
    capture.session_limit = 2000
    old = d.Trace("session_a")
    old.log("old", text="x" * 800)
    with d.request_trace(session(), {"type": "prompt"}) as trace:
        assert trace is not None
        for _ in range(4): d.event("next", text="y" * 300)
        assert not capture.safe("session_a", old.id).exists()
        assert capture.safe("session_a", trace.id).exists()
    assert capture.usage(capture.root) <= capture.session_limit
    if capture._sizes is not None:
        assert sum(capture._sizes.values()) == capture.usage(capture.root)


def test_reparse_path_is_rejected_without_read_or_removal(capture, monkeypatch):
    from pathlib import Path
    original = Path.lstat
    capture.root.mkdir()
    linked = capture.root / "linked"
    linked.mkdir()
    def attributes(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == linked:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=1024)
        return result
    monkeypatch.setattr(Path, "lstat", attributes)
    with pytest.raises(ValueError, match="links"):
        capture.safe("linked", "child")
    with pytest.raises(ValueError, match="links"):
        capture.files(capture.root)
    assert linked.exists()


def test_cancellation_resets_context(capture):
    async def run():
        with pytest.raises(asyncio.CancelledError):
            with d.request_trace(session(), {"type": "prompt"}):
                raise asyncio.CancelledError()
        assert d.CURRENT.get() is None
    asyncio.run(run())
    assert not capture.active
    assert capture.read("session_a")[0][-1]["data"]["outcome"] == "interrupted_or_failed"


def test_primary_identity_and_missing_sequence_survive_export(capture):
    current = session()
    current.primary_ledger_id = "primary"
    with d.request_trace(current, {"type": "prompt"}) as trace:
        assert trace is not None
        trace.sequence += 1  # Simulate an event lost after sequence allocation.
        d.event("next")
    records, missing = capture.read(current.id)
    assert records[0]["data"]["primary_ledger_id"] == "primary"
    assert any("Sequence gap" in item for item in missing)


def test_concurrent_exports_and_retention_keep_valid_bundles(capture):
    from concurrent.futures import ThreadPoolExecutor
    capture.session_limit = 6000
    first = d.Trace("session_a")
    first.log("request.begin")
    def write():
        for _ in range(20):
            capture.write(d.Trace("session_a"), "single", {"text": "x" * 300})
    def export():
        for _ in range(10):
            with ZipFile(io.BytesIO(capture.export("session_a", False, []))) as archive:
                assert archive.testzip() is None
                assert json.loads(archive.read("summary.json"))["events"] > 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write), pool.submit(export)]
        for future in futures: future.result()
    assert capture.usage(capture.root) <= 6000


def test_status_marks_restart_incomplete_without_exposing_prompt_payload(capture):
    d.Trace("retained").log("request.begin", request={"prompt": "private synthetic content"})
    restarted = d.DiagnosticStore(capture.root)
    status = restarted.status()
    assert status["sessions"][0]["requests"][0]["data"]["outcome"] == "incomplete / unknown"
    assert "private synthetic content" not in json.dumps(status)


def test_status_distinguishes_active_request_from_missing_completion(capture):
    with d.request_trace(session(), {"type": "prompt"}):
        assert capture.status()["sessions"][0]["requests"][0]["data"]["outcome"] == "running"
    assert capture.status()["sessions"][0]["requests"][-1]["data"]["outcome"] == "completed"
