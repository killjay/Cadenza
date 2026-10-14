import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cadenza_backend import geometry_jobs as jobs
from cadenza_backend.app import app
from cadenza_contracts.ledger import Ledger


# Private protocol double; faults run inside the same child that served success.
SERVER = """
import json, os, pathlib, sys, time
ready = pathlib.Path(sys.argv[2])
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({'protocol': 1, 'real_geometry': True, 'imports_ms': 0.0}))
temporary.replace(ready)
for line in sys.stdin.buffer:
    source, target = json.loads(line)
    request = json.loads(pathlib.Path(source).read_text())
    if request.get('crash'): os._exit(7)
    time.sleep(request.get('sleep', 0))
    response = [] if request.get('invalid') else {'ok': True, 'pid': os.getpid(), 'source': source}
    temporary = pathlib.Path(target).with_suffix('.tmp')
    temporary.write_text(json.dumps(response))
    temporary.replace(target)
"""


def pool(**kwargs):
    return jobs.GeometryJobRunner(command=(sys.executable, "-c", SERVER), persistent=True, **kwargs)


@pytest.mark.parametrize("denials", [1, 3])
def test_warm_result_read_recovers_without_redispatch(monkeypatch, caplog, denials):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = pool(capacity=1)
    original_read = Path.read_text
    reads, writes = [], []
    def read(path, *args, **kwargs):
        if path.name == "result.json":
            assert path.exists()
            reads.append(path)
            if len(reads) <= denials:
                raise PermissionError(13, "PRIVATE_PAYLOAD", "PRIVATE_PATH")
        return original_read(path, *args, **kwargs)
    async def scenario():
        first = await runner.run({"primary_id": "same"})
        worker = runner._idle[0]
        pipe = worker.process.stdin
        assert pipe is not None
        class CountingPipe:
            def write(self, data):
                writes.append(data)
                return pipe.write(data)
            def flush(self):
                pipe.flush()
            def close(self):
                pipe.close()
        monkeypatch.setattr(worker.process, "stdin", CountingPipe())
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", read)
            recovered = await runner.run({"primary_id": "same"})
        assert recovered["pid"] == first["pid"]
        assert recovered["source"] != first["source"]
        assert len(writes) == 1 and len(set(reads)) == 1
        assert len(reads) == denials + 1 and worker.jobs == 2
        assert runner._idle == [worker] and worker.process.poll() is None
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    records = [r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics")]
    assert records[1]["status"] == "success"
    assert records[1]["result_read_retries"] == denials
    assert records[1]["result_read_recovery_ms"] >= 0
    assert records[1]["result_read_denial"]["errno"] == 13
    assert "PRIVATE_PAYLOAD" not in caplog.text and "PRIVATE_PATH" not in caplog.text


@pytest.mark.parametrize("fault", ["persistent", "cancel", "crash", "malformed", "cleanup"])
def test_result_read_recovery_failure_reaps_worker(monkeypatch, caplog, fault):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = pool(capacity=1)
    original_read = Path.read_text
    reads = []
    original_directory = jobs.TemporaryDirectory
    class FailingCleanup(original_directory):
        def cleanup(self):
            super().cleanup()
            raise OSError(5, "PRIVATE_PATH")
    async def scenario():
        await runner.run({})
        worker = runner._idle[0]
        def read(path, *args, **kwargs):
            if path.name == "result.json":
                reads.append(path)
                if fault == "malformed" and len(reads) > 1: return "{broken"
                if fault == "crash":
                    worker.process.kill()
                    worker.process.wait()
                if fault == "cancel":
                    task = asyncio.current_task()
                    assert task is not None
                    asyncio.get_running_loop().call_soon(task.cancel)
                raise PermissionError(13, "PRIVATE_PAYLOAD", "PRIVATE_PATH")
            return original_read(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", read)
            if fault == "cleanup": patch.setattr(jobs, "TemporaryDirectory", FailingCleanup)
            with pytest.raises(asyncio.CancelledError if fault == "cancel" else jobs.GeometryJobError):
                await runner.run({})
        assert worker.process.poll() is not None and not runner._workers and not runner._idle
        assert reads and all(not path.parent.exists() for path in reads)
        assert (await runner.run({}))["pid"] != worker.process.pid
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    records = [r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics")]
    failed = records[1]
    assert failed["status"] == ("cancelled" if fault == "cancel" else "GEOMETRY_KERNEL_FAILURE")
    assert failed["failure"]["step"] == ("result_parse" if fault == "malformed" else "result_read")
    assert failed["result_read_retries"] <= 10
    assert failed["result_read_denial"]["errno"] == 13
    if fault == "cleanup":
        assert failed["failure"]["type"] == "PermissionError"
        assert failed["cleanup_failures"][0]["step"] == "directory_cleanup"
    assert records[2]["status"] == "success"
    assert "PRIVATE_PAYLOAD" not in caplog.text and "PRIVATE_PATH" not in caplog.text


def test_reaped_worker_is_removed_even_when_pipe_close_fails(monkeypatch, caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = pool(capacity=1, max_jobs=1)
    stop = runner._stop
    def close_failure(worker):
        stop(worker)
        raise BrokenPipeError(32, "private pipe path")
    async def scenario():
        with monkeypatch.context() as patch:
            patch.setattr(runner, "_stop", close_failure)
            with pytest.raises(jobs.GeometryJobError):
                await runner.run({})
            assert not runner._workers and not runner._idle
        assert (await runner.run({}))["ok"]
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    first = next(r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics"))
    assert first["status"] == "GEOMETRY_KERNEL_FAILURE"
    assert first["failure"]["step"] == "worker_release"


def test_warm_pipe_failure_replaces_worker(monkeypatch, caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = pool(capacity=1)
    async def scenario():
        first = await runner.run({})
        worker = runner._idle[0]
        # Inject dispatch failure without racing the child's observation of pipe EOF.
        assert worker.process.stdin is not None
        pipe = worker.process.stdin
        class BrokenPipe:
            def write(self, data):
                raise BrokenPipeError(32, "private pipe path")
            def close(self):
                pipe.close()
        monkeypatch.setattr(worker.process, "stdin", BrokenPipe())
        with pytest.raises(jobs.GeometryJobError):
            await runner.run({})
        assert worker.process.poll() is not None and not runner._workers
        assert (await runner.run({}))["pid"] != first["pid"]
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    failed = next(r.geometry_metrics for r in caplog.records
                  if hasattr(r, "geometry_metrics") and r.geometry_metrics["status"] != "success")
    assert failed["failure"]["step"] == "pipe_dispatch"


def test_reuse_recycling_and_shutdown(monkeypatch):
    runner = pool(capacity=1, max_jobs=2)
    monkeypatch.setattr(jobs, "runner", runner)
    async def scenario():
        first = await runner.run({})
        worker = runner._idle[0]
        second = await runner.run({})
        assert first["pid"] == second["pid"]
        assert worker.process.poll() is not None  # Retired at the configured limit.
        assert not Path(first["source"]).parent.exists()
        assert not Path(second["source"]).parent.exists()
        third = await runner.run({})
        assert third["pid"] != second["pid"]
        return runner._idle[0].process
    with TestClient(app):
        process = asyncio.run(scenario())
    assert process.poll() is not None and not runner._workers


@pytest.mark.parametrize("fault", ["sleep", "crash", "invalid", "cancel"])
def test_warm_worker_fault_replacement(fault):
    runner = pool(capacity=1, timeout_s=3)
    async def scenario():
        first = await runner.run({})
        worker = runner._idle[0]
        runner.timeout_s = 0.25
        request = {"sleep": 60} if fault in ("sleep", "cancel") else {fault: True}
        task = asyncio.create_task(runner.run(request))
        if fault == "cancel":
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(jobs.GeometryJobError) as error:
                await task
            assert error.value.code.value == ("GEOMETRY_TIMEOUT" if fault == "sleep" else "GEOMETRY_KERNEL_FAILURE")
        assert worker.process.poll() is not None and not runner._workers
        runner.timeout_s = 3
        recovered = await runner.run({})
        assert recovered["pid"] != first["pid"]
    try:
        asyncio.run(scenario())
    finally:
        runner.close()


def test_concurrent_capacity_affinity_and_idle_crash():
    runner = pool(capacity=2)
    async def scenario():
        tasks = [asyncio.create_task(runner.run({"primary_id": name, "sleep": 0.15})) for name in ("a", "b")]
        await asyncio.sleep(0.05)
        with pytest.raises(jobs.GeometryJobError) as error:
            await runner.run({})
        assert error.value.code.value == "BUSY"
        first, second = await asyncio.gather(*tasks)
        assert first["pid"] != second["pid"]
        assert (await runner.run({"primary_id": "a"}))["pid"] == first["pid"]
        assert (await runner.run({"primary_id": "b"}))["pid"] == second["pid"]
        for worker in runner._idle:
            worker.process.kill()
            worker.process.wait()
        assert (await runner.run({}))["pid"] not in (first["pid"], second["pid"])
    try:
        asyncio.run(scenario())
    finally:
        runner.close()


def test_real_dependency_edit_and_project_isolation(plate_ledger):
    runner = jobs.GeometryJobRunner(capacity=1)
    assembly = Ledger.model_validate({"assembly": {"components": [
        {"id": "comp_plate", "sub_assembly_id": plate_ledger.project_id}]}})
    ledgers = {assembly.project_id: assembly, plate_ledger.project_id: plate_ledger}
    async def build(primary, documents):
        result = await runner.run({"operation": "build", "primary_id": primary,
                                   "ledgers": {key: value.model_dump(mode="json") for key, value in documents.items()}})
        assert result["ok"] and not result["stub"]
        return result
    async def scenario():
        first = await build(assembly.project_id, ledgers)
        worker = runner._idle[0]
        repeated = await build(assembly.project_id, ledgers)
        assert repeated["volume_mm3"] == pytest.approx(first["volume_mm3"])
        edited = plate_ledger.model_dump(mode="json")
        edited["features"][0]["parameters"]["length"] = 120
        ledgers[plate_ledger.project_id] = Ledger.model_validate(edited)
        changed = await build(assembly.project_id, ledgers)
        assert changed["volume_mm3"] - first["volume_mm3"] == pytest.approx(20 * 100 * 20)
        other = Ledger.model_validate({"features": [{"id": "feat_other", "kind": "box",
            "parameters": {"length": 10, "width": 10, "height": 10}}]})
        assert (await build(other.project_id, {other.project_id: other}))["volume_mm3"] == pytest.approx(1000)
        restored = await build(assembly.project_id, {assembly.project_id: assembly, plate_ledger.project_id: plate_ledger})
        assert restored["volume_mm3"] == pytest.approx(first["volume_mm3"])
        assert runner._idle[0] is worker  # Correct results while the kernel/cache persists.
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
