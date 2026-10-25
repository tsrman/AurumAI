#!/usr/bin/env bash
# Hermes sandbox acceptance (phase 2c-i) on the standalone, disposable project
# also used by ./smoke-test-hermes-egress.sh, with the real
# docker-compose.hermes.yml. skill-executor creates sandboxes from this
# checkout's hermes-sandbox image; a Hermes run goes through hermes-egress to
# the real backend route and the deterministic test gateway, and is billed.
# Checked from inside and around the sandboxes: no capabilities, a non-root
# user, a read-only root filesystem, no secrets in the container's metadata,
# no route to another sandbox or the host, re-adoption after an executor
# restart, reconnection after the guard is recreated, and that closing or
# idle expiry leaves no container, network or volume behind.
#
# Prerequisites: Docker 28+, uv, free 55436/58000/58090, no other
# aurum-ai-hermes-egress-guard container. Never point this at a shared stack.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT_DIR"
PROJECT=hermes-egress-test
JOURNAL=/tmp/hermes-egress-acceptance/fixture.json
EXECUTOR=http://127.0.0.1:58090
export HERMES_EGRESS_SERVICE_SECRET=hermes-egress-acceptance-service-secret
export HERMES_EGRESS_MAX_MODE=packages HERMES_ENABLED=true SKILL_EXECUTOR_REAP_INTERVAL_SECONDS=5
STACK=(docker compose -p "$PROJECT" --env-file /dev/null --project-directory "$ROOT_DIR" -f scripts/fixtures/hermes-egress-base.yml -f docker-compose.hermes.yml --profile hermes --profile hermes-sandbox)
log() { printf '[hermes-sandbox] %s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }

if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]]; then
  echo "A $PROJECT project already exists; finish or remove it first" >&2; exit 1
fi
if docker container inspect aurum-ai-hermes-egress-guard >/dev/null 2>&1; then
  echo "An aurum-ai-hermes-egress-guard container already exists (another Hermes stack?)" >&2; exit 1
fi
if [[ -n "$(docker ps -aq --filter label=aurum-ai.hermes-sandbox=1)" ]]; then
  echo "Hermes sandboxes already exist on this daemon" >&2; exit 1
fi
for port in 55436 58000 58090; do
  if ss -ltn | grep -q ":$port "; then echo "Port $port is in use" >&2; exit 1; fi
done
if [[ -e "$JOURNAL" ]]; then echo "An earlier fixture journal exists: $JOURNAL" >&2; exit 1; fi

remove_sandboxes() {
  for name in $(docker ps -aq --filter label=aurum-ai.hermes-sandbox=1); do docker rm -f "$name" >/dev/null 2>&1 || true; done
  for name in $(docker network ls -q --filter label=aurum-ai.hermes-sandbox=1); do docker network rm "$name" >/dev/null 2>&1 || true; done
  for name in $(docker volume ls -q --filter label=aurum-ai.hermes-sandbox=1); do docker volume rm "$name" >/dev/null 2>&1 || true; done
}
teardown() {
  local status=$?
  log "removing the disposable project"
  remove_sandboxes
  "${STACK[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  remove_sandboxes
  if (( status == 0 )); then rm -f "$JOURNAL"; else log "journal kept for inspection: $JOURNAL"; fi
  exit "$status"
}
trap teardown EXIT

fixture() { (cd business-backend && uv run --frozen python ../scripts/hermes-egress-fixture.py "$@"); }
journal() { python3 -c "import json,sys; print(json.load(open('$JOURNAL'))[sys.argv[1]])" "$1"; }
inside() { docker exec "aurum-ai-hermes-$1" sh -c "$2"; }
ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "aurum-ai-hermes-$1"; }
create() {  # create <session hex>; prints the HTTP status
  curl -s -o /dev/null -w '%{http_code}' -X POST "$EXECUTOR/hermes/sandboxes" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$1\",\"org_id\":\"$ORG\",\"conversation_id\":\"$CONVERSATION\",\"token\":\"$TOKEN\",\"model\":\"hermes-egress-test\"}"
}
run_once() {  # run_once <session hex> <key>; prints the relay's result line
  curl -sN -X POST "$EXECUTOR/hermes/sandboxes/$1/runs" -H 'Content-Type: application/json' \
    -d "{\"key\":\"$2\",\"input\":\"Say hello.\",\"session_id\":\"$CONVERSATION\"}" | grep '"type":"result"' | tail -1
}
expect_completed() {
  python3 -c "import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if r['status']=='completed' and r['output'] else 1)" "$1" \
    || fail "run did not complete: $1"
}
gone() {
  ! docker container inspect "aurum-ai-hermes-$1" >/dev/null 2>&1 \
    && ! docker network inspect "aurum-ai-hermes-$1" >/dev/null 2>&1 \
    && ! docker volume inspect "aurum-ai-hermes-$1" >/dev/null 2>&1
}

