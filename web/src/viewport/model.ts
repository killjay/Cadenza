/**
 * GLB loading and hot-swap (blueprint §5 step 8).
 *
 * THE RULE: swapping the model NEVER touches the camera or the controls.
 * An edit that re-frames the view reads as the tool losing its place, and for
 * a non-technical user that is indistinguishable from the tool being broken.
 * The camera moves on first load, and when the user asks (Fit / F). That is all.
 *
 * The swap itself is: parse the new GLB fully, and only then take the old one
 * out. Parsing takes a few ms and is async — tearing down first would show a
 * hole in the viewport on every edit.
 */
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import type { Viewport } from "./scene";

const loader = new GLTFLoader();

export interface LoadedModel {
  root: THREE.Object3D;
  /**
   * Bounds in the GLB's OWN frame — glTF Y-up, not ledger coordinates. The
   * frame the camera and the ground plane care about only exists once the root
   * is under `vp.modelRoot` and its rotation applies, so `swapModel` remeasures
   * there. Do not feed this box to anything that thinks in mm and Z.
   */
  box: THREE.Box3;
  triangleCount: number;
  parseMs: number;
}

export function disposeSubtree(root: THREE.Object3D): void {
  root.traverse((o) => {
    const m = o as THREE.Mesh;
    if (m.geometry) m.geometry.dispose();
    const mat = m.material;
    if (Array.isArray(mat)) mat.forEach((x) => x.dispose());
    else if (mat) (mat as THREE.Material).dispose();
  });
  root.removeFromParent();
}

function stringToColor(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  const h = Math.abs(hash) % 360;
  const s = 60 + (Math.abs(hash) % 20);
  const l = 40 + (Math.abs(hash >> 8) % 20);
  return new THREE.Color().setHSL(h / 360, s / 100, l / 100).getHex();
}

export function parseGlb(data: ArrayBuffer): Promise<LoadedModel> {
  const t0 = performance.now();
  return new Promise((resolve, reject) => {
    loader.parse(
      data,
      "",
      (gltf) => {
        const root = gltf.scene;
        let triangleCount = 0;

        root.traverse((o) => {
          const mesh = o as THREE.Mesh;
          if (!mesh.isMesh) return;
          mesh.castShadow = true;
          mesh.receiveShadow = true;

          const index = mesh.geometry.getIndex();
          triangleCount += (index ? index.count : mesh.geometry.getAttribute("position").count) / 3;

          const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
          const newMats = [];
          for (const m of mats) {
            const std = (m as THREE.MeshStandardMaterial).clone();
            std.side = THREE.FrontSide;
            std.envMapIntensity = 1.0;
            if (std.metalness !== undefined && std.metalness < 0.05) std.metalness = 0.25;
            if (std.roughness !== undefined && std.roughness > 0.9) std.roughness = 0.45;

            // Assign a unique color per component (or feature), unless it's a hole or unattributed
            const componentId = mesh.userData?.component_id as string | undefined;
            const featureId = mesh.userData?.feature_id as string | undefined;
            const colorSource = componentId || featureId;
            if (colorSource && !colorSource.includes("_hole_") && colorSource !== "_unattributed") {
              std.color.setHex(stringToColor(colorSource));
            }
            newMats.push(std);
          }
          mesh.material = newMats.length === 1 ? newMats[0] : newMats;
        });

        root.updateMatrixWorld(true);
        const box = new THREE.Box3().setFromObject(root);
        resolve({ root, box, triangleCount, parseMs: performance.now() - t0 });
      },
      (err) => reject(err instanceof Error ? err : new Error(String(err))),
    );
  });
}

/**
 * Puts a parsed model into the viewport in place of whatever is there.
 * `isFirst` is the only thing that decides whether the camera moves.
 */
export function swapModel(vp: Viewport, model: LoadedModel, isFirst: boolean): void {
  const previous = vp.modelRoot.children.slice();

  vp.modelRoot.add(model.root);
  for (const old of previous) disposeSubtree(old);

  // Measure AFTER parenting: `modelRoot` carries the glTF Y-up -> ledger Z-up
  // rotation, so only a world-space box has a meaningful `.z` to stand the
  // ground plane on or to point a Z-up camera at.
  model.root.updateMatrixWorld(true);
  const worldBox = new THREE.Box3().setFromObject(model.root);

  vp.setGroundLevel(worldBox.min.z);
  if (isFirst) vp.frame(worldBox, false);
}

/** Exact B-rep edges from the worker, drawn as the CAD outline over the shading. */
export function buildWireframe(points: Float32Array, edgeGroups: Int32Array): THREE.LineSegments {
  // edgeGroups is [pointStart, pointCount, edgeHash] per edge, where the counts
  // are in floats. Each edge is a polyline; expand it into line segments.
  const segs: number[] = [];
  for (let e = 0; e + 2 < edgeGroups.length; e += 3) {
    const start = edgeGroups[e]!;
    const count = edgeGroups[e + 1]!;
    for (let i = 0; i + 5 < count; i += 3) {
      const a = start + i;
      segs.push(
        points[a]!,
        points[a + 1]!,
        points[a + 2]!,
        points[a + 3]!,
        points[a + 4]!,
        points[a + 5]!,
      );
    }
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(segs, 3));
  const mat = new THREE.LineBasicMaterial({
    color: 0x0b0d11,
    transparent: true,
    opacity: 0.55,
    depthTest: true,
  });
  const lines = new THREE.LineSegments(geo, mat);
  // Pull the outline a hair toward the camera so it is not z-fought by the
  // surface it traces.
  lines.renderOrder = 1;
  mat.polygonOffset = true;
  mat.polygonOffsetFactor = -2;
  mat.polygonOffsetUnits = -2;
  return lines;
}
