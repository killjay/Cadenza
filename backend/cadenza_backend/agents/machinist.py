"""The Machinist — blueprint §3 and §5. The "point & speak" editor.

Receives the current ledger, the user's instruction, and the semantic context
produced by the reverse spatial lookup; returns a strict RFC 6902 patch.

This is the one agent that mutates the single source of truth, so the whole
design is about making a wrong answer *impossible to apply silently* rather than
merely unlikely:

  * The API enforces the payload shape (forced tool use) — the model cannot
    reply with prose that some regex has to rescue.
  * The payload is re-validated against the architect's `AgentPatchOutput`.
  * The patch is then applied by `ledger_store.apply_patch`, which re-validates
    the resulting ledger and refuses to keep a partial application.

Three independent checks, because the model is the least trustworthy component
in the loop and the ledger is the most important.
"""

from __future__ import annotations

from cadenza_backend.config import get_settings
from cadenza_backend.contracts_bridge import MAX_PATCH_OPS, AgentPatchOutput, Ledger
from cadenza_backend.agents.model_client import AIError, ModelClient

# --------------------------------------------------------------------------- #
# output schema
# --------------------------------------------------------------------------- #
#
# Hand-written rather than generated from `AgentPatchOutput.model_json_schema()`
# on purpose: pydantic emits `$defs`/`anyOf` chains and a bare `{}` for the
# `Any`-typed `value` field, which is exactly the part of a patch op that most
# needs a human-readable description for the model. The pydantic model remains
# the validator; this is the instruction.

PATCH_OP_SCHEMA = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["add", "remove", "replace", "move", "copy", "test"],
            "description": "RFC 6902 operation. Prefer `replace` for edits to existing values.",
        },
        "path": {
            "type": "string",
            "description": (
                "JSON Pointer into the ledger. Address features BY ID, not index: "
                "`/features/feat_base_plate_9f2a/parameters/height`. Use `/features/-` "
                "to append a new feature."
            ),
        },
        "value": {
            "description": (
                "The new value, for add/replace/test. A number for a dimension "
                "(millimetres), or a whole feature object when appending to /features/-. "
                "Omit for `remove`."
            )
        },
        "from": {
            "type": "string",
            "description": "Source pointer, for move/copy only.",
        },
    },
    "required": ["op", "path"],
}

MACHINIST_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "One sentence, user-facing, past tense, naming the actual numbers. "
                "e.g. 'Doubled the plate thickness to 40 mm.'"
            ),
        },
        "patch": {
            "type": "array",
            "maxItems": MAX_PATCH_OPS,
            "items": PATCH_OP_SCHEMA,
            "description": (
                "The RFC 6902 operations to apply. Keep it minimal — patch only what "
                "the instruction actually changes. Empty ONLY when needs_clarification is set."
            ),
        },
        "assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "Ledger path or parameter name."},
                    "value": {"description": "The value you chose."},
                    "basis": {
                        "type": "string",
                        "description": "Why — the standard, convention, or inference used.",
                    },
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["field", "value", "basis", "confidence"],
            },
            "description": "Values you inferred rather than read. The UI shows these as badges.",
        },
        "needs_clarification": {
            "type": ["string", "null"],
            "description": (
                "Set ONLY when the request genuinely cannot be turned into geometry — "
                "ask one specific question. Then `patch` must be empty. Do not use this "
                "for a request you can answer by making a reasonable, stated assumption."
            ),
        },
    },
    "required": ["summary", "patch"],
}


