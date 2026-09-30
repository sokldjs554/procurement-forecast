import { hostname } from "node:os";
import { z } from "zod";

const list = z
  .string()
  .default("")
  .transform((s) =>
    s
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );

const Env = z.object({
  DATABASE_URL: z.string().startsWith("postgres"),
  PORT: z.coerce.number().int().positive().default(8787),
  // api: HTTP only · worker: download worker only · all: both in one process (local, small deploys)
  ROLE: z.enum(["api", "worker", "all"]).default("all"),
  MEDIA_STORAGE_DIR: z.string().default("./.data/media-in"),
  // Hosts the download stage may fetch from. Empty means nothing: a tenant-supplied URL is an
  // SSRF vector, so every allowed host is a deliberate configuration change.
  FETCH_ALLOWED_HOSTS: list,
  FETCH_ALLOW_INSECURE_HTTP: z.stringbool().default(false),
  FETCH_MAX_BYTES: z.coerce.number().int().positive().default(4 * 1024 ** 3),
  FETCH_TIMEOUT_SECONDS: z.coerce.number().positive().default(3600),
  // What the Python stage worker transcribes with; set by the deployment, never by a tenant.
  STT_SPEC: z.string().default("faster-whisper"),
  WORKER_ID: z.string().default(`${hostname()}:${process.pid}`),
  LEASE_SECONDS: z.coerce.number().int().min(5).default(60),
  HEARTBEAT_SECONDS: z.coerce.number().positive().default(15),
  POLL_SECONDS: z.coerce.number().positive().default(5),
  REAP_SECONDS: z.coerce.number().positive().default(30),
});

export type Config = z.infer<typeof Env>;

export function loadConfig(env: Record<string, string | undefined> = process.env): Config {
  const parsed = Env.safeParse(env);
  if (!parsed.success) {
    throw new Error(`invalid configuration: ${z.prettifyError(parsed.error)}`);
  }
  return parsed.data;
}
