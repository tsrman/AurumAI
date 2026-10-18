#!/usr/bin/env bash
set -euo pipefail

LANGFUSE_URL="${LANGFUSE_URL:-http://localhost:3001}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
REALM="${KEYCLOAK_REALM:-aurum-ai}"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:-business-ui}"
USERNAME="${TEST_USER:-admin}"
PASSWORD="${TEST_PASSWORD:-admin}"
LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-pk-lf-dev-public-key}"
LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-sk-lf-550e8400-e29b-41d4-a716-446655440001}"

log() { echo "[langfuse-smoke] $*" >&2; }

wait_for_url() {
  local url="$1" name="$2" timeout="${3:-120}"
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

get_token() {
  curl -sf -X POST \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=password" -d "client_id=$CLIENT_ID" \
    -d "username=$USERNAME" -d "password=$PASSWORD" \
    "$KEYCLOAK_URL/realms/$REALM/protocol/openid-connect/token" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
}

main() {
  wait_for_url "$LANGFUSE_URL/api/public/health" "langfuse"
  TOKEN=$(get_token)
  CONV=$(curl -sf -X POST "$BASE_URL/v1/conversations" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"title":"Langfuse smoke","model":"gpt-4o-mini"}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])")
  log "Created conversation $CONV"

  curl -sS -N -X POST "$BASE_URL/v1/chat/stream" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"conversation_id\":\"$CONV\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"model\":\"gpt-4o-mini\"}" \
    >/dev/null

  log "Waiting for Langfuse trace..."
  for _ in $(seq 1 30); do
    count=$(docker exec aurum-ai-backend python -c \
      "from src.core.langfuse_client import get_langfuse; lf=get_langfuse(); print(len(lf.fetch_traces(limit=10).data))" \
      2>/dev/null || echo 0)
    if [[ "$count" -gt 0 ]]; then
      log "Found $count trace(s) in Langfuse"
      log "Langfuse smoke test passed"
      return 0
    fi
    sleep 2
  done
  log "ERROR: no traces found in Langfuse"
  return 1
}

main "$@"
