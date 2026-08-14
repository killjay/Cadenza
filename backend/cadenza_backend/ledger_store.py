"""The JSON ledger and RFC 6902 patching — blueprint §2.

The contract that matters here, and the reason this module is more than a
`jsonpatch.apply_patch` call:

    A patch either applies completely and leaves a ledger that still validates,
    or it changes nothing at all and raises.

There is no third outcome. A half-applied patch would put the ledger — the
single source of truth the agents read, the frontend renders, and the geometry
service builds from — into a state no one wrote and no one can reproduce. That
is the failure mode worth engineering against, so this module:

  1. patches a *copy* (`in_place=False`), never the live document;
  2. re-validates the result against the `Ledger` model before adopting it;
  3. rejects patches that touch identity/bookkeeping fields the agent has no
     business writing (`schema_version`, `project_id`, `revision`);
  4. maps every failure onto a `CadenzaError` with a specific `ErrorCode`, so a
     bad pointer reads as PATCH_UNKNOWN_TARGET rather than a bare KeyError.

`revision` is deliberately NOT bumped here. Per the ledger contract's
invariants, a revision that exists is one that has been *built* — so the bump
belongs to whoever confirms the geometry rebuild, not to the patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import jsonpatch
import jsonpointer
from pydantic import ValidationError

from cadenza_backend.contracts_bridge import (
    CadenzaError,
    ErrorCode,
    Ledger,
    PatchOp,
    PointerResolutionError,
    empty_ledger,
    normalize_patch,
)

IMMUTABLE_FIELDS = ("schema_version", "project_id", "revision")
"""Top-level fields an agent patch may never change. See `_reject_identity_drift`."""

FORBIDDEN_PATHS = frozenset(
    {
        "/schema_version",
        "/project_id",
        "/revision",
        "/metadata/global_units",
        "/metadata/workspace_mode",
    }
)
"""CONTRACTS.md §5 "Forbidden paths" — rejected outright, no repair retry.

