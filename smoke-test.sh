#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
FRONTEND_URL="${FRONTEND_URL:-http://localhost:3000}"
TEMPORAL_UI_URL="${TEMPORAL_UI_URL:-http://localhost:8082}"
REALM="${KEYCLOAK_REALM:-aurum-ai}"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:-business-ui}"
USERNAME="${TEST_USER:-admin}"
PASSWORD="${TEST_PASSWORD:-admin}"

log() { echo "[smoke-test] $*" >&2; }

wait_for_url() {
  local url="$1"
  local name="$2"
  local timeout="${3:-120}"
  log "Waiting for $name at $url..."
  for i in $(seq 1 "$timeout"); do
    if curl -sf "$url" >/dev/null 2>&1; then
      log "$name is reachable"
      return 0
    fi
    sleep 1
  done
  log "ERROR: $name did not become reachable within ${timeout}s"
  return 1
}

wait_for_keycloak() {
  wait_for_url "$KEYCLOAK_URL/realms/$REALM" "Keycloak realm"
}

get_token() {
  log "Getting Keycloak token for user $USERNAME..."
  local response
  response=$(curl -sf \
    -X POST \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=password" \
    -d "client_id=$CLIENT_ID" \
    -d "username=$USERNAME" \
    -d "password=$PASSWORD" \
    "$KEYCLOAK_URL/realms/$REALM/protocol/openid-connect/token")
  echo "$response" | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])"
}

create_conversation() {
  local token="$1"
  log "Creating conversation..."
  local response
  response=$(curl -sf \
    -X POST \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d '{"title":"Smoke test conversation","model":"gpt-4o-mini"}' \
    "$BASE_URL/v1/conversations")
  echo "$response" | python3 -c "import sys, json; print(json.load(sys.stdin)['data']['id'])"
}

test_stream() {
  local token="$1"
  local conversation_id="$2"
  log "Sending message to /v1/chat/stream for conversation $conversation_id..."
  local tmpfile
  tmpfile=$(mktemp)
  local http_code
  http_code=$(curl -sS -N \
    -o "$tmpfile" \
    -w "%{http_code}" \
    -X POST \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "{\"conversation_id\":\"$conversation_id\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello from smoke test\"}],\"model\":\"gpt-4o-mini\"}" \
    "$BASE_URL/v1/chat/stream" || true)

  log "Stream HTTP status: $http_code"
  if [[ "$http_code" != "200" ]]; then
    log "ERROR: Expected HTTP 200, got $http_code"
    cat "$tmpfile" || true
    rm -f "$tmpfile"
    return 1
  fi

  local event_count
  event_count=$(grep -c "^event:" "$tmpfile" || true)
  log "Received $event_count SSE events"
  rm -f "$tmpfile"

  if [[ "$event_count" -lt 1 ]]; then
    log "ERROR: Expected at least one SSE event"
    return 1
  fi

  log "Stream smoke test passed"
  return 0
}

main() {
  log "Starting end-to-end smoke test..."

  wait_for_url "$BASE_URL/health" "backend"
  wait_for_url "$FRONTEND_URL" "frontend"
  wait_for_keycloak
  wait_for_url "$TEMPORAL_UI_URL" "temporal-ui"

  TOKEN=$(get_token)
  log "Keycloak token acquired"

  CONVERSATION_ID=$(create_conversation "$TOKEN")
  log "Created conversation: $CONVERSATION_ID"

  test_stream "$TOKEN" "$CONVERSATION_ID"

  log "All smoke tests passed!"
}

main "$@"
