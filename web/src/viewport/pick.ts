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
    for (let i = 0; i < data.faceCount; i++) {
      // faceGroups is [indexStart, indexCount, faceHash]
      // We convert indexStart to triStart for the binary search
      this.starts[i] = data.faceGroups[i * 3]! / 3;
    }
  }

  /** Which B-rep face owns triangle `tri`? */
  faceHashOfTriangle(tri: number): number {
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
    const triStart = this.starts[found]!;
    const triCount = this.faceGroups[found * 3 + 1]! / 3;
    if (tri >= triStart + triCount) return -1;
    return this.faceGroups[found * 3 + 2]!;
  }

  /** Triangle range of a face, as [triStart, triCount]. */
  triangleRange(faceHash: number): [number, number] | null {
    for (let i = 0; i < this.faceCount; i++) {
      if (this.faceGroups[i * 3 + 2] === faceHash) {
        return [this.faceGroups[i * 3]!, this.faceGroups[i * 3 + 1]!];
      }
    }
    return null;
  }

  pick(raycaster: THREE.Raycaster): PickResult | null {
    console.log('faceGroups:', this.faceGroups);
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

  buildFaceOverlay(faceHash: number): THREE.Mesh | null {
    const range = this.triangleRange(faceHash);
    if (!range) return null;
    const [indexStart, indexCount] = range;

    const src = this.mesh.geometry;
    const srcIndex = src.getIndex()!;
    
    const idx = new Uint32Array(indexCount);
    for (let i = 0; i < indexCount; i++) idx[i] = srcIndex.getX(indexStart + i);

    const geo = new THREE.BufferGeometry();
    // Clone the attributes so that disposing this overlay mesh does not
    // corrupt the shared pick-mesh buffers (which would break subsequent
    // buildFaceOverlay calls for the selection highlight).
    geo.setAttribute("position", (src.getAttribute("position") as THREE.BufferAttribute).clone());
    geo.setAttribute("normal", (src.getAttribute("normal") as THREE.BufferAttribute).clone());
    geo.setIndex(new THREE.BufferAttribute(idx, 1));
    geo.computeBoundingSphere();

    const mat = new THREE.MeshBasicMaterial({
      color: COLORS.highlight,
      transparent: true,
      opacity: 0.6,
      depthWrite: false,
      side: THREE.FrontSide,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -2,
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
