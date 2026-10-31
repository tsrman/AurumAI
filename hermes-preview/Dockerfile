# syntax=docker/dockerfile:1
# Previews of the apps Hermes Agent runs (docs/HERMES_AGENT.md): the only
# service browsers reach at a preview's origin.
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

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

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app/.venv ./.venv
COPY src ./src

RUN useradd -u 10001 -M -s /usr/sbin/nologin preview
USER preview

EXPOSE 8095

HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8095/health', timeout=2).status == 200 else 1)"

CMD ["python", "-m", "src.preview"]
