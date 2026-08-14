# CADenza — Workstreams

Three people, three directories, one contract. Read `ARCHITECTURE.md` §1 (why
the model never emits code) and `CONTRACTS.md` (what your neighbour expects),
then build only your own section.

```
cadenza/
  server/     Dev A   FastAPI, WebSocket, sessions, ledger, LLM agents
  geometry/   Dev B   build123d translator, GLB/STEP export, spatial lookup
  web/        Dev C   React, Three.js, Point & Speak
  shared/     ARCH    schemas and contracts — import, never edit
```

### Do this first — the contracts have landed and moved

All three of you started against a half-landed `cadenza_contracts` and wrote
shims to cope. **The package is now complete** — `geometry.py` and `messages.py`
exist, `apply_patch` is implemented and 20 tests pass. It lives at
`cadenza/shared/cadenza_contracts/`. **The import name is unchanged**; only the
location moved, and the empty `contracts/` and `frontend/` scaffolds are gone.

| You | Do this | Then delete |
|---|---|---|
| **Dev A** | `uv pip install -e ../shared`, then `from cadenza_contracts import …` directly | `cadenza_backend/contracts_bridge.py` — it exists only because `__init__` used to fail on missing submodules. It no longer does |
| **Dev B** | `uv pip install -e ../shared` | `cadenza_geometry/contracts.py` — `cadenza_contracts.geometry` is real now, with `GeometryService`, `BuildResult`, `SpatialQuery`, `SpatialHit`, `GeometryError` |
| **Dev C** | tsconfig path alias `@cadenza/shared` → `../shared/ts` (or `npm i ../shared/ts`) | `src/lib/types.ts` — replace with `import type { … } from "@cadenza/shared"`. The binary frame decoder is in there too; do not hand-roll it |

**Dev A: your spike is in `backend/`. The assigned directory is `server/`** —
`git mv backend server` and fix the two path references. The layout is fixed;
`backend/` is the only thing still off it.

**Dev C: `occt-wasm` and `comlink` are already in your `package.json`.** Keep the
spike if it is teaching you something, but **milestone 1 must not depend on it**
(`ARCHITECTURE.md` §4). The server's `spatial.probe` is the authoritative answer
to "what did I click"; occt-wasm is a latency cache in front of an unchanged
contract, later.

### Rules for everyone

1. **Never edit `shared/`.** A contract that is wrong gets fixed once, by the
   architect, for all three of you. A local patch means silent divergence and a
   lost integration day.
2. **Import the fixtures.** `cadenza_contracts.fixtures` has the milestone-1 ledger
   and both patches. Hand-typed JSON in a test is a test of your typing.
3. **mm and degrees. Z-up.** The only axis conversion in the entire system is in
   `web/src/viewport/`.
4. **No `exec`, no `eval`, no `compile`, no generated source.** Not for
   convenience, not "just for the translator". See `ARCHITECTURE.md` §1.
5. Run `cd shared && uv run pytest` before you start. 20 tests; they pin the
   behaviour you are about to rely on.

---

## Dev A — `server/` (backend + agents)

### Files

| File | Contents |
|---|---|
| `pyproject.toml` | deps: `fastapi`, `uvicorn[standard]`, `websockets`, the LLM SDK, `cadenza-contracts` (`-e ../shared`), `cadenza-geometry` (`-e ../geometry`) |
| `cadenza_server/app.py` | FastAPI app; `GET /health`; `WS /ws`; `GET /artifacts/{revision}/model.{glb,step}` |
| `cadenza_server/session.py` | `Session`: current `Ledger`, revision snapshots (list, for undo later), artifact store, **per-session turn lock** |
| `cadenza_server/turn.py` | The pipeline: route → locate → agent → `apply_patch` → `build` → commit → broadcast. One repair retry per `CONTRACTS.md` §5 |
| `cadenza_server/artifacts.py` | In-memory blob store, sha256, keeps the last 5 revisions |
| `cadenza_server/agents/llm.py` | Provider client. **Structured output / forced tool use is mandatory** — `AgentPatchOutput` is the response schema |
| `cadenza_server/agents/coordinator.py` | Rules, **not** a model call: `target` → Machinist; empty ledger → Draftsman; else Machinist |
| `cadenza_server/agents/draftsman.py` | Text → `AgentPatchOutput` of `add` ops on `/features/-` |
| `cadenza_server/agents/machinist.py` | prompt + `SpatialHit` + ledger → `AgentPatchOutput` |
| `cadenza_server/agents/defaults.py` | The Engineer, demoted to a dict: `"wall screw" → 4.5` etc. No LLM call |
| `cadenza_server/agents/prompts.py` | System prompts. The Machinist prompt embeds `hit.descriptor` **verbatim** |
| `tests/` | Turn pipeline against a stub `GeometryService` and a stub LLM |

### Contract to satisfy

- Parse with `parse_client_message`, serialise with `dump_message`. Nothing
  hand-rolled, no `json.dumps` on a wire frame.
