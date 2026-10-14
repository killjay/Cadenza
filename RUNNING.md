# Running CADenza

## The one thing that will confuse you

**The server must run from the top-level `.venv`.**

Runtime geometry builds and probes run in bounded child processes using the
server's Python interpreter. The top-level `.venv` must therefore contain all
dependencies across the workspace (API backend, geometric kernel, and shared
contracts). `GEOMETRY_MAX_WORKERS` defaults to 2 concurrent jobs and
`GEOMETRY_STARTUP_TIMEOUT_S` defaults to 60 seconds for a cold worker to load its
kernel and report readiness. `GEOMETRY_TIMEOUT_S` defaults to 20 seconds for each
job after readiness. A cold request can therefore use up to 80 seconds for these
two phases; upstream request handling and cleanup are additional. Warm workers
skip startup. One-shot benchmark/test processes retain a single overall deadline. Saturated
capacity rejects new jobs as busy; timed-out jobs are terminated and cleaned up.
Accepted designs remain unchanged when a job fails. Healthy workers retain their
kernel and four-entry geometry cache across jobs. Workers start lazily and recycle
after `GEOMETRY_WORKER_MAX_JOBS` jobs (default 100); shutdown reaps the pool.
Run `python backend/scripts/benchmark_geometry.py --repeats 3` for pooled timings,
or add `--fresh` for one-process-per-job comparison.

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

## Offline 3D workflow evaluation

From the repository root, with the combined environment and real build123d/OCCT
dependencies installed:

```powershell
.venv/Scripts/python.exe -m cadenza_backend.design_evaluation --split all --repeat 1 --output project_details/evaluation-run
.venv/Scripts/python.exe -m pytest backend/tests/test_design_evaluation.py -q
```

Choose a **new output directory** for every run. `--case AS03` selects a case and
its dimensional variants; `--split development` or `--split holdout` selects the
frozen split. `--repeat` accepts 1–3. Exit 0 means all selected automated checks
passed; nonzero means a mismatch, invalid configuration, interruption or reporting
failure. Required native geometry is never skipped. Run CAD evaluation and other
native CAD suites sequentially; cold STEP inspection processes can take time.

The runner uses synthetic sessions and one fixture response per turn through the
real part/source/assembly handlers. It never loads user projects or initializes a
provider client in offline mode. Authored Apply actions affect only those
synthetic sessions. Reports include `results.json`, `results.md`,
`human_review.md` and candidate STEP files. JSON records source/corpus/image hashes,
installed versions, geometry and state checks. Handler time includes worker work;
`build_ms` has the existing worker build scope. Neither measures provider latency.

**Offline passes do not establish AI interpretation or image accuracy.** Human
review remains pending; provider usage and cost are unknown, not estimated. These
checks cover the authored 3D vocabulary, not arbitrary engineering designs.

## Guarded 3D live evaluation

F2a supplies the machinery; its tests use mocked networking. A real run needs
separate approval of the provider/model, synthetic inputs, attempt ceiling and
spending cap, after checking current provider enforcement, pricing and image
support. No real run is authorized by the commands above.

The explicit options are `--live --provider openrouter --model <model-id>
--max-calls <ceiling> --cap-usd <ceiling> --output <new-directory>`.
The development pilot is `backend/tests/fixtures/design_evaluation/live_pilot.json`:
12 tasks, 16 initial turns and two optional authored follow-ups (18 maximum).
`--case NP03`, for example, selects one pilot task. Live holdout runs and repeats
are rejected. The default remains offline. Configure the dedicated evaluation
key locally; never include it in reports or version control.

The shared budget helper checks the official endpoint's non-resetting key limit
before every attempt, requires BYOK inclusion, and rejects route changes and
automatic extra calls. This depends on provider-side enforcement, not a local
token-cost estimate. Failed cap checks stop the run; timeouts consume their
admitted attempt. Cap-check HTTP requests are counted separately.

Live reports retain completed, failed and unattempted tasks/turns, sanitized model
responses, schema/outcome classifications, usage, latency, source/input hashes and
candidate STEP files. Missing cost stays unknown. Failed dependent sequences
stop; accepted-state safety failures stop the whole run. The same independent
candidate/native geometry and Apply/Cancel checks run as in offline mode. Human
review remains required, including clarification relevance and image meaning.
The pilot requires explicit new names; it does not normalize arbitrary naming
differences after seeing model outputs. Production modeling and UI are unchanged.

### Direct Anthropic evaluation

For an explicitly authorized shared-key trial, `--allow-shared-key` replaces all
`--workspace-*` options below. It uses the existing `ANTHROPIC_API_KEY` from normal
settings and retains call ceilings, token-count preflight, reservations and evidence.
Reports label this `shared_key_no_provider_cap`: the run allowance is an estimate,
not a provider-enforced account cap. Other consumers are not included in the local
ledger. Do not combine shared-key mode with workspace confirmation options.
Default mode still requires the dedicated-workspace confirmation described below.

