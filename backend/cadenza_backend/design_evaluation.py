"""Offline-first 3D evaluation; explicit live mode requires verified route and limits."""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
from importlib.metadata import version
import json
import math
import platform
import subprocess
import sys
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cadenza_backend import assembly_prompts, design_prompts, part_commands
from cadenza_backend.agents import assembly_intents, design_intents
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.evaluation_observer import Observer
from cadenza_backend.evaluation_budget import AnthropicBudget, Budget, EvaluationLimit
from cadenza_backend.diagnostics import clean
from cadenza_backend.geometry_jobs import runner
from cadenza_backend.geometry_jobs import inspect as inspect_geometry
from cadenza_backend.session import LedgerStore, Session, SessionLogger
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import AssemblyPrompt, DesignPrompt, PartPrompt, PreviewDecision

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "backend/tests/fixtures/design_evaluation"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Measure(Strict):
    volume: float
    minimum: list[float] = Field(min_length=3, max_length=3)
    maximum: list[float] = Field(min_length=3, max_length=3)


class Geometry(Measure):
    solids: int = Field(ge=1)
    per_solid: list[Measure] = Field(default_factory=list)
    bores: list[dict[str, float]] = Field(default_factory=list)


class Step(Strict):
    route: Literal["part", "source", "assembly"]
    request: dict[str, Any]
    outcome: Literal["proposal", "clarification", "unsupported", "rejection"]
    expected: dict[str, Any] | None = None
    geometry: Geometry | None = None
    decision: Literal["apply", "cancel"] = "cancel"
    image: str | None = None
    rejection_code: str | None = None
    rejection_contains: str | None = None
    patterns: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete_expectation(self):
        if self.outcome == "proposal" and (self.expected is None or self.geometry is None):
            raise ValueError("A proposal requires a complete state and geometry oracle")
        if self.outcome != "proposal" and (self.expected is not None or self.geometry is not None or self.decision != "cancel"):
            raise ValueError("Nonproposals cannot have candidates or Apply decisions")
        if self.outcome == "rejection" and not self.rejection_code:
            raise ValueError("Rejections require an exact error code")
        if self.image and (Path(self.image).name != self.image or not self.image.endswith(".png")):
            raise ValueError("Images must be local PNG fixture names")
        return self


class Case(Strict):
    id: str = Field(pattern=r"^[A-Z]+[0-9]{2}$")
    variant: str = Field(pattern=r"^[a-z0-9_]+$")
    split: Literal["development", "holdout"]
    category: Literal["task", "adversarial"] = "task"
    context: str
    steps: list[Step] = Field(min_length=1, max_length=8)


class Corpus(Strict):
    version: Literal[1]
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        keys = [(c.id, c.variant) for c in self.cases]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate case/variant")
        return self


class QuietLogger(SessionLogger):
    def __init__(self): pass
    def log(self, event_type: str, **kwargs): pass


class EvaluationLogger(QuietLogger):
    """Bounded synthetic request evidence; normal application logging is untouched."""
    def __init__(self):
        self.directory: Path | None = None

    def log(self, event_type: str, **kwargs):
        if self.directory is None: return
        if event_type == "evaluation.request":
            name = "extra-request.json" if (self.directory / "request.json").exists() else "request.json"
            self.write(name, kwargs)
        elif event_type == "llm_response":
            self.write("response.json", kwargs)

    def write(self, name: str, payload: dict):
        assert self.directory is not None
        try:
            sanitized = clean(payload)
            try:
                data = json.dumps(sanitized, allow_nan=False, indent=2).encode("utf-8")
            except ValueError:
                data = b'{"omitted": "Non-finite JSON in evaluation evidence"}'
            if len(data) > 2 * 1024 * 1024:
                raise EvaluationLimit("Evaluation evidence exceeds the 2 MiB bound")
            (self.directory / name).write_bytes(data)
        except (OSError, ValueError, TypeError) as exc:
            raise EvaluationLimit("Cannot retain evaluation evidence") from exc


class FixtureClient(ModelClient):
    """One authored response, without SDK construction or environment credentials."""
    def __init__(self, response: dict, route: str, images: list):
        self.response = copy.deepcopy(response)
        self.route, self.images = route, images
        self.calls = 0
        self.schema_valid = False

    async def complete_json(self, *args, **kwargs) -> dict:
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("Unexpected extra fixture call")
        if kwargs.get("images") != self.images:
            raise AssertionError("Image bytes did not reach interpreter")
        adapter = assembly_intents.INTENT if self.route == "assembly" else design_intents.INTENT
        adapter.validate_python(self.response)
        self.schema_valid = True
        return copy.deepcopy(self.response)


