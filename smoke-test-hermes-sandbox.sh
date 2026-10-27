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
# idle expiry leaves no container, network or volume behind. Phase 2c-ii adds
# workspace snapshots in a disposable MinIO: an idle sandbox's workspace comes
# back in a fresh sandbox without the old secrets, and a sandbox whose snapshot
# cannot be stored stays stopped with its volume (also across an executor
# restart), resumes, and is retired once the store is back. Phase 2c-ii-b adds
# the organization cap (the least recently used idle sandbox is retired), the
# soft workspace limit from Docker's volume usage, and the tmpfs workspace:
# files written after the start, a kernel-enforced size, and a pause instead
# of a stop when its snapshot cannot be stored. Phase 3 adds the MCP bridge:
# Hermes generates an image through hermes-egress as a durable operation of the
# bound execution, and between turns the sandbox can list the platform tools
# but not call them. Phase 3b adds platform skills: Hermes runs a granted MCP
# skill through tool-worker against the reference MCP server. Phase 4a adds a
# chat turn: a conversation runner hands a Hermes turn to the session workflow
# on tool-worker, which prepares a sandbox, binds the turn and persists the
# answer through the runner, leaving the session unbound; the next turn reuses
# the running sandbox and its session without rotating the token.
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
export SKILL_EXECUTOR_HERMES_SNAPSHOT_RETRY_SECONDS=5
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
  create_json "$1" -o /dev/null -w '%{http_code}'
}
create_json() {  # create_json <session hex> [curl args]; prints the response
  local session=$1; shift
  curl -s "$@" -X POST "$EXECUTOR/hermes/sandboxes" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$session\",\"org_id\":\"$ORG\",\"conversation_id\":\"$CONVERSATION\",\"token\":\"$TOKEN\",\"model\":\"hermes-egress-test\"}"
}
outcome() { python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('outcome'))" "$1"; }
run_status() {  # run_status <session hex> <key>; prints the HTTP status of starting a run
  curl -s -o /dev/null -w '%{http_code}' -X POST "$EXECUTOR/hermes/sandboxes/$1/runs" -H 'Content-Type: application/json' \
    -d "{\"key\":\"$2\",\"input\":\"Say hello.\",\"session_id\":\"$CONVERSATION\"}"
}
executor_with() {  # executor_with VAR=value...; recreates skill-executor with these settings
  env "$@" "${STACK[@]}" up -d --force-recreate skill-executor >/dev/null
  for _ in $(seq 30); do curl -fsS "$EXECUTOR/health" >/dev/null 2>&1 && break; sleep 1; done
}
state_of() { docker inspect -f '{{.State.Status}}' "aurum-ai-hermes-$1" 2>/dev/null || echo gone; }
mc_() { docker exec "$PROJECT-minio-1" mc "$@"; }
run_once() {  # run_once <session hex> <key> [input]; prints the relay's result line
  curl -sN -X POST "$EXECUTOR/hermes/sandboxes/$1/runs" -H 'Content-Type: application/json' \
    -d "{\"key\":\"$2\",\"input\":\"${3:-Say hello.}\",\"session_id\":\"$CONVERSATION\"}" | grep '"type":"result"' | tail -1
}
sql() { docker exec "$PROJECT-postgres-1" psql -U postgres -d aurum_ai -tAc "$1"; }
MCP_PROBE='import json, sys, urllib.request
token = json.load(open("/workspace/.hermes/config.yaml"))["mcp_servers"]["aurum"]["headers"]["Authorization"]
request = urllib.request.Request("http://hermes-egress:8080/v1/hermes/mcp", sys.argv[1].encode(),
                                 {"Authorization": token, "Content-Type": "application/json"})
