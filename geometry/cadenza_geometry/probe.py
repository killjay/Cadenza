import math
from typing import Any, Dict, List
from build123d import CenterOf, Compound, Face, Vector

def _get_direction_vector(direction: str) -> Vector:
    dir_map = {
        "+z": Vector(0, 0, 1), "top": Vector(0, 0, 1),
        "-z": Vector(0, 0, -1), "bottom": Vector(0, 0, -1),
        "+y": Vector(0, 1, 0), "back": Vector(0, 1, 0),
        "-y": Vector(0, -1, 0), "front": Vector(0, -1, 0),
        "+x": Vector(1, 0, 0), "right": Vector(1, 0, 0),
        "-x": Vector(-1, 0, 0), "left": Vector(-1, 0, 0),
    }
    return dir_map.get(direction.lower().strip(), None)

def _get_geom_type(face: Face) -> str:
    g = getattr(face, "geom_type", "")
    if callable(g):
        try:
            g = g()
        except Exception:
            pass
    return str(g).upper()

def _get_target_solid(built: Any, component_id: str | None) -> Any:
    if component_id is None:
        return built.solid
    leaf = built.instances[component_id]
    return leaf.location * leaf.solid


def _get_feature_faces(built: Any, feature_id: str | None, component_id: str | None = None) -> List[Face]:
    if feature_id is None:
        return list(_get_target_solid(built, component_id).faces())
    return [
        f for i, f in enumerate(built.faces) 
        if i in built.attribution and built.attribution[i][1].id == feature_id
        and built.attribution[i][0] == component_id
    ]

def _get_independent_shape(built: Any, fid: str | None, component_id: str | None = None) -> Any:
    from cadenza_geometry.frames import feature_location
    from cadenza_geometry.builder import _primitive, _cutter
    if fid is None:
        return _get_target_solid(built, component_id)
    leaf = built.instances[component_id] if component_id is not None else None
    plan = leaf.plan if leaf else built.plan
    solid = leaf.solid if leaf else built.solid
    f = next((f for f in plan.features if f.id == fid), None)
    if not f: return None
    loc = feature_location(f, plan)
    shape = loc * (_cutter(f, solid) if f.kind == "hole" else _primitive(f))
    return leaf.location * shape if leaf else shape


def _get_surviving_properties(shape: Any, solid: Any) -> Any:
    try:
        surviving = shape.intersect(solid)
    except Exception:
        return None
    if not surviving:
        return None
        
    try:
        from build123d import Compound
        if isinstance(surviving, (list, tuple)) or type(surviving).__name__ == 'ShapeList':
            if len(surviving) == 0: return None
            comp = Compound(list(surviving))
        else:
            comp = surviving
            
        c = comp.center(CenterOf.MASS)
        return {
            "volume_mm3": comp.volume,
            "center_of_mass": [c.X, c.Y, c.Z],
            "surface_area_mm2": comp.area
        }
    except Exception:
        return None


def _find_face_by_direction(faces: List[Face], direction: str) -> Face | None:
    vec = _get_direction_vector(direction)
    if not vec:
        # If it's something like "cylindrical", try to find a cylinder
        if direction.lower() == "cylindrical":
            cyls = [f for f in faces if "CYLINDER" in _get_geom_type(f)]
            return cyls[0] if cyls else None
        return None
        
    best_face = None
    best_dot = -2.0
    for f in faces:
        try:
            n = f.normal_at(f.center())
            dot = n.X * vec.X + n.Y * vec.Y + n.Z * vec.Z
            if dot > best_dot:
                best_dot = dot
                best_face = f
        except Exception:
            continue
            
    if best_dot > 0.5: # Needs to be at least somewhat pointing in that direction
        return best_face
    return None

