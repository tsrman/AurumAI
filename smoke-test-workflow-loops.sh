#!/usr/bin/env bash
# Manual/exclusive; shared C5 exact-owned recovery journal and Compose lifecycle.
set -euo pipefail
if (($#)); then
  printf 'Loop acceptance takes no arguments; use shared E2E_* overrides.\n' >&2
  exit 2
fi
for override in BASE_URL ADMIN_BASE_URL KEYCLOAK_URL KEYCLOAK_REALM \
  KEYCLOAK_CLIENT_ID ADMIN_KEYCLOAK_CLIENT_ID WORKFLOW_LOOPS_E2E_MODEL \
  WORKFLOW_LOOPS_E2E_DB_CONTAINER WORKFLOW_LOOPS_E2E_COMPOSE_PROJECT \
  WORKFLOW_LOOPS_E2E_RATE_LIMIT_PER_MINUTE WORKFLOW_LOOPS_E2E_RATE_LIMIT_PER_MINUTE_STREAM; do
  if [[ -v "$override" ]]; then
    printf 'Unsupported legacy override %s; use shared E2E_* settings (model/project/rates are C5-managed).\n' "$override" >&2
    exit 2
  fi
done
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ "${WORKFLOW_LOOPS_E2E_MANAGE_COMPOSE:-1}" != 1 ]]; then
  printf 'Standalone loop acceptance is unsupported; use the C5-managed wrapper.\n' >&2
  exit 2
fi
export WORKFLOW_ACCEPTANCE_KIND=loops
export TEST_GATEWAY_BARRIER_SECONDS="${TEST_GATEWAY_BARRIER_SECONDS:-8}"
exec bash "$ROOT_DIR/smoke-test-workflow-templates-ui.sh"
