"""The Draftsman — blueprint §3. Initial generation.

Turns a description (and, later, an uploaded sketch) into the first set of
ledger features. Where the Machinist edits an existing part, the Draftsman
creates one from nothing.

One deliberate design decision: **the model does not invent feature ids.** The
ledger's ids must match `^feat_[a-z0-9_]{1,60}$` and must be unique, and asking a
model to satisfy a regex across a list it is generating in one pass is a
needless source of validation failures. The model supplies a human name; the
server mints the id with the contract's own `new_feature_id()`. Uniqueness then
comes from the minting function rather than from the model's diligence.

Vision (sketch upload, blueprint §3) IS implemented. An image reaches the model
as an ordinary content block via `ModelClient`, which routes the call to a
vision-capable provider. The agent boundary is unchanged by its presence, which
is the property ARCHITECTURE.md §4 predicted: "a different input, the same
output schema". Nothing downstream of `run_draftsman` knows or cares whether the
features came from a sentence or a photograph.

What vision does NOT change is the ceiling. The feature vocabulary is still
box/cylinder/hole/gear, so anything outside it must be declined via
`needs_clarification` rather than approximated — ARCHITECTURE.md §1, "there is
no path where the user gets a silently wrong part".

The test is the FORM, not the medium. A photograph whose subject is already in
the vocabulary — a disc, a plate, a shaft, a spur gear — builds at an assumed
scale with every dimension marked low-confidence; a photograph of an arbitrary
object is declined. Scoping that rule to *drawings* was a bug: a photo of a gear
was refused for being a photo, while the identical part in a drawing built. What
remains out of scope is reconstruction — nothing here turns pixels into a mesh.

That ceiling rule lives in `DRAFTSMAN_SYSTEM`, not in the vision block, because
it is a property of the vocabulary and not of the input. "Make me a gear" typed
into the composer hits it exactly as hard as a gear drawing does, and for a
while only the drawing was caught — the sentence quietly got a plain disc back.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from cadenza_backend.contracts_bridge import (
    AIContext,
    Assumption,
    Feature,
    Ledger,
    Placement,
    new_feature_id,
)
from cadenza_backend.agents.model_client import AIError, ModelClient

_feature_adapter: TypeAdapter[Feature] = TypeAdapter(Feature)


class DraftedFeature(BaseModel):
    """What the model emits per feature — the contract's Feature minus the id."""

    model_config = ConfigDict(extra="ignore")

    kind: str
    name: str = ""
    operation: str = "add"
    parameters: dict[str, Any] = Field(default_factory=dict)
    placement: dict[str, Any] = Field(default_factory=dict)
    original_intent: str = ""


class DraftsmanOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    features: list[DraftedFeature] = Field(default_factory=list)
    assumptions: list[dict[str, Any]] = Field(default_factory=list)
    needs_clarification: str | None = None


DRAFTSMAN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence, user-facing, past tense, naming the real dimensions.",
        },
        "features": {
            "type": "array",
            "maxItems": 24,
            "description": "Features in BUILD ORDER. Additive stock first, cuts after.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["box", "cylinder", "hole", "gear"]},
                    "name": {
                        "type": "string",
                        "description": "Short human name, e.g. 'Base Plate' or 'Centre Hole'.",
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["add", "subtract"],
                        "description": "Holes are always 'subtract'. The first feature must be 'add'.",
                    },
                    "parameters": {
                        "type": "object",
                        "description": (
                            "Dimensions in MILLIMETRES. box: length, width, height. "
                            "cylinder: diameter, height. hole: diameter, through (bool), "
                            "and depth when through is false. gear: module, teeth, "
                            "height, and optionally pressure_angle_deg and shift."
                        ),
                        "properties": {
                            "length": {"type": "number"},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                            "diameter": {"type": "number"},
                            "through": {"type": "boolean"},
                            "depth": {"type": "number"},
                            "module": {
                                "type": "number",
                                "description": "Gear tooth size: mm of pitch diameter per tooth.",
                            },
                            "teeth": {
                                "type": "integer",
                                "minimum": 3,
                                "description": "Gear tooth count. A whole number.",
                            },
                            "pressure_angle_deg": {
                                "type": "number",
                                "description": "Gear pressure angle. Omit unless told; 20 is standard.",
                            },
                            "shift": {
                                "type": "number",
                                "description": "Gear profile shift in modules. Omit unless told; 0 is standard.",
                            },
                        },
                    },
                    "placement": {
                        "type": "object",
                        "description": "Where the feature sits, in mm.",
                        "properties": {
                            "origin": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 3,
                                "maxItems": 3,
                            },
                            "rotation_deg": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 3,
                                "maxItems": 3,
                            },
                        },
                    },
                    "original_intent": {
                        "type": "string",
                        "description": "The user's words that led to this feature.",
                    },
                },
                "required": ["kind", "name", "operation", "parameters"],
            },
        },
        "assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "value": {},
                    "basis": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["field", "value", "basis", "confidence"],
            },
            "description": "Every dimension you inferred rather than were told.",
        },
        "needs_clarification": {
            "type": ["string", "null"],
            "description": "Only when the description cannot be built at all. Then `features` is empty.",
        },
    },
    "required": ["summary", "features"],
}


