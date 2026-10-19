"""One occurrence gets a private editable branch; existing preview owns acceptance."""
from copy import deepcopy
from dataclasses import dataclass
import math
from typing import NoReturn

from cadenza_contracts.assembly_commands import AssemblyCommand, MakeIndependent
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.ids import new_project_id
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.part_commands import MAX_SOURCE_PARTS
from cadenza_contracts.references import validate_references
from cadenza_backend.geometry_jobs import inspect
from cadenza_backend.geometry_bridge import BuildOutcome


def verify_measurements(before: BuildOutcome, after: BuildOutcome):
    measurements = []
    for outcome in (before, after):
        if (not outcome.ok or outcome.stub or outcome.volume_mm3 is None or not outcome.bbox
                or any(len(outcome.bbox.get(key, [])) != 3 for key in ("min", "max"))):
            reject("Native shape measurements are unavailable. No changes were accepted.")
        measurements.append([outcome.volume_mm3, *outcome.bbox["min"], *outcome.bbox["max"]])
    if not all(math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7)
               for a, b in zip(*measurements)):
        reject("The independent copy did not preserve native shape measurements.")


def reject(message) -> NoReturn:
    raise CadenzaError(ErrorCode.BAD_MESSAGE, message)


def source_at(documents, lid, path):
    """Resolve identities only; unresolved annotations are allowed to stay unresolved."""
    for cid in path:
        doc = documents.get(lid, {})
        component = next((c for c in (doc.get("assembly") or {}).get("components", []) if c["id"] == cid), None)
        if component is None or not component.get("sub_assembly_id"):
            return None
        lid = component["sub_assembly_id"]
    return lid if lid in documents else None


@dataclass
class Independence:
    ledgers: dict[str, Ledger]
    # Includes primary -> primary; every other entry identifies a private clone.
    copies: dict[str, str]
    path: list[str]
    source_id: str
    new_source_id: str
    name: str
    references: list[tuple[str, str, str]]

    def result(self):
        return dict(component_path=self.path, old_source_id=self.source_id,
            new_source_id=self.new_source_id, name=self.name, copied_assemblies=len(self.copies) - 2)


