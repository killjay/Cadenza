"""Compile bounded sketch intent through the same commands as direct editing."""
import math

from cadenza_contracts.parameters import evaluate, resolve_parameters
from cadenza_contracts.profiles import validate_profile_region
from cadenza_contracts.sketch_draft import DraftUpdate, SketchDraft
from cadenza_backend.agents.sketch_editor import SketchIntent, Remove, Centre, Margin, Dimension
from cadenza_backend.sketch_drafts import PendingDraft, update_candidate


def margin_operations(state: SketchDraft, entity_id: str, edge: str, distance: float, name: str) -> list[dict]:
    entities = state.solution.entities if state.solution else state.definition.entities
    outer = entities[0]
    opening = next(e for e in entities if e.id == entity_id)
    if outer.kind != "rectangle" or opening.operation != "subtract":
        raise ValueError("Centre margins require an opening in a rectangular outer profile")
    if name in state.relationships.parameters: raise ValueError("Choose a new margin driver name")
    if any(c.kind == "coincident" and entity_id in (c.entity_a, c.entity_b) for c in state.definition.constraints):
        raise ValueError("Remove the opening's shared-centre constraint explicitly before assigning a margin")
    ops: list[dict] = []
    names = {*state.relationships.parameters, name}

    def dimension(entity, parameter):
        found = [c for c in state.definition.constraints if c.kind == "dimension" and c.entity_id == entity and c.parameter == parameter]
        if len(found) > 1: raise ValueError(f"Remove redundant {parameter} dimensions first")
        return found[0] if found else None

    def driver(parameter):
        c = dimension(outer.id, parameter)
        if c is None: raise ValueError(f"Fix the outer {parameter} dimension first")
        binding = next((b for b in state.relationships.bindings if b.constraint_id == c.id and b.feature_id == state.profile_id), None)
        if binding:
            key = binding.expression.strip()
            value = state.relationships.parameters.get(key)
            if value is None or value.expression is not None or value.unit != "mm":
                raise ValueError(f"Outer {parameter} uses a derived expression; use direct binding controls")
            return key
        key, suffix = f"sketch_{parameter}", 2
        while key in names:
            key = f"sketch_{parameter}_{suffix}"
            suffix += 1
        names.add(key)
        ops.extend([{"op": "add_driver", "parameter": key, "value": c.value},
                    {"op": "bind_dimension", "constraint_id": c.id, "expression": key}])
        return key

    x, y = driver("x"), driver("y")
    horizontal = edge in ("left", "right")
    size = driver("width" if horizontal else "height")
    ops.append({"op": "add_driver", "parameter": name, "value": distance})
    for parameter, centre in (("x", x), ("y", y)):
        c = dimension(entity_id, parameter)
        if c and any(b.constraint_id == c.id and b.feature_id == state.profile_id for b in state.relationships.bindings):
            raise ValueError(f"Opening {parameter} is already bound; edit its existing driver")
        ref = c.id if c else {"constraint_alias": parameter}
        if not c: ops.append({"op": "add_dimension", "alias": parameter, "entity_id": entity_id,
                             "parameter": parameter, "value": getattr(opening, parameter)})
        sign = "-" if edge in ("left", "bottom") else "+"
        expression = f"{centre} {sign} ({size} / 2 - {name})" if horizontal == (parameter == "x") else centre
        ops.append({"op": "bind_dimension", "constraint_id": ref, "expression": expression})
    return ops


