# Fixed image for Hermes Agent sessions (docs/HERMES_AGENT.md), run by
# skill-executor with no capabilities, a read-only root filesystem and a
# per-sandbox internal network whose only way out is hermes-egress.
FROM python:3.13-slim
COPY LICENSE /app/LICENSE

ARG AURUMAI_SOURCE_URL=""
ARG AURUMAI_REVISION=""
LABEL org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="${AURUMAI_SOURCE_URL}" \
      org.opencontainers.image.revision="${AURUMAI_REVISION}"

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# Hermes and every dependency are pinned with hashes; upgrading is a deliberate
# change to requirements.in and a recompile (see README.md).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY start.sh /usr/local/bin/hermes-sandbox-start
COPY hermes_relay.py /usr/local/bin/hermes-relay
RUN chmod 0555 /usr/local/bin/hermes-sandbox-start /usr/local/bin/hermes-relay \
    && useradd --uid 10001 --create-home --home-dir /home/hermes hermes \
    && mkdir -p /workspace && chown 10001:10001 /workspace

# Everything the agent writes lives in the workspace volume: Hermes's state,
# its home directory and `pip install --user` packages.
ENV HERMES_HOME=/workspace/.hermes \
    HOME=/workspace/home \
    PYTHONUSERBASE=/workspace/.local \
    PATH=/workspace/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PIP_USER=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/hermes-sandbox-start"]
