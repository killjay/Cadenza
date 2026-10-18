"""Reference integrity checks for raw documents before recursive geometry resolution."""
from typing import Any


def validate_references(document: dict[str, Any], all_documents: dict[str, Any] | None = None) -> None:
    def features(items):
        by_id = {item["id"]: item for item in items}
        if len(by_id) != len(items):
            raise ValueError("Duplicate feature identity")
        dependencies = {}
        for index, item in enumerate(items):
            refs = []
            placement = item.get("placement") or {}
            targets = item.get("targets", [])
            parameters = item.get("parameters") or {}
            for ref in (placement.get("relative_to"), parameters.get("target_feature"), *(target.get("reference_feature") for target in targets)):
                if not ref:
                    continue
                if ref not in by_id:
                    raise ValueError(f"Feature {item['id']} references missing feature {ref}")
                if not item.get("suppressed") and by_id[ref].get("suppressed"):
                    raise ValueError(f"Live feature {item['id']} references suppressed feature {ref}")
                refs.append(ref)
            source = parameters.get("target_feature")
            if source and source not in {f["id"] for f in items[:index]}:
                raise ValueError(f"Feature {item['id']} must follow its source {source}")
            dependencies[item["id"]] = refs
        acyclic(dependencies)

    def acyclic(graph):
        active, visited = set(), set()
        def visit(node):
            if node in active:
                raise ValueError(f"Dependency cycle at {node}")
            if node in visited:
                return
            active.add(node)
            for ref in graph.get(node, []):
                visit(ref)
            active.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)

    documents = dict(all_documents or {})
    documents[document.get("project_id", "primary")] = document
    graph = {}
    for lid, raw in documents.items():
        doc = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw
        features(doc.get("features", []))
        assembly = doc.get("assembly") or {}
        parts = assembly.get("parts", [])
        components = assembly.get("components", [])
        joints = assembly.get("joints", [])
        for items in (parts, components, joints):
            if len({item["id"] for item in items}) != len(items):
                raise ValueError("Duplicate assembly entity identity")
        for part in parts:
            features(part.get("features", []))
        part_ids = {part["id"] for part in parts}
        component_ids = {component["id"] for component in components}
        graph[lid] = []
        for component in components:
            if component.get("part_id") and component["part_id"] not in part_ids:
                raise ValueError(f"Component {component['id']} references missing part {component['part_id']}")
            ref = component.get("sub_assembly_id")
            if ref:
                if all_documents is not None and ref not in documents:
                    raise ValueError(f"Component {component['id']} references missing ledger {ref}")
                graph[lid].append(ref)
        for joint in joints:
            if joint.get("component_a") not in component_ids or joint.get("component_b") not in component_ids:
                raise ValueError(f"Joint {joint['id']} references a missing component")
            if any(joint.get(side, {}).get("mode") == "planar_face" for side in ("target_a", "target_b")):
                from cadenza_contracts.ledger import Joint, PlanarTarget
                typed = Joint.model_validate(joint)
                for side in ("a", "b"):
                    target = getattr(typed, f"target_{side}")
                    assert isinstance(target, PlanarTarget)
                    component = next(c for c in components if c["id"] == getattr(typed, f"component_{side}"))
                    source_id = component.get("sub_assembly_id")
                    if source_id != target.reference.source_ledger_id:
                        raise ValueError(f"Attachment {typed.id} references a different source")
                    if source_id in documents:
                        source = documents[source_id]
                        source = source.model_dump(mode="json") if hasattr(source, "model_dump") else source
                        if source.get("assembly") is not None:
                            raise ValueError("Planar attachment endpoints must be source parts")

    acyclic(graph)
