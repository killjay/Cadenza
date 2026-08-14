"""WebSocket transport — spike 1. No model calls: every frame here is answered
by the server without reaching an agent.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from cadenza_backend import geometry_bridge
from cadenza_backend.app import app
from cadenza_backend.contracts_bridge import CONTRACT_VERSION


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
    assert ready["ledger"]["features"] == []
    assert ready["ledger"]["revision"] == 0


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
    assert fresh["ledger"]["metadata"]["name"] == "Bracket"
