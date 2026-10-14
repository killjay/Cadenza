"""Offline-first evaluation of the real sketch proposal path on synthetic sessions.

Run from the repository root: python -m cadenza_backend.sketch_evaluation
Live mode requires an explicitly selected OpenRouter model and a verified non-resetting key cap.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from pydantic import ValidationError

from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.agents.sketch_editor import CARD, SketchIntent
from cadenza_backend.evaluation_budget import Budget, EvaluationLimit
from cadenza_backend.geometry_jobs import build_design, runner
from cadenza_backend.session import Session, LedgerStore, SessionLogger
from cadenza_backend.sketch_drafts import handle_draft, _document, PendingDraft
from cadenza_backend.sketch_prompts import handle_sketch_prompt
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.sketch_draft import DraftStart, DraftPrompt, SketchDraft

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = ROOT / "backend/tests/fixtures/sketch_prompt_corpus.json"


class QuietLogger(SessionLogger):
    def __init__(self): pass
    def log(self, event_type: str, **kwargs): pass


class FixtureClient(ModelClient):
    """No settings, SDK or network access; replay only the supplied synthetic responses."""
    def __init__(self, responses: list[dict]):
        self.responses = iter(copy.deepcopy(responses))
        self.calls = 0

    async def complete_json(self, *args, **kwargs) -> dict:
        self.calls += 1
        return next(self.responses)


class CheckedClient(ModelClient):
    """Classify schema failure independently of the production compiler's error code."""
    def __init__(self, delegate: ModelClient):
        self.delegate = delegate
        self.schema_valid: bool | None = None
        self.duration_ms = 0.0

    async def complete_json(self, *args, **kwargs) -> dict:
        started = monotonic()
        self.schema_valid = None
        try:
            raw = await self.delegate.complete_json(*args, **kwargs)
            try:
                SketchIntent.model_validate(raw)
                self.schema_valid = True
            except ValidationError:
                self.schema_valid = False
            return raw
        finally:
            self.duration_ms += (monotonic() - started) * 1000


async def ignore(_): pass


async def make_session(context: dict) -> Session:
    ledger = Ledger.model_validate(context["ledger"])
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})
    session.logger = QuietLogger()
    async with session.lock:
        await handle_draft(session, DraftStart(message_id="evaluation-start", base_revision=session.revision,
                                               profile_id=context.get("profile_id")), ignore)
    return session


def equal(left: Any, right: Any) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-7)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right


def expected_state(context: dict, case: dict) -> dict:
    """Apply authored expectations only. Never derive expectations from model actions."""
    result = copy.deepcopy(context["reference"])
    for section, changes in case.get("changes", {}).items():
        if isinstance(changes, dict):
            for key, value in changes.items():
                if value is None: result[section].pop(key, None)
                else: result[section][key] = value
        else:
            result[section] = changes
    return result


def project_state(draft: PendingDraft, state: SketchDraft, expected: dict) -> dict:
    """Normalize server IDs while retaining exact existing identity and all relationships."""
    if state.solution is None:
        raise ValueError("No solved candidate")
    old_ids = {e.id for e in draft.state.definition.entities}
    names = {e.id: e.id for e in state.solution.entities if e.id in old_ids}
    unmatched = [e for e in state.solution.entities if e.id not in old_ids]
    for name, shape in expected["entities"].items():
        if name in old_ids: continue
        found = [e for e in unmatched if equal(e.model_dump(exclude={"id"}, exclude_none=True), shape)]
        if len(found) != 1:
            raise ValueError("Created geometry differs from the authored outcome")
        names[found[0].id] = name
        unmatched.remove(found[0])
    if unmatched:
        raise ValueError("Unexpected additional entities")
    entities = {names[e.id]: e.model_dump(exclude={"id"}, exclude_none=True) for e in state.solution.entities}
    dimensions: dict[str, list[str]] = {name: [] for name in entities}
    constraints = {c.id: c for c in state.definition.constraints}
    centres = []
    for c in constraints.values():
        if c.kind == "dimension": dimensions[names[c.entity_id]].append(c.parameter)
        else: centres.append(sorted([names[c.entity_a], names[c.entity_b]]))
    bindings = []
    for b in state.relationships.bindings:
        if b.feature_id == state.profile_id:
            if b.constraint_id is None: raise ValueError("Binding lacks its dimension target")
            c = constraints[b.constraint_id]
            if c.kind != "dimension": raise ValueError("Binding target is not a dimension")
            target = f"{names[c.entity_id]}.{c.parameter}"
        else:
            target = f"{b.feature_id}.{b.target}"
        bindings.append([target, b.expression])
    document = Ledger.model_validate(_document(draft, state))
    profile = next(f for f in document.features if f.id == state.profile_id)
    consumers = {f.id: f.parameters.model_dump(mode="json", exclude_none=True)
                 for f in document.features if f.id != state.profile_id}
    return dict(entities=entities, dimensions={k: sorted(v) for k, v in dimensions.items()},
        centres=sorted(centres), parameters={k: v.model_dump(exclude_none=True) for k, v in state.relationships.parameters.items()},
        bindings=sorted(bindings), rules=[c.model_dump() for c in state.relationships.constraints],
        extrusion=state.extrusion_amount, placement=profile.placement.model_dump(), consumers=consumers)


