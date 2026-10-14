import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from cadenza_backend import geometry_jobs as jobs
from cadenza_backend.app import app
from cadenza_backend.contracts_bridge import ErrorCode
from fastapi.testclient import TestClient


def command(script):
    return (sys.executable, "-c", script)


SUCCESS = "import pathlib,sys; pathlib.Path(sys.argv[2]).write_text('{\"ok\":true}')"
SLOW = "import time; time.sleep(60)"


@pytest.mark.parametrize("case", ["readable", "recovered", "persistent", "attempt_limit", "deadline",
                                 "late_read", "other_errno", "missing", "io", "encoding"])
def test_result_read_policy_uses_original_deadline(monkeypatch, tmp_path, case):
    from types import SimpleNamespace
    from unittest.mock import Mock
    clock, reads, waits = [0.0], [], []
    metrics = {}
    process = Mock()
    process.poll.return_value = 0  # A completed one-shot worker remains supported.
    worker = jobs._Worker(process)
    runner = jobs.GeometryJobRunner()
    path = tmp_path / "result.json"
    path.write_text('{"ok":true}', encoding="utf-8")
    original_read = Path.read_text
    deadline = 0.05 if case == "deadline" else 10.0
    def read(target, *args, **kwargs):
        reads.append(clock[0])
        if case in ("persistent", "attempt_limit", "deadline") or (case == "recovered" and len(reads) == 1):
            raise PermissionError(13, "PRIVATE_PAYLOAD", "PRIVATE_PATH")
        if case == "other_errno": raise PermissionError(1, "denied")
        if case == "missing": raise FileNotFoundError(2, "missing")
        if case == "io": raise OSError(5, "io")
        if case == "encoding": raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        if case == "late_read": clock[0] = deadline
        return original_read(target, *args, **kwargs)
    async def sleep(delay):
        waits.append(delay)
        # No clock advance proves the separate attempt cap, without wall-clock races.
        if case != "attempt_limit": clock[0] += delay
    async def scenario():
        with monkeypatch.context() as patch:
            patch.setattr(jobs.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: clock[0]))
            patch.setattr(jobs.asyncio, "sleep", sleep)
            patch.setattr(Path, "read_text", read)
            if case in ("readable", "recovered"):
                assert await runner._read_published(worker, path, deadline, metrics) == '{"ok":true}'
            else:
                expected = (jobs.GeometryJobError if case in ("deadline", "late_read") else
                            FileNotFoundError if case == "missing" else
                            UnicodeDecodeError if case == "encoding" else OSError)
                with pytest.raises(expected) as error:
                    await runner._read_published(worker, path, deadline, metrics)
                if case in ("deadline", "late_read"):
                    assert isinstance(error.value, jobs.GeometryJobError)
                    assert error.value.code == ErrorCode.GEOMETRY_TIMEOUT
                if case == "deadline": assert isinstance(error.value.__cause__, PermissionError)
    asyncio.run(scenario())
    assert reads and all(attempt < deadline for attempt in reads)
    assert len(reads) <= 11 and all(0 < wait <= 0.02 for wait in waits)
    if case == "attempt_limit": assert len(reads) == 11
    if case == "persistent": assert all(attempt < 0.2 for attempt in reads) and clock[0] <= 0.2
    if case == "deadline": assert clock[0] == deadline
    if case == "recovered": assert len(reads) == 2 and metrics["result_read_retries"] == 1
    if case in ("readable", "late_read", "other_errno", "missing", "io", "encoding"):
        assert len(reads) == 1 and not waits and not metrics


