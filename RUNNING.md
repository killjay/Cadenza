# Running CADenza

## The one thing that will confuse you

**The server must run from the top-level `.venv`.**

Geometry is an in-process call (`ARCHITECTURE.md` §2), so the process that
serves WebSockets is the same process that runs build123d — it needs both
dependency sets. The top-level `.venv` is configured to hold all dependencies
across the workspace (API backend, geometric kernel, and shared contracts).

`GET /health` tells you which backend answered:

```json
{ "geometry": "build123d",  "stubbed": ["wire envelope"] }          // real
{ "geometry": "stub (ModuleNotFoundError: …)", "stubbed": ["geometry build", …] }
```

If the viewport stays empty and the transcript says *"built this without a
geometry kernel"*, you are running an interpreter without build123d installed.

## Start it

```bash
cd cadenza

# API keys. Vision needs Anthropic or OpenRouter; DeepSeek's chat endpoint is
# text-only and image input is auto-rerouted off it.
$EDITOR .env            # ANTHROPIC_API_KEY=sk-ant-…

# server
.venv\Scripts\python -m uvicorn cadenza_backend.app:app --reload --port 8000

# web (separate shell) — vite proxies /ws to :8000
cd web && npm run dev
```

Then open the printed URL, and either type a part description or drop a drawing
into the composer in the right-hand pane.

## Tests

```bash
cd backend  && ..\.venv\Scripts\python -m pytest -q   # 64
cd shared   && ..\.venv\Scripts\python -m pytest -q   # 20
cd geometry && ..\.venv\Scripts\python -m pytest -q   # 56
cd web      && npm run typecheck && npm run build
```

No test makes a model call — the provider is faked throughout, so the suite is
free and offline. That also means **the suite cannot tell you whether the
Draftsman actually reads drawings correctly**; only a live run can, and there is
no substitute for looking at the part it produced.

## Rebuilding the combined venv

If the root `.venv` is ever recreated, you can reinstall all dependencies via:

```bash
cd cadenza
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Image input, in one paragraph

Attach a sketch with the paperclip, a drag-and-drop onto the right pane, or ⌘V
of a screenshot. The browser downscales to ≤1568 px, flattens onto white,
re-encodes (PNG for line art, JPEG for photographs) and base64s it into the
`user.prompt` frame. Any image routes the turn to the Draftsman. Dimensioned
drawings work far better than photographs — see `ARCHITECTURE.md` §7 for what is
in and out of scope, and why a photograph of a real object is refused rather
than approximated.
