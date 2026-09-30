import { createHash, timingSafeEqual } from "node:crypto";
import { withSystem, type Pool } from "./db.js";

export const SCOPES = ["jobs:read", "jobs:write", "usage:read"] as const;
export type Scope = (typeof SCOPES)[number];

export interface Principal {
  orgId: number;
  keyId: number;
  prefix: string;
  scopes: ReadonlySet<Scope>;
}

// `pfk_<8 hex>.<secret>`, as `manage apikey create` prints it.
const KEY_RE = /^(pfk_[0-9a-f]{8})\.([A-Za-z0-9_-]{20,128})$/;

export function sha256Hex(s: string): string {
  return createHash("sha256").update(s).digest("hex");
}

/**
 * The key's organisation and scopes, or null. The prefix finds the row; the secret is compared
 * by hash in constant time, so neither a timing difference nor a leaked table reveals a key.
 */
export async function authenticate(pool: Pool, header: string | undefined): Promise<Principal | null> {
  const token = header?.match(/^Bearer\s+(\S+)$/i)?.[1];
  const m = token?.match(KEY_RE);
  if (!m) return null;
  const [, prefix, secret] = m as unknown as [string, string, string];
  return withSystem(pool, async (c) => {
    const { rows } = await c.query<{
      id: number;
      org_id: number;
      secret_sha256: string;
      scopes: string[];
      revoked_at: Date | null;
      stale: boolean;
    }>(
      `SELECT id, org_id, secret_sha256, scopes, revoked_at,
              last_used_at IS NULL OR last_used_at < now() - interval '1 minute' AS stale
         FROM api_keys WHERE prefix = $1`,
      [prefix],
    );
    const row = rows[0];
    const given = Buffer.from(sha256Hex(secret));
    const stored = Buffer.from(row?.secret_sha256 ?? "0".repeat(64));
    const match = timingSafeEqual(given, stored);
    if (!row || !match || row.revoked_at) return null;
    if (row.stale) {
      // Once a minute at most: a busy key must not turn every request into a row update.
      await c.query("UPDATE api_keys SET last_used_at = now() WHERE id = $1", [row.id]);
    }
    return {
      orgId: row.org_id,
      keyId: row.id,
      prefix,
      scopes: new Set(row.scopes.filter((s): s is Scope => (SCOPES as readonly string[]).includes(s))),
    };
  });
}
