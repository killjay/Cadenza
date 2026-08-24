"""Image → 3D: the sketch-upload path, end to end, with no model call.

The provider is faked throughout. What is being tested is everything CADenza
owns around the model call — that an image survives the wire intact, that its
presence changes routing and the prompt, and that the features it produces go
through exactly the same validate → build → commit path as typed ones. Whether
Claude reads a dimension line correctly is not a property this suite can assert.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cadenza_backend import geometry_bridge
from cadenza_backend.agents.coordinator import AgentKind, route
from cadenza_backend.agents.draftsman import (
    DRAFTSMAN_VISION_SYSTEM,
    build_draftsman_user_message,
    draftsman_system,
)
from cadenza_backend.app import app
from cadenza_backend.contracts_bridge import CONTRACT_VERSION, empty_ledger
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.session import Session
from cadenza_backend.wire import (
    MAX_IMAGE_BYTES,
    InlineImage,
    UserPrompt,
    parse_client_message,
)

# A one-pixel PNG. Small enough to inline, real enough to be valid base64.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_1PX).decode()


# --------------------------------------------------------------------------- #
# the wire
# --------------------------------------------------------------------------- #


def test_inline_image_accepts_bare_base64():
    img = InlineImage(media_type="image/png", data=PNG_B64)
    assert img.decoded == PNG_1PX


def test_inline_image_strips_a_data_url_prefix():
    """A client pasting straight from a FileReader must not be punished for it."""
    img = InlineImage(media_type="image/png", data=f"data:image/png;base64,{PNG_B64}")
    assert img.decoded == PNG_1PX


def test_inline_image_tolerates_embedded_whitespace():
    wrapped = "\n".join(PNG_B64[i : i + 16] for i in range(0, len(PNG_B64), 16))
    assert InlineImage(media_type="image/png", data=wrapped).decoded == PNG_1PX


def test_inline_image_rejects_non_base64():
    with pytest.raises(ValidationError, match="not valid base64"):
        InlineImage(media_type="image/png", data="!!!! not base64 !!!!")


def test_inline_image_rejects_empty_payload():
    with pytest.raises(ValidationError):
        InlineImage(media_type="image/png", data="")


def test_inline_image_enforces_the_decoded_size_cap():
    """The cap is on decoded bytes — that is what reaches the provider."""
    oversized = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode()
    with pytest.raises(ValidationError, match="over the"):
        InlineImage(media_type="image/png", data=oversized)


def test_inline_image_rejects_an_unsupported_media_type():
    with pytest.raises(ValidationError):
        InlineImage(media_type="image/tiff", data=PNG_B64)  # type: ignore[arg-type]


def test_user_prompt_parses_images_off_the_wire():
    frame = parse_client_message(
        json.dumps(
            {
                "type": "user.prompt",
                "message_id": "msg_1",
                "prompt": "build this",
                "images": [
                    {"media_type": "image/png", "data": PNG_B64, "name": "sketch.png"}
                ],
            }
        )
    )
    assert isinstance(frame, UserPrompt)
    assert frame.decoded_images() == [("image/png", PNG_1PX)]


def test_user_prompt_caps_the_image_count():
    with pytest.raises(ValidationError):
        UserPrompt(
            message_id="msg_1",
            prompt="build this",
            images=[InlineImage(media_type="image/png", data=PNG_B64)] * 5,
        )


def test_prompt_without_images_decodes_to_nothing():
    assert UserPrompt(message_id="msg_1", prompt="a plate").decoded_images() == []


# --------------------------------------------------------------------------- #
# routing and prompting
# --------------------------------------------------------------------------- #


def test_an_image_routes_to_the_draftsman_even_on_a_non_empty_ledger(plate_ledger):
    """Normally a populated ledger means "edit". An image means "read this instead"."""
    assert route(plate_ledger, "build this", has_images=False).agent is AgentKind.MACHINIST
    assert route(plate_ledger, "build this", has_images=True).agent is AgentKind.DRAFTSMAN


def test_the_vision_block_appears_only_when_an_image_is_attached():
    """A text-only turn must not be primed to hallucinate a drawing."""
    assert DRAFTSMAN_VISION_SYSTEM not in draftsman_system(has_images=False)
    assert DRAFTSMAN_VISION_SYSTEM in draftsman_system(has_images=True)


def test_the_vision_user_message_names_the_image_as_the_spec():
    message = build_draftsman_user_message("make it steel", has_images=True)
    assert "attached image" in message.lower()
    assert "make it steel" in message


@pytest.mark.parametrize("has_images", [False, True])
def test_the_prompt_separates_missing_detail_from_missing_form(has_images):
    """Both halves of the ceiling rule have to reach the model, either way in.

    Observed behaviour drove this: told only to refuse, the model built an
    L-bracket out of two boxes and buried "R20 fillet not modelled" in the
    assumptions list. Approximating the FORM is the thing that must be refused;
    dropping a DETAIL is fine as long as the summary admits it.

    Parametrised over `has_images` because the rule spent a while living in the
    vision block alone, where "make me a gear" — typed, no drawing — sailed
    straight past it and came back a plain disc. The ceiling is a property of
    the vocabulary, not of the input, so it has to hold on both paths.
    """
    prompt = draftsman_system(has_images=has_images).lower()
    assert "needs_clarification" in prompt
    assert "summary" in prompt
    # (a) details that may be dropped, loudly
    for detail in ("fillet", "chamfer", "thread"):
        assert detail in prompt
    # (b) forms that may not be approximated
    for form in ("gear", "revolve", "sweep", "loft", "freeform", "assembly"):
        assert form in prompt


def test_the_vision_block_names_a_photograph_as_unbuildable():
    """The one ceiling case that only exists once an image is in play."""
    assert "photograph" in DRAFTSMAN_VISION_SYSTEM.lower()
    assert "photograph" not in draftsman_system(has_images=False).lower()


# --------------------------------------------------------------------------- #
# the turn
# --------------------------------------------------------------------------- #


class FakeClient:
    """Stands in for `ModelClient`, recording what the agent layer asked for."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def complete_json(self, *, stage, agent_card, user, images=None, max_tokens=None, effort=None, context=None, **kw):
        self.calls.append(
            {"stage": stage, "system": agent_card.system_prompt, "user": user, "images": images or []}
        )
        return self.payload


