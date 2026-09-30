import pg from "pg";

// bigint ids fit in a double for any table this service will see; numeric money stays exact
// until it is formatted for JSON.
pg.types.setTypeParser(pg.types.builtins.INT8, (v) => Number(v));

export type Pool = pg.Pool;
export type Client = pg.PoolClient;

export function createPool(connectionString: string, max = 10): Pool {
  return new pg.Pool({ connectionString, max, statement_timeout: 30_000 });
}

async function transaction<T>(pool: Pool, setup: string[], params: unknown[][], fn: (c: Client) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    for (const [i, sql] of setup.entries()) await client.query(sql, params[i]);
    const out = await fn(client);
    await client.query("COMMIT");
    return out;
  } catch (err) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw err;
  } finally {
    client.release();
  }
}

/**
 * Everything a request does, as the tenant: row-level security (migration 0005) limits every
 * statement to the organisation's rows, so a query that forgets `WHERE org_id` still cannot
 * read or change another tenant's jobs.
 */
export function withTenant<T>(pool: Pool, orgId: number, fn: (c: Client) => Promise<T>): Promise<T> {
  return transaction(
    pool,
    ["SET LOCAL ROLE app_tenant", "SELECT set_config('app.org_id', $1, true)"],
    [[], [String(orgId)]],
    fn,
  );
}

/** The service's own view (key lookup, queue work): every tenant, no row-level security. */
export function withSystem<T>(pool: Pool, fn: (c: Client) => Promise<T>): Promise<T> {
  return transaction(pool, [], [], fn);
}