log "building images from this checkout"
docker tag aurum-ai-postgres:latest aurum-ai-postgres:hermes-egress-test
"${STACK[@]}" build migrate hermes-egress-guard skill-executor >/dev/null
"${STACK[@]}" --profile build build hermes-sandbox >/dev/null
log "starting the backend, hermes-egress and skill-executor"
"${STACK[@]}" up -d --wait postgres redis temporal gateway keycloak >/dev/null
"${STACK[@]}" up -d backend >/dev/null
for _ in $(seq 60); do curl -fsS http://127.0.0.1:58000/health >/dev/null 2>&1 && break; sleep 2; done
"${STACK[@]}" up -d --wait hermes-egress-guard hermes-egress >/dev/null
"${STACK[@]}" up -d skill-executor >/dev/null
for _ in $(seq 30); do curl -fsS "$EXECUTOR/health" >/dev/null 2>&1 && break; sleep 1; done
TOKEN=$(fixture prepare)
ORG=$(journal org); CONVERSATION=$(journal conversation)
A=$(journal session | tr -d -); B=$(python3 -c "import uuid; print(uuid.uuid4().hex)")

log "a sandbox per session, hardened, with no secrets in its metadata"
[[ "$(create "$A")" == 201 ]] || fail "sandbox A was not created"
[[ "$(create "$A")" == 200 ]] || fail "creating again is not idempotent"
[[ "$(inside "$A" 'grep CapEff /proc/self/status')" == *0000000000000000 ]] || fail "sandbox keeps capabilities"
[[ "$(inside "$A" 'id -u')" == 10001 ]] || fail "sandbox runs as root"
inside "$A" 'touch /usr/probe 2>/dev/null' && fail "root filesystem is writable"
# Docker's embedded DNS also listens on 127.0.0.11 inside the namespace.
listening=$(inside "$A" "awk 'NR>1 && \$4==\"0A\" {print \$2}' /proc/net/tcp /proc/net/tcp6" | grep -v '^0B00007F:' || true)
[[ "$listening" == 0100007F:21C2 ]] || fail "Hermes listens beyond 127.0.0.1:8642: $listening"
docker inspect "aurum-ai-hermes-$A" | grep -q "$TOKEN" && fail "the session token is in container metadata"

log "a Hermes run goes through hermes-egress to the backend and is billed"
expect_completed "$(run_once "$A" turn-1)"
billed=$(docker exec "$PROJECT-postgres-1" psql -U postgres -d aurum_ai -tAc \
  "SELECT count(*) FROM llm_usage_events WHERE conversation_id='$CONVERSATION' AND status<>'cancelled'")
(( billed >= 1 )) || fail "no billed model call"

log "sandboxes cannot reach each other or the host"
[[ "$(create "$B")" == 201 ]] || fail "sandbox B was not created"
docker exec -d "aurum-ai-hermes-$A" python3 -m http.server 8000 --bind 0.0.0.0 --directory /tmp
sleep 2
A_IP=$(ip_of "$A")
probe='import socket,sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 3).close(); print("open")
except OSError:
    print("closed")'
[[ "$(docker exec "aurum-ai-hermes-$A" python3 -c "$probe" 127.0.0.1 8000)" == open ]] || fail "probe server did not start"
[[ "$(docker exec "aurum-ai-hermes-$B" python3 -c "$probe" "$A_IP" 8000)" == closed ]] || fail "sandbox B reaches sandbox A"
[[ "$(docker exec "aurum-ai-hermes-$B" python3 -c "$probe" 172.17.0.1 22)" == closed ]] || fail "a sandbox reaches the host"
[[ "$(docker exec "aurum-ai-hermes-$B" python3 -c "$probe" "$(ip_of "$B" | awk -F. '{print $1"."$2"."$3".1"}')" 22)" == closed ]] \
  || fail "a sandbox reaches its network's host side"

log "a restarted executor re-adopts its sandboxes"
docker restart "$PROJECT-skill-executor-1" >/dev/null
for _ in $(seq 30); do curl -fsS "$EXECUTOR/hermes/sandboxes/$A" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "$EXECUTOR/hermes/sandboxes/$A" >/dev/null || fail "sandbox A was not re-adopted"
expect_completed "$(run_once "$A" turn-2)"

log "a recreated guard is reconnected"
"${STACK[@]}" up -d --wait --force-recreate hermes-egress-guard hermes-egress >/dev/null
sleep 12
expect_completed "$(run_once "$A" turn-3)"

log "a sandbox outlives an executor that starts without the guard"
"${STACK[@]}" stop hermes-egress hermes-egress-guard >/dev/null
SKILL_EXECUTOR_HERMES_ORPHAN_GRACE_SECONDS=1 "${STACK[@]}" up -d --force-recreate skill-executor >/dev/null
sleep 20
docker container inspect "aurum-ai-hermes-$A" >/dev/null 2>&1 || fail "the reaper removed live sandbox A"
docker volume inspect "aurum-ai-hermes-$A" >/dev/null 2>&1 || fail "the reaper removed sandbox A's workspace"
"${STACK[@]}" up -d --wait hermes-egress-guard hermes-egress >/dev/null
sleep 12
expect_completed "$(run_once "$A" turn-4)"

log "closing and idle expiry leave nothing behind"
curl -fsS -X DELETE "$EXECUTOR/hermes/sandboxes/$B" -o /dev/null || fail "closing B failed"
gone "$B" || fail "closing B left resources behind"
SKILL_EXECUTOR_HERMES_IDLE_TTL_SECONDS=15 "${STACK[@]}" up -d --force-recreate skill-executor >/dev/null
for _ in $(seq 40); do gone "$A" && break; sleep 2; done
gone "$A" || fail "idle sandbox A was not reaped"
log "PASS"