def compile_intent(draft: PendingDraft, intent: SketchIntent) -> tuple[SketchDraft, list[str]]:
    """No publication: every action operates on a private candidate."""
    before = draft.state
    working = PendingDraft(draft.base, before.model_copy(deep=True))
    aliases: dict[str, str] = {}
    descriptions: list[str] = []
    checks: list[tuple] = []
    commands: list[dict] = []
    removed: set[str] = set()

    def target(value):
        identity = aliases.get(value[1:]) if value.startswith("$") else value
        if identity is None or not any(e.id == identity for e in working.state.definition.entities):
            raise ValueError(f"Unknown sketch target: {value}; choose an existing entity or earlier creation")
        return identity

    def label(identity):
        index = next(i for i, e in enumerate(working.state.definition.entities) if e.id == identity)
        return "Outer" if index == 0 else f"Opening {index}"

    for action in intent.actions:
        state = working.state
        ops: list[dict] = []
        if action.kind == "create":
            if action.key in aliases: raise ValueError("Duplicate creation key")
            if bool(state.definition.entities) != (action.entity.operation == "subtract"):
                raise ValueError("Create one outer region first, then circular openings")
            ops.append({"op": "add_entity", "alias": "created", "entity": action.entity.model_dump()})
            variables = ("x", "y", "width", "height") if action.entity.kind == "rectangle" else ("x", "y", "diameter")
            ops.extend({"op": "add_dimension", "entity_id": {"entity_alias": "created"}, "parameter": p,
                        "value": getattr(action.entity, p)} for p in variables)
            descriptions.append(f"Create {action.entity.operation} {action.entity.kind}: " + ", ".join(f"{p}={getattr(action.entity, p):g} mm" for p in variables))
        elif action.kind == "extrusion":
            ops.append({"op": "set_extrusion", "amount": action.amount})
            checks.append(("extrusion", action.amount))
            descriptions.append(f"Set new extrusion amount to {action.amount:g} mm")
        elif action.kind == "driver":
            ops.append({"op": "set_driver", "parameter": action.parameter, "value": action.value})
            checks.append(("driver", action.parameter, action.value))
            descriptions.append(f"Set driver {action.parameter} to {action.value:g}; dependent dimensions may change")
        else:
            identity = target(action.entity)
            name = label(identity)
            constraints = state.definition.constraints
            if action.kind == "dimension":
                related = {identity}
                if action.parameter in ("x", "y"):
                    for _ in state.definition.entities:
                        for c in constraints:
                            if c.kind == "coincident" and related.intersection((c.entity_a, c.entity_b)):
                                related.update((c.entity_a, c.entity_b))
                dimensions = [c for c in constraints if c.kind == "dimension" and c.entity_id in related and c.parameter == action.parameter]
                if len(dimensions) > 1: raise ValueError("Choose a single driving constraint; repair redundant dimensions first")
                ops.append({"op": "set_dimension", "constraint_id": dimensions[0].id, "value": action.value} if dimensions else
                           {"op": "add_dimension", "entity_id": identity, "parameter": action.parameter, "value": action.value})
                checks.append(("dimension", identity, action.parameter, action.value))
                descriptions.append(f"Set {name} {action.parameter} to {action.value:g} mm")
            elif isinstance(action, (Remove, Centre)):
                if action.kind == "remove" and identity == state.definition.entities[0].id:
                    raise ValueError("Only unbound openings can be removed through sketch prompts")
                referencing = []
                for c in constraints:
                    if c.kind == "dimension":
                        if c.entity_id == identity and (action.kind == "remove" or c.parameter in ("x", "y")): referencing.append(c)
                    elif identity in (c.entity_a, c.entity_b): referencing.append(c)
                if any(b.constraint_id in {c.id for c in referencing} and b.feature_id == state.profile_id for b in state.relationships.bindings):
                    raise ValueError("This edit would detach a bound dimension; edit its driver or use direct controls")
                ops.extend({"op": "remove_constraint", "constraint_id": c.id} for c in referencing)
                if action.kind == "remove":
                    ops.append({"op": "remove_entity", "entity_id": identity})
                    removed.add(identity)
                    descriptions.append(f"Remove {name} and its {len(referencing)} referencing constraints")
                else:
                    other = target(action.other)
                    ops.append({"op": "add_centre", "entity_a": identity, "entity_b": other})
                    checks.append(("centre", identity, other))
                    descriptions.append(f"Centre {name} on {label(other)}; replace {len(referencing)} unbound location constraints")
            elif action.kind == "margin":
                ops = margin_operations(state, identity, action.edge, action.distance, action.driver_name)
                checks.append(("margin", identity, action.edge, action.distance, action.driver_name))
                descriptions.append(f"Keep {name} centre {action.distance:g} mm from the {action.edge} edge; centre the other coordinate")
        commands.extend(ops)
        if len(commands) > 32: raise ValueError("This request exceeds 32 sketch commands; split it into smaller edits")
        request = DraftUpdate.model_validate(dict(message_id="candidate", base_revision=state.base_revision,
            draft_id=state.id, draft_revision=state.revision, operations=ops))
        working.state = update_candidate(working, request)
        if action.kind == "create":
            created = working.state.definition.entities[-1]
            aliases[action.key] = created.id
            # Position may deliberately be established by a later centre/margin action.
            for p in (("width", "height") if created.kind == "rectangle" else ("diameter",)):
                checks.append(("dimension", created.id, p, getattr(action.entity, p)))
            repositioned = any(isinstance(a, (Centre, Margin, Dimension)) and a.entity == f"${action.key}"
                               and (a.kind != "dimension" or a.parameter in ("x", "y")) for a in intent.actions)
            if not repositioned:
                checks.extend(("dimension", created.id, p, getattr(action.entity, p)) for p in ("x", "y"))

    result = working.state
    if result.solution is None: raise ValueError("The proposed sketch is empty")
    validate_profile_region(result.solution)
    entities = {e.id: e for e in result.solution.entities}
    if len(entities) != len(before.definition.entities) + len(aliases) - len(removed):
        raise ValueError("Proposal failed entity-count verification")
    if removed.intersection(entities): raise ValueError("Proposal failed deletion verification")
    def same(a, b):
        if a is None or b is None or not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7):
            raise ValueError("Proposal does not satisfy all interpreted dimensions/positions")
    for check in checks:
        if check[0] == "dimension": same(getattr(entities[check[1]], check[2]), check[3])
        elif check[0] == "driver": same(result.relationships.parameters[check[1]].value, check[2])
        elif check[0] == "extrusion": same(result.extrusion_amount, check[1])
        elif check[0] == "centre":
            for p in ("x", "y"): same(getattr(entities[check[1]], p), getattr(entities[check[2]], p))
        elif check[0] == "margin":
            opening, outer = entities[check[1]], result.solution.entities[0]
            assert outer.width is not None and outer.height is not None
            edge, distance, driver_name = check[2:]
            measured = {"left": opening.x - (outer.x - outer.width / 2), "right": outer.x + outer.width / 2 - opening.x,
                        "top": outer.y + outer.height / 2 - opening.y, "bottom": opening.y - (outer.y - outer.height / 2)}[edge]
            same(measured, distance)
            same(opening.y if edge in ("left", "right") else opening.x, outer.y if edge in ("left", "right") else outer.x)
            same(result.relationships.parameters[driver_name].value, distance)
    # Existing seed definitions and binding formulas must survive unchanged. Only explicit
    # constraint edits or materialized named-driver values may change old constraints.
    for entity in before.definition.entities:
        if entity.id not in removed and entity not in result.definition.entities: raise ValueError("Unrelated entity definition changed")
    for binding in before.relationships.bindings:
        if binding not in result.relationships.bindings: raise ValueError("Existing relationship changed")
    allowed_drivers = {op["parameter"] for op in commands if op["op"] in ("set_driver", "add_driver")}
    for op in commands:
        if op["op"] == "set_dimension":
            binding = next((b for b in result.relationships.bindings if b.feature_id == result.profile_id
                            and b.constraint_id == op["constraint_id"]), None)
            if binding: allowed_drivers.add(binding.expression.strip())
    for name, parameter in before.relationships.parameters.items():
        if name not in allowed_drivers and result.relationships.parameters.get(name) != parameter:
            raise ValueError("Unrelated named parameter changed")
    if result.relationships.constraints != before.relationships.constraints:
        raise ValueError("Existing design rules changed")
    touched = {op.get("constraint_id") for op in commands if op["op"] in ("set_dimension", "remove_constraint")}
    quantities = resolve_parameters(result.relationships)
    for c in before.definition.constraints:
        if c.id in touched: continue
        expected = c.model_copy(deep=True)
        binding = next((b for b in result.relationships.bindings if b.feature_id == result.profile_id and b.constraint_id == c.id), None)
        if binding and expected.kind == "dimension": expected.value = evaluate(binding.expression, quantities.__getitem__)[0]
        if expected not in result.definition.constraints: raise ValueError("Unrelated constraint changed")
    result.revision = before.revision + 1
    if result.model_dump(exclude={"revision"}) == before.model_dump(exclude={"revision"}): raise ValueError("The proposal makes no changes")
    return result, descriptions
