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
    if [ "$variant" = changed ]; then
      uv run manage sources coverage --no-verify-raw --out "$audit_dir/coverage.json" > /dev/null
      uv run manage eval export-reviews --out "$audit_dir/reviews.jsonl" > "$audit_dir/reviews-report.json"
      uv run manage eval freeze --institution-code LG-41130 --code-revision "$(git rev-parse HEAD)" \
        --out "$audit_dir/forecast-snapshot.jsonl" > "$audit_dir/forecast-snapshot-report.json"
      # Exercise the shipped CLI against a disposable populated DB, never production.
      uv run manage pipeline revalidate --today 2026-09-29 \
        --out "$audit_dir/revalidation-dry-run.json" > /dev/null
      digest=$(jq -r .digest "$audit_dir/revalidation-dry-run.json")
      uv run manage pipeline revalidate --today 2026-09-29 --apply --expected-digest "$digest" \
        --out "$audit_dir/revalidation-apply.json" > /dev/null
      jq -e '.applied == true' "$audit_dir/revalidation-apply.json"
    fi
    for attempt in 1 2; do
      # replay rolls back its inserts, but rollback does not remove heap/index bloat or
      # reset planner statistics. Use independently initialized databases so the second
      # reproducibility run cannot inherit the first run's physical storage state.
      replay_db="replay_${variant}_${attempt}"
      createdb "$replay_db"
      export APP_DATABASE_URL="postgresql+asyncpg://app:app@localhost:5432/$replay_db"
      uv run manage db upgrade
      uv run manage seed --anchor 2026-09-25
      attempt_started=$SECONDS
      echo "Starting $variant replay $attempt in $replay_db"
      uv run manage link replay "$repo_dir/docs/data/seongnam-link-signals.jsonl.gz" \
        --out "$audit_dir/$variant-groups-$attempt.json" > "$audit_dir/$variant-replay-$attempt.json"
      echo "Completed $variant replay $attempt in $((SECONDS - attempt_started)) seconds"
    done
    cat "$audit_dir/$variant-replay-1.json"
    diff -u "$audit_dir/$variant-groups-1.json" "$audit_dir/$variant-groups-2.json"
    if [ "$variant" = changed ]; then
      for arrival in budget_first reverse; do
        replay_db="replay_arrival_$arrival"
        createdb "$replay_db"
        export APP_DATABASE_URL="postgresql+asyncpg://app:app@localhost:5432/$replay_db"
        uv run manage db upgrade
        uv run manage seed --anchor 2026-09-25
        arrival_args=(--order reverse)
        if [ "$arrival" = budget_first ]; then arrival_args=(--first budget_book); fi
        echo "Starting changed arrival audit: $arrival"
        uv run manage link replay "$repo_dir/docs/data/seongnam-link-signals.jsonl.gz" \
          "${arrival_args[@]}" --today 2026-09-29 \
          --out "$audit_dir/changed-groups-$arrival.json" > "$audit_dir/changed-replay-$arrival.json"
        cat "$audit_dir/changed-replay-$arrival.json"
      done
      uv run python - "$audit_dir" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
base = json.loads((root / 'changed-groups-1.json').read_text())
def memberships(groups):
    return {key: frozenset(group) for group in groups for key in group}
baseline = memberships(base)
report = {'scope': 'arrival_order_sensitivity', 'accuracy_evaluated': False, 'variants': {}}
for variant in ('budget_first', 'reverse'):
    groups = json.loads((root / f'changed-groups-{variant}.json').read_text())
    current = memberships(groups)
    report['variants'][variant] = {
        'groups': len(groups),
        'compared_signals': len(set(baseline) | set(current)),
        'changed_membership_signals': sum(baseline.get(k) != current.get(k) for k in set(baseline) | set(current)),
        'identical_partition': baseline == current,
    }
(root / 'arrival-sensitivity.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
assert all(v['identical_partition'] for v in report['variants'].values()), report
PY
    fi
  )
done
# The benchmark uses its own disposable app_bench database at the default production-like scale.
make bench
cp docs/performance.md "$audit_dir/performance.md"
cat docs/performance.md
cat "$audit_dir/baseline.md" "$audit_dir/changed.md" >> "$GITHUB_STEP_SUMMARY"