def run_probe_query(built: Any, query: dict) -> dict:
    qtype = query.get("query_type")
    if qtype == "pattern_effects":
        return _pattern_effects(built, query.get("feature_ids", []))
    cid1, cid2 = query.get("component_1_id"), query.get("component_2_id")
    pair_query = qtype in {"measure_distance", "get_minimum_clearance", "check_collision"}
    if not pair_query and (cid2 is not None or query.get("feature_2_id") is not None):
        return {"error": "Target 2 is only supported for distance, clearance and collision queries."}
    for index, cid in ((1, cid1), (2, cid2)):
        fid = query.get(f"feature_{index}_id")
        if fid == "":
            return {"error": f"feature_{index}_id cannot be empty."}
        if cid is not None and cid not in built.instances:
            return {"error": f"Unknown leaf instance path for component_{index}_id: '{cid}'."}
        if not hasattr(built.plan, "features") and fid is not None and cid is None:
            return {"error": f"component_{index}_id is required for an assembly feature measurement. Use a full leaf path, or select the source part ledger_id for local coordinates."}
    
    if qtype == "get_bounding_box":
        fid = query.get("feature_1_id")
        bb_final = built.solid.bounding_box()
        final_bb = {"min": [bb_final.min.X, bb_final.min.Y, bb_final.min.Z], "max": [bb_final.max.X, bb_final.max.Y, bb_final.max.Z]}
        
        if not fid:
            if cid1 is not None:
                bb = _get_target_solid(built, cid1).bounding_box()
                return {"final_component": {"min": [bb.min.X, bb.min.Y, bb.min.Z], "max": [bb.max.X, bb.max.Y, bb.max.Z]},
                        "final_assembly": final_bb}
            return {"final_assembly": final_bb}
        
        shape = _get_independent_shape(built, fid, cid1)
        if not shape:
            return {"error": f"Feature '{fid}' not found."}
        bb = shape.bounding_box()
        return {
            "independent_feature": {"min": [bb.min.X, bb.min.Y, bb.min.Z], "max": [bb.max.X, bb.max.Y, bb.max.Z]},
            "final_assembly": final_bb
        }
        
    elif qtype == "get_face_center":
        fid = query.get("feature_1_id")
        direction = query.get("direction")
        if not fid or not direction:
            return {"error": "feature_1_id and direction are required for get_face_center"}
            
        face = _find_face_by_direction(_get_feature_faces(built, fid, cid1), direction)
        if face is None:
            return {"error": f"Face '{direction}' not found for '{fid}'."}
        center = face.center()
        return {"surviving_face_in_assembly": {"center": [center.X, center.Y, center.Z]}}

    elif qtype == "get_mass_properties":
        fid = query.get("feature_1_id")
        
        c_final = built.solid.center(CenterOf.MASS)
        final_props = {
            "volume_mm3": sum(s.volume for s in built.solid.solids()),
            "center_of_mass": [c_final.X, c_final.Y, c_final.Z],
            "surface_area_mm2": built.solid.area
        }
        
        if not fid:
            if cid1 is not None:
                solid = _get_target_solid(built, cid1)
                c = solid.center(CenterOf.MASS)
                return {"final_component": {"volume_mm3": sum(s.volume for s in solid.solids()),
                        "center_of_mass": [c.X, c.Y, c.Z], "surface_area_mm2": solid.area},
                        "final_assembly": final_props}
            return final_props
            
        shape = _get_independent_shape(built, fid, cid1)
        if not shape:
            return {"error": f"Feature '{fid}' not found."}
        
        c = shape.center(CenterOf.MASS)
        surviving_props = _get_surviving_properties(shape, _get_target_solid(built, cid1))
        
        return {
            "independent_feature": {
                "surface_area_mm2": shape.area,
                "center_of_mass": [c.X, c.Y, c.Z],
                "volume_mm3": shape.volume
            },
            "surviving_feature": surviving_props,
            "final_assembly": final_props,
            "note": "Properties reflect the independent feature solid, its surviving volume in the final part (which will be null for subtractive features like holes), and the total final assembled part."
        }
        
    elif qtype == "get_face_properties":
        fid = query.get("feature_1_id")
        direction = query.get("direction")
        if not fid or not direction:
            return {"error": "feature_1_id and direction are required."}
            
        faces = _get_feature_faces(built, fid, cid1)
        face = _find_face_by_direction(faces, direction)
        if not face:
            return {"error": f"Face '{direction}' not found for '{fid}'."}
            
        geom = _get_geom_type(face)
        res = {"type": geom, "area": face.area}
        if "PLANE" in geom:
            n = face.normal_at(face.center())
            res["normal"] = [n.X, n.Y, n.Z]
        elif "CYLINDER" in geom:
            # face.radius works on cylinders in build123d
            try:
                from OCP.BRepAdaptor import BRepAdaptor_Surface
                res["radius"] = BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
            except Exception:
                res["radius"] = None
                
        shape = _get_independent_shape(built, fid, cid1)
        indep_bb = shape.bounding_box() if shape else None
                
        return {
            "surviving_face_in_assembly": res,
            "independent_feature": {
                "bounding_box": {"min": [indep_bb.min.X, indep_bb.min.Y, indep_bb.min.Z], "max": [indep_bb.max.X, indep_bb.max.Y, indep_bb.max.Z]} if indep_bb else None
            }
        }
        
    elif qtype == "get_edge_length":
        fid = query.get("feature_1_id")
        idx = query.get("edge_index")
        if not fid or idx is None:
            return {"error": "feature_1_id and edge_index are required."}
            
        faces = _get_feature_faces(built, fid, cid1)
        if not faces:
            return {"error": f"Feature '{fid}' not found."}
            
        comp = Compound(faces)
        edges = comp.edges()
        if idx < 0 or idx >= len(edges):
            return {"error": f"Edge index {idx} out of range (0-{len(edges)-1})."}
            
        return {"length": edges[idx].length}
        
    elif qtype == "measure_distance":
        fid1 = query.get("feature_1_id")
        fid2 = query.get("feature_2_id")
        if not (fid1 or cid1) or not (fid2 or cid2):
            return {"error": "Both targets require a feature ID or component path."}
            
        faces1 = _get_feature_faces(built, fid1, cid1)
        faces2 = _get_feature_faces(built, fid2, cid2)
        if not faces1 or not faces2:
            return {"error": "One or both features not found."}
            
        c1 = Compound(faces1)
        c2 = Compound(faces2)
        
        # BRepExtrema_DistShapeShape
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        calc = BRepExtrema_DistShapeShape(c1.wrapped, c2.wrapped)
        calc.Perform()
        if not calc.IsDone() or calc.NbSolution() < 1:
            return {"error": "Distance calculation failed."}
        return {"distance_mm": calc.Value()}
        
    elif qtype == "check_collision":
        fid1 = query.get("feature_1_id")
        fid2 = query.get("feature_2_id")
        if not (fid1 or cid1) or not (fid2 or cid2):
            return {"error": "Both targets require a feature ID or component path."}
            
        s1 = _get_independent_shape(built, fid1, cid1)
        s2 = _get_independent_shape(built, fid2, cid2)
        if not s1 or not s2:
            return {"error": "One or both features not found in plan."}
            
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        calc = BRepExtrema_DistShapeShape(s1.wrapped, s2.wrapped)
        calc.Perform()
        if not calc.IsDone() or calc.NbSolution() < 1:
            return {"error": "Collision check failed."}
            
        dist = calc.Value()
        intersection = s1.intersect(s2)
        shapes = [] if intersection is None else (
            list(intersection) if isinstance(intersection, (list, tuple)) or type(intersection).__name__ == "ShapeList"
            else [intersection]
        )
        volume = sum(solid.volume for shape in shapes for solid in shape.solids())
        return {"collides": volume > 1e-7, "touching": dist < 1e-5 and volume <= 1e-7,
                "distance_mm": dist, "intersection_volume_mm3": volume}
        
    elif qtype == "get_cylinder_axis":
        fid = query.get("feature_1_id")
        if not fid:
            return {"error": "feature_id required for get_cylinder_axis."}
            
        faces = _get_feature_faces(built, fid, cid1)
        if not faces:
            return {"error": f"Feature '{fid}' not found."}
            
        for f in faces:
            if "CYLINDER" in _get_geom_type(f):
                from OCP.BRepAdaptor import BRepAdaptor_Surface
                cyl = BRepAdaptor_Surface(f.wrapped).Cylinder()
                loc = cyl.Location()
                dir = cyl.Axis().Direction()
                return {
                    "origin": {"x": loc.X(), "y": loc.Y(), "z": loc.Z()},
                    "direction": {"x": dir.X(), "y": dir.Y(), "z": dir.Z()}
                }
        return {"error": f"No cylindrical face found on feature '{fid}'."}
        
    elif qtype == "get_minimum_clearance":
        fid1 = query.get("feature_1_id")
        fid2 = query.get("feature_2_id")
        if not (fid1 or cid1) or not (fid2 or cid2):
            return {"error": "Both targets require a feature ID or component path."}
            
        faces1 = _get_feature_faces(built, fid1, cid1)
        faces2 = _get_feature_faces(built, fid2, cid2)
        if not faces1 or not faces2:
            return {"error": "One or both features not found."}
            
        c1 = Compound(faces1)
        c2 = Compound(faces2)
        
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        calc = BRepExtrema_DistShapeShape(c1.wrapped, c2.wrapped)
        calc.Perform()
        if not calc.IsDone() or calc.NbSolution() < 1:
            return {"error": "Distance calculation failed."}
        return {"clearance": calc.Value()}
        
    return {"error": f"Unknown query type: {qtype}"}


