# Fixed image for Hermes Agent sessions (docs/HERMES_AGENT.md), run by
# skill-executor with no capabilities, a read-only root filesystem and a
# per-sandbox internal network whose only way out is hermes-egress.
FROM python:3.14-slim
COPY LICENSE /app/LICENSE

ARG AURUMAI_SOURCE_URL=""
ARG AURUMAI_REVISION=""
LABEL org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="${AURUMAI_SOURCE_URL}" \
      org.opencontainers.image.revision="${AURUMAI_REVISION}"

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# Hermes ships no wheel (its build refuses outside Nix) and runs from its
# source tree: the release's tag archive, verified by its hash. Its
# dependencies come from Hermes's own lock, pinned with hashes, as wheels only.
# Upgrading is a deliberate change of both ARGs and update-requirements.sh
# (see README.md).
ARG HERMES_VERSION=0.22.5
ARG HERMES_SHA256=a8f67e5ec14fea681acef6cc7ff1553f002bb6101fd2df22bfdd4b7eecc54eb2
RUN curl -fsSL -o /tmp/hermes.tar.gz \
        "https://github.com/NousResearch/hermes-agent/archive/refs/tags/v${HERMES_VERSION}.tar.gz" \
    && echo "${HERMES_SHA256}  /tmp/hermes.tar.gz" | sha256sum -c - \
    && mkdir /opt/hermes \
    && tar -xzf /tmp/hermes.tar.gz -C /opt/hermes --strip-components=1 --no-same-owner \
    && rm -rf /tmp/hermes.tar.gz /opt/hermes/tests /opt/hermes/.github \
    && ln -s /opt/hermes/hermes /usr/local/bin/hermes
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r /tmp/requirements.txt \
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
