#!/usr/bin/env bash
set -euo pipefail

# End-to-end smoke test for the MCP skill integration: registers two skills
# backed by the reference test MCP servers (docker compose --profile mcp),
# then drives a real chat turn through Keycloak + backend + conversation
# runner + tool-worker and asserts the LLM actually calls the tool and the
# MCP round trip completes.
#
# Requires: docker compose stack up, plus
#   docker compose --profile mcp up -d mcp-everything mcp-fetch

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8000}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-http://localhost:8001}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
MCP_EVERYTHING_HOST="${MCP_EVERYTHING_HOST:-localhost}"
MCP_EVERYTHING_PORT="${MCP_EVERYTHING_PORT:-3101}"
MCP_FETCH_HOST="${MCP_FETCH_HOST:-localhost}"
MCP_FETCH_PORT="${MCP_FETCH_PORT:-3102}"
MCP_FETCH_TARGET_URL="${MCP_FETCH_TARGET_URL:-http://backend:8000/openapi.json}"
MCP_FETCH_EXPECTED_TITLE="${MCP_FETCH_EXPECTED_TITLE:-AurumAI Backend}"
MCP_FETCH_FINAL_MARKER="FETCH FIXTURE VERIFIED: $MCP_FETCH_EXPECTED_TITLE"
REALM="${KEYCLOAK_REALM:-aurum-ai}"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:-business-ui}"
ADMIN_CLIENT_ID="${ADMIN_CLIENT_ID:-${ADMIN_KEYCLOAK_CLIENT_ID:-admin-ui}}"
USERNAME="${TEST_USER:-admin}"
PASSWORD="${TEST_PASSWORD:-admin}"
ADMIN_USERNAME="${TEST_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${TEST_ADMIN_PASSWORD:-admin}"
MCP_SETUP_MODE="${MCP_SETUP_MODE:-docker-psql}"
MCP_EVERYTHING_ENDPOINT="${MCP_EVERYTHING_ENDPOINT:-http://mcp-everything:3001/sse}"
MCP_FETCH_ENDPOINT="${MCP_FETCH_ENDPOINT:-http://mcp-fetch:8080/sse}"
# deepseek-v4-flash reliably follows the <tool_call> instruction format used
# by ConversationWorkflow's skills system prompt. Small/fast models
# sometimes invent their own tool-call syntax and never
# actually call the tool — see docs/TESTING.md for provider coverage.
MODEL="${MCP_TEST_MODEL:-${TEST_GATEWAY_MODEL_ID:-gpt-4o-mini}}"
ORG_ID=""
ADMIN_TOKEN=""
MCP_SUM_SKILL_ID=""
MCP_FETCH_SKILL_ID=""

log() { echo "[mcp-smoke] $*" >&2; }

wait_for_url() {
  local url="$1" name="$2" timeout="${3:-60}"
  log "Waiting for $name at $url..."
  for _ in $(seq 1 "$timeout"); do
    if curl -sf "$url" >/dev/null 2>&1; then
      log "$name is reachable"
      return 0
    fi
    sleep 1
  done
  log "ERROR: $name not reachable"
  return 1
}

wait_for_port() {
  local host="$1" port="$2" name="$3" timeout="${4:-60}"
  log "Waiting for $name at $host:$port..."
  for _ in $(seq 1 "$timeout"); do
    # Any HTTP response (even 404) means the port is accepting connections.
    # --ipv4 avoids a slow/hanging ::1 resolution on some hosts (WSL2).
    if curl -s --ipv4 --max-time 2 -o /dev/null "http://$host:$port/"; then
      log "$name is reachable"
      return 0
    fi
    sleep 1
  done
  log "ERROR: $name ($host:$port) not reachable. Start it with:"
  log "  docker compose --profile mcp up -d mcp-everything mcp-fetch"
  return 1
}

