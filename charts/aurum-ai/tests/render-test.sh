#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
chart="${repo_root}/charts/aurum-ai"
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

fail() {
  printf 'render contract failed: %s\n' "$1" >&2
  exit 1
}

created="${work_dir}/created.yaml"
helm template test "${chart}" \
  --namespace platform \
  --set runtimeNamespaces.create=true \
  --set secrets.create=true \
  --set 'networkPolicies.runner.destinations[0].cidr=198.51.100.0/24' \
  --set 'networkPolicies.runner.destinations[0].ports[0].protocol=TCP' \
  --set 'networkPolicies.runner.destinations[0].ports[0].port=7443' >"${created}"

existing="${work_dir}/existing.yaml"
helm template custom "${chart}" \
  --namespace tenant-app \
  --set runtimeNamespaces.create=false \
  --set runtimeNamespaces.runner=existing-runners \
  --set runtimeNamespaces.sandbox=existing-sandboxes \
  --set secrets.create=false >"${existing}"

no_secrets="${work_dir}/no-secrets.yaml"
helm template no-secrets "${chart}" \
  --namespace platform \
  --set runtimeNamespaces.create=true \
  --set secrets.create=false >"${no_secrets}"

langfuse_disabled="${work_dir}/langfuse-disabled.yaml"
helm template no-langfuse "${chart}" \
  --namespace platform \
  --set runtimeNamespaces.create=true \
  --set secrets.create=true \
  --set external.langfuse.enabled=false >"${langfuse_disabled}"

custom_images="${work_dir}/custom-images.yaml"
helm template images "${chart}" \
  --namespace platform \
  --set image.registry=registry.example.test/team \
  --set image.pullPolicy=Never \
  --set backend.image.pullPolicy=Always \
  --set backendAdmin.image.pullPolicy=IfNotPresent \
  --set frontend.image.pullPolicy=Always \
  --set adminUi.image.pullPolicy=IfNotPresent \
  --set toolWorker.image.pullPolicy=Always \
  --set skillExecutor.image.pullPolicy=IfNotPresent \
  --set 'imagePullSecrets[0].name=release-pull' \
  --set 'runner.imagePullSecrets[0]=runner-pull' \
  --set 'skillSandbox.imagePullSecrets[0]=sandbox-pull' >"${custom_images}"

dns_disabled="${work_dir}/dns-disabled.yaml"
helm template no-dns "${chart}" \
  --namespace platform \
  --set networkPolicies.dns.enabled=false \
  --set 'networkPolicies.runner.destinations[0].cidr=203.0.113.0/24' \
  --set 'networkPolicies.runner.destinations[0].ports[0].protocol=TCP' \
  --set 'networkPolicies.runner.destinations[0].ports[0].port=9443' >"${dns_disabled}"

runner_overlap_error="${work_dir}/runner-overlap.error"
if helm template overlap-runner "${chart}" \
  --namespace platform \
  --set runtimeNamespaces.runner=platform \
  --set runtimeNamespaces.sandbox=isolated-sandboxes \
  >"${work_dir}/runner-overlap.yaml" 2>"${runner_overlap_error}"; then
  fail "runtimeNamespaces.runner equal to the release namespace must be rejected"
fi
if ! rg -Fq \
  'runtimeNamespaces.runner must differ from the release namespace' \
  "${runner_overlap_error}"; then
  fail "runner/release namespace overlap must report a clear error"
fi

sandbox_overlap_error="${work_dir}/sandbox-overlap.error"
if helm template overlap-sandbox "${chart}" \
  --namespace platform \
  --set runtimeNamespaces.runner=isolated-runners \
  --set runtimeNamespaces.sandbox=platform \
  >"${work_dir}/sandbox-overlap.yaml" 2>"${sandbox_overlap_error}"; then
  fail "runtimeNamespaces.sandbox equal to the release namespace must be rejected"
fi
if ! rg -Fq \
  'runtimeNamespaces.sandbox must differ from the release namespace' \
  "${sandbox_overlap_error}"; then
  fail "sandbox/release namespace overlap must report a clear error"
fi

parser="${repo_root}/charts/aurum-ai/tests/render_contract.py"
if ! command -v uv >/dev/null 2>&1; then
  fail "uv is required to run the pinned Helm render contract parser"
fi
contract_cache="${UV_CACHE_DIR:-/tmp/uv-cache-k8s-helm-contract}"
UV_CACHE_DIR="${contract_cache}" uv run --script "${parser}" \
  "${created}" \
  "${existing}" \
  "${no_secrets}" \
  "${langfuse_disabled}" \
  "${custom_images}" \
  "${dns_disabled}"
