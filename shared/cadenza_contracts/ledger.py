"""The JSON ledger — CADenza's single source of truth.

INVARIANTS (enforced here, relied on everywhere):

  * Units are millimetres, angles degrees. Always. No other unit reaches the wire.
  * Coordinates are right-handed, Z-up (build123d native, glTF is Y-up — the
    exporter converts; the ledger never does).
  * `features` is an ORDERED array; order == build order and matters for booleans.
  * `revision` is monotonic and is bumped ONLY by the server, and ONLY when a
    patch applied AND the geometry rebuilt successfully. A revision that exists
    has been built. Agents may not patch it (see patch.FORBIDDEN_PATHS).
  * No kernel face/edge ordinals are stored as persistent selectors. TargetRef
    describes coordinate picks; surface_references stores supported semantic
    primitive roles and may explicitly become unresolved after geometry edits.
  * Feature inputs remain numeric. Part-local relationships are validated and
    materialized from bounded, unit-checked arithmetic before geometry builds.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator, model_serializer

from cadenza_contracts.ids import new_project_id
from cadenza_contracts.surface_reference import ReferenceId, SurfaceReference
from cadenza_contracts.parameters import Relationships, materialize
from cadenza_contracts.profiles import ProfileParameters

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
    targets: list[TargetRef] = Field(default_factory=list)
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
    target_feature: str | None = Field(default=None, pattern=FEATURE_ID_PATTERN)
    length: Positive
    radius: Positive


class EdgeChamferParameters(_Model):
    target_feature: str | None = Field(default=None, pattern=FEATURE_ID_PATTERN)
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
    radius: Positive  # Compatibility hint; actual radius comes from seed placement and center.
    sweep_angle: float = 360.0
    axis: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 1.0])  # Historical builder uses local Z; C1 admits +Z only.


Vec2 = tuple[float, float]

class SketchParameters(_Model):
    vertices: list[Vec2] = Field(min_length=3)
    height: float = Field(default=0, ge=0, allow_inf_nan=False)
    draft_angle: float = 0.0


class ThreadedHoleParameters(_Model):
    designation: str  # e.g. "M8"
    pitch: float | None = None
    diameter: Positive  # The tap drill size
    through: bool = True
    depth: Positive | None = None

    @model_validator(mode="after")
    def _depth_rules(self) -> ThreadedHoleParameters:
        if not self.through and self.depth is None:
            raise ValueError("a blind threaded hole (through=false) requires `depth`")
        return self


class FastenerParameters(_Model):
    designation: str  # e.g. "M8"
    diameter: Positive
    length: Positive
    head_type: Literal["socket", "hex", "flat", "button"] = "socket"


class ShellParameters(_Model):
    target_feature: str | None = Field(default=None, pattern=FEATURE_ID_PATTERN)
    thickness: float
    open_faces: list[str] = Field(default_factory=list)


class RevolveParameters(_Model):
    vertices: list[Vec2] = Field(min_length=3)
    angle: float = 360.0
    axis: Vec3 = Field(default_factory=lambda: [0.0, 1.0, 0.0])


class ExtrudeParameters(_Model):
    target_feature: str = Field(pattern=FEATURE_ID_PATTERN)
    amount: float
    taper: float = 0.0
    twist: float = 0.0


class EdgeDict(_Model):
    type: str
    p1: Vec2
    p2: Vec2
    p3: Vec2 | None = None

class LoftSection(_Model):
    vertices: list[Vec2] | None = None
    edges: list[EdgeDict] | None = None
    placement: Placement | None = None

class LoftParameters(_Model):
    sections: list[LoftSection] = Field(min_length=2)


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


class ProfileFeature(_FeatureBase):
    kind: Literal["profile"] = "profile"
    operation: Literal["add"] = "add"
    parameters: ProfileParameters


class LinearPatternFeature(_FeatureBase):
    kind: Literal["linear_pattern"] = "linear_pattern"
    operation: Literal["add", "subtract"] = "add"
    parameters: LinearPatternParameters


class CircularPatternFeature(_FeatureBase):
    kind: Literal["circular_pattern"] = "circular_pattern"
    operation: Literal["add", "subtract"] = "add"
    parameters: CircularPatternParameters


class ThreadedHoleFeature(_FeatureBase):
    kind: Literal["threaded_hole"] = "threaded_hole"
    operation: Literal["subtract"] = "subtract"
    parameters: ThreadedHoleParameters


class FastenerFeature(_FeatureBase):
    kind: Literal["fastener"] = "fastener"
    operation: Literal["add"] = "add"
    parameters: FastenerParameters


class ShellFeature(_FeatureBase):
    kind: Literal["shell"] = "shell"
    operation: Literal["modify"] = "modify"
    parameters: ShellParameters


class RevolveFeature(_FeatureBase):
    kind: Literal["revolve"] = "revolve"
    operation: Literal["add", "subtract"] = "add"
    parameters: RevolveParameters


class ExtrudeFeature(_FeatureBase):
    kind: Literal["extrude"] = "extrude"
    operation: Literal["add", "subtract"] = "add"
    parameters: ExtrudeParameters


class LoftFeature(_FeatureBase):
    kind: Literal["loft"] = "loft"
    operation: Literal["add", "subtract"] = "add"
    parameters: LoftParameters


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
        ProfileFeature,
        LinearPatternFeature,
        CircularPatternFeature,
        ThreadedHoleFeature,
        FastenerFeature,
        ShellFeature,
        RevolveFeature,
        ExtrudeFeature,
        LoftFeature,
    ],
    Field(discriminator="kind"),
]

FEATURE_KINDS = ("box", "cylinder", "hole", "gear", "cone", "sphere", "torus", "edge_fillet", "edge_chamfer", "sketch", "profile", "linear_pattern", "circular_pattern", "threaded_hole", "fastener", "shell", "revolve", "extrude", "loft")

# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #

class Part(_Model):
    id: str = Field(pattern=r"^part_[a-z0-9_]{1,60}$")
    name: str = "Untitled Part"
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
    def _first_feature_is_additive(self) -> Part:
        live = [f for f in self.features if not f.suppressed]
        if live and live[0].operation != "add":
            raise ValueError("the first live feature must have operation='add'")
        return self


class Component(_Model):
    id: str = Field(pattern=r"^comp_[a-z0-9_]{1,60}$")
    name: str = ""
    part_id: str | None = None
    catalog_sku: str | None = None
    sub_assembly_id: str | None = None
    placement: Placement = Field(default_factory=Placement)

    @model_validator(mode="after")
    def _must_have_source(self) -> Component:
        sources = sum(bool(x) for x in (self.part_id, self.catalog_sku, self.sub_assembly_id))
        if sources != 1:
            raise ValueError("Component must reference exactly one of: part_id, catalog_sku, or sub_assembly_id")
        return self


class PlanarTarget(BaseModel):
    """Metric coordinates in a deterministic feature-local face frame."""
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    mode: Literal["planar_face"] = "planar_face"
    reference: SurfaceReference
    uv: Annotated[list[float], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def supported_face(self):
        ref = self.reference
        if ref.component_path or ref.source_part_id or ref.feature_kind not in {"box", "cylinder"} or ref.role == "lateral_face":
            raise ValueError("Choose a part-local box face or cylinder end face.")
        return self


class Joint(_Model):
    """Targets use assembly coordinates after placement, before solving joints.

    Runtime currently supports rigid directed attachments: A anchors B. Supply
    both outward normals to oppose them, or neither for translation only.
    Coordinate targets retain placement-derived twist. Planar targets re-resolve
    explicit primitive faces and retain recorded tangent coordinates and twist.
    Motion, closed loops and feature-bound coordinate anchors remain unsupported.
    """
    id: str = Field(pattern=r"^joint_[a-z0-9_]{1,60}$")
    kind: Literal["rigid", "revolute", "cylindrical", "slider", "ball"]
    component_a: str = Field(pattern=r"^comp_[a-z0-9_]{1,60}$")
    component_b: str = Field(pattern=r"^comp_[a-z0-9_]{1,60}$")
    target_a: TargetRef | PlanarTarget
    target_b: TargetRef | PlanarTarget
    twist_deg: float | None = Field(default=None, strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def paired_modes(self):
        planar = isinstance(self.target_a, PlanarTarget)
        if planar != isinstance(self.target_b, PlanarTarget):
            raise ValueError("Both attachment targets must use the same mode.")
        if planar and (self.kind != "rigid" or self.twist_deg is None):
            raise ValueError("Planar attachments require a rigid joint and recorded twist.")
        if not planar and self.twist_deg is not None:
            raise ValueError("Coordinate attachments cannot specify planar twist.")
        return self

    @model_serializer(mode="wrap")
    def legacy_shape(self, handler):
        result = handler(self)
        if self.twist_deg is None:
            result.pop("twist_deg", None)
        return result


class Assembly(_Model):
    parts: list[Part] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    joints: list[Joint] = Field(default_factory=list)


class Ledger(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    project_id: str = Field(default_factory=new_project_id)
    revision: int = Field(default=0, ge=0)
    metadata: Metadata = Field(default_factory=Metadata)
    relationships: Relationships = Field(default_factory=Relationships)

    @model_validator(mode="before")
    @classmethod
    def _resolve_relationships(cls, data: Any) -> Any:
        resolved = materialize(data)
        if resolved is not data:
            # Assignment validation does not revalidate untouched fields.
            resolved["features"] = TypeAdapter(list[Feature]).validate_python(resolved["features"])
        return resolved
    
    # Old mode (Single Part)
    features: list[Feature] = Field(default_factory=list, max_length=MAX_FEATURES)
    
    # New mode (Assembly)
    assembly: Assembly | None = None

    # Saved annotations, not build dependencies: deletion/suppression may leave
    # these unresolved. Resolution is checked against the rebuilt geometry.
    surface_references: dict[ReferenceId, SurfaceReference] = Field(default_factory=dict, max_length=64)

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
