"""Cold startup has its own bound; execution, reuse and cleanup keep theirs."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from cadenza_backend import geometry_jobs as jobs
from cadenza_contracts.messages import Limits


READY = {"protocol": 1, "real_geometry": True, "imports_ms": 0.0}


def command(prefix="", ready=READY):
    return (sys.executable, "-c", f"""
import json, pathlib, sys, time, os
{prefix}
path = pathlib.Path(sys.argv[2])
temporary = path.with_suffix('.tmp')
temporary.write_text({json.dumps(json.dumps(ready))})
temporary.replace(path)
for line in sys.stdin.buffer:
    source, target = json.loads(line)
    request = json.loads(pathlib.Path(source).read_text())
    time.sleep(request.get('sleep', 0))
    path = pathlib.Path(target)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({{'ok': True, 'pid': os.getpid()}}))
    temporary.replace(path)
""")


def test_readiness_read_recovers_before_single_dispatch(monkeypatch, caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    original_read = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        if path.name == "ready.json":
            reads.append(path)
            assert path.exists()
            if len(reads) == 1: raise PermissionError(13, "PRIVATE_PAYLOAD", "PRIVATE_PATH")
        return original_read(path, *args, **kwargs)
    runner = jobs.GeometryJobRunner(1, 1, command(), persistent=True, startup_timeout_s=5)
    async def scenario():
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", read)
            first = await runner.run({})
        assert len(reads) == 2 and len(set(reads)) == 1
        worker = runner._idle[0]
        assert worker.jobs == 1
        assert (await runner.run({}))["pid"] == first["pid"]
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    records = [r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics")]
    assert records[0]["status"] == "success" and records[0]["readiness_read_retries"] == 1
    assert records[0]["readiness_read_denial"]["errno"] == 13
    assert "readiness_read_retries" not in records[1]  # Warm worker skips startup.
    assert "PRIVATE_PAYLOAD" not in caplog.text and "PRIVATE_PATH" not in caplog.text


@pytest.mark.parametrize("fault", ["persistent", "deadline", "cancel", "exit_zero", "crash", "malformed"])
def test_readiness_recovery_failure_never_dispatches(monkeypatch, caplog, fault):
    from unittest.mock import Mock
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = jobs.GeometryJobRunner(1, 1, command(), persistent=True, startup_timeout_s=5)
    original_read, original_checkout = Path.read_text, runner._checkout
    workers, reads, pipes = [], [], []
    def checkout(*args):
        worker = original_checkout(*args)
        workers.append(worker)
        pipe = Mock(wraps=worker.process.stdin)
        worker.process.stdin = pipe
        pipes.append(pipe)
        return worker
    async def scenario():
        loop = asyncio.get_running_loop()
        original_time = loop.time
        def read(path, *args, **kwargs):
            if path.name == "ready.json":
                reads.append(path)
                if fault == "malformed" and len(reads) > 1: return "[]"
                if fault == "deadline": monkeypatch.setattr(loop, "time", lambda: original_time() + 10)
                if fault in ("exit_zero", "crash"):
                    process = workers[0].process
                    if fault == "crash": process.kill()
                    else:
                        assert process.stdin is not None
                        process.stdin.close()
                    process.wait()
                if fault == "cancel":
                    task = asyncio.current_task()
                    assert task is not None
                    loop.call_soon(task.cancel)
                raise PermissionError(13, "PRIVATE_PAYLOAD", "PRIVATE_PATH")
            return original_read(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(runner, "_checkout", checkout)
            patch.setattr(Path, "read_text", read)
            try:
                with pytest.raises(asyncio.CancelledError if fault == "cancel" else jobs.GeometryJobError) as error:
                    await runner.run({})
                if fault == "deadline":
                    assert isinstance(error.value, jobs.GeometryJobError)
                    assert error.value.code.value == "GEOMETRY_TIMEOUT"
                    assert isinstance(error.value.__cause__, PermissionError)
            finally:
                monkeypatch.setattr(loop, "time", original_time)
        assert pipes and pipes[0].write.call_count == 0
        assert not runner._workers and not runner._idle
        assert workers[0].process.poll() is not None
        assert reads and all(not path.parent.exists() for path in reads)
        assert (await runner.run({}))["ok"]
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    first = next(r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics"))
    assert first["phase"] == "startup" and "execution_ms" not in first
    assert first["failure"]["step"] == ("readiness_parse" if fault == "malformed" else "readiness_read")
    assert first["readiness_read_retries"] <= 10
    assert "PRIVATE_PAYLOAD" not in caplog.text and "PRIVATE_PATH" not in caplog.text


def test_slow_start_then_warm_execution_and_recycling(caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = jobs.GeometryJobRunner(1, 0.5, command("time.sleep(1)"), persistent=True,
                                    startup_timeout_s=5, max_jobs=2)
    async def scenario():
        first = await runner.run({})
        worker = runner._idle[0]
        second = await runner.run({})
        assert first["pid"] == second["pid"] and worker.process.poll() is not None
        third = await runner.run({})
        assert third["pid"] != first["pid"]
        with pytest.raises(jobs.GeometryJobError, match="deadline"):
            await runner.run({"sleep": 60})
        assert not runner._workers
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    records = [r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics")]
    assert [r["status"] for r in records] == ["success", "success", "success", "GEOMETRY_TIMEOUT"]
    assert records[0]["startup_ms"] >= 1000 and records[2]["startup_ms"] >= 1000
    assert records[1]["startup_ms"] == records[3]["startup_ms"] == 0
    assert records[3]["phase"] == "execution"


@pytest.mark.parametrize("fault", ["timeout", "crash", "cancel", "shutdown"])
def test_startup_faults_reap_and_release_capacity(fault, caplog, monkeypatch):
    from unittest.mock import Mock
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = jobs.GeometryJobRunner(1, 1, command("os._exit(7)" if fault == "crash" else "time.sleep(60)"),
        persistent=True, startup_timeout_s=5 if fault == "crash" else 0.4)
    original_checkout, pipes = runner._checkout, []
    def checkout(*args):
        worker = original_checkout(*args)
        pipe = Mock(wraps=worker.process.stdin)
        worker.process.stdin = pipe
        pipes.append(pipe)
        return worker
    monkeypatch.setattr(runner, "_checkout", checkout)
    async def scenario():
        task = asyncio.create_task(runner.run({}))
        while not runner._workers:
            await asyncio.sleep(0.01)
        process = next(iter(runner._workers)).process
        assert isinstance(process.args, list) and isinstance(process.args[-1], str)
        ready_path = Path(process.args[-1])
        if fault == "cancel": task.cancel()
        if fault == "shutdown": runner.close()
        expected = asyncio.CancelledError if fault == "cancel" else jobs.GeometryJobError
        with pytest.raises(expected) as error:
            await task
        if fault == "timeout":
            assert isinstance(error.value, jobs.GeometryJobError)
            assert error.value.code.value == "GEOMETRY_TIMEOUT"
        assert process.poll() is not None and not ready_path.parent.exists()
        assert not runner._workers and not runner._idle
        runner.command = command()
        runner.startup_timeout_s = 5
        assert (await runner.run({}))["ok"]
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    records = [r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics")]
    assert records[0]["phase"] == "startup" and records[0]["startup_ms"] >= 0
    assert "execution_ms" not in records[0]
    assert records[0]["worker_jobs"] == 0
    assert pipes[0].write.call_count == 0
    assert records[0]["failure"]["step"] == "readiness_wait"
    if fault in ("timeout", "cancel"):
        assert records[0]["worker_exit_code_before_cleanup"] is None
        assert records[0]["worker_exit_code"] is not None
    elif fault == "crash":
        assert records[0]["worker_exit_code_before_cleanup"] == records[0]["worker_exit_code"] == 7
    else:
        assert records[0]["worker_exit_code_before_cleanup"] == records[0]["worker_exit_code"]


@pytest.mark.parametrize("ready", [[], {}, {**READY, "protocol": 2}, {**READY, "protocol": True},
    {**READY, "real_geometry": "yes"}, {**READY, "imports_ms": -1}, {**READY, "imports_ms": float("inf")},
    {**READY, "extra": "unexpected"}])
def test_malformed_readiness_never_receives_a_job(ready, caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = jobs.GeometryJobRunner(1, 1, command(ready=ready), persistent=True, startup_timeout_s=5)
    async def scenario():
        with pytest.raises(jobs.GeometryJobError) as error:
            await runner.run({})
        assert error.value.code.value == "GEOMETRY_KERNEL_FAILURE"
        assert not runner._workers
    try:
        asyncio.run(scenario())
    finally:
        runner.close()
    record = next(r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics"))
    assert record["failure"]["step"] == "readiness_parse"
    assert record["status"] == "GEOMETRY_KERNEL_FAILURE" and record["phase"] == "startup"


def test_stub_readiness_is_explicit(caplog):
    caplog.set_level("INFO", logger=jobs.__name__)
    runner = jobs.GeometryJobRunner(1, 1, command(ready={**READY, "real_geometry": False}), persistent=True)
    try:
        asyncio.run(runner.run({}))
    finally:
        runner.close()
    record = next(r.geometry_metrics for r in caplog.records if hasattr(r, "geometry_metrics"))
    assert record["real_geometry"] is False


def test_legacy_limits_default_startup_and_reject_invalid_allowance():
    assert Limits.model_validate({"build_timeout_s": 20}).startup_timeout_s == 60
    for value in (0, -1, 601, float("inf"), float("nan")):
        with pytest.raises(ValueError): Limits(startup_timeout_s=value)


def test_runner_deadlines_must_be_finite_and_positive():
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError): jobs.GeometryJobRunner(timeout_s=value)
        with pytest.raises(ValueError): jobs.GeometryJobRunner(startup_timeout_s=value)