def check_candidate(draft: PendingDraft, candidate: SketchDraft, expected: dict) -> tuple[list[str], dict]:
    try:
        actual = project_state(draft, candidate, expected)
    except (ValueError, KeyError) as exc:
        return [str(exc)], {}
    differences = [key for key in expected if not equal(actual.get(key), expected[key])]
    if candidate.revision != draft.state.revision + 1:
        differences.append("draft_revision")
    # Identity of surviving existing constraints must not be silently replaced.
    remaining = {c.id: c for c in candidate.definition.constraints}
    old_entities = {e.id for e in draft.state.definition.entities}
    for c in draft.state.definition.constraints:
        if c.kind == "dimension":
            required = c.entity_id in expected["dimensions"] and c.parameter in expected["dimensions"][c.entity_id]
        else:
            required = sorted([c.entity_a, c.entity_b]) in expected["centres"]
        if required and c.id not in remaining:
            differences.append("constraint_identity")
    if not old_entities.intersection(expected["entities"]).issubset({e.id for e in candidate.definition.entities}):
        differences.append("entity_identity")
    return sorted(set(differences)), actual


def unrelated_changes(before: Any, expected: Any, actual: Any, path: str = "") -> list[str]:
    if equal(before, expected):
        return [] if equal(expected, actual) else [path or "state"]
    if isinstance(before, dict) and isinstance(expected, dict) and isinstance(actual, dict):
        paths = [f"{path}/{k}" for k in actual.keys() - expected.keys()]
        for key in before.keys() & expected.keys():
            paths += unrelated_changes(before[key], expected[key], actual.get(key), f"{path}/{key}")
        return paths
    return []


