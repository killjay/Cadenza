/**
 * Turning a dropped file into something the wire will accept.
 *
 * Three jobs, all of which have to happen in the browser rather than on the
 * server:
 *
 *   1. DOWNSCALE. A phone photo of a whiteboard is 4000 px and 4 MB. Vision
 *      models bill by tile and stop resolving detail past ~1568 px on the long
 *      edge, so anything larger is paid for twice — once on the wire, once in
 *      tokens — and buys nothing. Doing it client-side also means the 5 MB
 *      server cap is a backstop against abuse rather than something an honest
 *      user trips over by photographing a napkin.
 *   2. RE-ENCODE. Format is chosen by measurement, not by guessing: line art
 *      (the common case — a pencil sketch, a screenshot of a drawing) is mostly
 *      flat white and compresses far better as PNG than JPEG, and JPEG's ringing
 *      artifacts land exactly on the thin dark strokes the model needs to read.
 *      So: try PNG, and fall back to JPEG only when PNG turns out fat, which is
 *      the signature of a photograph.
 *   3. STRIP METADATA. Re-encoding through a canvas drops EXIF, which is where
 *      GPS coordinates live. A user uploading a photo of a part in their garage
 *      should not be uploading their home address with it.
 *
 * EXIF orientation is applied by `createImageBitmap` before the canvas draw, so
 * a photo taken sideways arrives upright rather than the model being asked to
 * read a rotated drawing.
 */
import type { ImageMediaType, InlineImage } from "./types";

/** Anthropic's effective ceiling: more pixels cost tokens and resolve nothing. */
const MAX_EDGE_PX = 1568;

/** Past this, PNG is telling us the source is photographic, not line art. */
const PNG_BUDGET_BYTES = 900_000;

const JPEG_QUALITY = 0.85;

export const ACCEPTED_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

export class ImageTooLargeError extends Error {}
export class ImageUnreadableError extends Error {}

export interface PreparedImage extends InlineImage {
  /** For the thumbnail. Not sent — the wire carries `data` only. */
  previewUrl: string;
  bytes: number;
  width: number;
  height: number;
}

function fits(width: number, height: number): { w: number; h: number } {
  const longest = Math.max(width, height);
  if (longest <= MAX_EDGE_PX) return { w: width, h: height };
  const scale = MAX_EDGE_PX / longest;
  return { w: Math.round(width * scale), h: Math.round(height * scale) };
}

async function toBlob(
  canvas: HTMLCanvasElement,
  type: string,
  quality?: number,
): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new ImageUnreadableError(`encode to ${type} failed`))),
      type,
      quality,
    );
  });
}

async function blobToBase64(blob: Blob): Promise<string> {
  const buffer = await blob.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  // Chunked because String.fromCharCode(...veryLongArray) blows the call stack
  // somewhere north of ~100k arguments, which a 1568px image comfortably exceeds.
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

/**
 * File -> downscaled, re-encoded, base64 `InlineImage` ready to put on the wire.
 *
 * @param maxBytes the server's `limits.max_image_bytes`, so the client refuses
 *                 locally rather than having the turn rejected after upload.
 */
export async function prepareImage(file: File, maxBytes = 5_000_000): Promise<PreparedImage> {
  if (!ACCEPTED_TYPES.includes(file.type)) {
    throw new ImageUnreadableError(
      `${file.type || "that file"} is not an image CADenza can read. Use PNG, JPEG, WebP or GIF.`,
    );
  }

  let bitmap: ImageBitmap;
  try {
    // `imageOrientation` applies EXIF rotation; without it a phone photo of a
    // drawing arrives on its side and the model reads the dimensions sideways.
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch (err) {
    throw new ImageUnreadableError(
      `Could not decode that image: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  const { w, h } = fits(bitmap.width, bitmap.height);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new ImageUnreadableError("no 2d canvas context available");

  // Flatten onto white. A transparent PNG sketch would otherwise composite onto
  // black in the model's view and the strokes would vanish.
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, w, h);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bitmap, 0, 0, w, h);
  bitmap.close();

  let mediaType: ImageMediaType = "image/png";
  let blob = await toBlob(canvas, "image/png");
  if (blob.size > PNG_BUDGET_BYTES) {
    const jpeg = await toBlob(canvas, "image/jpeg", JPEG_QUALITY);
    if (jpeg.size < blob.size) {
      blob = jpeg;
      mediaType = "image/jpeg";
    }
  }

  if (blob.size > maxBytes) {
    throw new ImageTooLargeError(
      `That image is ${(blob.size / 1e6).toFixed(1)} MB after downscaling, over the ` +
        `${(maxBytes / 1e6).toFixed(1)} MB limit. Crop it to just the drawing and try again.`,
    );
  }

  return {
    media_type: mediaType,
    data: await blobToBase64(blob),
    name: file.name || null,
    previewUrl: URL.createObjectURL(blob),
    bytes: blob.size,
    width: w,
    height: h,
  };
}

/** Pull image files out of a paste or drop. Returns [] when there are none. */
export function imageFilesFrom(source: DataTransfer | ClipboardEvent["clipboardData"]): File[] {
  if (!source) return [];
  const out: File[] = [];
  for (const item of Array.from(source.items ?? [])) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file && ACCEPTED_TYPES.includes(file.type)) out.push(file);
  }
  if (out.length === 0) {
    for (const file of Array.from(source.files ?? [])) {
      if (ACCEPTED_TYPES.includes(file.type)) out.push(file);
    }
  }
  return out;
}
