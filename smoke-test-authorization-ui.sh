#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
E2E_BUSINESS_UI_URL="${E2E_BUSINESS_UI_URL:-http://localhost:3000}"
E2E_ADMIN_UI_URL="${E2E_ADMIN_UI_URL:-http://localhost:3100}"
E2E_BUSINESS_BACKEND_URL="${E2E_BUSINESS_BACKEND_URL:-http://localhost:8000}"
E2E_ADMIN_BACKEND_URL="${E2E_ADMIN_BACKEND_URL:-http://localhost:8001}"
E2E_KEYCLOAK_URL="${E2E_KEYCLOAK_URL:-http://localhost:8080}"
AUTHORIZATION_UI_E2E_RATE_LIMIT_PER_MINUTE="${AUTHORIZATION_UI_E2E_RATE_LIMIT_PER_MINUTE:-3000}"
AUTHORIZATION_UI_E2E_RATE_LIMIT_PER_MINUTE_STREAM="${AUTHORIZATION_UI_E2E_RATE_LIMIT_PER_MINUTE_STREAM:-500}"
TEST_GATEWAY_BARRIER_SECONDS="${TEST_GATEWAY_BARRIER_SECONDS:-8}"
MARKER="authorization-ui-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
COMPOSE_PROJECT_NAME=""
WORK_DIR=""
STATE_PATH=""

for required_command in docker curl npm python3; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    printf '[authorization-ui-e2e] ERROR: required command is missing: %s\n' \
      "$required_command" >&2
    exit 1
  fi
done
if [[ -z "${APP_SECRET_KEY:-}" ]]; then
  printf '[authorization-ui-e2e] ERROR: APP_SECRET_KEY must match the Compose stack\n' >&2
  exit 1
fi
if [[ ! "$MARKER" =~ ^authorization-ui-e2e-[a-f0-9]{10,32}$ ]]; then
  printf '[authorization-ui-e2e] ERROR: invalid ownership marker\n' >&2
  exit 1
fi

COMPOSE_PROJECT_NAME=$(docker inspect aurum-ai-backend \
  --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true)
if [[ -z "$COMPOSE_PROJECT_NAME" ]]; then
  printf '[authorization-ui-e2e] ERROR: aurum-ai-backend has no Compose project label\n' >&2
  exit 1
fi

verify_compose_service() {
  local container="$1" expected_service="$2"
  local actual_project actual_service
  actual_project=$(docker inspect "$container" \
    --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true)
  actual_service=$(docker inspect "$container" \
    --format '{{ index .Config.Labels "com.docker.compose.service" }}' 2>/dev/null || true)
  if [[ "$actual_project" != "$COMPOSE_PROJECT_NAME" || "$actual_service" != "$expected_service" ]]; then
    printf '[authorization-ui-e2e] ERROR: ownership mismatch for %s\n' "$container" >&2
    exit 1
  fi
}

verify_compose_service aurum-ai-backend backend
verify_compose_service aurum-ai-backend-admin backend-admin
verify_compose_service aurum-ai-frontend frontend
verify_compose_service aurum-ai-admin-ui admin-ui
verify_compose_service aurum-ai-postgres postgres
verify_compose_service aurum-ai-keycloak keycloak

WORK_DIR=$(mktemp -d /tmp/authorization-ui-e2e.XXXXXXXX)
chmod 700 "$WORK_DIR"
STATE_PATH="$WORK_DIR/state.json"

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
  printf '[authorization-ui-e2e] ERROR: %s did not become ready\n' "$name" >&2
  return 1
}

