#!/usr/bin/env bash
# Hermes Agent in the browser against the deterministic gateway: an
# administrator picks the organization's Hermes model in Admin UI; a member
# chooses Hermes in the composer, sees its tool cards (also after a reload),
# answers its question card, finds its memory in their settings and clears it,
# and finds a skill it wrote waiting for review; the administrator reviews and
# approves the draft, and the member's next turn has it in the sandbox. The test
# restores the organization capabilities, model grant and Hermes settings it
# changes and removes the skill it approved; this wrapper removes the Hermes
# sandboxes and services and restores the ordinary configuration on exit.
#
# With HERMES_E2E_REAL_LLM=true it runs the real-model scenario instead
# (e2e/hermes.real-llm.spec.ts) through the stack's own gateway
# (LLM_GATEWAY_URL), on E2E_HERMES_MODEL (default deepseek-flash-free), which
# needs native tool calling; E2E_HERMES_VISION=true, for a model that takes
# images, also has Hermes look at a picture.
#
# Prerequisites: a built Compose stack (`docker compose up -d`), the current
# APP_SECRET_KEY exported, Docker 28+, and no other aurum-ai-hermes-egress-guard
# container (./smoke-test-hermes-sandbox.sh must not run at the same time).
# Images are rebuilt from this checkout.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT_DIR"
log() { printf '[hermes-ui-e2e] %s\n' "$*"; }

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

REAL_LLM="${HERMES_E2E_REAL_LLM:-false}"
if [[ "$REAL_LLM" == true ]]; then
  # The stack's own gateway; no deterministic overlay.
  E2E=(docker compose -f docker-compose.yml -f docker-compose.hermes.yml --profile hermes --profile hermes-preview --profile skills-exec)
  GATEWAY=()
else
  E2E=(docker compose -f docker-compose.yml -f docker-compose.e2e.yml -f docker-compose.hermes.yml --profile hermes --profile hermes-preview --profile skills-exec)
  GATEWAY=(test-gateway)
fi
# Images on: the member's pictures reach a model marked as taking them.
export HERMES_ENABLED=true USER_QUESTIONS_ENABLED=true HERMES_EGRESS_MAX_MODE=packages IMAGES_ENABLED=true
# Shared by the backend and hermes-egress for this run only.
HERMES_EGRESS_SERVICE_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export HERMES_EGRESS_SERVICE_SECRET
# Previews of the apps Hermes runs, at *.preview.localhost, which browsers
# resolve to this machine; the secrets live for this run only.
export HERMES_PREVIEW_ENABLED=true HERMES_PREVIEW_ORIGIN='http://{preview}.preview.localhost:8095'
HERMES_PREVIEW_SERVICE_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
HERMES_PREVIEW_EXECUTOR_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export HERMES_PREVIEW_SERVICE_SECRET HERMES_PREVIEW_EXECUTOR_SECRET

remove_sandboxes() {
  for name in $(docker ps -aq --filter label=aurum-ai.hermes-sandbox=1); do docker rm -f "$name" >/dev/null 2>&1 || true; done
  for name in $(docker network ls -q --filter label=aurum-ai.hermes-sandbox=1); do docker network rm "$name" >/dev/null 2>&1 || true; done
  for name in $(docker volume ls -q --filter label=aurum-ai.hermes-sandbox=1); do docker volume rm "$name" >/dev/null 2>&1 || true; done
}

restore() {
  local status=$?
  if (( status != 0 )); then
    # Before the sandboxes go: what Hermes and the session workflow last said.
    for name in $(docker ps -aq --filter label=aurum-ai.hermes-sandbox=1); do
      docker logs "$name" 2>&1 | grep -E "ERROR|WARNING|Final error" | tail -20 >&2 || true
    done
    docker logs aurum-ai-tool-worker 2>&1 | grep -iE "hermes" | tail -20 >&2 || true
  fi
  log "removing the Hermes sandboxes and services, restoring the ordinary configuration"
  remove_sandboxes
  "${E2E[@]}" rm -sf "${GATEWAY[@]}" hermes-egress hermes-egress-guard hermes-preview >/dev/null 2>&1 || true
  HERMES_ENABLED=false HERMES_PREVIEW_ENABLED=false USER_QUESTIONS_ENABLED="${ORIGINAL_USER_QUESTIONS_ENABLED:-false}" \
    IMAGES_ENABLED="${ORIGINAL_IMAGES_ENABLED:-false}" \
    docker compose -f docker-compose.yml --profile skills-exec up -d --no-build --force-recreate \
    backend backend-admin tool-worker skill-executor >/dev/null 2>&1 || true
  exit "$status"
}
ORIGINAL_USER_QUESTIONS_ENABLED=$(grep -s '^USER_QUESTIONS_ENABLED=' .env | cut -d= -f2- || true)
ORIGINAL_IMAGES_ENABLED=$(grep -s '^IMAGES_ENABLED=' .env | cut -d= -f2- || true)
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

log "building the runner, the Hermes sandbox, the services and both UIs from this checkout"
"${E2E[@]}" --profile build build runner hermes-sandbox
"${E2E[@]}" build backend backend-admin "${GATEWAY[@]}" frontend admin-ui tool-worker skill-executor hermes-egress-guard hermes-preview
# Unbound pool runners outlive a backend restart and would keep serving the
# previous runner image; only unbound ones are removed, bound conversations
# keep their runner.
mapfile -t stale_pool < <(docker ps -aq --filter "name=^runner-pool-")
if (( ${#stale_pool[@]} )); then
  log "removing ${#stale_pool[@]} unbound pool runner(s) built from an older image"
  docker rm -f "${stale_pool[@]}" >/dev/null
fi
remove_sandboxes
log "starting the services with Hermes enabled (real model: $REAL_LLM)"
"${E2E[@]}" up -d --no-build --force-recreate \
  "${GATEWAY[@]}" backend backend-admin frontend admin-ui tool-worker skill-executor hermes-egress-guard hermes-egress hermes-preview

wait_for "business backend" "http://localhost:8000/health"
wait_for "admin backend" "http://localhost:8001/health"
wait_for "Business UI" "http://localhost:3000/login"
wait_for "Admin UI" "http://localhost:3100/login"
wait_for "preview service" "http://localhost:8095/health"

log "running the browser acceptance"
(
  cd business-ui
  if [[ "$REAL_LLM" == true ]]; then
    E2E_USE_REAL_LLM=true E2E_HERMES_MODEL="${E2E_HERMES_MODEL:-deepseek-flash-free}" \
      E2E_HERMES_VISION="${E2E_HERMES_VISION:-false}" \
      npx playwright test e2e/hermes.real-llm.spec.ts --workers=1 --trace=off
  else
    E2E_HERMES_UI=true npx playwright test e2e/hermes.real.spec.ts --workers=1 --trace=off
  fi
)
log "PASS"
