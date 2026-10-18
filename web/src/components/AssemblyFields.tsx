import { NumericInput } from "./NumericInput";
import type { AbsoluteAssemblyPlacement } from "../lib/types";

export const inputClass = "w-full rounded border border-hairline bg-chrome p-1";
export const buttonClass = "text-accent disabled:opacity-40";
export const zero: AbsoluteAssemblyPlacement = { origin: [0, 0, 0], rotation_deg: [0, 0, 0] };

export function VectorFields({ name, label, value }: { name: string; label: string; value: number[] }) {
  return <fieldset><legend>{label}</legend><div className="grid grid-cols-3 gap-1">
    {["X", "Y", "Z"].map((axis, i) => <label key={axis}>{axis}<NumericInput aria-label={`${name} ${axis}`}
      name={`${name}_${i}`} className={inputClass} required step="any" defaultValue={value[i]} /></label>)}
  </div></fieldset>;
}

export function PlacementFields({ placement, prefix = "" }: { placement: AbsoluteAssemblyPlacement; prefix?: string }) {
  return <><VectorFields name={`${prefix}origin`} label="Position (mm)" value={placement.origin} />
    <VectorFields name={`${prefix}rotation_deg`} label="Rotation (degrees, XYZ)" value={placement.rotation_deg} /></>;
}

export function readVector(data: FormData, key: string): [number, number, number] {
  const values = [0, 1, 2].map(i => {
    const text = String(data.get(`${key}_${i}`) ?? "").trim();
    if (!text || !Number.isFinite(Number(text))) throw new Error("Enter a finite number for each coordinate.");
    return Number(text);
  });
  return [values[0], values[1], values[2]];
}

export function readPlacement(data: FormData, prefix = ""): AbsoluteAssemblyPlacement {
  return { origin: readVector(data, `${prefix}origin`), rotation_deg: readVector(data, `${prefix}rotation_deg`) };
}

export function readName(data: FormData, field = "name") {
  const name = String(data.get(field) ?? "").trim();
  if (!name || name.length > 100) throw new Error("Enter a name with 1–100 characters.");
  return name;
}
