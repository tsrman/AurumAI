#!/usr/bin/env bash
# Hermes sandbox egress acceptance on a standalone, disposable project
# (scripts/fixtures/hermes-egress-base.yml plus the real docker-compose.hermes.yml,
# `-p hermes-egress-test`). A probe container joins a sandbox network created
# exactly as skill-executor creates one (internal, isolated gateway, from the
# pool, with only the guard connected) and checks, from inside: no DNS or routes of its own,
# only the platform completion path, and internet egress in each mode as the
# backend and the guard decide. Needs internet access for the package and
# public-mode probes (pypi.org, the npm registry, example.com). Everything is torn down after.
#
# Prerequisites: Docker 28+, uv, free 55436/58000, no other
# aurum-ai-hermes-egress-guard container. Images are built from this checkout; aurum-ai-web-egress:latest is
# rebuilt. Never point this at a shared or production stack.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT_DIR"
PROJECT=hermes-egress-test
JOURNAL=/tmp/hermes-egress-acceptance/fixture.json
PROBE_IMAGE=python:3.12-slim
SANDBOX_NET=hermes-egress-test-sandbox
export HERMES_EGRESS_SERVICE_SECRET=hermes-egress-acceptance-service-secret
export HERMES_EGRESS_MAX_MODE=public HERMES_ENABLED=true HERMES_EGRESS_RECHECK_SECONDS=3
STACK=(docker compose -p "$PROJECT" --env-file /dev/null --project-directory "$ROOT_DIR" -f scripts/fixtures/hermes-egress-base.yml -f docker-compose.hermes.yml --profile hermes)
log() { printf '[hermes-egress] %s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }

if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]]; then
  echo "A $PROJECT project already exists; finish or remove it first" >&2; exit 1
fi
if docker container inspect aurum-ai-hermes-egress-guard >/dev/null 2>&1; then
  echo "An aurum-ai-hermes-egress-guard container already exists (another Hermes stack?)" >&2; exit 1
fi
if docker network inspect "$SANDBOX_NET" >/dev/null 2>&1; then
  echo "The $SANDBOX_NET network already exists" >&2; exit 1
fi
for port in 55436 58000; do
  if ss -ltn | grep -q ":$port "; then echo "Port $port is in use" >&2; exit 1; fi
done
if [[ -e "$JOURNAL" ]]; then echo "An earlier fixture journal exists: $JOURNAL" >&2; exit 1; fi

teardown() {
  local status=$?
  log "removing the disposable project"
  docker rm -f "$PROJECT-hold" >/dev/null 2>&1 || true
  "${STACK[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  docker network rm "$SANDBOX_NET" >/dev/null 2>&1 || true
  if (( status == 0 )); then rm -f "$JOURNAL"; else log "journal kept for inspection: $JOURNAL"; fi
  exit "$status"
}
trap teardown EXIT

fixture() { (cd business-backend && uv run --frozen python ../scripts/hermes-egress-fixture.py "$@"); }
probe() {
  docker run --rm --network "$SANDBOX_NET" -e HERMES_TOKEN="$TOKEN" \
    -v "$ROOT_DIR/scripts/hermes-egress-probe.py:/probe.py:ro" "$PROBE_IMAGE" python /probe.py "$@"
}
expect() {  # expect <json> <python expression over r>
  python3 -c "import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if ($2) else 1)" "$1" \
    || fail "expected $2, got $1"
}

log "building images from this checkout"
docker tag aurum-ai-postgres:latest aurum-ai-postgres:hermes-egress-test
"${STACK[@]}" build migrate hermes-egress-guard >/dev/null
docker image inspect "$PROBE_IMAGE" >/dev/null 2>&1 || docker pull -q "$PROBE_IMAGE" >/dev/null
log "starting the backend and hermes-egress"
"${STACK[@]}" up -d --wait postgres redis temporal gateway keycloak >/dev/null
"${STACK[@]}" up -d backend >/dev/null
for _ in $(seq 60); do curl -fsS http://127.0.0.1:58000/health >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:58000/health >/dev/null || fail "backend did not start"
"${STACK[@]}" up -d --wait hermes-egress-guard hermes-egress >/dev/null
# As skill-executor does for each sandbox (src/hermes_runtime.py).
docker network create --internal -o com.docker.network.bridge.gateway_mode_ipv4=isolated \
  --subnet 172.31.255.0/28 "$SANDBOX_NET" >/dev/null
