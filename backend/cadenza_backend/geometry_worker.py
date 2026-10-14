"""Private subprocess entry point; no sessions, database writes or provider calls."""
import base64
from dataclasses import asdict
from functools import lru_cache
import json
from pathlib import Path
import sys
from time import perf_counter


@lru_cache(maxsize=1)
def initialize():
    from cadenza_contracts.ledger import Ledger
    from cadenza_backend import geometry_bridge
    from cadenza_backend.design import build_design
    return Ledger, geometry_bridge, build_design


def execute(request: dict) -> dict:
    started = perf_counter()
    Ledger, geometry_bridge, build_design = initialize()

    imported = perf_counter()
    result = _operate(request, Ledger, geometry_bridge, build_design)
    result["_timings"] = {"imports_ms": (imported - started) * 1000,
                          "operation_ms": (perf_counter() - imported) * 1000}
    return result


def _operate(request, Ledger, geometry_bridge, build_design) -> dict:
    ledgers = {lid: Ledger.model_validate(value) for lid, value in request["ledgers"].items()}
    primary = request["primary_id"]
    if request["operation"] == "build":
        result = asdict(build_design(ledgers, primary))
        for key in ("glb", "step"):
            result[key] = base64.b64encode(result[key]).decode("ascii") if result[key] is not None else None
        return result
    if request["operation"] == "probe":
        return asdict(geometry_bridge.probe(ledgers[primary], request["point"], request["normal"], ledgers))
    if request["operation"] == "inspect":
        return geometry_bridge.run_geometry_probe(ledgers[primary], request["query"], ledgers)
    raise ValueError("Unsupported geometry operation")


def publish(target: str, response: dict) -> None:
    result = Path(target)
    temporary = result.with_suffix(".tmp")
    temporary.write_text(json.dumps(response, allow_nan=False), encoding="utf-8")
    temporary.replace(result)  # Publish only complete responses to the polling parent.


def run_files(source: str, target: str) -> None:
    try:
        response = execute(json.loads(Path(source).read_text(encoding="utf-8")))
    except Exception as exc:
        response = {"error": f"Geometry worker failed: {type(exc).__name__}: {exc}"}
    publish(target, response)


if __name__ == "__main__":
    if sys.argv[1] == "--serve":
        started = perf_counter()
        _, bridge, _ = initialize()
        publish(sys.argv[2], {"protocol": 1, "real_geometry": bridge.REAL_GEOMETRY,
                              "imports_ms": (perf_counter() - started) * 1000})
        for line in sys.stdin.buffer:
            source, target = json.loads(line)
            run_files(source, target)
    else:
        run_files(sys.argv[1], sys.argv[2])