The same evaluator accepts `--provider anthropic` with exactly
`claude-sonnet-5-5` or `claude-opus-5-5`. Evaluation uses medium effort and at most
8,000 output tokens, including thinking. Application model defaults are unchanged.
This support is verified using mocks; actual model/account compatibility and
current pricing must be checked before a separately authorized live run.

For the default workspace-backed mode, create a dedicated evaluation workspace/key in Anthropic Console,
set a provider-side workspace spend limit of at most $6, and confirm its current
UTC month and starting usage. Keep the key local in the normal Anthropic key
configuration. No administrator credential is needed by this evaluator. The
workspace must have no other consumers. This manual confirmation is **not** an
API readback of the account's cap, balance or key/workspace membership.

Alongside the existing `--live --provider anthropic --model ... --max-calls ...
--cap-usd ... --output ...` options, supply all of the following in workspace-backed mode:

```text
--workspace-id wrkspc_YOUR_WORKSPACE
--workspace-cap-usd 6
--workspace-usage-usd CURRENT_MONTH_SPEND
--workspace-month YYYY-MM
--workspace-confirmed
```

These are placeholders, not an approved live command. Missing, invalid, expired
or inconsistent confirmation blocks admission. The local run cap must fit the
confirmed remaining workspace funds. Study target is $5, maximum $6 total;
proposed allocations are $3 Sonnet, $2 Opus comparisons, $1 unallocated reserve.
Track cumulative runs in the local test log; a new process has a new local
reservation ledger. Monthly limits reset, so confirmation must be renewed.

Before each model attempt the final request is sent to Anthropic's token-count
endpoint (separate from generation). This endpoint sends synthetic data to the
provider even though counting itself is currently free; preparation tests mock it.
Reserve the input estimate plus a 10%/512-token cushion at five-minute cache-write
pricing, and the full output allowance. Refuse requests exceeding the run cap.
Reservations are retained after success, timeout or unknown usage. Provider errors
stop this sequential run; no automatic retry or model fallback. The provider's
workspace cap is the spending backstop only in workspace-backed mode; shared-key
mode has no confirmed provider backstop. Estimates alone are not a billing guarantee.

Reports distinguish `reserved_usd`, usage-derived `estimated_cost_usd`, and
`provider_cost` (unknown when not supplied). The budget includes count-request
totals, price date/rates, and `user_confirmed_not_provider_verified` metadata.
Failed counting/model requests retain their safe error type and available HTTP
status; private provider error messages are excluded.
`provider_ms` includes adapter preflight work; per-attempt duration excludes token
counting. Each turn links bounded `context.json`, sanitized `request.json` and
`response.json`, plus `extra-request.json` if an additional call was blocked.
Image bytes and hidden reasoning are omitted; image hashes link to frozen fixtures.
An evidence-file write failure stops admission. Output folders must be new.

Live proposals also retain `candidate.json` and native `candidate.step` before
candidate-oracle checks, so a rejected candidate can be reviewed. Anthropic turns
retain `accepted.json` after the outcome, including failures. Compare starting
context, proposal and accepted state; a proposal image is not proof of Apply.
The Anthropic adapter wraps top-level result unions in an object tool envelope
and unwraps it before existing application validation, as required by the provider.

Offline verification (no account calls):

```powershell
.venv/Scripts/python.exe -m pytest backend/tests/test_evaluation_budget.py backend/tests/test_model_client_usage.py backend/tests/test_design_evaluation.py backend/tests/test_sketch_evaluation.py backend/tests/test_diagnostics.py backend/tests/test_diagnostics_api.py -q
```

## Capturing a live modeling problem

Open **Diagnostics** in the bottom status bar and check that it says **Recording
locally**. Then create, edit, preview, apply and save normally. If something goes
wrong, choose the relevant retained session and **Download diagnostics**. The ZIP
contains correlated requests, visible model/tool responses, geometry outcomes,
preview decisions and accepted snapshots. **Include drawings** adds captured
image bytes; it is unchecked by default. Reports contain proprietary design and
prompt content, so review them before sharing.

The panel works independently of modeling busy state. HTTP downloads can work
with a closed WebSocket. If the backend is unavailable, **Download browser-only**
saves limited metadata; it cannot recover server records and resets on reload.
Saved projects still contain design/history, not chat or diagnostics.

New records live under `backend/logs/diagnostics/v1/`; old `session_*.jsonl` logs
are preserved. Defaults: 14 days, 128 MiB per session, 1 GiB total, 64 KiB per
event and 8 MiB per snapshot. Retention runs on writes and preserves active
requests. Missing/oversized records and capture failures are reported; these logs
are not a crash-proof audit journal. Local loopback access is required.

Optional environment settings: `DIAGNOSTICS_ENABLED`,
`DIAGNOSTICS_CAPTURE_IMAGES`, `DIAGNOSTICS_PATH`, `DIAGNOSTICS_SESSION_BYTES`,
`DIAGNOSTICS_TOTAL_BYTES`, `DIAGNOSTICS_RETENTION_DAYS`. Disabling drawing capture
cannot remove drawings already captured. Diagnostics makes no AI calls and does
not impose a spending cap on ordinary UI prompts.

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
