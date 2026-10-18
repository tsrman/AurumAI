#!/usr/bin/env bash
set -euo pipefail

# Deterministic full-stack quota accounting smoke test.
#
# The script owns an isolated PostgreSQL container, a minimal backend process
# mounting the production auth/chat/usage routers, and a deterministic fake LLM
# gateway. Keycloak is the only shared prerequisite; no real provider, Redis,
# Temporal, or runner is contacted.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BACKEND_DIR="$ROOT_DIR/business-backend"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-aurum-ai}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-business-ui}"
ADMIN_CLIENT_ID="${ADMIN_CLIENT_ID:-admin-ui}"
MEMBER_USER="${TEST_USER:-user}"
MEMBER_PASSWORD="${TEST_PASSWORD:-user}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
# The partition migration needs pg_partman, which only the Compose postgres image installs.
POSTGRES_IMAGE="${QUOTA_E2E_POSTGRES_IMAGE:-aurum-ai-postgres:latest}"
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/quota-e2e-uv-cache}"
APP_SECRET_KEY="quota-e2e-only-secret-31f2072425c949a7"
GATEWAY_API_KEY="quota-e2e-test"
MODEL_ID="quota-e2e-model"
E2E_ORGANIZATION_UI="${E2E_ORGANIZATION_UI:-false}"

PG_CONTAINER=""
PG_CREATED=0
BACKEND_PID=""
ADMIN_PID=""
GATEWAY_PID=""
TMP_DIR=""

log() { echo "[quota-smoke] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "$BACKEND_PID" ]]; then
    kill "$BACKEND_PID" >/dev/null 2>&1 || true
    wait "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$ADMIN_PID" ]]; then
    kill "$ADMIN_PID" >/dev/null 2>&1 || true
    wait "$ADMIN_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$GATEWAY_PID" ]]; then
    kill "$GATEWAY_PID" >/dev/null 2>&1 || true
    wait "$GATEWAY_PID" >/dev/null 2>&1 || true
  fi
  if [[ "$PG_CREATED" == "1" && -n "$PG_CONTAINER" ]]; then
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
  fi
  if [[ -n "$TMP_DIR" && "$TMP_DIR" == /tmp/quota-e2e.* ]]; then
    rm -rf -- "$TMP_DIR"
  fi
  if [[ "$exit_code" != "0" ]]; then
    log "Failed; all resources owned by this run were cleaned up"
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is missing: $1"
}

pick_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

wait_for_url() {
  local url="$1" name="$2" pid="${3:-}" timeout="${4:-60}"
  log "Waiting for $name"
  for _ in $(seq 1 "$timeout"); do
    if curl -sf "$url" >/dev/null 2>&1; then
      return 0
    fi
    if [[ -n "$pid" ]] && ! kill -0 "$pid" >/dev/null 2>&1; then
      fail "$name process exited before becoming ready"
    fi
    sleep 1
  done
  fail "$name did not become ready in ${timeout}s"
}

run_browser_acceptance() {
  local user_id="$1" period_from="$2" period_to="$3"
  if [[ "${E2E_ORGANIZATION_UI:-false}" != "true" ]]; then
    return 0
  fi
  if [[ "$backend_port" != "8000" || "$admin_port" != "8001" ]]; then
    fail "browser acceptance requires backend ports 8000/8001"
  fi

  export E2E_ORGANIZATION_UI
  export E2E_ORGANIZATION_UI_BUSINESS_URL="http://localhost:3000"
  export E2E_ORGANIZATION_UI_ADMIN_URL="http://localhost:3100"
  export E2E_ORGANIZATION_UI_ADMIN_API_URL="$admin_url"
  export E2E_ORGANIZATION_UI_KEYCLOAK_URL="$KEYCLOAK_URL"
  export E2E_ORGANIZATION_UI_MODEL="$MODEL_ID"
  export E2E_ORGANIZATION_UI_USER_ID="$user_id"
  export E2E_ORGANIZATION_UI_PERIOD_FROM="$period_from"
  export E2E_ORGANIZATION_UI_PERIOD_TO="$period_to"

  log "Running organization models and analytics browser acceptance"
  (
    cd "$ROOT_DIR/business-ui"
    npm exec -- playwright test e2e/organization.real.spec.ts \
      --project=chromium --workers=1 --trace=off
  )
}

