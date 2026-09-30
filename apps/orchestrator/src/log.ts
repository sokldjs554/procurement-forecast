type Level = "debug" | "info" | "warn" | "error";

const order: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 };
const threshold = order[(process.env.LOG_LEVEL as Level | undefined) ?? "info"] ?? order.info;

/** One JSON object per line, like the Python services' structlog output. */
export function log(level: Level, event: string, fields: Record<string, unknown> = {}): void {
  if (order[level] < threshold) return;
  const line = JSON.stringify({ ts: new Date().toISOString(), level, event, service: "orchestrator", ...fields });
  if (level === "error" || level === "warn") console.error(line);
  else console.log(line);
}
