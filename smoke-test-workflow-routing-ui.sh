#!/usr/bin/env bash
# Same exclusive Compose/secret contract and exact-owned recovery as C5.
set -euo pipefail
if (( $# )); then echo "This launcher takes no arguments" >&2; exit 2; fi
export WORKFLOW_ACCEPTANCE_KIND=routing
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/smoke-test-workflow-templates-ui.sh"