# Keycloak can answer HTTP 500 for a few seconds after PostgreSQL restarts.
# Retry a bounded number of times and report the status instead of piping an
# empty error body into the JSON parser.
keycloak_token() {
  local client_id="$1" username="$2" password="$3" attempt response status
  for attempt in 1 2 3 4 5; do
    response=$(curl -sS -X POST \
      -H "Content-Type: application/x-www-form-urlencoded" \
      -d "grant_type=password" -d "client_id=$client_id" \
      -d "username=$username" --data-urlencode "password=$password" \
      -w $'\n%{http_code}' \
      "$KEYCLOAK_URL/realms/$REALM/protocol/openid-connect/token") || response=$'\n000'
    status=${response##*$'\n'}
    if [[ "$status" == 200 ]]; then
      printf '%s' "${response%$'\n'*}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
      return
    fi
    log "Keycloak token request for $username/$client_id returned HTTP $status (attempt $attempt/5)"
    sleep "${TOKEN_RETRY_DELAY:-2}"
  done
  log "ERROR: could not obtain a Keycloak token for $username/$client_id"
  return 1
}

get_token() {
  keycloak_token "$CLIENT_ID" "$USERNAME" "$PASSWORD"
}

get_admin_token() {
  keycloak_token "$ADMIN_CLIENT_ID" "$ADMIN_USERNAME" "$ADMIN_PASSWORD"
}

org_id_from_token() {
  python3 -c "
import sys, json, base64
payload = sys.argv[1].split('.')[1]
payload += '=' * (-len(payload) % 4)
print(json.loads(base64.urlsafe_b64decode(payload))['org_id'])
" "$1"
}

# Skills are seeded directly in Postgres in Compose mode; explicit organization
# grants make them visible to the smoke user under the authoritative ACL model.
setup_skills_docker_psql() {
  local org_id="$1"
  log "Registering test MCP skills for org $org_id..."
  docker exec -i aurum-ai-postgres psql -U postgres -d aurum_ai -v ON_ERROR_STOP=1 >/dev/null <<SQL
DELETE FROM access_grants
WHERE org_id = '$org_id' AND resource_type = 'skill'
  AND resource_id IN (
    SELECT id::text FROM skills
    WHERE org_id = '$org_id' AND name IN ('smoke-test-sum', 'smoke-test-fetch')
  );
DELETE FROM skills WHERE org_id = '$org_id' AND name IN ('smoke-test-sum', 'smoke-test-fetch');
INSERT INTO skills (id, org_id, name, description, type, mcp_transport, mcp_endpoint, activity_name, input_schema, created_at, updated_at)
VALUES
  (gen_random_uuid(), '$org_id', 'smoke-test-sum', 'Returns the sum of two numbers', 'mcp', 'sse', 'http://mcp-everything:3001/sse', 'get-sum',
   '{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}', now(), now()),
  (gen_random_uuid(), '$org_id', 'smoke-test-fetch', 'Fetches a URL and returns its content as text', 'mcp', 'sse', 'http://mcp-fetch:8080/sse', 'fetch',
   '{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}', now(), now());
INSERT INTO access_grants
  (id, org_id, resource_type, resource_id, principal_type, principal_id,
   permission, created_via)
SELECT gen_random_uuid(), '$org_id', 'skill', id::text, 'organization', '$org_id',
       'read', 'provisioning'
FROM skills
WHERE org_id = '$org_id' AND name IN ('smoke-test-sum', 'smoke-test-fetch');
SQL
}

cleanup_skills_docker_psql() {
  local org_id="$1"
  docker exec -i aurum-ai-postgres psql -U postgres -d aurum_ai -v ON_ERROR_STOP=1 \
    -c "DELETE FROM access_grants WHERE org_id = '$org_id' AND resource_type = 'skill' AND resource_id IN (SELECT id::text FROM skills WHERE org_id = '$org_id' AND name IN ('smoke-test-sum', 'smoke-test-fetch')); DELETE FROM skills WHERE org_id = '$org_id' AND name IN ('smoke-test-sum', 'smoke-test-fetch');" \
    >/dev/null 2>&1 || true
}

create_mcp_skill_admin_api() {
  local token="$1" name="$2" description="$3" endpoint="$4" activity="$5" schema="$6"
  local payload
  payload=$(python3 -c '
import json, sys
print(json.dumps({
    "name": sys.argv[1], "description": sys.argv[2], "type": "mcp",
    "mcpTransport": "sse", "mcpEndpoint": sys.argv[3],
    "activityName": sys.argv[4], "inputSchema": json.loads(sys.argv[5]),
}))
' "$name" "$description" "$endpoint" "$activity" "$schema")
  curl -sf -X POST "$ADMIN_BASE_URL/v1/skills/" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "$payload" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])"
}

