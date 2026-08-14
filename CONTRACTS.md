# CADenza — Contracts (frozen for milestone 1)

Everything three people who never speak to each other need in order to build
parts that fit. The authority is `shared/` — this document explains it; the code
enforces it. If they disagree, the code is right and this document is a bug.

Global rules, no exceptions:

- **Millimetres and degrees.** No other unit reaches the wire.
- **Right-handed, Z-up.** glTF is Y-up; the frontend converts at the Three.js
  boundary and nowhere else.
- **No topological ids are ever persisted.** Selection is by coordinate.
- **`revision` is server-owned.** A revision that exists has been built.
- **The model never emits code.** See `ARCHITECTURE.md` §1.

---

## 1. The ledger

`cadenza_contracts.ledger` / `shared/ts/ledger.ts`. The single source of truth. The
agents patch it, the geometry builds from it, the UI renders from it.

### Document

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `1` | Literal. Bump = migration |
| `project_id` | `str` | `prj_<hex>` |
| `revision` | `int ≥ 0` | **Server-owned.** Bumped only after a successful build. Forbidden to patch |
| `metadata` | `Metadata` | |
| `features` | `Feature[]` (≤ 64) | **Ordered = build order.** Unique ids. First live feature must be `operation: "add"` |

### `Metadata`

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Free text, agent-set |
| `workspace_mode` | `"3D"` | Literal. There is no 2D mode in M1 |
| `global_units` | `"mm"` | Literal. Forbidden to patch |
| `manufacturing_intent` | `str \| null` | **Inert in M1.** Field exists so wiring it up later is additive |
| `tolerance_class` | `str \| null` | **Inert in M1** |

### `Feature` — discriminated on `kind`

Common to every kind:

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | `^feat_[a-z0-9_]{1,60}$`. **Immutable** — forbidden to patch |
| `name` | `str` | Human label: "Main Body", "Centre Hole" |
| `suppressed` | `bool` | Skipped at build time, kept in the tree |
| `operation` | `"add" \| "subtract"` | `hole` is always `subtract` |
| `placement` | `Placement` | See below |
| `target` | `TargetRef \| null` | Provenance of a click-created feature |
| `ai_context` | `{original_intent, assumptions[]}` | Why this exists, what was guessed |
| `parameters` | per kind | All values `> 0` |

| `kind` | `parameters` | Geometry |
|---|---|---|
| `box` | `length` (+X), `width` (+Y), `height` (+Z) | Spans `[-l/2,+l/2] × [-w/2,+w/2] × [0,h]` from origin |
| `cylinder` | `diameter`, `height` | Axis +Z, origin at the centre of the bottom face |
| `hole` | `diameter`, `through` (bool), `depth` (required iff `through: false`) | Cut along the frame's −Z |
| `gear` | `module`, `teeth` (int ≥ 3), `height` (+Z), `pressure_angle_deg` (default 20), `shift` (default 0) | Involute spur gear, axis +Z, tooth 0 centred on +X |

> **A gear has no diameter.** Its size is `module × teeth` (pitch diameter);
> outer diameter is `module × (teeth + 2)` before profile shift. So there are two
> ways to make one bigger and they are not equivalent — more teeth still meshes
> with its partners, a bigger module does not. The axle bore is a separate
> `hole`; a `gear` on its own is a solid wheel.

### `Placement` — where a feature's frame sits

```ts
{ origin: [x, y, z], rotation_deg: [rx, ry, rz] }   // mm, intrinsic XYZ
```

| Kind | `origin` means |
|---|---|
| `box` | Centre of the footprint, at the **bottom** face |
| `cylinder` | Centre of the **bottom** circular face |
| `gear` | Centre of the **bottom** face, as `cylinder` |
| `hole` | Where the axis pierces the face it is cut from |

> **Ruling — through holes ignore `origin[2]`.** A `through: true` hole is cut
> by a cylinder spanning the *current* solid's bounding box along its axis, plus
> margin, re-derived at every rebuild. Only `origin[0]`/`origin[1]` matter.
> This is what makes "make this twice as thick" a one-field patch instead of a
> two-field patch that an LLM will get right two times out of three. Blind holes
> keep `origin[2]` and are correspondingly fragile under edits.

### `TargetRef` — coordinate-based targeting (blueprint §7)

```ts
{ mode: "coordinate", point: [10.5, 0, 20], normal: [0,0,1] | null,
  reference_feature: "feat_main_body_0001" | null }
```

