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
import { buildWireframe, disposeSubtree, parseGlb, swapModel } from "../viewport/model";
import { PickTarget, pointerToNdc } from "../viewport/pick";
import type { PickMeshData } from "../viewport/pick";
import { occt } from "../workers/occtClient";
import { useStore } from "../lib/store";
import type { Vec3, SurfaceReference } from "../lib/types";

/** Drag threshold, in px, above which a pointerup is an orbit and not a click. */
const CLICK_SLOP = 4;

export function Viewport() {
  const hostRef = useRef<HTMLDivElement>(null);
  const vpRef = useRef<Vp | null>(null);
  const pickRef = useRef<PickTarget | null>(null);
  const highlightRef = useRef<THREE.Object3D | null>(null);
  const wireRef = useRef<THREE.LineSegments | null>(null);
  const firstLoadRef = useRef(true);

  const showGrid = useStore((s) => s.showGrid);
  const showEdges = useStore((s) => s.showEdges);
  const sketchActive = useStore(s => !!s.draft && !s.preview);
  const selections = useStore((s) => s.selections);

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

  useEffect(() => { if (vpRef.current) vpRef.current.controls.enabled = !sketchActive; }, [sketchActive]);

  useEffect(() => {
    type PickDelivery = { mesh: PickMeshData; wireframe: { points: Float32Array; edgeGroups: Int32Array }; generation: number };
    let displayedGeneration = -1;
    let pendingPick: PickDelivery | null = null;
    let mounted = true;
    const invalidate = (): void => {
      displayedGeneration = -1;
      pendingPick = null;
      pickRef.current?.dispose();
      pickRef.current = null;
      clearHighlight();
      if (wireRef.current) disposeSubtree(wireRef.current);
      wireRef.current = null;
    };
    const clear = (): void => {
      invalidate();
      const vp = vpRef.current;
      if (vp) {
        for (const child of vp.modelRoot.children.slice()) disposeSubtree(child);
        vp.setGroundLevel(0);
      }
      firstLoadRef.current = true;
    };
    const applyPick = (data: PickDelivery): void => {
      const vp = vpRef.current;
      if (!vp || data.generation !== displayedGeneration) return;
      pickRef.current?.dispose();
      pickRef.current = new PickTarget(data.mesh);
      if (wireRef.current) disposeSubtree(wireRef.current);
      const lines = buildWireframe(data.wireframe.points, data.wireframe.edgeGroups);
      lines.visible = useStore.getState().showEdges;
      vp.overlayRoot.add(lines);
      wireRef.current = lines;
    };
    const onGlb = async (ev: Event): Promise<void> => {
      const vp = vpRef.current;
      if (!vp) return;
      const { glb, generation } = (ev as CustomEvent<{ glb: ArrayBuffer; generation: number }>).detail;
      invalidate();
      try {
        const model = await parseGlb(glb);
        if (!mounted || generation !== useStore.getState().geometryGeneration) {
          disposeSubtree(model.root);
          return;
        }
        const isFirst = firstLoadRef.current;
        firstLoadRef.current = false;
        swapModel(vp, model, isFirst);
        displayedGeneration = generation;
        useStore.setState((s) => ({
          model: s.model ? { ...s.model, parseMs: Math.round(model.parseMs), triangleCount: model.triangleCount } : s.model,
        }));
        if (pendingPick) applyPick(pendingPick);
      } catch (err) {
        if (mounted && generation === useStore.getState().geometryGeneration) {
          useStore.getState().say({ kind: "error", text: `Model display failed: ${String(err)}` });
        }
      }
    };
    const onPickMesh = (ev: Event): void => {
      const data = (ev as CustomEvent<PickDelivery>).detail;
      if (data.generation !== useStore.getState().geometryGeneration) return;
      pendingPick = data;
      applyPick(data);
    };
    window.addEventListener("cadenza:glb", onGlb as EventListener);
    window.addEventListener("cadenza:pickmesh", onPickMesh);
    window.addEventListener("cadenza:clear", clear);
    window.addEventListener("cadenza:invalidate", invalidate);
    return () => {
      mounted = false;
      invalidate();
      window.removeEventListener("cadenza:glb", onGlb as EventListener);
      window.removeEventListener("cadenza:pickmesh", onPickMesh);
      window.removeEventListener("cadenza:clear", clear);
      window.removeEventListener("cadenza:invalidate", invalidate);
    };
  }, []);

  /* ---------------- picking ---------------- */

  function clearHighlight(): void {
    const h = highlightRef.current;
    if (!h) return;
    h.traverse((o) => {
        const m = o as THREE.Mesh;
        if (m.isMesh) {
            if (m.geometry) m.geometry.dispose();
            if (m.material) {
                if (Array.isArray(m.material)) m.material.forEach((mat) => mat.dispose());
                else m.material.dispose();
            }
        }
    });
    h.removeFromParent();
    highlightRef.current = null;
  }

  useEffect(() => {
    const vp = vpRef.current;
    if (!vp) return;
    clearHighlight();
    if (!selections || selections.length === 0 || !pickRef.current) return;

    const group = new THREE.Group();

    selections.forEach((selection) => {
      if (selection.saved_reference || selection.faceHash === -1) return; // Re-resolved targets have no current face hash.
        if (selection.mode === "part" && selection.feature_id) {
            const meshes: THREE.Mesh[] = [];
            vp.modelRoot.traverse((o) => {
                if ((o as THREE.Mesh).isMesh && o.userData?.feature_id === selection.feature_id) {
                    meshes.push(o as THREE.Mesh);
                }
            });
            
            if (meshes.length > 0) {
                meshes.forEach(m => {
                    const geo = m.geometry.clone();
                    const mat = new THREE.MeshBasicMaterial({
                      color: 0x3b82f6,
                      transparent: true,
                      opacity: 0.65,
                      depthWrite: false,
                      side: THREE.DoubleSide,
                      polygonOffset: true,
                      polygonOffsetFactor: -1,
                      polygonOffsetUnits: -1,
                    });
                    const mesh = new THREE.Mesh(geo, mat);
                    mesh.matrix.copy(m.matrixWorld);
                    mesh.matrixAutoUpdate = false;
                    mesh.renderOrder = 2;
                    group.add(mesh);
                });
            }
        } else {
            const mesh = pickRef.current!.buildFaceOverlay(selection.faceHash);
            if (mesh) {
                if (selection.mode === "side") {
                   (mesh.material as THREE.MeshBasicMaterial).color.setHex(0x3b82f6);
                } else {
                   (mesh.material as THREE.MeshBasicMaterial).color.setHex(0x3b82f6);
                   (mesh.material as THREE.MeshBasicMaterial).opacity = 0.65;
                }
                mesh.renderOrder = 2;
                group.add(mesh);
            }
        }
    });

    vp.overlayRoot.add(group);
    highlightRef.current = group;
  }, [selections]);

  useEffect(() => {
    const vp = vpRef.current;
    const host = hostRef.current;
    if (!vp || !host) return;

    let downAt: { x: number; y: number } | null = null;
    let lastClickTime = 0;

    const hoverRef = { current: null as THREE.Mesh | null };
    let hoverHash = -1;

    const clearHover = () => {
        if (hoverRef.current) {
            hoverRef.current.geometry.dispose();
            (hoverRef.current.material as THREE.Material).dispose();
            hoverRef.current.removeFromParent();
            hoverRef.current = null;
            hoverHash = -1;
        }
    };

    window.addEventListener("cadenza:clear", clearHover);
    window.addEventListener("cadenza:invalidate", clearHover);
    window.addEventListener("cadenza:glb", clearHover);

    const onPointerMove = (e: PointerEvent): void => {
      if (useStore.getState().selectedSourceId || useStore.getState().preview?.display_ledger_id || (useStore.getState().draft && !useStore.getState().preview)) { clearHover(); return; }
      const target = pickRef.current;
      if (!target) return;

      const ndc = pointerToNdc(e, vp.renderer.domElement);
      vp.raycaster.setFromCamera(ndc, vp.camera);
      const hit = target.pick(vp.raycaster);

      const hitHash = hit ? hit.faceHash : -1;
      if (hitHash !== hoverHash) {
        clearHover();
        hoverHash = hitHash;

        if (hitHash !== -1) {
             const mesh = target.buildFaceOverlay(hitHash);
             if (mesh) {
                 (mesh.material as THREE.MeshBasicMaterial).opacity = 0.25;
                 (mesh.material as THREE.MeshBasicMaterial).color.setHex(0xffffff);
                 mesh.renderOrder = 1;
                 vp.overlayRoot.add(mesh);
                 hoverRef.current = mesh;
             }
        }
      }
    };

    const onPointerDown = (e: PointerEvent): void => {
      if (useStore.getState().selectedSourceId || useStore.getState().preview?.display_ledger_id || (useStore.getState().draft && !useStore.getState().preview)) return;
      if (e.button !== 0) return;
      downAt = { x: e.clientX, y: e.clientY };
    };

    const onPointerUp = (e: PointerEvent): void => {
      if (useStore.getState().selectedSourceId || useStore.getState().preview?.display_ledger_id || (useStore.getState().draft && !useStore.getState().preview)) { downAt = null; return; }
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

      const now = performance.now();
      const isDoubleClick = (now - lastClickTime < 300);
      lastClickTime = now;

      const glbHits = vp.raycaster.intersectObject(vp.modelRoot, true);
      const nearestGlb = glbHits.find((h) => (h.object as THREE.Mesh).isMesh);
      const glbHit = nearestGlb && nearestGlb.point.distanceTo(hit.point) < 0.25 ? nearestGlb : undefined;
      const feature_id = glbHit ? (glbHit.object.userData.feature_id as string) : undefined;
      const component_id = glbHit ? (glbHit.object.userData.component_id as string) : undefined;
      const part_id = glbHit ? (glbHit.object.userData.part_id as string) : undefined;
      const label = glbHit ? (glbHit.object.userData.label as string) : undefined;

      const currentSels = useStore.getState().selections;
      const isMulti = e.ctrlKey || e.metaKey;

      let mode: "face" | "side" | "part" = "face";
      if (e.shiftKey) {
        mode = "side";
      } else if (isDoubleClick) {
        mode = "part";
      }

      const existingIdx = currentSels.findIndex(s => s.faceHash === hit.faceHash);

      if (isMulti) {
        if (existingIdx !== -1) {
            const next = [...currentSels];
            next.splice(existingIdx, 1);
            useStore.getState().select(next.length > 0 ? next : null);
            return;
        }
      } else {
        if (
          mode === "face" &&
          currentSels.length === 1 &&
          currentSels[0].faceHash === hit.faceHash &&
          currentSels[0].mode === "face"
        ) {
          useStore.getState().select(null);
          return;
        }
      }

      const point: Vec3 = [hit.point.x, hit.point.y, hit.point.z];
      const newSel = {
        mode,
        feature_id,
        component_id,
        part_id,
        label,
        surface_reference: glbHit?.object.userData.surface_reference as SurfaceReference | undefined,
        selection_error: glbHit ? glbHit.object.userData.selection_error as string | undefined
          : "Could not match this face to its source. Select a clear face interior or edit from the feature tree.",
        faceHash: hit.faceHash,
        point,
        normal: [hit.normal.x, hit.normal.y, hit.normal.z] as Vec3,
        screen: { x: e.clientX, y: e.clientY },
      };

      if (isMulti) {
        useStore.getState().select([...currentSels, newSel]);
      } else {
        useStore.getState().select([newSel]);
      }

      // Exact facts about the face, from the kernel. Fills in a beat later.
      // A failure here used to be swallowed, which left the readout sitting on
      // "resolving face…" forever with nothing anywhere saying why. The chip is
      // cosmetic, so this still must not throw — but it must be audible.
      const generation = useStore.getState().geometryGeneration;
      void occt
        .faceFacts(hit.faceHash, point)
        .then((facts) => {
          if (generation === useStore.getState().geometryGeneration) useStore.getState().setFaceFacts(facts);
        })
        .catch((err: unknown) => {
          if (generation !== useStore.getState().geometryGeneration) return;
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
    el.addEventListener("pointermove", onPointerMove);
    return () => {
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("cadenza:clear", clearHover);
      window.removeEventListener("cadenza:invalidate", clearHover);
      window.removeEventListener("cadenza:glb", clearHover);
      el.removeEventListener("pointermove", onPointerMove);
      clearHover();
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
      if (useStore.getState().selectedSourceId || useStore.getState().preview?.display_ledger_id || (useStore.getState().draft && !useStore.getState().preview)) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t instanceof HTMLElement && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName))) return;

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
