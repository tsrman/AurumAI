#!/usr/bin/env bash
set -euo pipefail

# Repeatable local Kubernetes acceptance for the stateless platform plus its
# dynamic runner and sandbox Pods. The stateful data plane stays in Compose;
# k3d joins that exact Docker network so Pods can resolve the container names.
#
# K8S_TEST_ACCEPTANCE=hermes runs Hermes Agent on the whole platform instead: the
# chart with hermes.enabled, its guard and sandbox Pods, and the Hermes turns of
# scripts/hermes-k8s-platform-acceptance.py through the backend, tool-worker and
# skill-executor (files in the answer, the MCP bridge, memory across conversations,
# skill drafts, an idle sandbox snapshotted and restored). It first uses the release's
# deterministic test gateway; with K8S_TEST_REAL_GATEWAY_URL and
# K8S_TEST_REAL_GATEWAY_API_KEY (and K8S_TEST_REAL_MODEL, default deepseek-v4-flash) it
# then switches the release to that gateway for one billed turn that computes, installs
# a package through the guard and attaches a file. The user is K8S_TEST_USER (an
# organization administrator whose organization may use Hermes; its Hermes settings are
# changed for the run and restored). A Compose data plane started with generated
# secrets needs K8S_TEST_DATABASE_ROLE_PASSWORD, K8S_TEST_REDIS_PASSWORD and
# K8S_TEST_S3_SECRET_KEY (and the same APP_SECRET_KEY); the chart's defaults match a
# fresh one.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$REPO_ROOT/charts/aurum-ai"
CLUSTER_NAME="${K8S_TEST_CLUSTER:-aurum-ai-k8s-smoke}"
DOCKER_NETWORK="${K8S_TEST_DOCKER_NETWORK:-aurum-ai_default}"
COMPOSE_PROJECT="${K8S_TEST_COMPOSE_PROJECT:-aurum-ai}"
RELEASE_NAME="${K8S_TEST_RELEASE:-aurum-ai-k8s-smoke}"
RELEASE_NAMESPACE="${K8S_TEST_NAMESPACE:-aurum-ai-k8s-smoke}"
RUNNER_NAMESPACE="${K8S_TEST_RUNNER_NAMESPACE:-${RELEASE_NAME}-runners}"
SANDBOX_NAMESPACE="${K8S_TEST_SANDBOX_NAMESPACE:-${RELEASE_NAME}-sandboxes}"
HERMES_NAMESPACE="${K8S_TEST_HERMES_NAMESPACE:-${RELEASE_NAME}-hermes}"
HERMES_IDLE_SECONDS="${K8S_TEST_HERMES_IDLE_SECONDS:-45}"
KEEP_RESOURCES="${KEEP_K8S_TEST_RESOURCES:-0}"
ACCEPTANCE_PROFILE="${K8S_TEST_ACCEPTANCE:-full}"
# Compose interpolates the complete file even when `up` names only data-plane
# services, so its required backend secret must exist before any `compose ps`
# call. This value is local-test-only and does not override a caller's secret.
APP_SECRET_KEY="${APP_SECRET_KEY:-k8s-smoke-test-only-secret}"
export APP_SECRET_KEY
BACKEND_PORT="${K8S_TEST_BACKEND_PORT:-18000}"
ADMIN_PORT="${K8S_TEST_ADMIN_PORT:-18001}"
SKILL_EXECUTOR_PORT="${K8S_TEST_SKILL_EXECUTOR_PORT:-18090}"
GATEWAY_PORT="${K8S_TEST_GATEWAY_PORT:-18080}"
FRONTEND_PORT="${K8S_TEST_FRONTEND_PORT:-8081}"
INGRESS_NODE_PORT=30080
KEYCLOAK_PUBLIC_URL="${K8S_TEST_KEYCLOAK_URL:-http://localhost:8080}"
TEST_USERNAME="${K8S_TEST_USER:-admin}"
TEST_PASSWORD="${K8S_TEST_PASSWORD:-admin}"
TEMPORAL_NAMESPACE="${K8S_TEST_TEMPORAL_NAMESPACE:-${RELEASE_NAME}-$$}"
RUNNER_IDLE_SECONDS="${K8S_TEST_RUNNER_IDLE_SECONDS:-12}"
RUNNER_REAP_SECONDS="${K8S_TEST_RUNNER_REAP_SECONDS:-2}"
RUNNER_ACTIVE_IDLE_SECONDS="${K8S_TEST_ACTIVE_RUNNER_IDLE_SECONDS:-300}"
K8S_RATE_LIMIT_PER_MINUTE="${K8S_TEST_RATE_LIMIT_PER_MINUTE:-2000}"
K8S_RATE_LIMIT_PER_MINUTE_STREAM="${K8S_TEST_RATE_LIMIT_PER_MINUTE_STREAM:-500}"
OWNER_LABEL="aurum-ai.k8s-smoke/owned"
OWNER_CONFIGMAP="${RELEASE_NAME}-smoke-owner"
GATEWAY_NAME="${RELEASE_NAME}-test-gateway"
FETCH_FIXTURE_NAME="${RELEASE_NAME}-fetch-fixture-$$"
NETWORK_PROBE_NAME="${RELEASE_NAME}-skill-executor-deny-$$"
MCP_FETCH_TARGET_URL="http://${FETCH_FIXTURE_NAME}:8080/index.html"
MCP_FETCH_EXPECTED_TITLE="Kubernetes MCP Fetch Fixture"
WORK_DIR=""
KUBECONFIG=""
CREATED_CLUSTER=0
CREATED_NETWORK=0
CREATED_RELEASE_NAMESPACE=0
CREATED_RELEASE=0
CREATED_OWNER_CONFIGMAP=0
CREATED_GATEWAY=0
CREATED_FETCH_FIXTURE=0
CREATED_NETWORK_PROBE=0
CREATED_TEMPORAL_NAMESPACE=0
DIAGNOSTICS_DUMPED=0
USER_QUESTIONS_MARKER=""
USER_QUESTIONS_RECOVERY_DIR=""
USER_QUESTIONS_STATE=""
TEST_GATEWAY_MODEL_ID_YAML=""
PORT_FORWARD_PIDS=()
BACKGROUND_PIDS=()
COMPOSE_CREATED_SERVICES=()
COMPOSE_STARTED_SERVICES=()
# Track setup containers too: selective up must create the default namespace,
# and cleanup must restore/remove every setup service started/created by this run.
COMPOSE_SERVICES=(postgres redis keycloak temporal-admin-tools temporal temporal-create-namespace minio mcp-everything mcp-fetch)
POSTGRES_IP=""
REDIS_IP=""
KEYCLOAK_IP=""
TEMPORAL_IP=""
MINIO_IP=""
MCP_EVERYTHING_IP=""
MCP_FETCH_IP=""

log() { printf '[k8s-smoke] %s\n' "$*" >&2; }

# temporalio/server (unlike the deprecated temporalio/auto-setup this
# replaced) ships no `temporal` CLI binary at all — `docker exec` into the
# server container for operator/workflow commands no longer works. The CLI
# now only lives in temporalio/admin-tools, which the Compose stack already
# runs one-shot for schema setup — reuse that same service/image via `run`
# for every ad-hoc CLI invocation instead of exec'ing into `temporal`.
temporal_cli() {
  docker compose -p "$COMPOSE_PROJECT" run --rm --no-deps --entrypoint temporal \
    temporal-admin-tools --address temporal:7233 "$@"
}