DRAFTSMAN_SYSTEM = """\
You are the Draftsman in CADenza, a natural-language CAD system. You turn a plain-English \
description of a part into an ordered list of parametric features. You never write code — \
your entire output is the structured feature list.

FEATURE VOCABULARY (this is all you have; compose these)
  box      -> length (+X), width (+Y), height (+Z)
  cylinder -> diameter, height (+Z)
  hole     -> diameter, through (bool), depth (only when through is false); always subtract
  gear     -> module, teeth, height (+Z); optionally pressure_angle_deg, shift
              A standard involute spur gear. Its axle bore is a separate `hole`
              feature — a gear on its own is a solid wheel.

GEARS
A gear has NO diameter parameter. Its size is module x teeth:
    pitch diameter = module * teeth
    outer diameter = module * (teeth + 2)
Users almost never say "module". Translate what they do say:
  - "a 20-tooth gear, module 2"     -> exactly that. Nothing to infer.
  - "a 40 mm gear with 20 teeth"    -> a diameter in mm is the OUTER diameter, so
                                       module = 40 / (20 + 2) = 1.818. Record the
                                       module as an assumption naming this conversion.
  - "a gear about 50 mm across"     -> pick a whole-number module from the preferred
                                       series (0.5, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5),
                                       then teeth = round(50/module) - 2. Record both.
  - "a gear" with no size at all    -> module 2, 20 teeth, 10 mm thick. Record all three.
Two gears that must MESH need the SAME module and the same pressure angle — that is
what meshing means. Their ratio is the ratio of their tooth counts.
Prefer 17 or more teeth. Below that a real cutter undercuts the tooth root, and while
the part will still build, it is weaker than the drawing suggests; if the user asks for
fewer, build it and say so in `summary`.
`pressure_angle_deg` defaults to 20 and `shift` to 0. OMIT them unless the user gives
you a number — a value you invented for either is worse than the default.

PLACEMENT
  `placement.origin` is [x, y, z] in millimetres.
  - For box, cylinder and gear, the origin is the CENTRE of the footprint, with z at
    the BASE of the solid. A 100x100x20 plate at origin [0,0,0] therefore spans
    x -50..50, y -50..50, z 0..20.
  - For a hole, the origin is the point where the hole axis meets the face being cut,
    and the hole is cut along -Z. A through hole in the middle of that plate's top face
    has origin [0, 0, 20].

HARD RULES
1. Units are ALWAYS millimetres. Convert anything the user gives in other units and
   record the conversion as an assumption.
2. Coordinates are right-handed, Z-up.
3. Every dimension must be strictly positive.
4. Order matters: it is build order. Emit additive stock FIRST, then cuts. The first
   feature must have operation "add".
5. Centre features deliberately. "In the middle" means the geometric centre of the face
   in question, which for a plate centred on the origin means x=0, y=0.

WHAT YOU CANNOT BUILD
Your vocabulary is box, cylinder, hole and gear. Real parts routinely need more than
that, so decide which of two cases you are in and act accordingly.

  (a) The part's overall FORM is expressible, and what is missing is DETAIL — a fillet,
      a chamfer, a thread callout, a knurl. Build the form. Then say plainly IN THE
      `summary` which details you dropped ("...without the R20 fillet or the M8 thread"),
      and record each one in `assumptions` too. The user must learn what they are looking
      at from the sentence they read first, not by expanding a list.

  (b) The FORM itself is outside the vocabulary — a revolve, a sweep, a loft, a helix
      or a thread cut as real geometry, an organic or freeform surface, a sheet-metal
      bend, or an assembly of several parts. Do NOT approximate it out of boxes and
      cylinders and call it done. Set `needs_clarification` naming exactly what you
      cannot build, and emit only features you are confident in (an empty list is fine).

Note that SPUR GEARS are case neither — they are in the vocabulary above, so build them
properly rather than declining. Helical, bevel, worm and internal ring gears are still
case (b): a spur gear is not an approximation of any of them.

The test is what the user would say looking at the result. "That is my part, minus the
fillets" is case (a) and is a success. "That is not a gear" is case (b), and shipping it
silently is the one failure this system does not get to have.

INFERENCE
Real descriptions are underspecified. Fill the gaps with sensible engineering defaults
rather than refusing, and record every inferred number in `assumptions` with the basis
you used. Missing NUMBERS are for you to infer. A missing FORM is not: set
`needs_clarification` when the description needs geometry you cannot build, or when it
does not describe a physical part at all.
"""


