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
- Human-reviewed/manual groups, relation endpoints/evidence, and groups with customer
  feedback, notifications or briefs are protected as whole identities. The guarantee
  is for automatic evidence with the same protected decisions, not a replacement for
  human decisions or an assertion of linking accuracy.
- Plan in memory using shared production link rules; validate the input snapshot under
  short maintenance locks before applying membership deltas. A changed snapshot fails
  without partial writes and is retryable. No network/LLM calls.
- Reuse unchanged opportunity IDs, retain displaced IDs as dormant with zero claims,
  preserve prior link metadata in append-only events, and retain all customer history.

## Work

- [ ] Shared pure decision/summary rules and canonical partition planner; permutation tests.
- [ ] Durable generation/outbox state, migration, protected-scope snapshot and atomic apply.
- [ ] Worker/backfill/replay completion integration and unchanged-snapshot retries.
- [ ] SQL tests for insertion/batch permutations, protection, IDs, audit and idempotency.
- [ ] Full before/after evaluation, all retained arrival variants, operational query benchmark.
- [ ] Review, CI and main integration; publish measured results and remaining external limits.

Production DB access, independent labeled real data, and elapsed observation time are
separate prerequisites. This change cannot manufacture them or imply actual production
migrations were executed.
