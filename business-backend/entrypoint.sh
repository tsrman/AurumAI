#!/bin/sh
set -e

# Starting this image as root has exactly one purpose: hand image user
# appuser access to a mounted Docker socket, then drop straight back down.
# Compose's `backend` is the only service that does it (`user: "0:0"`);
# `backend-admin` runs non-root, the Helm chart sets no securityContext so
# its pods keep USER appuser, and the migration Job overrides this script
# entirely. That is why the socket handling below — including its
# fail-fast — lives inside the root branch and must not be hoisted out of
# it: a non-root start has no socket to map and no privileges to drop.
if [ "$(id -u)" = "0" ]; then
  # Resolve the socket the Docker client will actually connect to, rather
  # than assuming the default path: DOCKER_HOST is what aiodocker reads,
  # and mounting the socket elsewhere while pointing DOCKER_HOST at it is
  # a legitimate setup that a hardcoded /var/run/docker.sock silently
  # skips. DOCKER_SOCKET_PATH remains an explicit override for the case
  # where the two genuinely differ.
  docker_host="${DOCKER_HOST:-unix:///var/run/docker.sock}"
  if [ -n "${DOCKER_SOCKET_PATH:-}" ]; then
    socket_path="$DOCKER_SOCKET_PATH"
  elif [ "${docker_host#unix://}" != "$docker_host" ]; then
    socket_path="${docker_host#unix://}"
  else
    # tcp:// or ssh://: the daemon is remote, there is no local socket to
    # map and nothing here applies.
    socket_path=""
  fi

  if [ -n "$socket_path" ]; then
    if [ -S "$socket_path" ]; then
      socket_gid="$(stat -c '%g' "$socket_path")"
      # A root-group socket (GID 0) is only reachable by root; adding
      # appuser to `root` would grant nothing and hide that fact. Fall
      # through to the check below, which reports it.
      if [ "$socket_gid" != "0" ]; then
        socket_group="$(getent group "$socket_gid" | cut -d: -f1 || true)"
        if [ -z "$socket_group" ]; then
          socket_group="docker-host-${socket_gid}"
          suffix=1
          while getent group "$socket_group" >/dev/null 2>&1; do
            socket_group="docker-host-${socket_gid}-${suffix}"
            suffix=$((suffix + 1))
          done
          groupadd --gid "$socket_gid" "$socket_group"
        fi
        usermod -aG "$socket_group" appuser
      fi
    fi

    # Fail fast instead of failing open. A socket that is missing, or one
    # appuser still cannot connect to (connect(2) on AF_UNIX needs write
    # permission), makes every runner spawn fail — but nothing would say
    # so: DockerRunnerRuntime builds aiodocker.Docker() lazily and
    # maintain_pool() swallows the error into a warning, so the container
    # passes /health and the problem first surfaces as a 500 on the
    # initial POST /v1/chat/stream.
    if [ "${APP_RUNNER_RUNTIME:-docker}" = "docker" ] && ! gosu appuser test -w "$socket_path"; then
      echo "entrypoint: appuser cannot write the Docker socket at $socket_path" >&2
      if [ -S "$socket_path" ]; then
        echo "entrypoint: socket is $(stat -c 'mode %a, owner %u:%g' "$socket_path"); appuser needs group write access to it" >&2
      else
        echo "entrypoint: nothing is mounted there — mount the socket, or point DOCKER_HOST (or DOCKER_SOCKET_PATH) at where it is" >&2
      fi
      echo "entrypoint: set APP_RUNNER_RUNTIME=kubernetes if this deployment does not spawn runner containers" >&2
      exit 1
    fi
  fi

  exec gosu appuser "$0" "$@"
fi

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Running migrations..."
  MIGRATION_URL="${APP_MIGRATION_DATABASE_URL:-$APP_DATABASE_URL}"
  APP_DATABASE_URL="$MIGRATION_URL" python -m alembic upgrade head
fi

echo "Starting Gunicorn..."
exec gunicorn "${APP_MODULE:-src.main_business:app}" -c gunicorn.conf.py
