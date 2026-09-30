# Automatic link convergence

The existing incremental linker is greedy: candidate summaries, generic-name detection,
and runner-up scores depend on earlier attachments. On the retained 2,404-signal archive,
6 / 89 accepted signals have different memberships in budget-first / reverse arrival.
Sorting one input batch cannot repair this.

## Contract

- A completed reconciliation derives automatic memberships from the same retained,
  already-linked eligible evidence in canonical source/content order, independent of
  arrival batches and database-generated IDs. Incremental attachments are provisional.
- Worker processing persists a dirty institution generation in the linking transaction.
  A reconciliation job and periodic sweep converge it, then dispatch recommendations
  through a durable generation marker. Backfill and replay use the same completion step.
- The replay must not inspect accepted signals from batches that have not linked yet.
- Human-reviewed/manual groups and relation endpoints/evidence remain fixed. Customer
  feedback/notifications/briefs capture the original member core and identity; later
  automatic evidence can extend that lifecycle and is replayed on every reconciliation.
  The guarantee conditions on the same original cores and human decisions. It does not
  replace human decisions or assert linking accuracy. A blanket post-alert append ban
  was rejected during review because it would silently stop the primary product flow.
- Plan in memory using shared production link rules; validate the input snapshot under
  short maintenance locks before applying membership deltas. A changed snapshot fails
  without partial writes and is retryable. No network/LLM calls.
- Reuse unchanged opportunity IDs, retain displaced IDs as dormant with zero claims,
  preserve prior link metadata in append-only events, and retain all customer history.

## Work

- [x] Shared pure decision/summary rules and canonical partition planner; permutation tests.
- [x] Durable generation/outbox state, migration, protected-scope snapshot and atomic apply.
- [x] Worker/backfill/replay completion integration and unchanged-snapshot retries.
- [x] SQL tests for insertion/batch permutations, protection, IDs, audit and idempotency.
- [x] Full before/after evaluation, all retained arrival variants, operational query benchmark.
- [x] Review, CI and main integration; publish measured results and remaining external limits.

Production DB access, independent labeled real data, and elapsed observation time are
separate prerequisites. This change cannot manufacture them or imply actual production
migrations were executed.

## Verification record

Final code `9834b6a`: CI 36666923090 (782 API tests, 61 web tests, 3 browser tests;
zero API skips) and audit 36666923259 passed. The latter includes the complete archive
matrix and the independent default-scale benchmark. A 64–72 second history-scan plan
found in the previous audit blocked integration; indexed matching counts and customer
opportunity indexes reduced the same protected scopes to 439/533 ms. The benchmark
now gates protection latency and true hit/miss row counts. No gold labels or existing
quality thresholds were lowered. Main integration and measured documentation are
tracked by PR #54 and its documentation follow-up.
