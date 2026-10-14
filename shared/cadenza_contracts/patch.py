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

    ledger_id: str | None = None
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
    
    Supports:
      /features/<id>
      /assembly/parts/<id>
      /assembly/parts/<id>/features/<id>
      /assembly/components/<id>
      /assembly/joints/<id>
    """
    if not path.startswith("/features/") and path != "/features" and not path.startswith("/assembly/"):
        return path
        
    parts = path.split("/")
    
    # 1. /features/<id>
    if len(parts) >= 3 and parts[1] == "features":
        seg = parts[2]
        if seg != "-" and not seg.isdigit():
            idx = feature_index(ledger, seg)
            if idx is None:
                raise PointerResolutionError(path, seg)
            parts[2] = str(idx)
            
    # 2. /assembly/parts/<id>
    if len(parts) >= 4 and parts[1] == "assembly" and parts[2] == "parts":
        seg = parts[3]
        if seg != "-" and not seg.isdigit():
            idx = None
            if ledger.assembly:
                for i, p in enumerate(ledger.assembly.parts):
                    if p.id == seg:
                        idx = i
                        break
            if idx is None:
                raise PointerResolutionError(path, seg)
            parts[3] = str(idx)
            
        # 3. /assembly/parts/<id or idx>/features/<id>
        if len(parts) >= 6 and parts[4] == "features":
            f_seg = parts[5]
            if f_seg != "-" and not f_seg.isdigit():
                part_idx = int(parts[3])
                idx = None
                if ledger.assembly and part_idx < len(ledger.assembly.parts):
                    part = ledger.assembly.parts[part_idx]
                    for i, f in enumerate(part.features):
                        if f.id == f_seg:
                            idx = i
                            break
                if idx is None:
                    raise PointerResolutionError(path, f_seg)
                parts[5] = str(idx)

    # 4. /assembly/components/<id>
    if len(parts) >= 4 and parts[1] == "assembly" and parts[2] == "components":
        seg = parts[3]
        if seg != "-" and not seg.isdigit():
            idx = None
            if ledger.assembly:
                for i, c in enumerate(ledger.assembly.components):
                    if c.id == seg:
                        idx = i
                        break
            if idx is None:
                raise PointerResolutionError(path, seg)
            parts[3] = str(idx)

    # 5. /assembly/joints/<id>
    if len(parts) >= 4 and parts[1] == "assembly" and parts[2] == "joints":
        seg = parts[3]
        if seg != "-" and not seg.isdigit():
            idx = None
            if ledger.assembly:
                for i, j in enumerate(ledger.assembly.joints):
                    if j.id == seg:
                        idx = i
                        break
            if idx is None:
                raise PointerResolutionError(path, seg)
            parts[3] = str(idx)

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
    if len(parts) == 5 and parts[1] == "assembly" and parts[2] in ("parts", "components", "joints") and parts[4] == "id":
        raise CadenzaError(
            ErrorCode.PATCH_FORBIDDEN_PATH,
            f"{path}: assembly entity ids are immutable",
        )
    if len(parts) == 7 and parts[1] == "assembly" and parts[2] == "parts" and parts[4] == "features" and parts[6] == "id":
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
    document = ledger.model_dump(mode="json")
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
            if d.get("from"):
                d["from"] = _document_pointer(document, d["from"])
            destination = document
            if d["op"] == "move" and d.get("from") and op.path != op.from_:
                destination = jsonpatch.apply_patch(document, [{"op": "remove", "path": d["from"]}], in_place=False)
            d["path"] = _document_pointer(destination, d["path"])
        except PointerResolutionError as exc:
            known = ", ".join(f.id for f in ledger.features) or "(none)"
            raise CadenzaError(
                ErrorCode.PATCH_UNKNOWN_TARGET,
                str(exc),
                detail=f"known feature ids: {known}",
            ) from exc
        except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException, TypeError) as exc:
            raise CadenzaError(ErrorCode.PATCH_APPLY_FAILED, f"patch could not be resolved: {exc}") from exc
        try:
            after = jsonpatch.apply_patch(document, [d], in_place=False)
        except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException, TypeError) as exc:
            raise CadenzaError(ErrorCode.PATCH_APPLY_FAILED, f"patch could not be applied: {exc}") from exc
        for path in FORBIDDEN_PATHS:
            if jsonpointer.resolve_pointer(document, path, None) != jsonpointer.resolve_pointer(after, path, None):
                raise CadenzaError(ErrorCode.PATCH_FORBIDDEN_PATH, f"Parent replacement changed server-owned {path}")
        if d["op"] in ("replace", "add", "copy", "move"):
            before_value = jsonpointer.resolve_pointer(document, d["path"], None)
            after_value = jsonpointer.resolve_pointer(after, d["path"], None)
            # Array insertion moves existing identities; object replacement must preserve them.
            parent_path = d["path"].rsplit("/", 1)[0]
            parent = jsonpointer.resolve_pointer(document, parent_path, None)
            if d["op"] == "replace" or not isinstance(parent, list):
                _preserve_ids(before_value, after_value)
        document = after
        out.append(d)
    from cadenza_contracts.parameters import reject_bound_overrides
    try:
        reject_bound_overrides(ledger.model_dump(mode="json"), document)
    except ValueError as exc:
        raise CadenzaError(ErrorCode.LEDGER_INVALID, str(exc)) from exc
    return out


def _document_pointer(document: dict[str, Any], path: str) -> str:
    """Resolve IDs against the result of preceding operations, without model coercion."""
    tokens = jsonpointer.JsonPointer(path).parts
    current: Any = document
    resolved = []
    for token in tokens:
        if isinstance(current, list) and token != "-" and not token.isdigit():
            index = next((i for i, item in enumerate(current) if isinstance(item, dict) and item.get("id") == token), None)
            if index is None:
                raise PointerResolutionError(path, token)
            token = str(index)
        resolved.append(token.replace("~", "~0").replace("/", "~1"))
        if isinstance(current, dict):
            current = current.get(token)
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            current = None
    return "/" + "/".join(resolved)


def _preserve_ids(before: Any, after: Any) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        if "id" in before and before.get("id") != after.get("id"):
            raise CadenzaError(ErrorCode.PATCH_FORBIDDEN_PATH, "Replacement cannot rename an entity")
        for key in before.keys() & after.keys():
            _preserve_ids(before[key], after[key])
    elif isinstance(before, list) and isinstance(after, list):
        old = {item["id"]: item for item in before if isinstance(item, dict) and "id" in item}
        new = {item["id"]: item for item in after if isinstance(item, dict) and "id" in item}
        if old.keys() != new.keys():
            raise CadenzaError(ErrorCode.PATCH_FORBIDDEN_PATH, "Use explicit add/remove operations to change entity membership")
        for key in old:
            _preserve_ids(old[key], new[key])


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
        result = Ledger.model_validate(patched)
        from cadenza_contracts.references import validate_references
        validate_references(result.model_dump(mode="json"))
        from cadenza_contracts.patterns import validate_pattern_changes
        validate_pattern_changes(doc, patched, result.model_dump(mode="json"))
        return result
    except ValidationError as exc:
        raise CadenzaError(
            ErrorCode.LEDGER_INVALID,
            "the patched document is not a valid ledger",
            detail=exc.json(indent=None),
        ) from exc
    except ValueError as exc:
        raise CadenzaError(ErrorCode.LEDGER_INVALID, str(exc)) from exc
