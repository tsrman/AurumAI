#!/usr/bin/env bash
# Manual-only, existing Compose stack. No Docker/Keycloak configuration writes.
# Install business-ui dependencies and Chromium first; deploy the current UIs.
set -euo pipefail

if (( $# )); then
  echo "Usage: $0 (configure URLs and credentials through E2E_* environment variables)" >&2
  exit 2
fi

AUTH_E2E_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$AUTH_E2E_ROOT/business-ui"
if [[ ! -x node_modules/.bin/playwright ]]; then
  echo "Install prerequisites in business-ui: npm ci && npx playwright install chromium" >&2
  exit 1
fi

export E2E_AUTH_LIFECYCLE=true
exec node_modules/.bin/playwright test e2e/auth-lifecycle.real.spec.ts \
  --workers 1 --retries 0 --trace off
