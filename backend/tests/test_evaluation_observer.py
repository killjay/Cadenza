import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from typing import Any, cast

import pytest
from cadenza_backend.evaluation_observer import Observer, Manifest
from cadenza_backend import design_evaluation as evaluation


def test_order_and_preserved_decisions(tmp_path):
    async def run():
        observer = Observer(tmp_path)
        await observer.publish("before", glb=b"before", step="case/0", prompt="Change height", target="part")
        await observer.publish("proposal", glb=b"candidate")
        assert observer.current
        await observer.publish("decision", accepted=observer.current.before, detail="Recorded action: cancel")
        await observer.finish("completed", {"estimated_cost_usd": "0.01"})
    asyncio.run(run())
    manifest = Manifest.model_validate_json((tmp_path / "observer/manifest.json").read_bytes())
    assert manifest.sequence == 4 and manifest.estimated_usd == .01
    assert manifest.checkpoints[-1].accepted == "1.glb"
    assert (tmp_path / "observer/2.glb").read_bytes() == b"candidate"


def test_storage_failure_does_not_escape(tmp_path, monkeypatch):
    observer = Observer(tmp_path)
    monkeypatch.setattr(observer, "_write", lambda *a: (_ for _ in ()).throw(PermissionError()))
    asyncio.run(observer.publish("before", glb=b"test"))
    assert observer.failed and observer.manifest.observer_error
    asyncio.run(observer.finish("completed"))


@pytest.mark.parametrize("route,requested,expected", [
    ("source", "part", "part"), ("assembly", "module", "root"),
])
def test_initial_snapshot_and_no_provider(tmp_path, monkeypatch, route, requested, expected):
    from cadenza_backend import geometry_jobs
    snapshot = {"part": object(), "module": object(), "root": object()}
    session = SimpleNamespace(primary_ledger_id="root", stores=snapshot, snapshot=lambda: snapshot)
    build = AsyncMock(return_value=SimpleNamespace(ok=True, stub=False, glb=b"view"))
    monkeypatch.setattr(geometry_jobs, "build_design", build)
    observer = Observer(tmp_path)
    asyncio.run(observer.begin(session, SimpleNamespace(route=route), {"prompt": "edit", "ledger_id": requested}, "case/0"))
    build.assert_awaited_once_with(snapshot, expected)
    assert observer.current and observer.current.accepted == "2.glb"
    assert observer.current.target == expected


@pytest.mark.parametrize("route,primary,expected", [
    ("part", "root", "New part"), ("part", None, "New part"), ("assembly", None, "None"),
])
def test_no_starting_geometry(tmp_path, monkeypatch, route, primary, expected):
    from cadenza_backend import geometry_jobs
    build = AsyncMock()
    monkeypatch.setattr(geometry_jobs, "build_design", build)
    session = SimpleNamespace(primary_ledger_id=primary, stores={"module": object(), "root": object()})
    observer = Observer(tmp_path)
    asyncio.run(observer.begin(session, SimpleNamespace(route=route), {"ledger_id": "module"}, "case/0"))
    build.assert_not_awaited()
    assert observer.current and observer.current.target == expected
    assert observer.current.before is None and observer.current.accepted is None


@pytest.mark.parametrize("outcome,response", [
    ("clarification", dict(kind="clarification", question="Which position?")),
    ("unsupported", dict(kind="unsupported", reason="Unsupported edit")),
])
def test_no_candidate_preserves_root_display_target(tmp_path, monkeypatch, outcome, response):
    from cadenza_backend import geometry_jobs
    monkeypatch.setattr(geometry_jobs, "build_design", AsyncMock(return_value=SimpleNamespace(ok=True, stub=False, glb=b"root-view")))
    monkeypatch.setattr(evaluation, "ModelClient", lambda **kw: pytest.fail("No SDK allowed"))
    corpus, contexts, _ = evaluation.load()
    case = next(c for c in corpus.cases if c.id == "AS03" and c.variant == "base")
    context = contexts[case.context]
    session = evaluation.Session(primary_ledger_id=context["primary"], stores={
        k: evaluation.LedgerStore(evaluation.Ledger.model_validate(v)) for k, v in context["ledgers"].items()})
    observer = Observer(tmp_path)
    step = case.steps[0].model_copy(update={"outcome": outcome})
    result = asyncio.run(evaluation.run_step(session, step, response, tmp_path, evaluation.FIXTURES, observer=observer))
    assert result["outcome"] == outcome and result["state_preserved"]
    assert observer.current and observer.current.target == "root"
    assert observer.current.accepted == observer.current.before and observer.current.proposal is None


def test_observed_offline_clarification_and_no_sdk(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "ModelClient", lambda **kw: pytest.fail("No SDK allowed"))
    result = asyncio.run(evaluation.evaluate(tmp_path / "observed", case_id="NP03", observe_view=True))
    assert all(row["status"] == "passed" for row in result["results"])
    data = json.loads((tmp_path / "observed/observer/manifest.json").read_text())
    assert data["status"] == "completed"
    assert any(c["phase"] == "clarification" for c in data["checkpoints"])


def test_invalid_name_rejected_before_output(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(evaluation.evaluate(tmp_path / "bad name", case_id="NP03", observe_view=True))
    assert not (tmp_path / "bad name").exists()


def test_observer_storage_failure_does_not_change_evaluation(tmp_path, monkeypatch):
    monkeypatch.setattr(Observer, "_write", lambda *a: (_ for _ in ()).throw(PermissionError()))
    result = asyncio.run(evaluation.evaluate(tmp_path / "failed-observation", case_id="NP03", observe_view=True))
    assert all(row["status"] == "passed" for row in result["results"])
    assert (tmp_path / "failed-observation/results.json").exists()


def test_failed_step_keeps_candidate_checkpoint(tmp_path, monkeypatch):
    async def failing(*args, observer=None, **kwargs):
        assert observer is not None
        await observer.publish("proposal", glb=b"failed-candidate")
        raise AssertionError("Candidate bounds mismatch")
    monkeypatch.setattr(evaluation, "_run_step", failing)
    observer = Observer(tmp_path)
    with pytest.raises(AssertionError):
        asyncio.run(evaluation.run_step(cast(Any, None), cast(Any, None), {}, tmp_path, tmp_path, observer=observer))
    manifest = Manifest.model_validate_json((tmp_path / "observer/manifest.json").read_bytes())
    assert manifest.checkpoints[-1].phase == "failed"
    assert manifest.checkpoints[-1].proposal == "1.glb"
    assert manifest.checkpoints[-1].accepted is None
