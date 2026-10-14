from fastapi.testclient import TestClient
from cadenza_backend import app as transport
from cadenza_backend.session import registry


def test_capability_discovery_is_opt_in_and_read_only(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Read-only discovery cannot build or invoke an agent")
    monkeypatch.setattr(transport, "build_design", unexpected)
    session = registry.create("Capabilities")
    before = session.snapshot(), list(session.history), session.revision, len(session.artifacts)
    try:
        with TestClient(transport.app) as client, client.websocket_connect("/ws") as ws:
            ws.send_json(dict(type="client.hello", contract_version="1.0.0", session_id=session.id))
            assert ws.receive_json()["type"] == "session.ready"
            # Ping barrier: an old client receives no unsolicited capability frame.
            ws.send_json(dict(type="ping", t=1))
            assert ws.receive_json()["type"] == "pong"
            marker = object()
            monkeypatch.setattr(session, "preview", marker)
            monkeypatch.setattr(session, "draft", marker)
            ws.send_json(dict(type="design.capabilities", message_id="query"))
            response = ws.receive_json()
            assert response["type"] == "design.capabilities"
            assert response["message_id"] == "query" and response["session_id"] == session.id
            assert {item["kind"] for item in response["features"]} == {"box", "cylinder", "hole", "extrude"}
            assert session.preview is marker and session.draft is marker
            assert before == (session.snapshot(), list(session.history), session.revision, len(session.artifacts))
            monkeypatch.setattr(session, "preview", None)
            monkeypatch.setattr(session, "draft", None)
    finally:
        registry.drop(session.id)
