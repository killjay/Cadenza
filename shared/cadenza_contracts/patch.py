"""RFC 6902 patches, plus CADenza's one deviation: id-addressed pointer sugar.

The blueprint contradicts itself on ledger addressing (section 2 shows an array
`history_tree` but a patch path of `/objects/obj_base_01/...`). RULING:

    The ledger stores `features` as an ORDERED ARRAY.
    Agents MAY address a feature by id instead of index:

        /features/feat_base_9f2a/parameters/height     (agent writes this)
        /features/0/parameters/height                  (what gets applied)

`normalize_patch` rewrites the first form into the second before the patch goes
to `jsonpatch`. An unresolvable id is PATCH_UNKNOWN_TARGET, never a silent
no-op. Rationale: array indices shift under add/remove and an LLM tracking them
across turns is a bug factory; ids are stable and self-checking.

THE APPLY PATH LIVES HERE, not in server/. One implementation, so the backend
and any test harness can never disagree about what a patch means.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

import jsonpatch
import jsonpointer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.ledger import Ledger, feature_index

MAX_PATCH_OPS = 32
"""Hard cap. An agent emitting more than this in one turn is malfunctioning."""

FORBIDDEN_PATHS: tuple[str, ...] = (
    "/schema_version",
    "/project_id",
    "/revision",
    "/metadata/global_units",
    "/metadata/workspace_mode",
)
"""Server-owned fields. A patch touching one of these (or anything under it) is
rejected outright with no repair retry — an agent trying to bump its own
revision or switch units is not confused, it is out of contract.

Feature ids are also immutable: `/features/<n>/id` is rejected, because
renaming an id silently breaks click-attribution and undo. Adding a whole
feature object (which contains an `id`) is of course fine.
"""


class PatchOp(BaseModel):
    """One RFC 6902 operation."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    op: Literal["add", "remove", "replace", "move", "copy", "test"]
    path: str
    value: Any = None
    from_: str | None = Field(default=None, alias="from")

    def to_jsonpatch(self) -> dict[str, Any]:
        d: dict[str, Any] = {"op": self.op, "path": self.path}
        if self.op in ("add", "replace", "test"):
            d["value"] = self.value
        if self.op in ("move", "copy"):
            d["from"] = self.from_
        return d


