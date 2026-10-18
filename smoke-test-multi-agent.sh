#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BASE_URL="${BASE_URL:-http://localhost:8000}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-http://localhost:8001}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
MULTI_AGENT_E2E_RUNTIME="${MULTI_AGENT_E2E_RUNTIME:-docker}"
COMPOSE_PROJECT_NAME="${MULTI_AGENT_E2E_COMPOSE_PROJECT:-}"
MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE="${MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE:-2000}"
MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE_STREAM="${MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE_STREAM:-500}"

if [[ -z "$COMPOSE_PROJECT_NAME" ]]; then
  COMPOSE_PROJECT_NAME=$(docker inspect aurum-ai-postgres \
    --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)
  COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-aurum-ai}"
fi

export BASE_URL ADMIN_BASE_URL KEYCLOAK_URL MULTI_AGENT_E2E_RUNTIME

wait_for_url() {
  local url="$1" name="$2"
  local attempt
  for attempt in $(seq 1 120); do
    if curl -sf "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf '[multi-agent-smoke] ERROR: %s did not become ready\n' "$name" >&2
  return 1
}

if [[ "$MULTI_AGENT_E2E_RUNTIME" != "docker" || "${MULTI_AGENT_E2E_MANAGE_COMPOSE:-1}" != "1" ]]; then
  exec python3 "$ROOT_DIR/scripts/multi_agent_e2e.py"
fi

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  docker compose -p "$COMPOSE_PROJECT_NAME" \
    -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" \
    rm -sf test-gateway >/dev/null 2>&1 || true
  docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
    up -d --no-deps --force-recreate \
    backend backend-admin >/dev/null 2>&1 || true
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

docker compose -p "$COMPOSE_PROJECT_NAME" \
  -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" \
  --profile build build runner
docker compose -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
  up -d --no-recreate postgres redis keycloak temporal minio
APP_RATE_LIMIT_PER_MINUTE="$MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE" \
APP_RATE_LIMIT_PER_MINUTE_STREAM="$MULTI_AGENT_E2E_RATE_LIMIT_PER_MINUTE_STREAM" \
RUNNER_POOL_SIZE=0 docker compose -p "$COMPOSE_PROJECT_NAME" \
  -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" \
  up -d --build --no-deps --force-recreate backend backend-admin test-gateway
wait_for_url "$KEYCLOAK_URL/realms/aurum-ai" "Keycloak"
wait_for_url "$BASE_URL/ready" "backend"
wait_for_url "$ADMIN_BASE_URL/ready" "backend-admin"
python3 "$ROOT_DIR/scripts/multi_agent_e2e.py"
