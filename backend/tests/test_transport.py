"""WebSocket transport — spike 1. No model calls: every frame here is answered
by the server without reaching an agent.
"""

from __future__ import annotations

import json
import asyncio
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from cadenza_backend import geometry_bridge
from cadenza_backend.app import app
from cadenza_backend.contracts_bridge import CONTRACT_VERSION
from cadenza_backend.session import registry
from cadenza_backend.design import accept_design


async def _discard(_):
    pass


def _accept_plate(session, plate_ledger):
    plate = plate_ledger.model_copy(update={"project_id": session.primary_ledger_id})
    outcome = geometry_bridge.BuildOutcome(ok=True, detail="fixture", stub=False, glb=b"fixture-glb", step=b"fixture-step")
    asyncio.run(accept_design(session, session.stage({session.primary_ledger_id: plate}), outcome, "msg_fixture", _discard))


def _hello(ws, contract_version: str = CONTRACT_VERSION) -> dict:
    ws.send_text(json.dumps({"type": "client.hello", "contract_version": contract_version}))
    return json.loads(ws.receive_text())


def test_health_reports_configuration():
    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["contract_version"] == CONTRACT_VERSION
    # Whether build123d is importable decides which geometry backend answers, and
    # /health has to say which one, because "the part looks wrong" has a very
    # different cause in each case.
    if geometry_bridge.REAL_GEOMETRY:
        assert body["geometry"] == "build123d"
        assert "geometry build" not in body["stubbed"]
    else:
        assert "geometry build" in body["stubbed"]


def test_handshake_returns_session_and_empty_ledger():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ready = _hello(ws)

    assert ready["type"] == "session.ready"
    assert ready["session_id"].startswith("ses_")
    assert next(iter(ready["store"].values()))["features"] == []
    assert next(iter(ready["store"].values()))["revision"] == 0


def test_contract_major_mismatch_is_refused():
    """A client compiled against a different major must not be allowed to proceed."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            error = _hello(ws, contract_version="2.0.0")

    assert error["type"] == "error"
    assert error["code"] == "PROTOCOL_MISMATCH"
    assert error["recoverable"] is False


def test_ping_pong_round_trip():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            ws.send_text(json.dumps({"type": "ping", "t": 1.0}))
            pong = json.loads(ws.receive_text())

    assert pong["type"] == "pong"


def test_ledger_request_returns_current_state():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            ws.send_text(json.dumps({"type": "ledger.request", "message_id": "msg_1"}))
            frame = json.loads(ws.receive_text())

    assert frame["type"] == "ledger.updated"
    assert frame["message_id"] == "msg_1"
    assert frame["patch"] == []
    assert frame["revision"] == 0


def test_session_resumes_across_reconnect():
    """The session outlives its socket, so a dropped client keeps its part."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            session_id = _hello(ws)["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "client.hello",
                        "contract_version": CONTRACT_VERSION,
                        "session_id": session_id,
                    }
                )
            )
            resumed = json.loads(ws.receive_text())

    assert resumed["session_id"] == session_id


def test_spatial_probe_on_empty_ledger_reports_no_features():
    """`hit: null` means "nothing within tolerance" — a fact, not an error."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            ws.send_text(
                json.dumps(
                    {"type": "spatial.probe", "message_id": "msg_1", "point": [0, 0, 20]}
                )
            )
            frame = json.loads(ws.receive_text())

    assert frame["type"] == "spatial.result"
    assert frame["message_id"] == "msg_1"
    assert frame["hit"] is None


def test_unparseable_frame_is_reported_not_fatal():
    """A bad frame must not kill the socket — the next frame still works."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _hello(ws)
            ws.send_text(json.dumps({"type": "not.a.real.type"}))
            error = json.loads(ws.receive_text())

            ws.send_text(json.dumps({"type": "ping", "t": 1.0}))
            pong = json.loads(ws.receive_text())

    assert error["type"] == "error"
    assert error["code"] == "BAD_MESSAGE"
    assert pong["type"] == "pong"


def test_session_reset_yields_a_fresh_session():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            first = _hello(ws)["session_id"]
            ws.send_text(
                json.dumps(
                    {"type": "session.reset", "message_id": "msg_1", "name": "Bracket"}
                )
            )
            fresh = json.loads(ws.receive_text())

    assert fresh["type"] == "session.ready"
    assert fresh["session_id"] != first
    assert next(iter(fresh["store"].values()))["metadata"]["name"] == "Bracket"


