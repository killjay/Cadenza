"""Shared general-agent guidance for the supported pattern contract."""

PATTERN_INSTRUCTIONS = """
SUPPORTED FEATURE PATTERNS
The seed must be an earlier, active, absolute, unrotated box, cylinder or hole.
Box/cylinder seeds must have zero draft and cannot use primitive through mode.
Inherit the seed's operation: a hole pattern MUST subtract, never add. Count is
an integer from 2 to 32 INCLUDING the original seed; do not create a duplicate seed.
Machinist target_feature uses the existing seed ID. For initial Draftsman creation,
use the exact unique earlier seed name; the server resolves it to a minted ID.

Circular patterns support source +Z only: axis=[0,0,1], sweep_angle=360,
placement.origin=[center_x,center_y,0], rotation_deg=[0,0,0], relative_to=null.
The pivot Z MUST be zero, even when the seed hole enters a face at Z=15.
Leave the seed's height/placement unchanged unless the user asks to move it.
On creation, radius is REQUIRED positive compatibility data and must equal
sqrt((seed_x-center_x)^2 + (seed_y-center_y)^2). Actual radius follows the seed
and pivot. Never guess a default radius or resize an existing pattern by writing
its radius/axis fields; those stored compatibility values must remain unchanged.
For a requested radius change, move the seed or pivot explicitly as appropriate
to the instruction. Ask when the intended change is ambiguous.

Linear patterns use identity placement, positive spacing and a cardinal axis;
hole patterns support X/Y directions only. A source allows 128 generated copies
across active patterns. Unsupported seeds, frames or partial sweeps require
clarification, not a silently substituted pattern.

CIRCULAR CREATION EXAMPLE
```json
{"operation":"subtract","parameters":{"target_feature":"feat_seed","count":6,"radius":30,"sweep_angle":360,"axis":[0,0,1]},"placement":{"origin":[0,0,0],"rotation_deg":[0,0,0],"relative_to":null}}
```
This example assumes a 6 mm seed hole at [30,0,15] and a pivot at [0,0,0].
Adapt the seed reference and dimensions to the actual design. Keep unrelated
features, such as a separate centre hole, unchanged. Put clearance observations
in the response summary or supported assumptions, not invented geometry fields.
"""
