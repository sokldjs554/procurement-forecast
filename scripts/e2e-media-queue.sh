#!/usr/bin/env bash
# One council video through both languages on the Postgres job queue:
#   TypeScript API (API key, scopes) → TypeScript download worker (media.fetch)
#   → Python transcription worker (media.transcribe) → document → signals with video times.
#
# Needs: a migrated and seeded database (APP_DATABASE_URL), Redis, ffmpeg, tesseract + Korean
# fonts, uv, and a built orchestrator (pnpm build).
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
pg_url=${APP_DATABASE_URL/+asyncpg/}
port=${ORCH_PORT:-8787}
media_port=${MEDIA_PORT:-8899}
pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

cd "$root/apps/api"
uv run manage media synthetic "$work/src" > /dev/null
python3 -m http.server "$media_port" --bind 127.0.0.1 --directory "$work/src" > "$work/media.log" 2>&1 &
pids+=($!)

org=$(psql "$pg_url" -qAtc "INSERT INTO organizations (name) VALUES ('e2e media queue') RETURNING id")
key=$(uv run manage apikey create --org "$org" --name e2e \
  --scope jobs:write --scope jobs:read --scope usage:read)

cd "$root/apps/orchestrator"
DATABASE_URL="$pg_url" PORT="$port" ROLE=all \
  FETCH_ALLOWED_HOSTS=127.0.0.1 FETCH_ALLOW_INSECURE_HTTP=true \
  MEDIA_STORAGE_DIR="$work/store" STT_SPEC="fixture:$work/src/segments.json" \
  HEARTBEAT_SECONDS=1 POLL_SECONDS=1 \
  node dist/main.js > "$work/orchestrator.log" 2>&1 &
pids+=($!)
for _ in $(seq 1 30); do curl -fsS "localhost:$port/healthz" > /dev/null 2>&1 && break; sleep 1; done

api() { curl -fsS -H "authorization: Bearer $key" -H "content-type: application/json" "$@"; }

job=$(api -X POST "localhost:$port/v1/media-jobs" -d @- <<JSON | jq -r .id
{"source_url": "http://127.0.0.1:$media_port/meeting.mp4",
 "title": "제300회 도시건설위원회 제2차 회의", "meeting_date": "2026-03-18",
 "publisher": "경기도 성남시의회", "budget_usd": 1.0}
JSON
)
echo "job $job queued"

# The download stage hands the file to transcription as a child job.
for _ in $(seq 1 60); do
  state=$(api "localhost:$port/v1/jobs/$job" | jq -r '.stages[0].status // "none"')
  [ "$state" = queued ] && break
  sleep 1
done
[ "$state" = queued ] || { cat "$work/orchestrator.log"; echo "download stage did not finish"; exit 1; }

cd "$root/apps/api"
outcome=$(APP_MEDIA_WINDOW_SECONDS=10 APP_MEDIA_WINDOW_SEARCH_SECONDS=3 \
  uv run manage queue worker --once --heartbeat-seconds 1)
echo "python worker: $outcome"
[ "$outcome" = succeeded ]

tree=$(api "localhost:$port/v1/jobs/$job")
echo "$tree" | jq '{id, kind, status, stages: [.stages[] | {id, kind, status, attempts}]}'
echo "$tree" | jq -e '.status == "succeeded" and .stages[0].status == "succeeded"' > /dev/null
echo "$tree" | jq -e '
  [.stages[0].result.signals[] | select(.t0 != null and .t0 >= 12 and .t1 <= 22)] | length >= 1
' > /dev/null
echo "$tree" | jq '.stages[0].result.signals[] | {title, verdict, budget_krw, t0, t1}'

usage=$(api "localhost:$port/v1/usage")
echo "$usage" | jq .
echo "$usage" | jq -e '.meters[] | select(.meter == "stt_audio_seconds") | .quantity >= 35' > /dev/null
echo "OK: API → download (TypeScript) → transcription (Python) → signals with video seconds"