class CheckedLiveClient(ModelClient):
    """One explicitly admitted adapter call; only normal handler inputs reach it."""
    def __init__(self, delegate: ModelClient, budget: Budget | AnthropicBudget, route: str):
        self.delegate, self.budget, self.route = delegate, budget, route
        self.calls = 0
        self.schema_valid = False
        self.response: dict | None = None
        self.duration_ms = 0.0
        self.observed: str | None = None
        self.rejection_code: str | None = None

    async def complete_json(self, *args, **kwargs) -> dict:
        self.calls += 1
        if self.calls != 1:
            raise EvaluationLimit("Only one model request is permitted per authored turn")
        await self.budget.refresh(self.delegate)
        if isinstance(self.budget, AnthropicBudget):
            kwargs["effort"] = "medium"
        started = monotonic()
        try:
            raw = await self.delegate.complete_json(*args, **kwargs)
            self.response = clean(raw)
            try:
                json.dumps(self.response, allow_nan=False)
            except (ValueError, TypeError):
                self.response = {"omitted": "Model response is not finite JSON"}
            adapter = assembly_intents.INTENT if self.route == "assembly" else design_intents.INTENT
            adapter.validate_python(raw)
            self.schema_valid = True
            return raw
        finally:
            self.duration_ms = (monotonic() - started) * 1000


def equal(left: Any, right: Any) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isfinite(left) and math.isfinite(right) and math.isclose(left, right, rel_tol=1e-8, abs_tol=1e-5)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def documents(ledgers: dict[str, Ledger]) -> dict:
    return {key: value.model_dump(mode="json") for key, value in ledgers.items()}


def differences(actual: Any, expected: Any, path="") -> list[str]:
    if equal(actual, expected): return []
    if isinstance(actual, dict) and isinstance(expected, dict):
        return [item for key in sorted(actual.keys() | expected.keys())
            for item in differences(actual.get(key), expected.get(key), f"{path}/{key}")][:20]
    if isinstance(actual, list) and isinstance(expected, list) and len(actual) == len(expected):
        return [item for i, (a, e) in enumerate(zip(actual, expected)) for item in differences(a, e, f"{path}/{i}")][:20]
    return [f"{path}: expected {expected!r}, got {actual!r}"[:500]]


def state(session: Session) -> dict:
    return dict(ledgers=documents(session.snapshot()), primary=session.primary_ledger_id,
        history=[documents(s) for s in session.history], redo=[documents(s) for s in session.redo],
        artifacts=copy.deepcopy(session.artifacts._items), geometry=copy.deepcopy(session.last_geometry))


IDENTITY_FIELDS = {"id", "project_id", "sub_assembly_id", "part_id", "target_feature", "component_a", "component_b", "relative_to",
    "ledger_id", "feature_id", "component_id", "source_ledger_id", "joint_id", "owner_path", "detach_joint_ids"}


def replace_ids(value: Any, mapping: dict[str, str], key="") -> Any:
    if isinstance(value, dict): return {k: replace_ids(v, mapping, k) for k, v in value.items()}
    if isinstance(value, list): return [replace_ids(v, mapping, key) for v in value]
    return mapping.get(value, value) if key in IDENTITY_FIELDS and isinstance(value, str) else value


def map_documents(value: dict, mapping: dict[str, str]) -> dict:
    return {mapping.get(k, k): replace_ids(v, mapping) for k, v in value.items()}


def canonical(candidate: dict, before: dict, expected: dict, bindings: dict | None = None) -> dict:
    """Map only newly minted identity fields, by unique authored name/endpoints.

    Existing IDs, list order, metadata and all other values are compared unchanged.
    Aliases are ordinary schema-valid IDs in authored full expected documents.
    """
    mapping: dict[str, str] = {}
    remaining = set(expected) - set(before)
    for lid in set(candidate) - set(before):
        matches = [key for key in remaining if expected[key]["metadata"]["name"] == candidate[lid]["metadata"]["name"]]
        require(len(matches) == 1, "New source identity is missing or ambiguous")
        mapping[lid] = matches[0]
        remaining.remove(matches[0])
    require(not remaining, "Expected source was not created")
    for lid, ledger in candidate.items():
        target = expected.get(mapping.get(lid, lid))
        require(target is not None, "Unexpected source")
        for section in ("features", "components", "joints"):
            def items(doc):
                return doc.get("features", []) if section == "features" else (doc.get("assembly") or {}).get(section, [])
            previous_ids = {item["id"] for item in items(before.get(lid, {}))}
            new_expected = [item for item in items(target) if item["id"] not in previous_ids]
            for item in items(ledger):
                if item["id"] in previous_ids:
                    continue
                fields = ("component_a", "component_b") if section == "joints" else ("name",)
                matches = [other for other in new_expected if all(other.get(k) == mapping.get(item.get(k), item.get(k)) for k in fields)]
                require(len(matches) == 1, f"New {section} identity is missing or ambiguous")
                mapping[item["id"]] = matches[0]["id"]
                new_expected.remove(matches[0])
    if bindings is not None:
        bindings.update(mapping)
    return map_documents(candidate, mapping)


