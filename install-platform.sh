#!/usr/bin/env bash
# Start the ordinary platform and bounded starter-tool dependencies.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
apply=false
mcp_endpoint="http://mcp-everything:3001/sse"
org_args=()

usage() {
  cat <<'HELP'
Usage: ./install-platform.sh [--apply] [--org-id UUID] [--mcp-endpoint URL]

Dry-run by default. Apply builds the runner and skill sandbox, starts the
ordinary Compose platform plus mcp-everything and skill-executor, and installs
four global tools. Repeated --org-id targets receive tool-only read grants.
Existing definitions and previously removed grants are preserved.
Configure the ordinary stack environment before applying. No secrets are
created or replaced. See docs/STARTER_TOOLS.md for setup and limitations.
HELP
}

while (($#)); do
  case "$1" in
    --apply) apply=true; shift ;;
    --org-id)
      [[ $# -ge 2 && "$2" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || {
        echo 'Error: --org-id requires a UUID.' >&2; exit 2;
      }
      org_args+=(--org-id "$2"); shift 2 ;;
    --mcp-endpoint)
      [[ $# -ge 2 ]] || { echo 'Error: --mcp-endpoint requires a URL.' >&2; exit 2; }
      mcp_endpoint="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo 'Error: unknown option. Use --help.' >&2; exit 2 ;;
  esac
done

if [[ ! "$mcp_endpoint" =~ ^https?://[^/@?#[:space:]]+(/[^@?#[:space:]]*)?$ ]]; then
  echo 'Error: MCP endpoint must be an HTTP(S) URL without credentials, query or fragment.' >&2
  exit 2
fi

cd "$ROOT_DIR"
# Prevent inherited optional profiles (including a .env setting) from widening
# this bounded entry point. Explicit -f selects the ordinary Compose definition.
export COMPOSE_PROFILES=""
compose=(docker compose -f "$ROOT_DIR/docker-compose.yml")
commands() {
  "${compose[@]}" --profile build build runner skill-sandbox
  "${compose[@]}" up -d --build --wait --wait-timeout=300
  "${compose[@]}" --profile mcp --profile skills-exec up -d --build --wait --wait-timeout=300 mcp-everything skill-executor
  "${compose[@]}" exec -T backend python -m src.cli.install_starter_tools --apply "${org_args[@]}" "--mcp-endpoint=$mcp_endpoint"
}

if [[ "$apply" == true ]]; then
  commands
else
  echo 'Dry run: no commands executed. Re-run with --apply after reviewing:'
  docker() { printf '%q ' docker "$@"; printf '\n'; }
  commands
fi