class AgentPatchOutput(BaseModel):
    """THE agent output contract. EVERY ledger-mutating agent returns exactly
    this — Draftsman (first build) and Machinist (edits) alike.

    The Draftsman expresses a first build as appends:
        [{"op": "add", "path": "/features/-", "value": {...box...}}, ...]

    One output schema means one validator, one apply path, one failure mode.

    Obtained through the provider's structured-output / forced-tool-use
    facility, so a well-formed response is guaranteed at the API level and a
    malformed one is an API error rather than a parsing guess.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description=(
            "One sentence, user-facing, past tense. "
            "e.g. 'Doubled the plate thickness to 40 mm.'"
        )
    )
    patch: list[PatchOp] = Field(
        default_factory=list,
        max_length=MAX_PATCH_OPS,
        description=(
            "RFC 6902 operations against the ledger. "
            "Empty ONLY when needs_clarification is set."
        ),
    )
    assumptions: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Values inferred rather than read. "
            "Shape: {field, value, basis, confidence}."
        ),
    )
    needs_clarification: str | None = Field(
        default=None,
        description=(
            "A question for the user. Set ONLY when the request cannot be "
            "turned into geometry. Then `patch` must be empty."
        ),
    )

    @property
    def is_question(self) -> bool:
        return bool(self.needs_clarification) and not self.patch


class PointerResolutionError(ValueError):
    """`/features/<id>/...` named a feature that does not exist."""

    def __init__(self, path: str, feature_id: str) -> None:
        super().__init__(f"no feature with id {feature_id!r} (in path {path!r})")
        self.path = path
        self.feature_id = feature_id


def normalize_pointer(ledger: Ledger, path: str) -> str:
    """Rewrite an id-addressed feature pointer to an index-addressed one.

    Only the segment immediately after `/features` is treated as an id, and
    only when it is neither a non-negative integer nor the RFC 6902 append
    token `-`.

    >>> normalize_pointer(led, "/features/feat_base_9f2a/parameters/height")
    '/features/0/parameters/height'
    """
    if not path.startswith("/features/") and path != "/features":
        return path
    parts = path.split("/")  # ['', 'features', <seg>, ...]
    if len(parts) < 3:
        return path
    seg = parts[2]
    if seg == "-" or seg.isdigit():
        return path
    idx = feature_index(ledger, seg)
    if idx is None:
        raise PointerResolutionError(path, seg)
    parts[2] = str(idx)
    return "/".join(parts)


def _check_forbidden(path: str) -> None:
    for bad in FORBIDDEN_PATHS:
        if path == bad or path.startswith(bad + "/"):
            raise CadenzaError(
                ErrorCode.PATCH_FORBIDDEN_PATH,
                f"{path} is server-owned and cannot be patched",
                detail=f"forbidden prefixes: {', '.join(FORBIDDEN_PATHS)}",
            )
    parts = path.split("/")
    if len(parts) == 4 and parts[1] == "features" and parts[3] == "id":
        raise CadenzaError(
            ErrorCode.PATCH_FORBIDDEN_PATH,
            f"{path}: feature ids are immutable",
        )


def normalize_patch(ledger: Ledger, ops: list[PatchOp]) -> list[dict[str, Any]]:
    """Validate + normalise a patch into plain dicts ready for `jsonpatch`.

    Raises CadenzaError with PATCH_INVALID / PATCH_FORBIDDEN_PATH /
    PATCH_UNKNOWN_TARGET. Never mutates `ledger`.
    """
    if len(ops) > MAX_PATCH_OPS:
        raise CadenzaError(
            ErrorCode.PATCH_INVALID,
            f"patch has {len(ops)} operations, the maximum is {MAX_PATCH_OPS}",
        )
    out: list[dict[str, Any]] = []
    for op in ops:
        d = op.to_jsonpatch()
        if not d["path"].startswith("/"):
            raise CadenzaError(
                ErrorCode.PATCH_INVALID,
                f"path {d['path']!r} is not a JSON Pointer (must start with '/')",
            )
        _check_forbidden(d["path"])
        if d.get("from"):
            _check_forbidden(d["from"])
        try:
            d["path"] = normalize_pointer(ledger, d["path"])
            if d.get("from"):
                d["from"] = normalize_pointer(ledger, d["from"])
        except PointerResolutionError as exc:
            known = ", ".join(f.id for f in ledger.features) or "(none)"
            raise CadenzaError(
                ErrorCode.PATCH_UNKNOWN_TARGET,
                str(exc),
                detail=f"known feature ids: {known}",
            ) from exc
        out.append(d)
    return out


def apply_patch(ledger: Ledger, ops: list[PatchOp]) -> Ledger:
    """Apply a patch to a COPY of the ledger and re-validate the result.

    Returns the new Ledger with `revision` UNCHANGED — bumping the revision is
    the server's job, and only after geometry has rebuilt successfully.

    Raises CadenzaError with:
      PATCH_INVALID / PATCH_FORBIDDEN_PATH / PATCH_UNKNOWN_TARGET (from
      normalize_patch), PATCH_APPLY_FAILED (jsonpatch rejected it),
      LEDGER_INVALID (the resulting document is not a valid Ledger).

    The input `ledger` is never mutated, on any path.
    """
    normalized = normalize_patch(ledger, ops)
    doc = copy.deepcopy(ledger.model_dump(mode="json"))
    try:
        patched = jsonpatch.apply_patch(doc, normalized)
    except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException) as exc:
        raise CadenzaError(
            ErrorCode.PATCH_APPLY_FAILED,
            f"patch could not be applied: {exc}",
            detail=repr(normalized),
        ) from exc
    try:
        return Ledger.model_validate(patched)
    except ValidationError as exc:
        raise CadenzaError(
            ErrorCode.LEDGER_INVALID,
            "the patched document is not a valid ledger",
            detail=exc.json(indent=None),
        ) from exc
