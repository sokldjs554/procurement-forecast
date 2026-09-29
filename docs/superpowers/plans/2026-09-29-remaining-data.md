# Remaining Data Limitations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or independent tasks under superpowers:dispatching-parallel-agents. Steps use checkbox (`- [ ]`) syntax for tracking.

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

- [ ] Write tests for 1:N, institution mismatch, unsuitable source/destination, idempotent review, stale writes, source rejection and access control; observe failure.
- [ ] Implement additive persistence, transactional review, non-destructive mixed-group audit and UI.
- [ ] Run related tests, schema generation, web checks and migration CI.

### Task 2: Budget classification

**Files:** `domain/taxonomy.py`, `llm/providers/heuristic.py`, relevant new tests and regression evaluation artifact.

**Interfaces:** keep `classify_category(text)` compatible; add explicit budget title/detail classifier if needed.

- [ ] Reproduce title-vs-account contamination and technical-word misclassification with independent assertions.
- [ ] Apply purpose-first rules and conservative ambiguity handling without modifying golden answers.
- [ ] Run unit suite, real-archive regression comparison and CI `manage eval all` before/after.

### Task 3: Coverage and revisions

**Files:** `sources/clik.py`, new `sources/coverage.py`, tests. Root integrates CLI separately.

**Interfaces:** read-only coverage report with per-institution configured/collected/raw-available counts; revision selection accepts previous revision IDs through adapter configuration or cursor.

- [ ] Test revised DOCID refetch and unchanged revision skip; keep document review protection.
- [ ] Implement coverage query and revision metadata without claiming uncollected national coverage.
- [ ] Run source tests and add operational-scale coverage benchmark.

### Task 4: Review dataset and replay diagnostics

**Files:** new `eval/review_dataset.py`, `pipeline/link_replay.py`, `cli.py`, tests, audit script and evaluation docs.

**Interfaces:** deterministic JSONL export with original hashes and document-based split; replay `order` mode with explicit retrospective scope.

- [ ] Test repeatability, missing source exclusion, document split isolation and order selection.
- [ ] Implement export and ordering diagnostics with no API calls or source mutations.
- [ ] Add `eval/forecast_snapshot.py` and meaningful unit/SQL tests: current observed state only, consistent source/link/prediction snapshot, atomic file and digest, explicit missing inputs, fail on truncation. Root integrates `manage eval freeze` CLI.
- [ ] Run real archive variants in separate databases and record differences without treating grouping agreement as accuracy.

### Task 5: Integrate and verify

- [ ] Run lint/type/unit/API/web/integration gates; benchmark added queries.
- [ ] Review branch, fix material findings, verify CI tree and merge authorized repository changes.
- [ ] Update README and status with measured results and external-data dependencies; report exact commit and remaining constraints.