async def evaluate_case(context: dict, case: dict, client: ModelClient, *, kernel: bool = True,
                        budget: Budget | None = None) -> dict:
    session = await make_session(context)
    assert session.draft is not None
    draft = session.draft
    before = draft.state.model_copy(deep=True)
    snapshot = session.snapshot()
    start = monotonic()
    result: dict[str, Any] = dict(id=case["id"], category=case["category"], split=case["split"],
        expected_outcome=case["outcome"],
        reviewed=case.get("reviewed", False), passed=False, outcome="infrastructure_failure",
        turns=0, questions=[], differences=[], unrelated_changes=[], kernel="not_run")
    checked = CheckedClient(client)
    text, reply_to = case["prompt"], None
    expected = expected_state(context, case)
    try:
        for turn in range(2):  # At most one authored clarification answer.
            if budget: await budget.refresh(client)
            request = DraftPrompt(message_id=f"evaluation-{turn}", base_revision=session.revision,
                draft_id=before.id, draft_revision=before.revision, prompt=text,
                selected_entity_id=case.get("selected_entity_id"), reply_to=reply_to)
            result["turns"] += 1
            try:
                async with session.lock:
                    await handle_sketch_prompt(session, request, ignore, client=checked)
            except CadenzaError as exc:
                result["error_code"] = exc.code.value
                if exc.code == ErrorCode.PATCH_INVALID:
                    result["outcome"] = "schema_failure" if checked.schema_valid is False else "compiler_rejection"
                    result["passed"] = case["outcome"] == "reject" and checked.schema_valid is True
                else:
                    result["outcome"] = {ErrorCode.AGENT_TIMEOUT: "timeout", ErrorCode.AGENT_REFUSED: "provider_refusal",
                        ErrorCode.AGENT_INVALID_OUTPUT: "provider_or_schema_failure"}.get(exc.code, "infrastructure_failure")
                break
            proposal = draft.proposal
            if proposal is None:
                result["outcome"] = "missing_proposal"
                break
            if proposal.message.question:
                result["questions"].append(proposal.message.question)
                if turn == 0 and case.get("answer"):
                    text, reply_to = case["answer"], proposal.message.proposal_id
                    continue
                result["outcome"] = "safe_unsupported" if case["outcome"] == "unsupported" else "clarification"
                result["passed"] = case["outcome"] in ("clarification", "unsupported", "reject")
                result["question_review"] = "pending"  # Never grade question usefulness from wording.
                break
            if case.get("answer") and turn == 0:
                result["outcome"] = "missing_required_clarification"
                break
            if case["outcome"] != "proposal" or proposal.candidate is None:
                result["outcome"] = "unexpected_executable_proposal"
                break
            differences, actual = check_candidate(draft, proposal.candidate, expected)
            result.update(differences=differences, expected=expected, actual=actual)
            result["unrelated_changes"] = unrelated_changes(context["reference"], expected, actual)
            if differences:
                result["outcome"] = "incorrect_proposal"
                break
            if kernel:
                doc = _document(draft, proposal.candidate)
                if before.is_new:
                    doc["features"].append({"id": proposal.candidate.extrusion_id, "kind": "extrude",
                        "parameters": {"target_feature": before.profile_id, "amount": proposal.candidate.extrusion_amount}})
                built = await build_design(session.stage({before.ledger_id: Ledger.model_validate(doc)}), session.primary_ledger_id)
                result["kernel"] = "passed" if built.ok and not built.stub and built.step and built.glb else "failed"
                if result["kernel"] == "failed":
                    result["outcome"] = "geometry_failure"
                    result["error_code"] = built.code.value if built.code else None
                    break
                # All corpus solids are one straight extrusion. Check the built volume,
                # not merely whether the kernel accepted some valid shape.
                area = sum((1 if e["operation"] == "add" else -1) *
                    (e["width"] * e["height"] if e["kind"] == "rectangle" else math.pi * (e["diameter"] / 2) ** 2)
                    for e in expected["entities"].values())
                depth = expected["extrusion"] if before.is_new else expected["consumers"]["feat_extrusion"]["amount"]
                if built.volume_mm3 is None or not math.isclose(built.volume_mm3, area * depth, rel_tol=1e-6):
                    result.update(outcome="geometry_mismatch", kernel="failed")
                    break
            result["passed"] = True
            result["outcome"] = "resolved_clarification" if turn else "correct_first_attempt"
            break
    except EvaluationLimit:
        result["outcome"] = "budget_or_call_limit"
    except Exception as exc:
        result["outcome"] = "infrastructure_failure"
        result["error_type"] = type(exc).__name__  # Exception bodies may contain provider data.
    finally:
        result["duration_ms"] = (monotonic() - start) * 1000
        result["interpretation_ms"] = checked.duration_ms
        result["accepted_unchanged"] = (session.snapshot() == snapshot and not session.history and not session.redo
            and session.last_geometry is None and session.preview is None and not session.artifacts._items)
        result["draft_unchanged"] = session.draft is draft and draft.state == before
        if not result["accepted_unchanged"] or not result["draft_unchanged"]:
            result.update(passed=False, outcome="state_mutation")
    return result


def report_summary(results: list[dict], calls: list[dict], mode: str) -> dict:
    durations = sorted(r["duration_ms"] for r in results)
    def percentile(p):
        return durations[max(0, math.ceil(len(durations) * p) - 1)] if durations else None
    usage = [c.get("usage") or {} for c in calls]
    fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reported_cost_usd")
    supported = [r for r in results if r["expected_outcome"] == "proposal" and r["category"] != "clarification"]
    return dict(mode=mode, interpretation_quality="unmeasured" if mode == "offline" else "human_review_pending",
        attempted=len(results), passed=sum(r["passed"] for r in results),
        first_attempt=dict(correct=sum(r["outcome"] == "correct_first_attempt" for r in supported), eligible=len(supported)),
        unrelated_change_cases=sum(bool(r["unrelated_changes"]) for r in results),
        state_mutation_cases=sum(r["outcome"] == "state_mutation" for r in results),
        outcomes=dict(Counter(r["outcome"] for r in results)),
        categories={category: dict(attempted=sum(r["category"] == category for r in results),
            passed=sum(r["category"] == category and r["passed"] for r in results))
            for category in sorted({r["category"] for r in results})},
        splits={split: dict(attempted=sum(r["split"] == split for r in results),
            passed=sum(r["split"] == split and r["passed"] for r in results))
            for split in sorted({r["split"] for r in results})},
        human_review=dict(unreviewed_labels=sum(not r["reviewed"] for r in results),
                          questions_pending=sum(bool(r["questions"]) for r in results)),
        latency_ms=dict(sample_count=len(durations), p50=percentile(.5), p95=percentile(.95), scope="proposal_and_optional_kernel"),
        network_calls=len(calls),
        provider_latency_ms=dict(sample_count=len(calls),
            p50=sorted(c["duration_ms"] for c in calls)[max(0, math.ceil(len(calls) * .5) - 1)] if calls else None,
            p95=sorted(c["duration_ms"] for c in calls)[max(0, math.ceil(len(calls) * .95) - 1)] if calls else None),
        usage={field: dict(reported_total=sum(u[field] for u in usage if u.get(field) is not None) if any(u.get(field) is not None for u in usage) else None,
                           missing_calls=sum(u.get(field) is None for u in usage)) for field in fields},
        cost_status="no_provider_calls" if mode == "offline" else "provider_reported_only; no estimates")