Plus `/features/<n>/id`, which needs a pattern rather than a literal and is
checked separately. "An agent bumping its own revision or switching units is not
confused, it is out of contract."
"""

_FEATURE_ID_PATH = re.compile(r"^/features/[^/]+/id$")


def _reject_forbidden_paths(ops: list[dict[str, Any]]) -> None:
    """Pre-flight check. Runs before jsonpatch so nothing is attempted at all."""
    for op in ops:
        for key in ("path", "from"):
            path = op.get(key)
            if not path:
                continue
            if path in FORBIDDEN_PATHS or _FEATURE_ID_PATH.match(path):
                raise CadenzaError(
                    # NOTE: the contract specifies PATCH_FORBIDDEN_PATH here, but
                    # that member does not exist in errors.py yet. Raised with the
                    # architect; using PATCH_INVALID until it lands so the failure
                    # is still loud and correctly non-retryable.
                    ErrorCode.PATCH_INVALID,
                    f"patch touches the forbidden path `{path}`",
                    detail=(
                        "Forbidden: /schema_version, /project_id, /revision, "
                        "/metadata/global_units, /metadata/workspace_mode, /features/<n>/id"
                    ),
                    recoverable=False,
                )


@dataclass(frozen=True)
class AppliedPatch:
    """The outcome of a successful patch. `ledger` is a new object; the input is untouched."""

    ledger: Ledger
    ops: list[dict[str, Any]]
    """The normalised, index-addressed ops actually handed to jsonpatch."""


def _reject_identity_drift(before: dict[str, Any], after: dict[str, Any]) -> None:
    for name in IMMUTABLE_FIELDS:
        if before.get(name) != after.get(name):
            raise CadenzaError(
                ErrorCode.LEDGER_INVALID,
                f"patch changed `{name}`, which is server-owned",
                detail=f"{name}: {before.get(name)!r} -> {after.get(name)!r}",
            )


def apply_patch(ledger: Ledger, ops: list[PatchOp]) -> AppliedPatch:
    """Apply an RFC 6902 patch to `ledger`, returning a NEW ledger.

    Raises `CadenzaError` — never leaves a partially-patched ledger behind.

    Pointer sugar (`/features/<feature_id>/...`) is resolved to index form by
    the contract's `normalize_patch` before jsonpatch sees it.
    """
    if not ops:
        raise CadenzaError(
            ErrorCode.PATCH_INVALID,
            "empty patch",
            detail=(
                "An empty patch is only legal alongside `needs_clarification`, which the "
                "agent layer handles before reaching the ledger."
            ),
        )

    # 1. Normalise id-addressed pointers -> index-addressed. An id that names no
    #    feature dies here, loudly, instead of silently no-op'ing downstream.
    try:
        normalised = normalize_patch(ledger, ops)
    except PointerResolutionError as exc:
        raise CadenzaError(
            ErrorCode.PATCH_UNKNOWN_TARGET,
            f"patch targets a feature that does not exist: {exc.feature_id}",
            detail=str(exc),
        ) from exc
    except ValueError as exc:  # e.g. MAX_PATCH_OPS exceeded
        raise CadenzaError(ErrorCode.PATCH_INVALID, str(exc)) from exc

    # 2. Forbidden paths die before anything is attempted (CONTRACTS.md §5).
    _reject_forbidden_paths(normalised)

    before = ledger.model_dump(mode="json")

    # 3. Patch a copy. in_place=False is load-bearing: with in_place=True a
    #    multi-op patch that fails on op 3 leaves ops 1-2 applied to the live
    #    ledger, which is exactly the silent corruption we refuse to allow.
    try:
        after = jsonpatch.apply_patch(before, normalised, in_place=False)
    except (jsonpointer.JsonPointerException, jsonpatch.JsonPatchConflict) as exc:
        raise CadenzaError(
            ErrorCode.PATCH_UNKNOWN_TARGET,
            "patch targets a path that does not exist in the ledger",
            detail=f"{exc} | ops={normalised}",
        ) from exc
    except jsonpatch.InvalidJsonPatch as exc:
        raise CadenzaError(
            ErrorCode.PATCH_INVALID, "not a well-formed RFC 6902 patch", detail=str(exc)
        ) from exc
    except jsonpatch.JsonPatchException as exc:
        raise CadenzaError(
            ErrorCode.PATCH_APPLY_FAILED, "patch could not be applied", detail=str(exc)
        ) from exc

    # 4. Backstop for a wholesale parent replacement (e.g. `replace /metadata`)
    #    that slips a forbidden change past the path check.
    _reject_identity_drift(before, after)

    # 5. The result must still be a legal ledger — this is what catches an agent
    #    writing height=-5, or dropping a required parameter, or breaking the
    #    "first live feature must be additive" rule.
    try:
        patched = Ledger.model_validate(after)
    except ValidationError as exc:
        raise CadenzaError(
            ErrorCode.LEDGER_INVALID,
            "patch applied but produced an invalid ledger; discarded",
            detail=str(exc),
        ) from exc

    return AppliedPatch(ledger=patched, ops=normalised)


@dataclass
class LedgerStore:
    """One project's ledger plus its built-revision bookkeeping.

    In-memory and single-process — fine for the prototype, and the seam where a
    real store (Redis/Postgres) would slot in later.
    """

    ledger: Ledger = field(default_factory=empty_ledger)
    history: list[Ledger] = field(default_factory=list)

    @classmethod
    def new(cls, name: str = "Untitled Part") -> LedgerStore:
        return cls(ledger=empty_ledger(name))

    def apply(self, ops: list[PatchOp]) -> AppliedPatch:
        """Patch the held ledger. Adopted only if `apply_patch` fully succeeds."""
        result = apply_patch(self.ledger, ops)
        self.commit(result)
        return result

    def stage(self, ops: list[PatchOp]) -> AppliedPatch:
        """Patch WITHOUT adopting the result — the caller holds a candidate.

        This is the half of `apply` the turn pipeline actually wants. The
        contract's rule is that a failed build leaves no trace, and the cheapest
        way to guarantee that is to never mutate the store until geometry has
        confirmed the candidate builds. Then there is nothing to roll back.
        """
        return apply_patch(self.ledger, ops)

    def commit(self, result: AppliedPatch) -> None:
        """Adopt a staged candidate, keeping the pre-patch state for undo."""
        self.history.append(self.ledger)
        self.ledger = result.ledger

    def mark_built(self) -> int:
        """Bump `revision` — call ONLY after a successful geometry rebuild.

        The ledger contract's invariant is that a revision which exists has been
        built, so this is separated from `apply` on purpose.
        """
        self.ledger = self.ledger.model_copy(update={"revision": self.ledger.revision + 1})
        return self.ledger.revision

    def rollback(self) -> Ledger:
        """Undo the last applied patch. Prototype-grade: no redo stack."""
        if self.history:
            self.ledger = self.history.pop()
        return self.ledger
