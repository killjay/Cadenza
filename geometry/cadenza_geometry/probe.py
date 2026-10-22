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
    if qtype == "revolve_effects":
        return _revolve_effects(built, query.get("feature_ids", []))
    if qtype == "gear_effects":
        return _gear_effects(built, query.get("feature_ids", []))
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


def _gear_effects(built: Any, feature_ids: list[str]) -> dict:
    """Bounded gear/stock checks, including unchanged bores after parent edits."""
    from dataclasses import replace
    from build123d import Align, Cylinder, GeomType, Pos
    from cadenza_geometry.builder import build_solid
    from cadenza_geometry.contracts import GeometryError
    from cadenza_geometry.frames import pattern_locations, to_world
    from cadenza_geometry.plan import BuildPlan

    plan = built.plan
    if not isinstance(plan, BuildPlan) or len(feature_ids) != 1 or len(plan.features) > 32:
        return {"error": "Gear checks support one gear and at most 32 source features."}
    gear = plan.by_id(feature_ids[0])
    if gear is None or gear.gear is None or gear.relative_to or gear.rotation_deg[:2] != (0, 0):
        return {"error": "Select an absolute, source-Z spur gear."}
    g = gear.gear
    tol = 1e-5

    def volume(shape):
        if shape is None:
            return 0.0
        items = list(shape) if isinstance(shape, (list, tuple)) or type(shape).__name__ == "ShapeList" else [shape]
        return sum(s.volume for item in items for s in item.solids())

    def prefix(index):
        return build_solid(replace(plan, features=plan.features[:index], warnings=[]))

    def cylinders(shape, radius, x, y):
        result = []
        for face in shape.faces():
            if face.geom_type != GeomType.CYLINDER:
                continue
            axis = face.axis_of_rotation
            if axis is None or abs(abs(axis.direction.Z) - 1) > 1e-7:
                continue
            if math.hypot(axis.position.X - x, axis.position.Y - y) > tol:
                continue
            if face.radius is not None and abs(face.radius - radius) <= tol:
                result.append(face)
        return result

    try:
        if not built.solid.is_valid or len(built.solid.solids()) != 1:
            raise ValueError("Gear authoring requires one connected valid source solid.")
        tool = build_solid(replace(plan, features=[gear], warnings=[]))
        box = tool.bounding_box()
        height = gear.p["height"]
        x, y, z = gear.origin
        if not tool.is_valid or len(tool.solids()) != 1 or abs(box.min.Z-z) > tol or abs(box.max.Z-z-height) > tol:
            raise ValueError("Gear tool does not match its axial placement and width.")
        v = volume(tool)
        if not math.pi*g.root_radius**2*height < v < math.pi*g.tip_radius**2*height:
            raise ValueError("Gear volume falls outside its root/tip cylinders.")
        tips = cylinders(tool, g.tip_radius, x, y)
        roots = cylinders(tool, g.root_radius, x, y)
        final_tips = cylinders(built.solid, g.tip_radius, x, y)
        if len(tips) != g.teeth or len(roots) != g.teeth or len(final_tips) != g.teeth or not math.isclose(
            sum(f.area for f in tips), sum(f.area for f in final_tips), rel_tol=1e-6, abs_tol=1e-7):
            raise ValueError("The requested gear teeth did not survive the source feature chain.")
        index = plan.features.index(gear)
        effect = volume(prefix(index+1)) - (volume(prefix(index)) if index else 0)
        if effect <= max(1e-7, v*1e-9):
            raise ValueError("Gear has no measurable material effect.")
        bores = []
        for i, feature in enumerate(plan.features):
            if feature.id == gear.id:
                continue
            if feature.kind == "cylinder" and feature.operation == "add":
                if feature.is_rotated or feature.relative_to or math.hypot(feature.origin[0]-x, feature.origin[1]-y) > tol or feature.p["diameter"]/2 >= g.root_radius-tol:
                    raise ValueError("Gear hubs must be coaxial and inside the root body.")
                if volume(prefix(i+1))-(volume(prefix(i)) if i else 0) <= 1e-7:
                    raise ValueError("The hub has no measurable material effect.")
                continue
            if feature.kind in ("linear_pattern", "circular_pattern"):
                seed = plan.by_id(feature.p["target_feature"])
                if seed is None or seed.kind != "hole":
                    raise ValueError("Gear chains support interior hole patterns only.")
                for location in pattern_locations(feature, plan):
                    point = to_world(location, seed.origin)
                    if math.hypot(point[0]-x, point[1]-y)+seed.p["diameter"]/2 >= g.root_radius-tol:
                        raise ValueError("Keep patterned holes inside the gear root body.")
                continue
            if feature.kind != "hole" or feature.operation != "subtract" or feature.is_rotated or feature.relative_to:
                raise ValueError("Gear checks support coaxial hubs and vertical interior bores/patterns. Other chains are not yet supported.")
            hx, hy, hz = feature.origin
            radius = feature.p["diameter"]/2
            if math.hypot(hx-x, hy-y)+radius >= g.root_radius-tol or i == 0:
                raise ValueError("Keep the complete bore inside the gear root body.")
            stock = prefix(i)
            bounds = stock.bounding_box()
            if feature.through:
                low = bounds.min.Z-1
            else:
                assert feature.depth is not None
                low = hz-feature.depth
            high = bounds.max.Z+1 if feature.through else hz
            cutter = Pos(hx, hy, low) * Cylinder(radius, high-low, align=(Align.CENTER, Align.CENTER, Align.MIN))
            cut: Any = stock.intersect(cutter)
            pieces = list(cut) if isinstance(cut, (list, tuple)) or type(cut).__name__ == "ShapeList" else ([cut] if cut else [])
            if not pieces:
                raise ValueError("The bore has no material effect.")
            extent = Compound(pieces).bounding_box()
            depth = extent.max.Z-extent.min.Z
            expected = math.pi*radius**2*depth
            if depth <= tol or not math.isclose(volume(cut), expected, rel_tol=1e-6, abs_tol=1e-7):
                raise ValueError("The bore must cut one complete cylinder; overlapping bores or complex stock are unsupported.")
            if hz < extent.max.Z-tol:
                raise ValueError("The through-hole opening is below the new stock top. Move its opening above the intended top before changing gear width.")
            if not feature.through and abs(extent.min.Z-low) > tol:
                raise ValueError("The blind bore does not retain its requested depth.")
            if not feature.through:
                floors = [face for face in built.faces if face.geom_type == GeomType.PLANE
                    and abs(face.center().Z-low) <= tol and face.normal_at().Z > 1-1e-7
                    and math.hypot(face.center().X-hx, face.center().Y-hy) <= tol
                    and math.isclose(face.area, math.pi*radius**2, rel_tol=1e-6, abs_tol=1e-7)]
                if len(floors) != 1:
                    raise ValueError("The complete blind-hole floor did not survive.")
            if volume(built.solid.intersect(cutter)) > max(1e-7, expected*1e-9):
                raise ValueError("Material obstructs the intended bore; review its opening and downstream features.")
            faces = [f for f in cylinders(built.solid, radius, hx, hy)
                if abs(f.bounding_box().min.Z-extent.min.Z) <= tol and abs(f.bounding_box().max.Z-extent.max.Z) <= tol
                and math.isclose(f.area, 2*math.pi*radius*depth, rel_tol=1e-6, abs_tol=1e-7)
                and f.normal_at().X*(f.center().X-hx)+f.normal_at().Y*(f.center().Y-hy) < 0]
            if len(faces) != 1:
                raise ValueError("The complete, distinct bore did not survive the source chain.")
            bores.append(feature.id)
        warnings = ["Spur gear geometry uses radial root relief where needed; generated undercut and manufacturing tolerances are not modeled."]
        warnings.extend(plan.warnings)
        return {"gears": [dict(feature_id=gear.id, tooth_count=len(tips), tool_volume_mm3=v,
            material_effect_mm3=effect, complete_bores=bores)], "warnings": warnings}
    except (ValueError, GeometryError) as exc:
        return {"error": str(exc)}


