"""Stage A offline 3D lifecycle baseline using the real geometry service."""
import asyncio
import json
import math
from pathlib import Path

import pytest

from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.contracts_bridge import CadenzaError, Ledger, PatchOp
from cadenza_backend.design import accept_design, build_design, stage_operations
from cadenza_backend.edit_preview import decide_preview, preview_parameter, publish_preview
from cadenza_backend.projects import ProjectRepository
from cadenza_backend.session import LedgerStore, Session
from cadenza_contracts.messages import ParameterEdit, PreviewDecision
from cadenza_contracts.project import ProjectDocument

CORPUS = json.loads((Path(__file__).parent / "fixtures/three_d_workflows.json").read_text())


async def discard(_):
    pass


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Stage A must not invoke a model")
    monkeypatch.setattr(ModelClient, "complete_json", forbidden)


def seed(case_id):
    case = next(c for c in CORPUS["cases"] if c["id"] == case_id)
    ledgers = {key: Ledger.model_validate(CORPUS["ledgers"][key]) for key in case["sources"]}
    session = Session(primary_ledger_id=case["primary"], stores={key: LedgerStore(ledger=value) for key, value in ledgers.items()})
    staged = session.stage({})
    outcome = build_design(staged, session.primary_ledger_id)
    assert outcome.ok and not outcome.stub, outcome.detail
    asyncio.run(accept_design(session, staged, outcome, "msg_seed", discard))
    return session


def decide(session, action="apply"):
    assert session.preview is not None
    return PreviewDecision.model_validate(dict(message_id="msg_decide", base_revision=session.revision,
        preview_id=session.preview.id, action=action))


def accepted(session):
    return session.snapshot(), list(session.history), list(session.redo), len(session.artifacts), session.last_geometry


def without_revision(ledger):
    return ledger.model_dump(mode="json", exclude={"revision"})


@pytest.mark.parametrize("case_id", [c["id"] for c in CORPUS["cases"]])
def test_whole_design_baseline_and_repository_reopen(case_id, tmp_path):
    session = seed(case_id)
    case = next(c for c in CORPUS["cases"] if c["id"] == case_id)
    expected = case["expected"]
    assert session.last_geometry is not None
    assert session.last_geometry.stats.volume_mm3 == pytest.approx(expected["constant"] + math.pi * expected["pi_coefficient"])
    document = ProjectDocument(name=case_id, primary_ledger_id=session.primary_ledger_id,
        ledgers=session.snapshot(), history=session.history, redo=session.redo)
    path = tmp_path / "projects.sqlite3"
    project_id, version = ProjectRepository(path).save(document, None, 0)
    reopened, reopened_version = ProjectRepository(path).load(project_id)
    assert reopened_version == version and reopened == document
    rebuilt = build_design(reopened.ledgers, reopened.primary_ledger_id)
    assert rebuilt.ok and not rebuilt.stub, rebuilt.detail
    assert rebuilt.volume_mm3 == pytest.approx(expected["constant"] + math.pi * expected["pi_coefficient"])
    assert rebuilt.step is not None and rebuilt.step.startswith(b"ISO-10303-21")


def test_repeated_source_dimensions_update_four_instances_and_history():
    session = seed("fixture")
    original = session.snapshot()
    for diameter in (8, 4, 6):
        before = accepted(session)
        command = ParameterEdit(message_id="msg_pin", base_revision=session.revision,
            ledger_id="proj_pin", feature_id="feat_pin", parameter="diameter", value=diameter)
        asyncio.run(preview_parameter(session, command, discard))
        assert accepted(session) == before
        assert session.preview is not None
        assert session.preview.outcome.volume_mm3 == pytest.approx(76800 + math.pi * (-200 + 8 * diameter**2))
        asyncio.run(decide_preview(session, decide(session), discard))
        for key in original:
            if key != "proj_pin":
                assert without_revision(session.stores[key].ledger) == without_revision(original[key])
    undo = session.stage_undo()
    assert undo is not None
    ledgers, index = undo
    outcome = build_design(ledgers, session.primary_ledger_id)
    assert outcome.ok and outcome.volume_mm3 == pytest.approx(76800 - 72 * math.pi)
    asyncio.run(accept_design(session, ledgers, outcome, "msg_undo", discard, undo_index=index))
    redo = session.stage_redo()
    assert redo is not None
    outcome = build_design(redo, session.primary_ledger_id)
    assert outcome.ok and outcome.volume_mm3 == pytest.approx(76800 + 88 * math.pi)
    asyncio.run(accept_design(session, redo, outcome, "msg_redo", discard, redo=True))
    assert without_revision(session.stores["proj_pin"].ledger) == without_revision(original["proj_pin"])


def test_add_modify_remove_feature_preserves_other_parts():
    session = seed("independent_parts")
    originals = session.snapshot()
    addition = {"id": "feat_extra_bore", "kind": "hole", "parameters": {"diameter": 6, "through": True},
                "placement": {"origin": [-20, 10, 8]}}
    steps = [
        (PatchOp(op="add", ledger_id="proj_plate", path="/features/-", value=addition), -272),
        (PatchOp(op="replace", ledger_id="proj_plate", path="/features/feat_extra_bore/parameters/diameter", value=8), -328),
        (PatchOp(op="remove", ledger_id="proj_plate", path="/features/feat_extra_bore"), -200),
    ]
    for operation, coefficient in steps:
        before = accepted(session)
        _, staged = stage_operations(session, [operation])
        outcome = build_design(staged, session.primary_ledger_id)
        assert outcome.ok, outcome.detail
        assert outcome.volume_mm3 == pytest.approx(48000 + coefficient * math.pi)
        asyncio.run(publish_preview(session, staged, outcome, "msg_feature", discard))
        assert accepted(session) == before
        asyncio.run(decide_preview(session, decide(session), discard))
        for key in ("proj_shaft", "proj_spacer"):
            assert without_revision(session.stores[key].ledger) == without_revision(originals[key])
    assert without_revision(session.store.ledger) == without_revision(originals["proj_plate"])


def test_failed_secondary_and_missing_reference_preserve_accepted_state():
    session = seed("independent_parts")
    before = accepted(session)
    _, staged = stage_operations(session, [PatchOp(op="replace", ledger_id="proj_shaft",
        path="/features/feat_bore/parameters/diameter", value=200)])
    outcome = build_design(staged, session.primary_ledger_id)
    assert not outcome.ok
    assert accepted(session) == before and session.preview is None
    with pytest.raises(CadenzaError):
        stage_operations(session, [PatchOp(op="replace", ledger_id="proj_missing",
            path="/features/feat_bore/parameters/diameter", value=8)])
    assembly = seed("fixture")
    before = accepted(assembly)
    staged = assembly.stage({})
    del staged["proj_pin"]
    assert not build_design(staged, assembly.primary_ledger_id).ok
    assert accepted(assembly) == before


def test_cancel_and_stale_apply_never_commit_source_edit():
    session = seed("fixture")
    before = accepted(session)
    command = ParameterEdit(message_id="msg_pin", base_revision=session.revision,
        ledger_id="proj_pin", feature_id="feat_pin", parameter="diameter", value=8)
    asyncio.run(preview_parameter(session, command, discard))
    stale = decide(session)
    asyncio.run(decide_preview(session, decide(session, "cancel"), discard))
    with pytest.raises(CadenzaError):
        asyncio.run(decide_preview(session, stale, discard))
    assert accepted(session) == before