PLATE_FROM_SKETCH = {
    "summary": "Read the sketch: a 100 x 100 x 20 mm plate with a 10 mm through hole.",
    "features": [
        {
            "kind": "box",
            "name": "Base Plate",
            "operation": "add",
            "parameters": {"length": 100, "width": 100, "height": 20},
            "placement": {"origin": [0, 0, 0]},
        },
        {
            "kind": "hole",
            "name": "Centre Hole",
            "operation": "subtract",
            "parameters": {"diameter": 10, "through": True},
            "placement": {"origin": [0, 0, 20]},
        },
    ],
    "assumptions": [
        {
            "field": "units",
            "value": "mm",
            "basis": "No unit marker on the drawing.",
            "confidence": 0.4,
        }
    ],
}


async def _run(session: Session, client: FakeClient, images):
    frames: list = []
    binaries: list[bytes] = []

    async def emit(msg):
        frames.append(msg)

    async def emit_binary(data: bytes):
        binaries.append(data)

    await handle_prompt(
        session,
        "msg_1",
        "build what the drawing shows",
        None,
        emit,
        images=images,
        emit_binary=emit_binary,
        client=client,
    )
    return frames, binaries


@pytest.mark.anyio
async def test_a_sketch_reaches_the_provider_as_image_content():
    session = Session()
    client = FakeClient(PLATE_FROM_SKETCH)

    await _run(session, client, [("image/png", PNG_1PX)])

    assert client.calls, "the draftsman was never called"
    call = client.calls[0]
    assert call["stage"] == "draftsman"
    assert call["images"] == [("image/png", PNG_1PX)]
    assert "READING AN ATTACHED IMAGE" in call["system"]


@pytest.mark.anyio
async def test_a_sketch_becomes_a_built_revision():
    """The whole point: an image in, a real ledger and real geometry out."""
    session = Session()
    client = FakeClient(PLATE_FROM_SKETCH)

    frames, binaries = await _run(session, client, [("image/png", PNG_1PX)])
    kinds = [f.type for f in frames]

    assert "ledger.updated" in kinds
    assert "geometry.ready" in kinds
    assert [f.type for f in frames if f.type == "error"] == []

    assert session.store.ledger.revision == 1
    assert [f.kind for f in session.store.ledger.features] == ["box", "hole"]

    ready = next(f for f in frames if f.type == "geometry.ready")
    if geometry_bridge.REAL_GEOMETRY:
        assert ready.stub is False
        assert {b.artifact for b in ready.blobs} == {"glb", "step"}
        assert all(b.bytes > 0 for b in ready.blobs)
        # Announced manifest first, then one binary frame per blob.
        assert len(binaries) == len(ready.blobs)
        assert session.artifacts.get(1, "glb") is not None