Recorded when a feature was created or edited by clicking. Re-resolved against
the current solid at every rebuild, so it survives topology churn. It is
provenance and re-resolution input — **never** a topological id.

### `Assumption` (inside `ai_context`)

```ts
{ field: "/features/0/parameters/height", value: 20,
  basis: "Plates described as 'sturdy' at this footprint are typically 20 mm",
  confidence: 0.6 }
```

Assuming is fine; hiding is not. The UI renders these as badges.

### The milestone-1 ledger, in full

`cadenza_contracts.fixtures.plate_with_hole()` — import it, do not retype it.

```json
{
  "schema_version": 1,
  "project_id": "prj_milestone1",
  "revision": 1,
  "metadata": {
    "name": "Base Plate", "workspace_mode": "3D", "global_units": "mm",
    "manufacturing_intent": null, "tolerance_class": null
  },
  "features": [
    {
      "id": "feat_main_body_0001", "name": "Main Body", "suppressed": false,
      "placement": { "origin": [0,0,0], "rotation_deg": [0,0,0] },
      "target": null,
      "ai_context": { "original_intent": "a 100x100x20 plate", "assumptions": [] },
      "kind": "box", "operation": "add",
      "parameters": { "length": 100.0, "width": 100.0, "height": 20.0 }
    },
    {
      "id": "feat_centre_hole_0002", "name": "Centre Hole", "suppressed": false,
      "placement": { "origin": [0,0,20], "rotation_deg": [0,0,0] },
      "target": null,
      "ai_context": { "original_intent": "a 10 mm hole in the middle", "assumptions": [] },
      "kind": "hole", "operation": "subtract",
      "parameters": { "diameter": 10.0, "through": true, "depth": null }
    }
  ]
}
```

---

## 2. WebSocket protocol

One connection: `ws://localhost:8000/ws`.
`cadenza_contracts.messages` / `shared/ts/messages.ts`.

- **TEXT frames** — JSON control plane, discriminated on `type`.
- **BINARY frames** — geometry payloads, self-describing (§2.4).
- `message_id` is minted by the **client** (`msg_<hex>`) and echoed by every
  server frame belonging to that turn.
- Unparseable frame or unknown `type` ⇒ `error` with `BAD_MESSAGE`. The server
  never guesses.

### 2.1 Client → server

| `type` | When | Fields |
|---|---|---|
| `client.hello` | First frame after connect | `contract_version`, `session_id?` |
| `user.prompt` | The only frame that mutates anything | `message_id`, `prompt` (1–4000), `target?`, `base_revision?`, `images?` |
| `spatial.probe` | Hover/click preview. Read-only | `message_id`, `point`, `ray?`, `prefer?` |
| `ledger.request` | Resync after a dropped frame | `message_id` |
| `session.reset` | Start over | `message_id` |
| `ping` | Keepalive | `t` |

```json
{ "type": "client.hello", "contract_version": "1.0.0", "session_id": null }
```

Initial generation — no `target`:

```json
{ "type": "user.prompt", "message_id": "msg_7a1c9e2b4d05",
  "prompt": "a 100x100x20 plate with a 10 mm hole in the middle",
  "target": null, "base_revision": 0 }
```

Point & Speak — `target` present. `point` is the Three.js raycast hit against
the rendered GLB, **already converted to ledger coordinates (mm, Z-up)**:

```json
{ "type": "user.prompt", "message_id": "msg_be40f1aa77c2",
  "prompt": "make this twice as thick",
  "target": { "point": [10.5, 0.0, 20.0], "normal": [0.0, 0.0, 1.0],
              "ray": { "origin": [180, 120, 140], "direction": [-0.6, -0.5, -0.62] } },
  "base_revision": 1 }
```

`ray` is optional; supply it when you have the camera, because it disambiguates
a click on a thin wall where two faces are both within tolerance.

Sketch upload — `images` present. **Any image routes the turn to the Draftsman**,
whatever the ledger already holds:

```json
{ "type": "user.prompt", "message_id": "msg_3f81aa02c9d4",
  "prompt": "Build the part in this drawing.",
  "images": [ { "media_type": "image/png", "data": "iVBORw0KGgo…", "name": "plate.png" } ],
  "target": null, "base_revision": 0 }
```