cleanup() {
  local primary_code=$?
  local cleanup_code=0
  local command_code
  trap - EXIT INT TERM
  set +e

  if [[ -n "$STATE_PATH" && -f "$STATE_PATH" ]]; then
    python3 "$ROOT_DIR/scripts/authorization_ui_e2e.py" cleanup \
      --state "$STATE_PATH" --marker "$MARKER"
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

  wait_for_url "$E2E_BUSINESS_BACKEND_URL/ready" "ordinary backend"
  command_code=$?
  if ((command_code != 0 && cleanup_code == 0)); then cleanup_code=$command_code; fi
  wait_for_url "$E2E_ADMIN_BACKEND_URL/ready" "ordinary backend-admin"
  command_code=$?
  if ((command_code != 0 && cleanup_code == 0)); then cleanup_code=$command_code; fi

  if [[ -n "$STATE_PATH" && -f "$STATE_PATH" ]]; then
    python3 "$ROOT_DIR/scripts/authorization_ui_e2e.py" verify-clean \
      --state "$STATE_PATH" --marker "$MARKER"
    command_code=$?
    if ((command_code != 0 && cleanup_code == 0)); then cleanup_code=$command_code; fi
  fi

  if ((cleanup_code == 0)) && [[ "$WORK_DIR" == /tmp/authorization-ui-e2e.* && -d "$WORK_DIR" ]]; then
    rm -rf -- "$WORK_DIR"
  elif [[ -n "$STATE_PATH" ]]; then
    printf '[authorization-ui-e2e] ERROR: recovery state retained at %s\n' "$STATE_PATH" >&2
    printf '[authorization-ui-e2e] Retry cleanup: python3 %s cleanup --state %s --marker %s\n' \
      "$ROOT_DIR/scripts/authorization_ui_e2e.py" "$STATE_PATH" "$MARKER" >&2
    printf '[authorization-ui-e2e] Retry services: docker compose -p %s -f %s up -d --no-deps --force-recreate backend backend-admin\n' \
      "$COMPOSE_PROJECT_NAME" "$ROOT_DIR/docker-compose.yml" >&2
    printf '[authorization-ui-e2e] Retry verification: python3 %s verify-clean --state %s --marker %s\n' \
      "$ROOT_DIR/scripts/authorization_ui_e2e.py" "$STATE_PATH" "$MARKER" >&2
  fi

  if ((primary_code != 0)); then
    exit "$primary_code"
  fi
  exit "$cleanup_code"
}
trap cleanup EXIT INT TERM

docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
  up -d --no-recreate postgres redis keycloak temporal minio
# Empty KB deletion still consults RAG metadata. Start its API/dependencies
# before creating any fixture; indexing and the reranker are not needed here.
# Build separately: `up --build` would also rebuild and recreate postgres under
# the running Keycloak/Temporal connection pools.
docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
  --profile rag build rag-api
docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
  --profile rag up -d --no-build rag-api --wait --wait-timeout 180
docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
  up -d --no-deps --force-recreate backend backend-admin
wait_for_url "$E2E_KEYCLOAK_URL/realms/aurum-ai" "Keycloak"
wait_for_url "$E2E_BUSINESS_BACKEND_URL/ready" "ordinary backend"
wait_for_url "$E2E_ADMIN_BACKEND_URL/ready" "ordinary backend-admin"
python3 "$ROOT_DIR/scripts/authorization_ui_e2e.py" sync-identities

TEST_GATEWAY_MODEL_ID="$MARKER-model"
export E2E_AUTHORIZATION_UI=true E2E_AUTHORIZATION_UI_STATE="$STATE_PATH"
export E2E_AUTHORIZATION_UI_MARKER="$MARKER" TEST_GATEWAY_MODEL_ID

docker compose -p "$COMPOSE_PROJECT_NAME" \
  -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" \
  --profile build build runner
APP_RATE_LIMIT_PER_MINUTE="$AUTHORIZATION_UI_E2E_RATE_LIMIT_PER_MINUTE" \
APP_RATE_LIMIT_PER_MINUTE_STREAM="$AUTHORIZATION_UI_E2E_RATE_LIMIT_PER_MINUTE_STREAM" \
RUNNER_POOL_SIZE=0 docker compose -p "$COMPOSE_PROJECT_NAME" \
  -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" \
  up -d --build --no-deps --force-recreate backend backend-admin test-gateway
docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
  up -d --build --no-deps frontend admin-ui

wait_for_url "$E2E_BUSINESS_BACKEND_URL/ready" "backend"
wait_for_url "$E2E_ADMIN_BACKEND_URL/ready" "backend-admin"
wait_for_url "$E2E_BUSINESS_UI_URL/login" "Business UI"
wait_for_url "$E2E_ADMIN_UI_URL/login" "Admin UI"

python3 "$ROOT_DIR/scripts/authorization_ui_e2e.py" prepare \
  --state "$STATE_PATH" --marker "$MARKER"
(
  cd "$ROOT_DIR/business-ui"
  npm exec -- playwright test e2e/authorization-ui.real.spec.ts \
    --project=chromium --workers=1 --trace=off
)
