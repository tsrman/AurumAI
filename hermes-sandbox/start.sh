#!/bin/sh
# Hermes sandbox entrypoint. skill-executor places the platform-generated
# config (model endpoint, session token, API server key) and the egress proxy
# URL in the workspace before the container starts; nothing secret is in the
# container's environment, where `docker inspect` shows it.
set -eu
mkdir -p "$HERMES_HOME" "$HOME" /workspace/.aurum
# With a tmpfs workspace the files arrive after the start, behind a restored
# snapshot of up to the snapshot bound, so this waits as long as a restore may
# take (SKILL_EXECUTOR_HERMES_SNAPSHOT_TIMEOUT_SECONDS, fifteen minutes).
for _ in $(seq 9000); do
    [ -s "$HERMES_HOME/config.yaml" ] && [ -s /workspace/.aurum/proxy ] && break
    sleep 0.1
done
[ -s "$HERMES_HOME/config.yaml" ] || { echo "no platform config" >&2; exit 1; }
proxy=$(cat /workspace/.aurum/proxy)
# Only HTTPS goes through the CONNECT proxy; the platform endpoint is plain
# HTTP to hermes-egress and must never be proxied.
export HTTPS_PROXY="$proxy" https_proxy="$proxy" NO_PROXY="hermes-egress,localhost,127.0.0.1" no_proxy="hermes-egress,localhost,127.0.0.1"
export PIP_PROXY="$proxy" NPM_CONFIG_HTTPS_PROXY="$proxy" NPM_CONFIG_NOPROXY="hermes-egress,localhost,127.0.0.1"
exec hermes gateway run
