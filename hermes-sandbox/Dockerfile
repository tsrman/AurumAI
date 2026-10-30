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

# Chromium's headless shell (Debian's signed package, with fonts that cover
# Cyrillic) lets the agent check the web pages it builds, through Playwright.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates curl git xz-utils \
        chromium-headless-shell fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Node.js for JavaScript projects: the official release, verified by its hash.
# Native addons cannot build (no compilers); npm reaches the registry through
# hermes-egress, with the HTTPS proxy start.sh sets.
ARG TARGETARCH
ARG NODE_VERSION=24.22.1
ARG NODE_SHA256_AMD64=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6
ARG NODE_SHA256_ARM64=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e
RUN case "${TARGETARCH:-amd64}" in \
        amd64) arch=x64 sha256="${NODE_SHA256_AMD64}" ;; \
        arm64) arch=arm64 sha256="${NODE_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
    && echo "${sha256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
        --exclude='*/include' --exclude='*/share/doc' --exclude='*/CHANGELOG.md' --exclude='*/README.md' \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

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
# With the data libraries the agent makes charts and files with
# (requirements-data.txt, from the same script).
COPY requirements.txt requirements-data.txt /tmp/
RUN pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: \
        -r /tmp/requirements.txt -r /tmp/requirements-data.txt \
    && rm /tmp/requirements.txt /tmp/requirements-data.txt

COPY start.sh /usr/local/bin/hermes-sandbox-start
COPY hermes_relay.py /usr/local/bin/hermes-relay
RUN chmod 0555 /usr/local/bin/hermes-sandbox-start /usr/local/bin/hermes-relay \
    && useradd --uid 10001 --create-home --home-dir /home/hermes hermes \
    && mkdir -p /workspace && chown 10001:10001 /workspace

# Everything the agent writes lives in the workspace volume: Hermes's state,
# its home directory and `pip install --user` and `npm install -g` packages.
# Package caches live in .cache, which snapshots leave out; matplotlib draws
# without a display and keeps its font cache in the sandbox's /tmp. Browsers
# are never downloaded by npm packages: they use the system Chromium.
ENV HERMES_HOME=/workspace/.hermes \
    HOME=/workspace/home \
    PYTHONUSERBASE=/workspace/.local \
    PATH=/workspace/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PIP_USER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/workspace/.cache \
    PIP_CACHE_DIR=/workspace/.cache/pip \
    NPM_CONFIG_CACHE=/workspace/.cache/npm \
    NPM_CONFIG_PREFIX=/workspace/.local \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-headless-shell \
    CHROME_BIN=/usr/bin/chromium-headless-shell
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/hermes-sandbox-start"]