assert_status() {
  local actual="$1" expected="$2" context="$3"
  [[ "$actual" == "$expected" ]] || fail "$context: expected HTTP $expected, got $actual"
}

assert_usage() {
  local file="$1" requests="$2" tokens="$3" request_limit="$4" token_limit="$5"
  python3 -c '
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))["data"]
expected=(int(sys.argv[2]), int(sys.argv[3]))
actual=(d["organization"]["requestsUsed"], d["organization"]["tokensUsed"])
assert actual == expected, (actual, expected)
assert (d["user"]["requestsUsed"], d["user"]["tokensUsed"]) == expected
def value(raw): return None if raw == "null" else int(raw)
assert d["organization"]["requestLimit"] == value(sys.argv[4])
assert d["organization"]["tokenLimit"] == value(sys.argv[5])
print(d["periodEnd"])
' "$file" "$requests" "$tokens" "$request_limit" "$token_limit"
}

assert_quota_error() {
  local file="$1" resource="$2" used="$3" limit="$4" period_end="$5"
  python3 -c '
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
assert set(d) == {"error", "meta"}
assert d["error"]["code"] == "quota_exceeded"
assert d["error"]["message"] == "Monthly quota exceeded"
assert d["error"]["details"] == {
    "resource": sys.argv[2], "used": int(sys.argv[3]),
    "limit": int(sys.argv[4]), "periodEnd": sys.argv[5],
}
assert set(d["meta"]) == {"requestId", "timestamp"}
' "$file" "$resource" "$used" "$limit" "$period_end"
}

