"""Assembly intent compiles privately; the existing preview owns acceptance."""
from dataclasses import dataclass
from uuid import uuid4
from typing import NoReturn
from math import hypot

from cadenza_contracts.assembly_commands import (AssemblyCommand, AssemblyCapabilitiesResponse,
    AssemblyCapabilitiesRequest, AddRigidJoint, RemoveJoint, CreateSubassembly,
    InitializeAssembly, AddInstance, DuplicateInstance, SetInstancePlacement, RenameInstance,
    RemoveInstance, MAX_COMPONENTS, MAX_LEAVES, MAX_DEPTH, OPERATIONS, E2_OPERATIONS)
from cadenza_contracts.ledger import Component, Ledger, Assembly, Joint
from cadenza_contracts.references import validate_references
from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode, PatchOp
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.ledger_store import LedgerStore, apply_patch


@dataclass
class Instance:
    path: tuple[str, ...]
    names: tuple[str, ...]
    component: Component
    source_id: str | None


def assembly_instances(ledgers: dict[str, Ledger], root_id: str) -> list[Instance]:
    """Bounded occurrence traversal; never collapse repeated definitions into one instance."""
    rows: list[Instance] = []
    leaves = 0
    def visit(lid, path, names, ancestors):
        nonlocal leaves
        if lid in ancestors:
            raise ValueError("Assembly dependency cycle")
        owner = ledgers.get(lid)
        if owner is None:
            raise ValueError("Assembly source no longer exists")
        if owner.assembly is None:
            return
        assembly = owner.assembly
        if len(assembly.components) > MAX_COMPONENTS:
            raise ValueError(f"Assembly authoring supports at most {MAX_COMPONENTS} direct components per owner")
        if not assembly.components:
            raise ValueError("An assembly needs at least one component")
        for component in assembly.components:
            current = (*path, component.id)
            labels = (*names, component.name or component.id)
            if len(current) > MAX_DEPTH:
                raise ValueError(f"Assembly authoring supports depth up to {MAX_DEPTH}")
            source = ledgers.get(component.sub_assembly_id or "")
            if component.sub_assembly_id and source is None:
                raise ValueError("Assembly source no longer exists")
            if component.catalog_sku:
                raise ValueError("Resolve catalog geometry before editing this assembly")
            if component.part_id and not any(p.id == component.part_id for p in assembly.parts):
                raise ValueError("Assembly part no longer exists")
            rows.append(Instance(current, labels, component, component.sub_assembly_id))
            if source is not None and source.assembly is not None:
                visit(source.project_id, current, labels, (*ancestors, lid))
            else:
                leaves += 1
                if leaves > MAX_LEAVES:
                    raise ValueError(f"Assembly authoring supports at most {MAX_LEAVES} leaf occurrences")
    visit(root_id, (), (), ())
    return rows


def source_uses(ledgers: dict[str, Ledger], root_id: str, source_id: str) -> list[str]:
    return [" / ".join(row.names) + " (" + "/".join(row.path) + ")"
            for row in assembly_instances(ledgers, root_id) if row.source_id == source_id]


def resolve_owner(session, ledger_id: str, owner_path: list[str]) -> Ledger:
    owner = session.store.ledger
    for component_id in owner_path:
        component = next((c for c in owner.assembly.components if c.id == component_id), None) if owner.assembly else None
        source = session.stores.get(component.sub_assembly_id or "") if component else None
        if source is None or source.ledger.assembly is None:
            raise ValueError("Choose an accepted assembly occurrence from the project tree.")
        owner = source.ledger
    if owner.project_id != ledger_id:
        raise ValueError("The selected occurrence does not identify this assembly definition.")
    return owner


