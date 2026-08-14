/**
 * Generates the test geometry the frontend develops against, so Dev C never
 * blocks on the geometry service existing.
 *
 * Part: 100 x 100 x 20 plate, centred on the origin footprint, Z-up, with a
 * Ø10 through hole on the axis. Same part the blueprint uses in §4.
 *
 *   node scripts/make-fixtures.mjs
 *
 * Writes public/samples/plate.step and public/samples/plate.glb.
 *
 * Running this in Node is also the occt-wasm smoke test: if the kernel boots,
 * cuts, meshes, and writes STEP + GLB here, the only remaining browser-side
 * question is asset serving.
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";

import { OcctKernel } from "occt-wasm";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(here, "../public/samples");

const PLATE = { length: 100, width: 100, height: 20 };
const HOLE = { diameter: 10 };

function log(label, ms) {
  console.log(`  ${label.padEnd(28)} ${ms.toFixed(0)} ms`);
}

/**
 * OCCT's glTF writer emits model coordinates VERBATIM — a Z-up .glb, which the
 * spec says is wrong and which the real geometry service (geometry/glb.py) does
 * not produce: it maps (x, y, z) -> (x, z, -y) on the way out.
 *
 * A fixture that disagreed with the server about which way is up made the
 * viewport's single root rotation right for one of them and 90° wrong for the
 * other, so "Load fixture" and a real build could not both look correct. Rotate
 * the shape into the spec's frame before export and both producers agree.
 *
 * STEP is NOT rotated: STEP is Z-up model space, and it is what the kernel
 * picks and probes against.
 */
function toGltfFrame(kernel, shape) {
  return kernel.rotate(
    shape,
    { point: { x: 0, y: 0, z: 0 }, direction: { x: 1, y: 0, z: 0 } },
    -Math.PI / 2,
  );
}

/**
 * The mirror of `test_glb_geometry_lands_in_the_right_place_after_the_y_up_swap`
 * on the geometry side. An up-axis mistake is invisible in every number the
 * script already prints — volume, face count and the STEP bbox are all
 * unchanged by it — and only shows up as a part lying on its side in a viewport
 * nobody is looking at while running a fixture script. So assert it here.
 */
function assertGltfIsYUp(glb, expectedExtent) {
  const dv = new DataView(glb.buffer, glb.byteOffset, glb.byteLength);
  let at = 12;
  let json = null;
  while (at < glb.byteLength) {
    const len = dv.getUint32(at, true);
    const kind = dv.getUint32(at + 4, true);
    at += 8;
    if (kind === 0x4e4f534a) {
      json = JSON.parse(new TextDecoder().decode(glb.subarray(at, at + len)));
    }
    at += len + ((4 - (len % 4)) % 4);
  }
  if (!json) throw new Error("exported GLB has no JSON chunk");

  const bounds = json.accessors.filter((a) => a.min && a.max);
  const lo = [0, 1, 2].map((i) => Math.min(...bounds.map((a) => a.min[i])));
  const hi = [0, 1, 2].map((i) => Math.max(...bounds.map((a) => a.max[i])));
  const extent = [0, 1, 2].map((i) => hi[i] - lo[i]);

  console.log(`  glTF extent  ${extent.map((v) => v.toFixed(1)).join(" x ")} (Y-up)`);
  for (let i = 0; i < 3; i++) {
    if (Math.abs(extent[i] - expectedExtent[i]) > 0.1) {
      throw new Error(
        `glTF is not Y-up: extent ${extent.map((v) => v.toFixed(1)).join(" x ")}, ` +
          `expected ${expectedExtent.join(" x ")}. The viewport applies exactly one ` +
          `root rotation and this file would arrive 90° out from a server build.`,
      );
    }
  }
}

