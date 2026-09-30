/**
 * The job API. Every /v1 request carries an API key (`Authorization: Bearer pfk_….…`), is
 * checked for the scope its route needs, and runs as the key's tenant under row-level
 * security. Errors are RFC 9457 problem documents.
 */
import { randomUUID } from "node:crypto";
import { Hono, type Context, type Input } from "hono";
import type { ContentfulStatusCode } from "hono/utils/http-status";
import { z } from "zod";
import { authenticate, sha256Hex, type Principal, type Scope } from "./auth.js";
import type { Config } from "./config.js";
import { withSystem, withTenant, type Client, type Pool } from "./db.js";
import { log } from "./log.js";
import { FETCH_KIND, MediaJobInput, refuseUrl } from "./media.js";
import { cancel, enqueue } from "./queue.js";

type Env = { Variables: { principal: Principal; requestId: string } };

function problem<P extends string, I extends Input>(c: Context<Env, P, I>, status: ContentfulStatusCode, title: string, detail?: string) {
  return c.json({ type: "about:blank", title, status, ...(detail ? { detail } : {}) }, status, {
    "content-type": "application/problem+json",
  });
}

const JOB_COLUMNS = `id, parent_id, kind, status, progress, attempts, max_attempts, error, cost,
  budget_usd, result, created_at, started_at, finished_at, payload`;

interface JobRow {
  id: number;
  parent_id: number | null;
  kind: string;
  status: string;
  progress: Record<string, unknown>;
  attempts: number;
  max_attempts: number;
  error: string | null;
  cost: Record<string, unknown>;
  budget_usd: string | null;
  result: Record<string, unknown> | null;
  created_at: Date;
  started_at: Date | null;
  finished_at: Date | null;
  payload: Record<string, unknown>;
}

function jobJson(row: JobRow) {
  return {
    id: row.id,
    parent_id: row.parent_id,
    kind: row.kind,
    status: row.status,
    progress: row.progress,
    attempts: row.attempts,
    max_attempts: row.max_attempts,
    error: row.error,
    cost: row.cost,
    budget_usd: row.budget_usd === null ? null : Number(row.budget_usd),
    // The root job's payload is what the customer sent; a stage's payload is internal (paths,
    // engine), so only its result is shown.
    input: row.parent_id === null ? row.payload : undefined,
    result: row.result,
    created_at: row.created_at,
    started_at: row.started_at,
    finished_at: row.finished_at,
  };
}

async function jobTree(c: Client, id: number) {
  const { rows } = await c.query<JobRow>(
    `WITH RECURSIVE tree AS (
       SELECT id FROM jobs WHERE id = $1
       UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id = t.id
     )
     SELECT ${JOB_COLUMNS} FROM jobs WHERE id IN (SELECT id FROM tree) ORDER BY id`,
    [id],
  );
  const [root, ...stages] = rows;
  return root ? { ...jobJson(root), stages: stages.map(jobJson) } : null;
}

/** KST month [start, end) for "2026-09"; the month customers are billed by. */
export function kstMonth(month: string | undefined, now = new Date()): { month: string; start: Date; end: Date } {
  const kst = new Date(now.getTime() + 9 * 3600_000);
  const label = month ?? `${kst.getUTCFullYear()}-${String(kst.getUTCMonth() + 1).padStart(2, "0")}`;
  const [y, m] = label.split("-").map(Number) as [number, number];
  return {
    month: label,
    start: new Date(Date.UTC(y, m - 1, 1) - 9 * 3600_000),
    end: new Date(Date.UTC(y, m, 1) - 9 * 3600_000),
  };
}

