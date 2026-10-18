#!/usr/bin/env bash
# Fresh owned k3d release. No shared-stack mutation other than journaled grants.
set -euo pipefail
umask 077
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ "${WEB_ACCEPTANCE_EXCLUSIVE:-}" == 1 ]] || { printf 'BLOCKED: WEB_ACCEPTANCE_EXCLUSIVE=1 is required; stop all other org-mutating/browser smoke\n' >&2; exit 2; }
for prerequisite in APP_SECRET_KEY WEB_TOOLS_SERVICE_SECRET SEARXNG_SECRET; do
  [[ -n "${!prerequisite:-}" ]] || { printf 'BLOCKED: missing %s\n' "$prerequisite" >&2; exit 2; }
done
CLI="$ROOT_DIR/scripts/web_tools_k8s_e2e.py"
export E2E_WEB_TOOLS_PYTHON="${E2E_WEB_TOOLS_PYTHON:-$ROOT_DIR/business-backend/.venv/bin/python}"
export WEB_ACCEPTANCE_FIXTURE_IPV4="${WEB_ACCEPTANCE_FIXTURE_IPV4:-93.184.217.2}"
export WEB_ACCEPTANCE_FIXTURE_IPV6="${WEB_ACCEPTANCE_FIXTURE_IPV6:-2606:4700:ffff:217::2}"
"$E2E_WEB_TOOLS_PYTHON" "$CLI" preflight
LOCK_DIR="${WEB_ACCEPTANCE_LOCK:-/tmp/aurum-ai-web-acceptance.lock}"
mkdir -m 700 -- "$LOCK_DIR" 2>/dev/null || { printf 'BLOCKED: web acceptance lock exists\n' >&2; exit 2; }
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/web-tools-k8s-e2e.XXXXXXXX")
STATE_PATH="$WORK_DIR/state.json"
MARKER="workflow-templates-ui-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
export E2E_WEB_TOOLS=true E2E_WEB_TOOLS_STATE="$STATE_PATH" E2E_WEB_TOOLS_MARKER="$MARKER"
export E2E_WEB_TOOLS_RUNTIME=kubernetes E2E_WEB_TOOLS_FIXTURE_SCRIPT="$CLI"
export PLAYWRIGHT_NO_COPY_PROMPT=1
export TEST_GATEWAY_MODEL_ID="$MARKER-model" WEB_ACCEPTANCE_KIND=controlled
cleanup() {
  local primary=$? secondary=0 code
  trap - EXIT INT TERM
  set +e
  if [[ -f "$STATE_PATH" ]]; then
    "$E2E_WEB_TOOLS_PYTHON" "$CLI" cleanup --state "$STATE_PATH" --marker "$MARKER"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
    "$E2E_WEB_TOOLS_PYTHON" "$CLI" verify-clean --state "$STATE_PATH" --marker "$MARKER"
    code=$?; if ((code && !secondary)); then secondary=$code; fi
    # Failed closure/restore MUST retain the runtime needed for exact recovery.
    if ((!secondary)); then
      "$E2E_WEB_TOOLS_PYTHON" "$CLI" teardown --state "$STATE_PATH" --marker "$MARKER"
      code=$?; if ((code && !secondary)); then secondary=$code; fi
      "$E2E_WEB_TOOLS_PYTHON" "$CLI" verify-infra --state "$STATE_PATH" --marker "$MARKER"
      code=$?; if ((code && !secondary)); then secondary=$code; fi
    fi
  fi
  if ((!primary && !secondary)); then
    rm -- "$STATE_PATH" "$WORK_DIR/infra.json"
    rmdir -- "$WORK_DIR" "$LOCK_DIR"
    code=$?; if ((code)); then secondary=$code; fi
  else
    printf 'recovery state retained at %s\n' "$STATE_PATH" >&2
    printf 'Restore original secret and URL overrides; recover exclusively, in this order (stop on any error):\n' >&2
    for action in cleanup verify-clean teardown verify-infra; do
      printf '%q %q %s --state %q --marker %q\n' "$E2E_WEB_TOOLS_PYTHON" "$CLI" "$action" "$STATE_PATH" "$MARKER" >&2
    done
    printf 'Only after verification remove the exact journals and empty directories %q %q.\n' "$WORK_DIR" "$LOCK_DIR" >&2
  fi
  if ((primary)); then exit "$primary"; fi
  exit "$secondary"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$E2E_WEB_TOOLS_PYTHON" "$CLI" init --state "$STATE_PATH" --marker "$MARKER"
"$E2E_WEB_TOOLS_PYTHON" "$CLI" deploy --state "$STATE_PATH" --marker "$MARKER"
"$E2E_WEB_TOOLS_PYTHON" "$CLI" scenario --state "$STATE_PATH" --marker "$MARKER"
