#!/usr/bin/env bash
set -euo pipefail

# End-to-end smoke test for the code-skill feature (see project plan):
# uploads "code" skill bundles through the real POST /v1/skills/code
# endpoint, then drives real chat turns through Keycloak + backend +
# conversation-runner + tool-worker + skill-executor and asserts the
# commands actually ran inside a real ephemeral sandbox container and their
# output made it back to the client.
#
# Two scenarios:
#   1. A trivial one-command "greet" skill — proves the basic container/
#      session/CodeSkillSessionWorkflow plumbing works end to end.
#   2. A "multistep" skill whose SKILL.md requires two *sequential* shell
#      commands, where the second command's argument depends on the first
#      command's stdout — this can only pass if the model actually sees the
#      first command's real output before choosing the second command
#      (Stage 3's whole point: a live, multi-round sandbox session, not a
#      single fire-and-forget command).
#
# Deliberately uses trivial skills, not the full Anthropic pdf-skill — this
# proves the container/session/multi-round plumbing works; testing whether a
# model correctly follows pdf-skill-specific instructions is a separate,
# much flakier concern.
#
# Requires: docker compose stack up, plus
#   docker compose --profile skills-exec up -d skill-executor
#   docker compose --profile build build skill-sandbox   # once, or after any skill-sandbox change

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8000}"
# Skill upload/delete now live on the admin plane (see
# docs/ARCHITECTURE.md) — a separate backend container,
# separate Keycloak client audience.
ADMIN_BASE_URL="${ADMIN_BASE_URL:-http://localhost:8001}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
REALM="${KEYCLOAK_REALM:-aurum-ai}"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:-business-ui}"
ADMIN_CLIENT_ID="${ADMIN_CLIENT_ID:-${ADMIN_KEYCLOAK_CLIENT_ID:-admin-ui}}"
SKILL_EXECUTOR_HEALTH_URL="${SKILL_EXECUTOR_HEALTH_URL:-}"
# Uploading a code skill uses an admin identity with the create capability — unlike the MCP smoke test,
# there's no way around this via direct SQL, since the bundle bytes also
# need to actually land in MinIO through the real upload pipeline.
USERNAME="${TEST_ADMIN_USER:-admin}"
PASSWORD="${TEST_ADMIN_PASSWORD:-admin}"
MODEL="${CODE_SKILL_TEST_MODEL:-${TEST_GATEWAY_MODEL_ID:-gpt-4o-mini}}"
GREET_SKILL_ID=""
MULTISTEP_SKILL_ID=""
BUNDLE_DIR=""

log() { echo "[code-skill-smoke] $*" >&2; }

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

wait_for_skill_executor() {
  local timeout="${1:-60}"
  if [[ -n "$SKILL_EXECUTOR_HEALTH_URL" ]]; then
    wait_for_url "$SKILL_EXECUTOR_HEALTH_URL" "skill-executor" "$timeout"
    return
  fi
  log "Waiting for skill-executor..."
  for _ in $(seq 1 "$timeout"); do
    if docker exec aurum-ai-skill-executor python3 -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8090/health', timeout=2)" \
      >/dev/null 2>&1; then
      log "skill-executor is reachable"
      return 0
    fi
    sleep 1
  done
  log "ERROR: skill-executor not reachable. Start it with:"
  log "  docker compose --profile skills-exec up -d skill-executor"
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

# Skill upload/delete need an admin-ui-audience token (backend-admin's
# decode_token checks aud=admin-ui) — a business-ui token gets a plain 401
# there even for the admin user.
get_admin_token() {
  keycloak_token "$ADMIN_CLIENT_ID" "$USERNAME" "$PASSWORD"
}

build_greet_bundle() {
  local dir="$BUNDLE_DIR/greet-src"
  mkdir -p "$dir/scripts"
  cat > "$dir/SKILL.md" <<'EOF'
---
name: smoke-test-greet
description: Greets a person by name. To use, run "python scripts/greet.py \"<name>\"".
---

# Greet skill

Run `python scripts/greet.py "<name>"` to print a greeting for the given name.

Your entire reply must be the raw stdout the script printed, byte for byte,
and absolutely nothing else: no introductory sentence, no closing remark, no
markdown code fences, no quotation marks. Example: if stdout is `Hello,
World!`, your whole reply is exactly `Hello, World!` — nothing before it,
nothing after it.
EOF
  cat > "$dir/scripts/greet.py" <<'EOF'
import sys
name = sys.argv[1] if len(sys.argv) > 1 else "world"
print(f"Hello, {name}!")
EOF
  python3 -c "
import zipfile, os
base = '$dir'
with zipfile.ZipFile('$BUNDLE_DIR/greet.zip', 'w') as zf:
    for root, dirs, files in os.walk(base):
        for f in files:
            full = os.path.join(root, f)
            zf.write(full, os.path.relpath(full, base))
"
}

build_multistep_bundle() {
  local dir="$BUNDLE_DIR/multistep-src"
  mkdir -p "$dir"
  cat > "$dir/SKILL.md" <<'EOF'
---
name: smoke-test-multistep
description: Computes a two-step result using two sequential shell commands, where the second command's input depends on the first command's output. To use, ask it to compute the two-step result.
---

# Multi-step skill

You must run exactly two commands, one after the other, and read the output
of each before deciding the next command:

1. Run: `python3 -c "print(6 * 7)"` — this prints a number N.
2. Run a second, different command: `python3 -c "print(N + 100)"`, replacing
   `N` with the exact number printed by step 1 (do not guess it, use the
   real printed value).

Do not skip a step. Report the final printed number as your answer, in
exactly this form: `FINAL RESULT: <number>`.
EOF
  python3 -c "
import zipfile, os
base = '$dir'
with zipfile.ZipFile('$BUNDLE_DIR/multistep.zip', 'w') as zf:
    for root, dirs, files in os.walk(base):
        for f in files:
            full = os.path.join(root, f)
            zf.write(full, os.path.relpath(full, base))
"
}

upload_skill() {
  local token="$1" name="$2" zip_path="$3"
  curl -sf -X POST "$ADMIN_BASE_URL/v1/skills/code" \
    -H "Authorization: Bearer $token" \
    -F "name=$name" \
    -F "bundle=@$zip_path;type=application/zip" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])"
}

