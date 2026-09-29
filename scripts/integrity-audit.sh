#!/usr/bin/env bash
# Isolated databases, fixture inputs and saved real signals only. No paid LLM/network ingestion.
set -euo pipefail
repo_dir="$(pwd)"
audit_dir="$RUNNER_TEMP/integrity-audit"
base_dir="$RUNNER_TEMP/integrity-baseline"
mkdir -p "$audit_dir"
git worktree add --detach "$base_dir" "$AUDIT_BASE_SHA"
for variant in baseline changed; do
  project_dir="$repo_dir"
  if [ "$variant" = baseline ]; then project_dir="$base_dir"; fi
  (
    cd "$project_dir/apps/api"
    uv sync --frozen
    createdb "audit_$variant"
    export APP_DATABASE_URL="postgresql+asyncpg://app:app@localhost:5432/audit_$variant"
    export APP_STORAGE_URL="file://$RUNNER_TEMP/audit-raw-$variant"
    uv run manage db upgrade
    uv run manage seed --anchor 2026-09-25
    uv run manage demo run
    uv run manage eval all --report "$audit_dir/$variant.md" > "$audit_dir/$variant.json"
    jq -e '.extraction.precision >= 0.95 and .extraction.recall >= 0.95 and
      .linking.pairwise.precision >= 0.95 and .linking.pairwise.recall >= 0.95 and
      .ocr.amount_token_accuracy_corrected >= 0.98 and .ocr.cer_corrected <= 0.03' "$audit_dir/$variant.json"
    cat "$audit_dir/$variant.json"
    createdb "replay_$variant"
    export APP_DATABASE_URL="postgresql+asyncpg://app:app@localhost:5432/replay_$variant"
    uv run manage db upgrade
    uv run manage seed --anchor 2026-09-25
    for attempt in 1 2; do
      uv run manage link replay "$repo_dir/docs/data/seongnam-link-signals.jsonl.gz" \
        --out "$audit_dir/$variant-groups-$attempt.json" > "$audit_dir/$variant-replay-$attempt.json"
    done
    cat "$audit_dir/$variant-replay-1.json"
    diff -u "$audit_dir/$variant-groups-1.json" "$audit_dir/$variant-groups-2.json"
  )
done
# The benchmark uses its own disposable app_bench database at the default production-like scale.
make bench
cp docs/performance.md "$audit_dir/performance.md"
cat docs/performance.md
cat "$audit_dir/baseline.md" "$audit_dir/changed.md" >> "$GITHUB_STEP_SUMMARY"
