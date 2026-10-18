#!/usr/bin/env bash
# Manual/exclusive Compose acceptance for durable user questions. Do not run
# alongside any organization-mutating or browser smoke against the same tenant.
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export QUESTION_ACCEPTANCE_KIND="${QUESTION_ACCEPTANCE_KIND:-short}"
case "$QUESTION_ACCEPTANCE_KIND" in
  short|restart|hour) ;;
  *)
    printf 'QUESTION_ACCEPTANCE_KIND must be short, restart or hour\n' >&2
    exit 2
    ;;
esac
for command in docker curl flock npm python3; do command -v "$command" >/dev/null; done
LOCK_PATH="${TMPDIR:-/tmp}/aurum-ai-user-questions-e2e.lock"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  printf 'Another user-question acceptance owns %s\n' "$LOCK_PATH" >&2
  exit 3
fi
: "${APP_SECRET_KEY:?APP_SECRET_KEY must match the Compose stack}"

MARKER="user-questions-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
[[ "$MARKER" =~ ^user-questions-e2e-[a-f0-9]{16}$ ]] || exit 1

export E2E_BUSINESS_UI_URL="${E2E_BUSINESS_UI_URL:-http://localhost:3000}"
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
for service in backend backend-admin frontend keycloak temporal; do
  verify_service "aurum-ai-$service" "$service"
done

BASE=(docker compose -p "$PROJECT" -f "$ROOT_DIR/docker-compose.yml")
OVERLAY=("${BASE[@]}" -f "$ROOT_DIR/docker-compose.e2e.yml")
if [[ -n "$("${OVERLAY[@]}" ps -a -q test-gateway)" ]]; then
  printf 'Existing test-gateway: finish its owner recovery first\n' >&2
  exit 1
fi

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/user-questions-e2e.XXXXXXXX")
STATE_PATH="$WORK_DIR/state.json"
export E2E_USER_QUESTIONS=true E2E_USER_QUESTIONS_STATE="$STATE_PATH"
export E2E_USER_QUESTIONS_MARKER="$MARKER"
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
    python3 "$ROOT_DIR/scripts/user_questions_e2e.py" cleanup --state "$STATE_PATH" --marker "$MARKER"
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
    python3 "$ROOT_DIR/scripts/user_questions_e2e.py" verify-clean --state "$STATE_PATH" --marker "$MARKER"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
  fi
  if ((!primary && !secondary)); then
    if [[ -f "$STATE_PATH" ]]; then rm -- "$STATE_PATH"; fi
    rmdir -- "$WORK_DIR"
    code=$?; if ((code)); then secondary=$code; fi
  else
    printf 'recovery state retained at %s\n' "$STATE_PATH" >&2
    printf 'Run recovery exclusively with the original operator overrides; secrets are not stored in the journal.\n' >&2
    for action in cleanup verify-clean; do
      printf 'E2E_BUSINESS_BACKEND_URL=%q E2E_ADMIN_BACKEND_URL=%q E2E_KEYCLOAK_URL=%q E2E_POSTGRES_CONTAINER=%q python3 %q %s --state %q --marker %q\n' \
        "$E2E_BUSINESS_BACKEND_URL" "$E2E_ADMIN_BACKEND_URL" "$E2E_KEYCLOAK_URL" "$E2E_POSTGRES_CONTAINER" \
        "$ROOT_DIR/scripts/user_questions_e2e.py" "$action" "$STATE_PATH" "$MARKER" >&2
      if [[ "$action" == cleanup ]]; then
        printf 'TEST_GATEWAY_MODEL_ID=%q docker compose -p %q -f %q -f %q rm -sf test-gateway\n' \
          "$MARKER-model" "$PROJECT" "$ROOT_DIR/docker-compose.yml" "$ROOT_DIR/docker-compose.e2e.yml" >&2
        printf 'docker compose -p %q -f %q up -d --no-deps --force-recreate backend backend-admin\n' \
          "$PROJECT" "$ROOT_DIR/docker-compose.yml" >&2
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
OVERLAY_STARTED=1
APP_RATE_LIMIT_PER_MINUTE=3000 APP_RATE_LIMIT_PER_MINUTE_STREAM=500 RUNNER_POOL_SIZE=0 \
  USER_QUESTIONS_ENABLED=true \
  "${OVERLAY[@]}" up -d --build --no-deps --force-recreate backend backend-admin test-gateway
"${BASE[@]}" up -d --build --no-deps frontend
wait_ready "$E2E_KEYCLOAK_URL/realms/${E2E_KEYCLOAK_REALM:-aurum-ai}"
wait_ready "$E2E_BUSINESS_BACKEND_URL/ready"
wait_ready "$E2E_ADMIN_BACKEND_URL/ready"
wait_ready "$E2E_BUSINESS_UI_URL/login"
python3 "$ROOT_DIR/scripts/user_questions_e2e.py" prepare --state "$STATE_PATH" --marker "$MARKER"
(
  cd "$ROOT_DIR/business-ui"
  npm exec -- playwright test e2e/user-questions.real.spec.ts --project=chromium --workers=1 --trace=off
)