def check_geometry(measured: dict, expected: Geometry) -> None:
    require(equal(measured["volume"], expected.volume), "Volume mismatch")
    require(equal(measured["minimum"], expected.minimum), "Minimum bounds mismatch")
    require(equal(measured["maximum"], expected.maximum), "Maximum bounds mismatch")
    require(measured["solids"] == expected.solids, "Solid count mismatch")
    if expected.per_solid:
        remaining = copy.deepcopy(measured["per_solid"])
        for solid in expected.per_solid:
            matches = [i for i, actual in enumerate(remaining) if equal(actual, solid.model_dump())]
            require(len(matches) == 1, "Per-solid placement/volume missing or ambiguous")
            remaining.pop(matches[0])
        require(not remaining, "Unexpected exported solid")
    remaining_bores = copy.deepcopy(measured.get("bores", []))
    for bore in expected.bores:
        matches = [i for i, actual in enumerate(remaining_bores) if equal(actual, bore)]
        require(len(matches) == 1, "Exported bore position/radius/depth missing or ambiguous")
        remaining_bores.pop(matches[0])
    require(not remaining_bores, "Unexpected exported bore")


def inspect_step(path: Path) -> dict:
    result = subprocess.run([sys.executable, str(ROOT / "backend/scripts/inspect_evaluation_step.py"), str(path)],
        capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)


async def ignore(_): pass


