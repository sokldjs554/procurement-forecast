/**
 * `media.fetch`: download a council meeting video into storage, then hand it to the Python
 * `media.transcribe` stage as a child job.
 *
 * The URL comes from a tenant, so the download is where this service is most exposed: only
 * configured hosts (redirects included), https unless explicitly allowed, a size ceiling, and
 * no HTML pages mistaken for videos. A download cut off mid-way resumes with a Range request
 * on the next attempt instead of starting over.
 */
import { createHash } from "node:crypto";
import { once } from "node:events";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, rename, rm, stat } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { z } from "zod";
import type { Config } from "./config.js";
import type { Pool } from "./db.js";
import { enqueue } from "./queue.js";
import { PermanentError, type Handler } from "./worker.js";

export const FETCH_KIND = "media.fetch";
export const TRANSCRIBE_KIND = "media.transcribe";

export const MediaJobInput = z.object({
  source_url: z.url({ protocol: /^https?$/ }),
  title: z.string().trim().min(1).max(300),
  meeting_date: z.iso.date(),
  publisher: z.string().trim().max(200).optional(),
  institution: z.string().trim().max(40).optional(),
  published_at: z.iso.date().optional(),
});
export type MediaJobInput = z.infer<typeof MediaJobInput>;

/** Why a URL may not be fetched, or null when it may. */
export function refuseUrl(cfg: Pick<Config, "FETCH_ALLOWED_HOSTS" | "FETCH_ALLOW_INSECURE_HTTP">, url: URL): string | null {
  if (url.protocol !== "https:" && !(url.protocol === "http:" && cfg.FETCH_ALLOW_INSECURE_HTTP)) {
    return `${url.protocol.replace(":", "")} is not allowed; use https`;
  }
  if (url.username || url.password) return "credentials in the URL are not allowed";
  if (!cfg.FETCH_ALLOWED_HOSTS.includes(url.hostname.toLowerCase())) {
    return `host ${url.hostname} is not on the download allowlist`;
  }
  return null;
}

const MEDIA_EXT = new Set([".mp4", ".m4a", ".mp3", ".webm", ".mkv", ".ts", ".mov", ".wav", ".flac"]);
const TRANSIENT = new Set([408, 425, 429, 500, 502, 503, 504]);

async function sizeOf(path: string): Promise<number> {
  try {
    return (await stat(path)).size;
  } catch {
    return 0;
  }
}

async function open(cfg: Config, url: URL, from: number, signal: AbortSignal): Promise<Response> {
  let current = url;
  for (let hop = 0; hop <= 5; hop++) {
    const refused = refuseUrl(cfg, current);
    if (refused) throw new PermanentError(hop ? `redirect refused: ${refused}` : refused);
    const res = await fetch(current, {
      headers: from ? { range: `bytes=${from}-` } : {},
      redirect: "manual",
      signal: AbortSignal.any([signal, AbortSignal.timeout(cfg.FETCH_TIMEOUT_SECONDS * 1000)]),
    });
    const location = res.headers.get("location");
    if (res.status >= 300 && res.status < 400 && location) {
      await res.body?.cancel();
      current = new URL(location, current);
      continue;
    }
    return res;
  }
  throw new PermanentError("too many redirects");
}

export function fetchHandler(pool: Pool, cfg: Config): Handler {
  return async (job, ctx) => {
    const parsed = MediaJobInput.safeParse(job.payload);
    if (!parsed.success) throw new PermanentError(`invalid payload: ${z.prettifyError(parsed.error)}`);
    const input = parsed.data;
    const url = new URL(input.source_url);
    const dir = resolve(cfg.MEDIA_STORAGE_DIR, `org${job.orgId}`);
    await mkdir(dir, { recursive: true });
    const part = join(dir, `job${job.id}.part`);

    let have = await sizeOf(part);
    let res = await open(cfg, url, have, ctx.signal);
    if (have && res.status === 200) {
      have = 0; // the server ignored the Range header: start over
    } else if (have && res.status === 206) {
      const start = Number(res.headers.get("content-range")?.match(/^bytes (\d+)-/)?.[1]);
      if (start !== have) {
        await res.body?.cancel();
        have = 0;
        res = await open(cfg, url, 0, ctx.signal);
      }
    } else if (res.status === 416) {
      await res.body?.cancel();
      await rm(part, { force: true });
      throw new Error("range not satisfiable; restarting the download");
    }
    if (res.status !== 200 && res.status !== 206) {
      await res.body?.cancel();
      const message = `download failed: HTTP ${res.status}`;
      throw TRANSIENT.has(res.status) ? new Error(message) : new PermanentError(message);
    }
    const type = (res.headers.get("content-type") ?? "").split(";")[0]?.trim().toLowerCase() ?? "";
    if (type.startsWith("text/") || type.includes("html") || type.includes("json")) {
      await res.body?.cancel();
      throw new PermanentError(`not a media file: ${type}`);
    }
    const total =
      res.status === 206
        ? Number(res.headers.get("content-range")?.split("/")[1]) || null
        : Number(res.headers.get("content-length")) || null;
    if (total !== null && total > cfg.FETCH_MAX_BYTES) {
      await res.body?.cancel();
      throw new PermanentError(`video is ${total} bytes; the limit is ${cfg.FETCH_MAX_BYTES}`);
    }

    // The hash covers the whole file: what is already on disk, then what arrives.
    const hash = createHash("sha256");
    if (have) for await (const chunk of createReadStream(part)) hash.update(chunk as Buffer);
    const out = createWriteStream(part, { flags: have ? "a" : "w" });
    let bytes = have;
    try {
      if (!res.body) throw new Error("empty response body");
      for await (const chunk of res.body as AsyncIterable<Uint8Array>) {
        bytes += chunk.byteLength;
        if (bytes > cfg.FETCH_MAX_BYTES) throw new PermanentError(`video exceeds ${cfg.FETCH_MAX_BYTES} bytes`);
        hash.update(chunk);
        if (!out.write(chunk)) await once(out, "drain");
        ctx.progress = { stage: "download", done: bytes, total };
      }
    } finally {
      out.end();
      await once(out, "close");
    }
    if (total !== null && bytes !== total) throw new Error(`download ended at ${bytes} of ${total} bytes`);

    const sha256 = hash.digest("hex");
    const ext = MEDIA_EXT.has(extname(url.pathname).toLowerCase()) ? extname(url.pathname).toLowerCase() : ".media";
    const file = join(dir, `${sha256}${ext}`);
    if (await sizeOf(file)) await rm(part, { force: true });
    else await rename(part, file);

    const child = await enqueue(pool, {
      orgId: job.orgId,
      kind: TRANSCRIBE_KIND,
      payload: {
        video: `file://${file}`,
        sha256,
        title: input.title,
        meeting_date: input.meeting_date,
        publisher: input.publisher ?? null,
        institution: input.institution ?? null,
        url: input.source_url,
        published_at: input.published_at ?? null,
        stt: cfg.STT_SPEC,
      },
      dedupeKey: `sha256:${sha256}`,
      parentId: job.id,
      budgetUsd: job.budgetUsd,
    });
    return { file, bytes, sha256, transcribe_job_id: child.jobId, transcribe_job_created: child.created };
  };
}
