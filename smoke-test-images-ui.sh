#!/usr/bin/env bash
# Image acceptance against the deterministic gateway: Admin UI picks the
# organization image models and marks the chat model as a vision model; a
# member generates and edits images, attaches a picture the model must receive
# inline, edits that upload, and everything survives a reload. The test restores
# the organization capabilities, model grants and image settings it changes; this
# wrapper restores the ordinary backend configuration on exit.
#
# Prerequisites: a built Compose stack (`docker compose up -d`) and the current
# APP_SECRET_KEY exported. Images are rebuilt from this checkout.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT_DIR"
log() { printf '[images-ui-e2e] %s\n' "$*"; }

if [[ -z "${APP_SECRET_KEY:-}" ]]; then
  log "ERROR: APP_SECRET_KEY must match the Compose stack" >&2
  exit 1
fi
for required_command in docker curl npm; do
  command -v "$required_command" >/dev/null 2>&1 || {
    log "ERROR: required command is missing: $required_command" >&2
    exit 1
  }
done

E2E=(docker compose -f docker-compose.yml -f docker-compose.e2e.yml)
export IMAGES_ENABLED=true TEST_GATEWAY_IMAGE_MODEL_ID=test-image

restore() {
  local status=$?
  log "restoring the ordinary backend configuration"
  "${E2E[@]}" rm -sf test-gateway >/dev/null 2>&1 || true
  IMAGES_ENABLED=false docker compose -f docker-compose.yml up -d --no-build \
    --force-recreate backend backend-admin >/dev/null 2>&1 || true
  exit "$status"
}
trap restore EXIT

wait_for() {
  local name="$1" url="$2"
  for _ in $(seq 1 90); do
    if curl -fsS -o /dev/null "$url"; then log "$name is ready"; return 0; fi
    sleep 2
  done
  log "ERROR: $name did not become ready" >&2
  return 1
}

log "building runner, backend and both UIs from this checkout"
"${E2E[@]}" --profile build build runner
"${E2E[@]}" build backend backend-admin test-gateway frontend admin-ui
# Unbound pool runners outlive a backend restart and would keep serving the
# previous runner image; only unbound ones are removed, bound conversations
# keep their runner.
mapfile -t stale_pool < <(docker ps -aq --filter "name=^runner-pool-")
if (( ${#stale_pool[@]} )); then
  log "removing ${#stale_pool[@]} unbound pool runner(s) built from an older image"
  docker rm -f "${stale_pool[@]}" >/dev/null
fi
log "starting the deterministic gateway with image tools enabled"
"${E2E[@]}" up -d --no-build --force-recreate test-gateway backend backend-admin frontend admin-ui

wait_for "business backend" "http://localhost:8000/health"
wait_for "admin backend" "http://localhost:8001/health"
wait_for "Business UI" "http://localhost:3000/login"
wait_for "Admin UI" "http://localhost:3100/login"

log "running the browser acceptance"
(
  cd business-ui
  E2E_IMAGES_UI=true npx playwright test e2e/images.real.spec.ts --workers=1 --trace=off
)
log "PASS"