cleanup() {
  local token
  token=$(get_admin_token 2>/dev/null || true)
  if [[ -n "$token" ]]; then
    for id in "$GREET_SKILL_ID" "$MULTISTEP_SKILL_ID"; do
      if [[ -n "$id" ]]; then
        curl -s -X DELETE "$ADMIN_BASE_URL/v1/skills/$id" \
          -H "Authorization: Bearer $token" >/dev/null 2>&1 || true
      fi
    done
  fi
  [[ -n "$BUNDLE_DIR" ]] && rm -rf "$BUNDLE_DIR"
}

create_conversation() {
  local token="$1" title="$2"
  curl -sf -X POST "$BASE_URL/v1/conversations" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "{\"title\":\"$title\",\"model\":\"$MODEL\"}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])"
}

# Runs a chat turn and returns the raw SSE body path via stdout; caller
# checks markers/counts against it. Fails the whole test on a non-200.
run_turn() {
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

  echo "$tmpfile"
}

# Asserts every marker string appears at least once in the given file.
assert_contains() {
  local tmpfile="$1"
  shift
  local markers=("$@")
  local missing=0
  for marker in "${markers[@]}"; do
    if ! grep -q -- "$marker" "$tmpfile"; then
      log "ERROR: expected marker '$marker' not found in SSE stream"
      missing=1
    fi
  done
  if [[ "$missing" -ne 0 ]]; then
    log "--- stream body ---"
    cat "$tmpfile" >&2
    return 1
  fi
  return 0
}

# Asserts a marker string appears at least $2 times (line-count based, so
# each occurrence must be a separate SSE line — true for our "event: X" markers).
assert_min_count() {
  local tmpfile="$1" marker="$2" min_count="$3"
  local actual
  actual=$(grep -c -- "$marker" "$tmpfile" || true)
  if [[ "$actual" -lt "$min_count" ]]; then
    log "ERROR: expected marker '$marker' at least $min_count times, found $actual"
    log "--- stream body ---"
    cat "$tmpfile" >&2
    return 1
  fi
  log "marker '$marker' seen $actual times (>= $min_count)"
  return 0
}

assert_semantic_success() {
  local tmpfile="$1" scenario="$2"
  if ! python3 "$SCRIPT_DIR/scripts/validate-smoke-sse.py" "$scenario" "$tmpfile"; then
    log "--- stream body ---"
    cat "$tmpfile" >&2
    return 1
  fi
}

main() {
  log "Starting code-skill end-to-end smoke test..."

  wait_for_url "$BASE_URL/health" "backend"
  wait_for_url "$ADMIN_BASE_URL/health" "backend-admin"
  wait_for_url "$KEYCLOAK_URL/realms/$REALM" "Keycloak realm"
  wait_for_skill_executor

  local token admin_token
  token=$(get_token)
  admin_token=$(get_admin_token)
  log "Keycloak tokens acquired (business-ui + admin-ui audiences)"

  BUNDLE_DIR=$(mktemp -d)
  trap cleanup EXIT

  build_greet_bundle
  GREET_SKILL_ID=$(upload_skill "$admin_token" "smoke-test-greet" "$BUNDLE_DIR/greet.zip")
  log "Uploaded smoke-test-greet skill: $GREET_SKILL_ID"

  build_multistep_bundle
  MULTISTEP_SKILL_ID=$(upload_skill "$admin_token" "smoke-test-multistep" "$BUNDLE_DIR/multistep.zip")
  log "Uploaded smoke-test-multistep skill: $MULTISTEP_SKILL_ID"

  local conv_id stream

  conv_id=$(create_conversation "$token" "code-skill smoke test: greet")
  log "Created conversation $conv_id"
  stream=$(run_turn "$token" "$conv_id" \
    "Call the smoke-test-greet skill (skill_id: $GREET_SKILL_ID) with args {\\\"task\\\": \\\"Greet someone named World\\\"} and show me the exact greeting it printed." \
    90)
  assert_contains "$stream" "event: tool-calls" "event: tool-result" "event: run-command" "event: run-result"
  assert_semantic_success "$stream" "code-greet"
  rm -f "$stream"
  log "single-round code-skill (skill-executor) round trip OK"

  conv_id=$(create_conversation "$token" "code-skill smoke test: multistep")
  log "Created conversation $conv_id"
  stream=$(run_turn "$token" "$conv_id" \
    "Call the smoke-test-multistep skill (skill_id: $MULTISTEP_SKILL_ID) with args {\\\"task\\\": \\\"compute the two-step result\\\"} and tell me the FINAL RESULT number." \
    120)
  assert_contains "$stream" "event: tool-calls" "event: tool-result"
  assert_min_count "$stream" "event: run-command" 2
  assert_min_count "$stream" "event: run-result" 2
  assert_semantic_success "$stream" "code-multistep"
  rm -f "$stream"
  log "multi-round code-skill sandbox session (>=2 rounds) OK"

  log "All code-skill smoke tests passed!"
}

main "$@"
