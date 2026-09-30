import { createHash, randomBytes } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { FETCH_KIND, fetchHandler, TRANSCRIBE_KIND } from "../src/media.js";
import { cancel, enqueue } from "../src/queue.js";
import { workOne } from "../src/worker.js";
import { createOrg, job, pool, testConfig } from "./helpers.js";

const VIDEO = randomBytes(3 * 1024 * 1024 + 123);
const SHA = createHash("sha256").update(VIDEO).digest("hex");

// What the fake council server does next; each test sets it.
let behaviour: { cutAt?: number; slowMs?: number; ignoreRange?: boolean } = {};
const seen: { path: string; range: string | undefined }[] = [];

function serveVideo(req: IncomingMessage, res: ServerResponse) {
  const range = req.headers.range?.match(/^bytes=(\d+)-$/);
  const start = range && !behaviour.ignoreRange ? Number(range[1]) : 0;
  const body = VIDEO.subarray(start);
  res.writeHead(start ? 206 : 200, {
    "content-type": "video/mp4",
    "content-length": body.length,
    ...(start ? { "content-range": `bytes ${start}-${VIDEO.length - 1}/${VIDEO.length}` } : {}),
  });
  const { cutAt, ...rest } = behaviour;
  const { slowMs } = rest;
  behaviour = rest; // cut the first response only
  let sent = 0;
  const step = 64 * 1024;
  const pump = () => {
    if (sent >= body.length) return res.end();
    if (cutAt !== undefined && sent >= cutAt) return res.destroy(); // connection dropped
    const chunk = body.subarray(sent, sent + step);
    sent += chunk.length;
    res.write(chunk);
    if (slowMs) setTimeout(pump, slowMs);
    else setImmediate(pump);
  };
  pump();
}

const server: Server = createServer((req, res) => {
  seen.push({ path: req.url ?? "", range: req.headers.range });
  switch (req.url) {
    case "/vod/meeting.mp4":
      return serveVideo(req, res);
    case "/vod/page":
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      return res.end("<html>다시보기</html>");
    case "/vod/elsewhere":
      res.writeHead(302, { location: "http://169.254.169.254/latest/meta-data/" });
      return res.end();
    case "/vod/huge.mp4":
      res.writeHead(200, { "content-type": "video/mp4", "content-length": 50 * 1024 ** 3 });
      return res.end();
    case "/vod/busy.mp4":
      res.writeHead(503);
      return res.end();
    default:
      res.writeHead(404);
      return res.end();
  }
});

let base = "";
let org = 0;
const cfg = testConfig({ FETCH_MAX_BYTES: String(1024 ** 3) });
const handlers = { [FETCH_KIND]: fetchHandler(pool, cfg) };
const never = new AbortController().signal;

beforeAll(async () => {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  org = await createOrg("fetch");
  // Only media.fetch jobs of this suite's tenant should be claimable here.
  await pool.query("UPDATE jobs SET status = 'cancelled', finished_at = now() WHERE kind = $1 AND status = 'queued'", [
    FETCH_KIND,
  ]);
});

afterAll(async () => {
  server.close();
  await pool.end();
});

beforeEach(() => {
  behaviour = {};
  seen.length = 0;
});

async function submit(path: string, extra: Record<string, unknown> = {}) {
  const { jobId } = await enqueue(pool, {
    orgId: org,
    kind: FETCH_KIND,
    payload: { source_url: `${base}${path}`, title: "제300회 본회의", meeting_date: "2026-03-18", ...extra },
    dedupeKey: `test:${randomBytes(6).toString("hex")}`,
    budgetUsd: "2.50",
  });
  return jobId;
}

/** Starts a worker and waits until it holds the job. Wrapped, because an async function that
 *  returns a promise would wait for that promise too — here, for the whole job. */
async function startWorker(jobId: number, shutdown = never) {
  const running = workOne(pool, handlers, cfg, shutdown);
  for (let i = 0; i < 200 && (await job(jobId)).status !== "running"; i++) {
    await new Promise((r) => setTimeout(r, 10));
  }
  return { running };
}

