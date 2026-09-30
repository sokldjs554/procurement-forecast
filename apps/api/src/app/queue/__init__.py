"""Postgres job queue for long, costed, tenant-owned work (council video today).

The semantics — claim with ``SKIP LOCKED``, leases kept alive by heartbeats, retry with jittered
exponential backoff, reaping of jobs whose worker vanished, cancellation down a job tree — are
SQL functions (migration 0005), shared with the TypeScript orchestrator. This package is the
Python side: thin calls to those functions, a checkpoint store, and a worker loop.

The arq/Redis queue stays for the pipeline's own short jobs (ingest, process, link); see
ADR 0013 for why the two coexist.
"""
