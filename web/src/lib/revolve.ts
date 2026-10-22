import type { Feature } from "./types";

export function revolveReason(feature: Feature): string | null {
  if (feature.kind !== "revolve") return "Choose a revolve feature.";
  if (feature.placement.relative_to != null) return "Relative placement is not supported by these controls.";
  if (JSON.stringify(feature.parameters.axis) !== "[0,1,0]")
    return "Legacy revolve: geometry is preserved; these controls require local +Y.";
  try { readRevolve(feature.parameters.vertices.map(p => p.map(String)), String(feature.parameters.angle)); }
  catch { return "This legacy profile is outside the supported radius/angle limits."; }
  return null;
}

export function readRevolve(rows: string[][], angleText: string): { vertices: [number, number][]; angle: number } {
  const number = (text: string) => {
    if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(text.trim()) || !Number.isFinite(Number(text)))
      throw Error("Enter finite numeric coordinates and angle.");
    return Number(text);
  };
  if (rows.length < 3 || rows.length > 64) throw Error("Use 3 to 64 distinct corners. Closure is automatic.");
  const vertices = rows.map(row => {
    if (row.length !== 2) throw Error("Each corner needs radius and axial distance.");
    return row.map(number) as [number, number];
  });
  if (vertices.some(([r, y]) => r < 0 || r > 1e6 || Math.abs(y) > 1e6))
    throw Error("Radius must be 0 to 1,000,000 mm; axial distance must be within ±1,000,000 mm.");
  if (new Set(vertices.map(p => p.join(","))).size !== vertices.length) throw Error("Remove duplicate corners; closure is automatic.");
  const angle = number(angleText);
  if (angle <= 0 || angle > 360) throw Error("Angle must be greater than 0 and at most 360 degrees.");
  return { vertices, angle };
}