async def _run_step(session: Session, step: Step, response: dict, output: Path, fixtures: Path, bindings: dict | None = None,
                   *, live_client: CheckedLiveClient | None = None, allow_clarification=False, observer: Observer | None = None) -> dict:
    bindings = bindings if bindings is not None else {}
    before = state(session)
    aliases = {alias: actual for actual, alias in bindings.items()}
    raw = dict(replace_ids(step.request, aliases), message_id="evaluation", base_revision=session.revision)
    if observer:
        await observer.begin(session, step, raw, str(output.parent.name + "/" + output.name))
    images = []
    if step.image:
        data = (fixtures / "images" / step.image).read_bytes()
        images = [("image/png", data)]
        raw["images"] = [dict(media_type="image/png", data=base64.b64encode(data).decode())]
    client = live_client or FixtureClient(replace_ids(response, aliases), step.route, images)
    events = []
    async def emit(event): events.append(event)
    started = monotonic()
    error = None
    async with session.lock:
        try:
            if step.route == "part":
                await part_commands.prompt_part(session, PartPrompt.model_validate(raw), emit, client=client)
            elif step.route == "source":
                await design_prompts.handle_design_prompt(session, DesignPrompt.model_validate(raw), emit, client=client)
            else:
                await assembly_prompts.handle_assembly_prompt(session, AssemblyPrompt.model_validate(raw), emit, client=client)
        except CadenzaError as exc:
            error = exc
        except BaseException:
            require(state(session) == before, "Accepted state/history/artifacts changed before Apply")
            raise
        duration = (monotonic() - started) * 1000
        require(state(session) == before, "Accepted state/history/artifacts changed before Apply")
        require(client.calls == 1 and client.schema_valid, "Interpreter/schema failure, not a correct rejection")
        messages = [e for e in events if e.type == "agent.message"]
        observed = ("rejection" if error else "proposal" if session.preview else
            "clarification" if messages and messages[-1].needs_clarification else "unsupported" if messages else "missing")
        if observer:
            preview_view = session.preview
            display = (preview_view.display_outcome or preview_view.outcome) if preview_view else None
            display_target = preview_view.message.display_ledger_id if preview_view else None
            await observer.publish(observed, glb=display.glb if display else None,
                detail=error.message if error else messages[-1].text if messages else "",
                target=display_target or (observer.current.target if observer.current else raw.get("ledger_id", "")))
        if live_client:
            live_client.observed = observed
            live_client.rejection_code = error.code.value if error else None
        require(observed == step.outcome or (allow_clarification and observed == "clarification"),
            f"Expected {step.outcome}, observed {observed}" + (f": {error.code.value}: {error.message}" if error else ""))
        result: dict[str, Any] = dict(outcome=observed, fixture_calls=0 if live_client else client.calls, network_attempts=live_client.budget.turn_calls if live_client else 0,
            handler_ms=duration, build_ms=None, state_preserved=True,
            human_review="pending", clarification=messages[-1].text if messages else None)
        if error:
            require(error.code.value == step.rejection_code, f"Wrong rejection code: {error.code.value}")
            if step.rejection_contains:
                require(step.rejection_contains in error.message, "Rejection reason does not match the authored failure")
            result["rejection"] = dict(code=error.code.value, message=error.message)
        if observed != "proposal":
            require(session.preview is None, "Nonproposal created a preview")
            return result
        preview = session.preview
        assert preview is not None and step.expected is not None and step.geometry is not None
        outcome = preview.display_outcome or preview.outcome
        require(preview.outcome.ok and not preview.outcome.stub and outcome.ok and not outcome.stub and bool(outcome.step), "Missing real geometry")
        candidate = documents(preview.ledgers)
        if live_client:
            evidence = EvaluationLogger(); evidence.directory = output
            evidence.write("candidate.json", dict(primary=session.primary_ledger_id, ledgers=candidate,
                requested_decision=step.decision))
            (output / "candidate.step").write_bytes(outcome.step or b"")
        created: dict[str, str] = {}
        actual = canonical(map_documents(candidate, bindings), map_documents(before["ledgers"], bindings), step.expected, created)
        require(equal(actual, step.expected), "Complete candidate mismatch: " + "; ".join(differences(actual, step.expected)))
        require(equal(outcome.volume_mm3, step.geometry.volume), "Worker volume mismatch")
        require(equal(outcome.bbox, {"min": step.geometry.minimum, "max": step.geometry.maximum}), "Worker bounds mismatch")
        for alias, count in step.patterns.items():
            matches = [(lid, f.id) for lid, ledger in preview.ledgers.items() for f in ledger.features
                if created.get(f.id, bindings.get(f.id, f.id)) == alias]
            require(len(matches) == 1, "Pattern alias is missing or ambiguous")
            lid, fid = matches[0]
            effects = await inspect_geometry(preview.ledgers[lid], {"query_type": "pattern_effects", "feature_ids": [fid]}, preview.ledgers)
            require(not effects.get("error") and len(effects.get("patterns", [])) == 1, "Pattern inspection failed")
            effect = effects["patterns"][0]
            require(effect["requested_total"] == count and effect["measured_complete_bores"] == count, "Pattern bore count mismatch")
            result["pattern_measurement"] = effects
        step_path = output / "candidate.step"
        step_path.write_bytes(outcome.step or b"")
        measured = await asyncio.to_thread(inspect_step, step_path)
        check_geometry(measured, step.geometry)
        result.update(build_ms=preview.outcome.build_ms, measurement=measured, artifact=str(step_path))
        decision = PreviewDecision(message_id="evaluation-decision", base_revision=session.revision, preview_id=preview.id, action=step.decision)
        await decide_preview(session, decision, ignore)
        if step.decision == "apply":
            require(documents(session.snapshot()) == candidate, "Apply did not accept exact candidate")
            require(len(session.history) == len(before["history"]) + 1 and session.revision == decision.base_revision + 1, "Apply must advance once")
            require(not (set(created.values()) & set(bindings.values())), "Created aliases collide with accepted identities")
            bindings.update(created)
        else:
            require(state(session) == before, "Cancel changed accepted state")
        if observer and observer.current:
            await observer.publish("decision", accepted=observer.current.proposal if step.decision == "apply" else observer.current.before,
                detail=f"Recorded action: {step.decision}")
        committed = state(session)
        try:
            await decide_preview(session, decision, ignore)
        except CadenzaError:
            pass
        else:
            raise AssertionError("Duplicate decision was accepted")
        require(state(session) == committed, "Duplicate decision changed state")
        return result


async def run_step(session: Session, step: Step, response: dict, output: Path, fixtures: Path, bindings: dict | None = None,
                   *, live_client: CheckedLiveClient | None = None, allow_clarification=False, observer: Observer | None = None) -> dict:
    try:
        result = await _run_step(session, step, response, output, fixtures, bindings,
            live_client=live_client, allow_clarification=allow_clarification, observer=observer)
        if observer: await observer.publish("passed", detail="Automated checks passed; human review pending")
        return result
    except BaseException as exc:
        if observer:
            detail = str(clean(str(exc)))[:4000] if isinstance(exc, AssertionError) else type(exc).__name__
            await observer.publish("failed" if isinstance(exc, Exception) else "interrupted", detail=detail)
        raise


