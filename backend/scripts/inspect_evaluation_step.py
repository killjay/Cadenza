"""Native STEP measurement in a disposable process; stdout contains JSON only."""
import json
import sys

from build123d import GeomType, import_step


def measure(shape):
    bounds = shape.bounding_box()
    return dict(volume=shape.volume, minimum=list(bounds.min), maximum=list(bounds.max))


if __name__ == "__main__":
    shape = import_step(sys.argv[1])
    solids = list(shape.solids())
    bores = []
    for face in shape.faces():
        if face.geom_type != GeomType.CYLINDER:
            continue
        axis = face.axis_of_rotation
        if axis is None or abs(abs(axis.direction.Z) - 1) > 1e-7:
            continue
        center, normal = face.center(), face.normal_at()
        if normal.X * (center.X - axis.position.X) + normal.Y * (center.Y - axis.position.Y) >= 0:
            continue
        bounds = face.bounding_box()
        bores.append(dict(x=axis.position.X, y=axis.position.Y, radius=face.radius,
            z_min=bounds.min.Z, z_max=bounds.max.Z))
    print(json.dumps(dict(**measure(shape), solids=len(solids), per_solid=[measure(s) for s in solids], bores=bores), allow_nan=False))
