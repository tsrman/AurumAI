#!/usr/bin/env bash
# Conversation-image archive acceptance on the standalone, disposable lifecycle
# project (scripts/fixtures/data-lifecycle-compose.yml, `-p phaseb-lifecycle-test`).
# The real data-worker image archives and restores an old conversation with a
# generated image, an edit of it and an upload, using its operator database login,
# the archive account and the image account limited to conversation-images/*.
# Independent checkers read rows and bytes before, after the purge and after the
# restore. The project keeps everything in tmpfs and is always torn down.
#
# Prerequisites: Docker, uv and a free 55434/59000/57233. Images are built from
# this checkout. Never point this at a shared or production stack.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT_DIR"
PROJECT=phaseb-lifecycle-test
JOURNAL=/tmp/phaseb-lifecycle-acceptance/images-fixture.json
export LIFECYCLE_REALM_FILE=/dev/null
LIFECYCLE=(docker compose -p "$PROJECT" --env-file /dev/null -f scripts/fixtures/data-lifecycle-compose.yml)
log() { printf '[archive-images] %s\n' "$*"; }

if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]]; then
  echo "A $PROJECT project already exists; finish or remove it first" >&2
  exit 1
fi
for port in 55434 59000 57233; do
  if ss -ltn | grep -q ":$port "; then echo "Port $port is in use" >&2; exit 1; fi
done
if [[ -e "$JOURNAL" ]]; then echo "An earlier image journal exists: $JOURNAL" >&2; exit 1; fi

teardown() {
  local status=$?
  log "removing the disposable project"
  "${LIFECYCLE[@]}" --profile archive-stage --profile rag-stage down -v --remove-orphans >/dev/null 2>&1 || true
  if (( status == 0 )); then rm -f "$JOURNAL"; else log "journal kept for inspection: $JOURNAL"; fi
  exit "$status"
}
trap teardown EXIT

worker() {
  # The data-worker service as deployed, minus its long-running entrypoint.
  "${LIFECYCLE[@]}" --profile archive-stage run --rm --no-deps -T --entrypoint python "$@"
}
check() { (cd business-backend && uv run --frozen python "../scripts/$1" "${@:2}"); }

log "building the backend and RAG images from this checkout"
docker tag aurum-ai-postgres:latest aurum-ai-postgres:phaseb
"${LIFECYCLE[@]}" --profile rag-stage build migrate rag-api >/dev/null
log "starting PostgreSQL, MinIO and Temporal; migrating; creating operator roles"
"${LIFECYCLE[@]}" --profile archive-stage up -d postgres minio temporal >/dev/null
"${LIFECYCLE[@]}" --profile archive-stage up -d lifecycle-roles minio-init >/dev/null
for job in migrate rag-migrate lifecycle-roles minio-init; do
  code=$(docker wait "$PROJECT-$job-1")
  if [[ "$code" != 0 ]]; then log "$job failed (exit $code)"; exit 1; fi
done

log "bucket IAM, including the image account"
check check-data-lifecycle-storage.py
log "preparing the old conversation with images"
check prepare-data-lifecycle-images.py
check check-data-lifecycle-images.py before
read -r ORG CONVERSATION < <(python3 -c "import json; s=json.load(open('$JOURNAL')); print(s['org_id'], s['conversation_id'])")

log "without image storage the conversation is not claimed"
blocked=$(worker -e APP_ARCHIVE_IMAGES_S3_ENDPOINT= data-worker -m src.cli.archive_data run \
  --org-id "$ORG" --conversation-id "$CONVERSATION" --apply)
grep -q '"blocked_reason": "image_archive_not_configured"' <<<"$blocked"

log "archiving with the data worker"
worker data-worker -m src.cli.archive_data run --org-id "$ORG" --conversation-id "$CONVERSATION" --dry-run \
  | grep -q '"eligible": true'
archived=$(worker data-worker -m src.cli.archive_data run --org-id "$ORG" --conversation-id "$CONVERSATION" --apply)
grep -q '"state": "purged"' <<<"$archived"
ARCHIVE=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['archive_id'])" "$archived")
check check-data-lifecycle-images.py archived

log "restoring with the data worker"
worker data-worker -m src.cli.archive_data restore --org-id "$ORG" --archive-id "$ARCHIVE" --dry-run \
  | grep -q '"restorable": true'
worker data-worker -m src.cli.archive_data restore --org-id "$ORG" --archive-id "$ARCHIVE" --apply \
  | grep -q '"state": "restored"'
check check-data-lifecycle-images.py restored
log "PASS"