export function createApp(pool: Pool, cfg: Config) {
  const app = new Hono<Env>();

  app.use("*", async (c, next) => {
    const requestId = c.req.header("x-request-id") ?? randomUUID();
    c.set("requestId", requestId);
    const started = performance.now();
    await next();
    c.header("x-request-id", requestId);
    log("info", "http.request", {
      request_id: requestId,
      method: c.req.method,
      path: c.req.path,
      status: c.res.status,
      ms: Math.round(performance.now() - started),
      org_id: c.var.principal?.orgId,
      key: c.var.principal?.prefix,
    });
  });

  app.onError((err, c) => {
    log("error", "http.error", { request_id: c.var.requestId, error: String(err) });
    return problem(c, 500, "Internal Server Error");
  });

  app.get("/healthz", async (c) => {
    await pool.query("SELECT 1");
    return c.json({ ok: true });
  });

  app.use("/v1/*", async (c, next) => {
    const principal = await authenticate(pool, c.req.header("authorization"));
    if (!principal) {
      c.header("www-authenticate", 'Bearer realm="jobs"');
      return problem(c, 401, "Unauthorized", "a valid API key is required");
    }
    c.set("principal", principal);
    await next();
  });

  const need = (scope: Scope) => async (c: Context<Env>, next: () => Promise<void>) => {
    if (!c.var.principal.scopes.has(scope)) return problem(c, 403, "Forbidden", `this key lacks ${scope}`);
    await next();
  };

  app.post("/v1/media-jobs", need("jobs:write"), async (c) => {
    const body = MediaJobInput.extend({ budget_usd: z.number().positive().max(10_000).optional() }).safeParse(
      await c.req.json().catch(() => null),
    );
    if (!body.success) return problem(c, 422, "Unprocessable Content", z.prettifyError(body.error));
    const { budget_usd, ...input } = body.data;
    const refused = refuseUrl(cfg, new URL(input.source_url));
    if (refused) return problem(c, 422, "Unprocessable Content", refused);
    const idem = c.req.header("idempotency-key");
    if (idem !== undefined && !/^[\w.:-]{1,100}$/.test(idem)) {
      return problem(c, 400, "Bad Request", "Idempotency-Key: 1–100 of [A-Za-z0-9_.:-]");
    }
    const { principal } = c.var;
    const out = await withTenant(pool, principal.orgId, async (tx) => {
      const { jobId, created } = await enqueue(tx, {
        orgId: principal.orgId,
        kind: FETCH_KIND,
        payload: input,
        // Same video URL (or the same client key) → the job that already has it.
        dedupeKey: idem ? `idem:${idem}` : `url:${sha256Hex(input.source_url)}`,
        budgetUsd: budget_usd ?? null,
      });
      return { created, job: await jobTree(tx, jobId) };
    });
    c.header("location", `/v1/jobs/${out.job?.id}`);
    return c.json(out.job, out.created ? 202 : 200);
  });

  app.get("/v1/jobs/:id{[0-9]+}", need("jobs:read"), async (c) => {
    const job = await withTenant(pool, c.var.principal.orgId, (tx) => jobTree(tx, Number(c.req.param("id"))));
    return job ? c.json(job) : problem(c, 404, "Not Found");
  });

  const ListQuery = z.object({
    status: z.enum(["queued", "running", "succeeded", "failed", "cancelled"]).optional(),
    limit: z.coerce.number().int().min(1).max(100).default(20),
    before: z.coerce.number().int().positive().optional(),
  });

  app.get("/v1/jobs", need("jobs:read"), async (c) => {
    const q = ListQuery.safeParse(c.req.query());
    if (!q.success) return problem(c, 422, "Unprocessable Content", z.prettifyError(q.error));
    const { status, limit, before } = q.data;
    const rows = await withTenant(pool, c.var.principal.orgId, async (tx) => {
      const { rows } = await tx.query<JobRow>(
        `SELECT ${JOB_COLUMNS} FROM jobs
          WHERE parent_id IS NULL AND ($1::text IS NULL OR status = $1) AND ($2::bigint IS NULL OR id < $2)
          ORDER BY id DESC LIMIT $3`,
        [status ?? null, before ?? null, limit],
      );
      return rows;
    });
    const last = rows.at(-1);
    return c.json({ jobs: rows.map(jobJson), next_before: rows.length === limit && last ? last.id : null });
  });

  app.post("/v1/jobs/:id{[0-9]+}/cancel", need("jobs:write"), async (c) => {
    const state = await withTenant(pool, c.var.principal.orgId, (tx) => cancel(tx, Number(c.req.param("id"))));
    if (state === "not_found") return problem(c, 404, "Not Found");
    if (state === "succeeded" || state === "failed") {
      return problem(c, 409, "Conflict", `the job already ${state}`);
    }
    return c.json({ id: Number(c.req.param("id")), status: state }, state === "cancelling" ? 202 : 200);
  });

  app.get("/v1/usage", need("usage:read"), async (c) => {
    const month = c.req.query("month");
    if (month !== undefined && !/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
      return problem(c, 422, "Unprocessable Content", "month: YYYY-MM");
    }
    const range = kstMonth(month);
    const { orgId } = c.var.principal;
    const meters = await withTenant(pool, orgId, async (tx) => {
      const { rows } = await tx.query<{ meter: string; quantity: string; usd: string; events: number }>(
        `SELECT meter, sum(quantity) AS quantity, sum(usd) AS usd, count(*)::int AS events
           FROM usage_events WHERE created_at >= $1 AND created_at < $2
          GROUP BY meter ORDER BY meter`,
        [range.start, range.end],
      );
      return rows;
    });
    // The cap lives on the organisation, which a tenant session cannot read.
    const cap = await withSystem(pool, async (tx) => {
      const { rows } = await tx.query<{ cap: string | null }>(
        "SELECT usage_cap_usd AS cap FROM organizations WHERE id = $1",
        [orgId],
      );
      return rows[0]?.cap ?? null;
    });
    const totalUsd = meters.reduce((sum, m) => sum + Number(m.usd), 0);
    return c.json({
      month: range.month,
      timezone: "Asia/Seoul",
      meters: meters.map((m) => ({ meter: m.meter, quantity: Number(m.quantity), usd: Number(m.usd), events: m.events })),
      total_usd: Number(totalUsd.toFixed(6)),
      cap_usd: cap === null ? null : Number(cap),
    });
  });

  app.notFound((c) => problem(c, 404, "Not Found"));
  return app;
}
