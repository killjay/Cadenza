"""A minimal, fully in-memory GLB writer.

WHY THIS EXISTS: build123d's `export_gltf` cannot write to a buffer. It ends in

    writer = RWGltf_CafWriter(theFile=TCollection_AsciiString(fsdecode(file_path)), ...)

and `os.fsdecode` raises TypeError on a BytesIO. OCP's `RWGltf_CafWriter`
exposes only `Perform` -- there is no stream overload to reach for. So the
blueprint's §4 snippet

    export_gltf(base_plate.part, glb_bytes, binary=True)

does not run as written; it is not a matter of arguments. The options were a
temp file (disk I/O, which §4 explicitly wants to avoid) or writing the
container ourselves. It is ~150 lines of a well-specified format, and it buys
something a temp file could not:

    triangles are grouped BY LEDGER FEATURE, one glTF node per feature, named
    with the feature id.

which means a Three.js raycast hit already carries `feat_base_9f2a` in
`object.name`. The frontend can resolve the common case without a round trip,
and the server-side spatial probe becomes the exact-geometry fallback rather
than the only path.

glTF is Y-up and OCC is Z-up, so positions and normals are mapped
(x, y, z) -> (x, z, -y).
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field

from build123d import Face

FLOAT = 5126
UNSIGNED_INT = 5125
ARRAY_BUFFER = 34962
ELEMENT_ARRAY_BUFFER = 34963

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942


@dataclass
class MeshGroup:
    """Triangles belonging to one ledger feature."""

    name: str
    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not self.indices


def _tri_normal(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)


def tessellate_face(
    face: Face, group: MeshGroup, tolerance: float, angular_tolerance: float
) -> None:
    """Append one face's triangles to `group`, oriented outward.

    Faces are tessellated INDIVIDUALLY and their vertices are never shared
    between faces. That is deliberate: it keeps normals smooth across a
    curved face (the bore looks round) while preserving the hard crease where
    a bore meets the top face, with no angle-threshold heuristic.
    """
    try:
        verts, tris = face.tessellate(tolerance, angular_tolerance)
    except Exception:
        return
    if not tris:
        return

    pts = [(v.X, v.Y, v.Z) for v in verts]

    # OCC's triangulation winding follows the face's own orientation flag,
    # which a subtraction flips. Rather than trust it, measure: compare the
    # summed geometric normal against the face's true outward normal.
    acc = [0.0, 0.0, 0.0]
    for i0, i1, i2 in tris:
        n = _tri_normal(pts[i0], pts[i1], pts[i2])
        acc[0] += n[0]
        acc[1] += n[1]
        acc[2] += n[2]
    try:
        c = face.center()
        ref = face.normal_at(c)
        ref_v = (ref.X, ref.Y, ref.Z)
    except Exception:
        ref_v = (acc[0], acc[1], acc[2])
    flip = (acc[0] * ref_v[0] + acc[1] * ref_v[1] + acc[2] * ref_v[2]) < 0.0

    # Per-vertex normals, area-weighted, accumulated within this face only.
    vnormals = [[0.0, 0.0, 0.0] for _ in pts]
    ordered = []
    for i0, i1, i2 in tris:
        if flip:
            i1, i2 = i2, i1
        n = _tri_normal(pts[i0], pts[i1], pts[i2])
        for i in (i0, i1, i2):
            vnormals[i][0] += n[0]
            vnormals[i][1] += n[1]
            vnormals[i][2] += n[2]
        ordered.append((i0, i1, i2))

    base = len(group.positions)
    for p, n in zip(pts, vnormals):
        length = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5
        if length < 1e-12:
            nx, ny, nz = 0.0, 0.0, 1.0
        else:
            nx, ny, nz = n[0] / length, n[1] / length, n[2] / length
        # Z-up (OCC) -> Y-up (glTF)
        group.positions.append((p[0], p[2], -p[1]))
        group.normals.append((nx, nz, -ny))
    for i0, i1, i2 in ordered:
        group.indices.extend((base + i0, base + i1, base + i2))


def write_glb(groups: list[MeshGroup], generator: str = "cadenza-geometry") -> bytes:
    """Assemble grouped triangles into a GLB binary. Pure bytes, no disk."""
    groups = [g for g in groups if not g.is_empty]
    if not groups:
        raise ValueError("no geometry to write")

    bin_parts: list[bytes] = []
    offset = 0
    buffer_views: list[dict] = []
    accessors: list[dict] = []
    meshes: list[dict] = []
    nodes: list[dict] = []

    def add_view(payload: bytes, target: int) -> int:
        nonlocal offset
        pad = (-len(payload)) % 4
        bin_parts.append(payload)
        if pad:
            bin_parts.append(b"\x00" * pad)
        buffer_views.append(
            {"buffer": 0, "byteOffset": offset, "byteLength": len(payload), "target": target}
        )
        offset += len(payload) + pad
        return len(buffer_views) - 1

    for g in groups:
        pos_blob = b"".join(struct.pack("<3f", *p) for p in g.positions)
        nrm_blob = b"".join(struct.pack("<3f", *n) for n in g.normals)
        idx_blob = struct.pack(f"<{len(g.indices)}I", *g.indices)

        pos_view = add_view(pos_blob, ARRAY_BUFFER)
        nrm_view = add_view(nrm_blob, ARRAY_BUFFER)
        idx_view = add_view(idx_blob, ELEMENT_ARRAY_BUFFER)

        xs = [p[0] for p in g.positions]
        ys = [p[1] for p in g.positions]
        zs = [p[2] for p in g.positions]

        accessors.append(
            {
                "bufferView": pos_view,
                "componentType": FLOAT,
                "count": len(g.positions),
                "type": "VEC3",
                # POSITION accessors are REQUIRED to carry min/max; viewers use
                # them to frame the camera without walking the buffer.
                "min": [min(xs), min(ys), min(zs)],
                "max": [max(xs), max(ys), max(zs)],
            }
        )
        pos_acc = len(accessors) - 1
        accessors.append(
            {
                "bufferView": nrm_view,
                "componentType": FLOAT,
                "count": len(g.normals),
                "type": "VEC3",
            }
        )
        nrm_acc = len(accessors) - 1
        accessors.append(
            {
                "bufferView": idx_view,
                "componentType": UNSIGNED_INT,
                "count": len(g.indices),
                "type": "SCALAR",
            }
        )
        idx_acc = len(accessors) - 1

        meshes.append(
            {
                "name": g.name,
                "primitives": [
                    {
                        "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
                        "indices": idx_acc,
                        "material": 0,
                        "mode": 4,
                    }
                ],
            }
        )
        nodes.append(
            {
                "name": g.name,
                "mesh": len(meshes) - 1,
                # The whole point of the grouping: the ledger id travels with
                # the render node, so a raycast hit is already semantic.
                "extras": {"feature_id": g.name, "labels": sorted(g.labels)},
            }
        )

    gltf = {
        "asset": {"version": "2.0", "generator": generator},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": [
            {
                "name": "cadenza_default",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.72, 0.74, 0.78, 1.0],
                    "metallicFactor": 0.15,
                    "roughnessFactor": 0.55,
                },
                "doubleSided": False,
            }
        ],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": offset}],
    }

    json_blob = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_blob += b" " * ((-len(json_blob)) % 4)  # JSON chunk pads with SPACES
    bin_blob = b"".join(bin_parts)

    total = 12 + 8 + len(json_blob) + 8 + len(bin_blob)
    out = bytearray()
    out += struct.pack("<III", GLB_MAGIC, 2, total)
    out += struct.pack("<II", len(json_blob), CHUNK_JSON)
    out += json_blob
    out += struct.pack("<II", len(bin_blob), CHUNK_BIN)
    out += bin_blob
    return bytes(out)


def parse_glb(data: bytes) -> tuple[dict, bytes]:
    """Minimal reader, used by the tests to prove what we wrote is loadable."""
    if len(data) < 12:
        raise ValueError("too short to be a GLB")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        raise ValueError(f"bad GLB magic: {magic:#x}")
    if version != 2:
        raise ValueError(f"unsupported glTF version {version}")
    if length != len(data):
        raise ValueError(f"header length {length} != actual {len(data)}")

    pos = 12
    gltf: dict | None = None
    binary = b""
    while pos < len(data):
        clen, ctype = struct.unpack_from("<II", data, pos)
        pos += 8
        chunk = data[pos : pos + clen]
        if ctype == CHUNK_JSON:
            gltf = json.loads(chunk.decode("utf-8"))
        elif ctype == CHUNK_BIN:
            binary = chunk
        pos += clen + ((-clen) % 4)
    if gltf is None:
        raise ValueError("GLB has no JSON chunk")
    return gltf, binary
