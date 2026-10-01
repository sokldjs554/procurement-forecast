# Frozen forecast follow-up

## Evidence available on 2026-09-30

Real long-term forecast accuracy is **not measured** by the available archive. The evaluator
below makes follow-up possible without reconstructing past predictions or treating missing
tenders as failed procurements.

| Available evidence | Verified contents | What it cannot establish |
| --- | --- | --- |
| `docs/data/seongnam-link-signals.jsonl.gz` | 2,404 real-source signals: 2,329 budget rows and 75 council mentions, representing 20 documents with exported signals | No tender/award outcomes, capture manifest, frozen probabilities/windows, or historical availability timestamps; not a prospective forecast cohort |
| The same signal archive's publication provenance | 2,098 `fiscal_year`, 231 `last_modified`, 75 council signals with unspecified publication provenance | Fiscal-year, meeting and modification dates do not establish when source evidence became public or when the model knew it |
| Existing local forecast snapshots from 2026-09-29/30 | 27 documents, 30 signals, 16 opportunities; sources include `fixture_minutes`, `fixture_budget`, `fixture_bid` | Synthetic workflow evidence, not real accuracy or 540 days of follow-up |
| Current `run_backtest` | Retrospective statistics over present-day link groups, with uncertain public dates excluded | Historical forecasts, historically frozen membership, exhaustive tender coverage, or prospectively validated calibration |

The signal archive's compressed-file SHA-256 is
`fc62e037e3b017dacad4bd94f2320e1c43043a8f682e4f0789fbba8a894fac1b`.
The source run covered more documents than the 20 represented by exported signals; neither
the 2,404 signals nor their stage distribution is an accuracy denominator.

The existing synthetic snapshot captured at `2026-09-30T04:05:52.790079+00:00` is a useful
smoke check only: 13 opportunities already contain a tender; 3 are eligible but right-censored
at a 540-day horizon. The evaluator reports `n=0`, `rate=null`, not a 0% conversion rate.

## First real-source freeze (2026-09-30)

