#!/usr/bin/env bash
# Manual/exclusive. Other smoke families do not share this lock: the operator
# must stop all organization-mutating/browser smoke before acknowledging it.
set -euo pipefail
umask 077
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export WEB_ACCEPTANCE_KIND="${WEB_ACCEPTANCE_KIND:-controlled}"
case "$WEB_ACCEPTANCE_KIND" in controlled|public) ;; *) printf 'BLOCKED: invalid WEB_ACCEPTANCE_KIND\n' >&2; exit 2;; esac
[[ "${WEB_ACCEPTANCE_EXCLUSIVE:-}" == 1 ]] || { printf 'BLOCKED: set WEB_ACCEPTANCE_EXCLUSIVE=1 only after stopping all other org-mutating/browser smoke\n' >&2; exit 2; }
for prerequisite in APP_SECRET_KEY WEB_TOOLS_SERVICE_SECRET SEARXNG_SECRET; do
  [[ -n "${!prerequisite:-}" ]] || { printf 'BLOCKED: missing %s\n' "$prerequisite" >&2; exit 2; }
done
for command in docker curl python3 npm; do command -v "$command" >/dev/null || { printf 'BLOCKED: missing %s\n' "$command" >&2; exit 2; }; done
docker info >/dev/null 2>&1 || { printf 'BLOCKED: Docker daemon unavailable\n' >&2; exit 2; }
export E2E_BUSINESS_UI_URL="${E2E_BUSINESS_UI_URL:-http://localhost:3000}"
export E2E_ADMIN_UI_URL="${E2E_ADMIN_UI_URL:-http://localhost:3100}"
export E2E_BUSINESS_BACKEND_URL="${E2E_BUSINESS_BACKEND_URL:-http://localhost:8000}"
export E2E_ADMIN_BACKEND_URL="${E2E_ADMIN_BACKEND_URL:-http://localhost:8001}"
export E2E_KEYCLOAK_URL="${E2E_KEYCLOAK_URL:-http://localhost:8080}"
export E2E_POSTGRES_CONTAINER="${E2E_POSTGRES_CONTAINER:-aurum-ai-postgres}"
PROJECT=$(docker inspect "$E2E_POSTGRES_CONTAINER" --format '{{index .Config.Labels "com.docker.compose.project"}}')
[[ "$PROJECT" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || exit 2
verify_service() {
  local container="$1" service="$2" project_label service_label
  project_label=$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.project"}}')
  service_label=$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.service"}}')
  [[ "$project_label" == "$PROJECT" && "$service_label" == "$service" ]] || { printf 'BLOCKED: Compose ownership mismatch\n' >&2; return 2; }
}
verify_service "$E2E_POSTGRES_CONTAINER" postgres
for service in backend backend-admin frontend admin-ui keycloak temporal; do verify_service "aurum-ai-$service" "$service"; done
BASE=(docker compose -p "$PROJECT" -f "$ROOT_DIR/docker-compose.yml")
WEB=("${BASE[@]}" -f "$ROOT_DIR/docker-compose.web.yml" -f "$ROOT_DIR/docker-compose.e2e.yml" -f "$ROOT_DIR/docker-compose.e2e-web-probes.yml")
if [[ "$WEB_ACCEPTANCE_KIND" == controlled ]]; then WEB+=(-f "$ROOT_DIR/docker-compose.e2e-web.yml"); fi
WEB_SERVICES=(test-gateway web-tools web-tools-egress searxng searxng-egress web-private-fixture)
if [[ "$WEB_ACCEPTANCE_KIND" == controlled ]]; then WEB_SERVICES+=(web-fixture); fi
for service in "${WEB_SERVICES[@]}"; do
  # Do not adopt or stop any pre-existing web deployment or another smoke.
  [[ -z "$("${WEB[@]}" ps -a -q "$service")" ]] || { printf 'BLOCKED: existing %s; recover its owner first\n' "$service" >&2; exit 2; }
done
LOCK_DIR="${WEB_ACCEPTANCE_LOCK:-/tmp/aurum-ai-web-acceptance.lock}"
mkdir -m 700 -- "$LOCK_DIR" 2>/dev/null || { printf 'BLOCKED: web acceptance lock exists; recover its owner first\n' >&2; exit 2; }
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/web-tools-e2e.XXXXXXXX")
STATE_PATH="$WORK_DIR/state.json"
# Reuse the strict C5 journal schema/marker and its exact three conversation
# intents; do not broaden its ownership matcher for web acceptance.
MARKER="workflow-templates-ui-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
export TEST_GATEWAY_MODEL_ID="$MARKER-model"
export E2E_WEB_TOOLS=true E2E_WEB_TOOLS_STATE="$STATE_PATH" E2E_WEB_TOOLS_MARKER="$MARKER"
export PLAYWRIGHT_NO_COPY_PROMPT=1
OVERLAY_STARTED=0
wait_ready() {
  local attempt
  for ((attempt=0; attempt<120; attempt++)); do
    if curl -fsS --max-time 3 "$1" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  printf 'Readiness failed\n' >&2; return 1
}
cleanup() {
  local primary=$? secondary=0 code
  trap - EXIT INT TERM
  set +e
  if [[ -f "$STATE_PATH" ]]; then
    python3 "$ROOT_DIR/scripts/web_tools_e2e.py" cleanup --state "$STATE_PATH" --marker "$MARKER"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
  fi
  if ((OVERLAY_STARTED)); then
    "${WEB[@]}" --profile web rm -sf "${WEB_SERVICES[@]}"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
    "${BASE[@]}" up -d --no-deps --force-recreate backend backend-admin
    code=$?; if ((code && !secondary)); then secondary=$code; fi
    wait_ready "$E2E_BUSINESS_BACKEND_URL/ready"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
    wait_ready "$E2E_ADMIN_BACKEND_URL/ready"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
  fi
  if [[ -f "$STATE_PATH" ]]; then
    python3 "$ROOT_DIR/scripts/web_tools_e2e.py" verify-clean --state "$STATE_PATH" --marker "$MARKER"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
  fi
  if ((!primary && !secondary)); then
    if [[ -f "$STATE_PATH" ]]; then rm -- "$STATE_PATH"; fi
    rmdir -- "$WORK_DIR" "$LOCK_DIR"
    code=$?; if ((code)); then secondary=$code; fi
  else
    printf 'recovery state retained at %s\n' "$STATE_PATH" >&2
    printf 'Recover exclusively with original secret/URL/credential overrides; no secrets are stored in this journal.\n' >&2
    printf 'python3 %q cleanup --state %q --marker %q\n' "$ROOT_DIR/scripts/web_tools_e2e.py" "$STATE_PATH" "$MARKER" >&2
    printf 'TEST_GATEWAY_MODEL_ID=%q ' "$MARKER-model" >&2
    printf '%q ' "${WEB[@]}" --profile web rm -sf "${WEB_SERVICES[@]}" >&2
    printf '\n' >&2
    printf '%q ' "${BASE[@]}" up -d --no-deps --force-recreate backend backend-admin >&2
    printf '\n' >&2
    printf 'curl -fsS %q && curl -fsS %q\n' "$E2E_BUSINESS_BACKEND_URL/ready" "$E2E_ADMIN_BACKEND_URL/ready" >&2
    printf 'python3 %q verify-clean --state %q --marker %q\n' "$ROOT_DIR/scripts/web_tools_e2e.py" "$STATE_PATH" "$MARKER" >&2
    printf 'Only after successful verification remove exact journal %q and empty directories %q %q.\n' "$STATE_PATH" "$WORK_DIR" "$LOCK_DIR" >&2
  fi
  if ((primary)); then exit "$primary"; fi
  exit "$secondary"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 "$ROOT_DIR/scripts/web_tools_e2e.py" init --state "$STATE_PATH" --marker "$MARKER"
"${WEB[@]}" --profile build build runner
OVERLAY_STARTED=1
WEB_ENABLED=true USER_QUESTIONS_ENABLED=true RUNNER_POOL_SIZE=0 APP_RATE_LIMIT_PER_MINUTE=3000 APP_RATE_LIMIT_PER_MINUTE_STREAM=500 \
  "${WEB[@]}" --profile web up -d --build --no-deps --force-recreate backend backend-admin test-gateway
# Build separately: `up --build` would also rebuild and recreate postgres.
WEB_ENABLED=true "${WEB[@]}" --profile web build web-tools searxng web-private-fixture
WEB_ENABLED=true "${WEB[@]}" --profile web up -d --no-build web-tools searxng
WEB_ENABLED=true "${WEB[@]}" --profile web up -d --no-build --wait web-private-fixture
if [[ "$WEB_ACCEPTANCE_KIND" == controlled ]]; then
  WEB_ENABLED=true "${WEB[@]}" --profile web build web-fixture
  WEB_ENABLED=true "${WEB[@]}" --profile web up -d --no-build web-fixture
  python3 "$ROOT_DIR/scripts/web_tools_e2e.py" install-controlled-resolver --state "$STATE_PATH" --marker "$MARKER"
fi
"${BASE[@]}" up -d --build --no-deps --force-recreate frontend
wait_ready "$E2E_BUSINESS_BACKEND_URL/ready"
wait_ready "$E2E_ADMIN_BACKEND_URL/ready"
wait_ready "$E2E_BUSINESS_UI_URL/login"
wait_ready "$E2E_ADMIN_UI_URL/login"
python3 "$ROOT_DIR/scripts/web_tools_e2e.py" application-ready --state "$STATE_PATH" --marker "$MARKER"
python3 "$ROOT_DIR/scripts/web_tools_e2e.py" network-probes --state "$STATE_PATH" --marker "$MARKER"
python3 "$ROOT_DIR/scripts/web_tools_e2e.py" prepare --state "$STATE_PATH" --marker "$MARKER"
(
  cd "$ROOT_DIR/business-ui"
  npm exec -- playwright test e2e/web-tools.real.spec.ts --config=playwright.web-tools.config.ts --project=chromium --workers=1 --trace=off
)