def load(fixtures: Path = FIXTURES):
    corpus = Corpus.model_validate_json((fixtures / "corpus.json").read_text(encoding="utf-8"))
    contexts = json.loads((fixtures / "contexts.json").read_text(encoding="utf-8"))
    responses = json.loads((fixtures / "responses.json").read_text(encoding="utf-8"))
    require(set(responses) == {f"{c.id}/{c.variant}" for c in corpus.cases}, "Response bindings differ from corpus")
    for case in corpus.cases:
        require(case.context in contexts, "Unknown context")
        require(len(responses[f"{case.id}/{case.variant}"]) == len(case.steps), "Response/turn count mismatch")
        for step in case.steps:
            if step.expected:
                for lid, raw in step.expected.items():
                    require(Ledger.model_validate(raw).project_id == lid, "Expected ledger identity mismatch")
    return corpus, contexts, responses


class PilotTurn(Strict):
    oracle_step: int = Field(ge=0, le=7)
    request: dict[str, Any]
    followup: str | None = Field(default=None, min_length=1, max_length=8000)


class PilotTask(Strict):
    case_id: str
    turns: list[PilotTurn] = Field(min_length=1, max_length=8)


class Pilot(Strict):
    version: Literal[1]
    naming_policy: Literal["explicit_names_exact"]
    tasks: list[PilotTask] = Field(min_length=1, max_length=12)


def load_pilot(fixtures: Path = FIXTURES, case_id: str | None = None):
    """Load input/oracle references only. Never open the offline responses file."""
    pilot = Pilot.model_validate_json((fixtures / "live_pilot.json").read_text(encoding="utf-8"))
    corpus = Corpus.model_validate_json((fixtures / "corpus.json").read_text(encoding="utf-8"))
    contexts = json.loads((fixtures / "contexts.json").read_text(encoding="utf-8"))
    cases = {c.id: c for c in corpus.cases if c.variant == "base" and c.split == "development" and c.category == "task"}
    require(len({t.case_id for t in pilot.tasks}) == len(pilot.tasks), "Duplicate pilot task")
    selected = []
    for task in pilot.tasks:
        require(task.case_id in cases, "Pilot cannot include holdouts or adversarial fixture cases")
        case = cases[task.case_id]
        require(case.context in contexts, "Unknown pilot context")
        context = contexts[case.context]
        require(context["primary"] in context["ledgers"], "Missing primary ledger")
        for lid, raw in context["ledgers"].items():
            require(Ledger.model_validate(raw).project_id == lid, "Context ledger identity mismatch")
        require([t.oracle_step for t in task.turns] == list(range(len(case.steps))), "Pilot must preserve its complete dependent sequence")
        for turn in task.turns:
            step = case.steps[turn.oracle_step]
            require(step.outcome != "rejection", "Injected fixture errors are not live tasks")
            model = {"part": PartPrompt, "source": DesignPrompt, "assembly": AssemblyPrompt}[step.route]
            require("images" not in turn.request and "message_id" not in turn.request and "base_revision" not in turn.request,
                "Evaluation owns request identity, revision and image bytes")
            model.model_validate(dict(turn.request, message_id="pilot-validation", base_revision=0))
            if turn.followup:
                require(step.outcome == "proposal", "Follow-ups require a proposal oracle")
            if step.image:
                require((fixtures / "images" / step.image).is_file(), "Missing pilot image")
        if case_id is None or case_id == task.case_id:
            selected.append((case, task))
    require(bool(selected), "Empty or unknown pilot selection")
    return selected, contexts


def write_live_report(output: Path, report: dict) -> None:
    (output / "results.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# 3D live evaluation", "", "Human review pending; automated checks do not establish engineering fitness.", "",
        f"Admitted model attempts: {report['budget']['admitted_calls']}; cap checks: {report['budget']['verification_requests']}.", "",
        "| Task | Status | Classification |", "| --- | --- | --- |"]
    lines.extend(f"| {r['id']} | {r['status']} | {r.get('classification', '')} |" for r in report["results"])
    (output / "results.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    (output / "human_review.md").write_text("# Human review - pending\n\n" + "\n".join(
        f"- [ ] {r['id']}: intended dimensions/placement, selected target, image interpretation, clarification relevance, assumptions; notes:"
        for r in report["results"]), encoding="utf-8")


