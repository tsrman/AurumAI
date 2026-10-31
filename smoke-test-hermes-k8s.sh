#!/usr/bin/env bash
# Hermes Agent on Kubernetes acceptance (docs/HERMES_AGENT.md, phase 2d) on a
# disposable k3d cluster, whose policy controller enforces NetworkPolicy. The
# objects are the chart's own (`helm template` of the hermes-* templates) and the
# sandboxes are the real hermes-sandbox image run by skill-executor's
# KubernetesHermesSandboxes. Nothing of the platform's data plane is needed: the
# backend is a stand-in that answers the guard.
#
# Checked, in order:
#  * the guard starts under Pod Security "restricted" and forwards exactly the two
#    platform paths; a CONNECT without a token is refused;
#  * from a sandbox Pod only the guard answers: not a neighbour Pod, another
#    sandbox, the backend by address, kube-dns, the cluster API or the internet;
#    only sandbox Pods reach the guard, and the guard reaches only DNS, the
#    backend and public addresses (never a private Pod);
#  * the preview service starts and answers its health route;
#  * scripts/hermes-k8s-acceptance.py drives the runtime: create, files in and
#    out, the preview byte pipe, the network from inside the Pod, a snapshot of
#    the running Pod, retire, restore into a new Pod, re-adoption, disk usage.
#
# Prerequisites: Docker, k3d, kubectl, helm, uv. The images aurum-ai-web-egress,
# aurum-ai-hermes-sandbox (about 2.5 GB) and aurum-ai-hermes-preview are built
# here when missing. A run takes several minutes (the cluster, then the image
# import); run it in the background with a timeout.
# Set KEEP_HERMES_K8S_CLUSTER=1 to keep the cluster for inspection.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT_DIR"
CLUSTER=${HERMES_K8S_CLUSTER:-aurum-ai-hermes-k8s-smoke}
RELEASE=rel
PLATFORM_NS=platform
HERMES_NS=hermes-agents
WORK=$(mktemp -d)
FAILURES=0
log() { printf '[hermes-k8s] %s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }

for tool in docker k3d kubectl helm uv; do
  command -v "$tool" >/dev/null || fail "$tool is required"
done
if k3d cluster list -o json | grep -q "\"name\":\"$CLUSTER\""; then
  fail "a cluster named $CLUSTER already exists; delete it or set HERMES_K8S_CLUSTER"
fi

teardown() {
  local status=$?
  if [[ "${KEEP_HERMES_K8S_CLUSTER:-0}" == 1 && -f "$WORK/kubeconfig" ]]; then
    log "keeping cluster $CLUSTER (KUBECONFIG=$WORK/kubeconfig)"
  else
    log "removing the disposable cluster"
    k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
  exit "$status"
}
trap teardown EXIT

ensure_image() { # image context
  docker image inspect "$1" >/dev/null 2>&1 && return
  log "building $1"
  docker build -q -t "$1" "$2" >/dev/null
}
ensure_image aurum-ai-web-egress:latest infra/web-egress
ensure_image aurum-ai-hermes-preview:latest hermes-preview
ensure_image aurum-ai-hermes-sandbox:latest hermes-sandbox
docker pull -q busybox:1.37 >/dev/null 2>&1 || docker image inspect busybox:1.37 >/dev/null 2>&1 || fail "busybox:1.37 is needed"

export KUBECONFIG="$WORK/kubeconfig"
log "creating the cluster"
k3d cluster create "$CLUSTER" --servers 1 --agents 0 --no-lb --wait \
  --k3s-arg "--disable=traefik@server:0" --kubeconfig-update-default=false >/dev/null
k3d kubeconfig get "$CLUSTER" >"$KUBECONFIG"
kubectl wait --for=condition=Ready node --all --timeout=120s >/dev/null
log "importing the images"
# Ours must arrive: an image the cluster lacks would be pulled from a registry it
# cannot reach. busybox is only a probe tool; if it cannot be imported (some
# images carry manifests containerd refuses) the cluster pulls it itself.
k3d image import aurum-ai-web-egress:latest aurum-ai-hermes-preview:latest aurum-ai-hermes-sandbox:latest \
  -c "$CLUSTER" >/dev/null || fail "importing the platform images failed"
k3d image import busybox:1.37 -c "$CLUSTER" >/dev/null 2>&1 || log "busybox is pulled by the cluster"

kubectl create ns "$PLATFORM_NS" >/dev/null
kubectl -n "$PLATFORM_NS" apply -f - >/dev/null <<'EOF'
# A stand-in for the backend (any request gets a 200) and a Pod outside the Hermes namespace.
apiVersion: v1
kind: Pod
metadata:
  name: backend
  labels: {app.kubernetes.io/name: backend, app.kubernetes.io/instance: rel}
spec:
  containers:
  - name: c
    image: aurum-ai-web-egress:latest
    imagePullPolicy: IfNotPresent
    command: [python3, -c]
    args:
    - |
      import http.server
      class H(http.server.BaseHTTPRequestHandler):
          def _r(self):
              n = int(self.headers.get("content-length") or 0); self.rfile.read(n)
              b = b"backend-ok"
              self.send_response(200); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
          do_POST = do_GET = _r
      http.server.HTTPServer(("0.0.0.0", 8000), H).serve_forever()
---
apiVersion: v1
kind: Service
metadata: {name: rel-backend}
spec: {selector: {app.kubernetes.io/name: backend, app.kubernetes.io/instance: rel}, ports: [{port: 8000, targetPort: 8000}]}
---
apiVersion: v1
kind: Pod
metadata: {name: outsider}
spec:
  containers:
  - {name: c, image: "busybox:1.37", imagePullPolicy: IfNotPresent, command: [sleep, "3600"]}
EOF

log "applying the chart's Hermes objects"
helm template "$RELEASE" charts/aurum-ai -n "$PLATFORM_NS" \
  --set hermes.enabled=true --set "hermes.namespace=$HERMES_NS" \
  --set hermes.preview.enabled=true \
  --set 'hermes.preview.origin=http://{preview}.preview.example.test' \
  --set 'hermes.preview.host=*.preview.example.test' \
  -s templates/hermes-namespace.yaml -s templates/hermes-secret.yaml \
  -s templates/hermes-egress-deployment.yaml -s templates/hermes-egress-service.yaml \
  -s templates/hermes-network-policies.yaml -s templates/hermes-rbac.yaml \
  -s templates/hermes-preview-secret.yaml -s templates/hermes-preview-deployment.yaml \
  -s templates/hermes-preview-service.yaml >"$WORK/hermes.yaml"
kubectl apply -f "$WORK/hermes.yaml" >/dev/null
log "waiting for the guard under Pod Security restricted"
kubectl -n "$HERMES_NS" rollout status "deploy/$RELEASE-hermes-egress" --timeout=180s >/dev/null \
  || fail "the guard did not become ready"
log "waiting for the preview service"
kubectl -n "$PLATFORM_NS" rollout status "deploy/$RELEASE-hermes-preview" --timeout=180s >/dev/null \
  || fail "the preview service did not become ready"
kubectl -n "$PLATFORM_NS" wait --for=condition=Ready pod/backend pod/outsider --timeout=120s >/dev/null

SEC='securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}'
CSEC='securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}'
GUARD_IP=$(kubectl -n "$HERMES_NS" get svc hermes-egress -o jsonpath='{.spec.clusterIP}')
probe_pod() { # name labels
  kubectl -n "$HERMES_NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: $1, labels: $2}
spec:
  automountServiceAccountToken: false
  dnsPolicy: None
  dnsConfig: {nameservers: ["192.0.2.1"], options: [{name: timeout, value: "1"}, {name: attempts, value: "1"}]}
  hostAliases: [{ip: "$GUARD_IP", hostnames: [hermes-egress]}]
  $SEC
  containers:
  - {name: c, image: "busybox:1.37", imagePullPolicy: IfNotPresent, command: [sleep, "3600"], $CSEC}
EOF
}
SANDBOX_LABELS="{app.kubernetes.io/instance: $RELEASE, aurum-ai.component: hermes-sandbox}"
probe_pod sb1 "$SANDBOX_LABELS"
probe_pod sb2 "$SANDBOX_LABELS"
probe_pod neighbour '{role: neighbour}'
kubectl -n "$HERMES_NS" wait --for=condition=Ready pod/sb1 pod/sb2 pod/neighbour --timeout=120s >/dev/null
SB2=$(kubectl -n "$HERMES_NS" get pod sb2 -o jsonpath='{.status.podIP}')
NEIGHBOUR=$(kubectl -n "$HERMES_NS" get pod neighbour -o jsonpath='{.status.podIP}')
BACKEND=$(kubectl -n "$PLATFORM_NS" get pod backend -o jsonpath='{.status.podIP}')
OUTSIDER=$(kubectl -n "$PLATFORM_NS" get pod outsider -o jsonpath='{.status.podIP}')
DNS_IP=$(kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}')
API_IP=$(kubectl -n default get svc kubernetes -o jsonpath='{.spec.clusterIP}')
GUARD_POD=$(kubectl -n "$HERMES_NS" get pod -l app.kubernetes.io/name=hermes-egress -o jsonpath='{.items[0].metadata.name}')
GUARD_POD_IP=$(kubectl -n "$HERMES_NS" get pod "$GUARD_POD" -o jsonpath='{.status.podIP}')
PREVIEW_URL="http://$RELEASE-hermes-preview.$PLATFORM_NS.svc:8095/health"
sleep 10  # the policy controller programs the rules

check() { # label want(ok|no) namespace pod command...
  local label=$1 want=$2 namespace=$3 pod=$4 got out rc=0
  shift 4
  out=$(timeout 25 kubectl -n "$namespace" exec "$pod" -- "$@" 2>&1) || rc=$?
  if (( rc == 0 )); then got=ok; else got=no; fi
  if [[ "$got" == "$want" ]]; then
    log "PASS  $label"
  else
    log "FAIL  $label: expected $want, got $got"; printf '      %s\n' "$(head -c 300 <<<"$out")"
    FAILURES=$((FAILURES + 1))
  fi
}
FETCH="wget -q -T 3 -O-"
log "--- platform path and proxy"
check "the platform path reaches the backend through the guard" ok "$HERMES_NS" sb1 sh -c \
  'wget -q -T 5 -O- --post-data="{}" --header="Content-Type: application/json" http://hermes-egress:8080/v1/hermes/chat/completions | grep -q backend-ok'
check "any other platform path is refused" no "$HERMES_NS" sb1 sh -c \
  'wget -q -T 5 -O- --post-data="{}" http://hermes-egress:8080/v1/chat/completions'
check "a CONNECT without a token is refused" ok "$HERMES_NS" sb1 sh -c \
  'printf "CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n" | nc -w 4 hermes-egress 3128 | head -1 | grep -qE " 40[0-9] "'
log "--- a sandbox reaches only the guard"
check "a neighbour Pod" no "$HERMES_NS" sb1 $FETCH "http://$NEIGHBOUR:8080/"
check "another sandbox" no "$HERMES_NS" sb1 $FETCH "http://$SB2:8080/"
check "the backend by address" no "$HERMES_NS" sb1 $FETCH "http://$BACKEND:8000/"
check "kube-dns" no "$HERMES_NS" sb1 nslookup kubernetes.default.svc.cluster.local "$DNS_IP"
check "the cluster API" no "$HERMES_NS" sb1 $FETCH "https://$API_IP:443/"
check "the internet" no "$HERMES_NS" sb1 $FETCH http://1.1.1.1/
log "--- who reaches the guard, and what the guard reaches"
check "a non-sandbox Pod to the guard" no "$HERMES_NS" neighbour $FETCH "http://$GUARD_POD_IP:8080/"
check "a Pod in another namespace to the guard" no "$PLATFORM_NS" outsider $FETCH "http://$GUARD_POD_IP:8080/"
check "the guard to the backend" ok "$HERMES_NS" "$GUARD_POD" python3 -c \
  "import socket; socket.create_connection(('$RELEASE-backend.$PLATFORM_NS.svc',8000),4)"
check "the guard to a neighbour Pod" no "$HERMES_NS" "$GUARD_POD" python3 -c \
  "import socket; socket.create_connection(('$NEIGHBOUR',8080),4)"
check "the guard to a Pod in another namespace" no "$HERMES_NS" "$GUARD_POD" python3 -c \
  "import socket; socket.create_connection(('$OUTSIDER',8080),4)"
log "--- the preview service"
check "the preview service answers its health route" ok "$PLATFORM_NS" outsider $FETCH "$PREVIEW_URL"

log "--- the runtime"
if ! (cd skill-executor && SKILL_EXECUTOR_HERMES_ACCEPTANCE_NAMESPACE="$HERMES_NS" \
      SKILL_EXECUTOR_HERMES_ACCEPTANCE_RELEASE="$RELEASE" \
      SKILL_EXECUTOR_HERMES_ACCEPTANCE_API="$API_IP" \
      PYTHONPATH="$ROOT_DIR/skill-executor" timeout 900 uv run python ../scripts/hermes-k8s-acceptance.py); then
  FAILURES=$((FAILURES + 1))
fi

if (( FAILURES )); then fail "$FAILURES check(s) failed"; fi
log "all checks passed"
