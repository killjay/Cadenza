import test from "node:test";
import assert from "node:assert/strict";
import * as THREE from "three";
import { loadModule } from "./load-module.mjs";

test("interference tint follows occurrence prefixes and restores original materials", async () => {
  const {highlightInterference}=await loadModule("../src/viewport/model.ts",{three:THREE,
    "three/examples/jsm/loaders/GLTFLoader.js":{GLTFLoader:class{}}});
  const root=new THREE.Group();
  for(const path of ["comp_a/comp_leaf","comp_ab/comp_leaf","comp_b"]){
    const mesh=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshStandardMaterial({color:0xabcdef,opacity:.16}));
    mesh.userData.component_id=path;root.add(mesh);
  }
  highlightInterference(root,["comp_a"]);
  assert.equal(root.children[0].material.color.getHex(),0xf08c40);
  assert.equal(root.children[1].material.color.getHex(),0xabcdef);
  highlightInterference(root,[]);
  assert.ok(root.children.every(mesh=>mesh.material.color.getHex()===0xabcdef&&mesh.material.opacity===.16));
});

test("material policy preserves geometry, attribution, alpha and imported colours", async () => {
  let root;
  const inputs = [{ feature_id: "feat_stock", component_id: "comp_a/comp_leaf" },
    { feature_id: "feat_hole_cut", component_id: "comp_a/comp_leaf" },
    { feature_id: "feat_stock", component_id: "comp_b/comp_leaf" }, { feature_id: "feat_unattributed" }];
  class GLTFLoader { parse(_data, _path, done) { done({ scene: root }); } }
  const { parseGlb } = await loadModule("../src/viewport/model.ts", { three: THREE,
    "three/examples/jsm/loaders/GLTFLoader.js": { GLTFLoader } });
  for (const policy of ["source", "assembly", "imported"]) {
    root = new THREE.Group();
    for (const attribution of inputs) {
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(10, 20, 30), new THREE.MeshStandardMaterial({ color: 0x123456, opacity: 0.4, transparent: true }));
      mesh.userData = { ...attribution };root.add(mesh);
    }
    const geometry = root.children.map(mesh => mesh.geometry);
    const original = root.children.map(mesh => mesh.material);
    const bounds = new THREE.Box3().setFromObject(root).getSize(new THREE.Vector3()).toArray();
    const model = await parseGlb(new ArrayBuffer(0), policy);
    assert.deepEqual(model.box.getSize(new THREE.Vector3()).toArray(), bounds);
    assert.equal(model.triangleCount, 48);
    root.children.forEach((mesh, i) => {
      assert.equal(mesh.geometry, geometry[i]);assert.deepEqual(mesh.userData, inputs[i]);
      assert.notEqual(mesh.material, original[i]);assert.equal(mesh.material.opacity, 0.4);
      assert.equal(mesh.material.transparent, true);assert.equal(mesh.material.side, THREE.FrontSide);
    });
    const colours = root.children.map(mesh => mesh.material.color.getHex());
    if (policy === "source") { assert.equal(new Set(colours).size, 1);assert.equal(model.colorMode, "part"); }
    if (policy === "assembly") { assert.equal(colours[0], colours[1]);assert.notEqual(colours[0], colours[2]);assert.equal(model.colorMode, "instances"); }
    if (policy === "imported") {
      assert.ok(colours.every(colour => colour === 0x123456));assert.equal(model.colorMode, "imported");
      root.children.forEach((mesh, i) => { assert.equal(mesh.material.roughness, original[i].roughness);assert.equal(mesh.material.metalness, original[i].metalness); });
    }
  }
});

test("faded context uses inverse occurrence pose, hides only active instance and cannot intercept picking", async () => {
  const { assemblyContext, disposeSubtree } = await loadModule("../src/viewport/model.ts", { three: THREE,
    "three/examples/jsm/loaders/GLTFLoader.js": { GLTFLoader: class {} } });
  const root = new THREE.Group(), sourceToProject = new THREE.Matrix4().makeRotationY(Math.PI / 2);
  sourceToProject.setPosition(70, 20, 30);
  const localPoint = new THREE.Vector3(2, 3, 4), projectPoint = localPoint.clone().applyMatrix4(sourceToProject);
  // The GLB contains project geometry in Y-up. Place both occurrences there to test occlusion.
  const glbPoint = projectPoint.clone().applyAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2);
  let disposed = 0;
  for (const path of ["comp_left/comp_leaf", "comp_right/comp_leaf"]) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial());
    mesh.position.copy(glbPoint); mesh.userData = { component_id: path };
    mesh.geometry.addEventListener("dispose", () => disposed++);root.add(mesh);
  }
  const faded = assemblyContext({ root }, sourceToProject.clone().transpose().toArray(), ["comp_left", "comp_leaf"]);
  faded.updateMatrixWorld(true);
  assert.ok(root.children[1].getWorldPosition(new THREE.Vector3()).distanceTo(localPoint) < 1e-9);
  assert.equal(root.children[0].visible, false);assert.equal(root.children[1].visible, true);
  for (const mesh of root.children) {
    assert.equal(mesh.castShadow, false);assert.equal(mesh.receiveShadow, false);
    assert.equal(mesh.material.depthWrite, false);assert.equal(mesh.material.opacity, 0.16);
  }
  const ray = new THREE.Raycaster(localPoint.clone().add(new THREE.Vector3(0, 0, 10)), new THREE.Vector3(0, 0, -1));
  assert.deepEqual(ray.intersectObject(faded, true), []);
  disposeSubtree(faded);assert.equal(disposed, 2);assert.equal(faded.parent, null);
});
