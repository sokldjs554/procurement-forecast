import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createApp, kstMonth } from "../src/app.js";
import { createKey, createOrg, pool, testConfig } from "./helpers.js";

const cfg = testConfig({ FETCH_ALLOWED_HOSTS: "videos.example.test", FETCH_ALLOW_INSECURE_HTTP: "false" });
const app = createApp(pool, cfg);

let orgA: number, orgB: number;
let writerA: string, readerA: string, writerB: string, usageA: string, revokedA: string;

beforeAll(async () => {
  orgA = await createOrg("api-a");
  orgB = await createOrg("api-b");
  writerA = await createKey(orgA, ["jobs:write", "jobs:read"]);
  readerA = await createKey(orgA, ["jobs:read"]);
  usageA = await createKey(orgA, ["usage:read"]);
  revokedA = await createKey(orgA, ["jobs:write", "jobs:read"], { revoked: true });
  writerB = await createKey(orgB, ["jobs:write", "jobs:read"]);
});

afterAll(async () => {
  await pool.end();
});

const body = (n: number) => ({
  source_url: `https://videos.example.test/vod/${n}.mp4`,
  title: "제300회 도시건설위원회 제2차 회의",
  meeting_date: "2026-03-18",
  publisher: "경기도 성남시의회",
});

function call(method: string, path: string, key?: string, json?: unknown, headers: Record<string, string> = {}) {
  return app.request(path, {
    method,
    headers: {
      ...(key ? { authorization: `Bearer ${key}` } : {}),
      ...(json ? { "content-type": "application/json" } : {}),
      ...headers,
    },
    ...(json ? { body: JSON.stringify(json) } : {}),
  });
}

