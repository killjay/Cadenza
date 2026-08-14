# Running CADenza

## The one thing that will confuse you

**The server must run from `geometry/.venv`, not `backend/.venv`.**

Geometry is an in-process call (`ARCHITECTURE.md` §2), so the process that
serves WebSockets is the same process that runs build123d — it needs both
dependency sets. `geometry/.venv` is the one that has both. `backend/.venv`
still exists and still passes the tests, but it has no build123d, so it falls
back to the stub geometry backend and produces no GLB.

`GET /health` tells you which backend answered:

```json
{ "geometry": "build123d",  "stubbed": ["wire envelope"] }          // real
{ "geometry": "stub (ModuleNotFoundError: …)", "stubbed": ["geometry build", …] }
```

If the viewport stays empty and the transcript says *"built this without a
geometry kernel"*, you are running the wrong interpreter. That degradation is
deliberate — a venv without OCCT can still run the transport, the agents and the
tests — but it is not what you want in front of a user.

## Start it

```bash
cd cadenza

# API keys. Vision needs Anthropic or OpenRouter; DeepSeek's chat endpoint is
# text-only and image input is auto-rerouted off it.
$EDITOR .env            # ANTHROPIC_API_KEY=sk-ant-…

# server
geometry/.venv/bin/python -m uvicorn cadenza_backend.app:app --reload --port 8000

# web (separate shell) — vite proxies /ws to :8000
cd web && npm run dev
```

Then open the printed URL, and either type a part description or drop a drawing
into the composer in the right-hand pane.

## Tests

```bash
cd backend  && ../geometry/.venv/bin/python -m pytest -q   # 64
cd shared   && ../geometry/.venv/bin/python -m pytest -q   # 20
cd geometry && .venv/bin/python -m pytest -q               # 56
cd web      && npm run typecheck && npm run build
```

No test makes a model call — the provider is faked throughout, so the suite is
free and offline. That also means **the suite cannot tell you whether the
Draftsman actually reads drawings correctly**; only a live run can, and there is
no substitute for looking at the part it produced.

## Rebuilding the combined venv

If `geometry/.venv` is ever recreated, it needs the backend's dependencies too:

```bash
cd cadenza
uv pip install --python geometry/.venv/bin/python \
  "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic-settings>=2.4" \
  "httpx>=0.27" "anthropic>=0.40" "websockets>=13.0" "jsonpatch>=1.33" \
  "pytest>=8.0" "pytest-asyncio>=0.24"
uv pip install --python geometry/.venv/bin/python -e ./geometry -e ./backend
```

## Image input, in one paragraph

Attach a sketch with the paperclip, a drag-and-drop onto the right pane, or ⌘V
of a screenshot. The browser downscales to ≤1568 px, flattens onto white,
re-encodes (PNG for line art, JPEG for photographs) and base64s it into the
`user.prompt` frame. Any image routes the turn to the Draftsman. Dimensioned
drawings work far better than photographs — see `ARCHITECTURE.md` §7 for what is
in and out of scope, and why a photograph of a real object is refused rather
than approximated.