setup_skills_admin_api() {
  log "Registering test MCP skills through backend-admin..."
  ADMIN_TOKEN=$(get_admin_token)
  MCP_SUM_SKILL_ID=$(create_mcp_skill_admin_api "$ADMIN_TOKEN" \
    "smoke-test-sum" "Returns the sum of two numbers" \
    "$MCP_EVERYTHING_ENDPOINT" "get-sum" \
    '{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}')
  MCP_FETCH_SKILL_ID=$(create_mcp_skill_admin_api "$ADMIN_TOKEN" \
    "smoke-test-fetch" "Fetches a URL and returns its content as text" \
    "$MCP_FETCH_ENDPOINT" "fetch" \
    '{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}')
  local skill_id
  for skill_id in "$MCP_SUM_SKILL_ID" "$MCP_FETCH_SKILL_ID"; do
    curl -sf -X PUT "$ADMIN_BASE_URL/v1/admin/authorization/grants/reconcile" \
      -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
      -d "{\"resourceType\":\"skill\",\"resourceId\":\"$skill_id\",\"grants\":[{\"principalType\":\"organization\",\"principalId\":\"$ORG_ID\",\"permission\":\"read\"}]}" \
      >/dev/null
  done
}

cleanup_skills_admin_api() {
  local token="$ADMIN_TOKEN" id
  if [[ -z "$token" ]]; then
    token=$(get_admin_token 2>/dev/null || true)
  fi
  if [[ -z "$token" ]]; then
    return 0
  fi
  for id in "$MCP_SUM_SKILL_ID" "$MCP_FETCH_SKILL_ID"; do
    if [[ -n "$id" ]]; then
      curl -s -X DELETE "$ADMIN_BASE_URL/v1/skills/$id" \
        -H "Authorization: Bearer $token" >/dev/null 2>&1 || true
    fi
  done
}

setup_skills() {
  case "$MCP_SETUP_MODE" in
    docker-psql)
      setup_skills_docker_psql "$ORG_ID"
      ;;
    admin-api)
      setup_skills_admin_api
      ;;
    *)
      log "ERROR: MCP_SETUP_MODE must be docker-psql or admin-api (got: $MCP_SETUP_MODE)"
      return 2
      ;;
  esac
}

cleanup_skills() {
  case "$MCP_SETUP_MODE" in
    docker-psql) cleanup_skills_docker_psql "$ORG_ID" ;;
    admin-api) cleanup_skills_admin_api ;;
  esac
}

create_conversation() {
  local token="$1" title="$2"
  curl -sf -X POST "$BASE_URL/v1/conversations" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "{\"title\":\"$title\",\"model\":\"$MODEL\"}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])"
}

# Sends one chat message and asserts every marker string appears somewhere
# in the raw SSE response body.
assert_stream_contains() {
  local token="$1" conv_id="$2" content="$3" timeout="$4"
  shift 4
  local markers=("$@")
  local tmpfile http_code
  tmpfile=$(mktemp)
  http_code=$(curl -sS -N -o "$tmpfile" -w "%{http_code}" --max-time "$timeout" \
    -X POST "$BASE_URL/v1/chat/stream" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "{\"conversation_id\":\"$conv_id\",\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$content\"}]}" || true)

  if [[ "$http_code" != "200" ]]; then
    log "ERROR: expected HTTP 200 from /v1/chat/stream, got $http_code"
    cat "$tmpfile" >&2
    rm -f "$tmpfile"
    return 1
  fi

  local missing=0
  for marker in "${markers[@]}"; do
    if ! grep -q -- "$marker" "$tmpfile"; then
      log "ERROR: expected marker '$marker' not found in SSE stream"
      missing=1
    fi
  done
  if [[ "$missing" -ne 0 ]]; then
    log "--- stream body ($conv_id) ---"
    cat "$tmpfile" >&2
    rm -f "$tmpfile"
    return 1
  fi

  rm -f "$tmpfile"
  return 0
}