| Field | Rule |
|---|---|
| `media_type` | One of `image/png`, `image/jpeg`, `image/webp`, `image/gif`. Anything else is `BAD_MESSAGE` |
| `data` | **Bare base64.** A `data:` URL prefix is stripped on the way in, and embedded whitespace is tolerated, so a client pasting straight out of a `FileReader` is not punished |
| `name` | Optional, for the UI only. Never reaches the model |

Bounds are advertised in `session.ready.limits` as `max_images` (4) and
`max_image_bytes` (5 000 000). **The byte cap is on DECODED bytes** — that is
what reaches the provider. Both are enforced at parse time, so an oversized
image is refused before a model call is paid for.

Clients are expected to downscale to ≤1568 px on the long edge before encoding;
past that, vision models resolve no further detail and bill by tile. The server
cap is a backstop against abuse, not the intended limit.

```json
{ "type": "spatial.probe", "message_id": "msg_0f2b", "point": [10.5, 0.0, 20.0],
  "ray": null, "prefer": "face" }
```

### 2.2 Server → client

| `type` | Meaning |
|---|---|
| `session.ready` | Handshake accepted. Carries the current ledger and the limits |
| `agent.status` | Progress ticker. Cosmetic — never carries state the UI needs |
| `agent.message` | What the agent says to the user. One per turn |
| `ledger.updated` | The ledger changed **and** the geometry rebuilt. Both, or neither |
| `geometry.ready` | Manifest for the binary frames that follow |
| `geometry.failed` | Build failed; ledger has been **rolled back** |
| `spatial.result` | Answer to `spatial.probe` |
| `error` | Anything else went wrong |
| `pong` | |

```json
{ "type": "session.ready", "session_id": "ses_3f9a2c4b8e11",
  "contract_version": "1.0.0",
  "ledger": { "...empty ledger..." },
  "limits": { "max_prompt_chars": 4000, "max_patch_ops": 32,
              "max_features": 64, "build_timeout_s": 20.0 },
  "agents_available": true }
```

`agents_available: false` means no API key: show a banner, keep the viewport
alive. The geometry path still works.

Phases, in order: `routing → locating → thinking → patching → building →
exporting → done`. `locating` only appears for Point & Speak turns.

```json
{ "type": "agent.status", "message_id": "msg_be40f1aa77c2",
  "phase": "locating", "detail": "Finding what you clicked", "elapsed_ms": 40 }
{ "type": "agent.status", "message_id": "msg_be40f1aa77c2",
  "phase": "thinking", "detail": "Machinist", "elapsed_ms": 120 }
```

```json
{ "type": "agent.message", "message_id": "msg_be40f1aa77c2",
  "text": "Doubled the plate thickness to 40 mm.",
  "assumptions": [], "needs_clarification": false }
```

```json
{ "type": "ledger.updated", "message_id": "msg_be40f1aa77c2", "revision": 2,
  "ledger": { "...full ledger, height now 40..." },
  "patch": [ { "op": "replace",
               "path": "/features/feat_main_body_0001/parameters/height",
               "value": 40.0 } ],
  "summary": "Doubled the plate thickness to 40 mm." }
```

The full ledger is always sent. It is a few kB and the alternative is the client
maintaining its own patched copy — a second source of truth, for nothing.
`patch` is included so the UI can flash the changed row.

```json
{ "type": "geometry.ready", "message_id": "msg_be40f1aa77c2", "revision": 2,
  "blobs": [
    { "artifact": "glb",  "content_type": "model/gltf-binary", "bytes": 15284,
      "sha256": "9f2c…", "url": "/artifacts/2/model.glb" },
    { "artifact": "step", "content_type": "application/step",  "bytes": 48213,
      "sha256": "1ab7…", "url": "/artifacts/2/model.step" }
  ],
  "stats": { "bbox": { "min": [-50,-50,0], "max": [50,50,40] },
             "volume_mm3": 396858.4, "face_count": 8,
             "build_ms": 412, "export_ms": 88 },
  "warnings": [] }
```

```json
{ "type": "geometry.failed", "message_id": "msg_be40f1aa77c2",
  "current_revision": 1,
  "code": "GEOMETRY_INVALID_PARAMETER",
  "message": "A 120 mm hole does not fit in a 100 mm plate.",
  "feature_id": "feat_centre_hole_0002",
  "detail": { "parameter": "diameter", "actual": 120.0, "limit": 98.0,
              "alternatives": [] } }
```

`current_revision` is what the client is **still** showing. Do not clear the
viewport on a failure.