def validate_joints(assembly: Assembly) -> None:
    ids = {c.id for c in assembly.components}
    incoming: dict[str, str] = {}
    for joint in assembly.joints:
        if joint.kind != "rigid" or joint.target_a.reference_feature or joint.target_b.reference_feature:
            raise ValueError("These controls support coordinate-based rigid attachments only.")
        # Reuse strict authoring validation for imported anchors, without duplicating the solver.
        AddRigidJoint.model_validate(dict(op="add_rigid_joint", component_a=joint.component_a,
            component_b=joint.component_b, target_a=dict(point=list(joint.target_a.point), normal=list(joint.target_a.normal) if joint.target_a.normal else None),
            target_b=dict(point=list(joint.target_b.point), normal=list(joint.target_b.normal) if joint.target_b.normal else None)))
        a, b = joint.component_a, joint.component_b
        if a not in ids or b not in ids or a == b:
            raise ValueError("Attachments require two different direct instances.")
        if b in incoming:
            raise ValueError("An instance can have only one incoming rigid attachment.")
        incoming[b] = a
    for child in incoming:
        seen: set[str] = set()
        while child in incoming:
            if child in seen:
                raise ValueError("Rigid attachments must form an acyclic forest.")
            seen.add(child)
            child = incoming[child]


def capabilities(session, request: str | AssemblyCapabilitiesRequest) -> AssemblyCapabilitiesResponse:
    request = AssemblyCapabilitiesRequest(message_id=request) if isinstance(request, str) else request
    ledger_id = request.ledger_id or session.primary_ledger_id
    owner = None
    reason = None
    try:
        owner = resolve_owner(session, ledger_id, request.owner_path)
    except ValueError as exc:
        reason = str(exc)
    if owner is not None and (owner.features or (owner.assembly is None and (owner.surface_references or
            owner.relationships.parameters or owner.relationships.bindings or owner.relationships.constraints))):
        # A part-to-assembly conversion is deliberately a separate future operation.
        reason = "Starting an assembly from a populated primary part requires an explicit conversion, which is not supported yet."
    elif owner is not None and owner.assembly is not None:
        try:
            assembly_instances(session.snapshot(), session.primary_ledger_id)
            if request.version == 2:
                validate_joints(owner.assembly)
                if any(c.placement.relative_to for c in owner.assembly.components):
                    raise ValueError("Relative component placements are not supported by these controls.")
        except ValueError as exc:
            reason = str(exc)
    return AssemblyCapabilitiesResponse(message_id=request.message_id, session_id=session.id,
        revision=session.revision, ledger_id=ledger_id, version=request.version, owner_path=request.owner_path,
        operations=list(E2_OPERATIONS if request.version == 2 else OPERATIONS),
        can_initialize=reason is None and owner is not None and owner.assembly is None,
        can_edit=reason is None and owner is not None and owner.assembly is not None, reason=reason)