async function main() {
  mkdirSync(outDir, { recursive: true });

  let t = performance.now();
  const kernel = await OcctKernel.init();
  log("kernel boot", performance.now() - t);

  // Plate: makeBox builds from the origin corner, so shift it so the part is
  // centred in X/Y with its bottom face on z = 0 — the ledger's Placement
  // convention (origin = centre of footprint at the base).
  t = performance.now();
  const plate0 = kernel.makeBox(PLATE.length, PLATE.width, PLATE.height);
  const plate = kernel.translate(plate0, -PLATE.length / 2, -PLATE.width / 2, 0);

  // Through hole on the axis. Overshoot both faces so the cut is clean.
  const drill0 = kernel.makeCylinder(HOLE.diameter / 2, PLATE.height + 4);
  const drill = kernel.translate(drill0, 0, 0, -2);

  const cutCompound = kernel.cut(plate, drill);
  const [part] = kernel.getSubShapes(cutCompound, "solid");
  if (!part) throw new Error("boolean produced no solid");
  log("box + cylinder + cut", performance.now() - t);

  // ---- sanity: the numbers have to come out right -----------------------
  const volume = kernel.getVolume(part);
  const expected =
    PLATE.length * PLATE.width * PLATE.height -
    Math.PI * (HOLE.diameter / 2) ** 2 * PLATE.height;
  const bbox = kernel.getBoundingBox(part);
  const faces = kernel.getSubShapes(part, "face");
  console.log(
    `  volume ${volume.toFixed(1)} mm³ (expected ${expected.toFixed(1)}), ` +
      `${faces.length} faces`,
  );
  console.log(
    `  bbox   x[${bbox.xmin}, ${bbox.xmax}] y[${bbox.ymin}, ${bbox.ymax}] z[${bbox.zmin}, ${bbox.zmax}]`,
  );
  if (Math.abs(volume - expected) / expected > 0.001) {
    throw new Error("volume is wrong — the cut did not do what we think");
  }

  // ---- the picking primitive: per-face triangle groups -------------------
  t = performance.now();
  const mesh = kernel.meshShape(part, { linearDeflection: 0.1, angularDeflection: 0.3 });
  log("meshShape", performance.now() - t);
  console.log(
    `  ${mesh.triangleCount} triangles, ${mesh.vertexCount} verts, ` +
      `${mesh.faceCount} face groups, faceGroups=${mesh.faceGroups ? "yes" : "NO"}`,
  );
  if (!mesh.faceGroups || !mesh.faceCount) {
    throw new Error("no faceGroups — face picking would have no identity to return");
  }

  // ---- STEP -------------------------------------------------------------
  t = performance.now();
  const step = kernel.exportStep(part);
  log("exportStep", performance.now() - t);
  writeFileSync(resolve(outDir, "plate.step"), step);

  // ---- GLB (stand-in for what the geometry service will stream) ---------
  t = performance.now();
  const doc = kernel.createXCAFDocument();
  doc.addShape(toGltfFrame(kernel, part), { name: "Base Plate", color: [0.62, 0.68, 0.74] });
  const glb = doc.exportGLTF({ linearDeflection: 0.1, angularDeflection: 0.3 });
  doc.close();
  log("exportGLTF", performance.now() - t);
  assertGltfIsYUp(glb, [PLATE.length, PLATE.height, PLATE.width]);
  writeFileSync(resolve(outDir, "plate.glb"), glb);

  // ---- a second revision, to exercise hot-swap --------------------------
  // "Make this twice as thick" — the blueprint's own example edit.
  const thick0 = kernel.makeBox(PLATE.length, PLATE.width, PLATE.height * 2);
  const thick = kernel.translate(thick0, -PLATE.length / 2, -PLATE.width / 2, 0);
  const drill2 = kernel.translate(
    kernel.makeCylinder(HOLE.diameter / 2, PLATE.height * 2 + 4),
    0,
    0,
    -2,
  );
  const [thickPart] = kernel.getSubShapes(kernel.cut(thick, drill2), "solid");
  writeFileSync(resolve(outDir, "plate-thick.step"), kernel.exportStep(thickPart));
  const doc2 = kernel.createXCAFDocument();
  doc2.addShape(toGltfFrame(kernel, thickPart), { name: "Base Plate", color: [0.62, 0.68, 0.74] });
  writeFileSync(
    resolve(outDir, "plate-thick.glb"),
    doc2.exportGLTF({ linearDeflection: 0.1, angularDeflection: 0.3 }),
  );
  doc2.close();

  kernel.dispose?.();

  console.log(`\n  wrote ${outDir}/plate.{step,glb} and plate-thick.{step,glb}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
