# CADenza — Architecture

Browser CAD for people who do not know CAD. Natural language in, exact B-rep
out, edited by pointing at the model and saying what you want.

This document records **decisions**. `CONTRACTS.md` is the interface detail,
`WORKSTREAMS.md` is who builds what. Where this document and the blueprint
(`../new_architecture.html`) disagree, **this document governs** — the blueprint
contradicts itself in at least one load-bearing place, which is what section 1
below resolves.

---

## 1. The decision: the model never emits code

The blueprint says both of these:

- **§2** — the LLM patches a JSON ledger, and the backend generates the
  build123d script *from the ledger*.
- **§3** — the Draftsman agent "generates the initial `build123d` Python script."

These are different products. **Ruling: §2. The ledger is the only thing an
agent ever writes. No model-authored code is ever executed, in any pipeline, at
any stage, including "just for the first build".**

### Why

| | Ledger → deterministic translator (CHOSEN) | Model emits build123d script (REJECTED) |
|---|---|---|
| **Blast radius of a bad output** | An invalid ledger fails schema validation before anything runs | Arbitrary Python executes on the server: `os.system`, `open()`, outbound HTTP, the LLM API key in the same process |
| **Sandbox needed** | None. There is nothing to sandbox | A real one — separate container, no network, seccomp, rlimits, no shared filesystem, per-call teardown. Days of work, permanent operational surface, and it never gets removed once shipped |
| **Can you validate the output?** | Yes — `Ledger.model_validate`, before any geometry runs | Not without executing it. "Is this a 100×100×20 plate?" is undecidable from source text |
| **Point & Speak edits** | RFC 6902 patch on a JSON document. Trivial to apply, diff, revert, show in a UI | Patching Python source, or regenerating the whole script and hoping the untouched parts came back identical |
| **Undo / history** | Snapshot the ledger per revision | Snapshot source text and pray it still runs |
| **Determinism** | Same ledger ⇒ byte-identical GLB. Cacheable by hash | Same prompt ⇒ different script ⇒ different mesh. Not reproducible |
| **Debuggability** | A failing build points at a feature id and a parameter | A traceback inside generated code nobody wrote |
| **Expressive ceiling** | **Limited to the ledger vocabulary — the real cost of this choice** | Anything build123d can do |
| **Prior art** | draftsmith shipped on exactly this invariant ("the model never emits code") and it held | — |

### What we give up, and how we pay for it

The vocabulary is the ceiling. A request outside it (`box`, `cylinder`, `hole`,
`gear` today) cannot be built at all — so it must be **declined out loud**, never
approximated. The agent returns `needs_clarification`, or geometry raises
`UNSUPPORTED_FEATURE`; there is no path where the user gets a silently wrong
part. Widening the vocabulary is a schema entry plus a translator branch — data
and a `match` arm, not new architecture. Budget one afternoon per feature kind.

### Consequences that are now non-negotiable

1. **The translator makes build123d API calls directly.** It does not compose
   Python source and `exec` it. No `eval`, no `exec`, no `compile`, no string
   that becomes code — including strings the *translator itself* writes. A
   ledger-to-source generator is the same RCE class with extra steps.
2. **The Draftsman is not special.** First build and later edits both go
   through one path: agent → `AgentPatchOutput` → validate → apply → rebuild.
   The Draftsman just emits `add` ops against `/features/-` on an empty ledger.
   One output schema, one validator, one failure mode.
3. **Numbers only.** There is no expression language in the ledger (`"width":
   "length - 2*inset"` is not a thing). draftsmith needed an AST-allowlist
   evaluator for that; we are not paying for one in a prototype.
4. **The build123d call sites are the trust boundary** and they are ordinary
   reviewed code in `geometry/`. That is the entire security story, and it fits
   in a sentence — which is the point.

> A build123d script *can* still be shown to the user, as a read-only "here is
> what this ledger means" panel generated from the ledger. It is a **view**,
> never a mechanism: nothing ever reads it back.

**Translator input schema:** `cadenza_contracts.ledger.Ledger`. **Owned by the
architect** (`shared/`), implemented by Dev B in `geometry/`, consumed by Dev A.
Neither dev may add a field to it locally.

---

## 2. Shape of the system

```
browser (Dev C)                    server (Dev A)                geometry (Dev B)
──────────────                     ──────────────                ────────────────
React + Three.js                   FastAPI /ws                   build123d
  │  user.prompt {prompt, target}     │                            │
  ├──────────── WS text ─────────────►│                            │
  │                                   │ probe(ledger, point) ─────►│  reverse
  │                                   │◄──────── SpatialHit ───────┤  spatial lookup
  │                                   │                            │
  │                                   │ Machinist(prompt, hit,     │
  │                                   │           ledger) → patch  │
  │                                   │ apply_patch() → Ledger'    │
  │                                   │ build(Ledger') ───────────►│  translate +
  │                                   │◄──── BuildResult(glb,step)─┤  export in memory
  │◄─ ledger.updated ─────────────────┤                            │
  │◄─ geometry.ready + binary frames ─┤                            │
  │  GLTFLoader.parse → swap mesh     │                            │
```

