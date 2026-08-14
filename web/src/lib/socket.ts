/**
 * The WebSocket client. ONE module, on purpose — everything the rest of the app
 * knows about the wire is the interface at the bottom of this file.
 *
 * Speaks CONTRACTS.md §2:
 *
 *   TEXT frames    JSON control plane, discriminated on a DOTTED `type`
 *                  (`user.prompt`, `agent.status`, ...).
 *   BINARY frames  GLB and STEP bytes, each self-describing: a 4-byte
 *                  big-endian header length, a UTF-8 JSON header, then the
 *                  payload. Decoded with the shared codec, never a local copy.
 *
 * Two rules that look like details and are not:
 *
 *   * `message_id` is minted HERE, by the client, and echoed by every server
 *     frame belonging to that turn.
 *   * Blobs are keyed off the binary header's `(revision, artifact)` — NEVER
 *     off arrival order — and any blob older than the revision being shown is
 *     dropped. Two rebuilds in flight would otherwise race and paint the older
 *     mesh last.
 */
import { decodeBlobFrame } from "@cadenza/shared";
import type { ClientFrame, InlineImage, ServerFrame, Target, Vec3 } from "./types";

export type ConnectionState = "idle" | "connecting" | "open" | "closed" | "error";

export interface GeometryPayload {
  revision: number;
  glb: ArrayBuffer;
  step?: ArrayBuffer;
}

export interface SocketHandlers {
  onState: (state: ConnectionState, detail?: string) => void;
  onFrame: (frame: ServerFrame) => void;
  /** Fired once the blobs announced by a `geometry.ready` have arrived. */
  onGeometry: (payload: GeometryPayload) => void;
}

export interface ConnectOptions {
  url?: string;
  sessionId?: string | null;
}

export const CONTRACT_VERSION = "1.0.0";

function defaultUrl(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

/** `msg_` + random hex, per CONTRACTS.md §2.1. */
export function newMessageId(): string {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return `msg_${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

interface PendingGeometry {
  revision: number;
  expected: number;
  glb?: ArrayBuffer;
  step?: ArrayBuffer;
}

class CadenzaSocket {
  private ws: WebSocket | null = null;
  private handlers: SocketHandlers | null = null;
  private pending: PendingGeometry | null = null;
  private queue: string[] = [];
  private shownRevision = 0;

  connect(handlers: SocketHandlers, opts: ConnectOptions = {}): void {
    this.handlers = handlers;
    this.disconnect();

    const url = opts.url ?? defaultUrl();
    handlers.onState("connecting", url);
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      handlers.onState("open", url);
      this.send({
        type: "client.hello",
        contract_version: CONTRACT_VERSION,
        session_id: opts.sessionId ?? null,
      });
      for (const msg of this.queue.splice(0)) ws.send(msg);
    };
    ws.onclose = (e) => handlers.onState("closed", `code ${e.code}`);
    ws.onerror = () => handlers.onState("error", url);
    ws.onmessage = (ev: MessageEvent<string | ArrayBuffer>) => {
      if (typeof ev.data === "string") {
        let frame: ServerFrame;
        try {
          frame = JSON.parse(ev.data) as ServerFrame;
        } catch {
          return;
        }
        this.receive(frame);
      } else {
        this.handleBinary(ev.data);
      }
    };
  }

  private receive(frame: ServerFrame): void {
    if (frame.type === "geometry.ready") {
      // The manifest declares what is coming. `expected` counts only the blobs
      // the server actually built, so a STEP-less build still completes.
      this.pending = { revision: frame.revision, expected: frame.blobs.length };
      if (frame.blobs.length === 0) this.pending = null;
    }
    this.handlers?.onFrame(frame);
  }

  private handleBinary(buf: ArrayBuffer): void {
    let decoded: ReturnType<typeof decodeBlobFrame>;
    try {
      decoded = decodeBlobFrame(buf);
    } catch {
      return; // malformed frame: drop it rather than guessing what it was
    }
    const { header, payload } = decoded;

    // A blob older than what is on screen is stale by definition.
    if (header.revision < this.shownRevision) return;

    if (!this.pending || this.pending.revision !== header.revision) {
      this.pending = { revision: header.revision, expected: 2 };
    }
    if (header.artifact === "glb") this.pending.glb = payload;
    else if (header.artifact === "step") this.pending.step = payload;

    const have = (this.pending.glb ? 1 : 0) + (this.pending.step ? 1 : 0);
    if (have < this.pending.expected) return;

    const { revision, glb, step } = this.pending;
    this.pending = null;
    if (!glb) return; // a STEP with no GLB is nothing the viewport can render
    this.shownRevision = revision;
    this.handlers?.onGeometry({ revision, glb, step });
  }

  send(frame: ClientFrame): void {
    const msg = JSON.stringify(frame);
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(msg);
    else this.queue.push(msg);
  }

  /**
   * The one mutating frame. `target` present => Point & Speak; `images` present
   * => the server routes to the Draftsman and reads the sketch.
   */
  sendPrompt(args: {
    prompt: string;
    target?: Target | null;
    images?: InlineImage[];
    baseRevision?: number;
  }): string {
    const messageId = newMessageId();
    this.send({
      type: "user.prompt",
      message_id: messageId,
      prompt: args.prompt,
      target: args.target ?? null,
      base_revision: args.baseRevision ?? null,
      ...(args.images?.length ? { images: args.images } : {}),
    });
    return messageId;
  }

  probe(point: Vec3): string {
    const messageId = newMessageId();
    this.send({ type: "spatial.probe", message_id: messageId, point, prefer: "face" });
    return messageId;
  }

  reset(name = "Untitled Part"): void {
    this.shownRevision = 0;
    this.send({ type: "session.reset", message_id: newMessageId(), name });
  }

  /** Let the store tell us what is on screen, so stale blobs can be dropped. */
  setShownRevision(revision: number): void {
    this.shownRevision = revision;
  }

  disconnect(): void {
    this.ws?.close();
    this.ws = null;
    this.pending = null;
  }
}

export const socket = new CadenzaSocket();