```json
{ "type": "spatial.result", "message_id": "msg_0f2b",
  "hit": { "entity": { "kind": "face", "index": 5, "revision": 1 },
           "point": [10.5, 0.0, 20.0], "normal": [0.0, 0.0, 1.0],
           "distance_mm": 0.0021,
           "feature_id": "feat_main_body_0001", "feature_name": "Main Body",
           "feature_kind": "box",
           "descriptor": "the top face of 'Main Body' (a box), facing +Z, 100.0 x 100.0 mm",
           "area_mm2": 9921.5, "confidence": 0.97 } }
```

`hit: null` means nothing within tolerance — show "nothing selected", do not error.

```json
{ "type": "error", "message_id": "msg_be40f1aa77c2", "code": "AGENT_UNAVAILABLE",
  "message": "No model provider is configured.", "detail": null,
  "recoverable": true }
```

### 2.3 Turn sequence

```
C ──► user.prompt {prompt, target, base_revision}
  ◄── agent.status  routing
  ◄── agent.status  locating      (target present only)
  ◄── agent.status  thinking
  ◄── agent.status  patching
  ◄── agent.status  building
  ◄── agent.status  exporting
  ◄── ledger.updated  {revision: N+1, ledger, patch, summary}
  ◄── geometry.ready  {revision: N+1, blobs[], stats}
  ◄── BINARY  glb   (header {revision: N+1, artifact: "glb"})
  ◄── BINARY  step
  ◄── agent.message  {text}
  ◄── agent.status  done
```

Failure replaces `ledger.updated` + `geometry.ready` with **one** of
`geometry.failed` or `error`, followed by `agent.status done`. `agent.status
done` always terminates a turn — the UI can key its spinner on exactly that.

### 2.4 Binary frames

```
┌────────────┬───────────────────────┬──────────────────┐
│ 4 bytes BE │ UTF-8 JSON header     │ payload bytes    │
│ header len │ (≤ 4096 bytes)        │                  │
└────────────┴───────────────────────┴──────────────────┘

header = { "type": "geometry.blob", "revision": 2, "artifact": "glb", "bytes": 15284 }
```

Codecs are implemented, tested and shared: `encode_blob_frame` /
`decode_blob_frame` (Python), `decodeBlobFrame` (TS). Do not hand-roll.

Rules:

- The `geometry.ready` manifest is sent **before** its blobs, but the client
  **must** key blobs off the binary header's `(revision, artifact)` — never off
  arrival order.
- A blob whose `revision` is older than what the client is showing is **dropped**.
  This is how a fast second edit cannot resurrect a stale mesh.
- Blobs also exist at `GET /artifacts/{revision}/model.{glb|step}` (in-memory
  store, last 5 revisions). That path is for the download button and for
  debugging with `curl`; the render path is the WebSocket.

---

## 3. Geometry service

`cadenza_contracts.geometry`. Dev B implements it; Dev A imports the `Protocol` and
nothing else from `geometry/`. In-process call — same interpreter, same memory.

```python
class GeometryService(Protocol):
    def build(
        self,
        ledger: Ledger,
        *,
        exports: Sequence[ExportFormat] = ("glb", "step"),
        timeout_s: float = 20.0,
    ) -> BuildResult: ...

    def probe(self, ledger: Ledger, query: SpatialQuery) -> SpatialHit | None: ...
```

Both are **pure functions of the ledger**: no hidden state, no session affinity,
no cache surviving a call. Same ledger ⇒ byte-identical GLB and STEP.

```python
@dataclass(slots=True)
class BuildResult:
    ledger_revision: int
    stats: BuildStats          # bbox, volume_mm3, face_count, build_ms, export_ms
    glb: bytes | None          # in memory — no temp files, ever
    step: bytes | None
    warnings: list[str]
```

An empty `ledger.features` is **not** an error: zeroed stats, `glb=None`.

### Error shape

Everything that goes wrong inside `geometry/` raises `GeometryError`:

```python
GeometryError(
    code=GeometryErrorCode.INVALID_PARAMETER,
    message="A 120 mm hole does not fit in a 100 mm plate.",
    feature_id="feat_centre_hole_0002",
    parameter="diameter",
    actual=120.0,
    limit=98.0,                          # ← see below
    alternatives=[("x", 0.0)],
)
```