One WebSocket. Text frames are the JSON control plane; binary frames carry GLB
and STEP bytes. Geometry is an **in-process function call**, not a service — no
queue, no container, no serialisation of the ledger to a subprocess. It can
become a service later precisely because the interface is a `Protocol`.

### Module map

| Path | Owner | Contains | May import |
|---|---|---|---|
| `shared/cadenza_contracts/` | Architect | Ledger, patch semantics + apply, geometry `Protocol`, every WS frame, error codes, golden fixtures | pydantic, jsonpatch |
| `shared/ts/` | Architect | TypeScript mirror of the same, plus the binary frame decoder | nothing |
| `server/` | **Dev A** | FastAPI app, `/ws` handler, session + revision store, coordinator, Draftsman, Machinist, LLM client | `cadenza_contracts`, `cadenza_geometry` (via the Protocol only) |
| `geometry/` | **Dev B** | Ledger→build123d translator, GLB/STEP export, spatial lookup, face attribution | `cadenza_contracts`, build123d |
| `web/` | **Dev C** | React app, Three.js viewport, WS client, Point & Speak UI, feature tree | `@cadenza/shared` |

**Dev A never imports build123d. Dev B never imports FastAPI or an LLM SDK.
Nobody edits `shared/`.** A contract that is wrong gets fixed once, here.

### The turn, exactly

1. `user.prompt` arrives. If a turn is already running for this session → `BUSY`.
   If `base_revision` is stale → `REVISION_CONFLICT`. (Serialised per session;
   there is no merge story and a prototype does not need one.)
2. **Coordinator** — a rule, not an LLM call: `target` present → Machinist;
   ledger empty → Draftsman; otherwise → Machinist with no hit. Spending a model
   call and 800 ms on a three-line `if` is how prototypes get slow.
3. If `target`: `geometry.probe()` → `SpatialHit`, whose `descriptor` goes into
   the prompt verbatim. The user and the model read the same sentence.
4. Agent returns `AgentPatchOutput` (structured output — a malformed response is
   an API-level error, not a parsing guess).
5. `apply_patch(ledger, ops)` → new `Ledger`, **revision unchanged**.
6. `geometry.build(new_ledger)`.
7. Success → bump revision, snapshot, emit `ledger.updated` + `geometry.ready` +
   binary frames. Failure → **discard the new ledger entirely** and emit
   `geometry.failed`. There is no state in which the ledger has advanced but the
   geometry has not: a revision that exists has been built.

---

## 3. Milestone 1

The single sentence: **type a plate, see it, click its top face, say "make this
twice as thick", watch it rebuild.**

### In

| # | Step | Owner |
|---|---|---|
| 1 | "a 100×100×20 plate with a 10 mm hole in the middle" → ledger | A |
| 2 | Ledger → build123d solid | B |
| 3 | GLB + STEP exported **in memory**, no temp files | B |
| 4 | GLB over the WebSocket, rendered by Three.js | A + C |
| 5 | Click the top face; floating prompt box | C |
| 6 | Click point → `probe()` → `SpatialHit` with `feature_id` + `descriptor` | B |
| 7 | Machinist → RFC 6902 patch → rebuild → new GLB swapped in | A |

Vocabulary: `box`, `cylinder`, `hole`, `gear`. That is enough for milestone 1 and
its obvious neighbours ("add a second hole", "make it round").

`gear` is the one that proves the widening claim above, so it is worth saying
what it actually cost: a schema entry, a translator branch, and one new module
of planar maths (`geometry/cadenza_geometry/gear.py`). No kernel feature, no new
solid-modelling operation — a spur gear is a closed 2D outline that gets
extruded, exactly like FreeCAD's own gear workbenches build one. The involute
lives in the profile; the solid step is the same `extrude` a sketched pocket
gets. It is also the first kind whose SIZE is not a field: a gear is
`module` x `teeth`, so "make it bigger" has two different answers and the
Machinist prompt has to distinguish them.

### Out — do not build these

| Not in milestone 1 | Why |
|---|---|
| **2D sketch canvas / Fabric.js** | Explicitly out of scope. No 2D mode, no `workspace_mode: "2D"` |
| **Manufacturing intent, tolerance classes** | Explicitly out of scope. The ledger fields exist and stay `null`; nothing reads them |
| **occt-wasm in the browser** | See §4 — deferred, and milestone 1 does not depend on it |
| ~~Sketch/photo upload, vision~~ | **Now IN — built ahead of schedule. See §7** |
| Fillets, chamfers, shells, patterns, revolves | Vocabulary is 3 kinds |
| Expression/parametric relations | Numbers only |
| Undo/redo UI | Server snapshots every revision; no buttons yet |
| Persistence, accounts, multi-user | One in-memory session per connection. Restart = new part |
| STEP download button | The bytes reach the client; a save button is 20 minutes whenever it is wanted |
| Optimistic local transforms | See §4 |
| LangGraph | See §4 |