docker network connect --alias hermes-egress "$SANDBOX_NET" aurum-ai-hermes-egress-guard
TOKEN=$(fixture prepare)
BACKEND_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$PROJECT-backend-1" | awk '{print $1}')
POSTGRES_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$PROJECT-postgres-1")

log "sandbox network: no DNS or routes of its own"
r=$(probe network "$BACKEND_IP" "$POSTGRES_IP")
expect "$r" 'r["egress_dns"] and not r["external_dns"] and not r["direct_public"] and not r["direct_backend"] and not r["direct_postgres"]'

log "platform path: completions only, one request per connection"
r=$(probe platform)
expect "$r" 'r["completion_status"] == 200 and r["completion_streamed"] and r["unstreamed_json"] and r["internal_status"] == 404 and r["smuggled_responses"] == 1 and not r["direct_backend_port"]'

log "proxy refuses foreign tokens"
expect "$(probe bad-token)" 'r["status"] == 403 and r["reason"] == "hermes_token_invalid"'

log "guard: the proxy's own namespace reaches public 443 only, never private ranges"
guard_probe() {
  docker exec "$PROJECT-hermes-egress-1" python3 -c "import socket,sys
try:
    socket.create_connection(('$1', $2), 5).close(); print('open')
except OSError:
    print('closed')"
}
[[ "$(guard_probe 1.1.1.1 443)" == open ]] || fail "guard blocks public 443"
[[ "$(guard_probe 1.1.1.1 22)" == closed ]] || fail "guard allows public port 22"
[[ "$(guard_probe 10.0.0.1 443)" == closed ]] || fail "guard allows a private range"
[[ "$(guard_probe "$POSTGRES_IP" 5432)" == closed ]] || fail "guard allows PostgreSQL"

log "mode none"
fixture mode none
sleep 6
expect "$(probe connect pypi.org:443)" 'r["status"] == 403 and r["reason"] == "egress_disabled"'

log "mode packages: the listed index only"
fixture mode packages
sleep 6
expect "$(probe pip)" 'r["pip_ok"] and r["downloaded"] == ["six-1.17.0-py2.py3-none-any.whl"]'
expect "$(probe npm)" 'r["status"] == 200'
expect "$(probe connect example.com:443)" 'r["status"] == 403 and r["reason"] == "host_not_allowed"'

log "mode public without web.access stays packages"
fixture mode public
sleep 6
expect "$(probe connect example.com:443)" 'r["status"] == 403 and r["reason"] == "host_not_allowed"'

log "mode public with web.access"
fixture web-access on
sleep 6
expect "$(probe https example.com)" 'r["status"] == 200'
expect "$(probe connect example.com:22)" 'r["status"] == 403 and r["reason"] == "port_not_allowed"'
for target in 10.0.0.1:443 169.254.169.254:80 "$BACKEND_IP:443" "$POSTGRES_IP:443" 127.0.0.1:443 "[::1]:443"; do
  expect "$(probe connect "$target")" 'r["status"] == 403 and r["reason"] == "address_forbidden"'
done

log "an ended turn stops new connections"
fixture end-turn
sleep 6
expect "$(probe connect example.com:443)" 'r["status"] == 403 and r["reason"] == "hermes_turn_inactive"'
fixture bind
sleep 6
expect "$(probe connect example.com:443)" 'r["status"] == 200'

log "an open tunnel is cut when the session closes"
docker run -d --name "$PROJECT-hold" --network "$SANDBOX_NET" -e HERMES_TOKEN="$TOKEN" \
  -v "$ROOT_DIR/scripts/hermes-egress-probe.py:/probe.py:ro" "$PROBE_IMAGE" \
  python -u /probe.py hold example.com:443 >/dev/null
for _ in $(seq 30); do docker logs "$PROJECT-hold" 2>/dev/null | grep -q '"opened": true' && break; sleep 1; done
docker logs "$PROJECT-hold" | grep -q '"opened": true' || fail "tunnel did not open"
fixture close
docker wait "$PROJECT-hold" >/dev/null
r=$(docker logs "$PROJECT-hold" | tail -1)
expect "$r" 'r["closed"] and r["after_seconds"] < 30'
expect "$(probe connect example.com:443)" 'r["status"] == 403 and r["reason"] == "hermes_session_revoked"'
log "PASS"
