"""Shared authoring rules for the supported parameter relationship vocabulary."""

PARAMETER_INSTRUCTIONS = """
PERSISTENT PARAMETER RELATIONSHIPS
When dimensions must stay related after edits, persist the relationship instead of
only calculating coordinates once. The ledger has `relationships` with:
  parameters: {name: {unit: "mm"|"scalar", value: number, expression: null}
               OR {unit: "mm"|"scalar", value: null, expression: "..."}},
  bindings: [{feature_id: "feat_...", target: "length"|"width"|"height"|"diameter"|"depth"|"amount"|"x"|"y"|"z"|"value", expression: "...", constraint_id: null}],
  constraints: [{left: "...", operator: "<"|"<="|">"|">=", right: "...", message: "Reason"}].
Names use lowercase letters, digits and underscores, beginning with a letter (max 48).
Exactly one of value/expression is non-null. Expressions allow only parameter names,
numeric scalar constants, parentheses and + - * /. Addition/subtraction and comparisons
need matching units; multiplication/division must result in mm or scalar. Use a named
mm value for a dimensional constant, not 'width - 15' (15 is scalar).
At most 64 parameters, 256 bindings, 64 constraints; expression length <=256, syntax nodes <=64,
absolute values <=1e9, at most 16 dependency levels. No cycles, calls, powers, functions, scripts or cross-ledger references.
Bindings support TOP-LEVEL box/cylinder/hole dimensions, extrusion amount and placement
origin axes. Profile dimension values use target="value" with a constraint_id.
Feature parameters/coordinates remain numeric on the wire; the server resolves bindings.
For an existing bound dimension, patch /relationships/parameters/<driver>/value.
Never overwrite its materialized feature value. To change a formula, edit the binding
or derived parameter expression explicitly. Deleting a feature requires removing its bindings.
Preserve all unrelated parameters, bindings and constraints. Compound derived values
are not directly editable; change a driving value or ask which relationship should change.

Four-hole centred plate example: box length is X, width is Y. Define plate_length,
plate_width, thickness, hole_diameter and edge_margin in mm. Bind box length/width/height
and hole diameter. For corners, x = +/-(plate_length / 2 - edge_margin),
y = +/-(plate_width / 2 - edge_margin), z = thickness (zero-origin, unrotated plate).
Edge margin means hole CENTRE to edge; explicitly state this assumption. For other origins
or orientations, account for the actual coordinate frame or ask; don't apply this example blindly.
Persist constraints edge_margin > hole_diameter / 2,
plate_length - 2 * edge_margin > hole_diameter, and
plate_width - 2 * edge_margin > hole_diameter, with clear user-facing messages.
These preserve enclosed distinct holes and reject impossible spacing after later edits.
Do not claim arbitrary prompt requirements were independently verified.
"""
