/**
 * The TypeScript stage worker: the same contract as the Python one (app/queue/worker.py).
 * A heartbeat keeps the lease and publishes progress; its answer aborts the handler when the
 * job is cancelled or the lease is lost. Shutdown hands the job straight back to the queue.
 */
import pg from "pg";
import type { Config } from "./config.js";
import type { Pool } from "./db.js";
import { log } from "./log.js";
import { claim, complete, fail, heartbeat, reap, type ClaimedJob, type FailOutcome } from "./queue.js";

export class PermanentError extends Error {
  override name = "PermanentError";
}

export interface JobContext {
  job: ClaimedJob;
  /** Aborted on cancel, lost lease or shutdown; pass it to every fetch and stream. */
  signal: AbortSignal;
  progress: Record<string, unknown>;
}

export type Handler = (job: ClaimedJob, ctx: JobContext) => Promise<Record<string, unknown>>;
export type Outcome = "succeeded" | FailOutcome;

type Stop = "cancel" | "lost" | "shutdown";

export async function workOne(
  pool: Pool,
  handlers: Record<string, Handler>,
  cfg: Pick<Config, "WORKER_ID" | "LEASE_SECONDS" | "HEARTBEAT_SECONDS">,
  shutdown: AbortSignal,
): Promise<Outcome | null> {
  if (shutdown.aborted) return null;
  const job = await claim(pool, cfg.WORKER_ID, Object.keys(handlers), cfg.LEASE_SECONDS);
  if (!job) return null;
  const handler = handlers[job.kind];
  if (!handler) throw new Error(`no handler for ${job.kind}`);

  const aborter = new AbortController();
  let stop: Stop | null = null;
  const halt = (why: Stop) => {
    stop ??= why;
    aborter.abort(new Error(why));
  };
  const onShutdown = () => halt("shutdown");
  shutdown.addEventListener("abort", onShutdown, { once: true });
  const ctx: JobContext = { job, signal: aborter.signal, progress: {} };

  let beating = false;
  const beat = setInterval(() => {
    if (beating) return;
    beating = true;
    heartbeat(pool, job.id, cfg.WORKER_ID, cfg.LEASE_SECONDS, ctx.progress)
      .then((state) => {
        if (state !== "ok") halt(state);
      })
      .catch((err: unknown) => log("warn", "queue.heartbeat_failed", { job_id: job.id, error: String(err) }))
      .finally(() => {
        beating = false;
      });
  }, cfg.HEARTBEAT_SECONDS * 1000);

  log("info", "queue.claimed", { job_id: job.id, kind: job.kind, attempt: job.attempts });
  try {
    const result = await handler(job, ctx);
    if (stop === "lost") return "lost";
    if (stop === "cancel") return await fail(pool, job.id, cfg.WORKER_ID, "cancelled by request", { retryable: false });
    if (!(await complete(pool, job.id, cfg.WORKER_ID, result))) {
      log("warn", "queue.lease_lost_at_completion", { job_id: job.id });
      return "lost";
    }
    log("info", "queue.succeeded", { job_id: job.id, kind: job.kind });
    return "succeeded";
  } catch (err) {
    const halted = stop as Stop | null;
    if (halted === "lost") {
      log("warn", "queue.lease_lost", { job_id: job.id });
      return "lost";
    }
    if (halted === "cancel") {
      return await fail(pool, job.id, cfg.WORKER_ID, "cancelled by request", { retryable: false });
    }
    if (halted === "shutdown") {
      return await fail(pool, job.id, cfg.WORKER_ID, "worker shut down mid-job", {
        retryable: true,
        baseSeconds: 1,
        capSeconds: 1,
      });
    }
    const message = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
    const outcome = await fail(pool, job.id, cfg.WORKER_ID, message, { retryable: !(err instanceof PermanentError) });
    log("warn", "queue.job_failed", { job_id: job.id, outcome, error: message.slice(0, 300) });
    return outcome;
  } finally {
    clearInterval(beat);
    shutdown.removeEventListener("abort", onShutdown);
  }
}

export async function runWorker(
  pool: Pool,
  handlers: Record<string, Handler>,
  cfg: Pick<Config, "DATABASE_URL" | "WORKER_ID" | "LEASE_SECONDS" | "HEARTBEAT_SECONDS" | "POLL_SECONDS" | "REAP_SECONDS">,
  shutdown: AbortSignal,
): Promise<void> {
  const kinds = Object.keys(handlers);
  let wake: (() => void) | null = null;
  // LISTEN so an idle worker starts within milliseconds; polling covers a missed NOTIFY.
  const listener = new pg.Client({ connectionString: cfg.DATABASE_URL });
  try {
    await listener.connect();
    listener.on("notification", (msg) => {
      if (msg.payload && kinds.includes(msg.payload)) wake?.();
    });
    await listener.query("LISTEN jobq");
  } catch (err) {
    log("warn", "queue.listen_unavailable", { error: String(err) });
  }
  let lastReap = 0;
  try {
    while (!shutdown.aborted) {
      if (Date.now() - lastReap >= cfg.REAP_SECONDS * 1000) {
        const n = await reap(pool);
        if (n) log("warn", "queue.reaped", { jobs: n });
        lastReap = Date.now();
      }
      if ((await workOne(pool, handlers, cfg, shutdown)) !== null) continue;
      await new Promise<void>((resolve) => {
        const timer = setTimeout(done, cfg.POLL_SECONDS * 1000);
        function done() {
          clearTimeout(timer);
          wake = null;
          shutdown.removeEventListener("abort", done);
          resolve();
        }
        wake = done;
        shutdown.addEventListener("abort", done, { once: true });
      });
    }
  } finally {
    await listener.end().catch(() => undefined);
  }
}