print(urllib.request.urlopen(request, timeout=30).read().decode())'
mcp_from() {  # mcp_from <session hex> <JSON-RPC message>; the bridge's answer to the sandbox's own token
  docker exec "aurum-ai-hermes-$1" python3 -c "$MCP_PROBE" "$2"
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
"${STACK[@]}" build migrate hermes-egress-guard skill-executor tool-worker mcp-everything conversation-runner >/dev/null
"${STACK[@]}" --profile build build hermes-sandbox >/dev/null
log "starting the backend, hermes-egress and skill-executor"
"${STACK[@]}" up -d --wait postgres redis temporal gateway keycloak minio >/dev/null
"${STACK[@]}" up -d backend >/dev/null
for _ in $(seq 60); do curl -fsS http://127.0.0.1:58000/health >/dev/null 2>&1 && break; sleep 2; done
"${STACK[@]}" up -d --wait hermes-egress-guard hermes-egress >/dev/null
"${STACK[@]}" up -d skill-executor tool-worker mcp-everything >/dev/null
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
# Hermes 0.22's gateway also opens a loop-liveness ping on a random loopback port.
grep -qx '0100007F:21C2' <<<"$listening" || fail "Hermes's API server is not on 127.0.0.1:8642: $listening"
grep -v '^0100007F:' <<<"$listening" | grep -q . && fail "Hermes listens beyond loopback: $listening"
docker inspect "aurum-ai-hermes-$A" | grep -q "$TOKEN" && fail "the session token is in container metadata"

log "a Hermes run goes through hermes-egress to the backend and is billed"
expect_completed "$(run_once "$A" turn-1)"
billed=$(docker exec "$PROJECT-postgres-1" psql -U postgres -d aurum_ai -tAc \
  "SELECT count(*) FROM llm_usage_events WHERE conversation_id='$CONVERSATION' AND status<>'cancelled'")
(( billed >= 1 )) || fail "no billed model call"

log "Hermes calls platform tools over the MCP bridge, as operations of the bound execution"
result=$(run_once "$A" mcp-1 HERMES_MCP:generate_image)
expect_completed "$result"
[[ "$result" == *HERMES_MCP_DONE:generate_image:*imageId* ]] || fail "the image tool did not answer Hermes: $result"
[[ "$(sql "SELECT count(*) FROM tool_operations o JOIN hermes_sessions s ON s.active_execution_id=o.execution_id
  WHERE o.org_id='$ORG' AND o.kind='image_generate' AND o.status='completed'")" == 1 ]] \
  || fail "the image is not one completed operation of the bound execution"
(( $(sql "SELECT count(*) FROM conversation_images WHERE conversation_id='$CONVERSATION'") == 1 )) \
  || fail "the generated image was not stored"

log "Hermes runs a granted platform skill through tool-worker"
SKILL=$(fixture skill)
skills=$(mcp_from "$A" '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_skills","arguments":{}}}')
[[ "$skills" == *"$SKILL"* && "$skills" != *mcp-everything* ]] || fail "the skill listing is wrong or shows its endpoint: $skills"
result=$(run_once "$A" mcp-2 "HERMES_MCP:run_skill:$SKILL")
expect_completed "$result"
[[ "$result" == *HERMES_MCP_DONE:run_skill:*42* ]] || fail "the skill did not answer Hermes: $result"
fixture end-turn
listed=$(mcp_from "$A" '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
[[ "$listed" == *generate_image* ]] || fail "the sandbox cannot list the platform tools between turns: $listed"
called=$(mcp_from "$A" '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"knowledge_search","arguments":{"query":"x"}}}')
[[ "$called" == *hermes_turn_inactive* ]] || fail "a platform tool ran between turns: $called"
fixture bind

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

log "closing and idle expiry leave nothing behind, keeping the workspace in a snapshot"
inside "$A" 'mkdir -p $HOME/project && echo kept > $HOME/project/kept.txt'
curl -fsS -X DELETE "$EXECUTOR/hermes/sandboxes/$B" -o /dev/null || fail "closing B failed"
gone "$B" || fail "closing B left resources behind"
SKILL_EXECUTOR_HERMES_IDLE_TTL_SECONDS=15 "${STACK[@]}" up -d --force-recreate skill-executor >/dev/null
for _ in $(seq 40); do gone "$A" && break; sleep 2; done
gone "$A" || fail "idle sandbox A was not reaped"
PREFIX="local/aurum-ai-business/hermes-sessions/$ORG/$CONVERSATION"
SHA=$(mc_ cat "$PREFIX/latest.json" | python3 -c "import json,sys; print(json.load(sys.stdin)['sha256'])") \
  || fail "no snapshot pointer"
mc_ cat "$PREFIX/$SHA.tar" > "$(dirname "$JOURNAL")/snapshot.tar"
[[ "$(sha256sum < "$(dirname "$JOURNAL")/snapshot.tar" | cut -d' ' -f1)" == "$SHA" ]] || fail "the snapshot is not stored by its hash"
grep -aq "$TOKEN" "$(dirname "$JOURNAL")/snapshot.tar" && fail "the session token is in the snapshot"
tar -tf "$(dirname "$JOURNAL")/snapshot.tar" | grep -Eq '^(\.aurum|\.hermes/config\.yaml)' && fail "platform files are in the snapshot"
rm -f "$(dirname "$JOURNAL")/snapshot.tar"

log "the next create restores the workspace into a fresh sandbox"
for _ in $(seq 30); do curl -fsS "$EXECUTOR/health" >/dev/null 2>&1 && break; sleep 1; done
[[ "$(outcome "$(create_json "$A")")" == restored ]] || fail "sandbox A was not restored"
[[ "$(inside "$A" 'cat $HOME/project/kept.txt')" == kept ]] || fail "the workspace did not come back"
[[ "$(inside "$A" 'stat -c %u $HOME/project')" == 10001 ]] || fail "a restored directory is not the sandbox user's"
expect_completed "$(run_once "$A" turn-5)"

log "without the store, an idle sandbox stays stopped with its volume and resumes"
"${STACK[@]}" stop minio >/dev/null
for _ in $(seq 40); do [[ "$(state_of "$A")" == exited ]] && break; sleep 2; done
[[ "$(state_of "$A")" == exited ]] || fail "idle sandbox A was not stopped: $(state_of "$A")"
sleep 12
[[ "$(state_of "$A")" == exited ]] || fail "sandbox A did not stay stopped without the store"
docker volume inspect "aurum-ai-hermes-$A" >/dev/null 2>&1 || fail "the workspace of a failed snapshot was removed"
docker restart "$PROJECT-skill-executor-1" >/dev/null
for _ in $(seq 30); do curl -fsS "$EXECUTOR/health" >/dev/null 2>&1 && break; sleep 1; done
sleep 12
[[ "$(state_of "$A")" == exited ]] || fail "a restarted executor changed stopped sandbox A: $(state_of "$A")"
docker volume inspect "aurum-ai-hermes-$A" >/dev/null 2>&1 || fail "a restarted executor removed a stopped workspace"
[[ "$(outcome "$(create_json "$A")")" == resumed ]] || fail "stopped sandbox A was not resumed"
[[ "$(inside "$A" 'cat $HOME/project/kept.txt')" == kept ]] || fail "the resumed workspace changed"
expect_completed "$(run_once "$A" turn-6)"

log "once the store is back, the stopped sandbox is snapshotted and removed"
for _ in $(seq 40); do [[ "$(state_of "$A")" == exited ]] && break; sleep 2; done
"${STACK[@]}" up -d --wait minio >/dev/null
for _ in $(seq 40); do gone "$A" && break; sleep 2; done
gone "$A" || fail "stopped sandbox A was not retired after the store came back"

log "at the organization cap, the least recently used idle sandbox is retired"
C=$(python3 -c "import uuid; print(uuid.uuid4().hex)")
executor_with SKILL_EXECUTOR_HERMES_MAX_RUNNING_PER_ORG=2 SKILL_EXECUTOR_HERMES_WORKSPACE_MAX_BYTES=67108864 \
  SKILL_EXECUTOR_HERMES_DISK_CHECK_INTERVAL_SECONDS=5
[[ "$(outcome "$(create_json "$A")")" == restored ]] || fail "sandbox A was not restored"
sleep 2
[[ "$(create "$B")" == 201 ]] || fail "sandbox B was not created"
[[ "$(docker exec "aurum-ai-hermes-$B" hermes-relay active)" == '{"type":"active","run_ids":[]}' ]] \
  || fail "the relay reports active runs on an idle sandbox"
[[ "$(create "$C")" == 201 ]] || fail "sandbox C was not admitted at the cap"
gone "$A" || fail "the least recently used sandbox A was not retired"
docker container inspect "aurum-ai-hermes-$B" >/dev/null 2>&1 || fail "sandbox B was retired instead of A"

log "over the soft workspace limit, a sandbox gets no new runs until it is back under"
inside "$C" 'head -c 83886080 /dev/zero > $HOME/big'
for _ in $(seq 30); do [[ "$(run_status "$C" turn-7)" == 507 ]] && break; sleep 2; done
[[ "$(run_status "$C" turn-7)" == 507 ]] || fail "a sandbox over its workspace limit still starts runs"
inside "$C" 'rm $HOME/big'
for _ in $(seq 30); do [[ "$(run_status "$C" turn-8)" == 200 ]] && break; sleep 2; done
expect_completed "$(run_once "$C" turn-9)"
curl -fsS -X DELETE "$EXECUTOR/hermes/sandboxes/$B" -o /dev/null && curl -fsS -X DELETE "$EXECUTOR/hermes/sandboxes/$C" -o /dev/null \
  || fail "closing B and C failed"

log "a tmpfs workspace is restored after the start and bounded by the kernel"
executor_with SKILL_EXECUTOR_HERMES_WORKSPACE_MODE=tmpfs SKILL_EXECUTOR_HERMES_WORKSPACE_MAX_BYTES=67108864 \
  SKILL_EXECUTOR_HERMES_IDLE_TTL_SECONDS=15
[[ "$(outcome "$(create_json "$A")")" == restored ]] || fail "sandbox A was not restored onto tmpfs"
inside "$A" 'grep -q "^tmpfs /workspace tmpfs" /proc/mounts' || fail "the workspace is not a tmpfs"
[[ "$(inside "$A" 'cat $HOME/project/kept.txt')" == kept ]] || fail "the tmpfs workspace did not get the snapshot"
inside "$A" 'head -c 83886080 /dev/zero > $HOME/big 2>/dev/null' && fail "the tmpfs workspace is not bounded"
inside "$A" 'rm -f $HOME/big'
expect_completed "$(run_once "$A" turn-10)"

log "a tmpfs sandbox whose snapshot fails is paused, then retired once the store is back"
"${STACK[@]}" stop minio >/dev/null
for _ in $(seq 40); do [[ "$(state_of "$A")" == paused ]] && break; sleep 2; done
[[ "$(state_of "$A")" == paused ]] || fail "idle tmpfs sandbox A was not paused: $(state_of "$A")"
"${STACK[@]}" up -d --wait minio >/dev/null
for _ in $(seq 40); do gone "$A" && break; sleep 2; done
gone "$A" || fail "paused sandbox A was not retired after the store came back"
SHA=$(mc_ cat "$PREFIX/latest.json" | python3 -c "import json,sys; print(json.load(sys.stdin)['sha256'])") \
  || fail "no snapshot of the paused tmpfs sandbox"
mc_ cat "$PREFIX/$SHA.tar" | tar -tf - | grep -q 'home/project/kept.txt' || fail "the tmpfs snapshot lost the workspace"

log "a Hermes chat turn runs through the conversation runner and the session workflow"
executor_with
CHAT=$(fixture chat)
field() { python3 -c "import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])" "$CHAT" "$1"; }
CHAT_CONVERSATION=$(field conversation); CHAT_TURN=$(field turn)
"${STACK[@]}" run -d --no-deps --name "$PROJECT-runner" -e CONVERSATION_ID="$CHAT_CONVERSATION" \
  -e USER_ID="$(journal user)" -e ORG_ID="$ORG" -e SESSION_TOKEN="$(field token)" conversation-runner >/dev/null
temporal_() { docker exec "$PROJECT-temporal-1" temporal "$@" --address localhost:7233 >/dev/null; }
temporal_ workflow start --type ConversationWorkflow --task-queue "session-$CHAT_CONVERSATION" \
  --workflow-id "conversation-$CHAT_CONVERSATION" --input "{\"conversation_id\":\"$CHAT_CONVERSATION\"}"
chat_turn() {  # chat_turn <turn id> <content> <answers expected>
  temporal_ workflow signal --workflow-id "conversation-$CHAT_CONVERSATION" --name user_message --input \
    "{\"role\":\"user\",\"content\":\"$2\",\"turn_id\":\"$1\",\"model\":\"hermes-egress-test\",\"hermes\":true}"
  for _ in $(seq 120); do
    (( $(sql "SELECT count(*) FROM messages WHERE conversation_id='$CHAT_CONVERSATION' AND role='assistant' AND content<>''") >= $3 )) \
      && break
    sleep 3
  done
  (( $(sql "SELECT count(*) FROM messages WHERE conversation_id='$CHAT_CONVERSATION' AND role='assistant' AND content<>''") >= $3 )) \
    || fail "the Hermes chat turn $1 persisted no answer"
  [[ "$(sql "SELECT status FROM interaction_turns WHERE id='$1'")" == completed ]] || fail "the Hermes chat turn $1 did not complete"
}
EVENTS="$(dirname "$JOURNAL")/chat-events.log"
docker exec "$PROJECT-redis-1" redis-cli SUBSCRIBE "$ORG:session:$CHAT_CONVERSATION:events" > "$EVENTS" &
SUBSCRIBER=$!
sleep 1
chat_turn "$CHAT_TURN" "HERMES_MCP:knowledge_search" 1
sleep 1
kill "$SUBSCRIBER" 2>/dev/null || true
grep -q '"type": "tool-calls".*"tool": "hermes.knowledge"' "$EVENTS" || fail "the turn published no Hermes tool card"
grep -q '"type": "tool-result".*"status": "completed"' "$EVENTS" || fail "the turn published no Hermes tool result"
grep -q '"type": "keepalive"' "$EVENTS" \
  || { docker logs "$PROJECT-tool-worker-1" 2>&1 | tail -40 >&2; fail "the turn kept no stream open"; }
grep -q 'hermes-argument-sentinel' "$EVENTS" && fail "a tool argument reached the public stream"
[[ "$(sql "SELECT tool_calls::text FROM messages WHERE conversation_id='$CHAT_CONVERSATION' AND role='assistant'")" == *hermes.knowledge* ]] \
  || fail "the answer was persisted without its tool card"
CHAT_SESSION=$(sql "SELECT replace(id::text,'-','') FROM hermes_sessions WHERE conversation_id='$CHAT_CONVERSATION' AND state='open' AND active_turn_id IS NULL")
[[ -n "$CHAT_SESSION" ]] || fail "the chat session is missing or still bound after the turn"
[[ "$(state_of "$CHAT_SESSION")" == running ]] || fail "the chat session's sandbox is not running"
TOKEN_VERSION=$(sql "SELECT token_version FROM hermes_sessions WHERE replace(id::text,'-','')='$CHAT_SESSION'")
chat_turn "$(fixture chat-turn "$CHAT_CONVERSATION")" "Say hello again." 2
[[ "$(sql "SELECT count(*) FROM hermes_sessions WHERE conversation_id='$CHAT_CONVERSATION'")" == 1 ]] \
  || fail "the second turn opened another session"
[[ "$(sql "SELECT token_version FROM hermes_sessions WHERE replace(id::text,'-','')='$CHAT_SESSION'")" == "$TOKEN_VERSION" ]] \
  || fail "the second turn rotated the running sandbox's token"
[[ "$(state_of "$CHAT_SESSION")" == running ]] || fail "the second turn did not reuse the sandbox"

log "Hermes asks the user through a question card, and the answer reaches it"
chat_turn() {  # chat_turn <turn id> <content> <answers expected> [questions enabled]
  temporal_ workflow signal --workflow-id "conversation-$CHAT_CONVERSATION" --name user_message --input \
    "{\"role\":\"user\",\"content\":\"$2\",\"turn_id\":\"$1\",\"model\":\"hermes-egress-test\",\"hermes\":true,\"user_questions_enabled\":${4:-false}}"
  [[ "${5:-}" == ask ]] && return 0
  wait_answer "$1" "$3"
}
wait_answer() {
  for _ in $(seq 120); do
    (( $(sql "SELECT count(*) FROM messages WHERE conversation_id='$CHAT_CONVERSATION' AND role='assistant' AND content<>''") >= $2 )) \
      && break
    sleep 3
  done
  [[ "$(sql "SELECT status FROM interaction_turns WHERE id='$1'")" == completed ]] || fail "the Hermes chat turn $1 did not complete"
}
USER=$(fixture user-token)
python3 -c "import json,sys; print(json.dumps(json.loads(sys.argv[1])['jwks']))" "$USER" \
  | docker exec -i "$PROJECT-keycloak-1" sh -c 'cat > /tmp/jwks.json'
USER_TOKEN=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['token'])" "$USER")
ASK_TURN=$(fixture chat-turn "$CHAT_CONVERSATION" HERMES_CLARIFY)
chat_turn "$ASK_TURN" HERMES_CLARIFY 3 true ask
for _ in $(seq 60); do
  QUESTION=$(sql "SELECT id FROM user_questions WHERE turn_id='$ASK_TURN' AND status='pending'")
  [[ -n "$QUESTION" ]] && break
  sleep 2
done
[[ -n "$QUESTION" ]] || fail "Hermes's question did not become the turn's question card"
[[ "$(sql "SELECT text FROM user_questions WHERE id='$QUESTION'")" == "Which city should the trip start in?" ]] \
  || fail "the question card does not carry Hermes's question"
answered=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:58000/v1/conversations/$CHAT_CONVERSATION/questions/$QUESTION/answer" \
  -H "Authorization: Bearer $USER_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"idempotencyKey\":\"$(python3 -c 'import uuid; print(uuid.uuid4())')\",\"answer\":{\"optionId\":\"c1\"}}")
[[ "$answered" == 200 ]] || fail "answering the question card failed: $answered"
wait_answer "$ASK_TURN" 3
ANSWER=$(sql "SELECT content FROM messages WHERE turn_id='$ASK_TURN' AND role='assistant'")
[[ "$ANSWER" == *HERMES_CLARIFY_DONE:*Rome* ]] || fail "Hermes did not get the answer: $ANSWER"

log "with questions off, Hermes's question is skipped instead of waiting"
SKIP_TURN=$(fixture chat-turn "$CHAT_CONVERSATION" HERMES_CLARIFY)
chat_turn "$SKIP_TURN" HERMES_CLARIFY 4 false
[[ "$(sql "SELECT count(*) FROM user_questions WHERE turn_id='$SKIP_TURN'")" == 0 ]] || fail "a disabled question was asked"
ANSWER=$(sql "SELECT content FROM messages WHERE turn_id='$SKIP_TURN' AND role='assistant'")
[[ "$ANSWER" == *HERMES_CLARIFY_DONE:*'"user_response": "skip"'* ]] || fail "the skipped question did not end: $ANSWER"
log "PASS"