def prepare(session, request: AssemblyCommand) -> Independence:
    from cadenza_backend.assembly_commands import assembly_instances, resolve_owner, validate_joints, validate_source
    operation = request.operations[0]
    if request.scope != "occurrence" or len(request.operations) != 1 or not isinstance(operation, MakeIndependent):
        reject("Make independent requires one explicit occurrence request.")
    before = session.snapshot()
    try:
        owner = resolve_owner(session, request.ledger_id, request.owner_path)
        assembly_instances(before, session.primary_ledger_id)
    except ValueError as exc:
        reject(str(exc))
    if owner.assembly is None:
        reject("Select a placed part inside an assembly.")
    component = next((c for c in owner.assembly.components if c.id == operation.component_id), None)
    source = before.get(component.sub_assembly_id or "") if component else None
    if source is None or source.assembly is not None:
        reject("Select one placed source part. Embedded, catalog-only parts and whole subassemblies cannot be made independent.")
    assert source is not None
    validate_source(source)
    if sum(l.assembly is None and bool(l.features) for l in before.values()) >= MAX_SOURCE_PARTS:
        reject("Make independent would exceed the 32 source-part limit.")
    if any(l.assembly is None and l.metadata.name.strip().casefold() == operation.name.casefold() for l in before.values()):
        reject("Choose a distinct source part name.")
    if sum(l.assembly is not None and lid != session.primary_ledger_id for lid, l in before.items()) + len(request.owner_path) > 8:
        reject("Copying this branch would exceed the eight subassembly-definition limit. Original definitions are retained.")
    path = [*request.owner_path, operation.component_id]
    originals = {lid: l.model_dump(mode="json") for lid, l in before.items()}
    owners, lid = [], session.primary_ledger_id
    for cid in path:
        ledger = before[lid]
        assert ledger.assembly is not None
        if ledger.features:
            reject("Assembly definitions with modelling features are not supported for Make independent.")
        try:
            validate_joints(ledger.assembly)
        except ValueError as exc:
            reject(str(exc))
        if any(c.placement.relative_to for c in ledger.assembly.components):
            reject("Relative component placements are not supported for Make independent.")
        owners.append((lid, cid))
        lid = next(c.sub_assembly_id for c in ledger.assembly.components if c.id == cid)
    copies = {session.primary_ledger_id: session.primary_ledger_id,
              **{old: new_project_id() for old, _ in owners[1:]}, source.project_id: new_project_id()}
    candidate = deepcopy(originals)
    names = {l.metadata.name.casefold() for l in before.values()}
    for old, new in copies.items():
        doc = deepcopy(originals[old]); doc["project_id"] = new
        if old == source.project_id:
            doc["metadata"]["name"] = operation.name
        elif old != session.primary_ledger_id:
            base = before[old].metadata.name[:75] + " independent"
            name, suffix = base, 2
            while name.casefold() in names:
                name = f"{base} {suffix}"; suffix += 1
            names.add(name.casefold()); doc["metadata"]["name"] = name
        candidate[new] = doc
    for old, cid in owners:
        doc = candidate[copies[old]]
        child = next(c for c in doc["assembly"]["components"] if c["id"] == cid)
        old_child = child["sub_assembly_id"]
        child["sub_assembly_id"] = copies[old_child]
        for joint in doc["assembly"]["joints"]:
            for side in ("a", "b"):
                target = joint[f"target_{side}"]
                if joint[f"component_{side}"] == cid and target["mode"] == "planar_face":
                    ref = target["reference"]
                    if ref["ledger_id"] != old_child or ref["source_ledger_id"] != old_child:
                        reject("Repair the attachment's source reference before making this part independent.")
                    ref["ledger_id"] = ref["source_ledger_id"] = copies[old_child]
    references = []
    for old, new in copies.items():
        for key, ref in candidate[new]["surface_references"].items():
            if ref["ledger_id"] != old:
                if old == session.primary_ledger_id and source_at(originals, ref["ledger_id"], ref["component_path"]) == source_at(candidate, ref["ledger_id"], ref["component_path"]):
                    continue
                reject("A copied definition contains a foreign-owned saved surface. Repair its owner before making this part independent.")
            original_ref = originals[old]["surface_references"][key]
            was = source_at(originals, old, ref["component_path"])
            now = source_at(candidate, new, ref["component_path"])
            ref["ledger_id"] = new
            if was == ref["source_ledger_id"] and now is not None:
                ref["source_ledger_id"] = now
            if ref != original_ref:
                references.append((old, new, key))
    # Original definitions remain untouched. Reject foreign annotations which would
    # be broken by the changed primary branch rather than editing unrelated sources.
    for old, doc in originals.items():
        if old == session.primary_ledger_id:
            continue
        for ref in doc["surface_references"].values():
            if ref["ledger_id"] == session.primary_ledger_id and source_at(originals, ref["ledger_id"], ref["component_path"]) != source_at(candidate, ref["ledger_id"], ref["component_path"]):
                reject("An external saved surface references this occurrence. Repair that reference in its owner before making the part independent.")
    try:
        ledgers = session.stage({new: Ledger.model_validate(candidate[new]) for new in copies.values()})
        validate_references(ledgers[session.primary_ledger_id].model_dump(mode="json"), ledgers)
        assembly_instances(ledgers, session.primary_ledger_id)
    except ValueError as exc:
        reject(str(exc))
    result = Independence(ledgers, copies, path, source.project_id, copies[source.project_id], operation.name, references)
    validate_delta(before, result, session.primary_ledger_id)
    return result


def validate_delta(before: dict[str, Ledger], prepared: Independence, primary: str):
    try:
        _validate_delta(before, prepared, primary)
    except (KeyError, StopIteration, TypeError) as exc:
        reject(f"Independence candidate has an invalid branch structure ({type(exc).__name__}).")