assert_fetch_stream_success() {
  local token="$1" conv_id="$2" content="$3" timeout="$4"
  local tmpfile http_code
  tmpfile=$(mktemp)
  http_code=$(curl -sS -N -o "$tmpfile" -w "%{http_code}" --max-time "$timeout" \
    -X POST "$BASE_URL/v1/chat/stream" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "{\"conversation_id\":\"$conv_id\",\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$content\"}]}" || true)

  if [[ "$http_code" != "200" ]]; then
    log "ERROR: expected HTTP 200 from /v1/chat/stream, got $http_code"
    cat "$tmpfile" >&2
    rm -f "$tmpfile"
    return 1
  fi

  if ! python3 "$SCRIPT_DIR/scripts/validate-smoke-sse.py" mcp-fetch "$tmpfile" \
    --expected-title "$MCP_FETCH_EXPECTED_TITLE"; then
    log "--- stream body ($conv_id) ---"
    cat "$tmpfile" >&2
    rm -f "$tmpfile"
    return 1
  fi

  rm -f "$tmpfile"
  return 0
}

main() {
  log "Starting MCP end-to-end smoke test..."

  wait_for_url "$BASE_URL/health" "backend"
  if [[ "$MCP_SETUP_MODE" == "admin-api" ]]; then
    wait_for_url "$ADMIN_BASE_URL/health" "backend-admin"
  fi
  wait_for_url "$KEYCLOAK_URL/realms/$REALM" "Keycloak realm"
  wait_for_port "$MCP_EVERYTHING_HOST" "$MCP_EVERYTHING_PORT" "mcp-everything"
  wait_for_port "$MCP_FETCH_HOST" "$MCP_FETCH_PORT" "mcp-fetch"

  local token
  token=$(get_token)
  log "Keycloak token acquired"
  # Global (not `local`) so the EXIT trap below can still see it after
  # main() returns.
  ORG_ID=$(org_id_from_token "$token")
  log "Resolved org_id=$ORG_ID"

  trap cleanup_skills EXIT
  setup_skills

  local conv_sum
  conv_sum=$(create_conversation "$token" "MCP smoke test: sum")
  log "Created conversation $conv_sum"
  assert_stream_contains "$token" "$conv_sum" \
    "What is 17 plus 25? Use the available tool to compute it, then answer with just the number." \
    90 "event: tool-calls" "event: tool-result" "42"
  log "mcp-everything (get-sum) round trip OK"

  # Deliberately an in-stack URL, not a public one (this used to fetch
  # https://example.com): what's being tested is the mcp-fetch round trip,
  # not internet reachability, and a public target makes the test fail for
  # reasons that have nothing to do with the platform — restricted-egress
  # environments block example.com/.org/.net outright, and even where they
  # don't, remote content drifts. mcp-fetch sits on `default`, so it reaches
  # backend directly (see the SSRF note on the service in docker-compose.yml).
  # backend serves no robots.txt (404 => allowed), unlike keycloak, whose
  # robots.txt is `Disallow: /` and would make mcp-fetch refuse.
  # The marker is the FastAPI app title from the fetched body, not anything
  # in the URL itself — a URL substring would match the model's own
  # <tool_call> echo even if the fetch never succeeded.
  local conv_fetch
  conv_fetch=$(create_conversation "$token" "MCP smoke test: fetch")
  log "Created conversation $conv_fetch"
  assert_fetch_stream_success "$token" "$conv_fetch" \
    "Use the available tool to fetch $MCP_FETCH_TARGET_URL. After a successful tool result, answer exactly: $MCP_FETCH_FINAL_MARKER" \
    90
  log "mcp-fetch (fetch) round trip OK"

  log "All MCP smoke tests passed!"
}

main "$@"