def compile_command(session, request: AssemblyCommand, *, new_definition: Ledger | None = None) -> list[PatchOp]:
    def reject(message: str) -> NoReturn:
        raise CadenzaError(ErrorCode.BAD_MESSAGE, message)
    availability = capabilities(session, AssemblyCapabilitiesRequest(message_id=request.message_id,
        version=2, ledger_id=request.ledger_id, owner_path=request.owner_path))
    if availability.reason:
        reject(availability.reason)
    owner = resolve_owner(session, request.ledger_id, request.owner_path)
    initial = owner.assembly
    accepted = {c.id: c for c in initial.components} if initial else {}
    components = dict(accepted)
    old_joints = {j.id: j for j in initial.joints} if initial else {}
    joint_removals = [op.joint_id for op in request.operations if isinstance(op, RemoveJoint)]
    if len(set(joint_removals)) != len(joint_removals) or any(jid not in old_joints for jid in joint_removals):
        reject("Choose each accepted attachment at most once for removal.")
    joints = [j for j in old_joints.values() if j.id not in joint_removals]
    placed = {op.component_id for op in request.operations if isinstance(op, SetInstancePlacement)}
    initialized = False
    writes = set()
    removed = {op.component_id for op in request.operations if isinstance(op, RemoveInstance)}
    patches: list[PatchOp] = []
    def patch(op, path, value=None):
        patches.append(PatchOp(op=op, ledger_id=request.ledger_id, path=path, value=value))
    def unique_name(name, excluding=None):
        if any(c.id != excluding and c.name.casefold() == name.casefold() for c in components.values()):
            reject("Use a distinct instance name within this assembly.")
    # Removals first allow an explicit atomic replacement, independent of input order.
    for jid in joint_removals:
        patch("remove", f"/assembly/joints/{jid}")
    for index, operation in enumerate(request.operations):
        if isinstance(operation, InitializeAssembly):
            if index != 0 or initial is not None or request.owner_path:
                reject("Initialize an empty primary once, at the start of the request.")
            patch("add", "/assembly", dict(parts=[], components=[], joints=[]))
            patch("replace", "/metadata/name", operation.name)
            initialized = True
            continue
        if initial is None and not initialized:
            reject("Start the assembly before adding its instances.")
        if isinstance(operation, RemoveJoint):
            continue
        if isinstance(operation, AddRigidJoint):
            targets = {operation.component_a, operation.component_b}
            if not targets <= accepted.keys() or targets & removed or targets & placed:
                reject("Attach accepted direct instances; reposition or remove them in a separate request.")
            joint = Joint.model_validate({**operation.model_dump(exclude={"op"}), "id": f"joint_{uuid4().hex}", "kind": "rigid"})
            joints.append(joint)
            patch("add", "/assembly/joints/-", joint.model_dump(mode="json"))
            continue
        if isinstance(operation, (AddInstance, CreateSubassembly)):
            if isinstance(operation, CreateSubassembly):
                if new_definition is None:
                    reject("Subassembly creation requires private definition preparation.")
                source_id, name = new_definition.project_id, operation.instance_name
            else:
                source_id, name = operation.source_ledger_id, operation.name
                source = session.stores.get(source_id)
                if source is None or source_id == request.ledger_id:
                    reject("Choose an existing source definition other than this owner.")
                validate_source(source.ledger)
            unique_name(name)
            component = Component.model_validate(dict(id=f"comp_{uuid4().hex}", name=name,
                sub_assembly_id=source_id, placement=operation.placement.model_dump()))
            components[component.id] = component
            patch("add", "/assembly/components/-", component.model_dump(mode="json"))
            continue
        component = accepted.get(operation.component_id)
        if component is None:
            reject("Choose an existing direct instance; nested paths and newly added IDs are not editable here.")
        assert component is not None
        if operation.component_id in removed and not isinstance(operation, RemoveInstance):
            reject("An instance cannot be edited and removed in the same request.")
        target = (component.id, operation.op)
        if not isinstance(operation, DuplicateInstance):
            if target in writes:
                reject("Conflicting changes to the same instance.")
            writes.add(target)
        path = f"/assembly/components/{component.id}"
        if isinstance(operation, (SetInstancePlacement, DuplicateInstance)):
            if component.placement.relative_to is not None:
                reject("Relative component placements are not supported by these controls.")
        if isinstance(operation, (SetInstancePlacement, RemoveInstance)):
            if any(component.id in (j.component_a, j.component_b) for j in joints):
                reject("This instance has an attachment. Explicit attachment editing is required first.")
        if isinstance(operation, DuplicateInstance):
            unique_name(operation.name)
            copy = component.model_copy(update=dict(id=f"comp_{uuid4().hex}", name=operation.name,
                placement=component.placement.model_validate(operation.placement.model_dump())))
            components[copy.id] = copy
            patch("add", "/assembly/components/-", copy.model_dump(mode="json"))
        elif isinstance(operation, SetInstancePlacement):
            value = {**operation.placement.model_dump(), "relative_to": None}
            if value == component.placement.model_dump() and not any(component.id in (old_joints[jid].component_a, old_joints[jid].component_b) for jid in joint_removals):
                reject("The instance placement has not changed.")
            patch("replace", path + "/placement", value)
        elif isinstance(operation, RenameInstance):
            unique_name(operation.name, component.id)
            if component.name == operation.name:
                reject("The instance name has not changed.")
            components[component.id] = component.model_copy(update={"name": operation.name})
            patch("replace", path + "/name", operation.name)
        elif isinstance(operation, RemoveInstance):
            components.pop(component.id)
            patch("remove", path)
    if not components:
        reject("Keep at least one assembly instance. Undo can restore an earlier empty project.")
    def signature(joint):
        result = joint.model_dump(mode="json", exclude={"id"})
        for target in ("target_a", "target_b"):
            normal = result[target]["normal"]
            if normal is not None:
                magnitude = hypot(*normal)
                result[target]["normal"] = [value / magnitude for value in normal]
        return result
    if joint_removals and any(signature(old_joints[jid]) == signature(j) for jid in joint_removals for j in joints):
        reject("The replacement attachment has not changed.")
    if initialized:
        # Publish the new container once. Parent-add followed by child appends
        # aliases the inserted value inside the existing patch normalizer.
        patches = [PatchOp(op="add", ledger_id=request.ledger_id, path="/assembly",
            value=dict(parts=[], components=[c.model_dump(mode="json") for c in components.values()], joints=[])),
            patches[1]]
    candidate = apply_patch(owner, patches).ledger
    ledgers = {**session.snapshot(), owner.project_id: candidate}
    if new_definition is not None:
        ledgers[new_definition.project_id] = new_definition
    try:
        assert candidate.assembly is not None
        validate_joints(candidate.assembly)
        for ledger in ledgers.values():
            validate_references(ledger.model_dump(mode="json"), ledgers)
            if ledger.assembly is not None:
                assembly_instances(ledgers, ledger.project_id)
    except ValueError as exc:
        reject(str(exc))
    return patches


