"""The JSON ledger — CADenza's single source of truth.

INVARIANTS (enforced here, relied on everywhere):

  * Units are millimetres, angles degrees. Always. No other unit reaches the wire.
  * Coordinates are right-handed, Z-up (build123d native, glTF is Y-up — the
    exporter converts; the ledger never does).
  * `features` is an ORDERED array; order == build order and matters for booleans.
  * `revision` is monotonic and is bumped ONLY by the server, and ONLY when a
    patch applied AND the geometry rebuilt successfully. A revision that exists
    has been built. Agents may not patch it (see patch.FORBIDDEN_PATHS).
  * No topological ids (face_7, edge_12) are EVER stored. Selection is by
    coordinate — see TargetRef. This is how we sidestep the topological naming
    problem (blueprint section 7).
  * Every numeric field is a literal number. There is no expression language in
    milestone 1 — "width = length - 2*inset" is deferred, deliberately.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cadenza_contracts.ids import new_project_id

SCHEMA_VERSION = 1
MAX_FEATURES = 64

Vec3 = Annotated[list[float], Field(min_length=3, max_length=3)]

FEATURE_ID_PATTERN = r"^feat_[a-z0-9_]{1,60}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #


class Metadata(_Model):
    name: str = "Untitled Part"
    workspace_mode: Literal["3D"] = "3D"  # "2D" is out of scope for milestone 1
    global_units: Literal["mm"] = "mm"
    # Present in the blueprint, deliberately inert in milestone 1. Nullable so
    # that wiring them up later is additive, not a schema break.
    manufacturing_intent: str | None = None
    tolerance_class: str | None = None


# --------------------------------------------------------------------------- #
# shared feature parts
# --------------------------------------------------------------------------- #


class Placement(_Model):
    """Where the feature's local frame sits in model space (mm, degrees).

    Conventions, per feature kind — the translator (geometry/) implements these
    exactly and nothing else may reinterpret them:

      box       origin is the CENTRE of the footprint at the BOTTOM face.
                The solid spans [-length/2, +length/2] x [-width/2, +width/2]
                x [0, height] relative to origin.
      cylinder  origin is the CENTRE of the bottom circular face; axis +Z.
      gear      as cylinder: the CENTRE of the bottom face, axis +Z. Tooth 0 is
                centred on +X, so a gear and a cylinder of the same nominal size
                occupy the same place and `rotation_deg` about Z indexes the
                teeth by the angle a user would expect.
      hole      origin is the point where the axis pierces the face it is cut
                from; the hole is cut along the frame's -Z.

                *** For `through=True` holes, origin.z is IGNORED. ***
                A through hole is cut by a cylinder spanning the current
                solid's bounding box along its axis, plus margin. This is what
                makes "make the plate twice as thick" a ONE-field patch: the
                hole re-derives its own extent at every rebuild and can never
                end up stranded inside a thicker plate.

    `rotation_deg` is intrinsic XYZ (roll, pitch, yaw) applied about `origin`.
    Milestone 1 only ever emits [0, 0, 0]; it exists so that rotated features
    are not a schema change.
    """

    origin: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation_deg: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    relative_to: str | None = Field(default=None, pattern=FEATURE_ID_PATTERN)


class TargetRef(_Model):
    """Coordinate-based targeting — the "laser pointer" of blueprint section 7.

    Recorded when a feature was created or edited by clicking on the model. The
    geometry service re-resolves it against the CURRENT solid at every rebuild,
    so it survives topology churn. It is provenance and re-resolution input —
    never a topological id.
    """

    mode: Literal["coordinate"] = "coordinate"
    point: Vec3
    normal: Vec3 | None = None
    reference_feature: str | None = Field(default=None, pattern=FEATURE_ID_PATTERN)


class Assumption(_Model):
    """One value the agent inferred rather than read. Rendered as a badge.

    Stolen wholesale from draftsmith: assuming is fine, hiding is not.
    """

    field: str                       # JSON pointer, e.g. "/features/0/parameters/height"
    value: Any
    basis: str                       # "Plates in this size range are typically 20 mm"
    confidence: float = Field(ge=0.0, le=1.0)


class AIContext(_Model):
    original_intent: str | None = None
    assumptions: list[Assumption] = Field(default_factory=list)


class _FeatureBase(_Model):
    id: str = Field(pattern=FEATURE_ID_PATTERN)
    name: str = ""
    suppressed: bool = False
    placement: Placement = Field(default_factory=Placement)
    target: TargetRef | None = None
    ai_context: AIContext = Field(default_factory=AIContext)


# --------------------------------------------------------------------------- #
# parameters — all mm, all strictly positive
# --------------------------------------------------------------------------- #

Positive = Annotated[float, Field(gt=0.0)]


class BoxParameters(_Model):
    length: Positive  # along +X
    width: Positive   # along +Y
    height: Positive  # along +Z
    through: bool = False
    draft_angle: float = 0.0  # Taper angle in degrees


class CylinderParameters(_Model):
    diameter: Positive
    height: Positive  # along +Z
    through: bool = False
    draft_angle: float = 0.0  # Taper angle in degrees


class HoleParameters(_Model):
    diameter: Positive
    through: bool = True
    depth: Positive | None = None  # required when through is False, ignored when True

    @model_validator(mode="after")
    def _depth_rules(self) -> HoleParameters:
        if not self.through and self.depth is None:
            raise ValueError("a blind hole (through=false) requires `depth`")
        return self


class GearParameters(_Model):
    """A standard involute spur gear.

    `module` and `teeth` are the two numbers that define a gear; everything else
    a machinist would quote is derived from them, and the derivations live in
    `cadenza_geometry.gear` rather than here so this stays a data contract:

        pitch diameter = module * teeth
        outer diameter = module * (teeth + 2)      (before profile shift)

    That second identity is the one users actually reach for — "a 40 mm gear"
    almost always means the outer diameter — so the agent layer converts and
    records the conversion as an assumption rather than the schema guessing.

    `clearance` is deliberately NOT a field. It is the manufacturing gap under
    the mating tooth's tip, fixed at the standard 0.25 x module; exposing it
    would be a fourth number for a model to get wrong in exchange for a value
    almost nobody sets.
    """

    module: Positive                      # mm of pitch diameter per tooth
    teeth: int = Field(ge=3, le=400)
    height: Positive                      # face width, along +Z
    pressure_angle_deg: float = Field(default=20.0, ge=5.0, le=45.0)
    # Profile shift, in modules. Positive fattens the tooth root, which is how
    # a gear below the undercut limit is made without undercut.
    shift: float = Field(default=0.0, ge=-1.0, le=1.0)


class ConeParameters(_Model):
    bottom_diameter: Positive
    top_diameter: Positive
    height: Positive


class SphereParameters(_Model):
    diameter: Positive


class TorusParameters(_Model):
    major_diameter: Positive
    minor_diameter: Positive


class EdgeFilletParameters(_Model):
    length: Positive
    radius: Positive


class EdgeChamferParameters(_Model):
    length: Positive
    width: Positive


class LinearPatternParameters(_Model):
    target_feature: str = Field(pattern=FEATURE_ID_PATTERN)
    count: int = Field(ge=2)
    spacing: float
    axis: Vec3 = Field(default_factory=lambda: [1.0, 0.0, 0.0])


class CircularPatternParameters(_Model):
    target_feature: str = Field(pattern=FEATURE_ID_PATTERN)
    count: int = Field(ge=2)
    radius: Positive
    sweep_angle: float = 360.0


Vec2 = tuple[float, float]

class SketchParameters(_Model):
    vertices: list[Vec2] = Field(min_length=3)
    height: Positive
    draft_angle: float = 0.0


# --------------------------------------------------------------------------- #
# features — discriminated union on `kind`
# --------------------------------------------------------------------------- #


class BoxFeature(_FeatureBase):
    kind: Literal["box"] = "box"
    operation: Literal["add", "subtract"] = "add"
    parameters: BoxParameters


class CylinderFeature(_FeatureBase):
    kind: Literal["cylinder"] = "cylinder"
    operation: Literal["add", "subtract"] = "add"
    parameters: CylinderParameters


class HoleFeature(_FeatureBase):
    kind: Literal["hole"] = "hole"
    operation: Literal["subtract"] = "subtract"
    parameters: HoleParameters


class GearFeature(_FeatureBase):
    kind: Literal["gear"] = "gear"
    operation: Literal["add", "subtract"] = "add"
    parameters: GearParameters


class ConeFeature(_FeatureBase):
    kind: Literal["cone"] = "cone"
    operation: Literal["add", "subtract"] = "add"
    parameters: ConeParameters


class SphereFeature(_FeatureBase):
    kind: Literal["sphere"] = "sphere"
    operation: Literal["add", "subtract"] = "add"
    parameters: SphereParameters


class TorusFeature(_FeatureBase):
    kind: Literal["torus"] = "torus"
    operation: Literal["add", "subtract"] = "add"
    parameters: TorusParameters


class EdgeFilletFeature(_FeatureBase):
    kind: Literal["edge_fillet"] = "edge_fillet"
    operation: Literal["add", "subtract"] = "subtract"
    parameters: EdgeFilletParameters


class EdgeChamferFeature(_FeatureBase):
    kind: Literal["edge_chamfer"] = "edge_chamfer"
    operation: Literal["add", "subtract"] = "subtract"
    parameters: EdgeChamferParameters


class SketchFeature(_FeatureBase):
    kind: Literal["sketch"] = "sketch"
    operation: Literal["add", "subtract"] = "add"
    parameters: SketchParameters


class LinearPatternFeature(_FeatureBase):
    kind: Literal["linear_pattern"] = "linear_pattern"
    operation: Literal["add", "subtract"] = "add"
    parameters: LinearPatternParameters


class CircularPatternFeature(_FeatureBase):
    kind: Literal["circular_pattern"] = "circular_pattern"
    operation: Literal["add", "subtract"] = "add"
    parameters: CircularPatternParameters


Feature = Annotated[
    Union[
        BoxFeature,
        CylinderFeature,
        HoleFeature,
        GearFeature,
        ConeFeature,
        SphereFeature,
        TorusFeature,
        EdgeFilletFeature,
        EdgeChamferFeature,
        SketchFeature,
        LinearPatternFeature,
        CircularPatternFeature,
    ],
    Field(discriminator="kind"),
]

FEATURE_KINDS = ("box", "cylinder", "hole", "gear", "cone", "sphere", "torus", "edge_fillet", "edge_chamfer", "sketch", "linear_pattern", "circular_pattern")

# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #


class Ledger(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    project_id: str = Field(default_factory=new_project_id)
    revision: int = Field(default=0, ge=0)
    metadata: Metadata = Field(default_factory=Metadata)
    features: list[Feature] = Field(default_factory=list, max_length=MAX_FEATURES)

    @field_validator("features")
    @classmethod
    def _unique_ids(cls, v: list[Feature]) -> list[Feature]:
        seen: set[str] = set()
        for f in v:
            if f.id in seen:
                raise ValueError(f"duplicate feature id: {f.id}")
            seen.add(f.id)
        return v

    @model_validator(mode="after")
    def _first_feature_is_additive(self) -> Ledger:
        live = [f for f in self.features if not f.suppressed]
        if live and live[0].operation != "add":
            raise ValueError("the first live feature must have operation='add'")
        return self


def empty_ledger(name: str = "Untitled Part") -> Ledger:
    return Ledger(metadata=Metadata(name=name))


def feature_index(ledger: Ledger, feature_id: str) -> int | None:
    for i, f in enumerate(ledger.features):
        if f.id == feature_id:
            return i
    return None


def feature_by_id(ledger: Ledger, feature_id: str) -> Feature | None:
    i = feature_index(ledger, feature_id)
    return None if i is None else ledger.features[i]


def ledger_json_schema() -> dict[str, Any]:
    """JSON Schema for the whole document. Handed to agents as reference."""
    return Ledger.model_json_schema()
