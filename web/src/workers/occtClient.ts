/**
 * Main-thread handle on the OCCT worker.
 *
 * One worker for the whole app — the kernel is single-threaded and holds the
 * part, so a second one would just be a second copy of the truth.
 *
 * If occt-wasm ever has to be swapped for server-side picking, this module and
 * `occt.worker.ts` are the only two files that change: everything upstream
 * talks to `occt` through the interface below, which is already async and
 * already coordinate-based.
 */
import * as Comlink from "comlink";
import type {
  BootInfo,
  FaceFacts,
  ModelSummary,
  OcctApi,
  PickMesh,
  RayHit,
  WireframeData,
} from "./occt.worker";

export type { BootInfo, FaceFacts, ModelSummary, PickMesh, RayHit, WireframeData };

let worker: Worker | null = null;
let proxy: Comlink.Remote<OcctApi> | null = null;

function get(): Comlink.Remote<OcctApi> {
  if (proxy) return proxy;
  worker = new Worker(new URL("./occt.worker.ts", import.meta.url), {
    type: "module",
    name: "cadenza-occt",
  });
  proxy = Comlink.wrap<OcctApi>(worker);
  return proxy;
}

export const occt = {
  boot: (): Promise<BootInfo> => get().boot(),

  loadStep: (data: ArrayBuffer): Promise<ModelSummary> =>
    get().loadStep(Comlink.transfer(data, [data])),

  getPickMesh: (linearDeflection?: number, angularDeflection?: number): Promise<PickMesh> =>
    get().getPickMesh(linearDeflection, angularDeflection),

  getWireframe: (deflection?: number): Promise<WireframeData> => get().getWireframe(deflection),

  faceFacts: (faceHash: number, near?: [number, number, number]): Promise<FaceFacts> =>
    get().faceFacts(faceHash, near),

  raycast: (
    origin: [number, number, number],
    direction: [number, number, number],
  ): Promise<RayHit | null> => get().raycast(origin, direction),

  stats: () => get().stats(),

  terminate(): void {
    worker?.terminate();
    worker = null;
    proxy = null;
  },
};
