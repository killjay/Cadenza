/**
 * The exact-geometry worker. Owns an OCCT kernel and the STEP B-rep; the main
 * thread never touches either.
 *
 * The API here is deliberately domain-shaped (`loadStep`, `getPickMesh`,
 * `faceInfo`, `raycast`) rather than a thin proxy over the kernel. Comlink
 * round-trips cost ~0.2 ms each, and a face pick expressed as twenty kernel
 * calls would be twenty round trips; expressed as one domain call it is one.
 *
 * WHY A PICK MESH AND NOT A PER-CLICK KERNEL RAYCAST
 * --------------------------------------------------
 * `meshShape()` returns the tessellation *plus* `faceGroups`: a
 * `[triStart, triCount, faceHash]` triple per B-rep face. That is the whole
 * ball game — the triangles carry their parent face's exact identity. The main
 * thread raycasts that mesh with three.js (microseconds, no round trip) and
 * reads the face hash straight out of the groups. The identity it gets back is
 * the kernel's, not the renderer's.
 *
 * `raycast()` below does the same job entirely inside OCCT, against the exact
 * B-rep with no tessellation involved. It is 100-1000x slower and exists to
 * prove the fast path agrees with the kernel — the viewport calls it on demand
 * (the "verify" button), not on every click.
 */
import * as Comlink from "comlink";
import { OcctKernel } from "occt-wasm";
import type { ShapeHandle, Vec3 } from "occt-wasm";

// Vite emits the 22 MB wasm as an asset and hands back its URL. Do not remove
// in favour of occt-wasm's auto-locate: under a bundler the module URL is a
// hashed chunk and the sibling .wasm is not there.
import wasmUrl from "occt-wasm/dist/occt-wasm.wasm?url";

/* ------------------------------------------------------------------ */
/* payload shapes (structured-cloneable; typed arrays are transferred)  */
/* ------------------------------------------------------------------ */

/**
 * The wire's vector is a tuple (ledger.py: `Vec3 = list[float]`, length 3);
 * occt-wasm's is `{x, y, z}`. The conversion happens here and nowhere else, so
 * no coordinate crosses the worker boundary in the kernel's shape.
 */
export type Triple = [number, number, number];

const toVec = (t: Triple): Vec3 => ({ x: t[0], y: t[1], z: t[2] });
const fromVec = (v: Vec3): Triple => [v.x, v.y, v.z];

export interface ModelSummary {
  /** Number of B-rep faces. */
  faceCount: number;
  edgeCount: number;
  solidCount: number;
  volume: number;
  area: number;
  bbox: { min: [number, number, number]; max: [number, number, number] };
  /** ms spent inside importStep. */
  importMs: number;
}

export interface PickMesh {
  positions: Float32Array;
  normals: Float32Array;
  indices: Uint32Array;
  /** [triStart, triCount, faceHash] per face. */
  faceGroups: Int32Array;
  faceCount: number;
  triangleCount: number;
  tessellateMs: number;
}

export interface WireframeData {
  /** XYZ interleaved polyline samples. */
  points: Float32Array;
  /** [pointStart, pointCount, edgeHash] per edge. */
  edgeGroups: Int32Array;
  edgeCount: number;
}

export interface FaceFacts {
  faceHash: number;
  surfaceType: string;
  area: number;
  centerOfMass: [number, number, number];
  normal: [number, number, number] | null;
  radius: number | null;
}

export interface RayHit {
  faceHash: number;
  point: [number, number, number];
  distance: number;
  /** ms spent in the kernel for this query. */
  queryMs: number;
}

export interface BootInfo {
  bootMs: number;
  wasmUrl: string;
}

/* ------------------------------------------------------------------ */

let kernel: OcctKernel | null = null;
let bootInfo: BootInfo | null = null;

/** The current part. One shape at a time — this app edits a single part. */
let shape: ShapeHandle | null = null;
/** faceHash -> face handle, rebuilt on every load. */
let facesByHash = new Map<number, ShapeHandle>();

async function getKernel(): Promise<OcctKernel> {
  if (kernel) return kernel;
  const t0 = performance.now();
  kernel = await OcctKernel.init({ wasm: wasmUrl });
  bootInfo = { bootMs: performance.now() - t0, wasmUrl };
  return kernel;
}

