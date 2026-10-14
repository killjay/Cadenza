import type { EntitySeed, ProfileEntity } from "./types";

export interface Point2 { x: number; y: number; }
export interface SketchView extends Point2 { scale: number; }
export interface CanvasSize { width: number; height: number; }
export type SketchTool = "select" | "rectangle" | "circle" | "opening";

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
export function screenToSketch(point: Point2, view: SketchView, size: CanvasSize): Point2 {
  return { x: view.x + (point.x - size.width / 2) / view.scale,
    y: view.y - (point.y - size.height / 2) / view.scale };
}
export function sketchToScreen(point: Point2, view: SketchView, size: CanvasSize): Point2 {
  return { x: size.width / 2 + (point.x - view.x) * view.scale,
    y: size.height / 2 - (point.y - view.y) * view.scale };
}
export function panSketch(view: SketchView, dx: number, dy: number): SketchView {
  return { ...view, x: clamp(view.x - dx / view.scale, -4e6, 4e6), y: clamp(view.y + dy / view.scale, -4e6, 4e6) };
}
export function zoomSketch(view: SketchView, anchor: Point2, factor: number, size: CanvasSize): SketchView {
  const before = screenToSketch(anchor, view, size);
  const scale = clamp(view.scale * factor, 1e-5, 1e7);
  return { scale, x: clamp(before.x - (anchor.x - size.width / 2) / scale, -4e6, 4e6),
    y: clamp(before.y + (anchor.y - size.height / 2) / scale, -4e6, 4e6) };
}
export function fitSketch(entities: ProfileEntity[], size: CanvasSize): SketchView {
  if (!entities.length) return { x: 0, y: 0, scale: Math.max(1e-5, Math.min(size.width / 200, size.height / 140)) };
  const left = Math.min(...entities.map(e => e.x - (e.width ?? e.diameter ?? 0) / 2));
  const right = Math.max(...entities.map(e => e.x + (e.width ?? e.diameter ?? 0) / 2));
  const bottom = Math.min(...entities.map(e => e.y - (e.height ?? e.diameter ?? 0) / 2));
  const top = Math.max(...entities.map(e => e.y + (e.height ?? e.diameter ?? 0) / 2));
  return { x: (left + right) / 2, y: (top + bottom) / 2,
    scale: clamp(Math.min(Math.max(1, size.width - 180) / (right - left), Math.max(1, size.height - 180) / (top - bottom)), 1e-5, 1e7) };
}
export function gridSpacing(scale: number): number {
  const target = 70 / scale, power = 10 ** Math.floor(Math.log10(target));
  return [1, 2, 5, 10].map(n => n * power).find(n => n >= target) ?? power * 10;
}
export function drawnEntity(tool: Exclude<SketchTool, "select">, a: Point2, b: Point2): EntitySeed {
  const entity: EntitySeed = tool === "rectangle"
    ? { kind: "rectangle", x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, width: Math.abs(b.x - a.x), height: Math.abs(b.y - a.y) }
    : { kind: "circle", operation: tool === "opening" ? "subtract" : "add", x: a.x, y: a.y, diameter: 2 * Math.hypot(b.x - a.x, b.y - a.y) };
  const dimensions = entity.kind === "rectangle" ? [entity.width, entity.height] : [entity.diameter];
  if (![a.x, a.y, b.x, b.y].every(Number.isFinite)
    || ![entity.x!, entity.y!].every(n => Math.abs(n) <= 1e6)
    || !dimensions.every(n => Number.isFinite(n) && n > 1e-6 && n <= 1e6))
    throw new Error("Choose a nonzero size above 0.000001 mm and at most 1,000,000 mm, within the supported coordinate range.");
  return entity;
}
