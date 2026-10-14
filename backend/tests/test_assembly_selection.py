import asyncio
import time
from typing import cast

from fastapi.testclient import TestClient
from cadenza_backend import geometry_bridge, graph
from cadenza_backend.app import app
from cadenza_backend.session import Session, LedgerStore, registry
from cadenza_backend.wire import SpatialResult, Target
from cadenza_contracts.fixtures import plate_with_hole
from cadenza_contracts.ledger import Ledger


def test_source_context_reaches_backend_descriptor():
    part = plate_with_hole()
    root = Ledger.model_validate({"assembly": {"components": [{"id": "comp_plate", "sub_assembly_id": part.project_id,
        "placement": {"origin": [200, 0, 0]}}]}})
    hit = geometry_bridge.probe(root, [220, 20, 20], [0, 0, 1], {root.project_id: root, part.project_id: part})
    assert not hit.stub and hit.component_id == "comp_plate"
    assert hit.feature_id == part.features[0].id and hit.feature_kind == "box"
    assert part.project_id in hit.descriptor and "Source-local point" in hit.descriptor
    assert "every instance" in hit.descriptor


def test_zero_confidence_and_unknown_assembly_do_not_become_certain(monkeypatch):
    root = Ledger.model_validate({"assembly": {}})
    monkeypatch.setattr(geometry_bridge._service, "probe_point_legacy", lambda *args, **kwargs:
        {"node_id": "feat_test", "feature": "top_face", "confidence": 0.0, "component_id": "comp_test"})
    hit = geometry_bridge.probe(root, [0, 0, 0])
    assert hit.confidence == 0.0 and hit.component_id == "comp_test"
    monkeypatch.setattr(geometry_bridge._service, "probe_point_legacy", lambda *args, **kwargs: None)
    hit = geometry_bridge.probe(root, [0, 0, 0])
    assert hit.feature_id is None and hit.confidence == 0 and not hit.stub


def test_worker_graph_and_websocket_preserve_instance_context():
    part = plate_with_hole()
    root = Ledger.model_validate({"assembly": {"components": [{"id": "comp_plate", "sub_assembly_id": part.project_id,
        "placement": {"origin": [200, 0, 0]}}]}})
    session = Session(primary_ledger_id=root.project_id, stores={
        root.project_id: LedgerStore(root), part.project_id: LedgerStore(part)})
    registry.register(session)
    events = []
    async def emit(event):
        events.append(event)
    state = cast(graph.AgentState, {"session": session, "message_id": "msg_pick", "started": time.perf_counter(),
        "targets": [Target(point=[220, 20, 20], normal=[0, 0, 1])], "emit": emit, "prompt": "Change this part"})
    with TestClient(app) as client:
        output = asyncio.run(graph.setup_node(state))
        assert output["resolved_list"][0].component_id == "comp_plate"
        spatial = next(event for event in events if isinstance(event, SpatialResult))
        assert spatial.hit is not None and spatial.hit.component_id == "comp_plate"
        assert part.project_id in spatial.hit.descriptor
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": session.id})
            socket.receive_json()
            socket.send_json({"type": "spatial.probe", "message_id": "msg_pick", "point": [220, 20, 20]})
            result = socket.receive_json()
            assert result["type"] == "spatial.result"
            assert result["hit"]["component_id"] == "comp_plate"
            assert part.project_id in result["hit"]["descriptor"]
