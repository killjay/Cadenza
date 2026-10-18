"""Conservative primitive-face frames and finite native contact verification."""
import math
from types import SimpleNamespace

from build123d import Location, Plane, Vector

from cadenza_contracts.assembly_commands import PlanarPick
from cadenza_contracts.ledger import PlanarTarget
from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.frames import feature_location, to_world, dir_to_world
from cadenza_geometry.joints import normal_rotation
from cadenza_geometry.provenance import attribute
from cadenza_geometry.spatial import _closest_point, _normal_at
from cadenza_geometry.surface_references import resolve_native_surface

DISTANCE_TOL = 1e-4  # mm, numerical verification only
NORMAL_TOL = 1e-7   # 1 + dot(opposing unit normals)
AREA_TOL = 1e-6     # mm²; edge/point contact is insufficient


def invalid(message, joint_id=None):
    return GeometryError(GeometryErrorCode.INVALID_PARAMETER, message, joint_id)


def source_geometry(plan, solid):
    faces = list(solid.faces())
    attribution, ambiguity = attribute(plan, faces)
    return SimpleNamespace(plan=plan, solid=solid, faces=faces, instances={}, ambiguity=ambiguity,
        attribution={i: (None, feature, spec) for i, (feature, spec) in attribution.items()})


def face_frame(built, target: PlanarTarget):
    """Return native final face and deterministic frame at the recorded UV point."""
    result = resolve_native_surface(built, target.reference)
    if result["status"] != "resolved" or not result["face"]["planar"]:
        raise invalid(result.get("message", "Attachment needs a unique planar face."))
    feature = built.plan.by_id(target.reference.feature_id)
    height = feature.p["height"]
    # Each tuple is plane origin, tangent X and outward normal in feature space.
    roles = {
        "top_face": ((0, 0, height), (1, 0, 0), (0, 0, 1)),
        "bottom_face": ((0, 0, 0), (1, 0, 0), (0, 0, -1)),
    }
    if feature.kind == "box":
        x, y = feature.p["length"] / 2, feature.p["width"] / 2
        roles.update({
            "side_x_pos": ((x, 0, 0), (0, 1, 0), (1, 0, 0)),
            "side_x_neg": ((-x, 0, 0), (0, 1, 0), (-1, 0, 0)),
            "side_y_pos": ((0, y, 0), (1, 0, 0), (0, 1, 0)),
            "side_y_neg": ((0, -y, 0), (1, 0, 0), (0, -1, 0)),
        })
    origin, tangent, normal = roles[target.reference.role]
    frame = feature_location(feature, built.plan) * Plane(origin, x_dir=tangent, z_dir=normal).location
    frame = frame * Location((target.uv[0], target.uv[1], 0))
    return result["native_face"], frame


def on_face(face, frame):
    point = to_world(frame, (0, 0, 0))
    distance, nearest = _closest_point(face, point)
    normal = Vector(dir_to_world(frame, (0, 0, 1)))
    if distance > DISTANCE_TOL or normal.dot(Vector(_normal_at(face, nearest))) < 1 - NORMAL_TOL:
        raise invalid("The attachment point is outside its surviving face. Repick or detach the attachment.")


def capture(built, pick: PlanarPick):
    target = PlanarTarget(reference=pick.reference, uv=[0.0, 0.0])
    face, origin = face_frame(built, target)
    local = to_world(origin.inverse(), (pick.point[0], pick.point[1], pick.point[2]))
    if abs(local[2]) > DISTANCE_TOL:
        raise invalid("Selected attachment point does not lie on this face.")
    target = PlanarTarget(reference=pick.reference, uv=list(local[:2]))
    face, frame = face_frame(built, target)
    on_face(face, frame)
    picked_normal = Vector(pick.normal).normalized()
    if picked_normal.dot(Vector(dir_to_world(frame, (0, 0, 1)))) < 1 - NORMAL_TOL:
        raise invalid("Selected attachment normal does not match this face.")
    return target, frame


def capture_pair(built, operation):
    """Capture two source-local picks using accepted, solved owner-local poses."""
    captured, frames = [], []
    for side in ("a", "b"):
        cid = getattr(operation, f"component_{side}")
        leaf = built.instances.get(cid)
        pick = getattr(operation, f"target_{side}")
        if leaf is None or leaf.part_id is not None or leaf.plan.project_id != pick.reference.source_ledger_id:
            raise invalid("Choose two direct ledger-backed parts in this assembly.")
        target, frame = capture(source_geometry(leaf.plan, leaf.solid), pick)
        captured.append(target.model_dump(mode="json"))
        frames.append(leaf.location * frame)
    a, b = frames
    normal_a = Vector(dir_to_world(a, (0, 0, 1)))
    rotation = normal_rotation(Vector(dir_to_world(b, (0, 0, 1))), -normal_a)
    tangent_b = Vector(dir_to_world(rotation * b, (1, 0, 0)))
    tangent_a = Vector(dir_to_world(a, (1, 0, 0)))
    twist = math.degrees(math.atan2(normal_a.dot(tangent_a.cross(tangent_b)), tangent_a.dot(tangent_b)))
    return {"target_a": captured[0], "target_b": captured[1], "twist_deg": twist}


def verify_contact(face_a, frame_a, face_b, frame_b, joint_id):
    """Independent finite face checks; no bounding-box or nearest-face success."""
    try:
        on_face(face_a, frame_a)
        on_face(face_b, frame_b)
        separation = (Vector(to_world(frame_a, (0, 0, 0))) - Vector(to_world(frame_b, (0, 0, 0)))).length
        residual = abs(1 + Vector(dir_to_world(frame_a, (0, 0, 1))).dot(Vector(dir_to_world(frame_b, (0, 0, 1)))))
        if separation > DISTANCE_TOL or residual > NORMAL_TOL:
            raise invalid("Attached faces do not meet with opposite normals.", joint_id)
        common = face_a.intersect(face_b)
        shapes = common if isinstance(common, list) or type(common).__name__ == "ShapeList" else [common]
        area = sum(shape.area for shape in shapes if shape is not None)
        if not math.isfinite(area) or area <= AREA_TOL:
            raise invalid("Attached faces have no verified contact area.", joint_id)
        return dict(joint_id=joint_id, separation_mm=float(separation), normal_residual=float(residual),
                    area_mm2=float(area), references_resolved=True, contact_verified=True)
    except GeometryError:
        raise
    except Exception as exc:
        raise invalid("Native face contact could not be verified.", joint_id) from exc