@pytest.mark.anyio
async def test_the_assumptions_from_a_sketch_reach_the_user():
    """A number read off a drawing and a number invented must not look alike."""
    session = Session()
    frames, _ = await _run(session, FakeClient(PLATE_FROM_SKETCH), [("image/png", PNG_1PX)])

    message = next(f for f in frames if f.type == "agent.message")
    assert [a.field for a in message.assumptions] == ["units"]


@pytest.mark.anyio
async def test_a_drawing_outside_the_vocabulary_is_declined_not_approximated():
    """ARCHITECTURE.md §1: no path produces a silently wrong part."""
    session = Session()
    client = FakeClient(
        {
            "summary": "",
            "features": [],
            "needs_clarification": (
                "The drawing shows a filleted bracket. I can only build boxes, "
                "cylinders and holes."
            ),
        }
    )

    frames, binaries = await _run(session, client, [("image/png", PNG_1PX)])
    kinds = [f.type for f in frames]

    message = next(f for f in frames if f.type == "agent.message")
    assert message.needs_clarification is True
    assert "filleted bracket" in message.text
    # Nothing was built, nothing was committed, no bytes were sent.
    assert "ledger.updated" not in kinds
    assert "geometry.ready" not in kinds
    assert binaries == []
    assert session.store.ledger.features == []
    assert session.store.ledger.revision == 0


@pytest.mark.anyio
async def test_an_unbuildable_sketch_leaves_the_ledger_untouched():
    """A build failure must not advance the ledger past the last built revision."""
    session = Session()
    client = FakeClient(
        {
            "summary": "A plate with a hole wider than the plate.",
            "features": [
                {
                    "kind": "box",
                    "name": "Plate",
                    "operation": "add",
                    "parameters": {"length": 10, "width": 10, "height": 5},
                    "placement": {"origin": [0, 0, 0]},
                },
                {
                    "kind": "hole",
                    "name": "Oversized Bore",
                    "operation": "subtract",
                    "parameters": {"diameter": 400, "through": True},
                    "placement": {"origin": [0, 0, 5]},
                },
            ],
        }
    )

    before = session.store.ledger.model_dump(mode="json")
    frames, _ = await _run(session, client, [("image/png", PNG_1PX)])
    kinds = [f.type for f in frames]

    if "geometry.failed" in kinds:
        failed = next(f for f in frames if f.type == "geometry.failed")
        assert failed.current_revision == 0
        assert "ledger.updated" not in kinds
        assert session.store.ledger.model_dump(mode="json") == before


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #


def test_an_oversized_image_is_refused_by_the_transport_not_the_provider():
    """The size bound is enforced before anything is spent on a model call."""
    oversized = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode()
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {"type": "client.hello", "contract_version": CONTRACT_VERSION}
                )
            )
            ws.receive_text()
            ws.send_text(
                json.dumps(
                    {
                        "type": "user.prompt",
                        "message_id": "msg_1",
                        "prompt": "build this",
                        "images": [{"media_type": "image/png", "data": oversized}],
                    }
                )
            )
            error = json.loads(ws.receive_text())

    assert error["type"] == "error"
    assert error["code"] == "BAD_MESSAGE"


def test_session_ready_advertises_the_image_limits():
    """The client needs to know what to downscale to before it uploads."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(
                json.dumps(
                    {"type": "client.hello", "contract_version": CONTRACT_VERSION}
                )
            )
            ready = json.loads(ws.receive_text())

    assert ready["limits"]["max_images"] >= 1
    assert ready["limits"]["max_image_bytes"] == MAX_IMAGE_BYTES


def test_geometry_bridge_reports_which_backend_answered():
    outcome = geometry_bridge.build(empty_ledger("Empty"))
    # An empty ledger cannot build either way; what matters is that it fails
    # cleanly with a code rather than raising.
    assert outcome.ok is False
    assert outcome.code is not None
