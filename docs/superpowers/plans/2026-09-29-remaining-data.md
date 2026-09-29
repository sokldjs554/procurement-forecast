# Remaining Data Limitations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or independent tasks under superpowers:dispatching-parallel-agents. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Ship auditable project/contract relationships, safer budget classification, source coverage/revision tracking and reusable review evidence.

**Architecture:** Add relationships without changing existing signal ownership. Fix independent data-quality defects in their current modules and expose measured diagnostics rather than invent unavailable truth.

**Tech Stack:** Python 3.11, FastAPI, PostgreSQL/SQLAlchemy/Alembic, Next.js 16.

**Spec:** `docs/remaining-data-spec.md`

## Global Constraints

- Preserve existing signal, review, feedback and notification history.
- Extraction/link P/R ≥ 0.95; no truth_id in pipeline; no paid LLM calls.
- Benchmark query changes and compare `manage eval all` before/after.
- Public demo hosting is owned by the user and excluded.

## Review Focus

- A revoked source signal must not leave an apparently confirmed relationship visible.
- Repeated/conflicting relationship decisions must preserve history and prevent lost updates.
- Generic budget account labels must not override the project's purpose.
- New source revisions must preserve reviewed original evidence on refusal.
- Exported evaluation rows must neither leak a document between splits nor silently omit missing evidence.

### Task 1: Project-contract relations

**Files:** `db/models.py`, new migration, `pipeline/relations.py`, new API router/schemas, admin and opportunity web pages, new unit/integration tests.

**Interfaces:** relationship input uses project_id, contract_id, evidence_signal_ids, note, status and expected_version; outputs versioned relations with history and evidence. API paths under `/api/admin/relations` and `/api/opportunities/{id}/relations`.

- [x] Write tests for 1:N, institution mismatch, unsuitable source/destination, idempotent review, stale writes, source rejection and access control; observe failure.
- [x] Implement additive persistence, transactional review, non-destructive mixed-group audit and UI.
- [x] Run related tests, schema generation, web checks and migration CI.

### Task 2: Budget classification

**Files:** `domain/taxonomy.py`, `llm/providers/heuristic.py`, relevant new tests and regression evaluation artifact.

**Interfaces:** keep `classify_category(text)` compatible; add explicit budget title/detail classifier if needed.

- [x] Reproduce title-vs-account contamination and technical-word misclassification with independent assertions.
- [x] Apply purpose-first rules and conservative ambiguity handling without modifying golden answers.
- [x] Run unit suite, real-archive regression comparison and CI `manage eval all` before/after.

### Task 3: Coverage and revisions

**Files:** `sources/clik.py`, new `sources/coverage.py`, tests. Root integrates CLI separately.

**Interfaces:** read-only coverage report with per-institution configured/collected/raw-available counts; revision selection accepts previous revision IDs through adapter configuration or cursor.

- [x] Test revised DOCID refetch and unchanged revision skip; keep document review protection.
- [x] Implement coverage query and revision metadata without claiming uncollected national coverage.
- [x] Run source tests and add operational-scale coverage benchmark.

### Task 4: Review dataset and replay diagnostics

**Files:** new `eval/review_dataset.py`, `pipeline/link_replay.py`, `cli.py`, tests, audit script and evaluation docs.

**Interfaces:** deterministic JSONL export with original hashes and document-based split; replay `order` mode with explicit retrospective scope.

- [x] Test repeatability, missing source exclusion, document split isolation and order selection.
- [x] Implement export and ordering diagnostics with no API calls or source mutations.
- [x] Add `eval/forecast_snapshot.py` and meaningful unit/SQL tests: current observed state only, consistent source/link/prediction snapshot, atomic file and digest, explicit missing inputs, fail on truncation. Root integrates `manage eval freeze` CLI.
- [x] Run real archive variants in separate databases and record differences without treating grouping agreement as accuracy.

### Task 5: Integrate and verify

- [x] Run lint/type/unit/API/web/integration gates; benchmark added queries.
- [x] Review branch, fix material findings, verify CI tree and merge authorized repository changes.
- [x] Update README and status with measured results and external-data dependencies; report exact commit and remaining constraints.

## Verification record

- PR #53 merged as `dfec29a`; tested merge `be55e15` has identical tree `b7f283b216ca68d25756cdf1c21aa19d96402b19`.
- CI 36567195440: PostgreSQL/Redis 685 tests, web61, browser3, static/schema/build gates all pass.
- Audit 36567195391: unchanged synthetic gates, realistic P/R .929/.520, before/after repeatable archival replay, arrival sensitivity6/89 signals, production-volume query plans.
- Closed review findings: council executive ownership, retained-meeting revision sweep, existing civil-construction linking regression exposed by SQL CI.
- External-data and deployment boundaries remain in `docs/production-status.md`; completion of this implementation plan is not a claim that all empirical limitations disappeared.
