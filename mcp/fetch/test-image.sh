#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
image="${MCP_FETCH_TEST_IMAGE:-mcp-fetch:readiness-test}"
container="mcp-fetch-readiness-test-$$"
# Assigned before the trap is installed: an INT between the two would
# otherwise run cleanup with the variable unset, which `set -u` turns into
# a shell error instead of a cleanup.
probe_log=""

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  [ -n "$probe_log" ] && rm -f "$probe_log"
  return 0
}
trap cleanup EXIT INT TERM

# Deliberately bypass the Docker build cache: this regression must exercise
# today's dependency resolution, which is where an unbounded transitive mcp
# release previously produced an image that built but crashed at startup.
docker build --no-cache --tag "$image" "$script_dir"
docker run --detach --name "$container" "$image" >/dev/null

# The probe reports *why* it is not ready (a non-200 status line, a
# refused connection) on its own stderr. Retries stay quiet, but the last
# attempt's output is kept and printed on failure — without it a moved SSE
# endpoint fails this script after 30s next to `docker logs` showing a
# perfectly healthy "Uvicorn running on http://0.0.0.0:8080", with nothing
# to suggest the probe was being answered with 404s.
probe_log="$(mktemp)"

attempt=0
while [ "$attempt" -lt 30 ]; do
  if docker exec "$container" /usr/local/bin/python -c '
import socket

with socket.create_connection(("127.0.0.1", 8080), timeout=2) as client:
    client.sendall(b"GET /sse HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    status_line = client.recv(128).split(b"\r\n", 1)[0]
    if b" 200 " not in status_line:
        raise SystemExit(f"unexpected status: {status_line!r}")
' >"$probe_log" 2>&1; then
    echo "mcp-fetch image build and SSE readiness passed"
    exit 0
  fi

  if [ "$(docker inspect --format '{{.State.Running}}' "$container")" != "true" ]; then
    echo "mcp-fetch exited before SSE readiness" >&2
    echo "last readiness probe output:" >&2
    cat "$probe_log" >&2
    docker logs "$container" >&2
    exit 1
  fi

  attempt=$((attempt + 1))
  sleep 1
done

echo "mcp-fetch did not reach SSE readiness within 30 seconds" >&2
echo "last readiness probe output:" >&2
cat "$probe_log" >&2
docker logs "$container" >&2
exit 1
