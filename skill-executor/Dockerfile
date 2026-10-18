# syntax=docker/dockerfile:1
FROM python:3.14-slim AS builder

# Apply base-image security patches: python:3.12-slim is rebuilt
# less often than Debian publishes fixes; scans found high-severity
# issues in system packages such as util-linux and libexpat.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Use the pinned static uv binary from its official image instead of
# a separate pip installation. Pinning keeps dependency setup reproducible
# across builds at different times.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.14-slim AS runtime
COPY LICENSE /app/LICENSE

ARG AURUMAI_SOURCE_URL=""
ARG AURUMAI_REVISION=""
LABEL org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="${AURUMAI_SOURCE_URL}" \
      org.opencontainers.image.revision="${AURUMAI_REVISION}"


# Apply base-image security patches: python:3.12-slim is rebuilt
# less often than Debian publishes fixes; scans found high-severity
# issues in system packages such as util-linux and libexpat.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app/.venv ./.venv
COPY src ./src

# the security review flags this alongside tool-worker for running as root —
# deliberately NOT changed here, unlike tool-worker: this container mounts
# /var/run/docker.sock (docker-compose.yml) to create/exec/destroy sandbox
# containers, and that socket is itself root-equivalent host access
# regardless of this process's own UID — a non-root user here would either
# still need group access to the same socket (no real privilege reduction)
# or break container management outright. The actual privilege boundary for
# this feature is "only skill-executor has the socket, nothing else does"
# (see docker-compose.yml's skills-net comments), not this process's UID.
EXPOSE 8090

# Keep the Compose probe path in the image healthcheck too,
# for Kubernetes/manual launches outside this Compose stack.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "src.main"]