async def evaluate_live(output: Path, *, provider: str, model: str, max_calls: int, cap_usd: float,
                        fixtures: Path = FIXTURES, case_id: str | None = None,
                        workspace_id: str | None = None, workspace_cap_usd: float | None = None,
                        workspace_usage_usd: float | None = None, workspace_month: str | None = None,
                        workspace_confirmed=False, allow_shared_key=False, observe_view=False) -> dict:
    selected, contexts = load_pilot(fixtures, case_id)
    planned = sum(1 + bool(turn.followup) for _, task in selected for turn in task.turns)
    budget: Budget | AnthropicBudget
    if provider == "anthropic":
        budget = AnthropicBudget(model, max_calls, cap_usd, workspace_id=workspace_id, workspace_cap_usd=workspace_cap_usd,
            workspace_usage_usd=workspace_usage_usd, workspace_month=workspace_month,
            workspace_confirmed=workspace_confirmed, allow_shared_key=allow_shared_key)
    else:
        require(not allow_shared_key and not workspace_confirmed and all(v is None for v in (workspace_id, workspace_cap_usd, workspace_usage_usd, workspace_month)),
            "Workspace/shared-key options apply only to Anthropic")
        budget = Budget(provider, model, max_calls, cap_usd)
    require(max_calls >= planned, "Attempt ceiling is smaller than the selected manifest's maximum")
    from cadenza_backend.geometry_bridge import REAL_GEOMETRY
    require(REAL_GEOMETRY, "Live evaluation requires real geometry")
    observer = Observer(output) if observe_view else None
    output.mkdir(parents=True, exist_ok=False)
    attempts: list[dict] = []
    report: dict[str, Any] = dict(mode="live", human_review="pending", provider=provider, model=model,
        planned_max_attempts=planned, provider_cost=None, attempts=attempts,
        budget=dict(cap_usd=cap_usd, max_calls=max_calls, admitted_calls=0, verification_requests=0), results=[],
        python=platform.python_version(), versions={name: version(name) for name in ("build123d", "pydantic", "httpx")},
        hashes={str(p.relative_to(fixtures)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [fixtures / "live_pilot.json", fixtures / "corpus.json", fixtures / "contexts.json", * (fixtures / "images").glob("*.png")]
            },
        source_hashes={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*(ROOT / "backend/cadenza_backend").rglob("*.py"),
                *(ROOT / "shared/cadenza_contracts").rglob("*.py"),
                *(ROOT / "geometry/cadenza_geometry").rglob("*.py"), ROOT / "backend/scripts/inspect_evaluation_step.py"]})
    for case, task in selected:
        report["results"].append(dict(id=case.id, status="unattempted", steps=[],
            unattempted_turns=list(range(len(task.turns))),
            intended=[turn.model_dump() for turn in task.turns]))
    stopped = False
    logger = EvaluationLogger() if isinstance(budget, AnthropicBudget) else QuietLogger()
    def observe(attempt):
        attempts.append(attempt)
        if isinstance(budget, AnthropicBudget): budget.observe(attempt)
    async def prepare(client, payload):
        logger.log("evaluation.request", provider=provider, request=payload)
        assert isinstance(budget, AnthropicBudget)
        await budget.prepare(client, payload)
    try:
        options = {"before_anthropic_request": prepare} if isinstance(budget, AnthropicBudget) else {}
        delegate = ModelClient(logger=logger, observe=observe, before_request=budget.admit, **options)
        delegate.s = delegate.s.model_copy(update={"model_machinist": f"{provider}:{model}"})
        if isinstance(budget, AnthropicBudget):
            report.update(effort="medium", output_ceiling=min(delegate.s.agent_max_tokens, 8000))
            report["budget"].update(budget.summary())
        for (case, task), row in zip(selected, report["results"]):
            if stopped: break
            context = contexts[case.context]
            session = Session(primary_ledger_id=context["primary"], stores={lid: LedgerStore(Ledger.model_validate(raw)) for lid, raw in context["ledgers"].items()})
            session.logger = QuietLogger()
            bindings: dict[str, str] = {}
            row["status"] = "running"
            for index, turn in enumerate(task.turns):
                step = case.steps[turn.oracle_step].model_copy(update={"request": turn.request})
                for followup in range(2 if turn.followup else 1):
                    if followup:
                        step = step.model_copy(update={"request": {**turn.request, "prompt": turn.followup}})
                    client = CheckedLiveClient(delegate, budget, step.route)
                    before = state(session)
                    attempt_start = len(attempts)
                    directory = output / case.id / f"{index}-{followup}"
                    directory.mkdir(parents=True)
                    if isinstance(logger, EvaluationLogger): logger.directory = directory
                    entry: dict[str, Any] = dict(turn=index, followup=bool(followup), status="failed")
                    row["steps"].append(entry)
                    if index in row["unattempted_turns"]: row["unattempted_turns"].remove(index)
                    try:
                        if isinstance(logger, EvaluationLogger):
                            logger.write("context.json", dict(primary=before["primary"], ledgers=before["ledgers"], bindings=bindings))
                        result = await run_step(session, step, {}, directory, fixtures, bindings,
                            live_client=client, allow_clarification=bool(turn.followup) and followup == 0, observer=observer)
                        entry.update(result, status="passed")
                        if result["outcome"] == "clarification" and step.outcome == "proposal":
                            entry["status"] = "clarification_followup"
                            continue
                        break
                    except BaseException as exc:
                        unsafe = state(session) != before
                        if unsafe: classification = "accepted_state_safety_failure"
                        elif not isinstance(exc, Exception): classification = "interrupted"
                        elif isinstance(exc, EvaluationLimit) or (isinstance(budget, AnthropicBudget) and budget.stop_reason): classification = "budget_stop"
                        elif client.response is not None and not client.schema_valid: classification = "invalid_schema"
                        elif client.calls == 0: classification = "admission_rejection"
                        elif not client.schema_valid:
                            classification = "malformed_response" if attempts[attempt_start:] and attempts[-1]["outcome"] == "response" else "provider_error"
                        elif client.observed == "rejection": classification = "safe_application_rejection"
                        elif client.observed != step.outcome: classification = "wrong_intent"
                        else: classification = "candidate_or_geometry_mismatch"
                        entry.update(classification=classification, state_preserved=not unsafe, error_type=type(exc).__name__)
                        if isinstance(exc, AssertionError): entry["check_failure"] = clean(str(exc)[:4000])
                        row.update(status="failed", classification=classification)
                        stopped = unsafe or isinstance(exc, EvaluationLimit) or not isinstance(exc, Exception) or (isinstance(budget, AnthropicBudget) and bool(budget.stop_reason))
                        if not isinstance(exc, Exception): raise
                        break
                    finally:
                        entry.update(model_response=client.response, schema_valid=client.schema_valid, provider_ms=client.duration_ms,
                            observed=client.observed, rejection_code=client.rejection_code,
                            attempts=copy.deepcopy(attempts[attempt_start:]))
                        report["budget"].update(admitted_calls=budget.calls, verification_requests=budget.verification_requests)
                        if isinstance(budget, AnthropicBudget):
                            report["budget"].update(budget.summary())
                            assert isinstance(logger, EvaluationLogger)
                            accepted = state(session)
                            logger.write("accepted.json", dict(primary=accepted["primary"], ledgers=accepted["ledgers"],
                                requested_decision=step.decision, step_status=entry["status"],
                                revision=session.revision))
                            entry["evidence"] = {p.name: str(p.relative_to(output)) for p in directory.glob("*.json")}
                        costs = [(a.get("usage") or {}).get("reported_cost_usd") for a in attempts]
                        report["provider_cost"] = sum(c for c in costs if c is not None) if costs and all(c is not None for c in costs) else None
                        write_live_report(output, report)
                if row["status"] == "failed": break
            if row["status"] == "running": row["status"] = "passed"
        report["run_status"] = "stopped" if stopped else "completed"
        return report
    except BaseException as exc:
        report["run_status"] = "interrupted" if not isinstance(exc, Exception) else "failed"
        report["run_error_type"] = type(exc).__name__
        raise
    finally:
        runner.close()
        write_live_report(output, report)
        if observer:
            await observer.finish("interrupted" if report.get("run_status") == "interrupted" else
                "completed" if all(r["status"] == "passed" for r in report["results"]) else "failed", report["budget"])