def write_report(path: Path, report: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "results.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    summary = report["summary"]
    lines = ["# Sketch prompt evaluation", "", f"Mode: {summary['mode']}. Interpretation quality: {summary['interpretation_quality']}.",
        f"Cases: {summary['passed']}/{summary['attempted']} passed automated checks. Human labels/question review remain separate.",
        "", "| Case | Outcome | Passed | Differences |", "| --- | --- | --- | --- |"]
    lines += [f"| {r['id']} | {r['outcome']} | {r['passed']} | {', '.join(r['differences'])} |" for r in report["results"]]
    lines += ["", "Clarification/unsupported questions requiring human review:", ""]
    for result in report["results"]:
        for question in result["questions"]:
            lines.append(f"- {result['id']}: {question.replace(chr(10), ' ')}")
    lines += ["", "Summary:", "", "```json", json.dumps(summary, indent=2), "```", ""]
    (path / "results.md").write_text("\n".join(lines), encoding="utf-8")


async def run(args) -> dict:
    corpus_bytes = args.corpus.read_bytes()
    corpus = json.loads(corpus_bytes)
    cases = [c for c in corpus["cases"] if args.split == "all" or c["split"] == args.split]
    if not cases: raise ValueError("No cases selected")
    calls: list[dict] = []
    budget = Budget(args.provider, args.model, args.max_calls, args.cap_usd) if args.live else None
    client = ModelClient(observe=calls.append, before_request=budget.admit) if budget else None
    if client:
        client.s = client.s.model_copy(update={"model_machinist": f"{args.provider}:{args.model}"})
    results = []
    for repetition in range(args.repeat):
        for case in cases:
            fixture = FixtureClient([case["response"], *([case["followup_response"]] if "followup_response" in case else [])])
            result = await evaluate_case(corpus["contexts"][case["context"]], case, client or fixture,
                                         kernel=not args.skip_kernel, budget=budget)
            result["repetition"] = repetition + 1
            results.append(result)
            if result["outcome"] == "budget_or_call_limit": break
        if results[-1]["outcome"] == "budget_or_call_limit": break
    return dict(report_version=1, created_at=datetime.now(timezone.utc).isoformat(),
        corpus_version=corpus["version"], corpus_sha256=hashlib.sha256(corpus_bytes).hexdigest(),
        interpreter_prompt_sha256=hashlib.sha256(CARD.system_prompt.encode()).hexdigest(),
        intent_schema_sha256=hashlib.sha256(json.dumps(SketchIntent.model_json_schema(), sort_keys=True).encode()).hexdigest(),
        provider=args.provider if args.live else None, model=args.model if args.live else None,
        kernel_enabled=not args.skip_kernel, repeats=args.repeat, planned_cases=len(cases) * args.repeat,
        budget=dict(cap_usd=budget.cap_usd, max_calls=budget.max_calls, admitted_calls=budget.calls,
                    enforcement="verified_provider_key_cap; one_network_request_per_turn") if budget else None,
        summary=report_summary(results, calls, "live" if args.live else "offline"), calls=calls, results=results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    parser.add_argument("--output", type=Path, default=ROOT / "project_details/evaluation_results/latest")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--cap-usd", type=float, default=0)
    parser.add_argument("--repeat", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--split", choices=["all", "development", "holdout"], default="all")
    parser.add_argument("--skip-kernel", action="store_true", help="Fast evaluator check only; not the complete quality gate")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
        write_report(args.output, report)
        print(json.dumps(report["summary"], indent=2))
        return 0 if all(r["passed"] for r in report["results"]) else 1
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    finally:
        runner.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