dump_failure_diagnostics() {
  local conversation_id pod
  [[ "$DIAGNOSTICS_DUMPED" == "0" ]] || return 0
  DIAGNOSTICS_DUMPED=1
  [[ -n "$KUBECONFIG" && -s "$KUBECONFIG" ]] || return 0
  kubectl get pods -A -o wide >&2 || true
  kubectl get events -A --sort-by='.lastTimestamp' >&2 || true
  kubectl -n "$RELEASE_NAMESPACE" logs \
    "deployment/${RELEASE_NAME}-backend" --tail=200 >&2 || true
  kubectl -n "$RELEASE_NAMESPACE" logs \
    "deployment/$GATEWAY_NAME" --tail=200 >&2 || true
  kubectl -n "$RELEASE_NAMESPACE" logs \
    "deployment/${RELEASE_NAME}-tool-worker" --tail=300 >&2 || true
  kubectl -n "$RELEASE_NAMESPACE" logs \
    "deployment/${RELEASE_NAME}-skill-executor" --tail=300 >&2 || true
  for pod in $(kubectl -n "$RUNNER_NAMESPACE" get pods \
    -l 'aurum-ai.component=conversation-runner' -o name 2>/dev/null); do
    kubectl -n "$RUNNER_NAMESPACE" describe "$pod" >&2 || true
    kubectl -n "$RUNNER_NAMESPACE" logs "$pod" --tail=300 >&2 || true
  done
  for pod in $(kubectl -n "$SANDBOX_NAMESPACE" get pods -o name 2>/dev/null); do
    kubectl -n "$SANDBOX_NAMESPACE" describe "$pod" >&2 || true
    kubectl -n "$SANDBOX_NAMESPACE" logs "$pod" --tail=300 >&2 || true
  done
  if [[ -s "$WORK_DIR/ordinary-conversation-id" ]]; then
    conversation_id=$(<"$WORK_DIR/ordinary-conversation-id")
    temporal_cli workflow describe \
      --workflow-id "conversation-$conversation_id" --namespace "$TEMPORAL_NAMESPACE" \
      >&2 || true
  fi
}

