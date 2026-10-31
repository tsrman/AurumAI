#!/usr/bin/env bash
# Hermes egress modes on Kubernetes (docs/HERMES_AGENT.md, phase 2d), with real
# internet traffic, on a disposable k3d cluster whose policy controller enforces
# NetworkPolicy. The counterpart of ./smoke-test-hermes-k8s.sh, which checks the
# boundary and the runtime against nothing but the cluster: this one lets the guard
# reach the real package registries and public hosts.
#
# For each maximum mode (packages, public, none) it applies the chart's own Hermes
# objects and checks, from a sandbox-labelled Pod: the registries by TLS, a refused
# host, port, SNI and credential, the organization's mode clamped to the maximum,
# the metadata, cluster API, Pod, loopback and private-name addresses refused, an
# internal package mirror reachable by its exact host:port (and, as a finding, only
# when allowed by its Pod's address), the real pip and npm of the sandbox image
# through the guard, and an open tunnel closed by the periodic re-check when the
# mode is lowered or the session revoked. The policy layer is checked on its own
# from the guard Pod. The backend is a stand-in that reads a session's mode from its
# token.
#
# Prerequisites: Docker, k3d, kubectl, helm, python3 and the internet. The images
# aurum-ai-web-egress and aurum-ai-hermes-sandbox (about 2.5 GB) are built here when
# missing. A run takes about ten minutes; run it in the background with a timeout.
# Set KEEP_HERMES_K8S_CLUSTER=1 to keep the cluster for inspection, and HERMES_K8S_CNI=calico
# to run on Calico instead of k3s's own networking.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi
cd "$(dirname "${BASH_SOURCE[0]}")"
for tool in docker k3d kubectl helm python3; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done
ensure_image() { # image context
  docker image inspect "$1" >/dev/null 2>&1 && return
  echo "[hermes-k8s-egress] building $1"
  docker build -q -t "$1" "$2" >/dev/null
}
ensure_image aurum-ai-web-egress:latest infra/web-egress
ensure_image aurum-ai-hermes-sandbox:latest hermes-sandbox
exec python3 -u scripts/hermes-k8s-egress-modes.py
