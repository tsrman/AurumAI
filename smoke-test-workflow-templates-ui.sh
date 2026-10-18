#!/usr/bin/env bash
# Manual/exclusive: do not overlap ANY org-mutating smoke (authorization,
# groups, quota, organization, workflow, multi-agent, knowledge, browser smoke).
# This operator contract is shared with existing wrappers, not a universal lock.
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKFLOW_ACCEPTANCE_KIND="${WORKFLOW_ACCEPTANCE_KIND:-templates}"
case "$WORKFLOW_ACCEPTANCE_KIND" in
  templates|routing|branching|hitl|hitl-restart|loops) ;;
  *) printf 'Unknown workflow acceptance kind\n' >&2; exit 2 ;;
esac
export WORKFLOW_ACCEPTANCE_KIND
for command in docker curl python3; do command -v "$command" >/dev/null; done
if [[ "$WORKFLOW_ACCEPTANCE_KIND" != loops ]]; then command -v npm >/dev/null; fi
: "${APP_SECRET_KEY:?APP_SECRET_KEY must match the Compose stack}"
MARKER="workflow-templates-ui-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
[[ "$MARKER" =~ ^workflow-templates-ui-e2e-[a-f0-9]{16}$ ]] || exit 1

export E2E_BUSINESS_UI_URL="${E2E_BUSINESS_UI_URL:-http://localhost:3000}"
export E2E_ADMIN_UI_URL="${E2E_ADMIN_UI_URL:-http://localhost:3100}"
export E2E_BUSINESS_BACKEND_URL="${E2E_BUSINESS_BACKEND_URL:-http://localhost:8000}"
export E2E_ADMIN_BACKEND_URL="${E2E_ADMIN_BACKEND_URL:-http://localhost:8001}"
export E2E_KEYCLOAK_URL="${E2E_KEYCLOAK_URL:-http://localhost:8080}"
export E2E_POSTGRES_CONTAINER="${E2E_POSTGRES_CONTAINER:-aurum-ai-postgres}"
export TEST_GATEWAY_MODEL_ID="$MARKER-model"
PROJECT=$(docker inspect "$E2E_POSTGRES_CONTAINER" --format '{{index .Config.Labels "com.docker.compose.project"}}')
[[ "$PROJECT" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || exit 1

verify_service() {
  local container="$1" service="$2" project_label service_label
  project_label=$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.project"}}')
  service_label=$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.service"}}')
  if [[ "$project_label" != "$PROJECT" || "$service_label" != "$service" ]]; then
    printf 'Compose ownership mismatch: %s\n' "$container" >&2
    return 1
  fi
}
verify_service "$E2E_POSTGRES_CONTAINER" postgres
for service in backend backend-admin frontend admin-ui keycloak temporal; do
  verify_service "aurum-ai-$service" "$service"
done
BASE=(docker compose -p "$PROJECT" -f "$ROOT_DIR/docker-compose.yml")
OVERLAY=("${BASE[@]}" -f "$ROOT_DIR/docker-compose.e2e.yml")
# Refuse to adopt another test's already-running deterministic gateway.
if [[ -n "$("${OVERLAY[@]}" ps -a -q test-gateway)" ]]; then
  printf 'Existing test-gateway: finish its owner recovery first\n' >&2
  exit 1
fi
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/workflow-templates-ui-e2e.XXXXXXXX")
STATE_PATH="$WORK_DIR/state.json"
export E2E_WORKFLOW_TEMPLATES_UI=true E2E_WORKFLOW_TEMPLATES_UI_STATE="$STATE_PATH"
export E2E_WORKFLOW_TEMPLATES_UI_MARKER="$MARKER"
OVERLAY_STARTED=0

wait_ready() {
  local url="$1" attempt
  for ((attempt=0; attempt<120; attempt++)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  printf 'Readiness failed: %s\n' "$url" >&2
  return 1
}

cleanup() {
  local primary=$? secondary=0 code
  trap - EXIT INT TERM
  set +e
  if [[ -f "$STATE_PATH" ]]; then
    python3 "$ROOT_DIR/scripts/workflow_templates_ui_e2e.py" cleanup --state "$STATE_PATH" --marker "$MARKER"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
  fi
  if ((OVERLAY_STARTED)); then
    "${OVERLAY[@]}" rm -sf test-gateway
    code=$?; if ((code && !secondary)); then secondary=$code; fi
    "${BASE[@]}" up -d --no-deps --force-recreate backend backend-admin
    code=$?; if ((code && !secondary)); then secondary=$code; fi
    wait_ready "$E2E_BUSINESS_BACKEND_URL/ready"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
    wait_ready "$E2E_ADMIN_BACKEND_URL/ready"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
  fi
  if [[ -f "$STATE_PATH" ]]; then
    python3 "$ROOT_DIR/scripts/workflow_templates_ui_e2e.py" verify-clean --state "$STATE_PATH" --marker "$MARKER"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
  fi
  if ((!primary && !secondary)); then
    # Only our exact journal and now-empty mktemp directory, never recursive.
    if [[ -f "$STATE_PATH" ]]; then rm -- "$STATE_PATH"; fi
    rmdir -- "$WORK_DIR"
    code=$?; if ((code)); then secondary=$code; fi
  else
    printf 'recovery state retained at %s\n' "$STATE_PATH" >&2
    printf 'Run recovery exclusively; restore your original APP_SECRET_KEY and credential overrides (never stored here).\n' >&2
    for action in cleanup verify-clean; do
      printf 'E2E_BUSINESS_BACKEND_URL=%q E2E_ADMIN_BACKEND_URL=%q E2E_KEYCLOAK_URL=%q E2E_POSTGRES_CONTAINER=%q python3 %q %s --state %q --marker %q\n' \
        "$E2E_BUSINESS_BACKEND_URL" "$E2E_ADMIN_BACKEND_URL" "$E2E_KEYCLOAK_URL" "$E2E_POSTGRES_CONTAINER" \
        "$ROOT_DIR/scripts/workflow_templates_ui_e2e.py" "$action" "$STATE_PATH" "$MARKER" >&2
      if [[ "$action" == cleanup ]]; then
        printf 'TEST_GATEWAY_MODEL_ID=%q docker compose -p %q -f %q -f %q rm -sf test-gateway\n' "$MARKER-model" "$PROJECT" "$ROOT_DIR/docker-compose.yml" "$ROOT_DIR/docker-compose.e2e.yml" >&2
        printf 'docker compose -p %q -f %q up -d --no-deps --force-recreate backend backend-admin\n' "$PROJECT" "$ROOT_DIR/docker-compose.yml" >&2
        printf 'curl -fsS %q && curl -fsS %q\n' "$E2E_BUSINESS_BACKEND_URL/ready" "$E2E_ADMIN_BACKEND_URL/ready" >&2
      fi
    done
  fi
  if ((primary)); then exit "$primary"; fi
  exit "$secondary"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${BASE[@]}" up -d --no-recreate postgres redis keycloak temporal minio
"${OVERLAY[@]}" --profile build build runner
if [[ "$WORKFLOW_ACCEPTANCE_KIND" == hitl ]]; then
  OVERLAY+=(-f "$ROOT_DIR/docker-compose.e2e-hitl.yml")
  "${OVERLAY[@]}" --profile build build hitl-runner
fi
OVERLAY_STARTED=1
APP_RATE_LIMIT_PER_MINUTE=3000 APP_RATE_LIMIT_PER_MINUTE_STREAM=500 RUNNER_POOL_SIZE=0 \
  "${OVERLAY[@]}" up -d --build --no-deps --force-recreate backend backend-admin test-gateway
if [[ "$WORKFLOW_ACCEPTANCE_KIND" != loops ]]; then
  "${BASE[@]}" up -d --build --no-deps frontend admin-ui
fi
wait_ready "$E2E_KEYCLOAK_URL/realms/${E2E_KEYCLOAK_REALM:-aurum-ai}"
wait_ready "$E2E_BUSINESS_BACKEND_URL/ready"
wait_ready "$E2E_ADMIN_BACKEND_URL/ready"
if [[ "$WORKFLOW_ACCEPTANCE_KIND" != loops ]]; then
  wait_ready "$E2E_BUSINESS_UI_URL/login"
  wait_ready "$E2E_ADMIN_UI_URL/login"
fi
python3 "$ROOT_DIR/scripts/workflow_templates_ui_e2e.py" prepare --state "$STATE_PATH" --marker "$MARKER"
if [[ "$WORKFLOW_ACCEPTANCE_KIND" == loops ]]; then
  python3 "$ROOT_DIR/scripts/workflow_loops_e2e.py"
else
(
  cd "$ROOT_DIR/business-ui"
  npm exec -- playwright test "e2e/workflow-$WORKFLOW_ACCEPTANCE_KIND.real.spec.ts" --project=chromium --workers=1 --trace=off
)
fi
