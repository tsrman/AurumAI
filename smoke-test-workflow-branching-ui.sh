#!/usr/bin/env bash
# Manual-only, exclusive Compose acceptance; recovery belongs to the C5 wrapper.
set -euo pipefail
if (( $# )); then
  printf 'This launcher accepts no CLI overrides\n' >&2
  exit 2
fi
export WORKFLOW_ACCEPTANCE_KIND=branching
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/smoke-test-workflow-templates-ui.sh"
