import test from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./load-module.mjs";

async function setup() {
  const shared = await loadModule("../../shared/ts/messages.ts");
  class WebSocket {
    static OPEN = 1;
    static instances = [];
    readyState = 1;
    sent = [];
    constructor() { WebSocket.instances.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() {}
    receive(data) { this.onmessage({ data: typeof data === "object" && !(data instanceof ArrayBuffer) ? JSON.stringify(data) : data }); }
  }
  const diagnostics = await loadModule("../src/lib/diagnostics.ts");
  const { CadenzaSocket } = await loadModule("../src/lib/socket.ts", { "@cadenza/shared": shared, "./diagnostics": diagnostics }, { WebSocket });
  const frames = [], geometry = [];
  const handlers = { onState() {}, onFrame: f => frames.push(f), onGeometry: g => geometry.push(g) };
  const socket = new CadenzaSocket();
  const connect = () => {
    socket.connect(handlers, { url: "ws://test" });
    const ws = WebSocket.instances.at(-1);
    ws.onopen();
    return ws;
  };
  const blob = (revision, artifact, size = 3) => {
    // Server fixture: shared TS deliberately exposes only the decoder.
    const header = new TextEncoder().encode(JSON.stringify({ type: "geometry.blob", revision, artifact, bytes: size }));
    const frame = new ArrayBuffer(4 + header.length + size);
    new DataView(frame).setUint32(0, header.length, false);
    new Uint8Array(frame, 4, header.length).set(header);
    new Uint8Array(frame, 4 + header.length).fill(7);
    return frame;
  };
  const ready = (ws, revision, kinds = ["glb", "step"]) => ws.receive({ type: "geometry.ready", revision,
    blobs: kinds.map(artifact => ({ artifact, bytes: 3 })) });
  return { socket, connect, frames, geometry, blob, ready };
}

test("manifest controls assembly of reversed blobs; stale and unsolicited blobs are dropped", async () => {
  const s = await setup(), ws = s.connect();
  ws.receive(s.blob(1, "glb")); ws.receive(s.blob(1, "step"));
  assert.equal(s.geometry.length, 0);
  s.ready(ws, 4);
  ws.receive(s.blob(4, "step"));
  ws.receive(s.blob(4, "glb", 2));
  assert.equal(s.geometry.length, 0);
  ws.receive(s.blob(4, "glb"));
  assert.equal(s.geometry[0].revision, 4);
  s.ready(ws, 3); ws.receive(s.blob(3, "glb")); ws.receive(s.blob(3, "step"));
  assert.equal(s.geometry.length, 1);
});

test("reset accepts revision one and obsolete socket callbacks cannot replace it", async () => {
  const s = await setup(), old = s.connect();
  s.ready(old, 8, ["glb"]); old.receive(s.blob(8, "glb"));
  const ws = s.connect();
  ws.receive({ type: "session.ready", session_id: "new", revision: 0 });
  old.receive({ type: "session.ready", session_id: "old", revision: 99 });
  s.ready(ws, 1, ["glb"]); ws.receive(s.blob(1, "glb"));
  assert.equal(s.geometry.at(-1).revision, 1);
  assert.equal(s.frames.some(f => f.session_id === "old"), false);
});

test("new ledger generation invalidates pending old blobs; empty undo needs no binary", async () => {
  const s = await setup(), ws = s.connect();
  s.ready(ws, 2); ws.receive(s.blob(2, "step"));
  ws.receive({ type: "ledger.updated", revision: 3 });
  ws.receive(s.blob(2, "glb"));
  ws.receive({ type: "geometry.ready", revision: 3, empty: true, blobs: [] });
  assert.equal(s.geometry.length, 0);
  s.ready(ws, 4, ["glb"]); ws.receive(s.blob(4, "glb"));
  assert.equal(s.geometry[0].revision, 4);
  s.socket.undo(undefined, 4);
  assert.equal(ws.sent.at(-1).base_revision, 4);
});