[Run 36727719537](https://github.com/sokldjs554/procurement-forecast/actions/runs/36727719537)
processed two complete official Seosan transcripts in an empty PostgreSQL database with
zero-cost providers. It stored 10 signals and 9 opportunities, then froze the linked inputs
and predictions at **2026-09-30T14:15:51.046565Z**. The other source produced no stored signals;
it was not hidden or replaced. [Original snapshot and receipt](data/real-cohort-2026-09-30/README.md)
are preserved in the repository, beyond the temporary artifact retention period.

This starts observation of actual model outputs, **not verified accurate procurement forecasts**.
The sources were ingested late; some titles/scopes/budgets are wrong or ambiguous. Initial
pre-freeze tender screening is incomplete. All 9 rows are right-censored: `n=0`, `rate=null`,
540-day horizon ends 2028-03-23. All 9 original bid windows start before capture and are
excluded from window-accuracy scoring. Probabilities have no frozen calibration target.
No dates/windows were shifted to manufacture historical or prospective accuracy.

The first attempt ([36714070834](https://github.com/sokldjs554/procurement-forecast/actions/runs/36714070834))
failed with no stored signals because unmarked standalone speaker headings were not recognized.
That failure remains public; heuristic-v7 adds conservative speaker boundaries and was rerun.
The initial searched official planning records do not establish tender identity or exhaustive
coverage. Follow-up records must retain the original nine candidates and record ambiguity,
preexisting notices, failed access and coverage gaps without silently discarding failures.

## Commands

No database, provider key, network request, or paid model is used:

```bash
# From the repository root; inspect readiness without inventing observations.
python scripts/longitudinal-evaluate.py --snapshot /path/to/frozen.jsonl

# Once actual later observations and a coverage audit exist:
python scripts/longitudinal-evaluate.py \
  --snapshot /path/to/frozen.jsonl \
  --observations /path/to/observations.json \
  --horizon-days 540 > /path/to/followup-report.json
```

Use a distinct output path. The script reads inputs and writes JSON only to stdout. The API is
`app.eval.longitudinal.evaluate_longitudinal(snapshot_path, observations_path=None,
horizon_days=540, as_of=None)`. `as_of` is an optional `datetime.date` historical reporting cutoff;
the default is `app.clock.today_kst()`. Future reporting cutoffs are rejected. The evaluator
does not create snapshots and has no option to assert a historical capture time.

## Observation file contract

The following is a **synthetic schema example, not real observations**. Unit tests create their
own temporary synthetic fixtures. Do not copy the example's dates or outcomes into a real audit.

```json
{
  "version": "longitudinal-observations-v1",
  "snapshot_sha256": "SHA256_OF_THE_COMPLETE_ORIGINAL_SNAPSHOT_FILE",
  "data_origin": "synthetic",
  "observed_through": "2024-01-12",
  "coverage": [
    {
      "opportunity_id": 1,
      "institution_code": "LG-41130",
      "from_date": "2024-01-02",
      "through_date": "2024-01-12",
      "complete": true,
      "evidence_ref": "Synthetic complete tender census and matching audit",
      "recorded_at": "2024-01-12T12:00:00+00:00"
    }
  ],
  "outcomes": [
    {
      "opportunity_id": 1,
      "institution_code": "LG-41130",
      "bid_notice_id": "synthetic-bid-1",
      "kind": "bid_notice",
      "published_on": "2024-01-06",
      "publication_basis": "official_publication",
      "observed_at": "2024-01-07T00:00:00+00:00",
      "evidence_ref": "Synthetic archived notice and publication-date evidence",
      "match_basis": "Independent review of frozen project scope and notice identifiers"
    }
  ]
}
```

- `snapshot_sha256` covers the entire original file, including its manifest. The snapshot's
  own payload digest, entity counts, and manifest/configuration lineage must also match.
- `data_origin` must be `real` or `synthetic`. It is an explicit operator attestation, not a
  machine-verified determination. With no observation file, the report says `unverified`.
- `observed_through` is the last KST calendar date actually observed. Do not advance it merely
  because today's date advanced. All evidence recording timestamps must be timezone-aware,
  at or after capture, and no later than this cutoff or the actual application clock.
- Coverage requires one explicit interval per frozen opportunity. `complete=true` asserts an
  exhaustive source search **and** matching/adjudication of all potential notices across that
  interval, with the retained audit referenced in `evidence_ref`. A successful crawl or an
  institution-level API response alone does not establish complete project outcome coverage.
  Merge intervals only after auditing gaps; unknown/gapped coverage stays incomplete.
- `published_on` is the actual official tender-publication date, distinct from `observed_at`,
  when that evidence was recorded. Null, fiscal-year, inferred, meeting and modification dates
  cannot establish outcome truth. Preserve official evidence separately and reference it.
- Outcome matching uses the frozen opportunity ID and institution, not current link groups.
  Retain independent adjudication in `match_basis`; code does not independently verify that
  adjudication. Unknown IDs, mismatched institutions, duplicate/ambiguously assigned notice
  IDs, or unsupported outcome kinds fail closed. Split/merged projects require adjudication
  against the original frozen project scope, not silently replacing IDs.
- `kind` is `bid_notice` or `cancellation`. Cancellations are not positive tender outcomes.
  An original publication remains a publication even if subsequently cancelled. These metrics
  do not claim award, active tender status, spend, contract delivery, or commercial success.

## Denominators and temporal rules

Every frozen opportunity appears in the report; observations cannot select only the successes.
Already-published opportunities are excluded using both frozen summaries and accepted,
non-tentative bid/award memberships. Later-discovered notices published before or on the capture
date exclude that stale prediction. Same-day ordering is unproven with date-only publication.

The fixed horizon begins **after the KST capture date**, not at a historical meeting date or
signal `observed_at` value. A tender on capture date plus `horizon_days` is inside the horizon;
one later is not. A row enters the conversion denominator only after the whole horizon elapses,
`observed_through` reaches its end, and its explicit complete-coverage interval covers every day
from capture plus one day to the horizon end. Young successes and young unknowns are censored
equally. Known positives excluded for immature/incomplete follow-up are reported separately in
`observed_positives_excluded_from_rates`; they never justify a precision claim on selected cases.

`bid_window_hits` uses the original frozen window and the earliest tender. The entire window
must lie after freeze, have elapsed, and have complete outcome coverage from freeze through its
end. Expired, overlapping-capture, missing, immature, and incompletely covered windows do not
enter its denominator. The window denominator may differ from the fixed-horizon denominator.
There is no clipping, shifting, or recomputation of old windows to improve a result.

`lead_days_from_freeze` is conditional on observed conversions in the mature complete-horizon
cohort. It is not public-source lead time, average lead across all tenders, or retrieval recall.
Zero evaluable rows produce `rate=null` and `median=null`.

## Remaining empirical and provenance gaps

1. No real cohort with both genuinely earlier frozen predictions and sufficiently later
   adjudicated outcomes is currently available. Starting a cohort now cannot create elapsed
   follow-up. Retrospective source collection can support a separately labeled study, but cannot
   repair absent prospective model history.
2. A digest proves byte identity relative to the digest, not when those bytes existed. The v1
   capture timestamp and code revision are operator-attested, with no trusted timestamp or
   immutable external receipt. The exporter captures current state with late ingestion, and its
   capture clock precedes the consistent database read; it is not an exact database as-of proof.
3. Source coverage and outcome identity need independent audit. Missing notices may reflect
   collection, parsing, entity-resolution, or procurement-route gaps. National recall needs a
   separately sampled complete tender universe; this selected cohort cannot supply it.
4. Current `conversion_prob` does not freeze a probability target, fixed horizon, or original
   calibration lineage. The evaluator therefore reports probability calibration/Brier scoring
   **unavailable**, even if descriptive conversion rates become available. A future version must
   record those semantics at prediction time before probability accuracy can be measured.
5. Current snapshots omit original binary documents, model weights, and exact model requests.
   Reproducible scoring of frozen windows is not exact historical extraction/model replay.
6. Repeated freezes of the same projects are correlated. Do not pool reports as independent
   projects or select the best horizon after inspecting outcomes. Define target cohort, horizon,
   observation routes, project identity rules, audit responsibility and evaluation schedule before
   follow-up; preserve the original snapshot and independently recorded receipt.

Verification uses synthetic boundary tests for tampering, cutoff leakage, date provenance,
publication/observation separation, duplicate identity, cohort censoring and read-only CLI
behavior. Passing those tests establishes evaluator behavior, not real model accuracy.