def test_undo_to_empty_emits_new_revision_and_keeps_socket_open(plate_ledger):
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        session = registry.get(_hello(ws)["session_id"])
        _accept_plate(session, plate_ledger)
        ws.send_json({"type": "session.undo", "message_id": "msg_undo", "base_revision": 1})
        updated, geometry = ws.receive_json(), ws.receive_json()
        assert updated["revision"] == 2 and not updated["can_undo"]
        assert next(iter(updated["store"].values()))["features"] == []
        assert geometry["empty"] and not geometry["stub"] and geometry["blobs"] == []
        assert session.revision == 2 and not session.history
        ws.send_json({"type": "ping", "t": 7})
        assert ws.receive_json()["t"] == 7


def test_failed_undo_preserves_ledger_history_and_artifacts(monkeypatch, plate_ledger):
    import cadenza_backend.app as transport
    from cadenza_backend.contracts_bridge import ErrorCode
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        session = registry.get(_hello(ws)["session_id"])
        _accept_plate(session, plate_ledger)
        _accept_plate(session, plate_ledger)
        before, history, artifacts = session.snapshot(), list(session.history), len(session.artifacts)
        monkeypatch.setattr(transport, "build_design", AsyncMock(return_value=geometry_bridge.BuildOutcome(
            ok=False, detail="fixture failure", stub=False, code=ErrorCode.GEOMETRY_KERNEL_FAILURE)))
        ws.send_json({"type": "session.undo", "message_id": "msg_undo", "base_revision": 2})
        assert ws.receive_json()["type"] == "error"
        assert session.snapshot() == before and session.history == history
        assert session.revision == 2 and len(session.artifacts) == artifacts
        ws.send_json({"type": "ping", "t": 8})
        assert ws.receive_json()["t"] == 8


def test_stale_edit_is_rejected_before_agent_call(monkeypatch):
    from cadenza_backend import orchestrator
    async def unexpected(*a, **k):
        raise AssertionError("stale request reached an agent")
    monkeypatch.setattr(orchestrator, "handle_prompt", unexpected)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        _hello(ws)
        ws.send_json({"type": "user.prompt", "message_id": "msg_stale", "prompt": "change height", "base_revision": 99})
        assert ws.receive_json()["code"] == "REVISION_CONFLICT"
        ws.send_json({"type": "ping", "t": 9})
        assert ws.receive_json()["t"] == 9


def test_assembly_prompt_discovery_and_locked_revision_routing(monkeypatch):
    from cadenza_backend import app as transport
    handler = AsyncMock()
    monkeypatch.setattr(transport, "handle_assembly_prompt", handler)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        hello = _hello(ws)
        session = registry.get(hello["session_id"])
        assert session is not None
        ws.send_json(dict(type="design.assembly.prompt.capabilities", message_id="e3cap_transport",
            base_revision=0, ledger_id=session.primary_ledger_id, owner_path=[]))
        response = ws.receive_json()
        assert response["type"] == "design.assembly.prompt.capabilities" and response["version"] == 1
        assert response["session_id"] == session.id and response["owner_path"] == []
        request = dict(type="design.assembly.prompt", message_id="e3_transport", base_revision=99,
            ledger_id=session.primary_ledger_id, owner_path=[], scope="assembly_definition", target=dict(kind="definition"), prompt="Add a part")
        ws.send_json(request)
        assert ws.receive_json()["code"] == "REVISION_CONFLICT"
        handler.assert_not_called()
        async def check_lock(current, frame, emit):
            assert current.lock.locked()
        handler.side_effect = check_lock
        ws.send_json({**request, "base_revision": 0})
        ws.send_json(dict(type="ping", t=42))
        assert ws.receive_json()["t"] == 42
        handler.assert_awaited_once()


def test_reconnect_replays_accepted_geometry_and_download(plate_ledger):
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            sid = _hello(ws)["session_id"]
            _accept_plate(registry.get(sid), plate_ledger)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "client.hello", "contract_version": CONTRACT_VERSION, "session_id": sid})
            ready, geometry = ws.receive_json(), ws.receive_json()
            assert ready["revision"] == geometry["revision"] == 1
            assert ready["can_undo"]
            from cadenza_contracts.messages import decode_blob_frame
            header, data = decode_blob_frame(ws.receive_bytes())
            assert header.revision == 1 and data == b"fixture-glb"
            ws.receive_bytes()
            response = client.get(geometry["blobs"][0]["url"])
            assert response.status_code == 200 and response.content == b"fixture-glb"
            ws.send_json({"type": "session.reset", "message_id": "msg_reset"})
            fresh = ws.receive_json()
            assert fresh["session_id"] != sid and fresh["revision"] == 0
            assert not fresh["can_undo"]
