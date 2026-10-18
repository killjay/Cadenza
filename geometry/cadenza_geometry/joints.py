"""Deterministic rigid attachments; input anchors are in pre-joint assembly space."""
import math

from build123d import Location, Vector

from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.frames import dir_to_world, to_world
from cadenza_geometry.plan import RJoint


def ordered_joints(initial: dict[str, Location], joints: list[RJoint]) -> list[RJoint]:
    """A anchors B. Each B has one parent; roots retain their initial placement.

    Normals are opposing mating directions. Missing normals on both sides mean
    translation only. Placement supplies the remaining twist; this is no motion
    or closed-loop constraint solver and does not resolve face identities.
    """
    pending = list(joints)
    incoming = {}
    for joint in pending:
        def invalid(message):
            return GeometryError(GeometryErrorCode.INVALID_PARAMETER, message, joint.id)
        if joint.kind != "rigid":
            raise GeometryError(GeometryErrorCode.UNSUPPORTED_FEATURE,
                f"Joint kind {joint.kind!r} is not supported; only rigid attachments are implemented.", joint.id)
        if joint.component_a not in initial or joint.component_b not in initial:
            raise invalid("Joint references a missing component")
        if joint.component_a == joint.component_b:
            raise invalid("A joint cannot attach a component to itself")
        if joint.component_b in incoming:
            raise invalid("Multiple joints drive the same component; closed-loop constraints are not supported")
        incoming[joint.component_b] = joint
        if (joint.target_a_norm is None) != (joint.target_b_norm is None):
            raise invalid("Provide both joint normals or neither")
        for normal in (joint.target_a_norm, joint.target_b_norm):
            if normal is not None and (not all(math.isfinite(v) for v in normal) or math.hypot(*normal) < 1e-12):
                raise invalid("Joint normals must be finite and nonzero")
        for point in (joint.target_a_pt, joint.target_b_pt):
            if not all(math.isfinite(v) for v in point):
                raise invalid("Joint points must be finite")

    ordered = []
    completed = set(initial) - set(incoming)
    while pending:
        ready = [joint for joint in pending if joint.component_a in completed]
        if not ready:
            raise GeometryError(GeometryErrorCode.INVALID_PARAMETER,
                "Joint dependency cycle; closed-loop constraints are not supported", pending[0].id)
        for joint in ready:
            ordered.append(joint)
            completed.add(joint.component_b)
            pending.remove(joint)
    return ordered


def normal_rotation(current: Vector, desired: Vector) -> Location:
    axis = current.cross(desired)
    cosine = max(-1.0, min(1.0, current.dot(desired)))
    if axis.length < 1e-12:
        if cosine >= 0:
            return Location()
        basis = min((Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1)), key=lambda v: abs(current.dot(v)))
        axis = current.cross(basis)
        return Location((0, 0, 0), axis, 180)
    return Location((0, 0, 0), axis, math.degrees(math.acos(cosine)))


def solve_rigid_joints(initial: dict[str, Location], joints: list[RJoint],
                       planar_frames: dict[str, tuple[Location, Location]] | None = None) -> dict[str, Location]:
    solved = dict(initial)
    for joint in ordered_joints(initial, joints):
        if joint.planar_a is not None:
            if planar_frames is None or joint.id not in planar_frames or joint.twist_deg is None:
                raise GeometryError(GeometryErrorCode.INVALID_PARAMETER, "Planar attachment was not resolved", joint.id)
            frame_a, frame_b = planar_frames[joint.id]
            # In A's face frame, rotate around its normal then oppose B's normal.
            solved[joint.component_b] = (solved[joint.component_a] * frame_a
                * Location((0, 0, 0), (0, 0, joint.twist_deg))
                * Location((0, 0, 0), (180, 0, 0)) * frame_b.inverse())
            continue
        a, b = joint.component_a, joint.component_b
        # Freeze anchors relative to their originally placed components, then
        # follow upstream movement while solving the attachment chain.
        anchor_a = to_world(initial[a].inverse(), joint.target_a_pt)
        anchor_b = to_world(initial[b].inverse(), joint.target_b_pt)
        point_a = to_world(solved[a], anchor_a)
        point_b = to_world(solved[b], anchor_b)
        rotation = Location()
        if joint.target_a_norm is not None and joint.target_b_norm is not None:
            local_a = dir_to_world(initial[a].inverse(), joint.target_a_norm)
            local_b = dir_to_world(initial[b].inverse(), joint.target_b_norm)
            desired = -Vector(dir_to_world(solved[a], local_a))
            current = Vector(dir_to_world(solved[b], local_b))
            rotation = normal_rotation(current, desired)
        delta = Location(point_a) * rotation * Location(tuple(-v for v in point_b))
        solved[b] = delta * solved[b]
    return solved
