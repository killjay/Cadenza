"""Run from the workspace: python backend/scripts/benchmark_geometry.py --repeats 3.

JSON on stdout; no providers, sessions or project database. Default: pooled
workers. Use --fresh for the previous one-process-per-job baseline.
"""
import argparse
import asyncio
import json
import logging
import platform
from statistics import median

from cadenza_backend.geometry_jobs import GeometryJobRunner, logger
from cadenza_contracts.ledger import Ledger


def workloads():
    base = {"id": "feat_base", "kind": "box", "name": "Plate",
            "parameters": {"length": 100, "width": 100, "height": 20}}
    simple = Ledger.model_validate({"features": [base]})
    holes = [{"id": f"feat_hole_{i}", "kind": "hole", "name": "Hole",
              "parameters": {"diameter": 8, "through": True},
              "placement": {"origin": [x, y, 20]}}
             for i, (x, y) in enumerate(((-25, -25), (-25, 25), (25, -25), (25, 25)))]
    plate = Ledger.model_validate({"features": [base, *holes]})
    assembly = Ledger.model_validate({"assembly": {"components": [
        {"id": f"comp_plate_{i}", "sub_assembly_id": plate.project_id,
         "placement": {"origin": [i * 120, 0, 0]}} for i in range(4)]}})
    for name, primary, dependencies in (("box", simple, []), ("four_holes", plate, []),
                                        ("four_component_assembly", assembly, [plate])):
        request = {"operation": "build", "primary_id": primary.project_id,
                   "ledgers": {p.project_id: p.model_dump(mode="json") for p in [primary, *dependencies]}}
        yield name, request
    yield "plate_probe", {"operation": "probe", "primary_id": plate.project_id,
                          "ledgers": {plate.project_id: plate.model_dump(mode="json")},
                          "point": [10, 10, 20], "normal": [0, 0, 1]}


class Measurements(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        if hasattr(record, "geometry_metrics"):
            self.rows.append(dict(getattr(record, "geometry_metrics")))


async def benchmark(repeats, fresh=False):
    capture = Measurements()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    runner = GeometryJobRunner(persistent=not fresh)
    samples = []
    try:
        for name, request in workloads():
            for repeat in range(repeats):
                result = await runner.run(request)
                if result.get("stub") or result.get("ok") is False or "error" in result:
                    raise RuntimeError(f"{name}: real geometry benchmark failed")
                if request["operation"] == "build" and not (result.get("glb") and result.get("step")):
                    raise RuntimeError(f"{name}: missing artifacts")
                if request["operation"] == "probe" and not result.get("feature_id"):
                    raise RuntimeError(f"{name}: missing selection")
                samples.append({"workload": name, "repeat": repeat + 1, **capture.rows[-1]})
    finally:
        runner.close()
        logger.removeHandler(capture)
        logger.setLevel(previous)
    summary = {name: {key: round(median(s[key] for s in samples if s["workload"] == name), 2)
                      for key in ("imports_ms", "operation_ms", "total_ms")}
               for name, _ in workloads()}
    worker_states = {}
    for reused in (False, True):
        durations = [s["total_ms"] for s in samples if s["worker_reused"] == reused]
        if durations:
            worker_states["reused" if reused else "new"] = {"count": len(durations),
                "median_total_ms": round(median(durations), 2)}
    return {"python": platform.python_version(), "platform": platform.platform(),
            "mode": "fresh" if fresh else "pooled",
            "worker_states": worker_states, "median_ms": summary, "samples": samples}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    print(json.dumps(asyncio.run(benchmark(args.repeats, args.fresh)), indent=2))
