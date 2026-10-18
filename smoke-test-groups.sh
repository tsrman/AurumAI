#!/usr/bin/env bash
set -euo pipefail

# Deterministic capabilities/grants acceptance. Authorization is unconditional;
# there is no legacy mode or transition switch.
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/authorization_v2_e2e.py"