def validate_source(source: Ledger) -> None:
    if source.assembly is None and not any(not f.suppressed and f.operation == "add"
            and f.kind != "profile" and (f.kind != "sketch" or f.parameters.height > 0) for f in source.features):
        raise CadenzaError(ErrorCode.BAD_MESSAGE, "Choose a nonempty source definition.")


def prepare_command(session, request: AssemblyCommand) -> tuple[dict[str, Ledger], list[PatchOp]]:
    creations = [op for op in request.operations if isinstance(op, CreateSubassembly)]
    definition = None
    if creations:
        allowed = len(request.operations) == 1 or (len(request.operations) == 2 and isinstance(request.operations[0], InitializeAssembly))
        if len(creations) != 1 or not allowed:
            raise CadenzaError(ErrorCode.BAD_MESSAGE, "Create one subassembly per request, optionally initializing the empty project first.")
        operation = creations[0]
        assemblies = [s.ledger for s in session.stores.values() if s.ledger.assembly is not None]
        names = [l.metadata.name.casefold() for l in assemblies]
        names += [op.name.casefold() for op in request.operations if isinstance(op, InitializeAssembly)]
        if len([l for l in assemblies if l.project_id != session.primary_ledger_id]) >= 8 or operation.name.casefold() in names:
            raise CadenzaError(ErrorCode.BAD_MESSAGE, "Use a distinct assembly definition name; at most eight subassembly definitions can be created.")
        children: list[Component] = []
        for child in operation.children:
            source = session.stores.get(child.source_ledger_id)
            if source is None:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Choose an accepted child source definition.")
            validate_source(source.ledger)
            if any(c.name.casefold() == child.name.casefold() for c in children):
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Use distinct child instance names.")
            children.append(Component.model_validate(dict(id=f"comp_{uuid4().hex}", name=child.name,
                sub_assembly_id=child.source_ledger_id, placement=child.placement.model_dump())))
        definition = LedgerStore.new(name=operation.name).ledger
        definition = definition.model_copy(update={"assembly": Assembly(components=children)})
    patches = compile_command(session, request, new_definition=definition)
    owner = session.stores[request.ledger_id].ledger
    updates = {owner.project_id: apply_patch(owner, patches).ledger}
    if definition is not None:
        updates[definition.project_id] = definition
    candidate = session.stage(updates)
    validate_command_delta(session.snapshot(), candidate, request)
    return candidate, patches


