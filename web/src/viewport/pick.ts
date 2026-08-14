/**
 * Face picking against the exact B-rep.
 *
 * The worker hands over the kernel's own tessellation plus `faceGroups` —
 * `[triStart, triCount, faceHash]` per B-rep face. The triangles therefore
 * carry the kernel's face identity, and a pick is:
 *
 *     three.js raycast -> triangle index -> faceGroups lookup -> faceHash
 *
 * That is the identity OCCT would have returned, obtained without a round trip
 * to the worker. The alternative — shipping the ray to the kernel and running
 * a real B-rep intersection per click — is implemented too (`occt.raycast`)
 * and is 100-1000x slower; the viewport uses it to verify, not to interact.
 *
 * The pick mesh is deliberately NOT added to the scene. An Object3D only needs
 * its world matrix current to be raycast, so keeping it out of the graph costs
 * zero draw calls and cannot be mistaken for something the user can see.
 */
import * as THREE from "three";
import { COLORS } from "./scene";

export interface PickMeshData {
  positions: Float32Array;
  normals: Float32Array;
  indices: Uint32Array;
  faceGroups: Int32Array;
  faceCount: number;
}

export interface PickResult {
  faceHash: number;
  /** World-space hit point, in mm. This is the wire's `click_coordinate`. */
  point: THREE.Vector3;
  normal: THREE.Vector3;
  distance: number;
}

export class PickTarget {
  readonly mesh: THREE.Mesh;
  readonly faceGroups: Int32Array;
  readonly faceCount: number;
  /** Sorted triStart values, for the binary search. */
  private readonly starts: Int32Array;

  constructor(data: PickMeshData) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(data.positions, 3));
    geo.setAttribute("normal", new THREE.BufferAttribute(data.normals, 3));
    geo.setIndex(new THREE.BufferAttribute(data.indices, 1));
    geo.computeBoundingSphere();
    geo.computeBoundingBox();

    this.mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial());
    this.mesh.matrixAutoUpdate = false;
    this.mesh.updateMatrixWorld(true); // identity: world space == model space

    this.faceGroups = data.faceGroups;
    this.faceCount = data.faceCount;

    this.starts = new Int32Array(data.faceCount);
    for (let i = 0; i < data.faceCount; i++) this.starts[i] = data.faceGroups[i * 3]!;
  }

  /** Which B-rep face owns triangle `tri`? */
  faceHashOfTriangle(tri: number): number {
    // faceGroups come out of OCCT in triangle order, so this is a plain
    // binary search for the last group whose start is <= tri.
    let lo = 0;
    let hi = this.faceCount - 1;
    let found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.starts[mid]! <= tri) {
        found = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    if (found < 0) return -1;
    const start = this.faceGroups[found * 3]!;
    const count = this.faceGroups[found * 3 + 1]!;
    if (tri >= start + count) return -1;
    return this.faceGroups[found * 3 + 2]!;
  }

  /** Triangle range of a face, as [firstTriangle, triangleCount]. */
  triangleRange(faceHash: number): [number, number] | null {
    for (let i = 0; i < this.faceCount; i++) {
      if (this.faceGroups[i * 3 + 2] === faceHash) {
        return [this.faceGroups[i * 3]!, this.faceGroups[i * 3 + 1]!];
      }
    }
    return null;
  }

  pick(raycaster: THREE.Raycaster): PickResult | null {
    const hits = raycaster.intersectObject(this.mesh, false);
    const hit = hits[0];
    if (!hit || hit.faceIndex === undefined || hit.faceIndex === null) return null;

    const faceHash = this.faceHashOfTriangle(hit.faceIndex);
    if (faceHash < 0) return null;

    const normal = hit.face
      ? hit.face.normal.clone()
      : new THREE.Vector3(0, 0, 1);

    return { faceHash, point: hit.point.clone(), normal, distance: hit.distance };
  }

  /**
   * A mesh containing only the picked face's triangles — the highlight.
   * Built from the same buffers, so it sits exactly on the surface.
   */
  buildFaceOverlay(faceHash: number): THREE.Mesh | null {
    const range = this.triangleRange(faceHash);
    if (!range) return null;
    const [start, count] = range;

    const src = this.mesh.geometry;
    const srcIndex = src.getIndex()!;
    const idx = new Uint32Array(count * 3);
    for (let i = 0; i < count * 3; i++) idx[i] = srcIndex.getX(start * 3 + i);

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", src.getAttribute("position"));
    geo.setAttribute("normal", src.getAttribute("normal"));
    geo.setIndex(new THREE.BufferAttribute(idx, 1));
    geo.computeBoundingSphere();

    const mat = new THREE.MeshBasicMaterial({
      color: COLORS.highlight,
      transparent: true,
      opacity: 0.42,
      depthWrite: false,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -4,
      polygonOffsetUnits: -4,
    });

    const mesh = new THREE.Mesh(geo, mat);
    mesh.renderOrder = 2;
    return mesh;
  }

  dispose(): void {
    this.mesh.geometry.dispose();
    (this.mesh.material as THREE.Material).dispose();
  }
}

/** Pointer position -> normalised device coords for the raycaster. */
export function pointerToNdc(
  event: { clientX: number; clientY: number },
  el: HTMLElement,
  out = new THREE.Vector2(),
): THREE.Vector2 {
  const r = el.getBoundingClientRect();
  out.x = ((event.clientX - r.left) / r.width) * 2 - 1;
  out.y = -((event.clientY - r.top) / r.height) * 2 + 1;
  return out;
}
