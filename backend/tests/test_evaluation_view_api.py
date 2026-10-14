import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cadenza_backend import evaluation_view_api as api
from cadenza_backend.evaluation_observer import Observer


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(evaluation_view_root=tmp_path))
    output = tmp_path / "test-run"; output.mkdir()
    asyncio.run(Observer(output).publish("before", glb=b"actual-glb"))
    app = FastAPI(); app.include_router(api.router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        yield client


def test_manifest_and_only_referenced_models(client):
    response = client.get("/api/evaluations/test-run")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert client.get("/api/evaluations/test-run/models/1.glb").content == b"actual-glb"
    assert client.get("/api/evaluations/test-run/models/2.glb").status_code == 404
    assert client.get("/api/evaluations/test-run/models/request.json").status_code == 400
    assert client.post("/api/evaluations/test-run").status_code == 405


def test_access_and_disabled(client, monkeypatch):
    assert client.get("/api/evaluations/test-run", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/evaluations/test-run", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/api/evaluations/missing").status_code == 404
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(evaluation_view_root=None))
    assert client.get("/api/evaluations/test-run").status_code == 404


def test_corrupt_and_oversized(client, tmp_path):
    path = tmp_path / "test-run/observer/manifest.json"
    path.write_text('{"version":99}')
    assert client.get("/api/evaluations/test-run").status_code == 503
    path.write_bytes(b" " * (api.MAX_JSON + 1))
    assert client.get("/api/evaluations/test-run").status_code == 413


def test_resolved_escape(client, tmp_path, monkeypatch):
    from pathlib import Path
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **k: tmp_path.parent / "secret" if p.name == "manifest.json" else original(p, *a, **k))
    assert client.get("/api/evaluations/test-run").status_code == 403