def validate_command_delta(before: dict[str, Ledger], after: dict[str, Ledger], request: AssemblyCommand) -> None:
    """Independent scope check on compiler output, before spending a kernel build."""
    from cadenza_backend.edit_preview import changes_between
    operations = request.operations
    initialized = any(isinstance(op, InitializeAssembly) for op in operations)
    new_definitions = after.keys() - before.keys()
    creations = [op for op in operations if isinstance(op, CreateSubassembly)]
    allowed = set()
    removals = set()
    for op in operations:
        if isinstance(op, RenameInstance):
            allowed.add(f"/assembly/components/{op.component_id}/name")
        elif isinstance(op, SetInstancePlacement):
            allowed.update(f"/assembly/components/{op.component_id}/placement/{key}" for key in ("origin", "rotation_deg"))
        elif isinstance(op, RemoveInstance):
            allowed.add(f"/assembly/components/{op.component_id}")
            removals.add(f"/assembly/components/{op.component_id}")
        elif isinstance(op, RemoveJoint):
            allowed.add(f"/assembly/joints/{op.joint_id}")
            removals.add(f"/assembly/joints/{op.joint_id}")
    if initialized:
        allowed.update(("/assembly", "/metadata/name"))
    owner_before, owner_after = before[request.ledger_id], after[request.ledger_id]
    old_components = {c.id for c in owner_before.assembly.components} if owner_before.assembly else set()
    old_joints = {j.id for j in owner_before.assembly.joints} if owner_before.assembly else set()
    assembly = owner_after.assembly
    invalid = assembly is None or bool(before.keys() - after.keys()) or len(new_definitions) != len(creations)
    if assembly is not None:
        new_components = {c.id for c in assembly.components} - old_components
        new_joints = {j.id for j in assembly.joints} - old_joints
        invalid |= len(new_components) != sum(isinstance(op, (AddInstance, DuplicateInstance, CreateSubassembly)) for op in operations)
        invalid |= len(new_joints) != sum(isinstance(op, AddRigidJoint) for op in operations)
        invalid |= initialized and bool(assembly.parts or assembly.joints)
        allowed.update(f"/assembly/components/{cid}" for cid in new_components)
        allowed.update(f"/assembly/joints/{jid}" for jid in new_joints)
    for lid in new_definitions:
        ledger = after[lid]
        invalid |= bool(ledger.features) or ledger.relationships != Ledger().relationships
        invalid |= ledger.assembly is None or bool(ledger.assembly.parts or ledger.assembly.joints)
    for change in changes_between(before, after):
        if change.ledger_id in new_definitions and change.path == "":
            continue
        if change.ledger_id != request.ledger_id or change.path not in allowed:
            invalid = True
        if change.path in removals and change.after is not None:
            invalid = True
    if invalid:
        raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "The assembly candidate changed data outside the command scope.")


async def handle_assembly_command(session, request: AssemblyCommand, emit, *, prompt="", assumptions=None):
    from cadenza_backend.edit_preview import pending, publish_preview
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed. Refresh before editing.")
    if session.draft or pending(session):
        raise CadenzaError(ErrorCode.BUSY, "Finish the draft or apply/cancel the preview first.")
    ledgers, patches = prepare_command(session, request)
    outcome = await build_design(ledgers, session.primary_ledger_id)
    if not outcome.ok or outcome.stub:
        raise CadenzaError(outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE, outcome.detail)
    await publish_preview(session, ledgers, outcome, request.message_id, emit,
        summary=f"Preview of {len(request.operations)} assembly operation(s) in {ledgers[request.ledger_id].metadata.name}. Review shared-definition and attachment changes below.",
        patch=[p.model_dump(mode="json") for p in patches], prompt=prompt, assumptions=assumptions,
        prompt_scope="assembly")