def _pattern_effects(built: Any, feature_ids: list[str]) -> dict:
    """Private, bounded verification of C1 copies against prefix and final solids.

    This runs only in the geometry worker. Prefixes are temporary solids, never
    another project acceptance build. Complex/split bores fail closed: this
    verifier promises complete cylinders, not merely intersecting cutter axes.
    """
    from dataclasses import replace
    from time import perf_counter
    from build123d import GeomType, Location
    from cadenza_contracts.patterns import MAX_PATTERN_COPIES, MAX_PATTERN_COUNT
    from cadenza_geometry.builder import build_solid, _cutter, _primitive
    from cadenza_geometry.frames import feature_location, pattern_locations, to_world
    from cadenza_geometry.plan import BuildPlan

    start = perf_counter()
    plan = built.plan
    if not isinstance(plan, BuildPlan) or not feature_ids or len(set(feature_ids)) != len(feature_ids):
        return {"error": "Pattern effects require distinct pattern IDs in a source part."}
    selected = [f for f in plan.features if f.id in feature_ids]
    if len(selected) != len(feature_ids) or any(f.kind not in ("linear_pattern", "circular_pattern") for f in selected):
        return {"error": "Requested pattern is missing or unsupported."}
    if any(not 2 <= f.p["count"] <= MAX_PATTERN_COUNT for f in selected) or sum(f.p["count"] - 1 for f in selected) > MAX_PATTERN_COPIES:
        return {"error": "Pattern verification exceeds the source copy budget."}
    results = []
    # OCC's linear tolerance is ~1e-7 mm. Relative tolerance scales with the
    # feature/effect, not the entire stock volume (which could hide small holes).
    linear_tol = 1e-5

    def volume(shape):
        if shape is None:
            return 0.0
        shapes = list(shape) if isinstance(shape, (list, tuple)) or type(shape).__name__ == "ShapeList" else [shape]
        return sum(s.volume for item in shapes for s in item.solids())

    def prefix(index):
        return build_solid(replace(plan, features=plan.features[:index], warnings=[]))

    def bore_extent(stock, tool, seed, position):
        cut = stock.intersect(tool)
        pieces = list(cut) if isinstance(cut, (list, tuple)) or type(cut).__name__ == "ShapeList" else ([cut] if cut else [])
        if not pieces:
            raise ValueError("hole is outside stock or fully redundant")
        box = Compound(pieces).bounding_box()
        height, radius = box.max.Z - box.min.Z, seed.p["diameter"] / 2
        expected = math.pi * radius ** 2 * height
        if height <= linear_tol or not math.isclose(volume(cut), expected, rel_tol=1e-6, abs_tol=1e-7):
            raise ValueError("hole does not cut one complete cylindrical interval; overlap or complex stock is unsupported")
        if not seed.through and abs(box.min.Z - (position[2] - seed.depth)) > linear_tol:
            raise ValueError("blind hole does not retain the requested depth and floor")
        # Through holes must start at/above the stock and reach its underside.
        # A full interval ending at the cutter top inside stock is a blind cut.
        if seed.through and box.max.Z > position[2] + linear_tol:
            raise ValueError("through-hole origin must be at or above the stock opening")
        return box.min.Z, box.max.Z

    try:
        for pattern in selected:
            index = plan.features.index(pattern)
            seed = plan.by_id(pattern.p["target_feature"])
            if seed is None or seed.kind not in ("box", "cylinder", "hole") or seed.is_rotated or seed.relative_to:
                raise ValueError(f"{pattern.id}: unsupported seed")
            current = prefix(index)
            seed_stock = prefix(plan.features.index(seed)) if seed.kind == "hole" else None
            if seed.kind == "hole":
                assert seed_stock is not None
                local_tool = _cutter(seed, seed_stock)
            else:
                local_tool = _primitive(seed)
            tool = feature_location(seed, plan) * local_tool
            transforms = [Location(), *pattern_locations(pattern, plan)]
            placements = [to_world(t, seed.origin) for t in transforms]
            effects, extents = [], []
            if seed.kind == "hole":
                extents.append(bore_extent(seed_stock, tool, seed, placements[0]))
            for i, transform in enumerate(transforms[1:], 1):
                copy = transform * tool
                if seed.kind == "hole":
                    extents.append(bore_extent(current, copy, seed, placements[i]))
                next_solid = current + copy if pattern.operation == "add" else current - copy
                effect = abs(volume(next_solid) - volume(current))
                if effect <= max(1e-7, volume(copy) * 1e-9):
                    raise ValueError(f"{pattern.id} copy {i + 1}: no distinct material effect")
                effects.append(effect)
                current = next_solid
            if seed.kind == "hole":
                radius = seed.p["diameter"] / 2
                matched = set()
                final_tool = feature_location(seed, plan) * _cutter(seed, built.solid)
                for i, (position, extent) in enumerate(zip(placements, extents)):
                    if volume(built.solid.intersect(transforms[i] * final_tool)) > max(1e-7, math.pi * radius ** 2 * (extent[1] - extent[0]) * 1e-9):
                        raise ValueError(f"{pattern.id} instance {i + 1}: later material obstructs the intended hole")
                    if not seed.through:
                        floors = [face for face in built.faces if face.geom_type == GeomType.PLANE
                            and abs(face.center().Z - extent[0]) <= linear_tol
                            and math.hypot(face.center().X - position[0], face.center().Y - position[1]) <= linear_tol
                            and face.normal_at().Z > 1 - 1e-7
                            and math.isclose(face.area, math.pi * radius ** 2, rel_tol=1e-6, abs_tol=1e-7)]
                        if len(floors) != 1:
                            raise ValueError(f"{pattern.id} instance {i + 1}: complete blind-hole floor did not survive")
                    candidates = []
                    for face_index, face in enumerate(built.faces):
                        if face.geom_type != GeomType.CYLINDER:
                            continue
                        axis = face.axis_of_rotation
                        if axis is None or abs(abs(axis.direction.Z) - 1) > 1e-7:
                            continue
                        from OCP.BRepAdaptor import BRepAdaptor_Surface
                        face_radius = BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()
                        if math.hypot(axis.position.X - position[0], axis.position.Y - position[1]) > linear_tol or abs(face_radius - radius) > linear_tol:
                            continue
                        box = face.bounding_box()
                        normal, center = face.normal_at(), face.center()
                        inward = normal.X * (center.X - position[0]) + normal.Y * (center.Y - position[1]) < 0
                        if inward and abs(box.min.Z - extent[0]) <= linear_tol and abs(box.max.Z - extent[1]) <= linear_tol and math.isclose(face.area, 2 * math.pi * radius * (extent[1] - extent[0]), rel_tol=1e-6, abs_tol=1e-7):
                            candidates.append(face_index)
                    if len(candidates) != 1 or candidates[0] in matched:
                        raise ValueError(f"{pattern.id} instance {i + 1}: complete unique bore with intended extent did not survive")
                    matched.add(candidates[0])
            results.append({"feature_id": pattern.id, "requested_total": pattern.p["count"],
                            "expected_positions": placements, "copy_effects_mm3": effects,
                            "measured_complete_bores": len(extents) if seed.kind == "hole" else None})
    except ValueError as exc:
        return {"error": str(exc)}
    return {"patterns": results, "elapsed_ms": (perf_counter() - start) * 1000}
