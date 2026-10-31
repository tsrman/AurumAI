#!/usr/bin/env bash
# Hermes sandboxes on several Kubernetes nodes (docs/HERMES_AGENT.md, phase 2d), with
# eviction and a PID limit, on a disposable k3d cluster of three nodes with a local
# registry. The chart's own Hermes objects run on it and skill-executor's
# KubernetesHermesSandboxes drives the sandboxes with the real image.
#
# Checked: a cold pull of the 2.4 GB image on a node that never ran it, against the
# pod start timeout (and a pull that outlasts a short one is reported, the Pod removed);
# capacity eviction at the operator and the per-organization cap, with the evicted
# session restored from its snapshot; the soft workspace limit and, past the backstop,
# the kubelet evicting the Pod, the executor forgetting it and the session starting again
# from its snapshot; a RuntimeClass with a nodeSelector placing a sandbox on a node whose
# kubelet limits PIDs (a fork loop stops there and not elsewhere); and a node lost under
# a running sandbox, which the cluster reports not Ready within about a minute, after
# which create() starts the session anew on a live node from its snapshot.
#
# Prerequisites: Docker, k3d, kubectl, helm, uv, and about 10 GB of free disk. The images
# aurum-ai-web-egress and aurum-ai-hermes-sandbox (about 2.5 GB) are built here when
# missing. A run takes about twenty minutes; run it in the background with a timeout of
# at least thirty minutes. Set KEEP_HERMES_K8S_CLUSTER=1 to keep the cluster.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT_DIR"
for tool in docker k3d kubectl helm uv; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done
ensure_image() { # image context
  docker image inspect "$1" >/dev/null 2>&1 && return
  echo "[hermes-k8s-nodes] building $1"
  docker build -q -t "$1" "$2" >/dev/null
}
ensure_image aurum-ai-web-egress:latest infra/web-egress
ensure_image aurum-ai-hermes-sandbox:latest hermes-sandbox
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# The Kubernetes client reads the path when it is imported, so it is set before Python runs.
export KUBECONFIG="$WORK/kubeconfig"
cd skill-executor
PYTHONPATH="$ROOT_DIR/skill-executor" uv run python -u ../scripts/hermes-k8s-nodes-limits.py