function requireShape(): { k: OcctKernel; s: ShapeHandle } {
  if (!kernel || shape === null) throw new Error("no model loaded");
  return { k: kernel, s: shape };
}

/**
 * OCCT folds a face hash into [1, upperBound]. The bound is NOT a free choice:
 * `meshShape` tags every triangle group with the hash it computes internally,
 * and this map is the only thing that turns one of those tags back into a face
 * handle. Pick a different bound and the two live in disjoint number ranges —
 * every `faceFacts` call throws `unknown face hash`, the selection readout hangs
 * on "resolving face…", and nothing in the console says why.
 *
 * INT32_MAX is what `meshShape` uses (its `faceGroups` is an Int32Array of raw
 * folded hashes). Verified against occt-wasm 4.3: for every face of a shape,
 * `hashCode(face, HASH_UPPER_BOUND)` equals that face's `faceGroups` tag.
 */
const HASH_UPPER_BOUND = 2 ** 31 - 1;

function indexFaces(k: OcctKernel, s: ShapeHandle): void {
  facesByHash = new Map();
  // Hash each handle directly rather than zipping against `subShapeHashes` —
  // that returns DEDUPLICATED hashes, so a shape with two identical faces would
  // silently shift the pairing by one from that point on.
  for (const f of k.getSubShapes(s, "face")) {
    facesByHash.set(k.hashCode(f, HASH_UPPER_BOUND), f);
  }
}