DRAFTSMAN_VISION_SYSTEM = """\

READING AN ATTACHED IMAGE
One or more images are attached. They are the primary specification; any text the user
typed refines them. Work through the image in this order.

1. DIMENSIONS FIRST. Look for explicit numbers: dimension lines, leader notes, tables,
   handwritten figures. A number written on the drawing ALWAYS beats a number you
   estimate from pixels. Read them literally, including the ones that look redundant.
2. UNITS. Look for a unit marker (mm, cm, m, in, ", ft, '). If the drawing says inches,
   convert to millimetres and record an assumption for each converted value. If NOTHING
   indicates units, assume millimetres and record that as an assumption with low
   confidence — do not silently pick a scale.
3. SHAPE. Decide which of box / cylinder / hole each visible element is. A rectangle in
   one view plus a thickness in another is a box. A circle on a face with a diameter
   callout (or a Ø symbol) is a hole. A circle in plan with a height in elevation is a
   cylinder.
4. SCALE WITHOUT DIMENSIONS. If the drawing carries NO numbers at all, do not guess a
   size from the image. Pick a plausible overall size for the kind of part it appears to
   be, state it in `summary`, and record every dimension as an assumption with
   confidence <= 0.4 so the user knows to correct you.
5. MULTI-VIEW DRAWINGS. Front/top/side views describe ONE part, not three. Reconcile
   them into a single feature list. A circle that appears in the top view and as two
   hidden (dashed) lines in the front view is one through hole, not two features.
6. POSITION. Read hole positions off the dimensions to their centres. If a hole is
   dimensioned from an edge, convert to the centred coordinate system described above
   (origin at the centre of the part's footprint) before emitting it.

WHAT YOU CANNOT BUILD, IN A DRAWING
The ceiling rule above applies unchanged — drawings just reach it more often, and they
make the two cases easy to confuse. A title block, a surface-finish note, a GD&T frame, a
thread callout, a fillet or chamfer radius: that is DETAIL, case (a), so build the form
and name the omission in `summary`. A section view of a revolve, a swept profile, a
lofted transition, an exploded assembly: that is FORM, case (b), so say what you cannot
build instead of drawing a blocky lookalike of it.

A GEAR is neither, in a drawing OR in a photograph — build it. Gear drawings state the
tooth data in a table rather than as dimension lines, so read module (or DP), tooth count
and pressure angle out of that table before measuring anything off the picture. If the
table gives diametral pitch (DP) instead of module, module = 25.4 / DP; record the
conversion. A PHOTOGRAPH of a gear carries no table, so count the teeth off the image,
take the defaults for everything else, and record each one as an assumption with
confidence <= 0.4. A countable tooth count is real information; it beats declining.

A photograph of a real-world object, as opposed to a drawing of one, usually IS case (b),
because you cannot recover a form from pixels. But the test is the FORM, not the medium.
If what you see is a shape the vocabulary already has — a plate, a disc, a shaft, a spur
gear — build it at an assumed scale and mark every dimension low-confidence. Decline a
photograph because its form is outside the vocabulary, never because it is a photograph.

Never invent a dimension line that is not there, and never report a number as measured
when you inferred it. Every number you were not given belongs in `assumptions`.
"""


