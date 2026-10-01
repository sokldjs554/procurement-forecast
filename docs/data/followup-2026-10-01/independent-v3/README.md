# Independent historical procurement evaluation v3

Frozen source-grounded diagnostic set: **23 excerpts, 20 positive purchase signals in 15 positive cases, 8 difficult negative cases, 3 new authorities, 5 original documents**.

- Authorities: 광진구, 성북구, 양천구. All are Seoul districts; this is not geographic or statistical representativeness.
- Four budget-hearing minute records dated November–December 2023; one official 성북구도시관리공단 2024 annual workplan attached to the February 21, 2024 hearing.
- The cohort was fixed before full-document reading and excerpt selection. Broad historical budget/session searches were used; brief search snippets were visible before the cohort freeze. No extractor implementation or predictions were seen.
- Labels were authored by a separate evaluation agent and reread by that same agent. **Human/expert review: NONE.** This is independent from the implementation agent, not human adjudication or an independently sampled population.
- `committed` is the declared budget-backed intent convention, including executive budget proposals. It does not mean adopted appropriation, award, contract, or completed procurement.

## Files

`manifest.json` is the scoring entry point. `cases.jsonl` contains contiguous source excerpts, exact Unicode character start/end offsets, excerpt hashes, expected fields and case rationale. PDF cases also include original page numbers. `budget_book` is the loader's format code for the annual workplan; its actual document kind is recorded separately.

`cohort-selection.json` preserves the source-cohort selection timestamp and procedure. `annotation-review.json` records scope, field conventions, difficult decisions, remaining uncertainty and candidate exclusions made before any outputs. `fetch-log.json` and archived robots responses record retrieval provenance. HTML originals and the full workplan PDF are preserved together with complete normalized text. The workplan's date-provenance archive links to a byte-identical appendix.

`FROZEN_SHA256SUMS` freezes the manifest, cases, all sources and audit material. Verify from this directory:

```sh
sha256sum -c FROZEN_SHA256SUMS
```

All excerpts are exact slices of their source text. HTML extraction uses the source's `#canvas`, normalizes whitespace and retains paragraph/speaker boundaries. PDF extraction uses `pdftotext -layout`; form feeds remain as page separators. Pages 14, 30 and 43 were rendered and visually checked for the selected temporal heading, timetable and money rows.

## Limits and interpretation

This set tests purchase intent, temporal context, mixed prior/future projects, unit price versus total, replacement assets, multiple table rows, reductions/carryovers, maintenance, contingent budgets, councilmember suggestions and public-versus-private financing. Some category and scope decisions are explicitly judgmental. Two especially scope-sensitive negatives are the parking-lot solar suggestion and privately financed chargers. These decisions are fixed and disclosed before scoring.

The cases share authorities, documents and sometimes project context. All fiscal-year contexts are 2024. It is an excerpt-level purposive diagnostic set, not an end-to-end crawl benchmark, forecast-outcome backtest, nationwide validation or deployment gate. Report per-field denominators, case/entity counts and these limits. Do not relabel this version to fit outputs. If an error is discovered, preserve this version and create a separately named correction with a reason and fresh hashes.
