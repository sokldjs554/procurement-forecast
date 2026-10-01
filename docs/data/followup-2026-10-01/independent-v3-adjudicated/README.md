# Independent historical procurement evaluation v3 — pre-score adjudication

This sibling preserves the original frozen `../independent-v3/` and makes **one expected-field correction before any model output**: `iv3-yc-kiosk-reduction.expected[0].commitment`, `planned` → `committed`.

The executive says the next-year budget was reduced to buy and distribute about two education kiosks. Under the pre-existing convention, that is a concrete budget-backed executive commitment. Approximate quantity and absent total do not create a procurement condition. All source text, offsets, other expected fields and positive/negative decisions are unchanged.

An independent AI source reviewer flagged the inconsistency. The original evaluation annotator independently reread the source and convention and agreed. The final source-review report identified no other material errors or exclusions. **Human/expert review: NONE. No model predictions or extractor implementation inspected; no extraction/model run.** See `adjudication.json` for provenance.

Unchanged coverage: 23 cases; 15 positive cases containing20 purchase signals; 8 difficult negatives; 3 Seoul district authorities; 4 historical budget minute records and1 official annual workplan. All fiscal contexts are2024. It remains a clustered purposive diagnostic sample, not statistical, geographic, nationwide, outcome, or end-to-end validation. Original selection and limitations are retained in the manifest and original README.

The scoring entry point is `manifest.json`; full archives are referenced by relative paths into the preserved original folder. From this directory, verify the new version and all original archives:

```sh
sha256sum -c FROZEN_SHA256SUMS
```

Never overwrite either version to fit outputs. Any later correction requires another explicitly documented version.
