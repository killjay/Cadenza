import type { Feature, GearSettings, Ledger } from "./types";

export function readGear(fields: Record<string, string>): GearSettings {
  const number = (key: string, min: number, max: number) => {
    const text = fields[key]?.trim() ?? "";
    const value = Number(text);
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text)
      || !Number.isFinite(value) || value < min || value > max) throw Error(`Enter ${key} between ${min} and ${max}.`);
    return value;
  };
  const settings = { module: number("module", 1e-7, 1000), teeth: number("teeth", 3, 128),
    height: number("height", 1e-7, 1e6), pressure_angle_deg: number("pressure_angle_deg", 5, 45), shift: number("shift", -1, 1) };
  if (!Number.isInteger(settings.teeth)) throw Error("Tooth count must be a whole number.");
  if (settings.module <= 1e-7 || settings.height <= 1e-7) throw Error("Module and width must exceed 0.0000001 mm.");
  return settings;
}

export function gearReason(feature: Feature, ledger?: Ledger): string | null {
  if (feature.kind !== "gear" || feature.operation !== "add") return "These controls support additive external spur gears only.";
  if (feature.placement.relative_to || feature.placement.rotation_deg.slice(0, 2).some(v => v !== 0))
    return "Gear controls require absolute placement and rotation about source Z only.";
  if (ledger && ledger.features.filter(f => f.kind === "gear" && !f.suppressed).length > 1)
    return "These controls support one active gear per source.";
  try { readGear(Object.fromEntries(Object.entries(feature.parameters).map(([k, v]) => [k, String(v)]))); }
  catch { return "This legacy gear is outside supported settings; its geometry is preserved."; }
  return null;
}
