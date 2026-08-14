# cadenza/shared — the frozen interface layer

Owned by the architect. **Dev A, B and C import from here and never edit it.**
If a contract is wrong, say so and it gets changed here once — a local patch
means three workstreams silently diverge and the integration day is lost.

Two mirrors of one contract:

| | Python | TypeScript |
|---|---|---|
| Install | `uv pip install -e ../shared` | `npm i ../shared/ts` (or a `@cadenza/shared` tsconfig path alias) |
| Import | `from cadenza_contracts import Ledger, UserPrompt` | `import type { Ledger, UserPrompt } from "@cadenza/shared"` |

| Module | Contents |
|---|---|
| `ledger.py` / `ledger.ts` | The ledger — the single source of truth |
| `patch.py` | RFC 6902 ops, `AgentPatchOutput`, and **the one apply path** (`apply_patch`) |
| `geometry.py` / `geometry.ts` | `GeometryService` protocol, `BuildResult`, `SpatialQuery` / `SpatialHit`, `GeometryError` |
| `messages.py` / `messages.ts` | Every WebSocket frame, both directions, plus the binary blob codec |
| `errors.py` | `ErrorCode` — every `error` frame carries one |
| `ids.py` | ID minting (`feat_`, `prj_`, `ses_`, `msg_` prefixes) |
| `fixtures.py` | Golden milestone-1 objects — test against these, not hand-typed JSON |
| `version.py` | `CONTRACT_VERSION`; major mismatch refuses the connection |

Rules: millimetres and degrees everywhere; right-handed **Z-up** (glTF's Y-up is
converted at the Three.js boundary and nowhere else); no topological ids are
ever persisted; `revision` is server-owned.

See `../ARCHITECTURE.md` for why, `../CONTRACTS.md` for the wire-level detail
and `../WORKSTREAMS.md` for who builds what.
