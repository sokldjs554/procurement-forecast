import { randomBytes } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { sha256Hex, type Scope } from "../src/auth.js";
import { loadConfig, type Config } from "../src/config.js";
import { createPool } from "../src/db.js";

/**
 * A database migrated by the API's Alembic migrations (the schema has one owner):
 *   APP_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/orchestrator_test \
 *     uv run manage db upgrade      # from apps/api
 */
export const DATABASE_URL = process.env.DATABASE_URL ?? "postgres://app:app@localhost:5432/orchestrator_test";

export const pool = createPool(DATABASE_URL, 8);

export async function createOrg(name: string): Promise<number> {
  const { rows } = await pool.query<{ id: number }>("INSERT INTO organizations (name) VALUES ($1) RETURNING id", [name]);
  return rows[0]!.id;
}

/** The same shape `manage apikey create` issues; only the hash of the secret is stored. */
export async function createKey(orgId: number, scopes: Scope[], opts: { revoked?: boolean } = {}): Promise<string> {
  const prefix = `pfk_${randomBytes(4).toString("hex")}`;
  const secret = randomBytes(32).toString("base64url");
  await pool.query(
    `INSERT INTO api_keys (org_id, name, prefix, secret_sha256, scopes, revoked_at)
     VALUES ($1, 'test', $2, $3, $4, CASE WHEN $5 THEN now() END)`,
    [orgId, prefix, sha256Hex(secret), scopes, opts.revoked ?? false],
  );
  return `${prefix}.${secret}`;
}

export function testConfig(overrides: Record<string, string> = {}): Config {
  return loadConfig({
    DATABASE_URL,
    MEDIA_STORAGE_DIR: mkdtempSync(join(tmpdir(), "orch-media-")),
    FETCH_ALLOWED_HOSTS: "127.0.0.1",
    FETCH_ALLOW_INSECURE_HTTP: "true",
    STT_SPEC: "fixture:/fixtures/meeting.json",
    WORKER_ID: "test-worker",
    LEASE_SECONDS: "30",
    HEARTBEAT_SECONDS: "0.05",
    ...overrides,
  });
}

export async function job(id: number) {
  const { rows } = await pool.query<{
    id: number;
    status: string;
    error: string | null;
    result: Record<string, unknown> | null;
    attempts: number;
    parent_id: number | null;
    kind: string;
    payload: Record<string, unknown>;
    dedupe_key: string | null;
  }>("SELECT * FROM jobs WHERE id = $1", [id]);
  return rows[0]!;
}

export const uniqueKind = () => `test.${randomBytes(4).toString("hex")}`;