describe("authentication and scopes", () => {
  it.each([
    ["no key", undefined],
    ["not a key", "hello"],
    ["unknown prefix", "pfk_00000000.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
    ["revoked", "REVOKED"],
  ])("refuses %s with 401", async (_, key) => {
    const res = await call("GET", "/v1/jobs", key === "REVOKED" ? revokedA : key);
    expect(res.status).toBe(401);
    expect(res.headers.get("content-type")).toContain("application/problem+json");
    expect(res.headers.get("www-authenticate")).toContain("Bearer");
  });

  it("refuses a right prefix with a wrong secret", async () => {
    const [prefix] = writerA.split(".");
    const res = await call("GET", "/v1/jobs", `${prefix}.${"x".repeat(43)}`);
    expect(res.status).toBe(401);
  });

  it("needs the route's scope", async () => {
    expect((await call("POST", "/v1/media-jobs", readerA, body(1))).status).toBe(403);
    expect((await call("GET", "/v1/usage", readerA)).status).toBe(403);
    expect((await call("GET", "/v1/jobs", usageA)).status).toBe(403);
  });
});

describe("media jobs", () => {
  it("accepts a video once and returns the same job for the same URL", async () => {
    const first = await call("POST", "/v1/media-jobs", writerA, { ...body(10), budget_usd: 1.5 });
    expect(first.status).toBe(202);
    const job = (await first.json()) as { id: number; status: string; kind: string; input: unknown; budget_usd: number };
    expect(job).toMatchObject({ status: "queued", kind: "media.fetch", budget_usd: 1.5, input: body(10) });
    expect(first.headers.get("location")).toBe(`/v1/jobs/${job.id}`);

    const again = await call("POST", "/v1/media-jobs", writerA, body(10));
    expect(again.status).toBe(200);
    expect(((await again.json()) as { id: number }).id).toBe(job.id);

    // Another tenant sending the same URL gets its own job.
    const other = await call("POST", "/v1/media-jobs", writerB, body(10));
    expect(other.status).toBe(202);
    expect(((await other.json()) as { id: number }).id).not.toBe(job.id);
  });

  it("dedupes by the client's Idempotency-Key when given", async () => {
    const a = await call("POST", "/v1/media-jobs", writerA, body(20), { "idempotency-key": "retry-20" });
    const b = await call("POST", "/v1/media-jobs", writerA, body(21), { "idempotency-key": "retry-20" });
    expect([a.status, b.status]).toEqual([202, 200]);
    expect(((await b.json()) as { id: number }).id).toBe(((await a.json()) as { id: number }).id);
    expect((await call("POST", "/v1/media-jobs", writerA, body(22), { "idempotency-key": "no spaces" })).status).toBe(400);
  });

  it.each([
    ["a host off the allowlist", { source_url: "https://example.com/v.mp4" }, "allowlist"],
    ["plain http", { source_url: "http://videos.example.test/v.mp4" }, "https"],
    ["credentials in the URL", { source_url: "https://u:p@videos.example.test/v.mp4" }, "credentials"],
    ["a bad date", { meeting_date: "2026-13-01" }, "meeting_date"],
    ["a missing title", { title: "" }, "title"],
  ])("refuses %s with 422", async (_, patch, why) => {
    const res = await call("POST", "/v1/media-jobs", writerA, { ...body(30), ...patch });
    expect(res.status).toBe(422);
    expect(((await res.json()) as { detail: string }).detail).toContain(why);
  });
});

describe("tenant isolation", () => {
  it("shows, lists and cancels only the tenant's own jobs", async () => {
    const mine = (await (await call("POST", "/v1/media-jobs", writerA, body(40))).json()) as { id: number };
    const theirs = (await (await call("POST", "/v1/media-jobs", writerB, body(41))).json()) as { id: number };

    expect((await call("GET", `/v1/jobs/${theirs.id}`, writerA)).status).toBe(404);
    expect((await call("POST", `/v1/jobs/${theirs.id}/cancel`, writerA)).status).toBe(404);
    const list = (await (await call("GET", "/v1/jobs?limit=100", readerA)).json()) as { jobs: { id: number }[] };
    expect(list.jobs.map((j) => j.id)).toContain(mine.id);
    expect(list.jobs.map((j) => j.id)).not.toContain(theirs.id);

    const cancelled = await call("POST", `/v1/jobs/${mine.id}/cancel`, writerA);
    expect(cancelled.status).toBe(200);
    expect(await cancelled.json()).toEqual({ id: mine.id, status: "cancelled" });
    const theirJob = await call("GET", `/v1/jobs/${theirs.id}`, writerB);
    expect(((await theirJob.json()) as { status: string }).status).toBe("queued");
  });

  it("pages the list newest first", async () => {
    const res = await call("GET", "/v1/jobs?limit=1", readerA);
    const page = (await res.json()) as { jobs: { id: number }[]; next_before: number };
    expect(page.jobs).toHaveLength(1);
    const next = (await (await call("GET", `/v1/jobs?limit=1&before=${page.next_before}`, readerA)).json()) as {
      jobs: { id: number }[];
    };
    expect(next.jobs[0]!.id).toBeLessThan(page.jobs[0]!.id);
    expect((await call("GET", "/v1/jobs?status=exploded", readerA)).status).toBe(422);
  });
});

describe("usage", () => {
  it("sums the tenant's metered usage for a KST month and shows its cap", async () => {
    const { month, start } = kstMonth(undefined);
    await pool.query("UPDATE organizations SET usage_cap_usd = 25 WHERE id = $1", [orgA]);
    for (const [org, meter, qty, usd, key] of [
      [orgA, "stt_audio_seconds", 3600, 0.36, "a1"],
      [orgA, "stt_audio_seconds", 1800, 0.18, "a2"],
      [orgA, "ocr_frames", 40, 0, "a3"],
      [orgB, "stt_audio_seconds", 7200, 0.72, "b1"],
    ] as const) {
      await pool.query(
        `INSERT INTO usage_events (org_id, meter, quantity, usd, idempotency_key, created_at)
         VALUES ($1, $2, $3, $4, $5, $6)`,
        [org, meter, qty, usd, `usage-test-${org}-${key}`, new Date(start.getTime() + 3600_000)],
      );
    }
    const res = await call("GET", `/v1/usage?month=${month}`, usageA);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      month,
      timezone: "Asia/Seoul",
      meters: [
        { meter: "ocr_frames", quantity: 40, usd: 0, events: 1 },
        { meter: "stt_audio_seconds", quantity: 5400, usd: 0.54, events: 2 },
      ],
      total_usd: 0.54,
      cap_usd: 25,
    });
    expect((await call("GET", "/v1/usage?month=2026-9", usageA)).status).toBe(422);
  });

  it("bounds a month in Korean time", () => {
    const { start, end } = kstMonth("2026-09");
    expect(start.toISOString()).toBe("2026-08-31T15:00:00.000Z");
    expect(end.toISOString()).toBe("2026-09-30T15:00:00.000Z");
    // 14:30Z is 23:30 KST on 30 September; 15:30Z is 00:30 KST on 1 October.
    expect(kstMonth(undefined, new Date("2026-09-30T14:30:00Z")).month).toBe("2026-09");
    expect(kstMonth(undefined, new Date("2026-09-30T15:30:00Z")).month).toBe("2026-10");
  });
});

it("serves a health check and problem documents for unknown routes", async () => {
  expect(await (await app.request("/healthz")).json()).toEqual({ ok: true });
  const res = await call("GET", "/v1/nope", readerA);
  expect(res.status).toBe(404);
  expect(res.headers.get("x-request-id")).toBeTruthy();
});