---

## 4. Blueprint items deferred or rejected

| Blueprint | Ruling | Reasoning |
|---|---|---|
| §3 Draftsman emits a build123d script | **Rejected** | §1. The whole architecture turns on this |
| §1/§4/§5 `occt-wasm` worker does the raycast against STEP | **Deferred past M1** | Three.js already raycasts the GLB and gives a world-space point; the server then snaps that point to the exact B-rep and answers with the *authoritative* entity. Shipping occt-wasm in M1 buys sub-100 ms hover latency at the cost of a multi-MB WASM bundle, a Comlink worker, a second STEP parser and a second source of truth about what the user clicked. Do it later as a **latency cache in front of an unchanged contract** — `spatial.probe` stays the fallback and stays authoritative |
| §1 AG-UI protocol | **Rejected as a dependency** | Our message set is AG-UI-shaped (`message_id` correlation, phase events, streamed artifacts) but defined in `shared/messages.py` where we can change it in a minute. Adopting a spec to move JSON between two processes we both own is cost without benefit |
| §1 LangGraph | **Deferred** | Four "agents" of which two are live in M1, routed by an `if`. A graph framework earns its keep at cyclic multi-turn planning; we have a linear pipeline. Direct SDK calls, one function per agent. Revisit when there is a real loop |
| §2 "bitemporal" ledger | **Rejected for M1** | Bitemporality (valid-time *and* transaction-time) answers "what did we believe the part was on Tuesday?" Nobody is asking. We keep a linear list of revision snapshots, which is what undo actually needs |
| §2 patch path `/objects/obj_base_01/...` vs array `history_tree` | **Resolved** | The ledger stores `features` as an **ordered array**; agents may address a feature **by id**, and `normalize_pointer` rewrites `/features/<id>/…` → `/features/<n>/…`. Best of both: stable addressing for the model, ordered build semantics for the kernel. Unresolvable id ⇒ `PATCH_UNKNOWN_TARGET`, never a silent no-op |
| §3 Engineer agent (smart defaults) | **Deferred to a dict** | "Fits a wall screw" → `diameter: 4.5` is a lookup table, not an inference. A static dict in `server/agents/defaults.py`, consulted by the other agents. No LLM call, no latency, no nondeterminism |
| §3 Draftsman vision | **BUILT — see §7** | The prediction held exactly: the agent boundary did not move. An image is a content block on the way in and the output schema is untouched |
| §5 "rough local transformation" while waiting | **Rejected** | A local guess that disagrees with the rebuild teaches users not to trust the viewport, which is the one thing this product sells. Loading state + phase ticker instead. Rebuild target: under 3 s |
| §4 STEP → browser to maintain the B-rep | **Kept, unused in M1** | STEP is exported and sent because it is free once built, and it is the export format users need. Nothing in the browser parses it yet |
| §7 coordinate-based targeting | **Adopted wholesale** | The single best idea in the blueprint. No topological id is ever persisted — not in the ledger, not in a patch, not in a selector. `EntityRef.index` is valid for one revision and is flagged as such in the type |

---

## 5. Rulings on things the blueprint left ambiguous

These bit us while writing the contracts. They are settled; they are not
open questions.

1. **A through hole ignores its own `origin.z`.** It is cut by a cylinder
   spanning the current solid's bounding box. Otherwise "make the plate twice
   as thick" leaves a 10 mm hole stranded in the middle of a 40 mm plate, and
   milestone 1 fails at step 7. This makes the thicken edit a genuine
   **one-field patch**. Blind holes do keep `origin.z` and are correspondingly
   fragile under edits — a known, documented limitation.
2. **Coordinates are Z-up throughout.** glTF is Y-up; the frontend converts at
   the Three.js boundary and **nowhere else**. Anything sent to the server is in
   ledger coordinates. An axis convention that varies by module is a whole
   afternoon of nobody's favourite bug.
3. **`revision` is server-owned** and is in `FORBIDDEN_PATHS`. An agent cannot
   patch it, and it is bumped only after a successful build.
4. **Feature ids are immutable.** `/features/<n>/id` is a forbidden path —
   renaming an id breaks click-attribution and snapshot history.
5. **One turn at a time per session.** Concurrent prompts get `BUSY`. Optimistic
   concurrency via `base_revision` for the case where the UI is behind.
6. **Failed build ⇒ full rollback.** No half-applied state, ever.
7. **The `descriptor` string is contract, not decoration.** The server sends the
   same sentence to the user's selection chip and to the model's prompt, so what
   the user reads is what the model was told.

