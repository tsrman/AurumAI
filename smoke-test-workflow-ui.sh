#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
E2E_BUSINESS_UI_URL="${E2E_BUSINESS_UI_URL:-http://localhost:3000}"
E2E_ADMIN_UI_URL="${E2E_ADMIN_UI_URL:-http://localhost:3100}"
E2E_BUSINESS_BACKEND_URL="${E2E_BUSINESS_BACKEND_URL:-http://localhost:8000}"
E2E_ADMIN_BACKEND_URL="${E2E_ADMIN_BACKEND_URL:-http://localhost:8001}"
E2E_KEYCLOAK_URL="${E2E_KEYCLOAK_URL:-http://localhost:8080}"
WORKFLOW_UI_E2E_RATE_LIMIT_PER_MINUTE="${WORKFLOW_UI_E2E_RATE_LIMIT_PER_MINUTE:-2000}"
WORKFLOW_UI_E2E_RATE_LIMIT_PER_MINUTE_STREAM="${WORKFLOW_UI_E2E_RATE_LIMIT_PER_MINUTE_STREAM:-500}"
TEST_GATEWAY_BARRIER_SECONDS="${TEST_GATEWAY_BARRIER_SECONDS:-8}"
E2E_WORKFLOW_UI_MARKER="${E2E_WORKFLOW_UI_MARKER:-workflow-ui-e2e-$(openssl rand -hex 6)}"
COMPOSE_PROJECT_NAME="${WORKFLOW_UI_E2E_COMPOSE_PROJECT:-}"
TEMP_DIR=""
E2E_WORKFLOW_UI_STATE=""

if [[ -z "${APP_SECRET_KEY:-}" ]]; then
  printf '[workflow-ui-e2e] ERROR: APP_SECRET_KEY must match the Compose stack\n' >&2
  exit 1
fi
if [[ ! "$E2E_WORKFLOW_UI_MARKER" =~ ^workflow-ui-e2e-[a-f0-9]{10,32}$ ]]; then
  printf '[workflow-ui-e2e] ERROR: invalid ownership marker\n' >&2
  exit 1
fi
if [[ -z "$COMPOSE_PROJECT_NAME" ]]; then
  COMPOSE_PROJECT_NAME=$(docker inspect aurum-ai-postgres \
    --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)
  COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-aurum-ai}"
fi

TEMP_DIR=$(mktemp -d)
E2E_WORKFLOW_UI_STATE="$TEMP_DIR/state.json"
export E2E_WORKFLOW_UI=true
export E2E_WORKFLOW_UI_STATE E2E_WORKFLOW_UI_MARKER
export E2E_BUSINESS_UI_URL E2E_ADMIN_UI_URL
export E2E_BUSINESS_BACKEND_URL E2E_ADMIN_BACKEND_URL E2E_KEYCLOAK_URL
export TEST_GATEWAY_BARRIER_SECONDS

wait_for_url() {
  local url="$1" name="$2"
  local attempt
  for attempt in $(seq 1 120); do
    if curl -sf "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf '[workflow-ui-e2e] ERROR: %s did not become ready\n' "$name" >&2
  return 1
}

cleanup() {
  local exit_code=$?
  local cleanup_code=0
  local command_code
  trap - EXIT INT TERM
  set +e

  if [[ -n "$E2E_WORKFLOW_UI_STATE" ]]; then
    python3 "$ROOT_DIR/scripts/workflow_ui_e2e.py" cleanup \
      --state "$E2E_WORKFLOW_UI_STATE" --marker "$E2E_WORKFLOW_UI_MARKER"
    command_code=$?
    if ((command_code != 0 && cleanup_code == 0)); then cleanup_code=$command_code; fi
  fi
  docker compose -p "$COMPOSE_PROJECT_NAME" \
    -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" \
    rm -sf test-gateway
  command_code=$?
  if ((command_code != 0 && cleanup_code == 0)); then cleanup_code=$command_code; fi
  docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
    up -d --no-deps --force-recreate backend backend-admin
  command_code=$?
  if ((command_code != 0 && cleanup_code == 0)); then cleanup_code=$command_code; fi
  if ((cleanup_code == 0)) && [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf -- "$TEMP_DIR"
  elif [[ -n "$E2E_WORKFLOW_UI_STATE" ]]; then
    printf '[workflow-ui-e2e] ERROR: recovery state retained at %s\n' \
      "$E2E_WORKFLOW_UI_STATE" >&2
    printf '[workflow-ui-e2e] Retry cleanup: python3 %s cleanup --state %s --marker %s\n' \
      "$ROOT_DIR/scripts/workflow_ui_e2e.py" "$E2E_WORKFLOW_UI_STATE" \
      "$E2E_WORKFLOW_UI_MARKER" >&2
    printf '[workflow-ui-e2e] Retry services: docker compose -p %s -f %s up -d --no-deps --force-recreate backend backend-admin\n' \
      "$COMPOSE_PROJECT_NAME" "$ROOT_DIR/docker-compose.yml" >&2
  fi

  if ((exit_code != 0)); then
    exit "$exit_code"
  fi
  exit "$cleanup_code"
}
trap cleanup EXIT INT TERM

docker compose -p "$COMPOSE_PROJECT_NAME" \
  -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" \
  --profile build build runner
docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
  up -d --no-recreate postgres redis keycloak temporal minio
APP_RATE_LIMIT_PER_MINUTE="$WORKFLOW_UI_E2E_RATE_LIMIT_PER_MINUTE" \
APP_RATE_LIMIT_PER_MINUTE_STREAM="$WORKFLOW_UI_E2E_RATE_LIMIT_PER_MINUTE_STREAM" \
RUNNER_POOL_SIZE=0 docker compose -p "$COMPOSE_PROJECT_NAME" \
  -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" \
  up -d --build --no-deps --force-recreate backend backend-admin test-gateway
docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
  up -d --build --no-deps frontend admin-ui

wait_for_url "$E2E_KEYCLOAK_URL/realms/aurum-ai" "Keycloak"
wait_for_url "$E2E_BUSINESS_BACKEND_URL/ready" "backend"
wait_for_url "$E2E_ADMIN_BACKEND_URL/ready" "backend-admin"
wait_for_url "$E2E_BUSINESS_UI_URL/login" "Business UI"
wait_for_url "$E2E_ADMIN_UI_URL/login" "Admin UI"

(
  cd "$ROOT_DIR/business-ui"
  npm exec -- playwright test e2e/workflow.real.spec.ts \
    --project=chromium --workers=1 --trace=off
)
