"""The reverse spatial lookup: a 3D click -> the ledger node that made it.

Blueprint §5 step 5. The click arrives as a bare coordinate; we find the face
of the CURRENT solid nearest to it and report which feature generated that
face, semantically.

Distance is measured to the TRIMMED face, not to its underlying surface, via
OCC's `BRepExtrema`. The difference matters: a click straight down the middle
of a Ø10 bore is 0mm from the top face's *plane* but ~5mm from the top *face*,
because the plane there has a hole in it. Measuring to the plane would attribute
every click over a bore to the face surrounding it.

The honest limitation is in `confidence`: when the two nearest faces are nearly
equidistant, the winner is close to arbitrary and a small edit can flip it.
That is reported rather than smoothed over -- see NOTES.md.
"""

from __future__ import annotations

from build123d import Face, Shape

from cadenza_geometry.contracts import SpatialHit, SpatialQuery
from cadenza_geometry.plan import BuildPlan
from cadenza_geometry.provenance import Spec
from cadenza_geometry.plan import RFeature

# If the runner-up face is within this distance of the winner, the pick is not
# meaningfully determined by the geometry.
AMBIGUITY_BAND = 0.5  # mm

# cos(60 degrees). A face is "the same surface the user clicked" if it still
# faces within 60 degrees of the recorded normal. Loose on purpose: a fillet or
# a draft angle rotates a face somewhat, and rejecting those would trade a rare
# wrong answer for a common no answer.
NORMAL_AGREEMENT = 0.5


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(a):
    n = (a[0] ** 2 + a[1] ** 2 + a[2] ** 2) ** 0.5
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else (0.0, 0.0, 0.0)


# Below this the point is effectively still on the surface, so interiority is
# just numerical noise rather than evidence of drift.
INTERIOR_TOL = 1e-6


def _is_inside(solid: Shape, point: tuple[float, float, float]) -> bool:
    """Is the point strictly within the material?"""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_State

    try:
        clf = BRepClass3d_SolidClassifier(solid.wrapped)
        clf.Perform(gp_Pnt(*point), 1e-7)
        return clf.State() == TopAbs_State.TopAbs_IN
    except Exception:
        return False


def _closest_point(face: Face, point: tuple[float, float, float]):
    """(distance, point_on_face) using exact B-rep extrema."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    calc = BRepExtrema_DistShapeShape(face.wrapped, vertex)
    calc.Perform()
    if not calc.IsDone() or calc.NbSolution() < 1:
        return float("inf"), point
    p = calc.PointOnShape1(1)
    return calc.Value(), (p.X(), p.Y(), p.Z())


def _normal_at(face: Face, point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Outward normal at a specific point on the face, falling back to the
    centroid if the point sits on a seam or vertex where OCC declines."""
    from build123d import Vector

    for probe_point in (Vector(*point), face.center()):
        try:
            n = face.normal_at(probe_point)
            return (n.X, n.Y, n.Z)
        except Exception:
            continue
    return (0.0, 0.0, 1.0)


def probe(
    plan: BuildPlan,
    faces: list[Face],
    attribution: dict[int, tuple[RFeature, Spec]],
    ambiguity: dict[int, list[str]],
    query: SpatialQuery,
    solid: Shape | None = None,
) -> SpatialHit | None:
    """Nearest attributable face to `query.point`, as a semantic hit."""
    ranked: list[tuple[float, int, tuple[float, float, float]]] = []
    for i, face in enumerate(faces):
        dist, on_face = _closest_point(face, query.point)
        ranked.append((dist, i, on_face))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[0])

    # Walk outward to the nearest face we can actually name. An unattributed
    # face is useless to the agent layer -- it has no ledger node to patch --
    # so returning the next-nearest named face beats returning a shrug.
    named = [r for r in ranked if r[1] in attribution]
    if not named:
        return None
    chosen = named[0]

    # Normal filter. Distance alone is not enough to keep a stored coordinate
    # attached to the surface the user actually clicked: grow a plate and the
    # click point ends up INSIDE the solid, where the nearest face is whatever
    # happens to be closest -- typically the wall of an unrelated hole. Such a
    # face is nearly always oriented differently from the one that was clicked,
    # so when the caller supplies the click-time normal we take the nearest
    # face that still FACES THE SAME WAY, and only fall back to raw distance if
    # nothing does.
    normal_rescued = False
    _has_aligned = False
    if query.normal is not None:
        want = _unit(query.normal)
        if want != (0.0, 0.0, 0.0):
            aligned = [
                r for r in named
                if _dot(_unit(_normal_at(faces[r[1]], r[2])), want) >= NORMAL_AGREEMENT
            ]
            if aligned:
                _has_aligned = True
                normal_rescued = aligned[0][1] != chosen[1]
                chosen = aligned[0]

    dist, idx, on_face = chosen
    if query.max_distance is not None and dist > query.max_distance:
        return None

    feat, spec = attribution[idx]
    face = faces[idx]
    # At the HIT POINT, not at the centroid: on a bore the normal swings a full
    # 360 degrees around the axis, so a centroid normal would point the
    # frontend's callout widget at the opposite wall of the hole.
    normal = _normal_at(face, on_face)

    confidence = 1.0
    notes: list[str] = []
    rivals: list[str] = list(ambiguity.get(idx, ()))

    # (a) Is the pick decisive? A runner-up from a DIFFERENT feature within the
    # ambiguity band is the case that bites, because the wrong ledger node gets
    # patched and the user sees an edit land somewhere they did not click.
    for rdist, ridx, _ in named[1:]:
        if ridx not in attribution:
            continue
        rfeat, _rspec = attribution[ridx]
        if rfeat.id == feat.id:
            continue
        gap = rdist - dist
        if gap < AMBIGUITY_BAND:
            confidence = min(confidence, max(0.0, gap / AMBIGUITY_BAND) * 0.5 + 0.5)
            if rfeat.id not in rivals:
                rivals.append(rfeat.id)
            notes.append(f"within {AMBIGUITY_BAND}mm of a face from {rfeat.id}")
        break

    # (b) Has the coordinate gone STALE? This is the failure that margin-based
    # confidence cannot see. A click is recorded ON the surface; if the part
    # later grows past that point, the point is now INSIDE the solid and the
    # nearest face is simply whatever is closest from the inside -- often the
    # wall of an unrelated hole, and often by a wide, confident-looking margin.
    # Interiority is the direct evidence that the recorded point no longer
    # describes the surface it was recorded on.
    if solid is not None and dist > INTERIOR_TOL and _is_inside(solid, query.point):
        confidence = min(confidence, 0.3)
        notes.append(
            f"the stored point is now {dist:.2f}mm INSIDE the solid — the part has grown "
            "past it, so this attribution is a guess"
        )

    # (c) A recorded normal that matches nothing means every candidate face
    # points somewhere else than the surface that was clicked.
    if query.normal is not None:
        if not _has_aligned:
            confidence = min(confidence, 0.4)
            notes.append("no remaining face matches the recorded click normal")
        elif normal_rescued:
            notes.append("recorded normal overrode a nearer but differently-oriented face")

    return SpatialHit(
        feature_id=feat.id,
        label=spec.label,
        distance=dist,
        point_on_face=on_face,
        normal=normal,
        confidence=confidence,
        ambiguous_with=tuple(rivals),
        notes=tuple(notes),
    )
