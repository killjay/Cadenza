"""Milestone 1, end to end over the real WebSocket. Costs money; not part of pytest.

    .venv/bin/python scripts/milestone_smoke.py

  1. "a 100x100x20 plate with a 10 mm hole in the middle"  -> ledger exists
  2. click [10.5, 0, 20] + "make this twice as thick"      -> valid patch applied

Uses TestClient, which drives the ASGI app in-process — same code path as a real
uvicorn server, no port needed.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from cadenza_backend.app import app
from cadenza_backend.contracts_bridge import CONTRACT_VERSION

TERMINAL = {"error"}


def pump(ws, label: str) -> dict:
    """Read frames until the turn ends. Returns the last ledger seen."""
    print(f"\n--- {label} ---")
    ledger: dict | None = None
    while True:
        frame = json.loads(ws.receive_text())
        kind = frame["type"]

        if kind == "agent.status":
            print(f"  [{frame['phase']:<17}] {frame['detail']}")
        elif kind == "spatial.result":
            print(f"  [spatial          ] {frame['semantic_context'][:110]}")
        elif kind == "agent.message":
            print(f"  [agent            ] {frame['text']}")
            for a in frame.get("assumptions", []):
                print(f"      assumption: {a['field']} = {a['value']}  ({a['basis'][:60]})")
        elif kind == "ledger.updated":
            ledger = frame["ledger"]
            print(f"  [ledger           ] {len(ledger['features'])} feature(s); patch:")
            for op in frame["patch"]:
                print(f"      {json.dumps(op)}")
        elif kind == "geometry.ready":
            print(f"  [geometry         ] revision={frame['revision']} bbox={frame['bbox']} stub={frame['stub']}")
        elif kind == "geometry.failed":
            print(f"  [geometry FAILED  ] {frame['message']}")
        elif kind in TERMINAL:
            print(f"  [ERROR {frame['code']}] {frame['message']}")
            if frame.get("detail"):
                print(f"      {frame['detail']}")
            return ledger or {}

        if kind == "agent.status" and frame["phase"] == "done":
            return ledger or {}


def main() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "client.hello", "contract_version": CONTRACT_VERSION}))
            ready = json.loads(ws.receive_text())
            print(f"session {ready['session_id']}  contract {ready['contract_version']}")

            # ── step 1: create the part from a description ──────────────────
            ws.send_text(
                json.dumps(
                    {
                        "type": "user.prompt",
                        "prompt": "a 100x100x20 plate with a 10 mm hole in the middle",
                    }
                )
            )
            ledger = pump(ws, "STEP 1  create")

            features = ledger.get("features", [])
            print("\n  features:")
            for f in features:
                print(f"    {f['id']:<34} {f['kind']:<9} {f['operation']:<9} {f['parameters']}  @ {f['placement']['origin']}")

            plate = next((f for f in features if f["kind"] == "box"), None)
            hole = next((f for f in features if f["kind"] == "hole"), None)
            assert plate, "no plate was created"
            step1_ok = (
                plate["parameters"]["length"] == 100
                and plate["parameters"]["width"] == 100
                and plate["parameters"]["height"] == 20
                and hole is not None
                and hole["parameters"]["diameter"] == 10
            )
            print(f"\n  STEP 1: {'PASS' if step1_ok else 'CHECK — dimensions differ from the ask'}")

            # ── step 2: point & speak ───────────────────────────────────────
            ws.send_text(
                json.dumps(
                    {
                        "type": "user.prompt",
                        "prompt": "make this twice as thick",
                        "target": {"point": [10.5, 0.0, 20.0]},
                    }
                )
            )
            ledger = pump(ws, "STEP 2  point & speak")

            plate_after = next(f for f in ledger["features"] if f["kind"] == "box")
            height = plate_after["parameters"]["height"]
            print(f"\n  plate height 20 -> {height}")
            print(f"  STEP 2: {'PASS' if height == 40 else f'FAIL (got {height})'}")


if __name__ == "__main__":
    main()
