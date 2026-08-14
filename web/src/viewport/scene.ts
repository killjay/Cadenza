/**
 * The three.js scene. Framework-free on purpose: React owns the DOM around the
 * canvas, this owns everything inside it, and the boundary is the small
 * imperative surface at the bottom of the file.
 *
 * COORDINATE SYSTEM — Z-UP, MILLIMETRES.
 * The ledger is Z-up (contracts/ledger.py); glTF is Y-up, and our writer
 * (geometry/glb.py) honours the spec — it maps (x, y, z) -> (x, z, -y) on the
 * way out, which `test_glb_geometry_lands_in_the_right_place_after_the_y_up_swap`
 * pins. So the GLB arrives rotated and something has to undo it.
 *
 * That something is `modelRoot`, and only `modelRoot`: it carries the single
 * root rotation CONTRACTS.md promises ("glTF is Y-up; the frontend converts at
 * the Three.js boundary and nowhere else"). Everything else in the scene —
 * camera, grid, overlays, the STEP-derived pick mesh and wireframe — lives in
 * ledger coordinates, so world space IS model space and a click's world
 * coordinate goes on the wire as-is.
 *
 * The rotation belongs on the root and not in the GLB writer because the writer
 * also feeds `Open GLB…` and any external viewer, where a Z-up glTF would be
 * the file that is wrong.
 */
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";

export const COLORS = {
  bgTop: 0x1a1d23,
  bgBottom: 0x101216,
  gridFine: 0x2a2f38,
  gridCoarse: 0x39414d,
  axisX: 0x9c4b5c,
  axisY: 0x4b7a4f,
  edge: 0x0d0f13,
  highlight: 0x78c2ff,
} as const;

export interface Viewport {
  renderer: THREE.WebGLRenderer;
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  controls: OrbitControls;
  /**
   * Everything model-shaped hangs off here so a swap is one subtree — and so
   * the glTF Y-up -> ledger Z-up rotation is one node's transform.
   */
  modelRoot: THREE.Group;
  /** Overlays that must survive a model swap (highlight, hover). */
  overlayRoot: THREE.Group;
  raycaster: THREE.Raycaster;
  /** Frame the camera on a bounding box. Only called explicitly. */
  frame: (box: THREE.Box3, animate?: boolean) => void;
  /** Move the ground plane and grid to sit under the part. */
  setGroundLevel: (z: number) => void;
  setGridVisible: (on: boolean) => void;
  resize: () => void;
  dispose: () => void;
}

function makeGrid(): THREE.Group {
  const g = new THREE.Group();

  // GridHelper lies in XZ; rotate it into XY so it is the Z-up ground plane.
  const fine = new THREE.GridHelper(1000, 100, COLORS.gridFine, COLORS.gridFine);
  fine.rotation.x = Math.PI / 2;
  const fineMat = fine.material as THREE.LineBasicMaterial;
  fineMat.transparent = true;
  fineMat.opacity = 0.5;
  fineMat.fog = true;

  const coarse = new THREE.GridHelper(1000, 10, COLORS.gridCoarse, COLORS.gridCoarse);
  coarse.rotation.x = Math.PI / 2;
  const coarseMat = coarse.material as THREE.LineBasicMaterial;
  coarseMat.transparent = true;
  coarseMat.opacity = 0.75;
  coarseMat.fog = true;

  // X and Y axes, tinted so the user can tell which way is which.
  const axis = (color: number, dir: THREE.Vector3): THREE.Line => {
    const geo = new THREE.BufferGeometry().setFromPoints([
      dir.clone().multiplyScalar(-500),
      dir.clone().multiplyScalar(500),
    ]);
    const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.65, fog: true });
    return new THREE.Line(geo, mat);
  };

  g.add(fine, coarse, axis(COLORS.axisX, new THREE.Vector3(1, 0, 0)));
  g.add(axis(COLORS.axisY, new THREE.Vector3(0, 1, 0)));
  g.renderOrder = -1;
  return g;
}

