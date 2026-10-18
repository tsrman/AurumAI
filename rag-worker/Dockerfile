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

# docling (PDF/DOCX parsing) pulls in opencv-python-headless transitively —
# needs libgl1/libglib2.0-0 at both build (native extension compile checks)
# and runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Use the pinned static uv binary from its official image instead of
# a separate pip installation. Pinning keeps dependency setup reproducible
# across builds at different times.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

# Frozen uv.lock keeps builds from resolving a different torch version
# that bypasses the explicit CPU-source pin.
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

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv ./.venv
COPY src ./src

# the security review. -m (home dir) matters here specifically: docling
# caches parser models under $HOME, not just /app.
RUN useradd -u 10001 -m -s /usr/sbin/nologin ragworker && chown -R ragworker:ragworker /app
USER ragworker

CMD ["python", "-m", "src.main"]