const api = {
  /** Boots the kernel. Call early — it is ~4.5 MB brotli over the wire. */
  async boot(): Promise<BootInfo> {
    await getKernel();
    return bootInfo!;
  },

  /**
   * Replaces the current part with the one in this STEP file.
   * Accepts the raw bytes exactly as they come off the WebSocket.
   */
  async loadStep(data: ArrayBuffer): Promise<ModelSummary> {
    const k = await getKernel();

    // Drop the previous part's handles before allocating the new one.
    if (shape !== null) {
      k.releaseAll();
      shape = null;
      facesByHash = new Map();
    }

    const t0 = performance.now();
    const imported = k.importStep(data);
    const importMs = performance.now() - t0;

    // STEP import yields a compound; unwrap to the solid so face indices are
    // the part's, not the container's.
    const solids = k.getSubShapes(imported, "solid");
    shape = solids[0] ?? imported;

    indexFaces(k, shape);

    const bb = k.getBoundingBox(shape);
    return {
      faceCount: facesByHash.size,
      edgeCount: k.subShapeCount(shape, "edge"),
      solidCount: solids.length,
      volume: k.getVolume(shape),
      area: k.getSurfaceArea(shape),
      bbox: { min: [bb.xmin, bb.ymin, bb.zmin], max: [bb.xmax, bb.ymax, bb.zmax] },
      importMs,
    };
  },

  /**
   * The tessellation the main thread raycasts against, with every triangle
   * tagged by its parent B-rep face. Typed arrays are transferred, not copied.
   */
  async getPickMesh(linearDeflection = 0.1, angularDeflection = 0.3): Promise<PickMesh> {
    const { k, s } = requireShape();
    const t0 = performance.now();
    const mesh = k.meshShape(s, { linearDeflection, angularDeflection });
    const tessellateMs = performance.now() - t0;

    if (!mesh.faceGroups || mesh.faceCount === undefined) {
      throw new Error(
        "meshShape returned no faceGroups — occt-wasm changed shape; picking has no face identity",
      );
    }

    const payload: PickMesh = {
      positions: mesh.positions,
      normals: mesh.normals,
      indices: mesh.indices,
      faceGroups: mesh.faceGroups,
      faceCount: mesh.faceCount,
      triangleCount: mesh.triangleCount,
      tessellateMs,
    };
    return Comlink.transfer(payload, [
      payload.positions.buffer,
      payload.normals.buffer,
      payload.indices.buffer,
      payload.faceGroups.buffer,
    ]);
  },

  /** Exact B-rep edges, sampled as polylines. Drawn as the CAD outline. */
  async getWireframe(deflection = 0.05): Promise<WireframeData> {
    const { k, s } = requireShape();
    const w = k.wireframe(s, deflection);
    const payload: WireframeData = {
      points: w.points,
      edgeGroups: w.edgeGroups,
      edgeCount: w.edgeCount,
    };
    return Comlink.transfer(payload, [payload.points.buffer, payload.edgeGroups.buffer]);
  },

  /**
   * Everything exact we can say about a face, for the selection readout.
   * "Ø10.0 cylinder, 628.3 mm²" comes from the kernel, not from the mesh.
   */
  async faceFacts(faceHash: number, near?: [number, number, number]): Promise<FaceFacts> {
    const { k } = requireShape();
    const face = facesByHash.get(faceHash);
    if (face === undefined) throw new Error(`unknown face hash ${faceHash}`);

    const surfaceType = k.surfaceType(face);

    // Normal at the hit point when we have one, else at the middle of the
    // face's UV domain. This is the surface's own normal, evaluated from its
    // parameterisation — not an averaged vertex normal off the mesh.
    let normal: Triple | null = null;
    try {
      let u: number;
      let v: number;
      if (near) {
        ({ u, v } = k.uvFromPoint(face, toVec(near)));
      } else {
        const uv = k.uvBounds(face);
        u = (uv.uMin + uv.uMax) / 2;
        v = (uv.vMin + uv.vMax) / 2;
      }
      normal = fromVec(k.surfaceNormal(face, u, v));
    } catch {
      /* degenerate parameterisation — the readout just omits the normal */
    }

    const cyl = surfaceType === "cylinder" ? k.getFaceCylinderData(face) : null;

    return {
      faceHash,
      surfaceType,
      area: k.getSurfaceArea(face),
      centerOfMass: fromVec(k.getCenterOfMass(face)),
      normal,
      radius: cyl?.radius ?? null,
    };
  },

  /**
   * A true raycast against the exact B-rep — no tessellation anywhere in the
   * path. Section the ray (as a line edge) against the solid, take the nearest
   * intersection vertex, then ask which face owns it.
   *
   * This is the blueprint's §5 step 2 done literally. It is here to validate
   * the fast path, and as the fallback if `faceGroups` ever goes away.
   */
  async raycast(
    origin: Triple,
    direction: Triple,
    maxDistance = 10_000,
  ): Promise<RayHit | null> {
    const { k, s } = requireShape();
    const t0 = performance.now();

    const len = Math.hypot(...direction) || 1;
    const d: Triple = [direction[0] / len, direction[1] / len, direction[2] / len];
    const far: Triple = [
      origin[0] + d[0] * maxDistance,
      origin[1] + d[1] * maxDistance,
      origin[2] + d[2] * maxDistance,
    ];

    const mark = k.checkpoint();
    try {
      const ray = k.makeLineEdge(toVec(origin), toVec(far));
      const section = k.section(ray, s);
      const verts = k.getSubShapes(section, "vertex");
      if (verts.length === 0) return null;

      // Nearest intersection along the ray. A vertex's centre of mass is the
      // vertex itself, which is how we read its coordinates back out.
      let best: { p: Triple; t: number } | null = null;
      for (const v of verts) {
        const p = fromVec(k.getCenterOfMass(v));
        const t =
          (p[0] - origin[0]) * d[0] + (p[1] - origin[1]) * d[1] + (p[2] - origin[2]) * d[2];
        if (t > 1e-6 && (best === null || t < best.t)) best = { p, t };
      }
      if (!best) return null;

      // Which face owns that point? The one it sits on.
      const probe = k.makeVertex(best.p[0], best.p[1], best.p[2]);
      let hitHash = -1;
      let bestDist = Infinity;
      for (const [hash, face] of facesByHash) {
        const dist = k.distanceBetween(probe, face);
        if (dist < bestDist) {
          bestDist = dist;
          hitHash = hash;
        }
        if (dist < 1e-7) break;
      }

      return {
        faceHash: hitHash,
        point: best.p,
        distance: best.t,
        queryMs: performance.now() - t0,
      };
    } finally {
      k.releaseSince(mark);
    }
  },

  /** Diagnostics for the status bar. */
  async stats(): Promise<{ shapeCount: number; loaded: boolean; boot: BootInfo | null }> {
    return {
      shapeCount: kernel ? kernel.shapeCount : 0,
      loaded: shape !== null,
      boot: bootInfo,
    };
  },
};

export type OcctApi = typeof api;

Comlink.expose(api);