---

## 6. Needs a ruling from you

| # | Question | What we assumed meanwhile |
|---|---|---|
| 1 | **Which LLM provider and models?** draftsmith routes per stage across Anthropic + OpenRouter and its keys are still pending. Do we reuse that `.env`, or is CADenza single-provider? | Single provider, one model for both agents, key from `CADENZA_LLM_API_KEY`. Structured output is **required** — a provider without forced-tool-use/JSON-schema output changes Dev A's error handling |
| 2 | **Non-mm input** ("make it 4 inches wide"). Convert silently, convert-and-flag, or refuse? | Convert to mm, record an `Assumption` with the conversion as its basis. The ledger stays mm-only |
| 3 | **Ambiguous "this"** — the user clicks a hole's wall and says "make this twice as thick". The clicked *feature* has no thickness. Ask, or reinterpret as the parent body? | **Ask** (`needs_clarification`). Guessing wrong here is worse than one extra round-trip, and the click already told us it was ambiguous |
| 4 | **Does the prototype need persistence** (reload the page and your part is still there)? | No. In-memory, one session per connection |
| 5 | **Undo/redo in the demo?** Snapshots exist; only the buttons and two message types are missing | Not in M1. Cheap to add — say the word |
| 6 | **Is a visible "here is the build123d for your part" panel wanted?** It is a strong trust feature and, given §1, it is read-only by construction | Not in M1 |

---

## 7. Image → 3D (built)

**A drawing is an input to the Draftsman, and nothing else in the system
changed.** `UserPrompt.images` carries base64 image blocks; their presence
routes the turn to the Draftsman and appends a drawing-reading block to its
system prompt. The output schema, the patch, the validator, the build and the
commit are byte-for-byte the text path.

### What this is, and what it is not

This reads **specifications** — it does not reconstruct meshes. There is no path
from a point cloud to a ledger, and none is attempted: the model never measures
depth, it recognises a form and names it.

The medium is not the test, the **form** is. A photograph whose subject is
already in the vocabulary — a disc, a plate, a shaft, a spur gear — is built at
an assumed scale with every dimension marked low-confidence, because a form the
vocabulary has is a form we can build regardless of how it arrived. A photograph
of an arbitrary object is still declined, on the same grounds as a lofted
transition in a drawing: the form is not in the vocabulary.

§1's rule is unchanged and is what makes the feature safe: the model emits
ledger features, so a misread image is a *wrong part you can see and edit*,
never arbitrary code.

### The ceiling rule, in two cases

The interesting failure mode showed up immediately in testing. Told simply to
"refuse anything outside the vocabulary", the model was handed an L-bracket with
an R20 fillet and an M8 tapped hole — and built it as two boxes, mentioning the
dropped fillet only in the assumptions list. That is not a refusal, and burying
it is exactly the "silently wrong part" §1 forbids. But a hard refusal would
also be wrong: the L *form* is perfectly expressible, and refusing to build it
would be useless. So the rule distinguishes:

| Case | Rule |
|---|---|
| Overall **form** is expressible, a **detail** is not (fillet, chamfer, thread) | Build it, and name the omission **in the `summary` the user reads first** — not only in `assumptions` |
| The **form** itself is outside the vocabulary (revolve, sweep, loft, freeform, sheet-metal bend, assembly) | `needs_clarification`, naming what cannot be built. Never approximate |
| The subject is a **photograph**, but its form IS in the vocabulary | Build it at an assumed scale, every dimension an assumption at confidence ≤ 0.4. Declining a form we can build, because of the file it arrived in, is a false negative — not safety |

### Decisions

| Decision | Why |
|---|---|
| **Base64 in the `user.prompt` frame**, not a binary frame or an upload endpoint | An image is meaningless without the prompt it belongs to. A second transport needs upload ids, a lifetime, and a story for the orphan case where the upload lands and the prompt never does. One frame, one turn, no lifecycle |
| **Client downscales to ≤1568 px** before encoding | Vision models stop resolving detail past that and bill by tile, so larger images are paid for twice and buy nothing. It also makes the 5 MB server cap a backstop against abuse rather than something an honest user trips over |
| **PNG first, JPEG only if PNG is fat** | Line art is mostly flat white and compresses better as PNG; JPEG ringing lands directly on the thin strokes the model must read. A fat PNG is the signature of a photograph, which is where JPEG belongs |
| **Re-encode through a canvas** | Drops EXIF as a side effect. A user photographing a part in their garage should not upload their home address with it |
| **Cap enforced on DECODED bytes** | That is what reaches the provider, and what a malicious client would try to inflate |
| Every inferred number lands in `assumptions`, surfaced in the UI | A dimension read off a drawing and one invented from proportions must never look alike |
