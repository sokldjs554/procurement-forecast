import { serve } from "@hono/node-server";
import { createApp } from "./app.js";
import { loadConfig } from "./config.js";
import { createPool } from "./db.js";
import { log } from "./log.js";
import { FETCH_KIND, fetchHandler } from "./media.js";
import { runWorker } from "./worker.js";

const cfg = loadConfig();
const pool = createPool(cfg.DATABASE_URL);
const shutdown = new AbortController();
const tasks: Promise<unknown>[] = [];

if (cfg.ROLE !== "worker") {
  const server = serve({ fetch: createApp(pool, cfg).fetch, port: cfg.PORT }, (info) =>
    log("info", "http.listening", { port: info.port }),
  );
  tasks.push(
    new Promise<void>((resolve) =>
      shutdown.signal.addEventListener("abort", () => server.close(() => resolve()), { once: true }),
    ),
  );
}
if (cfg.ROLE !== "api") {
  tasks.push(runWorker(pool, { [FETCH_KIND]: fetchHandler(pool, cfg) }, cfg, shutdown.signal));
  log("info", "worker.started", { worker_id: cfg.WORKER_ID, kinds: [FETCH_KIND] });
}

for (const sig of ["SIGTERM", "SIGINT"] as const) {
  process.once(sig, () => {
    log("info", "shutdown", { signal: sig });
    shutdown.abort();
  });
}

await Promise.all(tasks);
await pool.end();
