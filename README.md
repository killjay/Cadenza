# CADenza

Browser CAD for people who do not know CAD. Natural language in, exact B-rep
out, edited by pointing at the model and saying what you want.

You describe a part — or drop a dimensioned drawing into the composer — and an
agent writes a **JSON ledger** describing it. A deterministic translator turns
that ledger into real [build123d](https://build123d.readthedocs.io) geometry and
exports GLB (for the viewport) and STEP (for manufacturing). To edit, click a
face in the 3D view and say what should change.

The load-bearing invariant: **the model never emits code.** Agents only ever
produce RFC 6902 patches against the ledger, which are schema-validated before
anything runs. There is no `exec`, no sandbox, and no path where a model writes
Python that the server executes. See [ARCHITECTURE.md](ARCHITECTURE.md) §1.

---

## Running it on localhost

### Prerequisites

| | Version | Notes |
|---|---|---|
| Python | **3.11+** (3.12 tested) | macOS system Python is 3.9 — too old. Use `uv`, pyenv or Homebrew. |
| Node.js | **20+** (24 tested) | |
| [`uv`](https://docs.astral.sh/uv/) | any recent | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

An API key for **Anthropic**, **OpenRouter**, or **DeepSeek**. Without one the
server still starts and the transport still works, but no agent turn can run.
Image input needs Anthropic or OpenRouter — DeepSeek's chat endpoint is
text-only and vision turns are automatically rerouted off it.

### 1. Clone and configure

```bash
git clone https://github.com/killjay/Cadenza.git
cd Cadenza

cp .env.example .env
$EDITOR .env          # set ANTHROPIC_API_KEY=sk-ant-…
```

### 2. Build the Python environment

> **Read this or nothing will render.** Geometry is an *in-process* call, not a
> separate service — the process serving WebSockets is the same process that
> runs build123d, so it needs the backend's dependencies **and** build123d in
> one interpreter. That combined environment is `geometry/.venv`. This is the
> single most common way to get a working server that produces no geometry.

```bash
uv venv --python 3.12 geometry/.venv
uv pip install --python geometry/.venv/bin/python -e ./shared -e ./geometry -e ./backend
uv pip install --python geometry/.venv/bin/python "pytest>=8" "pytest-asyncio>=0.24"
```

That installs the three local packages as editable (`cadenza-contracts`,
`cadenza-geometry`, `cadenza-backend`) and pulls FastAPI, uvicorn, anthropic,
websockets and build123d in behind them.

### 3. Install the web dependencies

```bash
cd web && npm install && cd ..
```

`occt-wasm` ships a ~22 MB `.wasm` payload, so the first install is not fast.

### 4. Start both processes

**Shell 1 — API server (port 8000):**

```bash
geometry/.venv/bin/python -m uvicorn cadenza_backend.app:app --reload --port 8000
```

**Shell 2 — web dev server (port 5173):**

```bash
cd web && npm run dev
```

Then open <http://localhost:5173>. Vite proxies `/ws` and `/api` through to
port 8000, so the browser only ever talks to 5173 — no CORS setup needed for
local development.

### 5. Verify the geometry kernel actually loaded

```bash
curl -s localhost:8000/health
```

```jsonc
{ "geometry": "build123d", "stubbed": ["wire envelope"] }              // ✅ real geometry
{ "geometry": "stub (ModuleNotFoundError: …)", "stubbed": ["geometry build", …] }  // ❌ wrong interpreter
```

If you get the second one, you are running the server from an environment
without build123d (most likely `backend/.venv`). The degradation is deliberate —
a venv without OCCT can still run the transport, the agents and the whole test
suite — but the viewport will stay empty and the transcript will say it *"built
this without a geometry kernel."*

### Try it

Type a part description into the right-hand pane — *"a 100×100×20 plate with a
20 mm hole in the centre"* — or attach a sketch with the paperclip, drag an
image onto the pane, or ⌘V a screenshot. Any image routes the turn to the
Draftsman. Dimensioned engineering drawings work far better than photographs;
see [ARCHITECTURE.md](ARCHITECTURE.md) §7 for why a photo of a real object is
declined rather than approximated.

The feature vocabulary today is `box`, `cylinder`, `hole` and `gear`. Anything
outside it is refused out loud rather than approximated — that is a deliberate
design choice, not a bug.

---

## Tests

No test makes a real model call — the provider is faked throughout, so the suite
is free and runs offline.

```bash
cd backend  && ../geometry/.venv/bin/python -m pytest -q
cd shared   && ../geometry/.venv/bin/python -m pytest -q
cd geometry && .venv/bin/python -m pytest -q
cd web      && npm run typecheck && npm run build
```

Because the provider is faked, the suite **cannot** tell you whether the
Draftsman reads drawings correctly. Only a live run can, and there is no
substitute for looking at the part it produced.

---

## Layout

```
shared/     Ledger schema, patch semantics, WS frame definitions, error codes,
            golden fixtures — plus shared/ts/, the TypeScript mirror the web
            client imports as @cadenza/shared. Single source of truth.
backend/    FastAPI app, /ws transport, session + revision store, coordinator,
            and the agent personas. Never imports build123d.
geometry/   Ledger → build123d translator, GLB/STEP export, spatial lookup,
            face attribution. Never imports FastAPI or an LLM SDK.
web/        React 19 + Three.js viewport, WS client, Point & Speak UI.
```

One WebSocket carries everything: text frames are the JSON control plane, binary
frames carry GLB and STEP bytes.

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Decisions and why they went that way |
| [CONTRACTS.md](CONTRACTS.md) | Interface detail — schemas, frames, error codes |
| [RUNNING.md](RUNNING.md) | Operational notes and troubleshooting |
| [WORKSTREAMS.md](WORKSTREAMS.md) | Who builds what |

---

## Troubleshooting

**Viewport stays empty, transcript says "built this without a geometry kernel"**
Wrong interpreter — see step 5. Run from `geometry/.venv`.

**`ModuleNotFoundError: cadenza_contracts`**
The editable installs didn't take. Re-run the `uv pip install -e` line from
step 2.

**Agent turns fail immediately**
No API key. Check `.env` has a non-empty `ANTHROPIC_API_KEY` (or OpenRouter /
DeepSeek), and restart the server — settings are cached at process start.

**Dropping an image does nothing**
Vision needs Anthropic or OpenRouter. A DeepSeek-only configuration has no
vision-capable provider to route to.

**WebSocket won't connect**
The API server isn't up on 8000, or you opened the Vite URL on a different port
than the proxy expects. Both processes must be running.