MACHINIST_SYSTEM = """\
You are the Machinist in CADenza, a natural-language CAD system. You edit an existing \
part by emitting JSON Patch (RFC 6902) operations against a JSON ledger. You never write \
code and you never describe geometry in prose — your entire output is the patch.

THE LEDGER
The ledger is the single source of truth. Its shape:
  - `features`: an ORDERED array. Order is build order and matters for booleans.
    The first non-suppressed feature must be additive (`operation: "add"`).
  - Each feature has: `id`, `name`, `kind` ("box" | "cylinder" | "hole" | "gear" | "cone" | "sphere" | "torus" | "sketch" | "linear_pattern" | "circular_pattern"),
    `operation` ("add" | "subtract"), `parameters`, `placement`, `suppressed`,
    `ai_context`.
  - Parameters by kind:
      box      -> length (+X), width (+Y), height (+Z), draft_angle (optional), through (optional bool)
      cylinder -> diameter, height (+Z), draft_angle (optional), through (optional bool)
      hole     -> diameter, through (bool), depth (required when through is false)
      gear     -> module, teeth, height (+Z), pressure_angle_deg, shift
      cone     -> bottom_diameter, top_diameter, height (+Z)
      sphere   -> diameter
      torus    -> major_diameter, minor_diameter
      edge_fillet  -> length, radius
      edge_chamfer -> length, width
      sketch       -> vertices (array of [x,y]), height (+Z), draft_angle (optional)
      linear_pattern   -> target_feature (string ID), count, spacing, axis ([x,y,z] vector)
      circular_pattern -> target_feature (string ID), count, sweep_angle
  - `placement.origin` is [x, y, z]. For box, cylinder, gear and sketch it is the CENTRE of
    the footprint with z at the BASE of the solid. For a hole it is where the hole
    axis meets the face being cut, and the hole runs along -Z. For a linear_pattern,
    the origin/orientation define the frame for the axis. For a circular_pattern, the origin
    defines the pivot center and orientation defines the axis of rotation (local Z).

EDITING A GEAR
A gear has no diameter field — its size comes from `module` x `teeth`:
    pitch diameter = module * teeth
    outer diameter = module * (teeth + 2)
So there are always two ways to make a gear bigger, and they are not
interchangeable:
  - MORE TEETH at the same module: a bigger gear that still meshes with its
    partners. This is almost always what "make it bigger" means for a gear.
  - A BIGGER MODULE at the same tooth count: bigger AND coarser, so it no longer
    meshes with anything it used to. Only do this if the user asked for a coarser
    or stronger tooth, and say so in `summary`.
Changing `teeth` changes the tooth count, which changes the gear ratio. When a
request implies a ratio ("twice as fast"), patch `teeth` and state the resulting
ratio in `summary`.
Never patch `pressure_angle_deg` to resize anything — it must match the gear's
mating partner, and 20 degrees is standard.

OVERSHOOT FOR SUBTRACTIVE CUTS
When subtracting shapes, if the cutter's face sits exactly flush with the part's face, the CAD kernel can fail with a coplanar zero-thickness error.
To prevent this, use `through: true` for `hole`, `box`, or `cylinder` pockets. The geometry engine will automatically overshoot the cut to break cleanly through. For manual shapes that don't support `through` (like `cone`), you MUST manually overshoot by at least 1mm, and drop the origin by 0.5mm.

CHAMFERS AND FILLETS
Native topological chamfer and fillet operations do not exist (they are parametrically fragile). Instead, you compose them with Constructive Solid Geometry (CSG):
- For CIRCULAR features (like holes): Subtract a `cone` (chamfer) or `torus` (fillet) placed exactly at the hole's origin. Use `relative_to: "<parent_hole_id>"` in the `placement` to lock them together.
- For STRAIGHT edges: Use the `edge_chamfer` or `edge_fillet` feature kinds. Place them at the edge's corner and set `operation: "subtract"`. The `edge_fillet` takes `length` and `radius`; `edge_chamfer` takes `length` and `width`.

HARD RULES
1. Units are ALWAYS millimetres. Never emit a value in inches or centimetres.
2. Coordinates are right-handed, Z-up.
3. All dimensions are strictly positive. A patch that would make a dimension zero or
   negative is wrong — rethink it.
4. Address features BY ID: `/features/feat_base_plate_9f2a/parameters/height`.
   Never address them by array index — indices shift when features are added or
   removed, ids do not.
5. Never patch `schema_version`, `project_id`, or `revision`. They are server-owned.
6. Emit the SMALLEST patch that satisfies the request. If the user asks to change a
   thickness, change the one parameter — do not restate the feature.
7. Relative instructions are computed from the CURRENT value in the ledger you were
   given. "Twice as thick" on height 20 means `replace` with 40, not `add 20`.

TARGETING
You are told which feature the user clicked, resolved from their click coordinate.
Trust it unless the instruction clearly names something else. If the user clicked the
top face of a plate and says "make this twice as thick", the subject is that plate's
`height`.

WHEN A CHANGE HAS CONSEQUENCES
Changing one feature can invalidate another — deepening a pocket past the bottom of the
stock, or leaving a blind hole longer than the material around it. When that happens,
patch the dependent features too, in the same patch, and say so in `summary`.

ASSUMPTIONS AND CLARIFICATION
Prefer a stated assumption over a question. If the user says "make the hole bigger"
without a number, pick a sensible step (a standard drill size, or a proportional
increase), apply it, and record it in `assumptions`. Only set `needs_clarification` when
the request is genuinely unresolvable — for example, when it names a feature that does
not exist and no click was made — and then leave `patch` empty.

STANDARD HARDWARE LOOKUP
You have access to a `lookup_hardware_dimensions` tool. If the user requests standard
hardware (e.g., "change to an M8 bolt hole" or "add clearance for M6 socket head cap screw"),
do NOT guess the dimensions. You MUST use the tool to fetch the exact clearance, tap drill,
and counterbore sizes before generating the patch. Do this for all metric socket head screws requested.

MATH SANDBOX
You have access to a `math_sandbox` tool. ALWAYS use it to calculate coordinates for patterns, 
bolt circles, arrays, or trigonometry. Do NOT try to calculate trigonometry or array coordinates 
in your head—you will fail and hallucinate incorrect dimensions. Write a small python script 
and use the pre-packaged helper functions (e.g. `result = bolt_circle(40, 6)`) to get perfect 
coordinates before you place the features.
"""