def write_report(output: Path, report: dict) -> None:
    (output / "results.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# Offline 3D evaluation", "", "Live intent/image accuracy unmeasured. Human review pending.", "",
        f"Attempted {len(report['results'])}/{report['selected_executions']} executions; fixture calls are not network calls.", "",
        "| Case / variant / repeat | Result | Detail |", "| --- | --- | --- |"]
    for row in report["results"]:
        detail = row.get("error", "Automated checks passed").replace("|", "/").replace("\n", " ")
        lines.append(f"| {row['id']} / {row['variant']} / {row['repeat']} | {row['status']} | {detail} |")
    (output / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "human_review.md").write_text("# Human review — pending\n\nOffline fixtures do not measure model understanding.\n\n"
        + "\n".join(f"- [ ] {r['id']}/{r['variant']}: engineering intent, question usefulness, image ambiguity; notes:" for r in report["results"]), encoding="utf-8")


async def evaluate(output: Path, *, split="all", case_id=None, repeat=1, fixtures=FIXTURES, observe_view=False) -> dict:
    require(split in ("all", "development", "holdout") and type(repeat) is int and 1 <= repeat <= 3, "Invalid selection/repeat")
    corpus, contexts, responses = load(fixtures)
    selected = [c for c in corpus.cases if (split == "all" or c.split == split) and (case_id is None or c.id == case_id)]
    require(bool(selected), "Empty or unknown selection")
    observer = Observer(output) if observe_view else None
    output.mkdir(parents=True, exist_ok=False)
    files = [*fixtures.rglob("*.json"), *fixtures.rglob("*.png"), *fixtures.rglob("*.svg"),
        *(ROOT / "backend/cadenza_backend").rglob("*.py"), *(ROOT / "shared/cadenza_contracts").rglob("*.py"),
        *(ROOT / "geometry/cadenza_geometry").rglob("*.py"), ROOT / "backend/scripts/inspect_evaluation_step.py"]
    report: dict[str, Any] = dict(mode="offline", live_accuracy="unmeasured", provider_usage=None, provider_cost=None,
        python=platform.python_version(), selected_cases=len({c.id for c in selected}),
        versions={name: version(name) for name in ("build123d", "pydantic", "httpx")},
        selected_executions=len(selected) * repeat, results=[],
        hashes={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    try:
        for case in selected:
            for repetition in range(1, repeat + 1):
                row: dict[str, Any] = dict(id=case.id, variant=case.variant, repeat=repetition, split=case.split, category=case.category, status="failed", steps=[],
                    intended=[dict(route=s.route, request=s.request, outcome=s.outcome, decision=s.decision, image=s.image) for s in case.steps])
                report["results"].append(row)
                try:
                    context = contexts[case.context]
                    session = Session(primary_ledger_id=context["primary"], stores={lid: LedgerStore(Ledger.model_validate(raw)) for lid, raw in context["ledgers"].items()})
                    session.logger = QuietLogger()
                    bindings: dict[str, str] = {}
                    for index, step in enumerate(case.steps):
                        directory = output / f"{case.id}-{case.variant}-{repetition}" / str(index)
                        directory.mkdir(parents=True)
                        row["steps"].append(await run_step(session, step, responses[f"{case.id}/{case.variant}"][index], directory, fixtures, bindings, observer=observer))
                    row["status"] = "passed"
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                except BaseException as exc:
                    row["error"] = f"Interrupted: {type(exc).__name__}"
                    raise
                finally:
                    write_report(output, report)
        return report
    finally:
        runner.close()
        write_report(output, report)
        if observer:
            await observer.finish("completed" if len(report["results"]) == report["selected_executions"] and
                all(r["status"] == "passed" for r in report["results"]) else "failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["all", "development", "holdout"], default="all")
    parser.add_argument("--case")
    parser.add_argument("--repeat", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observe", action="store_true", help="Publish local read-only viewer checkpoints; no additional inference")
    parser.add_argument("--live", action="store_true", help="Requires explicit live-run authorization and provider-specific budget admission")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--cap-usd", type=float)
    parser.add_argument("--workspace-id")
    parser.add_argument("--workspace-cap-usd", type=float)
    parser.add_argument("--workspace-usage-usd", type=float)
    parser.add_argument("--workspace-month", help="UTC YYYY-MM of the manually confirmed cap")
    parser.add_argument("--workspace-confirmed", action="store_true", help="Attest a dedicated workspace/key and current provider-side cap; not API verification")
    parser.add_argument("--allow-shared-key", action="store_true", help="Explicit Anthropic local-estimate-only budget; no confirmed provider-side cap")
    args = parser.parse_args()
    try:
        if args.live:
            require(args.split != "holdout" and args.repeat == 1, "Live mode uses only the frozen development pilot, once")
            require(all(value is not None for value in (args.provider, args.model, args.max_calls, args.cap_usd)), "Explicit live route and limits required")
            report = asyncio.run(evaluate_live(args.output, provider=args.provider, model=args.model,
                max_calls=args.max_calls, cap_usd=args.cap_usd, case_id=args.case,
                workspace_id=args.workspace_id, workspace_cap_usd=args.workspace_cap_usd,
                workspace_usage_usd=args.workspace_usage_usd, workspace_month=args.workspace_month,
                workspace_confirmed=args.workspace_confirmed, allow_shared_key=args.allow_shared_key, observe_view=args.observe))
        else:
            require(all(value is None for value in (args.provider, args.model, args.max_calls, args.cap_usd)), "Live options require --live")
            require(not args.allow_shared_key and not args.workspace_confirmed and all(v is None for v in (args.workspace_id, args.workspace_cap_usd,
                args.workspace_usage_usd, args.workspace_month)), "Workspace options require Anthropic live mode")
            report = asyncio.run(evaluate(args.output, split=args.split, case_id=args.case, repeat=args.repeat, observe_view=args.observe))
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    passed = sum(row["status"] == "passed" for row in report["results"])
    if args.live:
        print(f"Live: {passed}/{len(report['results'])} automated passes; human review pending. {args.output}")
        return 0 if passed == len(report["results"]) else 1
    print(f"Offline: {passed}/{report['selected_executions']} passed; live accuracy unmeasured. {args.output}")
    return 0 if passed == report["selected_executions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