| Field | Contract |
|---|---|
| `code` | `UNSUPPORTED_FEATURE`, `INVALID_PARAMETER`, `KERNEL_FAILURE`, `EMPTY_RESULT`, `EXPORT_FAILURE`, `TIMEOUT` |
| `message` | One sentence, shown to the user verbatim and fed to the repair prompt |
| `feature_id` | Which ledger feature is at fault, when known |
| `parameter` | The offending scalar's name on that feature |
| `actual` | What it was |
| `limit` | **The nearest value that WORKS** — not "the bound". For a hole diameter that is a ceiling, for a plate thickness a floor, and a caller that had to know which would get it wrong. `setattr(params, err.parameter, err.limit)` fixes the error in *every* case |
| `alternatives` | Other remedies, most-preferred first, as `(parameter, value)`. A hole that does not fit can **move** as well as shrink — always taking the first remedy is how three repair attempts get spent shrinking a bore that needed moving |

`ErrorCode.GEOMETRY_*` is the wire mirror; the server maps `GeometryErrorCode.X`
→ `ErrorCode.GEOMETRY_X` mechanically.

---

## 4. Reverse spatial lookup

The step that turns a click into meaning. Blueprint §5.5, §7.

### Request

```python
SpatialQuery(
    point=[10.5, 0.0, 20.0],   # world-space, ledger coords (mm, Z-up)
    ray=Ray(origin=[180,120,140], direction=[-0.6,-0.5,-0.62]) | None,
    prefer="face",             # "face" | "edge" | "vertex" | "any"
    max_distance_mm=2.0,
)
```

`point` comes from a Three.js raycast against the **tessellated** GLB, so it can
sit microns off the true surface — `max_distance_mm` is the snap tolerance.
When `ray` is present the implementation **should** prefer the first entity the
ray actually strikes over the merely nearest one; when absent,
nearest-by-distance is correct.

### Response — `SpatialHit | None`

| Field | Contract |
|---|---|
| `entity` | `{kind, index, revision}`. **`index` is valid for that revision only.** Never persist it, never send it back as a selector, never cache it across a rebuild |
| `point` | The query point **snapped onto the exact B-rep** |
| `normal` | Outward, for faces |
| `distance_mm` | Query point → snapped point. A large value means a doubtful click |
| `feature_id` / `feature_name` / `feature_kind` | **The attribution — the whole reason this call exists.** Which ledger feature produced this face |
| `descriptor` | A human sentence. Goes into the Machinist prompt **verbatim** and into the user's selection chip. What the user reads is what the model was told |
| `area_mm2` | For faces |
| `confidence` | 0–1. Low when two entities were within tolerance of each other |

`None` ⇒ the server emits `spatial.result` with `hit: null`, or for a
`user.prompt` proceeds with no hit and lets the agent ask.

**Attribution requirement (Dev B):** every face of the built solid must trace
back to the feature that created it. Build incrementally and record which
feature contributed which faces; a face touched by several features belongs to
the **last** one that modified it. Perfect attribution is not required for M1 —
an honest `confidence` is. `descriptor` examples:

```
the top face of 'Main Body' (a box), facing +Z, 100.0 x 100.0 mm
the cylindrical wall of 'Centre Hole' (a hole), 10.0 mm diameter, 20.0 mm deep
a side face of 'Main Body' (a box), facing -Y, 100.0 x 20.0 mm
```

---

## 5. JSON Patch contract

`cadenza_contracts.patch`. **`apply_patch` lives in `shared/` and is the only
implementation.** Dev A calls it; nobody reimplements it.

### Agent output — one schema for every agent

```python
class AgentPatchOutput(BaseModel):
    summary: str                      # one sentence, user-facing, past tense
    patch: list[PatchOp]              # ≤ 32 ops; empty only when asking
    assumptions: list[dict]           # {field, value, basis, confidence}
    needs_clarification: str | None   # set ⇒ patch must be empty
```

Obtained via the provider's **structured output / forced tool use**, so a
well-formed response is guaranteed at the API level and a malformed one is an
API error rather than a parsing guess.

The Draftsman is not a special case — a first build is appends:

```json
{ "summary": "Created a 100 x 100 x 20 mm plate with a 10 mm centre hole.",
  "patch": [
    { "op": "replace", "path": "/metadata/name", "value": "Base Plate" },
    { "op": "add", "path": "/features/-", "value": { "id": "feat_main_body_0001", "kind": "box", "...": "" } },
    { "op": "add", "path": "/features/-", "value": { "id": "feat_centre_hole_0002", "kind": "hole", "...": "" } }
  ],
  "assumptions": [], "needs_clarification": null }
```

