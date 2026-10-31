#!/usr/bin/env bash
# Installs Calico as the CNI and policy controller of the k3d cluster that KUBECONFIG
# points at, for the Hermes acceptance runs (HERMES_K8S_CNI=calico). The cluster must
# have been created without k3s's own networking:
#   k3d cluster create ... \
#     --k3s-arg "--flannel-backend=none@server:*" --k3s-arg "--disable-network-policy@server:*"
# Needs the internet (the manifest is fetched from GitHub, the images from quay.io).
# CALICO_VERSION and POD_CIDR (k3s's default cluster CIDR) can be overridden.
set -euo pipefail
VERSION=${CALICO_VERSION:-v3.33.0}
POD_CIDR=${POD_CIDR:-10.42.0.0/16}
URL="https://raw.githubusercontent.com/projectcalico/calico/${VERSION}/manifests/calico.yaml"
# The manifest leaves the pool commented out and would use 192.168.0.0/16, which is not
# inside k3s's cluster CIDR: the pool must be set before calico-node first starts.
curl -fsSL "$URL" | python3 -c '
import sys
cidr = sys.argv[1]
text = sys.stdin.read()
old = "            # - name: CALICO_IPV4POOL_CIDR\n            #   value: \"192.168.0.0/16\"\n"
new = f"            - name: CALICO_IPV4POOL_CIDR\n              value: \"{cidr}\"\n"
if old not in text:
    sys.exit("the Calico manifest no longer has the expected pool setting")
sys.stdout.write(text.replace(old, new, 1))
' "$POD_CIDR" | kubectl apply -f - >/dev/null
kubectl -n kube-system rollout status ds/calico-node --timeout=300s
kubectl -n kube-system rollout status deploy/calico-kube-controllers --timeout=300s
kubectl wait --for=condition=Ready node --all --timeout=300s >/dev/null
echo "Calico ${VERSION} is the CNI (pool ${POD_CIDR})"