def _validate_delta(before: dict[str, Ledger], prepared: Independence, primary: str):
    """Reverse only the allowed identity edits, then compare full original payloads."""
    after, copies = prepared.ledgers, prepared.copies
    new_ids = {new for old, new in copies.items() if old != new}
    if set(after) != set(before) | new_ids or new_ids & set(before):
        reject("Independence candidate changed the set of definitions unexpectedly.")
    old_docs = {lid: l.model_dump(mode="json", exclude={"revision"}) for lid, l in before.items()}
    new_docs = {lid: l.model_dump(mode="json", exclude={"revision"}) for lid, l in after.items()}
    old_owner, new_owner = primary, primary
    allowed_children = {}
    for cid in prepared.path:
        old_component = next(c for c in old_docs[old_owner]["assembly"]["components"] if c["id"] == cid)
        new_component = next(c for c in new_docs[new_owner]["assembly"]["components"] if c["id"] == cid)
        expected = copies[old_component["sub_assembly_id"]]
        if new_component["sub_assembly_id"] != expected:
            reject("Independence candidate relinked a different branch.")
        allowed_children[new_owner] = (cid, old_component["sub_assembly_id"], expected)
        old_owner, new_owner = old_component["sub_assembly_id"], expected
    if (old_owner, new_owner) != (prepared.source_id, prepared.new_source_id):
        reject("Independence candidate changed the selected source.")
    for lid in before:
        if lid != primary and old_docs[lid] != new_docs[lid]:
            reject("Independence changed an original definition.")
    for old, new in copies.items():
        normalized = deepcopy(new_docs[new]); original = old_docs[old]
        normalized["project_id"] = old
        if old == prepared.source_id and normalized["metadata"]["name"] != prepared.name:
            reject("Independence candidate changed the requested name.")
        if old != primary:
            normalized["metadata"]["name"] = original["metadata"]["name"]
        if new in allowed_children:
            cid, source, copied = allowed_children[new]
            next(c for c in normalized["assembly"]["components"] if c["id"] == cid)["sub_assembly_id"] = source
            for j in normalized["assembly"]["joints"]:
                for side in ("a", "b"):
                    target = j[f"target_{side}"]
                    if j[f"component_{side}"] == cid and target["mode"] == "planar_face":
                        ref = target["reference"]
                        if ref["ledger_id"] != copied or ref["source_ledger_id"] != copied:
                            reject("Independence did not preserve an attachment endpoint.")
                        ref["ledger_id"] = ref["source_ledger_id"] = source
        for key, ref in normalized["surface_references"].items():
            prior = original["surface_references"].get(key)
            if old == primary and prior == ref and ref["ledger_id"] != old:
                if source_at(old_docs, ref["ledger_id"], ref["component_path"]) == source_at(new_docs, ref["ledger_id"], ref["component_path"]):
                    continue
            if prior is None or ref["ledger_id"] != new:
                reject("Independence changed saved surface identity.")
            old_source = source_at(old_docs, old, prior["component_path"])
            new_source = source_at(new_docs, new, prior["component_path"])
            expected = new_source if old_source == prior["source_ledger_id"] and new_source is not None else prior["source_ledger_id"]
            if ref["source_ledger_id"] != expected:
                reject("Independence retargeted a saved surface.")
            ref["ledger_id"], ref["source_ledger_id"] = prior["ledger_id"], prior["source_ledger_id"]
        if normalized != original:
            reject("Independence candidate changed geometry, placement or data outside the requested copy.")


async def verify(before, prepared: Independence, primary):
    from cadenza_backend.assembly_commands import assembly_instances
    after = prepared.ledgers
    old_rows = assembly_instances(before, primary)
    new_rows = {row.path: row for row in assembly_instances(after, primary)}
    if set(new_rows) != {row.path for row in old_rows}:
        reject("Independence changed the assembly occurrence paths.")
    for row in old_rows:
        if row.source_id is None or before[row.source_id].assembly is not None:
            continue
        new_source = new_rows[row.path].source_id
        a = await inspect(before[primary], dict(query_type="instance_context", component_path=list(row.path), source_ledger_id=row.source_id), before)
        b = await inspect(after[primary], dict(query_type="instance_context", component_path=list(row.path), source_ledger_id=new_source), after)
        av, bv = a.get("source_to_project", []), b.get("source_to_project", [])
        if len(av) != 16 or len(bv) != 16 or not all(math.isfinite(x) and math.isfinite(y) and math.isclose(x, y, rel_tol=0, abs_tol=1e-7) for x, y in zip(av, bv)):
            reject("The independent copy changed an assembly pose. No changes were accepted.")
    warnings = []
    for old, new, key in prepared.references:
        a = await inspect(before[old], dict(query_type="resolve_surface_reference", reference_id=key), before)
        b = await inspect(after[new], dict(query_type="resolve_surface_reference", reference_id=key), after)
        if any(result.get("status") not in {"resolved", "missing", "ambiguous", "unsupported"} for result in (a, b)):
            reject("Saved surface verification returned incomplete evidence. No changes were accepted.")
        if a.get("status") == "resolved" and b.get("status") != "resolved":
            reject("A saved surface could not be preserved in the independent copy.")
        if a.get("status") != "resolved":
            if b.get("status") != a.get("status"):
                reject("An unresolved saved surface changed meaning during the copy. Repair it first.")
            warnings.append(f"Saved surface {key} remains {b.get('status')}; it was not repaired by copying.")
    return ["Copied editable geometry and solved placement preserved; original sources retained.", *warnings]
