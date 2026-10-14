"""Stage 2: BuildPlan -> exact B-rep solid.

Uses build123d's algebra API (`+` / `-`) rather than the `BuildPart` context the
blueprint sketches. The blueprint's §4 snippet selects the face to cut with
`faces().sort_by(Axis.Z)[-1]` -- a topological selector, and exactly the thing
§7 says we do not do. Every feature here is placed by explicit coordinate, so
nothing depends on face ordering and the build is reproducible under edits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from build123d import (
    Align,
    Box,
    Cylinder,
    Edge,
    Face,
    Location,
    Pos,
    Shape,
    Wire,
    extrude,
    Compound,
    Vector,
    Axis,
    Polygon,
    Rectangle,
    Circle,
    RegularPolygon,
    Cone,
    Sphere,
    Torus,
    offset,
    revolve,
    loft,
    Curve,
    Line,
    ThreePointArc,
    make_face,
    fillet,
    chamfer,
    Rotation,
)
from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.frames import feature_location, pattern_locations
from cadenza_geometry.joints import solve_rigid_joints
from cadenza_geometry.gear import Arc, Line, Spline, gear_profile
from cadenza_geometry.plan import BuildPlan, RFeature, AssemblyPlan, RPart, RComponent, RJoint

# Overshoot for cutters, so a coplanar boolean never has to be decided exactly.
EPS = 0.01
_BASE_ALIGN = (Align.CENTER, Align.CENTER, Align.MIN)


def _gear(f: RFeature) -> Shape:
    """A spur gear: the involute outline from `gear.py`, extruded.

    Which is the whole trick — a gear is not a primitive the kernel knows, it is
    a closed 2D wire that gets swept. FreeCAD's gear workbenches do exactly this;
    all the gear-ness is in the profile, and the solid step is the same extrude
    a sketched pocket would get.

    Each flank becomes ONE spline edge rather than a run of short segments. That
    is not only for the face count: `provenance.py` attributes faces by matching
    analytic surfaces, and a flank split into twenty facets would be twenty
    faces the attributor has to recognise individually.
    """
    assert f.gear is not None  # plan.py resolves this for every gear
    edges: list[Edge] = []
    for seg in gear_profile(f.gear):
        if isinstance(seg, Spline):
            edges.append(Edge.make_spline([(x, y, 0.0) for x, y in seg.points]))
        elif isinstance(seg, Line):
            edges.append(Edge.make_line((*seg.start, 0.0), (*seg.end, 0.0)))
        else:
            # Three-point arc: OCC needs an interior point to fix the sense, and
            # a start/end pair alone leaves the major/minor choice ambiguous.
            mid = seg.midpoint
            edges.append(
                Edge.make_three_point_arc(
                    (seg.radius * math.cos(seg.start), seg.radius * math.sin(seg.start), 0.0),
                    (mid[0], mid[1], 0.0),
                    (seg.radius * math.cos(seg.end), seg.radius * math.sin(seg.end), 0.0),
                )
            )

    wire = Wire(edges)
    if not wire.is_closed:
        raise GeometryError(
            GeometryErrorCode.KERNEL_FAILURE,
            f"the gear outline for {f.id!r} did not close",
            f.id,
            detail="the profile segments did not join end-to-end",
        )
    return extrude(Face(wire), amount=f.p["height"])


def _parse_align(align_val: Any) -> tuple[Align, Align, Align]:
    if not align_val:
        return _BASE_ALIGN
    if isinstance(align_val, str):
        align_val = [align_val] * 3
    mapping = {"min": Align.MIN, "center": Align.CENTER, "max": Align.MAX}
    a0, a1, a2 = (mapping.get(str(a).lower(), Align.MIN) for a in align_val)
    return (a0, a1, a2)

def _primitive(f: RFeature, current: Shape | None = None, built_shapes: dict[str, Shape] | None = None) -> Shape:
    align = _parse_align(f.p.get("align"))
    if f.kind == "box":
        return Box(f.p["length"], f.p["width"], f.p["height"], align=align)
    if f.kind == "cylinder":
        return Cylinder(radius=f.p["diameter"] / 2, height=f.p["height"], align=align)
    if f.kind == "cone":
        return Cone(bottom_radius=f.p["bottom_diameter"]/2, top_radius=f.p["top_diameter"]/2, height=f.p["height"], align=align)
    if f.kind == "sphere":
        return Sphere(radius=f.p["diameter"] / 2, align=align)
    if f.kind == "torus":
        return Torus(major_radius=f.p["major_diameter"] / 2, minor_radius=f.p["minor_diameter"] / 2, align=align)
    if f.kind == "fastener":
        d = f.p["diameter"]
        l = f.p["length"]
        head = f.p.get("head_type", "socket")
        shank = Cylinder(radius=d/2, height=l, align=(Align.CENTER, Align.CENTER, Align.MAX))
        if head == "socket":
            hd, hh = d * 1.5, d
            head_cyl = Cylinder(radius=hd/2, height=hh, align=(Align.CENTER, Align.CENTER, Align.MIN))
            hx = make_face(RegularPolygon(radius=d/2.5, side_count=6))
            sock = extrude(hx, amount=hh*0.6)
            sock = Pos(0, 0, hh*0.4) * sock
            head_shape = head_cyl - sock
        elif head == "hex":
            hd, hh = d * 1.5, d * 0.7
            hx = make_face(RegularPolygon(radius=hd/2, side_count=6))
            head_shape = extrude(hx, amount=hh)
        elif head == "flat":
            hd, hh = d * 2, d / 2
            head_shape = Cone(bottom_radius=d/2, top_radius=hd/2, height=hh, align=(Align.CENTER, Align.CENTER, Align.MIN))
        else: # button
            hd, hh = d * 1.9, d * 0.5
            head_shape = Cylinder(radius=hd/2, height=hh, align=(Align.CENTER, Align.CENTER, Align.MIN))
        return head_shape + shank
    if f.kind == "gear":
        return _gear(f)
    if f.kind == "edge_fillet":
        r, l = f.p["radius"], f.p["length"]
        # Solid box (R,R,L) anchored at (0,0,0) going +X, +Y, and symmetric Z
        b = Box(r, r, l, align=(Align.MIN, Align.MIN, Align.CENTER))
        c = Pos(r, r, 0) * Cylinder(radius=r, height=l, align=(Align.MIN, Align.MIN, Align.CENTER))
        return b - c
    if f.kind == "edge_chamfer":
        w, l = f.p["width"], f.p["length"]
        poly = Polygon([(0, 0), (w, 0), (0, w)])
        # Extrude pushes +Z. Center it.
        return Pos(0, 0, -l / 2) * extrude(poly, amount=l)
    if f.kind == "profile":
        profile = None
        for entity in f.p["entities"]:
            region = Rectangle(entity["width"], entity["height"]) if entity["kind"] == "rectangle" else Circle(entity["diameter"] / 2)
            region = Pos(entity["x"], entity["y"], 0) * region
            profile = region if profile is None else profile - region
        return profile
    if f.kind == "sketch":
        edges = f.p.get("edges", [])
        if edges:
            curves = []
            for e in edges:
                if e["type"] == "line":
                    import build123d
                    curves.append(build123d.Line(tuple(e["p1"]), tuple(e["p2"])))
                elif e["type"] == "arc":
                    import build123d
                    curves.append(build123d.ThreePointArc(tuple(e["p1"]), tuple(e["p2"]), tuple(e["p3"])))
            poly = make_face(Curve() + curves)
        else:
            verts = f.p["vertices"]
            verts = [tuple(v) for v in verts]
            if verts and verts[0] != verts[-1]:
                verts.append(verts[0])
            poly = Polygon(*verts)
        draft = f.p.get("draft_angle", 0.0)
        height = f.p.get("height", 0.0)
        if height <= 0:
            return poly
        elif draft == 0.0:
            return extrude(poly, amount=height)
        else:
            return extrude(poly, amount=height, taper=draft)
    if f.kind == "loft":
        sections = f.p.get("sections", [])
        faces = []
        for s in sections:
            # build the face for this section
            edges = s.get("edges", [])
            verts = s.get("vertices", [])
            if edges:
                curves = []
                for e in edges:
                    if e.get("type") == "line":
                        curves.append(Line(tuple(e["p1"]), tuple(e["p2"])))
                    elif e.get("type") == "arc":
                        curves.append(ThreePointArc(tuple(e["p1"]), tuple(e["p2"]), tuple(e["p3"])))
                face = make_face(Curve() + curves)
            else:
                verts = [tuple(v) for v in verts]
                if verts and verts[0] != verts[-1]:
                    verts.append(verts[0])
                face = make_face(Polygon(*verts))
            # transform the face
            loc_dict = s.get("placement", {})
            o = loc_dict.get("origin", [0, 0, 0])
            rot = loc_dict.get("rotation", [0, 0, 0])
            loc = Location(tuple(o), tuple(rot))
            faces.append(loc * face)
        return loft(faces)
    if f.kind == "revolve":
        verts = f.p["vertices"]
        verts = [tuple(v) for v in verts]
        if verts and verts[0] != verts[-1]:
            verts.append(verts[0])
        poly = make_face(Polygon(*verts))
        axis_dir = f.p.get("axis", (0, 1, 0))
        axis = Axis((0, 0, 0), tuple(axis_dir))
        return revolve(poly, axis=axis, revolution_arc=f.p["angle"])
    if f.kind == "extrude":
        target_id = f.p["target_feature"]
        if built_shapes is None or target_id not in built_shapes:
            raise GeometryError(GeometryErrorCode.INVALID_PARAMETER, f"extrude target_feature {target_id!r} not found", f.id)
        target = built_shapes[target_id]
        if target.solids():
            raise GeometryError(GeometryErrorCode.INVALID_PARAMETER, f"extrude target_feature {target_id!r} is not a 2D profile", f.id)
        # If it's a list (e.g. from sketch with multiple disconnected profiles), compound it
        if isinstance(target, list):
            target = Compound(target)
        amount = f.p["amount"]
        taper = f.p.get("taper", 0.0)
        if f.p.get("profile_normal"):
            return extrude(target, amount=amount, dir=target.faces()[0].normal_at(), taper=taper)
        return extrude(target, amount=amount, taper=taper)
    raise GeometryError(
        GeometryErrorCode.UNSUPPORTED_FEATURE, f"no primitive for kind {f.kind!r}", f.id
    )


def _cutter(f: RFeature, current: Shape) -> Shape:
    """The tool for a hole: a cylinder running along -Z of the feature frame.

    A through hole is sized from the current solid's bounding-box diagonal, so
    it exits no matter how the part is oriented or how the ledger is edited
    later. Sizing it from a fixed number is how "through" holes quietly become
    blind ones after someone doubles a dimension.
    """
    r = f.p["diameter"] / 2
    if f.through:
        bb = current.bounding_box()
        span = bb.diagonal + 2 * EPS
    else:
        span = (f.depth or 0.0) + EPS
    # align=MAX puts the cutter's top at local z=0; lift by EPS so it starts
    # just proud of the face it is cutting from. The floor of a blind hole
    # therefore lands exactly at z = -depth, which is what `hole_bottom`
    # declares in provenance.py.
    tool = Cylinder(radius=r, height=span, align=(Align.CENTER, Align.CENTER, Align.MAX))
    return Pos(0, 0, EPS) * tool


def build_solid(plan: BuildPlan) -> Shape:
    """BuildPlan -> a single valid solid, or raise GeometryError."""
    solid: Shape | None = None

    built_shapes: dict[str, Shape] = {}

    for f in plan.features:
        loc: Location = feature_location(f, plan)
        try:
            if f.kind in ("linear_pattern", "circular_pattern"):
                target_id = f.p["target_feature"]
                if target_id not in built_shapes:
                    raise GeometryError(GeometryErrorCode.INVALID_PARAMETER, f"target_feature {target_id!r} not found or not built yet", f.id)
                target = built_shapes[target_id]
                shape = Compound([transform * target for transform in pattern_locations(f, plan)])
                built_shapes[f.id] = shape
            elif f.kind in ("hole", "threaded_hole"):
                if solid is None:  # gated in plan.py; belt and braces
                    raise GeometryError(
                        GeometryErrorCode.INVALID_PARAMETER,
                        f"{f.kind} {f.id!r} has nothing to cut",
                        f.id,
                    )
                shape = loc * _cutter(f, solid)
                if isinstance(shape, list):
                    if len(shape) == 1:
                        shape = shape[0]
                    else:
                        shape = Compound(shape)
                built_shapes[f.id] = shape
                # hole inherently subtracts, so we don't need to do it twice
            elif f.kind in ("shell", "edge_fillet", "edge_chamfer") and ("length" not in f.p or f.kind == "shell"):
                if solid is None:
                    raise GeometryError(
                        GeometryErrorCode.INVALID_PARAMETER,
                        f"{f.kind} must modify an existing solid",
                        f.id,
                    )
                if f.kind == "shell":
                    t = f.p["thickness"]
                    open_faces = f.p.get("open_faces", [])
                    openings = []
                    for pt in open_faces:
                        openings.append(solid.faces().sort_by_distance(tuple(pt))[0])
                    
                    if openings:
                        if t < 0:
                            solid = offset(solid, amount=t, openings=openings)
                        else:
                            outer = offset(solid, amount=t, openings=openings)
                            # if it's an outward shell, offset actually makes the whole shell
                            # wait, offset with openings creates a shell, it doesn't just inflate!
                            # if t > 0, offset creates a shell growing outwards.
                            solid = outer
                    else:
                        if t < 0:
                            void = offset(solid, amount=t)
                            solid = solid - void
                        else:
                            outer = offset(solid, amount=t)
                            solid = outer - solid
                elif f.kind == "edge_fillet":
                    if f.p.get("select_all", False):
                        edges_to_mod = solid.edges()
                    else:
                        edges_to_mod = []
                        for pt in f.p.get("edge_selectors", []):
                            edges_to_mod.append(solid.edges().sort_by_distance(tuple(pt))[0])
                    solid = fillet(edges_to_mod, radius=f.p["radius"])
                elif f.kind == "edge_chamfer":
                    if f.p.get("select_all", False):
                        edges_to_mod = solid.edges()
                    else:
                        edges_to_mod = []
                        for pt in f.p.get("edge_selectors", []):
                            edges_to_mod.append(solid.edges().sort_by_distance(tuple(pt))[0])
                    solid = chamfer(edges_to_mod, length=f.p["width"])
                built_shapes[f.id] = solid
                continue
            else:
                shape = loc * _primitive(f, solid, built_shapes)
                if isinstance(shape, list):
                    if len(shape) == 1:
                        shape = shape[0]
                    else:
                        shape = Compound(shape)
                built_shapes[f.id] = shape
            
            # Apply additive or subtractive boolean
            if not shape.solids():
                pass # 2D shape, already in built_shapes
            else:
                if f.operation == "add":
                    solid = shape if solid is None else solid + shape
                elif f.operation == "subtract":
                    if solid is None:
                        raise GeometryError(
                            GeometryErrorCode.INVALID_PARAMETER,
                            f"{f.id!r} subtracts from nothing",
                            f.id,
                        )
                    solid = solid - shape
        except GeometryError:
            raise
        except Exception as exc:  # OCC failures are opaque; name the feature
            raise GeometryError(
                GeometryErrorCode.KERNEL_FAILURE,
                f"kernel failed while applying {f.kind} {f.id!r}",
                f.id,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

        if solid is not None:
            if solid.wrapped is None or not solid.solids():
                raise GeometryError(
                    GeometryErrorCode.EMPTY_RESULT,
                    f"the solid became empty after {f.kind} {f.id!r}",
                    f.id,
                    detail="a cut removed all remaining material",
                )

    if solid is None:
        raise GeometryError(
            GeometryErrorCode.EMPTY_RESULT,
            "No 3D solid was produced by the plan",
            plan.id,
        )

    if not solid.is_valid:
        raise GeometryError(
            GeometryErrorCode.KERNEL_FAILURE,
            "the resulting solid is not a valid B-Rep",
            None
        )
    if solid.volume <= 0.0:
        raise GeometryError(GeometryErrorCode.EMPTY_RESULT, "the resulting solid has no volume")

    # More than one lump means the ledger describes disconnected bodies. Legal
    # geometry, but the frontend renders one part and the agent reasons about
    # one part, so flag it rather than silently shipping the surprise.
    lumps = len(solid.solids())
    if lumps > 1:
        plan.warnings.append(
            f"The final part is a multi-body assembly containing {lumps} disconnected pieces. This is officially supported."
        )

    return solid


@dataclass
class AssemblyLeaf:
    path: tuple[str, ...]
    plan: BuildPlan
    part_id: str | None
    solid: Shape
    location: Location


def build_assembly(plan: AssemblyPlan, *, leaves: list[AssemblyLeaf] | None = None) -> Compound:
    """AssemblyPlan -> a Compound containing all placed components, or raise GeometryError."""
    locations = solve_rigid_joints({comp.id: Location(comp.origin, comp.rotation_deg)
                                    for comp in plan.components}, plan.joints)
    parts: dict[str, Shape] = {}
    part_plans: dict[str, BuildPlan] = {}
    for rpart in plan.parts:
        try:
            # We construct a fake BuildPlan to reuse build_solid
            part_plan = BuildPlan(
                project_id=plan.project_id,
                name=rpart.name,
                revision=plan.revision,
                units=plan.units,
                features=rpart.features
            )
            parts[rpart.id] = build_solid(part_plan)
            part_plans[rpart.id] = part_plan
        except GeometryError as e:
            # Re-raise with part context if possible
            raise GeometryError(e.code, f"Failed to build part {rpart.id!r}: {e.message}", e.feature_id, detail=e.detail)

    components: dict[str, Shape] = {}
    solids_list: list[Shape] = []

    # Place components
    for comp in plan.components:
        child_leaves: list[AssemblyLeaf] = []
        if comp.sub_assembly:
            base_solid = build_assembly(comp.sub_assembly, leaves=child_leaves if leaves is not None else None)
        elif comp.part_id in parts:
            base_solid = parts[comp.part_id]
            if leaves is not None:
                child_leaves.append(AssemblyLeaf((), part_plans[comp.part_id],
                    None if comp.id == "sub_comp" else comp.part_id, base_solid, Location()))
        else:
            raise GeometryError(
                GeometryErrorCode.INVALID_PARAMETER,
                f"Component {comp.id!r} references missing part {comp.part_id!r}",
                comp.id
            )
        
        # Clone the solid so we can independently position it
        
        # Apply component placement
        # The placement rotation is in degrees (intrinsic XYZ).
        loc = locations[comp.id]
        if leaves is not None:
            # sub_comp is the resolver's synthetic single-part wrapper, never a
            # schema-valid user component ID. Keep only real instance identities.
            prefix = () if comp.id == "sub_comp" else (comp.id,)
            leaves.extend(AssemblyLeaf(prefix + leaf.path, leaf.plan, leaf.part_id,
                                      leaf.solid, loc * leaf.location) for leaf in child_leaves)
        
        placed_solid = loc * base_solid
        
        # Labels aid diagnostics; explicit leaf records carry source identity.
        placed_solid.label = comp.id
        
        components[comp.id] = placed_solid
        solids_list.append(placed_solid)

    # Interference check
    comp_ids = list(components.keys())
    for i in range(len(comp_ids)):
        for j in range(i + 1, len(comp_ids)):
            id_a = comp_ids[i]
            id_b = comp_ids[j]
            shape_a = components[id_a]
            shape_b = components[id_b]
            
            try:
                intersection = shape_a.intersect(shape_b)
                if intersection is None:
                    continue
                if isinstance(intersection, list) or type(intersection).__name__ == "ShapeList":
                    vol = sum(s.volume for s in intersection)
                else:
                    vol = intersection.volume
                if vol > EPS:
                    raise GeometryError(
                        GeometryErrorCode.KERNEL_FAILURE,
                        f"Interference detected: Component {id_a!r} collides with {id_b!r}",
                        id_a,
                        detail=f"Intersection volume: {vol:.2f} mm^3"
                    )
            except Exception as e:
                if isinstance(e, GeometryError):
                    raise
                raise GeometryError(GeometryErrorCode.KERNEL_FAILURE,
                    f"Could not check interference between {id_a!r} and {id_b!r}", id_a,
                    detail=str(e)) from e

    # Return a Compound of all components
    assembly = Compound(solids_list)
    return assembly
