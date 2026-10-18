#!/usr/bin/env bash
# Exclusive Compose acceptance; isolated runner image uses a real 65s deadline.
set -euo pipefail
if (( $# )); then
  printf 'This launcher accepts no CLI overrides\n' >&2
  exit 2
fi
export WORKFLOW_ACCEPTANCE_KIND=hitl
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/smoke-test-workflow-templates-ui.sh"