fail() {
  log "ERROR: $*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

kill_port_forwards() {
  local pid
  for pid in "${PORT_FORWARD_PIDS[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  done
  PORT_FORWARD_PIDS=()
}

cleanup() {
  local pid service fixture_python recovery_status=0
  for pid in "${BACKGROUND_PIDS[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  done
  if [[ -n "$USER_QUESTIONS_STATE" ]]; then
    fixture_python="$REPO_ROOT/business-backend/.venv/bin/python"
    if [[ ! -f "$USER_QUESTIONS_STATE" ]]; then
      log "user-question recovery journal missing; cannot prove cleanup"
      recovery_status=1
    elif "$fixture_python" "$REPO_ROOT/scripts/user_questions_k8s_e2e.py" cleanup \
      --state "$USER_QUESTIONS_STATE" --marker "$USER_QUESTIONS_MARKER"; then
      "$fixture_python" "$REPO_ROOT/scripts/user_questions_k8s_e2e.py" verify-clean \
        --state "$USER_QUESTIONS_STATE" --marker "$USER_QUESTIONS_MARKER" \
        || recovery_status=$?
    else
      recovery_status=$?
    fi
    if [[ "$recovery_status" == "0" ]]; then
      if rm -- "$USER_QUESTIONS_STATE" && rmdir -- "$USER_QUESTIONS_RECOVERY_DIR"; then
        USER_QUESTIONS_STATE=""
      else
        recovery_status=$?
      fi
    fi
    if [[ "$recovery_status" != "0" ]]; then
      # Stop only this wrapper's local forwards/background processes. Never
      # destroy the history/runtime needed to prove exact-owned recovery.
      kill_port_forwards
      log "user-question recovery unresolved; preserving release, cluster, Temporal namespace and Compose dependencies"
      log "recovery journal: $USER_QUESTIONS_STATE"
      log "recovery work directory: $WORK_DIR (kubeconfig retained)"
      log "restore the original operator environment and port-forwards, then run cleanup and verify-clean; do not recreate the cluster or Temporal namespace"
      return "$recovery_status"
    fi
  fi
  kill_port_forwards

  if [[ "$KEEP_RESOURCES" == "1" ]]; then
    log "KEEP_K8S_TEST_RESOURCES=1; preserving Kubernetes and Compose resources"
  else
    if [[ "$CREATED_FETCH_FIXTURE" == "1" ]]; then
      docker rm -f "$FETCH_FIXTURE_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$CREATED_NETWORK_PROBE" == "1" ]] && [[ -n "$KUBECONFIG" ]]; then
      kubectl -n "$RELEASE_NAMESPACE" delete pod "$NETWORK_PROBE_NAME" \
        --ignore-not-found >/dev/null 2>&1 || true
    fi
    if [[ "$CREATED_RELEASE" == "1" ]] && [[ -n "$KUBECONFIG" ]]; then
      helm uninstall "$RELEASE_NAME" -n "$RELEASE_NAMESPACE" >/dev/null 2>&1 || true
    fi
    if [[ "$CREATED_GATEWAY" == "1" ]] && [[ -n "$KUBECONFIG" ]]; then
      kubectl -n "$RELEASE_NAMESPACE" delete \
        "deployment/$GATEWAY_NAME" "service/$GATEWAY_NAME" "configmap/$GATEWAY_NAME" \
        --ignore-not-found >/dev/null 2>&1 || true
    fi
    if [[ "$CREATED_OWNER_CONFIGMAP" == "1" ]] && [[ -n "$KUBECONFIG" ]]; then
      kubectl -n "$RELEASE_NAMESPACE" delete configmap "$OWNER_CONFIGMAP" \
        --ignore-not-found >/dev/null 2>&1 || true
    fi
    if [[ "$CREATED_RELEASE_NAMESPACE" == "1" ]] && [[ -n "$KUBECONFIG" ]]; then
      kubectl delete namespace "$RELEASE_NAMESPACE" --ignore-not-found \
        >/dev/null 2>&1 || true
    fi
    if [[ "$CREATED_CLUSTER" == "1" ]]; then
      k3d cluster delete "$CLUSTER_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$CREATED_TEMPORAL_NAMESPACE" == "1" ]]; then
      temporal_cli operator namespace delete \
        --namespace "$TEMPORAL_NAMESPACE" --yes >/dev/null 2>&1 || true
    fi
    for service in "${COMPOSE_STARTED_SERVICES[@]}" "${COMPOSE_CREATED_SERVICES[@]}"; do
      [[ -n "$service" ]] \
        && docker compose -p "$COMPOSE_PROJECT" stop "$service" >/dev/null 2>&1 || true
    done
    for service in "${COMPOSE_CREATED_SERVICES[@]}"; do
      docker compose -p "$COMPOSE_PROJECT" rm -f "$service" >/dev/null 2>&1 || true
    done
    if [[ "$CREATED_NETWORK" == "1" ]]; then
      docker network rm "$DOCKER_NETWORK" >/dev/null 2>&1 || true
    fi
  fi

  if [[ -n "$WORK_DIR" && -d "$WORK_DIR" && "$WORK_DIR" == /tmp/* ]]; then
    rm -rf -- "$WORK_DIR"
  fi
}

on_exit() {
  local status=$? cleanup_status=0
  trap - EXIT
  if [[ "$status" != "0" ]]; then
    dump_failure_diagnostics
  fi
  cleanup || cleanup_status=$?
  if [[ "$status" == "0" ]]; then status=$cleanup_status; fi
  exit "$status"
}

trap on_exit EXIT
trap 'exit 130' INT TERM

cluster_exists() {
  k3d cluster list -o json | python3 -c '
import json, sys
name = sys.argv[1]
raise SystemExit(0 if any(item.get("name") == name for item in json.load(sys.stdin)) else 1)
' "$CLUSTER_NAME"
}

compose_service_state() {
  local service="$1" id
  id=$(docker compose -p "$COMPOSE_PROJECT" ps -a -q "$service")
  if [[ -z "$id" ]]; then
    printf 'missing\n'
  elif [[ "$(docker inspect -f '{{.State.Running}}' "$id")" == "true" ]]; then
    printf 'running\n'
  else
    printf 'stopped\n'
  fi
}

start_compose_data_plane() {
  local service state
  if ! docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1; then
    CREATED_NETWORK=1
  fi
  for service in "${COMPOSE_SERVICES[@]}"; do
    state=$(compose_service_state "$service")
    case "$state" in
      missing) COMPOSE_CREATED_SERVICES+=("$service") ;;
      stopped) COMPOSE_STARTED_SERVICES+=("$service") ;;
    esac
  done
  log "Starting the external Compose data plane and reference MCP servers"
  docker compose -p "$COMPOSE_PROJECT" --profile mcp up -d --no-recreate \
    "${COMPOSE_SERVICES[@]}"
}

wait_for_temporal() {
  local i temporal_container
  temporal_container=$(docker compose -p "$COMPOSE_PROJECT" ps -q temporal)
  [[ -n "$temporal_container" ]] || fail "Temporal container is unavailable"
  # Each attempt spins up a throwaway admin-tools container (see temporal_cli),
  # noticeably heavier per-iteration than the old in-place `docker exec` was —
  # 90 attempts keeps roughly the same effective wall-clock budget as the
  # original 60x1s loop had against `auto-setup`.
  for i in $(seq 1 90); do
    if temporal_cli operator namespace describe --namespace default >/dev/null 2>&1; then
      log "Temporal frontend is ready"
      return 0
    fi
    sleep 1
  done
  fail "Temporal frontend did not become ready"
}

ensure_temporal_namespace() {
  local i
  if ! temporal_cli operator namespace describe \
    --namespace "$TEMPORAL_NAMESPACE" >/dev/null 2>&1; then
    temporal_cli operator namespace create \
      --namespace "$TEMPORAL_NAMESPACE" --retention 1d >/dev/null
    CREATED_TEMPORAL_NAMESPACE=1
  fi
  for i in $(seq 1 45); do
    if temporal_cli operator namespace describe \
      --namespace "$TEMPORAL_NAMESPACE" >/dev/null 2>&1; then
      log "Temporal namespace $TEMPORAL_NAMESPACE is ready"
      return 0
    fi
    sleep 1
  done
  fail "Temporal namespace $TEMPORAL_NAMESPACE did not become ready"
}

ensure_cluster() {
  local browser_ports=()
  local browser_k3s_args=()
  if [[ "$ACCEPTANCE_PROFILE" == "user-questions" || "$ACCEPTANCE_PROFILE" == "hermes" ]]; then
    browser_ports=(-p "127.0.0.1:$FRONTEND_PORT:$INGRESS_NODE_PORT@loadbalancer")
    browser_k3s_args=(--k3s-arg '--disable=servicelb@server:*')
  fi
  if cluster_exists; then
    log "Reusing k3d cluster $CLUSTER_NAME"
    docker inspect "k3d-${CLUSTER_NAME}-server-0" \
      --format '{{json .NetworkSettings.Networks}}' \
      | python3 -c '
import json, sys
network = sys.argv[1]
raise SystemExit(0 if network in json.load(sys.stdin) else 1)
' "$DOCKER_NETWORK" \
      || fail "existing k3d cluster is not attached to $DOCKER_NETWORK"
  else
    log "Creating k3d cluster $CLUSTER_NAME on $DOCKER_NETWORK"
    CREATED_CLUSTER=1
    k3d cluster create "$CLUSTER_NAME" \
      --network "$DOCKER_NETWORK" \
      "${browser_ports[@]}" \
      --agents 1 \
      "${browser_k3s_args[@]}" \
      --wait \
      --kubeconfig-update-default=false \
      --kubeconfig-switch-context=false
  fi
  configure_cluster_access
}

configure_ingress_node_port() {
  local i ready=0
  [[ "$ACCEPTANCE_PROFILE" == "user-questions" || "$ACCEPTANCE_PROFILE" == "hermes" ]] || return 0
  for i in $(seq 1 120); do
    if kubectl -n kube-system get service traefik >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  ((ready)) || fail "Traefik service did not become ready"
  kubectl -n kube-system patch service traefik --type merge -p \
    '{"spec":{"type":"NodePort","ports":[{"name":"web","port":80,"protocol":"TCP","targetPort":"web","nodePort":30080},{"name":"websecure","port":443,"protocol":"TCP","targetPort":"websecure"}]}}' >/dev/null
  kubectl -n kube-system rollout status deployment/traefik --timeout=180s
}

configure_cluster_access() {
  k3d kubeconfig get "$CLUSTER_NAME" >"$KUBECONFIG"
  chmod 600 "$KUBECONFIG"
  kubectl cluster-info >/dev/null
}

preflight_existing_cluster_reuse() {
  if cluster_exists; then
    log "Checking existing cluster resources before starting external services"
    configure_cluster_access
    assert_reuse_policy
  fi
}

assert_reuse_policy() {
  local kind existing=""
  [[ "$KEEP_RESOURCES" != "1" ]] || return 0
  if helm status "$RELEASE_NAME" -n "$RELEASE_NAMESPACE" >/dev/null 2>&1; then
    existing="Helm release $RELEASE_NAME"
  fi
  for kind in deployment service configmap; do
    if kubectl -n "$RELEASE_NAMESPACE" get "$kind" "$GATEWAY_NAME" \
      >/dev/null 2>&1; then
      existing="${existing:+$existing, }gateway $kind/$GATEWAY_NAME"
    fi
  done
  if [[ -n "$existing" ]]; then
    DIAGNOSTICS_DUMPED=1
    fail "$existing already exists; set KEEP_K8S_TEST_RESOURCES=1 to reuse and preserve it"
  fi
}

build_and_import_images() {
  local images=(
    aurum-ai-backend:latest
    aurum-ai-frontend:latest
    aurum-ai-admin-ui:latest
    aurum-ai-tool-worker:latest
    aurum-ai-skill-executor:latest
    aurum-ai-conversation-runner:latest
    aurum-ai-skill-sandbox:latest
  )
  log "Building seven application/runtime images"
  docker build -t "${images[0]}" "$REPO_ROOT/business-backend"
  docker build -t "${images[1]}" "$REPO_ROOT/business-ui"
  docker build -t "${images[2]}" "$REPO_ROOT/admin-ui"
  docker build -t "${images[3]}" "$REPO_ROOT/tool-worker"
  docker build -t "${images[4]}" "$REPO_ROOT/skill-executor"
  docker build -t "${images[5]}" "$REPO_ROOT/conversation-runner"
  docker build -t "${images[6]}" "$REPO_ROOT/skill-sandbox"
  if [[ "$ACCEPTANCE_PROFILE" == "hermes" ]]; then
    local extra image context
    for extra in aurum-ai-web-egress:latest:infra/web-egress \
      aurum-ai-hermes-sandbox:latest:hermes-sandbox aurum-ai-hermes-preview:latest:hermes-preview; do
      image=${extra%:*}
      context=${extra##*:}
      docker build -t "$image" "$REPO_ROOT/$context"
      images+=("$image")
    done
  fi
  log "Importing application/runtime images into k3d"
  k3d image import -c "$CLUSTER_NAME" "${images[@]}"
}

start_fetch_fixture() {
  local fixture_dir="$WORK_DIR/fetch-fixture" i
  mkdir -p "$fixture_dir"
  cat >"$fixture_dir/index.html" <<EOF
<!doctype html>
<html><head><title>$MCP_FETCH_EXPECTED_TITLE</title></head>
<body><h1>$MCP_FETCH_EXPECTED_TITLE</h1></body></html>
EOF
  log "Starting owned MCP fetch fixture $FETCH_FIXTURE_NAME"
  docker run -d --name "$FETCH_FIXTURE_NAME" \
    --network "$DOCKER_NETWORK" \
    --label "$OWNER_LABEL=true" \
    --entrypoint python \
    -v "$fixture_dir:/fixture:ro" \
    aurum-ai-backend:latest \
    -m http.server 8080 --bind 0.0.0.0 --directory /fixture >/dev/null
  CREATED_FETCH_FIXTURE=1
  for i in $(seq 1 30); do
    if docker exec "$FETCH_FIXTURE_NAME" python -c \
      'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/index.html").read()' \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "MCP fetch fixture did not become ready"
}

container_ip() {
  local container="$1" ip
  ip=$(docker inspect -f \
    "{{(index .NetworkSettings.Networks \"$DOCKER_NETWORK\").IPAddress}}" "$container")
  [[ -n "$ip" ]] || fail "container $container has no address on $DOCKER_NETWORK"
  printf '%s\n' "$ip"
}

ensure_owned_namespace() {
  local owned
  if kubectl get namespace "$RELEASE_NAMESPACE" >/dev/null 2>&1; then
    owned=$(kubectl get namespace "$RELEASE_NAMESPACE" \
      -o "jsonpath={.metadata.labels['$OWNER_LABEL']}" 2>/dev/null || true)
    [[ "$owned" == "true" ]] \
      || fail "namespace $RELEASE_NAMESPACE exists without $OWNER_LABEL=true"
  else
    kubectl create namespace "$RELEASE_NAMESPACE"
    CREATED_RELEASE_NAMESPACE=1
    kubectl label namespace "$RELEASE_NAMESPACE" "$OWNER_LABEL=true"
  fi

  if kubectl -n "$RELEASE_NAMESPACE" get configmap "$OWNER_CONFIGMAP" >/dev/null 2>&1; then
    owned=$(kubectl -n "$RELEASE_NAMESPACE" get configmap "$OWNER_CONFIGMAP" \
      -o "jsonpath={.metadata.labels['$OWNER_LABEL']}" 2>/dev/null || true)
    [[ "$owned" == "true" ]] || fail "ownership ConfigMap has an unexpected owner"
  else
    kubectl -n "$RELEASE_NAMESPACE" create configmap "$OWNER_CONFIGMAP" \
      --from-literal=cluster="$CLUSTER_NAME" \
      --from-literal=release="$RELEASE_NAME"
    CREATED_OWNER_CONFIGMAP=1
    kubectl -n "$RELEASE_NAMESPACE" label configmap "$OWNER_CONFIGMAP" "$OWNER_LABEL=true"
  fi
}

deploy_gateway() {
  local kind owned gateway_config gateway_labeled gateway_manifest existing_count=0
  for kind in deployment service configmap; do
    if kubectl -n "$RELEASE_NAMESPACE" get "$kind" "$GATEWAY_NAME" >/dev/null 2>&1; then
      existing_count=$((existing_count + 1))
      owned=$(kubectl -n "$RELEASE_NAMESPACE" get "$kind" "$GATEWAY_NAME" \
        -o "jsonpath={.metadata.labels['$OWNER_LABEL']}" 2>/dev/null || true)
      [[ "$owned" == "true" ]] \
        || fail "gateway $kind exists without ownership label"
    fi
  done
  if [[ "$existing_count" != "0" && "$existing_count" != "3" ]]; then
    fail "gateway resources are only partially present; refusing to overwrite them"
  fi

  gateway_config="$WORK_DIR/gateway-config.yaml"
  gateway_labeled="$WORK_DIR/gateway-config-labeled.yaml"
  gateway_manifest="$WORK_DIR/gateway-workload.yaml"
  kubectl -n "$RELEASE_NAMESPACE" create configmap "$GATEWAY_NAME" \
    --from-file=k8s-test-gateway.py="$REPO_ROOT/scripts/k8s-test-gateway.py" \
    --dry-run=client -o yaml >"$gateway_config"
  kubectl label --local -f "$gateway_config" "$OWNER_LABEL=true" -o yaml >"$gateway_labeled"
  kubectl apply -f "$gateway_labeled"
  if [[ "$existing_count" == "0" ]]; then
    CREATED_GATEWAY=1
  fi
  cat >"$gateway_manifest" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $GATEWAY_NAME
  namespace: $RELEASE_NAMESPACE
  labels:
    $OWNER_LABEL: "true"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: $GATEWAY_NAME
  template:
    metadata:
      labels:
        app: $GATEWAY_NAME
        $OWNER_LABEL: "true"
    spec:
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: gateway
          image: aurum-ai-backend:latest
          imagePullPolicy: IfNotPresent
          command: ["python", "/gateway/k8s-test-gateway.py"]
          env:
            - name: TEST_GATEWAY_MODEL_ID
              value: $TEST_GATEWAY_MODEL_ID_YAML
          ports:
            - name: http
              containerPort: 8080
          readinessProbe:
            httpGet:
              path: /health
              port: http
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: source
              mountPath: /gateway
              readOnly: true
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: source
          configMap:
            name: $GATEWAY_NAME
        - name: tmp
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: $GATEWAY_NAME
  namespace: $RELEASE_NAMESPACE
  labels:
    $OWNER_LABEL: "true"
spec:
  selector:
    app: $GATEWAY_NAME
  ports:
    - name: http
      port: 8080
      targetPort: http
EOF
  kubectl apply -f "$gateway_manifest"
  kubectl -n "$RELEASE_NAMESPACE" rollout restart "deployment/$GATEWAY_NAME"
  kubectl -n "$RELEASE_NAMESPACE" rollout status \
    "deployment/$GATEWAY_NAME" --timeout=120s
}

write_helm_values() {
  local docker_subnet="$1"
  local browser_api="http://127.0.0.1:$BACKEND_PORT/v1" browser_ingress=false
  if [[ "$ACCEPTANCE_PROFILE" == "user-questions" || "$ACCEPTANCE_PROFILE" == "hermes" ]]; then
    browser_api="http://backend.127.0.0.1.nip.io:$FRONTEND_PORT/v1"
    browser_ingress=true
  fi
  cat >"$WORK_DIR/k8s-smoke-values.yaml" <<EOF
image:
  pullPolicy: IfNotPresent
  tag: latest
secrets:
  appSecretKey: $APP_SECRET_KEY
  gatewayApiKey: k8s-smoke-test-key
  # The chart's own defaults match a fresh Compose data plane; a stack started with
  # generated secrets (bootstrap-dev-env.sh) needs its own values here.
  databaseRolePassword: "${K8S_TEST_DATABASE_ROLE_PASSWORD:-aurum_ai_app}"
  migrationDatabasePassword: "${K8S_TEST_MIGRATION_DATABASE_PASSWORD:-postgres}"
  redisPassword: "${K8S_TEST_REDIS_PASSWORD:-redis_dev_password}"
  s3SecretKey: "${K8S_TEST_S3_SECRET_KEY:-miniosecret}"
ingress:
  enabled: $browser_ingress
  externalPort: $FRONTEND_PORT
backend:
  replicaCount: 1
  debug: true
  # The smoke shares Redis with the Compose data plane, so use an explicit
  # test-only budget that is independent from earlier local E2E requests.
  rateLimitPerMinute: $K8S_RATE_LIMIT_PER_MINUTE
  rateLimitPerMinuteStream: $K8S_RATE_LIMIT_PER_MINUTE_STREAM
  autoscaling:
    enabled: false
  userQuestionsEnabled: $([[ "$ACCEPTANCE_PROFILE" == "user-questions" ]] && printf true || printf false)
  evaluator:
    enabled: false
backendAdmin:
  replicaCount: 1
  debug: true
  rateLimitPerMinute: $K8S_RATE_LIMIT_PER_MINUTE
  rateLimitPerMinuteStream: $K8S_RATE_LIMIT_PER_MINUTE_STREAM
  autoscaling:
    enabled: false
frontend:
  replicaCount: 1
  autoscaling:
    enabled: false
  runtimeConfig:
    apiUrl: $browser_api
    keycloakUrl: $KEYCLOAK_PUBLIC_URL
adminUi:
  replicaCount: 1
  autoscaling:
    enabled: false
toolWorker:
  replicaCount: 1
skillExecutor:
  replicaCount: 1
runtimeNamespaces:
  create: true
  runner: $RUNNER_NAMESPACE
  sandbox: $SANDBOX_NAMESPACE
  labels:
    $OWNER_LABEL: "true"
runnerRuntime:
  type: kubernetes
  idleReapSeconds: $RUNNER_ACTIVE_IDLE_SECONDS
  reapIntervalSeconds: $RUNNER_REAP_SECONDS
sandboxRuntime:
  type: kubernetes
  sessionTtlSeconds: 90
  sessionIdleTtlSeconds: 30
  reapIntervalSeconds: 2
external:
  postgres:
    host: $POSTGRES_IP
  redis:
    host: $REDIS_IP
  keycloak:
    url: http://$KEYCLOAK_IP:8080
    issuer: $KEYCLOAK_PUBLIC_URL/realms/aurum-ai
  temporal:
    host: $TEMPORAL_IP:7233
    namespace: $TEMPORAL_NAMESPACE
  minio:
    endpoint: http://$MINIO_IP:9000
  langfuse:
    enabled: false
  llmGateway:
    url: http://$GATEWAY_NAME:8080
    verifySsl: false
networkPolicies:
  runner:
    destinations:
      - cidr: $docker_subnet
        ports:
          - protocol: TCP
            port: 6379
          - protocol: TCP
            port: 7233
EOF
  if [[ "$ACCEPTANCE_PROFILE" == "hermes" ]]; then
    # Hermes on the real chart: its own namespace and guard, short lifetimes so
    # an idle sandbox is snapshotted within the run.
    cat >>"$WORK_DIR/k8s-smoke-values.yaml" <<EOF
hermes:
  enabled: true
  namespace: $HERMES_NAMESPACE
  egress:
    maxMode: packages
    recheckSeconds: 5
  sandbox:
    idleTtlSeconds: $HERMES_IDLE_SECONDS
    cpuRequest: 100m
    memoryRequest: 256Mi
EOF
  fi
}

render_and_install() {
  local values="$WORK_DIR/k8s-smoke-values.yaml" rendered="$WORK_DIR/rendered.yaml"
  log "Validating rendered Helm resources"
  bash "$CHART/tests/render-test.sh"
  helm lint "$CHART" -f "$values"
  helm template "$RELEASE_NAME" "$CHART" \
    -n "$RELEASE_NAMESPACE" -f "$values" >"$rendered"
  kubeconform -strict -summary "$rendered"

  if helm status "$RELEASE_NAME" -n "$RELEASE_NAMESPACE" >/dev/null 2>&1; then
    log "Upgrading existing smoke-owned Helm release"
    helm upgrade "$RELEASE_NAME" "$CHART" -n "$RELEASE_NAMESPACE" -f "$values" --wait \
      --timeout 8m
  else
    log "Installing Helm release"
    CREATED_RELEASE=1
    helm install "$RELEASE_NAME" "$CHART" -n "$RELEASE_NAMESPACE" -f "$values" --wait \
      --timeout 8m
  fi

  wait_for_rollouts
  log "Running a no-op Helm upgrade acceptance"
  helm upgrade "$RELEASE_NAME" "$CHART" -n "$RELEASE_NAMESPACE" -f "$values" --wait \
    --timeout 8m
  wait_for_rollouts
}

wait_for_rollouts() {
  local component
  for component in backend backend-admin frontend admin-ui tool-worker skill-executor; do
    kubectl -n "$RELEASE_NAMESPACE" rollout status \
      "deployment/${RELEASE_NAME}-${component}" --timeout=180s
  done
}

restart_permanent_workloads() {
  local component
  log "Restarting all permanent workloads after importing local images"
  for component in backend backend-admin frontend admin-ui tool-worker skill-executor; do
    kubectl -n "$RELEASE_NAMESPACE" rollout restart \
      "deployment/${RELEASE_NAME}-${component}"
  done
  wait_for_rollouts
}

start_port_forward() {
  local service="$1" mapping="$2" logfile="$3"
  kubectl -n "$RELEASE_NAMESPACE" port-forward "service/$service" "$mapping" \
    >"$logfile" 2>&1 &
  PORT_FORWARD_PIDS+=("$!")
}

restart_backend_port_forward() {
  # install_short_runner_ttl rolls the backend Deployment. kubectl
  # port-forward selects one backing Pod and exits when that Pod disappears,
  # even though the Service and replacement Pod are healthy.
  kill_port_forwards
  start_port_forward "${RELEASE_NAME}-backend" "$BACKEND_PORT:8000" \
    "$WORK_DIR/backend-port-forward.log"
  start_port_forward "${RELEASE_NAME}-backend-admin" "$ADMIN_PORT:8000" \
    "$WORK_DIR/admin-port-forward.log"
  start_port_forward "${RELEASE_NAME}-skill-executor" "$SKILL_EXECUTOR_PORT:8090" \
    "$WORK_DIR/skill-executor-port-forward.log"
  start_port_forward "$GATEWAY_NAME" "$GATEWAY_PORT:8080" \
    "$WORK_DIR/gateway-port-forward.log"
  wait_for_url "http://127.0.0.1:$BACKEND_PORT/ready" "backend after TTL rollout"
  wait_for_url "http://127.0.0.1:$ADMIN_PORT/ready" "backend-admin after TTL rollout"
  wait_for_url "http://127.0.0.1:$SKILL_EXECUTOR_PORT/ready" \
    "skill-executor after TTL rollout"
  wait_for_url "http://127.0.0.1:$GATEWAY_PORT/health" \
    "deterministic gateway after TTL rollout"
}

run_user_questions_acceptance() {
  local fixture_python="$REPO_ROOT/business-backend/.venv/bin/python"
  USER_QUESTIONS_RECOVERY_DIR=$(mktemp -d "${TMPDIR:-/tmp}/user-questions-k8s-e2e.XXXXXXXX")
  chmod 700 "$USER_QUESTIONS_RECOVERY_DIR"
  USER_QUESTIONS_STATE="$USER_QUESTIONS_RECOVERY_DIR/state.json"
  export E2E_USER_QUESTIONS=true
  export E2E_USER_QUESTIONS_STATE="$USER_QUESTIONS_STATE"
  export E2E_USER_QUESTIONS_MARKER="$USER_QUESTIONS_MARKER"
  export E2E_USER_QUESTIONS_FIXTURE_SCRIPT="$REPO_ROOT/scripts/user_questions_k8s_e2e.py"
  export E2E_USER_QUESTIONS_PYTHON="$fixture_python"
  export E2E_BUSINESS_UI_URL="http://chat.127.0.0.1.nip.io:$FRONTEND_PORT"
  export E2E_BUSINESS_BACKEND_URL="http://backend.127.0.0.1.nip.io:$FRONTEND_PORT"
  export E2E_ADMIN_BACKEND_URL="http://backend-admin.127.0.0.1.nip.io:$FRONTEND_PORT"
  export E2E_KEYCLOAK_URL="$KEYCLOAK_PUBLIC_URL"
  export E2E_POSTGRES_CONTAINER="aurum-ai-postgres"
  export K8S_QUESTION_RUNNER_NAMESPACE="$RUNNER_NAMESPACE"
  export K8S_QUESTION_RELEASE_NAME="$RELEASE_NAME"
  export K8S_QUESTION_TEMPORAL_HOST="127.0.0.1:7233"
  export K8S_QUESTION_TEMPORAL_NAMESPACE="$TEMPORAL_NAMESPACE"
  export QUESTION_ACCEPTANCE_KIND=short

  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/aurum-ai-uv-cache}" \
    uv sync --project "$REPO_ROOT/business-backend" --frozen
  "$fixture_python" "$REPO_ROOT/scripts/user_questions_k8s_e2e.py" prepare \
    --state "$USER_QUESTIONS_STATE" --marker "$USER_QUESTIONS_MARKER"
  (
    cd "$REPO_ROOT/business-ui"
    npm exec -- playwright test e2e/user-questions.real.spec.ts \
      --project=chromium --workers=1 --trace=off
  )
  "$fixture_python" "$REPO_ROOT/scripts/user_questions_k8s_e2e.py" cleanup \
    --state "$USER_QUESTIONS_STATE" --marker "$USER_QUESTIONS_MARKER"
  "$fixture_python" "$REPO_ROOT/scripts/user_questions_k8s_e2e.py" verify-clean \
    --state "$USER_QUESTIONS_STATE" --marker "$USER_QUESTIONS_MARKER"
  rm -- "$USER_QUESTIONS_STATE"
  rmdir -- "$USER_QUESTIONS_RECOVERY_DIR"
  USER_QUESTIONS_STATE=""
}

wait_for_url() {
  local url="$1" name="$2" timeout="${3:-120}"
  local i
  for i in $(seq 1 "$timeout"); do
    if curl -sf "$url" >/dev/null 2>&1; then
      log "$name is ready at $url"
      return 0
    fi
    sleep 1
  done
  fail "$name did not become ready at $url"
}

get_token() {
  curl -sf -X POST \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=password" -d "client_id=business-ui" \
    -d "username=$TEST_USERNAME" -d "password=$TEST_PASSWORD" \
    "$KEYCLOAK_PUBLIC_URL/realms/aurum-ai/protocol/openid-connect/token" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])"
}

create_conversation() {
  local token="$1"
  curl -sf -X POST "http://127.0.0.1:$BACKEND_PORT/v1/conversations" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d '{"title":"Kubernetes runner reuse","model":"k8s-test"}' \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['id'])"
}

ordinary_turn() {
  local token="$1" conversation_id="$2" text="$3" output="$4" code curl_status=0
  code=$(curl -sS -N --max-time 180 -o "$output" -w '%{http_code}' \
    -X POST "http://127.0.0.1:$BACKEND_PORT/v1/chat/stream" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "{\"conversation_id\":\"$conversation_id\",\"model\":\"k8s-test\",\"messages\":[{\"role\":\"user\",\"content\":\"$text\"}]}" \
    ) || curl_status=$?
  [[ "$curl_status" == "0" ]] \
    || { cat "$output" >&2; fail "ordinary chat curl failed with status $curl_status"; }
  [[ "$code" == "200" ]] || { cat "$output" >&2; fail "ordinary chat returned HTTP $code"; }
  python3 "$REPO_ROOT/scripts/validate-smoke-sse.py" ordinary "$output" \
    --expected-marker "deterministic Kubernetes" \
    || { cat "$output" >&2; fail "ordinary chat SSE validation failed"; }
}

runner_uid() {
  local conversation_id="$1" uids="" runner_count i
  local -a runner_uids=()
  for i in $(seq 1 60); do
    uids=$(kubectl -n "$RUNNER_NAMESPACE" get pods \
      -l "aurum-ai.conversation=$conversation_id" \
      -o jsonpath='{range .items[*]}{.metadata.uid}{" "}{end}' 2>/dev/null || true)
    runner_uids=()
    read -r -a runner_uids <<<"$uids" || true
    runner_count=${#runner_uids[@]}
    if ((runner_count > 1)); then
      fail "$runner_count runner Pods found for conversation $conversation_id"
    fi
    if ((runner_count == 1)); then
      printf '%s\n' "${runner_uids[0]}"
      return 0
    fi
    sleep 1
  done
  fail "runner Pod for conversation $conversation_id was not found"
}

assert_skill_executor_network_isolation() {
  log "Checking that only tool-worker can reach skill-executor"
  kubectl -n "$RELEASE_NAMESPACE" run "$NETWORK_PROBE_NAME" \
    --image=aurum-ai-backend:latest --image-pull-policy=Never \
    --restart=Never \
    --labels="app.kubernetes.io/instance=$RELEASE_NAME,app.kubernetes.io/name=network-probe" \
    --command -- python -c '
import socket
import sys

address = socket.gethostbyname(sys.argv[1])
try:
    with socket.create_connection((address, 8090), timeout=3):
        raise SystemExit("unexpected skill-executor access")
except OSError:
    raise SystemExit(0)
' "${RELEASE_NAME}-skill-executor"
  CREATED_NETWORK_PROBE=1
  if ! kubectl -n "$RELEASE_NAMESPACE" wait \
    --for=jsonpath='{.status.phase}'=Succeeded "pod/$NETWORK_PROBE_NAME" \
    --timeout=30s; then
    kubectl -n "$RELEASE_NAMESPACE" logs "$NETWORK_PROBE_NAME" >&2 || true
    fail "non-tool-worker Pod unexpectedly reached skill-executor"
  fi
  kubectl -n "$RELEASE_NAMESPACE" delete pod "$NETWORK_PROBE_NAME" --wait=true
  CREATED_NETWORK_PROBE=0
  log "Non-tool-worker access to skill-executor is denied"
}

assert_runner_reuse() {
  local token conversation_id first_uid second_uid
  token=$(get_token)
  conversation_id=$(create_conversation "$token")
  printf '%s\n' "$conversation_id" >"$WORK_DIR/ordinary-conversation-id"
  ordinary_turn "$token" "$conversation_id" "ordinary Kubernetes turn one" \
    "$WORK_DIR/ordinary-1.sse"
  first_uid=$(runner_uid "$conversation_id")
  ordinary_turn "$token" "$conversation_id" "ordinary Kubernetes turn two" \
    "$WORK_DIR/ordinary-2.sse"
  second_uid=$(runner_uid "$conversation_id")
  [[ "$first_uid" == "$second_uid" ]] \
    || fail "runner UID changed between turns: $first_uid -> $second_uid"
  log "Runner UID $first_uid was reused for two turns"
}

run_skill_smokes() {
  local captured=0 code_pid code_status i
  log "Running MCP acceptance through backend-admin registration"
  BASE_URL="http://127.0.0.1:$BACKEND_PORT" \
  ADMIN_BASE_URL="http://127.0.0.1:$ADMIN_PORT" \
  KEYCLOAK_URL="$KEYCLOAK_PUBLIC_URL" \
  ADMIN_CLIENT_ID="admin-ui" \
  MCP_SETUP_MODE="admin-api" \
  MCP_TEST_MODEL="k8s-test" \
  MCP_EVERYTHING_ENDPOINT="http://$MCP_EVERYTHING_IP:3001/sse" \
  MCP_FETCH_ENDPOINT="http://$MCP_FETCH_IP:8080/sse" \
  MCP_FETCH_TARGET_URL="$MCP_FETCH_TARGET_URL" \
  MCP_FETCH_EXPECTED_TITLE="$MCP_FETCH_EXPECTED_TITLE" \
    "$REPO_ROOT/smoke-test-mcp.sh"

  log "Running multi-round code-skill acceptance"
  (
    BASE_URL="http://127.0.0.1:$BACKEND_PORT" \
    ADMIN_BASE_URL="http://127.0.0.1:$ADMIN_PORT" \
    KEYCLOAK_URL="$KEYCLOAK_PUBLIC_URL" \
    ADMIN_CLIENT_ID="admin-ui" \
    CODE_SKILL_TEST_MODEL="k8s-test" \
    SKILL_EXECUTOR_HEALTH_URL="http://127.0.0.1:$SKILL_EXECUTOR_PORT/ready" \
      "$REPO_ROOT/smoke-test-code-skill.sh"
  ) &
  code_pid=$!
  BACKGROUND_PIDS+=("$code_pid")
  for i in $(seq 1 1200); do
    if kubectl -n "$SANDBOX_NAMESPACE" get pods \
      -l "app.kubernetes.io/instance=$RELEASE_NAME,aurum-ai.component=skill-sandbox" \
      --no-headers 2>/dev/null | rg -q .; then
      kubectl -n "$SANDBOX_NAMESPACE" get pods \
        -l "app.kubernetes.io/instance=$RELEASE_NAME,aurum-ai.component=skill-sandbox" \
        -o yaml >"$WORK_DIR/live-sandbox.yaml"
      captured=1
      break
    fi
    kill -0 "$code_pid" >/dev/null 2>&1 || break
    sleep 0.1
  done
  if wait "$code_pid"; then
    code_status=0
  else
    code_status=$?
  fi
  BACKGROUND_PIDS=()
  [[ "$code_status" == "0" ]] || return "$code_status"
  [[ "$captured" == "1" ]] || fail "no live sandbox Pod was observed during code-skill smoke"
}

assert_sandboxes_disappear() {
  local count i
  for i in $(seq 1 60); do
    count=$(kubectl -n "$SANDBOX_NAMESPACE" get pods \
      -l "app.kubernetes.io/instance=$RELEASE_NAME,aurum-ai.component=skill-sandbox" \
      --no-headers 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$count" == "0" ]]; then
      log "All smoke-test sandbox Pods disappeared"
      return 0
    fi
    sleep 1
  done
  kubectl -n "$SANDBOX_NAMESPACE" get pods >&2 || true
  fail "sandbox Pods remained after code-skill completion"
}

assert_runner_reaped() {
  local conversation_id count i
  conversation_id=$(<"$WORK_DIR/ordinary-conversation-id")
  for i in $(seq 1 $((RUNNER_IDLE_SECONDS + 60))); do
    count=$(kubectl -n "$RUNNER_NAMESPACE" get pods \
      -l "aurum-ai.conversation=$conversation_id" --no-headers 2>/dev/null \
      | wc -l | tr -d ' ')
    if [[ "$count" == "0" ]]; then
      log "Idle runner was deleted with the short test TTL"
      return 0
    fi
    sleep 1
  done
  fail "runner for $conversation_id was not reaped"
}

install_short_runner_ttl() {
  log "Upgrading the release with a short runner idle TTL"
  helm upgrade "$RELEASE_NAME" "$CHART" -n "$RELEASE_NAMESPACE" \
    -f "$WORK_DIR/k8s-smoke-values.yaml" \
    --set "runnerRuntime.idleReapSeconds=$RUNNER_IDLE_SECONDS" \
    --set "runnerRuntime.reapIntervalSeconds=$RUNNER_REAP_SECONDS" \
    --wait --timeout 8m
  kubectl -n "$RELEASE_NAMESPACE" rollout status \
    "deployment/${RELEASE_NAME}-backend" --timeout=180s
}

assert_rbac_denials() {
  local backend_sa="${RELEASE_NAME}-backend"
  local executor_sa="${RELEASE_NAME}-skill-executor"
  local answer
  answer=$(kubectl auth can-i create pods --subresource=exec -n "$RUNNER_NAMESPACE" \
    --as="system:serviceaccount:${RELEASE_NAMESPACE}:${backend_sa}" || true)
  [[ "$answer" == "no" ]] || fail "backend unexpectedly can exec runner Pods"
  answer=$(kubectl auth can-i get pods -n "$RUNNER_NAMESPACE" \
    --as="system:serviceaccount:${RELEASE_NAMESPACE}:${executor_sa}" || true)
  [[ "$answer" == "no" ]] || fail "skill-executor unexpectedly can access runner Pods"
  log "RBAC negative permission checks passed"
}

assert_safe_specs() {
  local rendered="$WORK_DIR/rendered.yaml" live="$WORK_DIR/live-pods.yaml" namespace
  : >"$live"
  for namespace in "$RELEASE_NAMESPACE" "$RUNNER_NAMESPACE" "$SANDBOX_NAMESPACE"; do
    kubectl -n "$namespace" get pods -o yaml >>"$live"
  done
  if [[ -s "$WORK_DIR/live-sandbox.yaml" ]]; then
    cat "$WORK_DIR/live-sandbox.yaml" >>"$live"
  fi
  if rg -n '/var/run/docker\.sock|privileged:[[:space:]]*true' "$rendered" "$live"; then
    fail "Docker socket or privileged mode found in rendered/live specs"
  fi
  log "Rendered and live specs contain no Docker socket or privileged mode"
}

run_authorization_acceptance() {
  BASE_URL="http://127.0.0.1:$BACKEND_PORT" \
  ADMIN_BASE_URL="http://127.0.0.1:$ADMIN_PORT" \
  KEYCLOAK_URL="$KEYCLOAK_PUBLIC_URL" \
  GROUPS_SMOKE_MODEL="k8s-test" \
  AUTHORIZATION_E2E_MODE="v2" \
  AUTHORIZATION_E2E_RUNTIME="kubernetes" \
  AUTHORIZATION_E2E_DB_CONTAINER="aurum-ai-postgres" \
  AUTHORIZATION_E2E_KUBECONFIG="$KUBECONFIG" \
  AUTHORIZATION_E2E_RUNNER_NAMESPACE="$RUNNER_NAMESPACE" \
  AUTHORIZATION_E2E_SESSION_SECRET="$APP_SECRET_KEY" \
    "$REPO_ROOT/smoke-test-groups.sh"
  log "Kubernetes authorization live-revoke acceptance passed"
}

run_multi_agent_acceptance() {
  BASE_URL="http://127.0.0.1:$BACKEND_PORT" \
  ADMIN_BASE_URL="http://127.0.0.1:$ADMIN_PORT" \
  KEYCLOAK_URL="$KEYCLOAK_PUBLIC_URL" \
  MULTI_AGENT_E2E_RUNTIME="kubernetes" \
  MULTI_AGENT_E2E_DB_CONTAINER="aurum-ai-postgres" \
  MULTI_AGENT_E2E_KUBECONFIG="$KUBECONFIG" \
  MULTI_AGENT_E2E_RUNNER_NAMESPACE="$RUNNER_NAMESPACE" \
  MULTI_AGENT_E2E_MODEL="k8s-test" \
    "$REPO_ROOT/smoke-test-multi-agent.sh"
  log "Kubernetes multi-agent acceptance passed"
}

run_hermes_acceptance() {
  local mode
  export K8S_HERMES_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
  export K8S_HERMES_ADMIN_URL="http://127.0.0.1:$ADMIN_PORT"
  export K8S_HERMES_KEYCLOAK_URL="$KEYCLOAK_PUBLIC_URL"
  export K8S_HERMES_USER="$TEST_USERNAME" K8S_HERMES_PASSWORD="$TEST_PASSWORD"
  export K8S_HERMES_NAMESPACE="$HERMES_NAMESPACE" K8S_HERMES_RELEASE="$RELEASE_NAME"
  export K8S_HERMES_PLATFORM_NAMESPACE="$RELEASE_NAMESPACE"
  export K8S_HERMES_IDLE_SECONDS="$HERMES_IDLE_SECONDS"
  export K8S_HERMES_WORK_DIR="$WORK_DIR"
  python3 "$REPO_ROOT/scripts/hermes-k8s-platform-acceptance.py" deterministic \
    || fail "the deterministic Hermes acceptance failed"
  if [[ -n "${K8S_TEST_REAL_GATEWAY_URL:-}" && -n "${K8S_TEST_REAL_GATEWAY_API_KEY:-}" ]]; then
    log "Switching the release to the real model gateway"
    helm upgrade "$RELEASE_NAME" "$CHART" -n "$RELEASE_NAMESPACE" --reuse-values --wait \
      --timeout 8m \
      --set "external.llmGateway.url=$K8S_TEST_REAL_GATEWAY_URL" \
      --set external.llmGateway.verifySsl=true \
      --set-string "secrets.gatewayApiKey=$K8S_TEST_REAL_GATEWAY_API_KEY" >/dev/null
    wait_for_rollouts
    restart_backend_port_forward
    wait_for_url "http://127.0.0.1:$BACKEND_PORT/ready" "backend readiness"
    export K8S_HERMES_REAL_MODEL="${K8S_TEST_REAL_MODEL:-deepseek-v4-flash}"
    python3 "$REPO_ROOT/scripts/hermes-k8s-platform-acceptance.py" real \
      || fail "the real-model Hermes acceptance failed"
  else
    log "No real gateway given (K8S_TEST_REAL_GATEWAY_URL and K8S_TEST_REAL_GATEWAY_API_KEY): skipping the real-model turn"
  fi
}

main() {
  local command docker_subnet
  for command in docker k3d kubectl helm kubeconform curl flock npm python3 rg uv; do
    require_command "$command"
  done
  docker compose version >/dev/null
  docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable"
  [[ "$KEEP_RESOURCES" == "0" || "$KEEP_RESOURCES" == "1" ]] \
    || fail "KEEP_K8S_TEST_RESOURCES must be 0 or 1"
  [[ "$ACCEPTANCE_PROFILE" == "full" || "$ACCEPTANCE_PROFILE" == "user-questions" \
    || "$ACCEPTANCE_PROFILE" == "hermes" ]] \
    || fail "K8S_TEST_ACCEPTANCE must be full, user-questions or hermes"
  if [[ "$ACCEPTANCE_PROFILE" == "user-questions" ]]; then
    [[ "${QUESTION_ACCEPTANCE_KIND:-short}" == "short" ]] \
      || fail "Kubernetes user-question acceptance supports only short mode"
    exec 8>"${TMPDIR:-/tmp}/aurum-ai-user-questions-e2e.lock"
    flock -n 8 || fail "another user-question acceptance is running"
    cluster_exists && fail "user-question acceptance requires a fresh owned k3d cluster"
    USER_QUESTIONS_MARKER="user-questions-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
    TEST_GATEWAY_MODEL_ID="$USER_QUESTIONS_MARKER-model"
  else
    TEST_GATEWAY_MODEL_ID="${TEST_GATEWAY_MODEL_ID:-k8s-test}"
  fi
  export TEST_GATEWAY_MODEL_ID
  TEST_GATEWAY_MODEL_ID_YAML=$(python3 -c \
    'import json, os; print(json.dumps(os.environ["TEST_GATEWAY_MODEL_ID"]))')

  WORK_DIR=$(mktemp -d /tmp/aurum-ai-k8s-smoke.XXXXXX)
  KUBECONFIG="$WORK_DIR/kubeconfig"
  export KUBECONFIG
  cd "$REPO_ROOT"

  preflight_existing_cluster_reuse
  start_compose_data_plane
  wait_for_temporal
  POSTGRES_IP=$(container_ip aurum-ai-postgres)
  REDIS_IP=$(container_ip aurum-ai-redis)
  KEYCLOAK_IP=$(container_ip aurum-ai-keycloak)
  TEMPORAL_IP=$(container_ip aurum-ai-temporal)
  MINIO_IP=$(container_ip aurum-ai-minio)
  MCP_EVERYTHING_IP=$(container_ip aurum-ai-mcp-everything)
  MCP_FETCH_IP=$(container_ip aurum-ai-mcp-fetch)
  ensure_cluster
  configure_ingress_node_port
  assert_reuse_policy
  ensure_temporal_namespace
  build_and_import_images
  start_fetch_fixture
  ensure_owned_namespace
  deploy_gateway

  docker_subnet=$(docker network inspect "$DOCKER_NETWORK" \
    --format '{{(index .IPAM.Config 0).Subnet}}')
  [[ -n "$docker_subnet" ]] || fail "could not determine $DOCKER_NETWORK subnet"
  write_helm_values "$docker_subnet"
  render_and_install
  restart_permanent_workloads

  start_port_forward "${RELEASE_NAME}-backend" "$BACKEND_PORT:8000" \
    "$WORK_DIR/backend-port-forward.log"
  start_port_forward "${RELEASE_NAME}-backend-admin" "$ADMIN_PORT:8000" \
    "$WORK_DIR/admin-port-forward.log"
  start_port_forward "${RELEASE_NAME}-skill-executor" "$SKILL_EXECUTOR_PORT:8090" \
    "$WORK_DIR/skill-executor-port-forward.log"
  start_port_forward "$GATEWAY_NAME" "$GATEWAY_PORT:8080" \
    "$WORK_DIR/gateway-port-forward.log"

  wait_for_url "http://127.0.0.1:$GATEWAY_PORT/health" "deterministic gateway"
  wait_for_url "$KEYCLOAK_PUBLIC_URL/realms/aurum-ai" "public Keycloak realm"
  wait_for_url "http://127.0.0.1:$BACKEND_PORT/ready" "backend readiness"
  wait_for_url "http://127.0.0.1:$ADMIN_PORT/ready" "backend-admin readiness"
  wait_for_url "http://127.0.0.1:$SKILL_EXECUTOR_PORT/ready" "skill-executor readiness"

  if [[ "$ACCEPTANCE_PROFILE" == "user-questions" ]]; then
    wait_for_url "http://backend.127.0.0.1.nip.io:$FRONTEND_PORT/ready" "business API ingress"
    wait_for_url "http://backend-admin.127.0.0.1.nip.io:$FRONTEND_PORT/ready" "admin API ingress"
    wait_for_url "http://chat.127.0.0.1.nip.io:$FRONTEND_PORT/login" "business UI"
    run_user_questions_acceptance
    log "Kubernetes user-question acceptance passed"
    return
  fi

  if [[ "$ACCEPTANCE_PROFILE" == "hermes" ]]; then
    wait_for_url "http://backend.127.0.0.1.nip.io:$FRONTEND_PORT/ready" "business API ingress"
    run_hermes_acceptance
    log "Kubernetes Hermes acceptance passed"
    return
  fi

  assert_skill_executor_network_isolation
  assert_runner_reuse
  run_skill_smokes
  run_multi_agent_acceptance
  assert_sandboxes_disappear
  assert_safe_specs
  install_short_runner_ttl
  assert_runner_reaped
  assert_rbac_denials
  restart_backend_port_forward
  run_authorization_acceptance
  log "Kubernetes acceptance passed"
}

main "$@"
