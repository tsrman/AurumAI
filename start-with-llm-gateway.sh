#!/usr/bin/env bash
# Start the optional Bifrost gateway overlay above base Compose
# using explicit provider configuration.
#
# Usage: supply the OpenRouter provider key.
#   export OPENROUTER_API_KEY="sk-or-v1-..."
#   ./start-with-llm-gateway.sh
#
# Bootstrap persists the app secret and preserves existing values.
# Additional options/service arguments are forwarded to
# docker compose up -d.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

log() { echo "[start-with-llm-gateway] $*" >&2; }

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  log "ERROR: OPENROUTER_API_KEY is not set."
  log "  export OPENROUTER_API_KEY=\"sk-or-v1-...\"  # https://openrouter.ai/settings/keys"
  exit 1
fi

./scripts/bootstrap-dev-env.sh

log "Starting the stack with docker-compose.yml + docker-compose.llm-gateway.yml..."
docker compose -f docker-compose.yml -f docker-compose.llm-gateway.yml up -d "$@"

log "Done."
log "  Frontend:        http://localhost:3000"
log "  Backend API:      http://localhost:8000"
log "  Bifrost gateway:  http://localhost:8090 (loopback only, no authentication)"
log "  Model list:       curl -s http://localhost:8090/v1/models | jq '.data[].id'"
log "  ('openrouter/...' models are listed only when OPENROUTER_API_KEY is set)"
