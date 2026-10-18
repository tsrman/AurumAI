# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

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

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Use the pinned static uv binary from its official image instead of
# a separate pip installation. Pinning keeps dependency setup reproducible
# across builds at different times.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
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

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv ./.venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY gunicorn.conf.py ./
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# the security review
RUN useradd -u 10001 -m -s /usr/sbin/nologin ragapi && chown -R ragapi:ragapi /app
USER ragapi

EXPOSE 8110


# Keep the Compose probe path in the image healthcheck too,
# for Kubernetes/manual launches outside this Compose stack.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8110/health', timeout=4).status == 200 else 1)"
ENTRYPOINT ["./entrypoint.sh"]
