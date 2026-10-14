"""Bounded, killable geometry workers. Only the parent accepts design state."""
from __future__ import annotations

import atexit
import asyncio
import base64
from dataclasses import dataclass
import errno
import json
import logging
import math
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cadenza_backend.config import get_settings
from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode, Ledger
from cadenza_backend.geometry_bridge import BuildOutcome, ProbeOutcome

logger = logging.getLogger(__name__)


class GeometryJobError(CadenzaError):
    pass


class _Readiness(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: int = Field(ge=1, le=1)
    real_geometry: bool
    imports_ms: float = Field(ge=0, allow_inf_nan=False)


@dataclass(eq=False)
class _Worker:
    process: subprocess.Popen
    jobs: int = 0
    project: str | None = None


class GeometryJobRunner:
    def __init__(self, capacity: int = 2, timeout_s: float = 20,
                 command: tuple[str, ...] | None = None, *,
                 persistent: bool | None = None, max_jobs: int = 100, startup_timeout_s: float = 60):
        if (capacity < 1 or max_jobs < 1 or any(not math.isfinite(value) or value <= 0
                                             for value in (timeout_s, startup_timeout_s))):
            raise ValueError("Worker limits must be positive")
        self.slots = threading.BoundedSemaphore(capacity)
        self.timeout_s = timeout_s
        self.startup_timeout_s = startup_timeout_s
        self.command = command or (sys.executable, "-m", "cadenza_backend.geometry_worker")
        self.persistent = command is None if persistent is None else persistent
        self.max_jobs = max_jobs
        self._guard = threading.Lock()
        self._idle: list[_Worker] = []
        self._workers: set[_Worker] = set()

    @staticmethod
    def _stop(worker: _Worker) -> None:
        process = worker.process
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdin:
            process.stdin.close()

    def close(self) -> None:
        """Reap every child. Call after draining requests at application shutdown."""
        with self._guard:
            for worker in self._workers:
                self._stop(worker)
            self._workers.clear()
            self._idle.clear()

    def _checkout(self, project: str | None, source: Path, result: Path) -> _Worker:
        with self._guard:
            # Prune exited idle workers before selecting an affinity/cache hit.
            for worker in self._idle[:]:
                if worker.process.poll() is not None:
                    self._stop(worker)
                    self._idle.remove(worker)
                    self._workers.discard(worker)
            if self._idle:
                worker = next((w for w in reversed(self._idle) if w.project == project), self._idle[-1])
                self._idle.remove(worker)
                return worker
            args = [*self.command, "--serve", str(source.parent / "ready.json")] if self.persistent else [*self.command, str(source), str(result)]
            process = subprocess.Popen(args, stdin=subprocess.PIPE if self.persistent else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            worker = _Worker(process)
            self._workers.add(worker)
            return worker

    async def _wait_ready(self, worker: _Worker, path: Path, deadline: float,
                          metrics: dict[str, Any]) -> _Readiness:
        metrics["step"] = "readiness_wait"
        while not path.exists():
            if worker.process.poll() is not None:
                raise GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "Geometry worker exited during startup.")
            if asyncio.get_running_loop().time() >= deadline:
                raise GeometryJobError(ErrorCode.GEOMETRY_TIMEOUT,
                    f"Geometry startup exceeded the {self.startup_timeout_s:g} second deadline.")
            await asyncio.sleep(0.02)
        metrics["step"] = "readiness_read"
        raw = await self._read_published(worker, path, deadline, metrics, startup=True)
        metrics["step"] = "readiness_parse"
        ready = _Readiness.model_validate_json(raw)
        if asyncio.get_running_loop().time() >= deadline:
            raise GeometryJobError(ErrorCode.GEOMETRY_TIMEOUT, "Geometry startup deadline exceeded.")
        if worker.process.poll() is not None:
            raise GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "Geometry worker exited during startup.")
        return ready

    def _release(self, worker: _Worker, healthy: bool, project: str | None) -> None:
        with self._guard:
            if worker not in self._workers:
                return  # Already reaped by shutdown.
            worker.jobs += 1
            worker.project = project
            if healthy and self.persistent and worker.jobs < self.max_jobs and worker.process.poll() is None:
                self._idle.append(worker)
            else:
                try:
                    self._stop(worker)
                finally:
                    # Pipe close can fail after the process was already reaped.
                    if worker.process.poll() is not None:
                        self._workers.discard(worker)

    async def _read_published(self, worker: _Worker, path: Path, deadline: float,
                              metrics: dict[str, Any], *, startup: bool = False) -> str:
        loop = asyncio.get_running_loop()
        stage = "readiness_read" if startup else "result_read"
        timeout_message = "Geometry startup deadline exceeded." if startup else "Geometry deadline exceeded."
        denial: PermissionError | None = None
        recovery_started = 0.0
        try:
            while True:
                now = loop.time()
                if now >= deadline:
                    raise GeometryJobError(ErrorCode.GEOMETRY_TIMEOUT, timeout_message) from denial
                exit_code = worker.process.poll()
                if (exit_code is not None if startup else exit_code not in (None, 0)):
                    raise GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "Geometry worker exited.") from denial
                if denial is not None:
                    if now >= recovery_started + 0.2 or metrics[f"{stage}_retries"] >= 10:
                        raise denial
                    metrics[f"{stage}_retries"] += 1
                try:
                    raw = path.read_text(encoding="utf-8")
                except PermissionError as exc:
                    if exc.errno != errno.EACCES:
                        raise
                    if denial is None:
                        recovery_started = loop.time()
                        metrics[f"{stage}_retries"] = 0
                        metrics[f"{stage}_denial"] = self._failure(exc, stage)
                    denial = exc
                    remaining = min(deadline, recovery_started + 0.2) - loop.time()
                    if remaining > 0 and metrics[f"{stage}_retries"] < 10:
                        await asyncio.sleep(min(0.02, remaining))
                    continue
                if loop.time() >= deadline:
                    raise GeometryJobError(ErrorCode.GEOMETRY_TIMEOUT, timeout_message) from denial
                return raw
        finally:
            if denial is not None:
                metrics[f"{stage}_recovery_ms"] = (loop.time() - recovery_started) * 1000

    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        started = perf_counter()
        operation = request.get("operation")
        metrics: dict[str, Any] = {"operation": operation if operation in ("build", "probe", "inspect") else "unknown",
            "job_id": uuid4().hex, "step": "admission", "startup_timeout_s": self.startup_timeout_s,
            "timeout_s": self.timeout_s}
        if not self.slots.acquire(blocking=False):
            self._record(metrics, started, "BUSY")
            raise GeometryJobError(ErrorCode.BUSY, "All geometry workers are busy. Try again shortly.")
        worker = None
        directory = None
        healthy = False
        status = "GEOMETRY_KERNEL_FAILURE"
        project = request.get("primary_id")
        failure: BaseException | None = None
        response: dict[str, Any] = {}
        phase_started = perf_counter()
        try:
            metrics["step"] = "directory_create"
            directory = TemporaryDirectory(prefix="cadenza_geometry_")
            source, result = Path(directory.name) / "request.json", Path(directory.name) / "result.json"
            metrics["step"] = "request_write"
            source.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
            deadline = asyncio.get_running_loop().time() + self.timeout_s
            startup_deadline = asyncio.get_running_loop().time() + self.startup_timeout_s
            phase_started = perf_counter()
            metrics.update(phase="startup" if self.persistent else "execution", startup_ms=0.0, step="checkout")
            worker = self._checkout(project, source, result)
            metrics.update(worker_reused=worker.jobs > 0, worker_pid=worker.process.pid, worker_jobs=worker.jobs)
            process = worker.process
            if self.persistent:
                if worker.jobs == 0:
                    try:
                        ready = await self._wait_ready(worker, source.parent / "ready.json", startup_deadline, metrics)
                        metrics.update(imports_ms=ready.imports_ms, real_geometry=ready.real_geometry)
                    finally:
                        metrics["startup_ms"] = (perf_counter() - phase_started) * 1000
                metrics["phase"] = "execution"
                phase_started = perf_counter()
                deadline = asyncio.get_running_loop().time() + self.timeout_s
                assert process.stdin is not None
                metrics["step"] = "pipe_dispatch"
                # Only a small path message enters the pipe; ledger/artifacts stay in files.
                process.stdin.write((json.dumps([str(source), str(result)]) + "\n").encode("utf-8"))
                process.stdin.flush()
            metrics["step"] = "result_wait"
            while (not result.exists() if self.persistent else process.poll() is None):
                if process.poll() is not None:
                    raise GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "Geometry worker exited.")
                if asyncio.get_running_loop().time() >= deadline:
                    raise GeometryJobError(ErrorCode.GEOMETRY_TIMEOUT,
                        f"Geometry exceeded the {self.timeout_s:g} second deadline.")
                await asyncio.sleep(0.02)
            if asyncio.get_running_loop().time() >= deadline:
                raise GeometryJobError(ErrorCode.GEOMETRY_TIMEOUT, "Geometry deadline exceeded.")
            if process.poll() not in (None, 0) or not result.exists():
                raise GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE,
                    "Geometry worker exited without a valid result.")
            metrics["step"] = "result_read"
            raw = await self._read_published(worker, result, deadline, metrics)
            metrics["step"] = "result_parse"
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "Invalid geometry worker response.")
            if "error" in response:
                raise GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE, response["error"])
            timings = response.pop("_timings", {})
            if not isinstance(timings, dict) or any(
                key not in ("imports_ms", "operation_ms") or type(value) not in (int, float)
                or not math.isfinite(value) or value < 0 for key, value in timings.items()
            ):
                raise ValueError("Invalid geometry worker timings")
            timings["imports_ms"] = metrics.get("imports_ms", 0.0) + timings.get("imports_ms", 0.0)
            metrics.update(timings)
            if asyncio.get_running_loop().time() >= deadline:
                raise GeometryJobError(ErrorCode.GEOMETRY_TIMEOUT, "Geometry deadline exceeded.")
            status = "failed" if response.get("ok") is False else "success"
            healthy = status == "success"
        except BaseException as exc:
            # Defer rethrow until both cleanup steps run; neither may replace this cause.
            failure = exc
            metrics["failure"] = self._failure(exc, metrics["step"])
        finally:
            if metrics.get("phase") == "execution":
                metrics["execution_ms"] = (perf_counter() - phase_started) * 1000
            if worker is not None:
                try:
                    metrics["worker_exit_code_before_cleanup"] = worker.process.poll()
                except Exception:
                    pass  # Missing observation must not replace the outcome or skip cleanup.
            for step in ("worker_release", "directory_cleanup"):
                try:
                    if step == "worker_release" and worker is not None:
                        self._release(worker, healthy, project)
                    elif step == "directory_cleanup" and directory is not None:
                        directory.cleanup()
                except BaseException as exc:
                    detail = self._failure(exc, step)
                    if failure is None:
                        failure = exc
                        metrics["failure"] = detail
                    else:
                        metrics.setdefault("cleanup_failures", []).append(detail)
            if worker is not None:
                metrics["worker_exit_code"] = worker.process.poll()
            if failure is not None:
                status = (failure.code.value if isinstance(failure, GeometryJobError) else
                          "cancelled" if isinstance(failure, asyncio.CancelledError) else "GEOMETRY_KERNEL_FAILURE")
            self.slots.release()
            self._record(metrics, started, status)
        if isinstance(failure, (OSError, ValueError)):
            raise GeometryJobError(ErrorCode.GEOMETRY_KERNEL_FAILURE,
                "Could not execute the geometry job.") from failure
        if failure is not None:
            raise failure
        return response

    @staticmethod
    def _failure(exc: BaseException, step: str) -> dict[str, Any]:
        # No exception text, request data, absolute paths or frame locals in diagnostics.
        chain = []
        cause: BaseException | None = exc
        while cause is not None and len(chain) < 4:
            item: dict[str, Any] = {"type": type(cause).__name__}
            for key in ("errno", "winerror"):
                value = getattr(cause, key, None)
                if isinstance(value, int):
                    item[key] = value
            frames = []
            tb = cause.__traceback__
            while tb is not None:
                frames.append({"file": Path(tb.tb_frame.f_code.co_filename).name,
                               "function": tb.tb_frame.f_code.co_name, "line": tb.tb_lineno})
                tb = tb.tb_next
            item["frames"] = frames[-8:]
            chain.append(item)
            cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
        detail = chain.pop(0)
        detail["step"] = step
        if chain:
            detail["causes"] = chain
        return detail

    @staticmethod
    def _record(metrics: dict[str, Any], started: float, status: str) -> None:
        metrics.update(total_ms=(perf_counter() - started) * 1000, status=status)
        try:
            from cadenza_backend.diagnostics import CURRENT, event
            trace = CURRENT.get()
            if trace:
                metrics.update(trace_id=trace.id, session_id=trace.session_id, message_id=trace.message_id)
                event("geometry.job", **metrics)
            emit = logger.warning if "failure" in metrics else logger.info
            emit("geometry_job %s", json.dumps(metrics), extra={"geometry_metrics": metrics})
        except Exception:
            pass  # Diagnostics must never change the geometry outcome.


