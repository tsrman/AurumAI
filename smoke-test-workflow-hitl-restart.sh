#!/usr/bin/env bash
# Exclusive Compose acceptance; plain runner image, production 1800s deadline —
# proves a waiting_human node survives the conversation-runner container being
# stopped and restarted while it waits (unlike smoke-test-workflow-hitl-ui.sh,
# which uses a shortened 65s deadline unsuitable for a restart cycle).
set -euo pipefail
if (( $# )); then
  printf 'This launcher accepts no CLI overrides\n' >&2
  exit 2
fi
export WORKFLOW_ACCEPTANCE_KIND=hitl-restart
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/smoke-test-workflow-templates-ui.sh"
