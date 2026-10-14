import test from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./load-module.mjs";
const manifest = () => ({ version:1, run_id:"run", sequence:1, updated:"2026-10-11T12:00:00Z", status:"running", observer_error:null, estimated_usd:null,
  checkpoints:[{sequence:1,step:"case/0",phase:"before",prompt:"<script>text only</script>",target:"part",detail:"",before:null,proposal:null,accepted:null}] });
test("strict viewer identifiers and versioned checkpoints", async () => {
  const m = await loadModule("../src/lib/evaluation-view.ts");
  assert.equal(m.validRun("../secret"), false);
  assert.equal(m.parseManifest(manifest(), "run").checkpoints[0].before, null);
  for (const change of [{version:2}, {sequence:0}, {run_id:"other"}, {status:"imaginary"}, {estimated_usd:-1}])
    assert.throws(() => m.parseManifest({...manifest(), ...change}, "run"));
  const data=manifest(); data.checkpoints[0].proposal="../secret";
  assert.throws(() => m.parseManifest(data, "run"));
});
test("viewer fetch is read-only and propagates abort signal", async () => {
  const calls=[]; const signal={};
  const m = await loadModule("../src/lib/evaluation-view.ts", {}, { fetch:async (...args) => { calls.push(args); return {ok:true,text:async()=>JSON.stringify(manifest())}; } });
  await m.fetchManifest("run", signal);
  assert.equal(calls.length,1); assert.equal(calls[0][0],"/api/evaluations/run");
  assert.equal(calls[0][1].signal,signal); assert.equal(calls[0][1].method,undefined);
  await assert.rejects(m.fetchManifest("../bad",signal)); assert.equal(calls.length,1);
});

test("observer URL never falls through to editor for invalid batch inputs", async () => {
  const m = await loadModule("../src/lib/evaluation-view.ts");
  assert.equal(m.viewerLocation(new URLSearchParams("foo=bar")).kind,"editor");
  for (const query of ["evaluation=", "evaluations=one,", "evaluation=one&evaluations=two", "evaluation=one&evaluation=two", "evaluations=one,one", "evaluation=../secret", "evaluation=one,two", "evaluations="+Array.from({length:13},(_,i)=>`run${i}`).join(",")])
    assert.equal(m.viewerLocation(new URLSearchParams(query)).kind,"error",query);
  assert.deepEqual([...m.viewerLocation(new URLSearchParams("evaluations=one,two")).runs],["one","two"]);
  assert.deepEqual([...m.viewerLocation(new URLSearchParams("evaluation=one")).runs],["one"]);
});
test("Auto shows unaccepted candidates without inferring a decision from test success", async () => {
  const m = await loadModule("../src/lib/evaluation-view.ts");
  const c={...manifest().checkpoints[0],before:"1.glb",accepted:"1.glb",proposal:"2.glb"};
  assert.equal(m.autoPhase(c),"proposal"); // Cancel or failed candidate.
  assert.equal(m.autoPhase({...c,accepted:"2.glb"}),"proposal"); // Apply, same bytes.
  assert.equal(m.autoPhase({...c,proposal:null}),"accepted"); // Clarification.
  assert.equal(m.autoPhase({...c,proposal:null,accepted:null}),"before");
  assert.equal(m.recordedDecision({...manifest(),checkpoints:[{...c,phase:"passed"}]},c),undefined);
  const decision={...c,sequence:2,phase:"decision",detail:"Recorded action: cancel"};
  const complete={...c,sequence:3,phase:"completed",detail:"Passed"};
  assert.equal(m.recordedDecision({...manifest(),checkpoints:[c,decision,complete]},c),undefined);
  assert.equal(m.recordedDecision({...manifest(),checkpoints:[c,decision,complete]},complete),decision.detail);
});
test("batch follows ordered started runs and pins manual run/sequence selections", async () => {
  const m = await loadModule("../src/lib/evaluation-view.ts");
  const a={...manifest(),run_id:"a",sequence:20,checkpoints:[{...manifest().checkpoints[0],sequence:20}]};
  const b={...manifest(),run_id:"b"};
  const states={a:{manifest:a},b:{manifest:b},c:{waiting:true}};
  assert.equal(m.activeSelection(["a","b","c"],states,null).run,"b");
  const manual={run:"a",sequence:20};
  assert.equal(m.activeSelection(["a","b","c"],states,manual),manual);
  assert.equal(m.mergeManifest(states.a,{...a,sequence:1}).manifest.sequence,20);
  assert.equal(m.mergeManifest(states.b,{...b,sequence:2}).manifest.sequence,2);
  const steps=m.recordedSteps(["a","b","c"],states);
  assert.deepEqual(steps.map(s=>s.run).join(","),"a,b");
  assert.equal(m.batchStatus(["a","b","c"],states),"Batch running");
  states.a.manifest={...a,status:"completed"};states.b.manifest={...b,status:"completed"};
  assert.equal(m.batchStatus(["a","b","c"],states),"Batch waiting for remaining runs");
  assert.equal(m.batchStatus(["a","b"],states),"Batch completed");
  states.b.manifest={...b,status:"failed"};
  assert.match(m.batchStatus(["a","b"],states),/failures/);
});
test("poller retains terminal runs, labels waiting/errors, and stops requests after cleanup", async () => {
  const calls=[], updates=[], timers=new Map();let nextTimer=0;
  const m=await loadModule("../src/lib/evaluation-view.ts",{}, {
    AbortController,
    setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
    fetch:async(url)=>{ calls.push(url); return url.endsWith("/done")
      ? {ok:true,text:async()=>JSON.stringify({...manifest(),run_id:"done",status:"completed"})}
      : {ok:false,status:404}; }
  });
  const stop=m.watchRuns(["done","future"],s=>updates.push(s));
  await new Promise(r=>setImmediate(r));
  assert.equal(calls.length,2);assert.equal(updates.at(-1).future.waiting,true);
  const repeat=[...timers.values()].find(t=>t.ms===1000);assert.ok(repeat);repeat.fn();
  await new Promise(r=>setImmediate(r));
  assert.equal(calls.filter(u=>u.endsWith("/done")).length,1);
  stop();const count=calls.length;repeat.fn();await new Promise(r=>setImmediate(r));assert.equal(calls.length,count);
});
