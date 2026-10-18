#!/usr/bin/env bash
set -euo pipefail

# Compose-only real browser acceptance for the authorized Knowledge/RAG lifecycle.
# It changes an organization-wide capability and therefore must run exclusively
# against the built-in development organization.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
BUSINESS_URL="${BASE_URL:-http://localhost:8000}"
ADMIN_URL="${ADMIN_BASE_URL:-http://localhost:8001}"
BUSINESS_UI_URL="${PLAYWRIGHT_BASE_URL:-http://localhost:3000}"
ADMIN_UI_URL="${ADMIN_UI_URL:-http://localhost:3100}"
KEYCLOAK_PUBLIC_URL="${KEYCLOAK_URL:-http://localhost:8080}"
: "${APP_SECRET_KEY:?APP_SECRET_KEY must be set before Knowledge UI E2E}"
export APP_SECRET_KEY

WORK_DIR=""
STATE_PATH=""
MARKER=""

log() { printf '[knowledge-ui-smoke] %s\n' "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is missing: $1"
}

wait_for_url() {
  local url="$1" name="$2" attempts="${3:-120}"
  local i
  for i in $(seq 1 "$attempts"); do
    if curl -sf "$url" >/dev/null 2>&1; then
      log "$name is ready"
      return 0
    fi
    sleep 1
  done
  fail "$name did not become ready"
}

run_fixture() {
  local command="$1"
  shift
  python3 "$ROOT_DIR/scripts/knowledge_ui_e2e.py" "$command" \
    --state "$STATE_PATH" --marker "$MARKER" "$@"
}

on_exit() {
  local primary_status=$? cleanup_status=0 verify_status=0 final_status
  trap - EXIT INT TERM
  set +e
  if [[ -n "$STATE_PATH" && -s "$STATE_PATH" ]]; then
    python3 "$ROOT_DIR/scripts/knowledge_ui_e2e.py" cleanup \
      --state "$STATE_PATH" --marker "$MARKER"
    cleanup_status=$?
    if [[ "$cleanup_status" == "0" ]]; then
      python3 "$ROOT_DIR/scripts/knowledge_ui_e2e.py" verify-clean \
        --state "$STATE_PATH" --marker "$MARKER"
      verify_status=$?
    fi
  fi
  if [[ "$cleanup_status" == "0" && "$verify_status" == "0" ]]; then
    [[ -n "$WORK_DIR" && "$WORK_DIR" == /tmp/knowledge-ui-e2e.* ]] \
      && rm -rf -- "$WORK_DIR"
  else
    log "cleanup verification failed; journal retained at $STATE_PATH"
    log "retry: python3 scripts/knowledge_ui_e2e.py cleanup --state $STATE_PATH --marker $MARKER"
  fi
  final_status="$primary_status"
  if [[ "$final_status" == "0" ]]; then
    if [[ "$cleanup_status" != "0" ]]; then
      final_status="$cleanup_status"
    else
      final_status="$verify_status"
    fi
  fi
  exit "$final_status"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

require_command curl
require_command docker
require_command npm
require_command python3

# Reuse the project that owns the fixed-name development containers. Its
# static runner/RAG subnets cannot coexist under a second Compose project.
existing_project="$(
  docker inspect \
    --format '{{ index .Config.Labels "com.docker.compose.project" }}' \
    aurum-ai-backend 2>/dev/null || true
)"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$existing_project}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-aurum-ai}"
log "using Compose project $COMPOSE_PROJECT_NAME"

WORK_DIR="$(mktemp -d /tmp/knowledge-ui-e2e.XXXXXX)"
chmod 700 "$WORK_DIR"
STATE_PATH="$WORK_DIR/state.json"
MARKER="knowledge-ui-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"

log "starting real Knowledge/RAG Compose services"
# Build only application images: `up --build` would also rebuild and recreate
# postgres under the running Keycloak/Temporal connection pools.
docker compose -p "$COMPOSE_PROJECT_NAME" \
  -f "$ROOT_DIR/docker-compose.yml" --profile rag \
  build rag-api rag-worker backend backend-admin frontend admin-ui
docker compose -p "$COMPOSE_PROJECT_NAME" \
  -f "$ROOT_DIR/docker-compose.yml" --profile rag \
  up -d --no-build \
  postgres redis keycloak temporal minio etcd milvus reranker \
  rag-api rag-worker backend backend-admin frontend admin-ui

wait_for_url "$KEYCLOAK_PUBLIC_URL/realms/aurum-ai" "Keycloak"
wait_for_url "$BUSINESS_URL/health" "business backend"
wait_for_url "$ADMIN_URL/health" "admin backend"
wait_for_url "$BUSINESS_UI_URL/login" "Business UI"
wait_for_url "$ADMIN_UI_URL/login" "Admin UI"

rag_ready=0
for _ in $(seq 1 120); do
  if [[ "$(
    docker inspect --format '{{.State.Health.Status}}' \
      aurum-ai-rag-api 2>/dev/null || true
  )" == "healthy" ]]; then
    rag_ready=1
    break
  fi
  sleep 1
done
[[ "$rag_ready" == "1" ]] || fail "RAG API did not become ready"

reranker_ready=0
for _ in $(seq 1 "${RERANKER_READY_ATTEMPTS:-900}"); do
  if docker exec aurum-ai-rag-api python -c \
    "import urllib.request; urllib.request.urlopen('http://reranker:7997/models', timeout=5).close()" \
    >/dev/null 2>&1; then
    reranker_ready=1
    break
  fi
  sleep 1
done
[[ "$reranker_ready" == "1" ]] || fail "reranker did not become ready"
log "reranker is ready"

run_fixture prepare

export BASE_URL="$BUSINESS_URL"
export ADMIN_BASE_URL="$ADMIN_URL"
export KEYCLOAK_URL="$KEYCLOAK_PUBLIC_URL"
export PLAYWRIGHT_BASE_URL="$BUSINESS_UI_URL"
export ADMIN_UI_URL
export E2E_KNOWLEDGE_UI=true
export E2E_KNOWLEDGE_UI_STATE="$STATE_PATH"
export E2E_KNOWLEDGE_UI_MARKER="$MARKER"

log "running one-worker browser lifecycle"
cd "$ROOT_DIR/business-ui"
npm exec -- playwright test e2e/knowledge.real.spec.ts \
  --project=chromium --workers=1 --trace=off