@pytest.mark.parametrize("boundary", ["read", "parse"])
def test_result_processing_cannot_accept_after_deadline(monkeypatch, boundary, caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = jobs.GeometryJobRunner(1, 3, command(SUCCESS))
    original_read, original_loads = Path.read_text, jobs.json.loads
    async def scenario():
        loop = asyncio.get_running_loop()
        original_time = loop.time
        def expire():
            monkeypatch.setattr(loop, "time", lambda: original_time() + 100)
        def read(path, *args, **kwargs):
            result = original_read(path, *args, **kwargs)
            if path.name == "result.json" and boundary == "read": expire()
            return result
        def loads(*args, **kwargs):
            result = original_loads(*args, **kwargs)
            if boundary == "parse": expire()
            return result
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", read)
            patch.setattr(jobs.json, "loads", loads)
            try:
                with pytest.raises(jobs.GeometryJobError) as error:
                    await runner.run({})
                assert error.value.code == ErrorCode.GEOMETRY_TIMEOUT
                assert not runner._workers
            finally:
                monkeypatch.setattr(loop, "time", original_time)
        assert await runner.run({}) == {"ok": True}
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    first = next(r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics"))
    assert first["status"] == "GEOMETRY_TIMEOUT" and first["failure"]["step"] == f"result_{boundary}"


@pytest.mark.parametrize("primary", ["success", "timeout", "cancel"])
def test_cleanup_failure_preserves_primary_and_reports_failure(monkeypatch, caplog, primary):
    caplog.set_level("INFO", logger=jobs.__name__)
    original_directory = jobs.TemporaryDirectory
    directories = []
    class FailingCleanup(original_directory):
        def cleanup(self):
            super().cleanup()
            raise PermissionError(13, "PRIVATE_PAYLOAD", "PRIVATE_PATH")
    def directory(*args, **kwargs):
        result = FailingCleanup(*args, **kwargs)
        directories.append(result.name)
        return result
    monkeypatch.setattr(jobs, "TemporaryDirectory", directory)
    runner = jobs.GeometryJobRunner(1, 0.3 if primary != "success" else 3,
        command(SUCCESS if primary == "success" else SLOW))
    async def scenario():
        task = asyncio.create_task(runner.run({"secret": "PRIVATE_PAYLOAD"}))
        if primary == "cancel":
            while not runner._workers:
                await asyncio.sleep(0.01)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if primary == "cancel" else jobs.GeometryJobError) as error:
            await task
        if primary != "cancel":
            assert isinstance(error.value, jobs.GeometryJobError)
            assert error.value.code == (ErrorCode.GEOMETRY_TIMEOUT if primary == "timeout"
                                       else ErrorCode.GEOMETRY_KERNEL_FAILURE)
        assert not runner._workers
        monkeypatch.setattr(jobs, "TemporaryDirectory", original_directory)
        runner.command, runner.timeout_s = command(SUCCESS), 3
        assert await runner.run({}) == {"ok": True}
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    records = [r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics")]
    first = records[0]
    assert first["worker_exit_code_before_cleanup"] == (0 if primary == "success" else None)
    assert first["worker_exit_code"] is not None
    assert first["status"] == {"success": "GEOMETRY_KERNEL_FAILURE", "timeout": "GEOMETRY_TIMEOUT",
                               "cancel": "cancelled"}[primary]
    failure = first["failure"] if primary == "success" else first["cleanup_failures"][0]
    assert failure["step"] == "directory_cleanup" and failure["type"] == "PermissionError"
    assert failure["errno"] == 13
    assert "PRIVATE_PAYLOAD" not in caplog.text and "PRIVATE_PATH" not in caplog.text
    assert all(not Path(path).exists() for path in directories)
    assert records[1]["status"] == "success" and first["job_id"] != records[1]["job_id"]


@pytest.mark.parametrize("primary", ["timeout", "cancel"])
def test_pre_cleanup_observation_failure_preserves_outcome(monkeypatch, caplog, primary):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = jobs.GeometryJobRunner(1, 0.3, command(SLOW))
    original_failure = runner._failure
    observed = []
    def failure(exc, step):
        detail = original_failure(exc, step)
        worker = next(iter(runner._workers))
        original_poll = worker.process.poll
        def poll():
            monkeypatch.setattr(worker.process, "poll", original_poll)
            observed.append(True)
            raise OSError(5, "PRIVATE_DIAGNOSTIC")
        monkeypatch.setattr(worker.process, "poll", poll)
        return detail
    async def scenario():
        with monkeypatch.context() as patch:
            patch.setattr(runner, "_failure", failure)
            task = asyncio.create_task(runner.run({}))
            while not runner._workers:
                await asyncio.sleep(0.01)
            worker = next(iter(runner._workers))
            if primary == "cancel": task.cancel()
            with pytest.raises(asyncio.CancelledError if primary == "cancel" else jobs.GeometryJobError) as error:
                await task
            if primary == "timeout":
                assert isinstance(error.value, jobs.GeometryJobError)
                assert error.value.code == ErrorCode.GEOMETRY_TIMEOUT
            assert worker.process.poll() is not None and not runner._workers
        runner.command, runner.timeout_s = command(SUCCESS), 3
        assert await runner.run({}) == {"ok": True}
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    first = next(r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics"))
    assert observed == [True] and "worker_exit_code_before_cleanup" not in first
    assert first["status"] == ("cancelled" if primary == "cancel" else "GEOMETRY_TIMEOUT")
    assert "PRIVATE_DIAGNOSTIC" not in caplog.text


@pytest.mark.parametrize("fault", ["request_write", "checkout", "result_read", "result_parse", "worker_release"])
def test_parent_failure_stage_and_recovery(monkeypatch, caplog, fault):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = jobs.GeometryJobRunner(1, 3, command(SUCCESS))
    original_write, original_read = Path.write_text, Path.read_text
    original_checkout, original_release = runner._checkout, runner._release
    def fail():
        raise OSError(5, "PRIVATE_PAYLOAD", "PRIVATE_PATH")
    def write(path, *args, **kwargs):
        if path.name == "request.json": fail()
        return original_write(path, *args, **kwargs)
    def read(path, *args, **kwargs):
        if path.name == "result.json":
            if fault == "result_read": fail()
            return "{PRIVATE_PAYLOAD"
        return original_read(path, *args, **kwargs)
    def checkout(*args):
        fail()
    def release(*args):
        original_release(*args)
        fail()
    async def scenario():
        with monkeypatch.context() as patch:
            if fault == "request_write": patch.setattr(Path, "write_text", write)
            elif fault == "checkout": patch.setattr(runner, "_checkout", checkout)
            elif fault in ("result_read", "result_parse"): patch.setattr(Path, "read_text", read)
            else: patch.setattr(runner, "_release", release)
            with pytest.raises(jobs.GeometryJobError) as error:
                await runner.run({})
            assert error.value.code == ErrorCode.GEOMETRY_KERNEL_FAILURE
            assert isinstance(error.value.__cause__, (OSError, ValueError))
        assert not runner._workers
        assert await runner.run({}) == {"ok": True}
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    first = next(r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics"))
    assert first["status"] == "GEOMETRY_KERNEL_FAILURE" and first["failure"]["step"] == fault
    assert "PRIVATE_PAYLOAD" not in caplog.text and "PRIVATE_PATH" not in caplog.text


@pytest.mark.parametrize("failed_job", [False, True])
def test_logging_failure_does_not_change_job_result(monkeypatch, failed_job):
    def broken_log(*args, **kwargs):
        raise OSError("log unavailable")
    monkeypatch.setattr(jobs.logger, "info", broken_log)
    monkeypatch.setattr(jobs.logger, "warning", broken_log)
    runner = jobs.GeometryJobRunner(1, 3, command(SUCCESS))
    try:
        if failed_job:
            with pytest.raises(jobs.GeometryJobError):
                asyncio.run(runner.run({"invalid": float("nan")}))
        else:
            assert asyncio.run(runner.run({})) == {"ok": True}
        assert asyncio.run(runner.run({})) == {"ok": True}
    finally:
        runner.close()


@pytest.mark.parametrize("timings", [None, [], {"step": "PRIVATE_PAYLOAD"}, {"imports_ms": -1},
                                    {"operation_ms": True}])
def test_worker_timings_cannot_overwrite_parent_diagnostics(timings, caplog):
    import json
    caplog.set_level("INFO", logger=jobs.__name__)
    result = json.dumps({"ok": True, "_timings": timings})
    runner = jobs.GeometryJobRunner(1, 3, command(
        f"import pathlib,sys; pathlib.Path(sys.argv[2]).write_text({result!r})"))
    try:
        with pytest.raises(jobs.GeometryJobError):
            asyncio.run(runner.run({}))
        assert not runner._workers
    finally:
        runner.close()
    record = next(r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics"))
    assert record["failure"]["step"] == "result_parse"
    assert record["status"] == "GEOMETRY_KERNEL_FAILURE" and "PRIVATE_PAYLOAD" not in caplog.text


@pytest.mark.parametrize("operation", ["undo", "redo"])
@pytest.mark.parametrize("fault", ["cleanup", "result_read"])
def test_worker_failure_preserves_history_and_artifacts(monkeypatch, operation, fault):
    from cadenza_contracts.messages import decode_blob_frame
    from cadenza_backend.session import registry
    from cadenza_backend.design import accept_design
    from cadenza_backend.geometry_bridge import BuildOutcome
    current = registry.create()
    async def discard(_):
        pass
    def accept(ledgers, undo_index=None):
        asyncio.run(accept_design(current, ledgers,
            BuildOutcome(ok=True, detail="fixture", stub=False, glb=b"accepted", step=b"accepted-step"),
            "msg_seed", discard, undo_index=undo_index))
    accept(current.stage({}))
    accept(current.stage({}))
    undo = current.stage_undo()
    assert undo is not None
    accept(undo[0], undo[1])
    before, history, redo = current.snapshot(), list(current.history), list(current.redo)
    revision, manifest = current.revision, current.last_geometry
    artifacts = [current.artifacts.get(revision, kind) for kind in ("glb", "step")]
    original_directory = jobs.TemporaryDirectory
    class FailingCleanup(original_directory):
        def cleanup(self):
            super().cleanup()
            raise PermissionError(13, "injected cleanup failure")
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == "result.json": raise PermissionError(13, "injected read denial")
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(jobs, "runner", jobs.GeometryJobRunner(capacity=1))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": current.id})
        ws.receive_json()
        ws.receive_json()
        assert decode_blob_frame(ws.receive_bytes())[1] == b"accepted"
        assert decode_blob_frame(ws.receive_bytes())[1] == b"accepted-step"
        with monkeypatch.context() as patch:
            if fault == "cleanup": patch.setattr(jobs, "TemporaryDirectory", FailingCleanup)
            else: patch.setattr(Path, "read_text", read)
            ws.send_json({"type": f"session.{operation}", "message_id": "msg_failed", "base_revision": revision})
            failure = ws.receive_json()
            assert failure["code"] == "GEOMETRY_KERNEL_FAILURE"
        assert current.snapshot() == before and current.revision == revision
        assert current.history == history and current.redo == redo and current.last_geometry == manifest
        assert [current.artifacts.get(revision, kind) for kind in ("glb", "step")] == artifacts
        assert artifacts[0] is not None and artifacts[0].data == b"accepted"
        assert artifacts[1] is not None and artifacts[1].data == b"accepted-step"
        ws.send_json({"type": f"session.{operation}", "message_id": "msg_recovered", "base_revision": revision})
        assert ws.receive_json()["type"] == "ledger.updated"
        assert ws.receive_json()["empty"]
        assert current.revision == revision + 1


def test_timeout_crash_cancel_and_recovery(monkeypatch, caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    processes = []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(jobs.subprocess, "Popen", launch)

    async def scenario():
        runner = jobs.GeometryJobRunner(capacity=1, timeout_s=0.3, command=command(SLOW))
        with pytest.raises(jobs.GeometryJobError) as error:
            await runner.run({})
        assert error.value.code == ErrorCode.GEOMETRY_TIMEOUT
        assert processes[-1].poll() is not None
        assert not Path(processes[-1].args[-1]).parent.exists()
        runner.command = command("import os; os._exit(7)")
        with pytest.raises(jobs.GeometryJobError) as error:
            await runner.run({})
        assert error.value.code == ErrorCode.GEOMETRY_KERNEL_FAILURE
        runner.command = command("import pathlib,sys; pathlib.Path(sys.argv[2]).write_text('[]')")
        with pytest.raises(jobs.GeometryJobError) as error:
            await runner.run({})
        assert error.value.code == ErrorCode.GEOMETRY_KERNEL_FAILURE
        runner.command = command(SLOW)
        task = asyncio.create_task(runner.run({}))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[-1].poll() is not None
        assert not Path(processes[-1].args[-1]).parent.exists()
        runner.command = command(SUCCESS)
        runner.timeout_s = 3
        assert await runner.run({}) == {"ok": True}
    asyncio.run(scenario())
    records = [r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics")]
    assert [r["status"] for r in records] == ["GEOMETRY_TIMEOUT", "GEOMETRY_KERNEL_FAILURE",
        "GEOMETRY_KERNEL_FAILURE", "cancelled", "success"]
    assert all(r["total_ms"] >= 0 for r in records)


def test_admission_limit_and_server_responsiveness():
    async def scenario():
        runner = jobs.GeometryJobRunner(capacity=1, timeout_s=2, command=command(SLOW))
        task = asyncio.create_task(runner.run({}))
        await asyncio.sleep(0.05)
        try:
            with pytest.raises(jobs.GeometryJobError) as error:
                await runner.run({})
            assert error.value.code == ErrorCode.BUSY
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
                assert response.status_code == 200
                assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(scenario())


def test_real_build_probe_and_inspection(plate_ledger, caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    async def scenario():
        result = await jobs.build_design({plate_ledger.project_id: plate_ledger}, plate_ledger.project_id)
        assert result.ok and not result.stub
        assert result.glb is not None and result.glb[:4] == b"glTF"
        assert result.step is not None and b"ISO-10303-21" in result.step
        hit = await jobs.probe(plate_ledger, [20, 20, 20])
        assert hit.feature_id == plate_ledger.features[0].id
        from cadenza_backend.tools.probe import GeometryProbeTool
        measurement = await GeometryProbeTool().execute_async(
            context={"ledgers": {plate_ledger.project_id: plate_ledger}}, query_type="get_mass_properties")
        assert "error" not in measurement and "_timings" not in measurement
    asyncio.run(scenario())
    records = [r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics")]
    assert [r["operation"] for r in records] == ["build", "probe", "inspect"]
    for metrics in records:
        assert metrics["status"] == "success"
        assert metrics["imports_ms"] >= 0 and metrics["operation_ms"] >= 0
        assert metrics["total_ms"] >= metrics["imports_ms"] + metrics["operation_ms"]
        assert metrics["startup_ms"] >= 0 and metrics["execution_ms"] >= metrics["operation_ms"]
        assert metrics["phase"] == "execution"
        assert set(metrics) <= {"operation", "status", "imports_ms", "operation_ms", "total_ms", "worker_reused",
                                "startup_ms", "execution_ms", "phase", "real_geometry", "job_id", "step",
                                "startup_timeout_s", "timeout_s", "worker_pid", "worker_jobs", "worker_exit_code",
                                "worker_exit_code_before_cleanup",
                                "result_read_retries", "result_read_denial", "result_read_recovery_ms",
                                "readiness_read_retries", "readiness_read_denial", "readiness_read_recovery_ms"}


@pytest.mark.parametrize("startup", [False, True])
def test_second_socket_ping_during_timed_out_undo(monkeypatch, startup):
    import threading
    from cadenza_backend.session import registry
    from cadenza_backend.design import accept_design
    from cadenza_backend.geometry_bridge import BuildOutcome
    current = registry.create()
    async def discard(_):
        pass
    asyncio.run(accept_design(current, current.stage({}),
        BuildOutcome(ok=True, detail="accepted", stub=False, glb=b"accepted"), "msg_initial", discard))
    before, history, manifest = current.snapshot(), list(current.history), current.last_geometry
    started = threading.Event()
    original = subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        started.set()
        return process
    monkeypatch.setattr(jobs.subprocess, "Popen", launch)
    monkeypatch.setattr(jobs, "runner", jobs.GeometryJobRunner(1, 1.5, command(SLOW),
        persistent=startup, startup_timeout_s=1.5))
    with TestClient(app) as client, client.websocket_connect("/ws") as slow, client.websocket_connect("/ws") as other:
        slow.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": current.id})
        slow.receive_json()
        slow.receive_json()
        slow.receive_bytes()
        other.send_json({"type": "client.hello", "contract_version": "1.0.0"})
        other.receive_json()
        slow.send_json({"type": "session.undo", "message_id": "msg_undo", "base_revision": current.revision})
        assert started.wait(2)
        other.send_json({"type": "ping", "t": 42})
        assert other.receive_json() == {"type": "pong", "t": 42}
        assert current.lock.locked(), "ping must complete while the other session is still building"
        assert slow.receive_json()["code"] == "GEOMETRY_TIMEOUT"
        assert current.snapshot() == before and current.history == history
        assert current.last_geometry == manifest
        artifact = current.artifacts.get(1, "glb")
        assert artifact is not None and artifact.data == b"accepted"
