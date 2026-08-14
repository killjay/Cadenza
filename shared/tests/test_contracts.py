"""Contract self-tests. `uv run pytest` from cadenza/shared/.

These pin the behaviour the three workstreams rely on. If one of these breaks,
somebody changed a contract without telling anybody.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cadenza_contracts import (
    BBox,
    BlobDescriptor,
    BuildStats,
    CadenzaError,
    EntityRef,
    ErrorCode,
    GeometryReady,
    LedgerUpdated,
    PatchOp,
    SpatialHit,
    SpatialResult,
    Target,
    UserPrompt,
    apply_patch,
    compatible,
    dump_message,
    empty_ledger,
    new_message_id,
    parse_client_message,
    parse_server_message,
)
from cadenza_contracts.fixtures import (
    DRAFTSMAN_PATCH,
    HOLE_ID,
    PLATE_ID,
    THICKEN_PATCH,
    plate_with_hole,
)
from cadenza_contracts.messages import BlobHeader, decode_blob_frame, encode_blob_frame


# --------------------------------------------------------------------------- #
# milestone 1, start to finish
# --------------------------------------------------------------------------- #


def test_draftsman_patch_builds_the_milestone_ledger():
    built = apply_patch(empty_ledger(), DRAFTSMAN_PATCH)
    assert [f.id for f in built.features] == [PLATE_ID, HOLE_ID]
    assert built.metadata.name == "Base Plate"
    assert built.features[0].parameters.height == 20.0
    assert built.features[1].parameters.diameter == 10.0
    # revision is server-owned: apply_patch never touches it
    assert built.revision == 0


def test_thicken_is_a_single_field_patch_and_does_not_mutate_the_input():
    led = plate_with_hole()
    thick = apply_patch(led, THICKEN_PATCH)
    assert thick.features[0].parameters.height == 40.0
    assert led.features[0].parameters.height == 20.0
    # the through-hole needs no patch of its own — this is the whole point of
    # the "origin.z is ignored for through holes" rule
    assert thick.features[1].parameters == led.features[1].parameters


# --------------------------------------------------------------------------- #
# malformed patches — one row per line of the CONTRACTS.md table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code,ops",
    [
        (
            ErrorCode.PATCH_UNKNOWN_TARGET,
            [PatchOp(op="replace", path="/features/feat_nope_9999/parameters/height", value=1)],
        ),
        (ErrorCode.PATCH_FORBIDDEN_PATH, [PatchOp(op="replace", path="/revision", value=99)]),
        (
            ErrorCode.PATCH_FORBIDDEN_PATH,
            [PatchOp(op="replace", path="/features/0/id", value="feat_x_0001")],
        ),
        (
            ErrorCode.PATCH_FORBIDDEN_PATH,
            [PatchOp(op="replace", path="/metadata/global_units", value="in")],
        ),
        (
            ErrorCode.LEDGER_INVALID,
            [PatchOp(op="replace", path="/features/0/parameters/height", value=-5)],
        ),
        (
            ErrorCode.LEDGER_INVALID,
            [PatchOp(op="replace", path=f"/features/{HOLE_ID}/parameters/through", value=False)],
        ),
        (
            ErrorCode.PATCH_APPLY_FAILED,
            [PatchOp(op="replace", path="/features/9/parameters/height", value=5)],
        ),
        (ErrorCode.PATCH_INVALID, [PatchOp(op="replace", path="features/0", value=5)]),
        (
            ErrorCode.PATCH_INVALID,
            [PatchOp(op="replace", path="/metadata/name", value="x")] * 33,
        ),
    ],
)
def test_bad_patch_maps_to_its_error_code(code, ops):
    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_with_hole(), ops)
    assert exc.value.code is code


def test_removing_the_only_additive_feature_is_rejected():
    led = plate_with_hole()
    with pytest.raises(CadenzaError) as exc:
        apply_patch(led, [PatchOp(op="remove", path=f"/features/{PLATE_ID}")])
    assert exc.value.code is ErrorCode.LEDGER_INVALID


# --------------------------------------------------------------------------- #
# the wire
# --------------------------------------------------------------------------- #


def test_client_message_round_trip():
    msg = UserPrompt(
        message_id=new_message_id(),
        prompt="make this twice as thick",
        target=Target(point=[10.5, 0.0, 20.0], normal=[0.0, 0.0, 1.0]),
        base_revision=1,
    )
    assert parse_client_message(dump_message(msg)) == msg


def test_unknown_message_type_is_rejected():
    with pytest.raises(ValidationError):
        parse_client_message('{"type": "user.nope"}')


def test_server_message_round_trip():
    led = plate_with_hole(height=40.0, revision=2)
    for msg in (
        LedgerUpdated(revision=2, ledger=led, patch=THICKEN_PATCH, summary="ok"),
        GeometryReady(
            revision=2,
            blobs=[
                BlobDescriptor(
                    artifact="glb",
                    content_type="model/gltf-binary",
                    bytes=12,
                    sha256="ab" * 32,
                )
            ],
            stats=BuildStats(
                bbox=BBox(min=[-50, -50, 0], max=[50, 50, 40]),
                volume_mm3=396_858.4,
                face_count=8,
                build_ms=120,
            ),
        ),
        SpatialResult(
            message_id="msg_x",
            hit=SpatialHit(
                entity=EntityRef(kind="face", index=5, revision=2),
                point=[10.5, 0.0, 40.0],
                normal=[0.0, 0.0, 1.0],
                distance_mm=0.002,
                feature_id=PLATE_ID,
                feature_name="Main Body",
                feature_kind="box",
                descriptor="the top face of 'Main Body' (a box), facing +Z, 100.0 x 100.0 mm",
                area_mm2=10_000.0,
            ),
        ),
    ):
        assert parse_server_message(dump_message(msg)) == msg


def test_blob_frame_round_trip():
    payload = b"glTF\x02\x00\x00\x00" + b"\xde\xad\xbe\xef" * 64
    frame = encode_blob_frame(BlobHeader(revision=2, artifact="glb", bytes=0), payload)
    header, out = decode_blob_frame(frame)
    assert out == payload
    assert header.artifact == "glb"
    assert header.bytes == len(payload)  # encode fixes up the declared length


@pytest.mark.parametrize("bad", [b"", b"\x00\x00\x00", b"\x00\x00\x00\x05abc"])
def test_malformed_blob_frame_raises(bad):
    with pytest.raises(ValueError):
        decode_blob_frame(bad)


def test_version_gate():
    assert compatible("1.9.3")
    assert not compatible("2.0.0")
    assert not compatible("garbage")
