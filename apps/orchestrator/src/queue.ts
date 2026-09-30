/**
 * The Postgres job queue from TypeScript: thin calls to the `jobq_*` SQL functions of
 * migration 0005, the same ones the Python stage workers call. Claim, lease, heartbeat,
 * backoff and reaping are defined once, in SQL.
 */
import type { Client, Pool } from "./db.js";

export interface ClaimedJob {
  id: number;
  orgId: number;
  kind: string;
  payload: Record<string, unknown>;
  attempts: number;
  maxAttempts: number;
  budgetUsd: string | null;
  parentId: number | null;
}

export type Heartbeat = "ok" | "cancel" | "lost";
export type FailOutcome = "retry" | "failed" | "cancelled" | "lost";

type Queryable = Pool | Client;

export interface EnqueueArgs {
  orgId: number;
  kind: string;
  payload: Record<string, unknown>;
  dedupeKey?: string | null;
  priority?: number;
  maxAttempts?: number;
  budgetUsd?: number | string | null;
  parentId?: number | null;
}

export async function enqueue(db: Queryable, a: EnqueueArgs): Promise<{ jobId: number; created: boolean }> {
  const { rows } = await db.query<{ job_id: number; created: boolean }>(
    "SELECT job_id, created FROM jobq_enqueue($1, $2, $3::jsonb, $4, $5::smallint, $6, $7, $8)",
    [
      a.orgId,
      a.kind,
      JSON.stringify(a.payload),
      a.dedupeKey ?? null,
      a.priority ?? 0,
      a.maxAttempts ?? 5,
      a.budgetUsd ?? null,
      a.parentId ?? null,
    ],
  );
  const row = rows[0];
  if (!row) throw new Error("jobq_enqueue returned nothing");
  return { jobId: row.job_id, created: row.created };
}

export async function claim(db: Queryable, worker: string, kinds: string[], leaseSeconds: number): Promise<ClaimedJob | null> {
  const { rows } = await db.query<{
    id: number;
    org_id: number;
    kind: string;
    payload: Record<string, unknown>;
    attempts: number;
    max_attempts: number;
    budget_usd: string | null;
    parent_id: number | null;
  }>(
    "SELECT id, org_id, kind, payload, attempts, max_attempts, budget_usd, parent_id FROM jobq_claim($1, $2, $3)",
    [worker, kinds, leaseSeconds],
  );
  const r = rows[0];
  if (!r) return null;
  return {
    id: r.id,
    orgId: r.org_id,
    kind: r.kind,
    payload: r.payload,
    attempts: r.attempts,
    maxAttempts: r.max_attempts,
    budgetUsd: r.budget_usd,
    parentId: r.parent_id,
  };
}

export async function heartbeat(
  db: Queryable,
  jobId: number,
  worker: string,
  leaseSeconds: number,
  progress?: Record<string, unknown>,
): Promise<Heartbeat> {
  const { rows } = await db.query<{ state: Heartbeat }>("SELECT jobq_heartbeat($1, $2, $3, $4::jsonb) AS state", [
    jobId,
    worker,
    leaseSeconds,
    progress ? JSON.stringify(progress) : null,
  ]);
  return rows[0]?.state ?? "lost";
}

export async function complete(
  db: Queryable,
  jobId: number,
  worker: string,
  result: Record<string, unknown>,
  cost: Record<string, unknown> = {},
): Promise<boolean> {
  const { rows } = await db.query<{ done: boolean }>(
    "SELECT jobq_complete($1, $2, $3::jsonb, $4::jsonb) AS done",
    [jobId, worker, JSON.stringify(result), JSON.stringify(cost)],
  );
  return rows[0]?.done ?? false;
}

export async function fail(
  db: Queryable,
  jobId: number,
  worker: string,
  error: string,
  opts: { retryable: boolean; baseSeconds?: number; capSeconds?: number },
): Promise<FailOutcome> {
  const { rows } = await db.query<{ outcome: FailOutcome }>(
    "SELECT jobq_fail($1, $2, $3, $4, $5, $6, NULL) AS outcome",
    [jobId, worker, error.slice(0, 4000), opts.retryable, opts.baseSeconds ?? 15, opts.capSeconds ?? 3600],
  );
  return rows[0]?.outcome ?? "lost";
}

export async function reap(db: Queryable): Promise<number> {
  const { rows } = await db.query<{ n: number }>("SELECT jobq_reap() AS n");
  return rows[0]?.n ?? 0;
}

export async function cancel(db: Queryable, jobId: number): Promise<string> {
  const { rows } = await db.query<{ state: string }>("SELECT jobq_cancel($1) AS state", [jobId]);
  return rows[0]?.state ?? "not_found";
}
