"""Gears through the agent layer: prompt -> ledger -> built revision.

The geometry package already proves the teeth are involutes and the solid is
valid (`geometry/tests/test_gear.py`). What is left to prove here is that the
server treats `gear` like every other feature kind — that it validates against
the contract, survives a patch, and comes back as real bytes — plus the one
thing that is genuinely new at this layer: a gear is the first feature whose
size is not a dimension you can read off the ledger, and the agents have to be
told how to reason about `module` x `teeth` instead.

The provider is faked throughout, as in `test_vision.py`. Whether Claude picks a
sensible module for "a 40 mm gear" is not a property this suite can assert; that
the schema and prompt give it what it needs to, is.
"""

from __future__ import annotations

import pytest

from cadenza_backend import geometry_bridge
from cadenza_backend.agents.draftsman import (
    DRAFTSMAN_OUTPUT_SCHEMA,
    _to_contract_features,
    DraftedFeature,
    draftsman_system,
)
from cadenza_backend.agents.machinist import MACHINIST_SYSTEM
from cadenza_backend.contracts_bridge import FEATURE_KINDS, Ledger
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.session import Session

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #


def test_gear_is_in_the_feature_vocabulary():
    assert "gear" in FEATURE_KINDS


def test_a_gear_feature_validates_against_the_contract():
    features = _to_contract_features(
        [
            DraftedFeature(
                kind="gear",
                name="Spur Gear",
                operation="add",
                parameters={"module": 2.0, "teeth": 20, "height": 10.0},
            )
        ]
    )
    (gear,) = features
    assert gear.kind == "gear"
    assert gear.parameters.teeth == 20
    # The two nobody supplies, defaulted rather than left absent.
    assert gear.parameters.pressure_angle_deg == 20.0
    assert gear.parameters.shift == 0.0


def test_the_contract_rejects_a_gear_that_could_never_mesh():
    from pydantic import ValidationError

    for params, why in [
        ({"module": 2.0, "teeth": 2, "height": 5.0}, "two teeth"),
        ({"module": 0.0, "teeth": 20, "height": 5.0}, "zero module"),
        ({"module": 2.0, "teeth": 20, "height": 5.0, "pressure_angle_deg": 90.0}, "90 degrees"),
    ]:
        with pytest.raises(ValidationError, match=r".*"):
            Ledger.model_validate(
                {
                    "features": [
                        {"id": "feat_g", "kind": "gear", "parameters": params},
                    ]
                }
            )


# --------------------------------------------------------------------------- #
# what the agents are told
# --------------------------------------------------------------------------- #


def test_the_draftsman_schema_offers_gear_and_its_parameters():
    items = DRAFTSMAN_OUTPUT_SCHEMA["properties"]["features"]["items"]
    assert "gear" in items["properties"]["kind"]["enum"]
    params = items["properties"]["parameters"]["properties"]
    assert params["teeth"]["type"] == "integer", "a fractional tooth count must not typecheck"
    assert "module" in params


@pytest.mark.parametrize("has_images", [False, True])
def test_the_draftsman_is_told_how_to_size_a_gear(has_images):
    """`module` is the one parameter no user ever says out loud, so the prompt
    has to carry the conversion from what they DO say."""
    prompt = draftsman_system(has_images=has_images).lower()
    assert "module * teeth" in prompt
    assert "module * (teeth + 2)" in prompt
    assert "mesh" in prompt


def test_a_spur_gear_is_no_longer_listed_as_unbuildable():
    """It was case (b) until the vocabulary grew. Leaving it there would make
    the Draftsman decline something it can now do."""
    prompt = draftsman_system(has_images=True).lower()
    assert "spur gears are case neither" in prompt
    # The gear forms still outside the vocabulary stay named.
    for unbuildable in ("helical", "bevel", "worm"):
        assert unbuildable in prompt


def test_the_machinist_knows_the_two_ways_to_resize_a_gear():
    """More teeth still meshes; a bigger module does not. Picking the wrong one
    silently breaks the gear train the part belongs to."""
    prompt = MACHINIST_SYSTEM.lower()
    assert "gear" in prompt
    assert "more teeth" in prompt
    assert "pressure_angle_deg" in prompt


# --------------------------------------------------------------------------- #
# the turn
# --------------------------------------------------------------------------- #


class FakeClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def complete_json(self, *, stage, system, user, schema=None, images=None, **kw):
        self.calls.append({"stage": stage, "system": system, "user": user})
        return self.payload


