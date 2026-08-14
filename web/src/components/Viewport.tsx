/**
 * The React <-> three.js boundary.
 *
 * React mounts the canvas once and never re-renders it. Everything that
 * changes per frame lives behind a ref; everything the UI needs to draw goes
 * up into the store. Geometry arrives via window events rather than through
 * React state so a 9 MB ArrayBuffer never becomes a render dependency.
 */
import { useEffect, useRef } from "react";
import * as THREE from "three";
import { createViewport } from "../viewport/scene";
import type { Viewport as Vp } from "../viewport/scene";
import { buildWireframe, parseGlb, swapModel } from "../viewport/model";
import { PickTarget, pointerToNdc } from "../viewport/pick";
import type { PickMeshData } from "../viewport/pick";
import { occt } from "../workers/occtClient";
import { useStore } from "../lib/store";
import type { Vec3 } from "../lib/types";

/** Drag threshold, in px, above which a pointerup is an orbit and not a click. */
const CLICK_SLOP = 4;

export function Viewport() {
  const hostRef = useRef<HTMLDivElement>(null);
  const vpRef = useRef<Vp | null>(null);
  const pickRef = useRef<PickTarget | null>(null);
  const highlightRef = useRef<THREE.Mesh | null>(null);
  const wireRef = useRef<THREE.LineSegments | null>(null);
  const firstLoadRef = useRef(true);

  const showGrid = useStore((s) => s.showGrid);
  const showEdges = useStore((s) => s.showEdges);
  const selection = useStore((s) => s.selection);

  /* ---------------- mount ---------------- */

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const vp = createViewport(host);
    vpRef.current = vp;

    const ro = new ResizeObserver(() => vp.resize());
    ro.observe(host);

    return () => {
      ro.disconnect();
      pickRef.current?.dispose();
      pickRef.current = null;
      vp.dispose();
      vpRef.current = null;
    };
  }, []);

  /* ---------------- geometry in ---------------- */

  useEffect(() => {
    const onGlb = async (ev: Event): Promise<void> => {
      const vp = vpRef.current;
      if (!vp) return;
      const { glb } = (ev as CustomEvent<{ glb: ArrayBuffer; revision: number }>).detail;

      const model = await parseGlb(glb);
      const isFirst = firstLoadRef.current;
      firstLoadRef.current = false;

      // THE hot-swap: parse first, swap second, camera untouched unless first.
      swapModel(vp, model, isFirst);

      useStore.setState((s) => ({
        model: s.model
          ? { ...s.model, parseMs: Math.round(model.parseMs), triangleCount: model.triangleCount }
          : s.model,
      }));

      // The old highlight belonged to the old model.
      clearHighlight();
    };

    const onPickMesh = async (ev: Event): Promise<void> => {
      const vp = vpRef.current;
      if (!vp) return;
      const data = (ev as CustomEvent<PickMeshData>).detail;

      pickRef.current?.dispose();
      pickRef.current = new PickTarget(data);

      // Exact B-rep outline, straight from the kernel.
      try {
        const wf = await occt.getWireframe(0.05);
        if (wireRef.current) {
          wireRef.current.geometry.dispose();
          (wireRef.current.material as THREE.Material).dispose();
          wireRef.current.removeFromParent();
        }
        const lines = buildWireframe(wf.points, wf.edgeGroups);
        lines.visible = useStore.getState().showEdges;
        vp.overlayRoot.add(lines);
        wireRef.current = lines;
      } catch {
        /* outline is cosmetic; a failure here must not break picking */
      }
    };

    window.addEventListener("cadenza:glb", onGlb as EventListener);
    window.addEventListener("cadenza:pickmesh", onPickMesh as EventListener);
    return () => {
      window.removeEventListener("cadenza:glb", onGlb as EventListener);
      window.removeEventListener("cadenza:pickmesh", onPickMesh as EventListener);
    };
  }, []);

  /* ---------------- picking ---------------- */

  function clearHighlight(): void {
    const h = highlightRef.current;
    if (!h) return;
    h.geometry.dispose();
    (h.material as THREE.Material).dispose();
    h.removeFromParent();
    highlightRef.current = null;
  }

  useEffect(() => {
    const vp = vpRef.current;
    if (!vp) return;
    clearHighlight();
    if (!selection || !pickRef.current) return;

    const mesh = pickRef.current.buildFaceOverlay(selection.faceHash);
    if (mesh) {
      vp.overlayRoot.add(mesh);
      highlightRef.current = mesh;
    }
  }, [selection]);

  useEffect(() => {
    const vp = vpRef.current;
    const host = hostRef.current;
    if (!vp || !host) return;

    let downAt: { x: number; y: number } | null = null;

    const onPointerDown = (e: PointerEvent): void => {
      if (e.button !== 0) return;
      downAt = { x: e.clientX, y: e.clientY };
    };

    const onPointerUp = (e: PointerEvent): void => {
      if (e.button !== 0 || !downAt) return;
      const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
      downAt = null;
      if (moved > CLICK_SLOP) return; // that was an orbit

      const target = pickRef.current;
      if (!target) return;

      const ndc = pointerToNdc(e, vp.renderer.domElement);
      vp.raycaster.setFromCamera(ndc, vp.camera);
      const hit = target.pick(vp.raycaster);

      if (!hit) {
        useStore.getState().select(null);
        return;
      }

      const point: Vec3 = [hit.point.x, hit.point.y, hit.point.z];
      useStore.getState().select({
        faceHash: hit.faceHash,
        point,
        normal: [hit.normal.x, hit.normal.y, hit.normal.z],
        screen: { x: e.clientX, y: e.clientY },
      });

      // Exact facts about the face, from the kernel. Fills in a beat later.
      // A failure here used to be swallowed, which left the readout sitting on
      // "resolving face…" forever with nothing anywhere saying why. The chip is
      // cosmetic, so this still must not throw — but it must be audible.
      void occt
        .faceFacts(hit.faceHash, point)
        .then((facts) => useStore.getState().setFaceFacts(facts))
        .catch((err: unknown) => {
          useStore.getState().say({
            kind: "error",
            text: `Could not read face ${hit.faceHash} from the kernel: ${
              err instanceof Error ? err.message : String(err)
            }`,
          });
        });
    };

    const el = vp.renderer.domElement;
    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("pointerup", onPointerUp);
    return () => {
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointerup", onPointerUp);
    };
  }, []);

  /* ---------------- toggles + keys ---------------- */

  useEffect(() => {
    vpRef.current?.setGridVisible(showGrid);
  }, [showGrid]);

  useEffect(() => {
    if (wireRef.current) wireRef.current.visible = showEdges;
  }, [showEdges]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const t = e.target;
      if (t instanceof HTMLElement && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;

      if (e.key === "f" || e.key === "F") {
        const vp = vpRef.current;
        if (!vp) return;
        const box = new THREE.Box3().setFromObject(vp.modelRoot);
        vp.frame(box, true);
      }
      if (e.key === "Escape") useStore.getState().select(null);
      if (e.key === "g" || e.key === "G") useStore.getState().toggleGrid();
      if (e.key === "e" || e.key === "E") useStore.getState().toggleEdges();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return <div ref={hostRef} className="absolute inset-0" />;
}

/** Lets the toolbar drive the camera without owning the viewport. */
export function useViewportActions() {
  return {
    fit(): void {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "f" }));
    },
  };
}