runner = GeometryJobRunner(get_settings().geometry_max_workers, get_settings().geometry_timeout_s,
                           max_jobs=get_settings().geometry_worker_max_jobs,
                           startup_timeout_s=get_settings().geometry_startup_timeout_s)
atexit.register(runner.close)


def _snapshot(ledgers: dict[str, Ledger]) -> dict[str, Any]:
    return {lid: ledger.model_dump(mode="json") for lid, ledger in ledgers.items()}


async def build_design(ledgers: dict[str, Ledger], primary_id: str) -> BuildOutcome:
    from cadenza_backend.diagnostics import event, snapshot
    candidate = await asyncio.to_thread(snapshot, ledgers)
    event("geometry.candidate", primary_id=primary_id, snapshot=candidate)
    try:
        result = await runner.run({"operation": "build", "ledgers": _snapshot(ledgers), "primary_id": primary_id})
        for key in ("glb", "step"):
            result[key] = base64.b64decode(result[key], validate=True) if result[key] is not None else None
        if result.get("code") is not None:
            result["code"] = ErrorCode(result["code"])
        event("geometry.outcome", ok=result.get("ok"), code=result.get("code"), primary_id=primary_id)
        return BuildOutcome(**result)
    except GeometryJobError as exc:
        event("geometry.outcome", ok=False, code=exc.code, primary_id=primary_id)
        return BuildOutcome(ok=False, stub=False, detail=exc.message, code=exc.code)


async def probe(ledger: Ledger, point: list[float] | None, normal: list[float] | None = None,
                all_ledgers: dict[str, Ledger] | None = None) -> ProbeOutcome:
    result = await runner.run({"operation": "probe", "ledgers": _snapshot(all_ledgers or {ledger.project_id: ledger}),
        "primary_id": ledger.project_id, "point": point, "normal": normal})
    return ProbeOutcome(**result)


async def inspect(ledger: Ledger, query: dict, all_ledgers: dict[str, Ledger]) -> dict:
    return await runner.run({"operation": "inspect", "ledgers": _snapshot(all_ledgers),
        "primary_id": ledger.project_id, "query": query})