def draftsman_system(has_images: bool = False) -> str:
    """The system prompt, extended with drawing-reading rules when relevant.

    The vision block is appended rather than always present so a text-only turn
    does not pay for instructions about dimension lines it will never see — and,
    more usefully, so the model is not primed to hallucinate an image.
    """
    return DRAFTSMAN_SYSTEM + (DRAFTSMAN_VISION_SYSTEM if has_images else "")


def build_draftsman_user_message(prompt: str, has_images: bool = False) -> str:
    if has_images:
        return (
            "The attached image(s) are the specification for the part.\n\n"
            f"WHAT THE USER TYPED ALONGSIDE THEM:\n{prompt or '(nothing)'}\n\n"
            "Read the drawing, reconcile it with any text above, and emit the ordered "
            "feature list that builds this part. Put every number you were not "
            "explicitly given into `assumptions`."
        )
    return (
        f"PART DESCRIPTION:\n{prompt}\n\n"
        "Emit the ordered feature list that builds this part."
    )


def _to_contract_features(drafted: list[DraftedFeature]) -> list[Feature]:
    """Mint ids and validate each drafted feature against the contract's union."""
    features: list[Feature] = []
    for item in drafted:
        payload: dict[str, Any] = {
            "id": new_feature_id(item.name or item.kind),
            "kind": item.kind,
            "name": item.name,
            "parameters": item.parameters,
            "placement": Placement(**item.placement).model_dump()
            if item.placement
            else Placement().model_dump(),
            "ai_context": AIContext(original_intent=item.original_intent or None).model_dump(),
        }
        # `hole` pins operation to "subtract" in the contract, so passing it
        # explicitly would be rejected as a mismatch if the model said "add".
        if item.kind != "hole":
            payload["operation"] = item.operation

        try:
            features.append(_feature_adapter.validate_python(payload))
        except ValidationError as exc:
            raise AIError(
                f"Draftsman emitted a feature that is not buildable "
                f"({item.kind} '{item.name}'): {exc}"
            ) from exc
    return features


async def run_draftsman(
    prompt: str,
    *,
    ledger: Ledger | None = None,
    images: list[tuple[str, bytes]] | None = None,
    client: ModelClient | None = None,
) -> tuple[list[Feature], DraftsmanOutput]:
    """Generate the initial feature list. Returns (contract features, raw output)."""
    client = client or ModelClient()
    has_images = bool(images)

    raw = await client.complete_json(
        stage="draftsman",
        system=draftsman_system(has_images),
        user=build_draftsman_user_message(prompt, has_images),
        schema=DRAFTSMAN_OUTPUT_SCHEMA,
        images=images,
    )

    try:
        output = DraftsmanOutput.model_validate(raw)
    except ValidationError as exc:
        raise AIError(f"Draftsman returned an unusable payload: {exc}") from exc

    if output.needs_clarification and not output.features:
        return [], output

    return _to_contract_features(output.features), output


def parse_assumptions(items: list[dict[str, Any]]) -> list[Assumption]:
    """Best-effort coercion — a malformed assumption is dropped, never fatal.

    Assumptions are advisory UI badges. Failing a build because the model wrote
    confidence as "high" instead of 0.8 would trade a real result for a cosmetic one.
    """
    out: list[Assumption] = []
    for item in items:
        try:
            out.append(Assumption.model_validate(item))
        except ValidationError:
            continue
    return out