- Apply patches with `cadenza_contracts.patch.apply_patch`. **Do not reimplement
  it**, do not `jsonpatch.apply` directly.
- Consume geometry through the `GeometryService` **Protocol only**. Import
  `build123d` and the contract is broken.
- Bump `revision` yourself, and only after `build()` returns. Rollback on any
  failure — the live ledger is never mutated by a failed turn.
- Emit phases in order (`routing, locating, thinking, patching, building,
  exporting, done`); **every turn ends with `agent.status done`**, success or
  failure. Dev C keys the spinner on it.
- Manifest (`geometry.ready`) before its binary frames; frame them with
  `encode_blob_frame`.
- Second prompt while a turn runs → `BUSY`. Stale `base_revision` →
  `REVISION_CONFLICT`.
- Missing API key → `session.ready` with `agents_available: false`, and
  `AGENT_UNAVAILABLE` on any prompt. The server still starts and still builds.

### Definition of done (milestone 1)

- [ ] `uvicorn` starts; `GET /health` returns ok with `geometry` and `agents` flags.
- [ ] `client.hello` with a bad major version is closed with `PROTOCOL_MISMATCH`.
- [ ] `user.prompt` with the milestone-1 sentence on an empty ledger produces a
      ledger **equal to `fixtures.plate_with_hole()`** modulo ids and revision.
- [ ] `user.prompt` with `target.point = [10.5, 0, 20]` and "make this twice as
      thick" produces exactly one `replace` op on the box's `height`, value `40`.
- [ ] Both turns emit the full phase sequence, `ledger.updated`,
      `geometry.ready`, two binary frames, `agent.message`, `agent.status done`.
- [ ] Every row of the `CONTRACTS.md` §5 malformed-patch table has a test using
      a stub LLM that returns that bad output, asserting the code **and** that
      `session.ledger` is unchanged.
- [ ] A geometry failure leaves the client on the previous revision and sends
      `geometry.failed` carrying `parameter` / `limit`.

---

## Dev B — `geometry/` (build123d)

### Files

| File | Contents |
|---|---|
| `pyproject.toml` | deps: `build123d`, `cadenza-contracts` (`-e ../shared`) |
| `cadenza_geometry/__init__.py` | Exports `Build123dGeometry()` — satisfies `GeometryService` |
| `cadenza_geometry/translate.py` | `Ledger` → build123d solid. **Direct API calls. No source text, no `exec`.** |
| `cadenza_geometry/export.py` | GLB + STEP into `io.BytesIO`. No temp files, no disk |
| `cadenza_geometry/probe.py` | `SpatialQuery` → `SpatialHit`, including the `descriptor` sentence |
| `cadenza_geometry/attribution.py` | face → `feature_id`. Build incrementally, record contributions |
| `tests/` | Golden tests on `fixtures.plate_with_hole()` |

### Contract to satisfy

- `build()` and `probe()` are **pure functions of the ledger**. No hidden state,
  no session affinity, no cache surviving a call. Same ledger ⇒ byte-identical
  GLB and STEP.
- Placement conventions exactly as `CONTRACTS.md` §1 — in particular
  **`through: true` holes ignore `origin[2]`** and span the current solid's
  bounding box. Get this wrong and milestone 1 step 7 fails.
- Empty `features` is **not** an error: zeroed stats, `glb=None`.
- Every failure is a `GeometryError` with `feature_id`, `parameter`, `actual`
  and **`limit` = the nearest value that works** (not "the bound"). Populate
  `alternatives` when moving a feature is as valid a fix as shrinking it.
- An unknown `kind` is `UNSUPPORTED_FEATURE`. Never approximate, never skip.
- `EntityRef.index` you return is valid for that revision only. Never persist it
  anywhere.
- `probe()` returns `None` rather than raising when nothing is within
  `max_distance_mm`.

### Definition of done (milestone 1)

- [ ] `build(fixtures.plate_with_hole())` returns GLB and STEP bytes; GLB opens
      in an external glTF viewer, STEP opens in FreeCAD.
- [ ] `stats.bbox == {min: [-50,-50,0], max: [50,50,20]}`, `volume_mm3` ≈
      `100·100·20 − π·5²·20 = 198429.2`, `face_count == 8`.
- [ ] `build(plate_with_hole(height=40))` gives z-max 40 and the hole **still
      goes all the way through** — the through-hole rule, tested.
- [ ] `probe(ledger, SpatialQuery(point=[10.5, 0, 20]))` returns a `face` hit
      with `feature_id` = the box, `normal ≈ [0,0,1]`, `distance_mm < 0.01`, and
      a `descriptor` a person would recognise.
- [ ] `probe` on the hole's wall attributes to the **hole**, not the box.
- [ ] `probe` at `[500, 500, 500]` returns `None`.
- [ ] Two builds of the same ledger produce identical bytes.
- [ ] `build()` completes in **under 2 s** for the milestone-1 part.
- [ ] Bad parameters (hole wider than the plate) raise `INVALID_PARAMETER` with
      a usable `limit`.