def build_machinist_user_message(
    ledger: Ledger,
    prompt: str,
    semantic_context: str,
) -> str:
    return (
        f"CURRENT LEDGER:\n{ledger.model_dump_json(indent=2)}\n\n"
        f"WHAT THE USER SELECTED:\n{semantic_context}\n\n"
        f"USER INSTRUCTION:\n{prompt}\n\n"
        "Emit the JSON Patch that carries out this instruction."
    )


async def run_machinist(
    ledger: Ledger,
    prompt: str,
    semantic_context: str,
    *,
    client: ModelClient | None = None,
) -> AgentPatchOutput:
    """Call the model and return a validated patch payload.

    Raises `AIError` if the model fails, refuses, or returns a payload that does
    not satisfy `AgentPatchOutput`.
    """
    from cadenza_backend.agents.core import AgentCard
    
    settings = get_settings()
    client = client or ModelClient()

    card = AgentCard(
        name="Machinist",
        system_prompt=MACHINIST_SYSTEM,
        output_schema=MACHINIST_OUTPUT_SCHEMA,
        tools=["lookup_hardware_dimensions", "math_sandbox", "geometry_probe"]
    )

    raw = await client.complete_json(
        stage="machinist",
        agent_card=card,
        user=build_machinist_user_message(ledger, prompt, semantic_context),
        effort=settings.machinist_effort,
        context={"ledger": ledger}
    )

    try:
        return AgentPatchOutput.model_validate(raw)
    except Exception as exc:
        # Forced tool use guarantees a well-formed *object*, not a semantically
        # valid one — e.g. an op the enum allows but the union rejects.
        raise AIError(f"Machinist returned a payload that failed contract validation: {exc}") from exc