GEAR_AND_BORE = {
    "summary": "Built a 20-tooth module-2 spur gear, 44 mm across and 10 mm thick, on a Ø8 bore.",
    "features": [
        {
            "kind": "gear",
            "name": "Spur Gear",
            "operation": "add",
            "parameters": {"module": 2.0, "teeth": 20, "height": 10.0},
            "placement": {"origin": [0, 0, 0]},
        },
        {
            "kind": "hole",
            "name": "Axle Bore",
            "operation": "subtract",
            "parameters": {"diameter": 8.0, "through": True},
            "placement": {"origin": [0, 0, 10]},
        },
    ],
    "assumptions": [
        {
            "field": "module",
            "value": 2.0,
            "basis": "No tooth size given; module 2 is a common stock size.",
            "confidence": 0.5,
        }
    ],
}


async def _run(session: Session, client: FakeClient, prompt: str):
    frames: list = []
    binaries: list[bytes] = []

    async def emit(msg):
        frames.append(msg)

    async def emit_binary(data: bytes):
        binaries.append(data)

    await handle_prompt(
        session, "msg_1", prompt, None, emit, emit_binary=emit_binary, client=client
    )
    return frames, binaries


async def test_a_gear_prompt_becomes_a_built_revision():
    """The thing that used to be impossible: ask for a gear, get a gear."""
    session = Session()
    frames, binaries = await _run(session, FakeClient(GEAR_AND_BORE), "make me a 20 tooth gear")

    kinds = [f.type for f in frames]
    assert [f.type for f in frames if f.type == "error"] == [], [
        (f.type, getattr(f, "message", "")) for f in frames
    ]
    assert "ledger.updated" in kinds
    assert "geometry.ready" in kinds

    assert session.store.ledger.revision == 1
    assert [f.kind for f in session.store.ledger.features] == ["gear", "hole"]

    ready = next(f for f in frames if f.type == "geometry.ready")
    if geometry_bridge.REAL_GEOMETRY:
        assert ready.stub is False
        assert {b.artifact for b in ready.blobs} == {"glb", "step"}
        assert all(b.bytes > 0 for b in ready.blobs)
        assert len(binaries) == len(ready.blobs)


async def test_the_inferred_module_reaches_the_user_as_an_assumption():
    """Module is the number the user never gave and cannot check by eye, so it
    is exactly the kind that must not arrive silently."""
    session = Session()
    frames, _ = await _run(session, FakeClient(GEAR_AND_BORE), "make me a gear")

    message = next(f for f in frames if f.type == "agent.message")
    assert [a.field for a in message.assumptions] == ["module"]


async def test_an_undercut_gear_warns_the_user_without_failing():
    """11 teeth builds, but not into what a hobbed gear would look like. The
    build succeeds and the caveat travels with it."""
    payload = {
        "summary": "Built an 11-tooth module-2 spur gear.",
        "features": [
            {
                "kind": "gear",
                "name": "Pinion",
                "operation": "add",
                "parameters": {"module": 2.0, "teeth": 11, "height": 8.0},
                "placement": {"origin": [0, 0, 0]},
            }
        ],
        "assumptions": [],
    }
    session = Session()
    frames, _ = await _run(session, FakeClient(payload), "an 11 tooth pinion")

    assert [f.type for f in frames if f.type == "error"] == []
    assert session.store.ledger.revision == 1
    if geometry_bridge.REAL_GEOMETRY:
        ready = next(f for f in frames if f.type == "geometry.ready")
        assert any("undercut" in w for w in ready.warnings), ready.warnings


async def test_an_unbuildable_gear_fails_with_the_parameter_named():
    """A pointed-tooth gear is a design error, and the error has to say which
    number caused it — 'the build failed' is what an LLM cannot repair."""
    payload = {
        "summary": "Built a 5-tooth gear.",
        "features": [
            {
                "kind": "gear",
                "name": "Tiny",
                "operation": "add",
                "parameters": {"module": 2.0, "teeth": 5, "height": 5.0, "shift": 1.0},
                "placement": {"origin": [0, 0, 0]},
            }
        ],
        "assumptions": [],
    }
    session = Session()
    frames, _ = await _run(session, FakeClient(payload), "a 5 tooth gear")

    if geometry_bridge.REAL_GEOMETRY:
        failed = [f for f in frames if f.type in ("geometry.failed", "error")]
        assert failed, [f.type for f in frames]
        assert "point" in failed[0].message.lower()
        # Failed build => full rollback. ARCHITECTURE.md §6.
        assert session.store.ledger.revision == 0