---

## Dev C — `web/` (React + Three.js + Point & Speak)

### Files

| File | Contents |
|---|---|
| `package.json`, `vite.config.ts` | React + TS + Vite; proxy `/ws` and `/artifacts` → `:8000` |
| `tsconfig.json` | Path alias `@cadenza/shared` → `../shared/ts` |
| `src/lib/ws.ts` | Typed client: connect, hello/version check, `send(msg)`, `on(type, fn)`, **binary demux via `decodeBlobFrame`**, reconnect |
| `src/lib/store.ts` | `ledger`, `revision`, `phase`, `messages`, `selection`. Server-authoritative — never patch the ledger locally |
| `src/viewport/Viewport.tsx` | Three.js scene, camera, lights, grid; `GLTFLoader.parse(arrayBuffer)`; swap the mesh on `geometry.ready` |
| `src/viewport/raycast.ts` | Click → world point → **ledger coordinates** (the one axis conversion in the system) |
| `src/components/PromptBox.tsx` | Floating input pinned to the click, screen-projected from the 3D point |
| `src/components/Chat.tsx` | Prompt history, agent replies, assumption badges |
| `src/components/StatusTicker.tsx` | Phase spinner, keyed on `message_id`, cleared by `agent.status done` |
| `src/components/FeatureTree.tsx` | `ledger.features` list; hover highlights, click selects |
| `src/workers/` | **Empty in milestone 1.** occt-wasm lands here later — see `ARCHITECTURE.md` §4 |

### Contract to satisfy

- Import types from `@cadenza/shared`. Do not redeclare a wire type locally.
- **Mint `message_id`** (`msg_` + random hex) and send `base_revision`.
- Convert the raycast hit **out of glTF space into ledger coordinates before
  sending**. Load the GLB under a single `modelRoot` group and convert with
  `modelRoot.worldToLocal(hit.point.clone())` — one line, using Three.js's own
  math, impossible to get subtly wrong.
- Key blobs off the **binary header** `(revision, artifact)`, never arrival
  order. **Drop any blob older than the revision you are showing** — this is
  what stops a fast second edit resurrecting a stale mesh.
- On `geometry.failed`: keep showing `current_revision`. **Do not clear the
  viewport.** Show the message; the part on screen is still valid.
- `hit: null` on a probe means "nothing selected", not an error.
- No local geometry guessing. No optimistic transform. Loading state only
  (`ARCHITECTURE.md` §4).
- No occt-wasm, no Fabric.js, no 2D canvas in milestone 1.

### Definition of done (milestone 1)

- [ ] Connects, handshakes, renders the empty state without errors.
- [ ] Typing the milestone-1 sentence shows the phase ticker, then a plate with
      a hole appears in the viewport, correctly oriented (Z up, 100 mm across).
- [ ] Orbit / pan / zoom. Camera framed to `stats.bbox` on the first build.
- [ ] Clicking the top face opens the floating prompt box at the cursor and
      shows the hit `descriptor` in a selection chip.
- [ ] "make this twice as thick" → the plate visibly doubles, camera undisturbed,
      no flicker to empty between meshes.
- [ ] The feature tree lists "Main Body" and "Centre Hole" and updates on rebuild.
- [ ] A `geometry.failed` shows an error toast and leaves the model on screen.
- [ ] Killing the server mid-session shows a disconnected state and reconnects.

---

## Integration

Two checkpoints, before the end-to-end demo:

| Checkpoint | Who | Test |
|---|---|---|
| **I1 — geometry ↔ server** | A + B | Dev A's turn pipeline against Dev B's real `Build123dGeometry` on `fixtures.plate_with_hole()`. Until B lands, A stubs the Protocol |
| **I2 — server ↔ web** | A + C | A `--fake-agent` server flag that skips the LLM and replays `fixtures.DRAFTSMAN_PATCH` / `THICKEN_PATCH`. Unblocks C entirely and makes the demo reproducible without an API key. **Dev A: build this early — it is C's whole runway** |

### Known integration risks

| Risk | Owner | Do this |
|---|---|---|
| **glTF Y-up vs ledger Z-up.** If build123d's exporter applies its own Y-up conversion, `web/`'s single root rotation is wrong by 90° | **Dev B** | Check what `export_gltf` actually writes on day one and say so in the channel. It is one line in `web/` — but only if it is caught deliberately, rather than by two people arguing about which way is up |
| Face attribution is fuzzy on shared/coincident faces | Dev B | Return an honest `confidence`; do not fake 1.0 |
| The Machinist edits the hole when the user clicked the plate | Dev A | Test it with the real `SpatialHit` from I1, not a hand-written one |
| Ambiguous "this" (user clicks a hole wall, says "twice as thick") | Dev A | `needs_clarification` — ask. Do not guess (`ARCHITECTURE.md` §6.3) |