def _revolve_effects(built: Any, feature_ids: list[str]) -> dict:
    """Measure bounded revolve tools independently and require a material effect."""
    from dataclasses import replace
    from cadenza_geometry.builder import build_solid
    from cadenza_geometry.contracts import GeometryError
    from cadenza_geometry.plan import BuildPlan
    from cadenza_contracts.revolve import RevolveSettings

    plan = built.plan
    if not isinstance(plan, BuildPlan) or not 1 <= len(feature_ids) <= 32 or len(set(feature_ids)) != len(feature_ids):
        return {"error": "Select at most 32 distinct revolves in a source part."}
    results = []
    try:
        if len(built.solid.solids()) != 1:
            raise ValueError("Revolve authoring requires one connected final source solid.")
        for fid in feature_ids:
            feature = plan.by_id(fid)
            if feature is None or feature.kind != "revolve" or feature.relative_to or tuple(feature.p["axis"]) != (0, 1, 0):
                raise ValueError("Revolve is missing or outside the supported subset.")
            settings = RevolveSettings(vertices=feature.p["vertices"], angle=feature.p["angle"])
            points = settings.vertices
            moment = sum((a[0]+b[0])*(a[0]*b[1]-b[0]*a[1]) for a, b in zip(points, points[1:]+points[:1]))
            expected = abs(moment) * math.pi / 3 * settings.angle / 360
            tool = build_solid(replace(plan, features=[replace(feature, operation="add")], warnings=[]))
            tool_volume = sum(s.volume for s in tool.solids())
            if not math.isclose(tool_volume, expected, rel_tol=1e-6, abs_tol=1e-7):
                raise ValueError("Revolve tool volume does not match the requested profile and angle.")
            index = plan.features.index(feature)
            prefix = plan.features[:index]
            has_stock = any(f.kind != "profile" and (f.kind != "sketch" or f.p["height"] > 0) for f in prefix)
            before = sum(s.volume for s in build_solid(replace(plan, features=prefix, warnings=[])).solids()) if has_stock else 0
            after = sum(s.volume for s in build_solid(replace(plan, features=plan.features[:index+1], warnings=[])).solids())
            delta = after-before if feature.operation == "add" else before-after
            if delta <= max(1e-7, expected*1e-9):
                raise ValueError("Revolve has no measurable material effect.")
            results.append(dict(feature_id=fid, tool_volume_mm3=tool_volume, material_effect_mm3=delta))
        return {"revolves": results}
    except (ValueError, GeometryError) as exc:
        return {"error": str(exc)}


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
