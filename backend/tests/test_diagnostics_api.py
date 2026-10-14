import io
import json
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cadenza_backend import diagnostics as d, diagnostics_api as api


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    store = d.DiagnosticStore(tmp_path / "managed")
    monkeypatch.setattr(d, "store", lambda: store)
    monkeypatch.setattr(api, "store", lambda: store)
    d.Trace("retained").log("request.begin", request={"type": "prompt", "text": "synthetic"})
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        yield client


def test_retained_session_download_without_websocket(local_client):
    response = local_client.get("/api/diagnostics/status")
    assert response.status_code == 200
    assert response.json()["sessions"][0]["session_id"] == "retained"
    response = local_client.post("/api/diagnostics/sessions/retained/export", json={})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert json.loads(archive.read("summary.json"))["incomplete_traces"]


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.test"}, {"Host": "evil.test"}, {"Origin": "null"}])
def test_unapproved_origin_and_host_rejected(local_client, headers):
    assert local_client.get("/api/diagnostics/status", headers=headers).status_code == 403


def test_limits_and_missing_sessions(local_client):
    base = "/api/diagnostics/sessions/retained/export"
    assert local_client.post(base, json={"path": "C:/private"}).status_code == 422
    assert local_client.post(base, content=b"x" * (256*1024+1)).status_code == 413
    assert local_client.post("/api/diagnostics/sessions/unknown/export", json={}).status_code == 404


def test_forwarded_loopback_does_not_authorize_remote_client():
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, base_url="http://localhost", client=("192.0.2.1", 12345)) as client:
        assert client.get("/api/diagnostics/status", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
