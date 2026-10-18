#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
COMPOSE_PROJECT_NAME=""
BACKENDS_REPLACED=0
STACK_APP_SECRET_KEY=""

log() { printf '[organization-ui-e2e] %s\n' "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

wait_for_url() {
  local url="$1" name="$2" attempt
  for attempt in $(seq 1 120); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  log "ERROR: $name did not become ready"
  return 1
}

require_owned_service() {
  local container="$1" service="$2" actual_project actual_service
  actual_project=$(docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project"}}' "$container")
  actual_service=$(docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.service"}}' "$container")
  if [[ "$actual_project" != "$COMPOSE_PROJECT_NAME" || "$actual_service" != "$service" ]]; then
    fail "$container is not the expected $COMPOSE_PROJECT_NAME/$service service"
  fi
}

cleanup() {
  local exit_code=$? restore_code=0
  trap - EXIT INT TERM
  set +e
  if [[ "$BACKENDS_REPLACED" == "1" ]]; then
    APP_SECRET_KEY="$STACK_APP_SECRET_KEY" docker compose \
      -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
      up -d --no-deps --force-recreate backend backend-admin
    restore_code=$?
    if ((restore_code == 0)); then
      wait_for_url "http://localhost:8000/ready" "backend" || restore_code=$?
      wait_for_url "http://localhost:8001/ready" "backend-admin" || restore_code=$?
    fi
    if ((restore_code != 0)); then
      log "ERROR: backend restore failed"
      log "Retry: docker compose -p $COMPOSE_PROJECT_NAME -f $ROOT_DIR/docker-compose.yml up -d --no-deps --force-recreate backend backend-admin"
    fi
  fi
  if ((exit_code != 0)); then
    exit "$exit_code"
  fi
  exit "$restore_code"
}
trap cleanup EXIT INT TERM

main() {
  command -v curl >/dev/null 2>&1 || fail "required command is missing: curl"
  command -v docker >/dev/null 2>&1 || fail "required command is missing: docker"
  wait_for_url "http://localhost:8000/ready" "backend" || exit 1
  wait_for_url "http://localhost:8001/ready" "backend-admin" || exit 1
  wait_for_url "http://localhost:3000/login" "Business UI" || exit 1
  wait_for_url "http://localhost:3100/login" "Admin UI" || exit 1

  COMPOSE_PROJECT_NAME=$(docker inspect aurum-ai-postgres \
    -f '{{index .Config.Labels "com.docker.compose.project"}}')
  [[ -n "$COMPOSE_PROJECT_NAME" ]] || fail "Compose project label is missing"
  require_owned_service "aurum-ai-backend" "backend"
  require_owned_service "aurum-ai-backend-admin" "backend-admin"
  STACK_APP_SECRET_KEY=$(docker inspect -f \
    '{{range .Config.Env}}{{println .}}{{end}}' aurum-ai-backend | \
    sed -n 's/^APP_SECRET_KEY=//p')
  [[ -n "$STACK_APP_SECRET_KEY" ]] || fail "owned backend has no APP_SECRET_KEY"

  log "Building both UI applications from the current worktree"
  APP_SECRET_KEY="$STACK_APP_SECRET_KEY" docker compose \
    -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
    up -d --build --no-deps --force-recreate frontend admin-ui
  wait_for_url "http://localhost:3000/login" "Business UI" || exit 1
  wait_for_url "http://localhost:3100/login" "Admin UI" || exit 1

  BACKENDS_REPLACED=1
  APP_SECRET_KEY="$STACK_APP_SECRET_KEY" docker compose \
    -p "$COMPOSE_PROJECT_NAME" -f "$ROOT_DIR/docker-compose.yml" \
    stop backend backend-admin

  E2E_ORGANIZATION_UI=true \
  QUOTA_E2E_BACKEND_PORT=8000 \
  QUOTA_E2E_ADMIN_PORT=8001 \
    "$ROOT_DIR/smoke-test-quota.sh"
}

main "$@"