### Addressing — id sugar over an ordered array

The blueprint shows an array (`history_tree`) but patch paths like
`/objects/obj_base_01/...`. Ruling: **array storage, id addressing.**

```
/features/feat_main_body_0001/parameters/height     ← what the agent writes
/features/0/parameters/height                       ← what gets applied
```

`normalize_pointer` rewrites the first into the second. Only the segment
immediately after `/features` is treated as an id, and only when it is neither
an integer nor the RFC 6902 append token `-`. Array indices shift under
add/remove and an LLM tracking them across turns is a bug factory; ids are
stable and self-checking. An unresolvable id is an **error**, never a no-op.

### Forbidden paths — rejected outright, no repair retry

`/schema_version` · `/project_id` · `/revision` · `/metadata/global_units` ·
`/metadata/workspace_mode` · `/features/<n>/id`

An agent bumping its own revision or switching units is not confused, it is out
of contract.

### Malformed-patch handling — the complete table

Order of operations, and the exact code for each way it can go wrong:

| # | Check | Failure code | Repair retry? |
|---|---|---|---|
| 1 | Response matches `AgentPatchOutput` | `AGENT_INVALID_OUTPUT` | Yes — once |
| 2 | `len(ops) ≤ 32`; every `path` starts with `/` | `PATCH_INVALID` | **No** |
| 3 | No forbidden path touched | `PATCH_FORBIDDEN_PATH` | **No** |
| 4 | Every `/features/<id>` resolves | `PATCH_UNKNOWN_TARGET` | Yes — retry prompt carries the list of valid ids |
| 5 | `jsonpatch.apply_patch` succeeds (bad index, missing parent, failed `test`) | `PATCH_APPLY_FAILED` | Yes — retry prompt carries the exception text |
| 6 | Result validates as `Ledger` (negative height, blind hole with no depth, duplicate id, first feature not additive) | `LEDGER_INVALID` | Yes — retry prompt carries the pydantic error |
| 7 | `geometry.build()` succeeds | `GEOMETRY_*` | Yes — retry prompt carries `message`, `parameter`, `limit`, `alternatives` |

**Exactly one repair retry, then give up** and surface the error. Two retries
turn a 3-second interaction into a 10-second one and rarely converge.

Guarantees on every failing path:

- `apply_patch` works on a **deep copy**. The live ledger is never mutated, on
  any path, including a partially-applied patch.
- The revision is **not** bumped. Nothing is broadcast. The client's state stays
  exactly what it was.
- On a geometry failure the candidate ledger is **discarded entirely** — no
  half-applied state, ever.

```python
# Dev A's happy path, in full
try:
    candidate = apply_patch(session.ledger, output.patch)   # raises CadenzaError
    result = geometry.build(candidate)                      # raises GeometryError
except CadenzaError as e:
    ... # repair retry per the table, else emit `error`
except GeometryError as e:
    ... # repair retry, else emit `geometry.failed`; session.ledger UNCHANGED
else:
    session.commit(candidate, result)   # bumps revision, snapshots, broadcasts
```

---

## 6. Error codes

| Group | Codes |
|---|---|
| Transport | `BAD_MESSAGE`, `PROTOCOL_MISMATCH`, `UNKNOWN_SESSION`, `BUSY` |
| Agent | `AGENT_UNAVAILABLE`, `AGENT_INVALID_OUTPUT`, `AGENT_REFUSED`, `AGENT_NEEDS_CLARIFICATION`, `AGENT_TIMEOUT` |
| Patch/ledger | `PATCH_INVALID`, `PATCH_FORBIDDEN_PATH`, `PATCH_UNKNOWN_TARGET`, `PATCH_APPLY_FAILED`, `LEDGER_INVALID`, `REVISION_CONFLICT` |
| Geometry | `GEOMETRY_UNSUPPORTED_FEATURE`, `GEOMETRY_INVALID_PARAMETER`, `GEOMETRY_KERNEL_FAILURE`, `GEOMETRY_EMPTY_RESULT`, `GEOMETRY_EXPORT_FAILURE`, `GEOMETRY_TIMEOUT` |
| Spatial | `NO_HIT` |
| Other | `INTERNAL` |

Version handshake: `client.hello.contract_version` major must equal the
server's, else the connection closes with `PROTOCOL_MISMATCH`. Minor bumps are
additive-only during the prototype.
