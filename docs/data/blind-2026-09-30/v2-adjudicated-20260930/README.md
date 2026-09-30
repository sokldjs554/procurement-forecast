# Independent real-document holdout, 2026-09-30 — adjudicated v2

This is a frozen, AI-authored extraction evaluation set assembled independently from six official documents belonging to three previously unused institutions: Seosan, Gangnam-gu, and Geumcheon-gu. It contains 33 contiguous excerpts: 17 with prospective procurement signals and 16 negatives. The signal list is intended to be exhaustive for each excerpt; it is not expert-reviewed gold.

The annotating agent did not read production extractor implementation, development gold, or prior evaluation results, and did not run extraction. Only the requested evaluation-schema file was inspected. All source research and downloads used free tools. Freeze hashes were recorded before scoring.

## Files

- `manifest.json`: schema `source-holdout-v1`, provenance, dates, source paths and SHA-256 hashes.
- `cases.jsonl`: frozen text excerpts and labels, with exact Unicode character and line offsets.
- `sources/*.html` / `sources/*.pdf`: complete actual HTTP response bodies obtained from the official URLs, not reconstructed or fabricated originals.
- `sources/*.txt`: complete parsed minutes roots or complete 545-page PDF text.
- `sources/*.retrieval.json`: retrieval timestamps and original-response hashes.
- `FREEZE.json` and `SHA256SUMS`: pre-scoring integrity record.
- `verification.json`: checks performed without importing or executing the extractor.
- `review-changes.json` and `review/source-review.json`: source-only pre-score review and exact versioned changes. Original acquisition/annotation scripts remain with v1.
- `qa/`: local PDF renderings used to verify table units and precise amounts; optional review evidence, not evaluator inputs.

## Source and date fidelity

HTML parsing uses the official minutes root (`div#content` for Seosan; `div#minutes` for the other councils), entity decoding, block boundaries, and whitespace normalization. The whole selected root is preserved, including headers, speaker provenance and attendance. No semantic corrections were made. Original HTML response bodies are archived separately. The PDF uses `pdftotext -layout` on the archived response, preserving tables and form feeds. Offsets index the archived text after UTF-8 decoding, not the original HTML/PDF or UTF-8 byte positions.

Every included original was obtained successfully. Initial candidate pages from Suncheon and Cheongju returned HTTP 502 and were excluded. Search-engine metadata was sometimes inconsistent with the current page header; actual archived headers determine identity and meeting dates. Online publication dates could not be verified and remain null. Meeting dates, fiscal years, and the budget report's cover date are explicitly separate. The set therefore cannot establish an exact historical publication-time discovery claim.

The `budget_book` source is an official council budget review report containing proposed allocations, not the municipality's original standalone budget book. Its title is preserved in the manifest. Four positive budget subsections were checked against rendered PDF pages 36, 60, 112 and 128 for units/amounts. Proposed amounts are not represented as contract award values or final paid costs.

## Annotation policy

An included signal must identify a future/proposed purchase, construction project, system, or outsourced study/service, supported by an executive response or official budget plan. A keyword, member request, ordinance, transfer/subsidy, old completed project or normal operating payment is insufficient. Recurring goods procurement can qualify when an executive explicitly describes purchases; an annual operating appropriation alone does not. Existing work in progress is background unless the excerpt establishes a new prospective procurement scope.

Each distinct named programme is one signal; components and sites inside an explicitly aggregated programme are not duplicated. Separately named acquisitions/studies within an excerpt are exhaustively listed. Predeclared title phrases come from the source text. `planned` covers executive intentions and budget proposals; `committed` requires stronger evidence such as secured funding plus a described implementation. The sampled documents do not cover all lifecycle states or every category, so no claim about full taxonomy or cancellation/reviewing performance is warranted.

Amounts use the exact project/annual scope described in `budget_scope`. A precise table value takes precedence over rounded prose. Larger department totals, hypothetical expansion costs and past expenditures are excluded. Null means no usable project-specific amount is stated in the excerpt. An omitted category/year is unscorable because its interpretation is ambiguous; a present year is supported by explicit relative wording or the document's clearly identified fiscal plan.

## Limits and reuse

This is purposive sampling for institution, document format, procurement semantics and negative-case diversity, not a random population sample. The 33 excerpts are not representative estimates of all Korean government documents. It measures extraction on selected parsed text; it does not measure OCR, source discovery coverage, tender linkage, actual future purchasing, or sales forecasts.

Labels require Korean procurement-domain expert review. Source ambiguities and programme-granularity choices remain potential annotation error. Do not tune against these labels or observed failures and continue describing the set as unseen. If labels need correction after scoring, retain this frozen version and publish the correction as a separately versioned adjudicated set.

## Version 2 pre-score adjudication

The original frozen files are unchanged and identified by SHA-256 in the manifest lineage. Before any scoring or exposure to extractor outputs, the annotator accepted one material ambiguity from an independent AI source review: a negative excerpt combined explicit household subsidies with a senior-centre installation programme whose contracting mechanism was unclear. Version 2 narrows that excerpt to the executive header and explicit household-support sentence. Its empty expected list is unchanged. Two category fields are omitted conservatively because no broad taxonomy convention was predeclared. Every other excerpt and substantive label remains unchanged. The independent review remains AI review, not human expert validation.
