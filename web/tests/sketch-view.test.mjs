import test from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./load-module.mjs";

const view = await loadModule("../src/lib/sketchView.ts");
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-8, `${a} != ${b}`);

test("sketch coordinates roundtrip with Y up across pan, zoom and resize", () => {
  for (const size of [{ width: 900, height: 600 }, { width: 1300, height: 850 }]) {
    const camera = { x: 18, y: -35, scale: 4 };
    const point = { x: -70, y: 22 };
    const screen = view.sketchToScreen(point, camera, size);
    const restored = view.screenToSketch(screen, camera, size);
    near(restored.x, point.x); near(restored.y, point.y);
    assert.ok(view.sketchToScreen({ x: 18, y: -34 }, camera, size).y < size.height / 2);
    const zoomed = view.zoomSketch(camera, screen, 1.7, size);
    const anchored = view.screenToSketch(screen, zoomed, size);
    near(anchored.x, point.x); near(anchored.y, point.y);
    const moved = view.sketchToScreen(point, view.panSketch(camera, 36, -20), size);
    near(moved.x, screen.x + 36); near(moved.y, screen.y - 20);
  }
});

test("fit handles empty sketches, off-origin holes and extreme supported scales", () => {
  const size = { width: 900, height: 600 };
  const empty = view.fitSketch([], size);
  assert.ok(Number.isFinite(empty.scale) && empty.scale > 0);
  const entities = [
    { id: "ent_outer", kind: "rectangle", x: 0, y: 0, width: 100, height: 60 },
    { id: "ent_hole", kind: "circle", x: 200, y: 100, diameter: 20 },
  ];
  const fitted = view.fitSketch(entities, size);
  for (const p of [{ x: -50, y: -30 }, { x: 210, y: 110 }]) {
    const screen = view.sketchToScreen(p, fitted, size);
    assert.ok(screen.x >= 89 && screen.x <= size.width - 89);
    assert.ok(screen.y >= 89 && screen.y <= size.height - 89);
  }
  for (const diameter of [0.000002, 1e6]) {
    const fit = view.fitSketch([{ kind: "circle", x: 1e6, y: -1e6, diameter }], size);
    assert.ok(Number.isFinite(fit.scale) && fit.scale >= 1e-5 && fit.scale <= 1e7);
    const grid = view.gridSpacing(fit.scale) * fit.scale;
    assert.ok(grid >= 70 && grid <= 175);
  }
  assert.equal(view.zoomSketch(empty, { x: 0, y: 0 }, 1e30, size).scale, 1e7);
  assert.equal(view.zoomSketch(empty, { x: 0, y: 0 }, 1e-30, size).scale, 1e-5);
  assert.equal(view.panSketch(empty, 1e30, -1e30).x, -4e6);
});

test("drawing converts corners and radii to server seeds without minting IDs", () => {
  for (const [a, b] of [[{ x: -50, y: -30 }, { x: 50, y: 30 }], [{ x: 50, y: 30 }, { x: -50, y: -30 }]]) {
    const result = view.drawnEntity("rectangle", a, b);
    assert.equal(result.width, 100); assert.equal(result.height, 60);
    assert.equal(result.x, 0); assert.equal(result.y, 0);
    assert.equal(result.id, undefined);
  }
  const hole = view.drawnEntity("opening", { x: 10, y: 20 }, { x: 13, y: 24 });
  assert.equal(hole.diameter, 10); assert.equal(hole.operation, "subtract");
  assert.equal(view.drawnEntity("circle", { x: 0, y: 0 }, { x: 10, y: 0 }).operation, "add");
  assert.equal(view.drawnEntity("circle", { x: 1e6, y: 0 }, { x: 1.5e6, y: 0 }).diameter, 1e6);
  for (const end of [{ x: 0, y: 0 }, { x: Infinity, y: 1 }, { x: 1e7, y: 1 }, { x: 1e-8, y: 0 }])
    assert.throws(() => view.drawnEntity("circle", { x: 0, y: 0 }, end));
  assert.throws(() => view.drawnEntity("rectangle", { x: 0, y: 0 }, { x: 100, y: 0 }));
});