describe("media.fetch", () => {
  it("downloads, hashes, stores and hands the video to the transcription stage", async () => {
    const id = await submit("/vod/meeting.mp4", { publisher: "경기도 성남시의회" });
    expect(await workOne(pool, handlers, cfg, never)).toBe("succeeded");
    const done = await job(id);
    const result = done.result as { file: string; bytes: number; sha256: string; transcribe_job_id: number };
    expect(result).toMatchObject({ bytes: VIDEO.length, sha256: SHA });
    expect(result.file.endsWith(`${SHA}.mp4`)).toBe(true);
    expect(createHash("sha256").update(readFileSync(result.file)).digest("hex")).toBe(SHA);

    const child = await job(result.transcribe_job_id);
    expect(child).toMatchObject({ kind: TRANSCRIBE_KIND, status: "queued", parent_id: id, dedupe_key: `sha256:${SHA}` });
    expect(child.payload).toMatchObject({
      video: `file://${result.file}`,
      sha256: SHA,
      stt: cfg.STT_SPEC, // the deployment's engine, not anything the tenant sent
      publisher: "경기도 성남시의회",
      url: `${base}/vod/meeting.mp4`,
    });
  });

  it("resumes a dropped download where it stopped", async () => {
    behaviour = { cutAt: 1024 * 1024 };
    const id = await submit("/vod/meeting.mp4");
    expect(await workOne(pool, handlers, cfg, never)).toBe("retry");
    const kept = `${cfg.MEDIA_STORAGE_DIR}/org${org}/job${id}.part`;
    expect(existsSync(kept)).toBe(true);
    const have = readFileSync(kept).length;
    expect(have).toBeGreaterThanOrEqual(1024 * 1024);

    await pool.query("UPDATE jobs SET run_after = now() WHERE id = $1", [id]);
    expect(await workOne(pool, handlers, cfg, never)).toBe("succeeded");
    expect(seen.map((s) => s.range)).toEqual([undefined, `bytes=${have}-`]);
    expect((await job(id)).result).toMatchObject({ sha256: SHA, bytes: VIDEO.length });
  });

  it("starts over when the server ignores the Range header", async () => {
    behaviour = { cutAt: 512 * 1024 };
    const id = await submit("/vod/meeting.mp4");
    expect(await workOne(pool, handlers, cfg, never)).toBe("retry");
    behaviour = { ignoreRange: true };
    await pool.query("UPDATE jobs SET run_after = now() WHERE id = $1", [id]);
    expect(await workOne(pool, handlers, cfg, never)).toBe("succeeded");
    expect((await job(id)).result).toMatchObject({ sha256: SHA, bytes: VIDEO.length });
  });

  it.each([
    ["an HTML page", "/vod/page", "not a media file: text/html"],
    ["a redirect off the allowlist", "/vod/elsewhere", "redirect refused: host 169.254.169.254"],
    ["a file over the size limit", "/vod/huge.mp4", "the limit is"],
    ["a missing file", "/vod/gone.mp4", "HTTP 404"],
  ])("fails %s without retrying", async (_, path, why) => {
    const id = await submit(path);
    expect(await workOne(pool, handlers, cfg, never)).toBe("failed");
    const failed = await job(id);
    expect(failed.error).toContain(why);
    expect(failed.attempts).toBe(1);
  });

  it("retries a busy server", async () => {
    const id = await submit("/vod/busy.mp4");
    expect(await workOne(pool, handlers, cfg, never)).toBe("retry");
    expect((await job(id)).error).toContain("HTTP 503");
  });

  it("stops a download the customer cancelled", async () => {
    behaviour = { slowMs: 20 };
    const id = await submit("/vod/meeting.mp4");
    const { running } = await startWorker(id);
    expect(await cancel(pool, id)).toBe("cancelling");
    expect(await running).toBe("cancelled");
    expect((await job(id)).error).toBe("cancelled by request");
  });

  it("hands the job back when the worker shuts down", async () => {
    behaviour = { slowMs: 20 };
    const id = await submit("/vod/meeting.mp4");
    const shutdown = new AbortController();
    const { running } = await startWorker(id, shutdown.signal);
    shutdown.abort();
    expect(await running).toBe("retry");
    const back = await job(id);
    expect(back.status).toBe("queued");
    expect(back.error).toBe("worker shut down mid-job");
  });
});