export function createViewport(container: HTMLElement): Viewport {
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: false,
    powerPreference: "high-performance",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);
  renderer.domElement.style.display = "block";
  renderer.domElement.style.touchAction = "none";

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(COLORS.bgBottom);
  // Fades the grid out at range instead of letting it stretch to a hard edge.
  scene.fog = new THREE.Fog(COLORS.bgBottom, 400, 1400);

  // A room environment gives the GLB's PBR materials something to reflect.
  // Without it a metal-ish part reads as flat grey; with it, it reads as metal.
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envRT = pmrem.fromScene(new RoomEnvironment(), 0.04);
  scene.environment = envRT.texture;
  scene.environmentIntensity = 0.55;
  pmrem.dispose();

  const camera = new THREE.PerspectiveCamera(
    38,
    container.clientWidth / Math.max(container.clientHeight, 1),
    0.5,
    5000,
  );
  camera.up.set(0, 0, 1); // Z-up
  camera.position.set(180, -220, 150);

  // Key light casts the shadow; the rest is fill so nothing goes pure black.
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(120, -160, 220);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.near = 1;
  key.shadow.camera.far = 900;
  key.shadow.camera.left = -260;
  key.shadow.camera.right = 260;
  key.shadow.camera.top = 260;
  key.shadow.camera.bottom = -260;
  key.shadow.bias = -0.0008;
  key.shadow.normalBias = 0.4;
  scene.add(key);

  const fill = new THREE.DirectionalLight(0x9fc4ff, 0.6);
  fill.position.set(-180, 120, 60);
  scene.add(fill);

  const rim = new THREE.DirectionalLight(0xffd9a8, 0.35);
  rim.position.set(0, 200, -120);
  scene.add(rim);

  scene.add(new THREE.HemisphereLight(0xdfe9ff, 0x20242c, 0.5));

  const grid = makeGrid();
  scene.add(grid);

  // Shadow catcher — a plane that is invisible except where shadow lands.
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(2000, 2000),
    new THREE.ShadowMaterial({ opacity: 0.38 }),
  );
  ground.receiveShadow = true;
  scene.add(ground);

  // THE single axis conversion in the frontend. glTF hands us (x, z, -y) of the
  // ledger point; +90° about X sends it back to (x, y, z). Children of
  // `modelRoot` are therefore in ledger coordinates once their world matrix is
  // applied — which is what `swapModel` measures the bounding box in.
  const modelRoot = new THREE.Group();
  modelRoot.rotation.x = Math.PI / 2;
  scene.add(modelRoot);

  // NOT rotated: overlays are built from the STEP-derived pick mesh, which the
  // kernel already gives us in ledger coordinates.
  const overlayRoot = new THREE.Group();
  scene.add(overlayRoot);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.09;
  controls.rotateSpeed = 0.7;
  controls.zoomSpeed = 0.9;
  controls.panSpeed = 0.8;
  controls.screenSpacePanning = true;
  controls.minDistance = 5;
  controls.maxDistance = 2500;
  controls.target.set(0, 0, 10);
  controls.update();

  const raycaster = new THREE.Raycaster();

  /* ---------------- render loop ---------------- */

  let running = true;
  const tick = (): void => {
    if (!running) return;
    requestAnimationFrame(tick);
    controls.update();
    renderer.render(scene, camera);
  };
  tick();

  /* ---------------- camera framing ---------------- */

  function frame(box: THREE.Box3, animate = true): void {
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.length() / 2, 1);

    const fov = THREE.MathUtils.degToRad(camera.fov);
    const dist = (radius / Math.sin(fov / 2)) * 1.25;

    // Keep the current view direction — framing should not also re-orient.
    const dir = camera.position.clone().sub(controls.target);
    if (dir.lengthSq() < 1e-6) dir.set(1, -1.2, 0.85);
    dir.normalize();

    const toPos = center.clone().addScaledVector(dir, dist);

    camera.near = Math.max(dist / 500, 0.1);
    camera.far = dist * 20;
    camera.updateProjectionMatrix();

    if (!animate) {
      camera.position.copy(toPos);
      controls.target.copy(center);
      controls.update();
      return;
    }

    const fromPos = camera.position.clone();
    const fromTarget = controls.target.clone();
    const t0 = performance.now();
    const dur = 420;
    const ease = (x: number): number => 1 - Math.pow(1 - x, 3);
    const step = (): void => {
      const t = Math.min((performance.now() - t0) / dur, 1);
      const e = ease(t);
      camera.position.lerpVectors(fromPos, toPos, e);
      controls.target.lerpVectors(fromTarget, center, e);
      controls.update();
      if (t < 1) requestAnimationFrame(step);
    };
    step();
  }

  function setGroundLevel(z: number): void {
    ground.position.z = z - 0.05;
    grid.position.z = z - 0.05;
  }

  function resize(): void {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (w === 0 || h === 0) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }

  function dispose(): void {
    running = false;
    controls.dispose();
    envRT.dispose();
    scene.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose?.();
      const mat = m.material;
      if (Array.isArray(mat)) mat.forEach((x) => x.dispose());
      else mat?.dispose?.();
    });
    renderer.dispose();
    renderer.domElement.remove();
  }

  return {
    renderer,
    scene,
    camera,
    controls,
    modelRoot,
    overlayRoot,
    raycaster,
    frame,
    setGroundLevel,
    setGridVisible: (on) => {
      grid.visible = on;
      ground.visible = on;
    },
    resize,
    dispose,
  };
}