assert_success_stream() {
  python3 -c '
import json, sys
frames=[line[6:] for line in open(sys.argv[1], encoding="utf-8") if line.startswith("data: ")]
assert frames[-1].strip() == "[DONE]", frames
chunks=[json.loads(frame) for frame in frames[:-1]]
usage=[chunk["usage"] for chunk in chunks if chunk.get("usage") is not None]
assert usage == [{"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}], usage
assert any(choice.get("delta", {}).get("content") for chunk in chunks for choice in chunk.get("choices", []))
' "$1"
}

assert_unaccounted_stream() {
  python3 -c '
import json, sys
frames=[line[6:] for line in open(sys.argv[1], encoding="utf-8") if line.startswith("data: ")]
assert frames[-1].strip() == "[DONE]", frames
chunks=[json.loads(frame) for frame in frames[:-1]]
assert all(chunk.get("usage") is None for chunk in chunks), chunks
assert any(choice.get("delta", {}).get("content") for chunk in chunks for choice in chunk.get("choices", []))
' "$1"
}

keycloak_token() {
  local client_id="$1" username="$2" password="$3"
  local response
  response=$(curl -sf -X POST \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=password" \
    -d "client_id=$client_id" \
    -d "username=$username" \
    -d "password=$password" \
    "$KEYCLOAK_URL/realms/$KEYCLOAK_REALM/protocol/openid-connect/token")
  python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' <<<"$response"
}

set_limits() {
  local org_id="$1" request_limit="$2" token_limit="$3"
  local request_sql token_sql
  [[ "$request_limit" == "null" ]] && request_sql="NULL" || request_sql="$request_limit"
  [[ "$token_limit" == "null" ]] && token_sql="NULL" || token_sql="$token_limit"
  docker exec "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d aurum_ai \
    -c "UPDATE organizations SET quota_requests=$request_sql, quota_tokens=$token_sql WHERE id='$org_id'::uuid" \
    >/dev/null
}

grant_model_access() {
  local org_id="$1" model_id="$2"
  docker exec "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d aurum_ai \
    -c "INSERT INTO organization_capabilities (org_id, capability)
        VALUES ('$org_id'::uuid, 'chat.send')
        ON CONFLICT DO NOTHING;
        INSERT INTO access_grants
          (id, org_id, resource_type, resource_id, principal_type, principal_id,
           permission, created_via)
        VALUES
          (gen_random_uuid(), '$org_id'::uuid, 'model', '$model_id',
           'organization', '$org_id'::uuid, 'read', 'provisioning')
        ON CONFLICT DO NOTHING" \
    >/dev/null
}

main() {
  require_command curl
  require_command docker
  require_command python3
  require_command uv

  wait_for_url "$KEYCLOAK_URL/realms/$KEYCLOAK_REALM" "Keycloak realm" "" 20
  TMP_DIR=$(mktemp -d /tmp/quota-e2e.XXXXXX)
  PG_CONTAINER="quota-e2e-postgres-$$"

  log "Starting isolated PostgreSQL"
  docker image inspect "$POSTGRES_IMAGE" >/dev/null 2>&1 \
    || fail "PostgreSQL image $POSTGRES_IMAGE is missing; run: docker compose build postgres"
  docker run -d --name "$PG_CONTAINER" \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=aurum_ai \
    -p 127.0.0.1::5432 \
    "$POSTGRES_IMAGE" >/dev/null
  PG_CREATED=1

  local postgres_ready=0
  for _ in $(seq 1 60); do
    if docker exec "$PG_CONTAINER" pg_isready -U postgres -d aurum_ai >/dev/null 2>&1; then
      postgres_ready=1
      break
    fi
    if [[ "$(docker inspect "$PG_CONTAINER" --format '{{.State.Running}}' 2>/dev/null || true)" != "true" ]]; then
      docker logs --tail 100 "$PG_CONTAINER" >&2 || true
      fail "isolated PostgreSQL exited during startup"
    fi
    sleep 1
  done
  if [[ "$postgres_ready" != "1" ]]; then
    docker logs --tail 100 "$PG_CONTAINER" >&2 || true
    fail "isolated PostgreSQL did not become ready"
  fi

  local pg_port migration_database_url app_database_url
  pg_port=$(docker port "$PG_CONTAINER" 5432/tcp | sed -n 's/.*://p' | tail -1)
  [[ "$pg_port" =~ ^[0-9]+$ ]] || fail "could not determine PostgreSQL port"
  migration_database_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:$pg_port/aurum_ai"
  app_database_url="postgresql+asyncpg://aurum_ai_app:aurum_ai_app@127.0.0.1:$pg_port/aurum_ai"

  log "Applying the full Alembic migration chain"
  (
    cd "$BACKEND_DIR"
    env APP_DEBUG=true \
      APP_DATABASE_URL="$migration_database_url" \
      APP_DATABASE_ROLE_PASSWORD=aurum_ai_app \
      UV_CACHE_DIR="$UV_CACHE_DIR" \
      uv run --frozen alembic upgrade head
  ) >/dev/null

  local backend_port admin_port gateway_port backend_url admin_url gateway_url
  backend_port="${QUOTA_E2E_BACKEND_PORT:-$(pick_port)}"
  admin_port="${QUOTA_E2E_ADMIN_PORT:-$(pick_port)}"
  gateway_port="${QUOTA_E2E_GATEWAY_PORT:-$(pick_port)}"
  [[ "$backend_port" != "$admin_port" && "$backend_port" != "$gateway_port" && "$admin_port" != "$gateway_port" ]] \
    || fail "backend, admin, and gateway ports must differ"
  backend_url="http://127.0.0.1:$backend_port"
  admin_url="http://127.0.0.1:$admin_port"
  gateway_url="http://127.0.0.1:$gateway_port"

  log "Starting deterministic gateway"
  (
    cd "$BACKEND_DIR"
    env QUOTA_E2E_GATEWAY_PORT="$gateway_port" UV_CACHE_DIR="$UV_CACHE_DIR" \
      uv run --frozen python ../scripts/quota-test-gateway.py
  ) >"$TMP_DIR/gateway.log" 2>&1 &
  GATEWAY_PID=$!

  log "Starting minimal backend with production quota routes"
  (
    cd "$BACKEND_DIR"
    env APP_DEBUG=true \
      APP_DATABASE_URL="$app_database_url" \
      APP_SECRET_KEY="$APP_SECRET_KEY" \
      APP_KEYCLOAK_URL="$KEYCLOAK_URL" \
      APP_KEYCLOAK_REALM="$KEYCLOAK_REALM" \
      APP_KEYCLOAK_CLIENT_ID="$KEYCLOAK_CLIENT_ID" \
      APP_LLM_GATEWAY_URL="$gateway_url" \
      APP_GATEWAY_API_KEY="$GATEWAY_API_KEY" \
      APP_RUNNER_POOL_SIZE=0 \
      QUOTA_E2E_BACKEND_PORT="$backend_port" \
      UV_CACHE_DIR="$UV_CACHE_DIR" \
      uv run --frozen python ../scripts/quota-test-app.py
  ) >"$TMP_DIR/backend.log" 2>&1 &
  BACKEND_PID=$!

  log "Starting minimal admin backend with production analytics routes"
  (
    cd "$BACKEND_DIR"
    env APP_DEBUG=true \
      APP_DATABASE_URL="$app_database_url" \
      APP_SECRET_KEY="$APP_SECRET_KEY" \
      APP_KEYCLOAK_URL="$KEYCLOAK_URL" \
      APP_KEYCLOAK_REALM="$KEYCLOAK_REALM" \
      APP_KEYCLOAK_CLIENT_ID="$ADMIN_CLIENT_ID" \
      APP_LLM_GATEWAY_URL="$gateway_url" \
      APP_GATEWAY_API_KEY="$GATEWAY_API_KEY" \
      QUOTA_E2E_ADMIN_PORT="$admin_port" \
      UV_CACHE_DIR="$UV_CACHE_DIR" \
      uv run --frozen python ../scripts/quota-test-admin-app.py
  ) >"$TMP_DIR/admin.log" 2>&1 &
  ADMIN_PID=$!

  wait_for_url "$gateway_url/health" "deterministic gateway" "$GATEWAY_PID"
  wait_for_url "$backend_url/health" "quota backend" "$BACKEND_PID"
  wait_for_url "$admin_url/health" "quota admin backend" "$ADMIN_PID"

  local token admin_token member_admin_token
  token=$(keycloak_token "$KEYCLOAK_CLIENT_ID" "$MEMBER_USER" "$MEMBER_PASSWORD")
  admin_token=$(keycloak_token "$ADMIN_CLIENT_ID" "$ADMIN_USER" "$ADMIN_PASSWORD")
  member_admin_token=$(keycloak_token "$ADMIN_CLIENT_ID" "$MEMBER_USER" "$MEMBER_PASSWORD")
  [[ -n "$token" ]] || fail "Keycloak returned an empty access token"
  [[ -n "$admin_token" && -n "$member_admin_token" ]] \
    || fail "Keycloak returned an empty admin-plane access token"

  local user_id org_id
  read -r user_id org_id < <(python3 -c '
import base64,json,sys,uuid
payload=sys.stdin.read().strip().split(".")[1]
payload += "=" * (-len(payload) % 4)
claims=json.loads(base64.urlsafe_b64decode(payload))
user_id=str(uuid.UUID(claims["sub"]))
org_id=str(uuid.UUID(claims["org_id"]))
print(user_id, org_id)
' <<<"$token")
  [[ -n "$user_id" && -n "$org_id" ]] || fail "token is missing quota identities"

  local usage_file="$TMP_DIR/usage.json" status period_end
  status=$(curl -sS -o "$usage_file" -w "%{http_code}" \
    -H "Authorization: Bearer $token" "$backend_url/v1/usage/me")
  assert_status "$status" 200 "initial usage"
  period_end=$(assert_usage "$usage_file" 0 0 10000 1000000000)
  log "Initial usage is zero and first access provisioned the organization"

  set_limits "$org_id" 10 20
  grant_model_access "$org_id" "$MODEL_ID"

  local conversation_file="$TMP_DIR/conversation.json" conversation_id
  status=$(curl -sS -o "$conversation_file" -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "{\"title\":\"Quota E2E\",\"model\":\"$MODEL_ID\"}" \
    "$backend_url/v1/conversations")
  assert_status "$status" 200 "conversation creation"
  conversation_id=$(python3 -c 'import json,sys,uuid; print(uuid.UUID(json.load(open(sys.argv[1]))["data"]["id"]))' "$conversation_file")

  local session_token
  session_token=$(
    cd "$BACKEND_DIR"
    env APP_DEBUG=true \
      APP_DATABASE_URL="$app_database_url" \
      APP_SECRET_KEY="$APP_SECRET_KEY" \
      QUOTA_E2E_CONVERSATION_ID="$conversation_id" \
      QUOTA_E2E_USER_ID="$user_id" \
      QUOTA_E2E_ORG_ID="$org_id" \
      UV_CACHE_DIR="$UV_CACHE_DIR" \
      uv run --frozen python -c 'import os; from src.services.session_token import session_token_service; print(session_token_service.issue(os.environ["QUOTA_E2E_CONVERSATION_ID"], os.environ["QUOTA_E2E_USER_ID"], os.environ["QUOTA_E2E_ORG_ID"]))'
  )

  local stream_file="$TMP_DIR/stream.txt"
  status=$(curl -sS -N -o "$stream_file" -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $session_token" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"quota-e2e-upstream-error\"}],\"stream\":true}" \
    "$backend_url/v1/chat/completions")
  assert_status "$status" 200 "proxied upstream failure stream"
  grep -q 'deterministic upstream failure' "$stream_file" \
    || fail "upstream failure was not exposed in the SSE error frame"
  status=$(curl -sS -o "$usage_file" -w "%{http_code}" \
    -H "Authorization: Bearer $token" "$backend_url/v1/usage/me")
  assert_status "$status" 200 "usage after failed gateway call"
  assert_usage "$usage_file" 0 0 10 20 >/dev/null
  log "Rejected upstream request refunded its reservation"

  for call in 1 2; do
    status=$(curl -sS -N -o "$stream_file" -w "%{http_code}" -X POST \
      -H "Authorization: Bearer $session_token" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"$MODEL_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"successful quota call $call\"}],\"stream\":true}" \
      "$backend_url/v1/chat/completions")
    assert_status "$status" 200 "successful proxy call $call"
    assert_success_stream "$stream_file"
  done
  status=$(curl -sS -o "$usage_file" -w "%{http_code}" \
    -H "Authorization: Bearer $token" "$backend_url/v1/usage/me")
  assert_status "$status" 200 "usage after two successful calls"
  period_end=$(assert_usage "$usage_file" 2 24 10 20)
  log "Two successful calls accounted exactly 2 requests and 24 tokens"

  local error_file="$TMP_DIR/error.json"
  status=$(curl -sS -o "$error_file" -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $session_token" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"must be rejected\"}],\"stream\":true}" \
    "$backend_url/v1/chat/completions")
  assert_status "$status" 429 "token quota enforcement"
  assert_quota_error "$error_file" tokens 24 20 "$period_end"
  log "Token quota allows boundary crossing and rejects the next request"

  set_limits "$org_id" 2 null
  local state_file="$TMP_DIR/state.json" before_messages after_messages
  status=$(curl -sS -o "$state_file" -w "%{http_code}" \
    -H "Authorization: Bearer $token" "$backend_url/v1/conversations/$conversation_id")
  assert_status "$status" 200 "conversation state before request rejection"
  before_messages=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["data"]["messages"]["data"]))' "$state_file")

  status=$(curl -sS -o "$error_file" -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "{\"conversation_id\":\"$conversation_id\",\"messages\":[{\"role\":\"user\",\"content\":\"must not persist\"}],\"model\":\"$MODEL_ID\"}" \
    "$backend_url/v1/chat/stream")
  assert_status "$status" 429 "public request quota enforcement"
  assert_quota_error "$error_file" requests 2 2 "$period_end"

  status=$(curl -sS -o "$state_file" -w "%{http_code}" \
    -H "Authorization: Bearer $token" "$backend_url/v1/conversations/$conversation_id")
  assert_status "$status" 200 "conversation state after request rejection"
  after_messages=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["data"]["messages"]["data"]))' "$state_file")
  [[ "$after_messages" == "$before_messages" ]] \
    || fail "early quota rejection persisted a message ($before_messages -> $after_messages)"
  log "Public request limit rejects before message persistence or runner access"

  set_limits "$org_id" 10 100
  status=$(curl -sS -N -o "$stream_file" -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $session_token" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"quota-e2e-no-usage\"}],\"stream\":true}" \
    "$backend_url/v1/chat/completions")
  assert_status "$status" 200 "accepted request without provider usage"
  assert_unaccounted_stream "$stream_file"
  status=$(curl -sS -o "$usage_file" -w "%{http_code}" \
    -H "Authorization: Bearer $token" "$backend_url/v1/usage/me")
  assert_status "$status" 200 "usage after accepted request without usage"
  assert_usage "$usage_file" 3 24 10 100 >/dev/null
  log "Accepted request without usage stays counted with tokens unaccounted"

  local foreign_org_id foreign_user_id foreign_event_id foreign_request_id
  foreign_org_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
  foreign_user_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
  foreign_event_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
  foreign_request_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
  docker exec "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d aurum_ai \
    -c "INSERT INTO organizations (id, name, slug, quota_requests, quota_tokens)
        VALUES ('$foreign_org_id'::uuid, 'Foreign analytics E2E',
                'foreign-analytics-$foreign_org_id', 10000, 1000000000);
        INSERT INTO users (id, keycloak_id, email, full_name, org_id, role, status)
        VALUES ('$foreign_user_id'::uuid, '$foreign_user_id'::uuid,
                'foreign-analytics@example.invalid', 'Foreign Analytics',
                '$foreign_org_id'::uuid, 'member', 'active');
        INSERT INTO llm_usage_events
          (id, org_id, user_id, request_id, period_start, model, status,
           prompt_tokens, completion_tokens, total_tokens, accepted_at, completed_at)
        VALUES
          ('$foreign_event_id'::uuid, '$foreign_org_id'::uuid,
           '$foreign_user_id'::uuid, '$foreign_request_id'::uuid,
           (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date,
           'foreign-analytics-marker', 'completed', 500, 500, 1000,
           CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)" >/dev/null

  local analytics_file="$TMP_DIR/admin-analytics.json"
  local models_file="$TMP_DIR/admin-models.json"
  local users_file="$TMP_DIR/admin-users.json"
  local settings_file="$TMP_DIR/settings.json"
  local period_from period_to
  period_from=$(date -u +%Y-%m-01)
  period_to=$(date -u +%F)

  status=$(curl -sS -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $member_admin_token" \
    "$admin_url/v1/admin/analytics/usage?from=$period_from&to=$period_to")
  assert_status "$status" 403 "member admin analytics denial"
  status=$(curl -sS -o /dev/null -w "%{http_code}" \
    "$admin_url/v1/admin/analytics/usage?from=$period_from&to=$period_to")
  assert_status "$status" 401 "anonymous admin analytics denial"
  status=$(curl -sS -o "$analytics_file" -w "%{http_code}" \
    -H "Authorization: Bearer $admin_token" \
    "$admin_url/v1/admin/analytics/usage?from=$period_from&to=$period_to")
  assert_status "$status" 200 "admin analytics report"
  python3 -c '
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))["data"]
assert d["period"] == {"from": sys.argv[2], "to": sys.argv[3], "timezone": "UTC"}
assert d["limits"] == {"requestLimit": 10, "tokenLimit": 100}
# Read off organization_usage_monthly — the same counter the quota gate
# itself enforces, and the same 3/24 /v1/usage/me reported above. Remaining
# is never arithmetic over the report period: the limits are per month.
assert d["quota"] == {
    "periodStart": sys.argv[2], "requestsUsed": 3, "tokensUsed": 24,
    "requestsRemaining": 7, "tokensRemaining": 76,
}
assert d["totals"] == {
    "requests": 3, "promptTokens": 14, "completionTokens": 10,
    "totalTokens": 24, "unaccountedRequests": 1,
}
assert "foreign-analytics-marker" not in json.dumps(d)
model=next(row for row in d["byModel"] if row["model"] == sys.argv[4])
assert (model["requests"], model["totalTokens"], model["unaccountedRequests"]) == (3, 24, 1)
# byUser is capped server-side; under the cap the count matches the rows.
assert d["byUserTotal"] == len(d["byUser"])
user=next(row for row in d["byUser"] if row["userId"] == sys.argv[5])
assert (user["requests"], user["totalTokens"], user["unaccountedRequests"]) == (3, 24, 1)
' "$analytics_file" "$period_from" "$period_to" "$MODEL_ID" "$user_id"
  log "Admin analytics reports exact completed/unaccounted totals and excludes foreign org"

  status=$(curl -sS -o "$models_file" -w "%{http_code}" \
    -H "Authorization: Bearer $admin_token" "$admin_url/v1/admin/models")
  assert_status "$status" 200 "admin model catalog"
  python3 -c '
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))["data"]
assert d["gatewayStatus"] == "available"
model=next(row for row in d["models"] if row["id"] == sys.argv[2])
assert model["organizationReadable"] is True
' "$models_file" "$MODEL_ID"
  status=$(curl -sS -o "$models_file" -w "%{http_code}" -X PUT \
    -H "Authorization: Bearer $admin_token" \
    -H "Content-Type: application/json" \
    -d "{\"defaultModel\":\"$MODEL_ID\",\"defaultTemperature\":null}" \
    "$admin_url/v1/admin/settings/models")
  assert_status "$status" 200 "set organization default model"
  status=$(curl -sS -o "$settings_file" -w "%{http_code}" \
    -H "Authorization: Bearer $token" "$backend_url/v1/settings/me")
  assert_status "$status" 200 "business settings after organization default update"
  python3 -c '
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))["data"]
assert d["organization_default_model"] == sys.argv[2], d
' "$settings_file" "$MODEL_ID"

  status=$(curl -sS -o "$models_file" -w "%{http_code}" -X PUT \
    -H "Authorization: Bearer $admin_token" \
    -H "Content-Type: application/json" \
    -d '{"defaultModel":null,"defaultTemperature":null}' \
    "$admin_url/v1/admin/settings/models")
  assert_status "$status" 200 "clear organization default model"
  status=$(curl -sS -o "$settings_file" -w "%{http_code}" \
    -H "Authorization: Bearer $token" "$backend_url/v1/settings/me")
  assert_status "$status" 200 "business settings after organization default clear"
  python3 -c 'import json,sys; assert json.load(open(sys.argv[1], encoding="utf-8"))["data"]["organization_default_model"] is None' "$settings_file"

  status=$(curl -sS -o "$users_file" -w "%{http_code}" \
    -H "Authorization: Bearer $admin_token" "$admin_url/v1/admin/users?limit=50&offset=0")
  assert_status "$status" 200 "admin user directory"
  python3 -c '
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
assert any(row["id"] == sys.argv[2] for row in d["data"]), d
assert d["pagination"]["total"] >= 2, d
' "$users_file" "$user_id"
  log "Admin model setting propagates immediately and user directory is org-scoped"

  run_browser_acceptance "$user_id" "$period_from" "$period_to"

  log "All quota accounting and organization analytics E2E scenarios passed"
}

main "$@"
