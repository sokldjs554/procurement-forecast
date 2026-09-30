import { afterAll, expect, it } from "vitest";
import { enqueue, reap } from "../src/queue.js";
import { workOne, type Handler } from "../src/worker.js";
import { createOrg, job, pool, testConfig, uniqueKind } from "./helpers.js";

afterAll(async () => {
  await pool.end();
});

const never = new AbortController().signal;

it("gives every job to exactly one of several racing workers", async () => {
  const org = await createOrg("race");
  const kind = uniqueKind();
  const ids: number[] = [];
  for (let i = 0; i < 24; i++) ids.push((await enqueue(pool, { orgId: org, kind, payload: { i } })).jobId);

  const ran: number[] = [];
  const handler: Handler = async (j) => {
    ran.push(j.id);
    await new Promise((r) => setTimeout(r, 5));
    return { ok: true };
  };
  const worker = async (n: number) => {
    const cfg = testConfig({ WORKER_ID: `racer-${n}` });
    while ((await workOne(pool, { [kind]: handler }, cfg, never)) !== null);
  };
  await Promise.all([0, 1, 2, 3, 4, 5].map(worker));

  expect(ran.toSorted((a, b) => a - b)).toEqual(ids);
  const { rows } = await pool.query<{ status: string; n: number }>(
    "SELECT status, count(*)::int AS n FROM jobs WHERE kind = $1 GROUP BY status",
    [kind],
  );
  expect(rows).toEqual([{ status: "succeeded", n: 24 }]);
});

it("stops a handler whose lease was taken back, and writes nothing", async () => {
  const org = await createOrg("lease");
  const kind = uniqueKind();
  const { jobId } = await enqueue(pool, { orgId: org, kind, payload: {} });
  let aborted = false;
  const slow: Handler = (_, ctx) =>
    new Promise((_resolve, reject) => {
      ctx.signal.addEventListener("abort", () => {
        aborted = true;
        reject(new Error("aborted"));
      });
    });
  const cfg = testConfig({ WORKER_ID: "sleepy", LEASE_SECONDS: "5", HEARTBEAT_SECONDS: "0.3" });
  const running = workOne(pool, { [kind]: slow }, cfg, never);
  await new Promise((r) => setTimeout(r, 100));
  // The worker is alive, but as far as the queue knows its lease ran out: reap and requeue.
  await pool.query("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = $1", [jobId]);
  expect(await reap(pool)).toBeGreaterThanOrEqual(1);
  expect(await running).toBe("lost");
  expect(aborted).toBe(true);
  const row = await job(jobId);
  expect(row.status).toBe("queued");
  expect(row.result).toBeNull();
});
